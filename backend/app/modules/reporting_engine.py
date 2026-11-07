"""CampusBite AI v2.0 — Automated 14-Report Business Intelligence & Export Engine (PDF, Excel .xlsx, CSV).

Implements all 14 required reports over real relational records:
 1. daily_sales             — Daily sales report
 2. daily_orders            — Daily order report
 3. daily_payments          — Daily payment report
 4. weekly_sales            — Weekly sales report
 5. monthly_financial       — Monthly financial report
 6. yearly_performance      — Yearly performance report
 7. best_selling_products   — Best-selling product report
 8. least_selling_products  — Least-selling product report
 9. inventory               — Inventory & batch stock report
10. supplier_purchase       — Supplier & purchase order report
11. staff_performance       — Staff performance report
12. customer_activity       — Customer activity report
13. refunds                 — Refund report
14. profit_expense          — Profit & expense report

Supports:
- Native PDF 1.4 multi-page vector/table generation (zero external C dependencies)
- Native OpenXML Excel (.xlsx) workbook generation (opens natively in Excel / Calc / Sheets)
- UTF-8 CSV export & JSON preview
- Automated daily generation & configurable admin email delivery
"""
from __future__ import annotations

from collections import defaultdict
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import io
import json
from typing import Any
from xml.sax.saxutils import escape as xml_escape
import zipfile
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.database import get_db
from app.emailer import send_email
from app.envelope import ApiError, ok
from app.models import (
    CanteenCounter,
    Category,
    GeneratedReport,
    Ingredient,
    InventoryMovement,
    InventoryMovementType,
    OperationalExpense,
    Order,
    OrderStatus,
    OrderStatusHistory,
    Payment,
    PaymentStatus,
    Pickup,
    Product,
    PurchaseOrder,
    Refund,
    StockMovement,
    Supplier,
    User,
    UserRole,
    paise_to_rupees,
    rupees_to_paise,
)
from app.modules.analytics_bi import compute_unit_cost_map
from app.modules.common import as_utc, get_app_setting, parse_json_safe, record_audit, set_app_setting
from app.security import require_roles

router = APIRouter(prefix="/admin/reports", tags=["v1-admin-reports-v2"])

REPORT_DEFINITIONS: dict[str, dict[str, str]] = {
    "daily_sales": {"title": "Daily Sales Report", "cadence": "daily", "description": "Day-by-day paid orders, gross subtotal, discounts, tax, and net revenue."},
    "daily_orders": {"title": "Daily Order Report", "cadence": "daily", "description": "Complete itemized log of orders placed, statuses, counters, and fulfillment times."},
    "daily_payments": {"title": "Daily Payment Report", "cadence": "daily", "description": "UPI / Razorpay payment intents, captures, failures, and gateway references."},
    "weekly_sales": {"title": "Weekly Sales Report", "cadence": "weekly", "description": "ISO week-by-week order volume, unique customers, AOV, and net revenue."},
    "monthly_financial": {"title": "Monthly Financial Report", "cadence": "monthly", "description": "Monthly gross sales, discounts, taxes, refunds, COGS, expenses, and net profit."},
    "yearly_performance": {"title": "Yearly Performance Report", "cadence": "yearly", "description": "Annual revenue, order volume, customer base, expenses, and net margin."},
    "best_selling_products": {"title": "Best-Selling Product Report", "cadence": "product", "description": "Top menu items ranked by units sold, revenue, COGS, and gross contribution."},
    "least_selling_products": {"title": "Least-Selling Product Report", "cadence": "product", "description": "Slowest-moving active menu items (including zero-sale items) for menu optimization."},
    "inventory": {"title": "Inventory & Stock Report", "cadence": "operational", "description": "Current menu stock, raw ingredients, reorder thresholds, and stock status."},
    "supplier_purchase": {"title": "Supplier & Purchase Order Report", "cadence": "procurement", "description": "Active suppliers, purchase orders, line counts, status, and total procurement spend."},
    "staff_performance": {"title": "Staff Performance Report", "cadence": "operational", "description": "Orders accepted/prepared/collected per staff member and average preparation speed."},
    "customer_activity": {"title": "Customer Activity Report", "cadence": "customer", "description": "Student ordering frequency, total spend, average basket value, and last order date."},
    "refunds": {"title": "Refund Report", "cadence": "financial", "description": "Processed and pending refunds with gateway refund IDs, reasons, and amounts."},
    "profit_expense": {"title": "Profit & Expense Report", "cadence": "financial", "description": "Comprehensive daily P&L combining net sales, COGS, wastage, refunds, and operating expenses."},
}


# ==============================================================================
# Pure-Python Standards-Compliant PDF 1.4 & OpenXML (.xlsx) Renderers
# ==============================================================================
def _pdf_safe_text(val: Any) -> str:
    s = str(val if val is not None else "")
    s = s.replace("₹", "INR ").replace("—", "-").replace("–", "-").replace("•", "*")
    # Keep printable ASCII to guarantee clean rendering in PDF Type1 Helvetica
    s = s.encode("ascii", "replace").decode("ascii")
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def render_pdf_document(
    title: str,
    subtitle: str,
    summary_pairs: list[tuple[str, Any]],
    columns: list[str],
    rows: list[list[Any]],
) -> bytes:
    """Generate a multi-page landscape A4 PDF 1.4 report with header, KPI summary & paginated table."""
    page_w, page_h = 842, 595  # A4 Landscape in points
    margin_x = 32
    usable_w = page_w - 2 * margin_x
    col_count = max(1, len(columns))
    col_w = usable_w / col_count
    max_chars_per_col = max(8, int(col_w / 4.8) - 2)

    rows_per_first_page = 22
    rows_per_next_page = 30

    chunks: list[tuple[bool, list[list[Any]]]] = []
    if not rows:
        chunks.append((True, []))
    else:
        chunks.append((True, rows[:rows_per_first_page]))
        idx = rows_per_first_page
        while idx < len(rows):
            chunks.append((False, rows[idx : idx + rows_per_next_page]))
            idx += rows_per_next_page

    total_pages = len(chunks)
    page_streams: list[bytes] = []

    for page_idx, (is_first, page_rows) in enumerate(chunks, start=1):
        cmds: list[str] = []
        # Top header bar
        cmds.append("0.08 0.14 0.26 rg")
        cmds.append(f"{margin_x} {page_h - 62} {usable_w} 38 re f")
        cmds.append("1 1 1 rg")
        cmds.append(f"BT /F2 13 Tf {margin_x + 10} {page_h - 42} Td ({_pdf_safe_text('CampusBite AI v2.0 - ' + title)}) Tj ET")
        cmds.append(f"BT /F1 8.5 Tf {margin_x + 10} {page_h - 55} Td ({_pdf_safe_text(subtitle)}) Tj ET")
        cmds.append(f"BT /F1 8.5 Tf {page_w - margin_x - 95} {page_h - 47} Td (Page {page_idx} of {total_pages}) Tj ET")

        y = page_h - 80
        if is_first and summary_pairs:
            cmds.append("0.95 0.97 0.99 rg")
            cmds.append(f"{margin_x} {y - 34} {usable_w} 38 re f")
            cmds.append("0.12 0.18 0.28 rg")
            per_row = min(5, max(1, len(summary_pairs)))
            box_w = usable_w / per_row
            for s_idx, (k_lbl, v_val) in enumerate(summary_pairs[:10]):
                r_line = s_idx // per_row
                c_pos = s_idx % per_row
                sx = margin_x + 8 + c_pos * box_w
                sy = y - 10 - (r_line * 16)
                txt = f"{k_lbl}: {v_val}"[:36]
                cmds.append(f"BT /F2 8.5 Tf {sx:.1f} {sy:.1f} Td ({_pdf_safe_text(txt)}) Tj ET")
            y -= 50

        # Table header row
        cmds.append("0.16 0.26 0.42 rg")
        cmds.append(f"{margin_x} {y - 16} {usable_w} 18 re f")
        cmds.append("1 1 1 rg")
        for c_i, col_name in enumerate(columns):
            cx = margin_x + 4 + c_i * col_w
            c_txt = str(col_name)[:max_chars_per_col]
            cmds.append(f"BT /F2 8 Tf {cx:.1f} {y - 11:.1f} Td ({_pdf_safe_text(c_txt)}) Tj ET")
        y -= 20

        if not page_rows:
            cmds.append("0.3 0.35 0.4 rg")
            cmds.append(f"BT /F1 9 Tf {margin_x + 8} {y - 14} Td (No records matched the selected filter window.) Tj ET")
        else:
            for r_i, row_vals in enumerate(page_rows):
                if r_i % 2 == 1:
                    cmds.append("0.96 0.97 0.98 rg")
                    cmds.append(f"{margin_x} {y - 13} {usable_w} 14 re f")
                cmds.append("0.1 0.12 0.16 rg")
                for c_i in range(col_count):
                    cell_val = row_vals[c_i] if c_i < len(row_vals) else ""
                    cx = margin_x + 4 + c_i * col_w
                    c_txt = str(cell_val if cell_val is not None else "")[:max_chars_per_col]
                    cmds.append(f"BT /F1 7.8 Tf {cx:.1f} {y - 9.5:.1f} Td ({_pdf_safe_text(c_txt)}) Tj ET")
                y -= 14

        # Footer note
        cmds.append("0.45 0.5 0.56 rg")
        cmds.append(
            f"BT /F1 7.5 Tf {margin_x} 18 Td "
            f"({_pdf_safe_text('Generated from authoritative CampusBite AI v2.0 database records. Zero fabricated figures.')}) Tj ET"
        )
        page_streams.append("\n".join(cmds).encode("ascii"))

    # Assemble PDF objects:
    # 1: Catalog, 2: Pages, 3: Font F1 (Helvetica), 4: Font F2 (Helvetica-Bold)
    # Then for each page i: (5 + 2*i) = Page object, (6 + 2*i) = Content stream
    objects: list[bytes] = []
    page_obj_ids = [5 + 2 * i for i in range(total_pages)]
    kids_str = " ".join(f"{pid} 0 R" for pid in page_obj_ids)

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids_str}] /Count {total_pages} >>".encode("ascii"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")

    for i, stream_bytes in enumerate(page_streams):
        content_id = 6 + 2 * i
        page_dict = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w} {page_h}] "
            f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode("ascii")
        stream_obj = f"<< /Length {len(stream_bytes)} >>\nstream\n".encode("ascii") + stream_bytes + b"\nendstream"
        objects.append(page_dict)
        objects.append(stream_obj)

    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = [0]
    for obj_num, obj_body in enumerate(objects, start=1):
        offsets.append(buf.tell())
        buf.write(f"{obj_num} 0 obj\n".encode("ascii"))
        buf.write(obj_body)
        buf.write(b"\nendobj\n")

    xref_pos = buf.tell()
    total_objs = len(objects) + 1
    buf.write(f"xref\n0 {total_objs}\n".encode("ascii"))
    buf.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        buf.write(f"{off:010d} 00000 n \n".encode("ascii"))
    buf.write(
        f"trailer\n<< /Size {total_objs} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii")
    )
    return buf.getvalue()


def _xlsx_col_letter(col_idx_1based: int) -> str:
    res = ""
    n = col_idx_1based
    while n > 0:
        n, rem = divmod(n - 1, 26)
        res = chr(65 + rem) + res
    return res


def render_xlsx_workbook(
    sheet_title: str,
    report_title: str,
    subtitle: str,
    summary_pairs: list[tuple[str, Any]],
    columns: list[str],
    rows: list[list[Any]],
) -> bytes:
    """Generate a native OpenXML (.xlsx) workbook with zero external library dependencies."""
    clean_sheet = "".join(ch for ch in sheet_title if ch not in "[]:*?/\\")[:31] or "Report"

    def _cell_xml(r_num: int, c_num: int, val: Any, style_id: int = 0) -> str:
        ref = f"{_xlsx_col_letter(c_num)}{r_num}"
        s_attr = f' s="{style_id}"' if style_id else ""
        if isinstance(val, bool):
            return f'<c r="{ref}"{s_attr} t="inlineStr"><is><t>{"TRUE" if val else "FALSE"}</t></is></c>'
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return f'<c r="{ref}"{s_attr} t="n"><v>{val}</v></c>'
        txt = xml_escape(str(val if val is not None else ""))
        return f'<c r="{ref}"{s_attr} t="inlineStr"><is><t xml:space="preserve">{txt}</t></is></c>'

    xml_rows: list[str] = []
    cur_r = 1
    xml_rows.append(f'<row r="{cur_r}">{_cell_xml(cur_r, 1, "CampusBite AI v2.0 — " + report_title, 1)}</row>')
    cur_r += 1
    xml_rows.append(f'<row r="{cur_r}">{_cell_xml(cur_r, 1, subtitle, 0)}</row>')
    cur_r += 2

    if summary_pairs:
        for k_lbl, v_val in summary_pairs:
            xml_rows.append(
                f'<row r="{cur_r}">{_cell_xml(cur_r, 1, k_lbl, 1)}{_cell_xml(cur_r, 2, v_val, 0)}</row>'
            )
            cur_r += 1
        cur_r += 1

    # Header row
    hdr_cells = "".join(_cell_xml(cur_r, idx, col, 1) for idx, col in enumerate(columns, start=1))
    xml_rows.append(f'<row r="{cur_r}">{hdr_cells}</row>')
    cur_r += 1

    for data_row in rows:
        cells = "".join(_cell_xml(cur_r, idx, val, 0) for idx, val in enumerate(data_row, start=1))
        xml_rows.append(f'<row r="{cur_r}">{cells}</row>')
        cur_r += 1

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>"
        + "".join(xml_rows)
        + "</sheetData></worksheet>"
    )

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{xml_escape(clean_sheet)}" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    )
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2">'
        '<font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font>'
        "</fonts>"
        '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
        "</cellXfs>"
        "</styleSheet>"
    )

    out = io.BytesIO()
    with zipfile.ZipFile(out, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/styles.xml", styles_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return out.getvalue()


def render_csv_bytes(columns: list[str], rows: list[list[Any]]) -> bytes:
    sio = io.StringIO(newline="")
    writer = csv.writer(sio)
    writer.writerow(columns)
    for r in rows:
        writer.writerow(r)
    return sio.getvalue().encode("utf-8")


# ==============================================================================
# 14 Authoritative Report Dataset Builders
# ==============================================================================
def _date_bounds(start_date: date | None, end_date: date | None, default_days: int = 30) -> tuple[date, date, datetime, datetime]:
    tz = ZoneInfo(get_settings().canteen_timezone)
    today_local = datetime.now(tz).date()
    end_d = end_date or today_local
    start_d = start_date or (end_d - timedelta(days=default_days - 1))
    if start_d > end_d:
        raise ApiError(422, "INVALID_DATE_RANGE", "startDate cannot be after endDate.")
    start_utc = datetime(start_d.year, start_d.month, start_d.day, tzinfo=tz).astimezone(timezone.utc)
    end_utc = datetime(end_d.year, end_d.month, end_d.day, 23, 59, 59, tzinfo=tz).astimezone(timezone.utc)
    return start_d, end_d, start_utc, end_utc


def _rupees_str(paise: int) -> str:
    return f"INR {paise_to_rupees(paise)}"


def build_report_dataset(
    db: Session,
    report_type: str,
    start_date: date | None = None,
    end_date: date | None = None,
    counter_id: int | None = None,
) -> dict[str, Any]:
    rt = report_type.strip().lower()
    if rt not in REPORT_DEFINITIONS:
        raise ApiError(422, "UNKNOWN_REPORT_TYPE", f"Unsupported reportType '{report_type}'.")

    default_window = 365 if rt in {"yearly_performance", "monthly_financial"} else (84 if rt == "weekly_sales" else 30)
    start_d, end_d, start_utc, end_utc = _date_bounds(start_date, end_date, default_days=default_window)
    tz = ZoneInfo(get_settings().canteen_timezone)
    meta = REPORT_DEFINITIONS[rt]
    unit_costs = compute_unit_cost_map(db)
    counters = {c.id: c.name for c in db.scalars(select(CanteenCounter)).all()}
    categories = {c.id: c.name for c in db.scalars(select(Category)).all()}

    def _local_day(dt: datetime | None) -> date:
        u = as_utc(dt)
        return u.astimezone(tz).date() if u else start_d

    # Load orders in window where needed
    o_stmt = select(Order).options(joinedload(Order.items), joinedload(Order.user), joinedload(Order.payments)).where(
        Order.created_at >= start_utc,
        Order.created_at <= end_utc,
    )
    if counter_id is not None:
        o_stmt = o_stmt.where(Order.counter_id == counter_id)
    orders = db.scalars(o_stmt.order_by(Order.created_at.desc())).unique().all()
    paid_orders = [o for o in orders if o.payment_status == PaymentStatus.PAID]

    columns: list[str] = []
    rows: list[list[Any]] = []
    summary_pairs: list[tuple[str, Any]] = []

    if rt == "daily_sales":
        by_day: dict[str, dict[str, Any]] = defaultdict(lambda: {"orders": 0, "customers": set(), "sub": 0, "disc": 0, "tax": 0, "net": 0})
        for o in paid_orders:
            dk = _local_day(o.created_at).isoformat()
            b = by_day[dk]
            b["orders"] += 1
            b["customers"].add(o.user_id)
            b["sub"] += int(o.subtotal_paise or rupees_to_paise(o.subtotal))
            b["disc"] += int(o.discount_paise or rupees_to_paise(o.discount_amount))
            b["tax"] += int(o.tax_paise or rupees_to_paise(o.tax_amount))
            b["net"] += o.effective_total_paise
        columns = ["Date", "Paid Orders", "Unique Customers", "Gross Subtotal (INR)", "Discounts (INR)", "Tax (INR)", "Net Revenue (INR)"]
        for dk in sorted(by_day.keys(), reverse=True):
            b = by_day[dk]
            rows.append([
                dk,
                b["orders"],
                len(b["customers"]),
                float(paise_to_rupees(b["sub"])),
                float(paise_to_rupees(b["disc"])),
                float(paise_to_rupees(b["tax"])),
                float(paise_to_rupees(b["net"])),
            ])
        total_net = sum(o.effective_total_paise for o in paid_orders)
        summary_pairs = [
            ("Paid Orders", len(paid_orders)),
            ("Net Revenue", _rupees_str(total_net)),
            ("Active Days", len(by_day)),
        ]

    elif rt == "daily_orders":
        columns = ["Order #", "Created At", "Counter", "Customer", "Items", "Status", "Payment Status", "Total (INR)"]
        for o in orders:
            u_dt = as_utc(o.created_at)
            ts = u_dt.astimezone(tz).strftime("%Y-%m-%d %H:%M") if u_dt else ""
            item_summary = "; ".join(f"{it.quantity}x {it.product_name}" for it in o.items)[:80]
            rows.append([
                o.order_number or f"ACE-{o.id:06d}",
                ts,
                counters.get(o.counter_id or 0, "Main"),
                o.user.full_name if o.user else f"User #{o.user_id}",
                item_summary,
                o.status.value.upper(),
                o.payment_status.value.upper(),
                float(paise_to_rupees(o.effective_total_paise)),
            ])
        summary_pairs = [
            ("Total Orders", len(orders)),
            ("Paid Orders", len(paid_orders)),
            ("Completed/Collected", sum(1 for o in orders if o.status in {OrderStatus.COMPLETED, OrderStatus.COLLECTED})),
            ("Cancelled", sum(1 for o in orders if o.status in {OrderStatus.CANCELLED, OrderStatus.REFUND_PENDING})),
        ]

    elif rt == "daily_payments":
        p_stmt = select(Payment).options(joinedload(Payment.order)).where(Payment.created_at >= start_utc, Payment.created_at <= end_utc).order_by(Payment.created_at.desc())
        payments = db.scalars(p_stmt).unique().all()
        if counter_id is not None:
            payments = [p for p in payments if p.order and p.order.counter_id == counter_id]
        columns = ["Payment ID", "Order #", "Provider", "Gateway Order ID", "Gateway Payment ID", "Status", "Late Capture", "Amount (INR)", "Created At"]
        succ_paise = 0
        fail_cnt = 0
        for p in payments:
            if p.status == "SUCCESS":
                succ_paise += int(p.amount_paise)
            elif p.status in {"FAILED", "EXPIRED"}:
                fail_cnt += 1
            u_dt = as_utc(p.created_at)
            rows.append([
                p.id,
                p.order.order_number if p.order else f"#{p.order_id}",
                p.provider,
                p.provider_order_id,
                p.provider_payment_id or "-",
                p.status,
                "YES" if p.late_capture_flagged else "NO",
                float(paise_to_rupees(p.amount_paise)),
                u_dt.astimezone(tz).strftime("%Y-%m-%d %H:%M") if u_dt else "",
            ])
        summary_pairs = [
            ("Payment Records", len(payments)),
            ("Captured Value", _rupees_str(succ_paise)),
            ("Failed/Expired", fail_cnt),
        ]

    elif rt == "weekly_sales":
        by_wk: dict[str, dict[str, Any]] = defaultdict(lambda: {"orders": 0, "buyers": set(), "net": 0})
        for o in paid_orders:
            od = _local_day(o.created_at)
            yr, wk, _ = od.isocalendar()
            key = f"{yr}-W{wk:02d}"
            by_wk[key]["orders"] += 1
            by_wk[key]["buyers"].add(o.user_id)
            by_wk[key]["net"] += o.effective_total_paise
        columns = ["ISO Week", "Paid Orders", "Unique Customers", "Avg Order Value (INR)", "Net Revenue (INR)"]
        for wk in sorted(by_wk.keys(), reverse=True):
            b = by_wk[wk]
            aov = int(round(b["net"] / b["orders"])) if b["orders"] else 0
            rows.append([wk, b["orders"], len(b["buyers"]), float(paise_to_rupees(aov)), float(paise_to_rupees(b["net"]))])
        summary_pairs = [
            ("Weeks Covered", len(by_wk)),
            ("Total Paid Orders", len(paid_orders)),
            ("Total Revenue", _rupees_str(sum(o.effective_total_paise for o in paid_orders))),
        ]

    elif rt in {"monthly_financial", "yearly_performance"}:
        is_yearly = rt == "yearly_performance"
        expenses = db.scalars(select(OperationalExpense).where(OperationalExpense.expense_date >= start_d, OperationalExpense.expense_date <= end_d)).all()
        refunds = db.scalars(select(Refund).where(Refund.created_at >= start_utc, Refund.created_at <= end_utc)).all()
        buckets: dict[str, dict[str, int]] = defaultdict(lambda: {"orders": 0, "rev": 0, "disc": 0, "tax": 0, "cogs": 0, "exp": 0, "ref": 0})

        for o in paid_orders:
            od = _local_day(o.created_at)
            k = f"{od.year:04d}" if is_yearly else f"{od.year:04d}-{od.month:02d}"
            buckets[k]["orders"] += 1
            buckets[k]["rev"] += o.effective_total_paise
            buckets[k]["disc"] += int(o.discount_paise or 0)
            buckets[k]["tax"] += int(o.tax_paise or 0)
            for it in o.items:
                buckets[k]["cogs"] += int(it.quantity) * unit_costs.get(it.product_id, 0)

        for ex in expenses:
            if counter_id is not None and ex.counter_id is not None and ex.counter_id != counter_id:
                continue
            k = f"{ex.expense_date.year:04d}" if is_yearly else f"{ex.expense_date.year:04d}-{ex.expense_date.month:02d}"
            buckets[k]["exp"] += int(ex.amount_paise)

        for rf in refunds:
            if rf.status in {"PROCESSED", "SUCCESS"}:
                rd = _local_day(rf.created_at)
                k = f"{rd.year:04d}" if is_yearly else f"{rd.year:04d}-{rd.month:02d}"
                buckets[k]["ref"] += int(rf.amount_paise or rupees_to_paise(rf.amount))

        columns = ["Year" if is_yearly else "Month", "Paid Orders", "Net Revenue (INR)", "COGS (INR)", "Operating Expenses (INR)", "Refunds (INR)", "Net Profit (INR)"]
        tot_rev = 0
        tot_prof = 0
        for k in sorted(buckets.keys(), reverse=True):
            b = buckets[k]
            prof = b["rev"] - b["cogs"] - b["exp"] - b["ref"]
            tot_rev += b["rev"]
            tot_prof += prof
            rows.append([
                k,
                b["orders"],
                float(paise_to_rupees(b["rev"])),
                float(paise_to_rupees(b["cogs"])),
                float(paise_to_rupees(b["exp"])),
                float(paise_to_rupees(b["ref"])),
                float(paise_to_rupees(prof)),
            ])
        summary_pairs = [
            ("Periods", len(buckets)),
            ("Total Revenue", _rupees_str(tot_rev)),
            ("Estimated Net Profit", _rupees_str(tot_prof)),
        ]

    elif rt in {"best_selling_products", "least_selling_products"}:
        all_prods = db.scalars(select(Product).where(Product.is_archived.is_(False))).all()
        if counter_id is not None:
            all_prods = [p for p in all_prods if p.counter_id is None or p.counter_id == counter_id]
        units_by_p: dict[int, int] = defaultdict(int)
        rev_by_p: dict[int, int] = defaultdict(int)
        for o in paid_orders:
            for it in o.items:
                units_by_p[it.product_id] += int(it.quantity)
                rev_by_p[it.product_id] += int(it.line_total_paise if it.line_total_paise is not None else rupees_to_paise(it.line_total))

        prod_stats = []
        for p in all_prods:
            u = units_by_p.get(p.id, 0)
            rv = rev_by_p.get(p.id, 0)
            cg = u * unit_costs.get(p.id, 0)
            prod_stats.append((u, rv, p, rv - cg))

        if rt == "best_selling_products":
            prod_stats.sort(key=lambda x: (-x[0], -x[1], x[2].name))
        else:
            prod_stats.sort(key=lambda x: (x[0], x[1], x[2].name))

        columns = ["Product ID", "Item Name", "Category", "Unit Price (INR)", "Units Sold", "Revenue (INR)", "Gross Margin (INR)", "Stock Remaining"]
        for u, rv, p, margin in prod_stats[:50]:
            rows.append([
                p.id,
                p.name,
                categories.get(p.category_id, "General"),
                float(paise_to_rupees(p.effective_price_paise)),
                u,
                float(paise_to_rupees(rv)),
                float(paise_to_rupees(margin)),
                p.stock_quantity,
            ])
        summary_pairs = [
            ("Catalog Items Evaluated", len(prod_stats)),
            ("Total Units Sold", sum(x[0] for x in prod_stats)),
        ]

    elif rt == "inventory":
        prods = db.scalars(select(Product).where(Product.is_archived.is_(False)).order_by(Product.stock_quantity.asc(), Product.name.asc())).all()
        ings = db.scalars(select(Ingredient).where(Ingredient.is_active.is_(True)).order_by(Ingredient.name.asc())).all()
        columns = ["Type", "ID", "Name", "Current Stock", "Unit", "Reorder Threshold", "Unit Price/Cost (INR)", "Status"]
        low_count = 0
        for p in prods:
            is_low = p.stock_quantity <= p.low_stock_threshold
            if is_low:
                low_count += 1
            rows.append([
                "Menu Item",
                p.id,
                p.name,
                p.stock_quantity,
                "units",
                p.low_stock_threshold,
                float(paise_to_rupees(p.effective_price_paise)),
                "LOW STOCK" if is_low else "OK",
            ])
        for ing in ings:
            is_low = Decimal(ing.current_quantity) <= Decimal(ing.reorder_level)
            if is_low:
                low_count += 1
            rows.append([
                "Ingredient",
                ing.id,
                ing.name,
                float(ing.current_quantity),
                ing.unit,
                float(ing.reorder_level),
                float(ing.cost_per_unit or 0),
                "LOW STOCK" if is_low else "OK",
            ])
        summary_pairs = [
            ("Menu Items", len(prods)),
            ("Raw Ingredients", len(ings)),
            ("Low-Stock Alerts", low_count),
        ]

    elif rt == "supplier_purchase":
        suppliers = {s.id: s for s in db.scalars(select(Supplier)).all()}
        pos = db.scalars(select(PurchaseOrder).where(PurchaseOrder.created_at >= start_utc, PurchaseOrder.created_at <= end_utc).order_by(PurchaseOrder.created_at.desc())).all()
        columns = ["PO ID", "Supplier", "Contact", "Status", "Line Count", "Total Cost (INR)", "Created At", "Received At"]
        total_spend_paise = 0
        for po in pos:
            sup = suppliers.get(po.supplier_id)
            lines = parse_json_safe(po.lines_json, [])
            cost_p = int(po.total_cost_paise or 0)
            if cost_p == 0 and isinstance(lines, list):
                for ln in lines:
                    if isinstance(ln, dict):
                        cost_p += rupees_to_paise(Decimal(str(ln.get("quantity", 0))) * Decimal(str(ln.get("unit_cost", ln.get("unitCost", 0)) or 0)))
            total_spend_paise += cost_p
            c_dt = as_utc(po.created_at)
            r_dt = as_utc(po.received_at)
            rows.append([
                f"PO-{po.id:04d}",
                sup.name if sup else f"Supplier #{po.supplier_id}",
                (sup.phone or sup.email or "-") if sup else "-",
                po.status.upper(),
                len(lines) if isinstance(lines, list) else 0,
                float(paise_to_rupees(cost_p)),
                c_dt.astimezone(tz).strftime("%Y-%m-%d") if c_dt else "",
                r_dt.astimezone(tz).strftime("%Y-%m-%d") if r_dt else "-",
            ])
        summary_pairs = [
            ("Registered Suppliers", len(suppliers)),
            ("Purchase Orders", len(pos)),
            ("Total PO Spend", _rupees_str(total_spend_paise)),
        ]

    elif rt == "staff_performance":
        staff_list = db.scalars(select(User).where(User.role.in_([UserRole.STAFF, UserRole.ADMIN])).order_by(User.full_name.asc())).all()
        hist = db.scalars(select(OrderStatusHistory).where(OrderStatusHistory.created_at >= start_utc, OrderStatusHistory.created_at <= end_utc)).all()
        pickups = db.scalars(select(Pickup).where(Pickup.collected_at >= start_utc, Pickup.collected_at <= end_utc)).all()

        accepted_by: dict[int, int] = defaultdict(int)
        ready_by: dict[int, int] = defaultdict(int)
        for h in hist:
            if not h.actor_user_id:
                continue
            st = h.new_status.upper()
            if st == "ACCEPTED":
                accepted_by[h.actor_user_id] += 1
            elif st == "READY":
                ready_by[h.actor_user_id] += 1
        collected_by: dict[int, int] = defaultdict(int)
        for pk in pickups:
            if pk.collected_by:
                collected_by[pk.collected_by] += 1

        prep_times: dict[int, list[float]] = defaultdict(list)
        handled_cnt: dict[int, int] = defaultdict(int)
        for o in paid_orders:
            if o.handled_by_staff_id:
                handled_cnt[o.handled_by_staff_id] += 1
                if o.ready_at and o.created_at:
                    mins = ((as_utc(o.ready_at) - as_utc(o.created_at)).total_seconds() / 60.0)  # type: ignore[operator]
                    if 0.5 <= mins <= 180:
                        prep_times[o.handled_by_staff_id].append(mins)

        columns = ["Staff ID", "Full Name", "Role", "Orders Handled", "Accepted", "Marked Ready", "QR Handovers", "Avg Prep Minutes"]
        for u in staff_list:
            p_list = prep_times.get(u.id, [])
            avg_m = round(sum(p_list) / len(p_list), 2) if p_list else 0.0
            total_h = max(handled_cnt.get(u.id, 0), accepted_by.get(u.id, 0), ready_by.get(u.id, 0))
            rows.append([
                u.id,
                u.full_name,
                u.role.value.upper(),
                total_h,
                accepted_by.get(u.id, 0),
                ready_by.get(u.id, 0),
                collected_by.get(u.id, 0),
                avg_m,
            ])
        summary_pairs = [
            ("Active Staff & Admins", len(staff_list)),
            ("Total QR Handovers", sum(collected_by.values())),
        ]

    elif rt == "customer_activity":
        students = {u.id: u for u in db.scalars(select(User).where(User.role == UserRole.STUDENT)).all()}
        cust_stats: dict[int, dict[str, Any]] = defaultdict(lambda: {"orders": 0, "spend": 0, "last": None})
        for o in paid_orders:
            st = cust_stats[o.user_id]
            st["orders"] += 1
            st["spend"] += o.effective_total_paise
            od = _local_day(o.created_at)
            if st["last"] is None or od > st["last"]:
                st["last"] = od
        columns = ["User ID", "Student Name", "Email", "Paid Orders", "Avg Order Value (INR)", "Total Spend (INR)", "Last Order Date"]
        sorted_cust = sorted(cust_stats.items(), key=lambda kv: (-kv[1]["spend"], -kv[1]["orders"]))
        for uid, st in sorted_cust[:200]:
            u_obj = students.get(uid)
            aov = int(round(st["spend"] / st["orders"])) if st["orders"] else 0
            rows.append([
                uid,
                u_obj.full_name if u_obj else f"User #{uid}",
                u_obj.email if u_obj else "-",
                st["orders"],
                float(paise_to_rupees(aov)),
                float(paise_to_rupees(st["spend"])),
                st["last"].isoformat() if st["last"] else "-",
            ])
        summary_pairs = [
            ("Registered Students", len(students)),
            ("Ordering Customers", len(cust_stats)),
            ("Repeat Customers (2+)", sum(1 for v in cust_stats.values() if v["orders"] >= 2)),
        ]

    elif rt == "refunds":
        refunds = db.scalars(select(Refund).options(joinedload(Refund.order)).where(Refund.created_at >= start_utc, Refund.created_at <= end_utc).order_by(Refund.created_at.desc())).all()
        columns = ["Refund ID", "Order #", "Gateway Refund ID", "Status", "Reason", "Amount (INR)", "Created At"]
        tot_ref = 0
        for rf in refunds:
            ap = int(rf.amount_paise or rupees_to_paise(rf.amount))
            tot_ref += ap
            c_dt = as_utc(rf.created_at)
            rows.append([
                rf.id,
                rf.order.order_number if rf.order else f"#{rf.order_id}",
                rf.provider_refund_id or "-",
                rf.status,
                rf.reason,
                float(paise_to_rupees(ap)),
                c_dt.astimezone(tz).strftime("%Y-%m-%d %H:%M") if c_dt else "",
            ])
        summary_pairs = [
            ("Total Refund Records", len(refunds)),
            ("Total Refund Amount", _rupees_str(tot_ref)),
        ]

    elif rt == "profit_expense":
        expenses = db.scalars(select(OperationalExpense).where(OperationalExpense.expense_date >= start_d, OperationalExpense.expense_date <= end_d)).all()
        refunds = db.scalars(select(Refund).where(Refund.created_at >= start_utc, Refund.created_at <= end_utc)).all()
        p_waste = db.scalars(select(InventoryMovement).options(joinedload(InventoryMovement.product)).where(InventoryMovement.movement_type == InventoryMovementType.WASTE, InventoryMovement.created_at >= start_utc, InventoryMovement.created_at <= end_utc)).all()
        i_waste = db.scalars(select(StockMovement).options(joinedload(StockMovement.ingredient)).where(StockMovement.movement_type == "wastage", StockMovement.created_at >= start_utc, StockMovement.created_at <= end_utc)).all()

        pnl_by_day: dict[str, dict[str, int]] = defaultdict(lambda: {"rev": 0, "cogs": 0, "waste": 0, "exp": 0, "ref": 0})
        for o in paid_orders:
            dk = _local_day(o.created_at).isoformat()
            pnl_by_day[dk]["rev"] += o.effective_total_paise
            for it in o.items:
                pnl_by_day[dk]["cogs"] += int(it.quantity) * unit_costs.get(it.product_id, 0)
        for ex in expenses:
            if counter_id is not None and ex.counter_id is not None and ex.counter_id != counter_id:
                continue
            pnl_by_day[ex.expense_date.isoformat()]["exp"] += int(ex.amount_paise)
        for rf in refunds:
            if rf.status in {"PROCESSED", "SUCCESS"}:
                pnl_by_day[_local_day(rf.created_at).isoformat()]["ref"] += int(rf.amount_paise or rupees_to_paise(rf.amount))
        for mv in p_waste:
            u_cost = unit_costs.get(mv.product_id, 0) or (mv.product.effective_price_paise if mv.product else 0)
            pnl_by_day[_local_day(mv.created_at).isoformat()]["waste"] += abs(int(mv.quantity_delta)) * u_cost
        for sm in i_waste:
            if sm.ingredient and sm.ingredient.cost_per_unit is not None:
                pnl_by_day[_local_day(sm.created_at).isoformat()]["waste"] += rupees_to_paise(abs(Decimal(sm.quantity_delta)) * Decimal(sm.ingredient.cost_per_unit))

        columns = ["Date", "Net Sales (INR)", "COGS (INR)", "Wastage Loss (INR)", "Operating Expenses (INR)", "Refunds (INR)", "Net Profit (INR)"]
        tot_rev = tot_exp_all = tot_prof = 0
        for dk in sorted(pnl_by_day.keys(), reverse=True):
            b = pnl_by_day[dk]
            exp_sum = b["cogs"] + b["waste"] + b["exp"] + b["ref"]
            prof = b["rev"] - exp_sum
            tot_rev += b["rev"]
            tot_exp_all += exp_sum
            tot_prof += prof
            rows.append([
                dk,
                float(paise_to_rupees(b["rev"])),
                float(paise_to_rupees(b["cogs"])),
                float(paise_to_rupees(b["waste"])),
                float(paise_to_rupees(b["exp"])),
                float(paise_to_rupees(b["ref"])),
                float(paise_to_rupees(prof)),
            ])
        summary_pairs = [
            ("Total Net Sales", _rupees_str(tot_rev)),
            ("Total Costs & Expenses", _rupees_str(tot_exp_all)),
            ("Estimated Net Profit", _rupees_str(tot_prof)),
        ]

    subtitle = f"Period: {start_d.isoformat()} to {end_d.isoformat()}" + (f" | Counter #{counter_id}" if counter_id else " | All Counters")
    return {
        "reportType": rt,
        "title": meta["title"],
        "description": meta["description"],
        "startDate": start_d.isoformat(),
        "endDate": end_d.isoformat(),
        "subtitle": subtitle,
        "summary": [{"label": k, "value": v} for k, v in summary_pairs],
        "summaryPairs": summary_pairs,
        "columns": columns,
        "rows": rows,
        "rowCount": len(rows),
    }


def generate_daily_reports_job(
    db: Session,
    target_date: date | None = None,
    email_recipients: list[str] | None = None,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    """Generate automated daily executive reports and optionally email summary to admins."""
    cfg = get_settings()
    tz = ZoneInfo(cfg.canteen_timezone)
    day = target_date or datetime.now(tz).date()

    configured_str = get_app_setting(db, "daily_report_emails", cfg.automated_daily_report_emails)
    recipients = email_recipients if email_recipients is not None else [
        e.strip() for e in configured_str.split(",") if e.strip() and "@" in e
    ]

    daily_bundle = ("daily_sales", "daily_orders", "daily_payments", "inventory", "profit_expense")
    generated_items = []
    email_lines = [
        f"CampusBite AI v2.0 — Automated Daily Executive Summary ({day.isoformat()})",
        "=" * 68,
        "",
    ]

    for rt in daily_bundle:
        ds = build_report_dataset(db, rt, start_date=day, end_date=day)
        rec = GeneratedReport(
            report_type=rt,
            period_label=day.isoformat(),
            start_date=day,
            end_date=day,
            format="automated_bundle",
            row_count=ds["rowCount"],
            emailed_to=",".join(recipients) if recipients else None,
            summary_json=json.dumps({k: str(v) for k, v in ds["summaryPairs"]}, separators=(",", ":")),
            actor_user_id=actor_user_id,
        )
        db.add(rec)
        db.flush()
        generated_items.append({
            "id": rec.id,
            "reportType": rt,
            "title": ds["title"],
            "rowCount": ds["rowCount"],
            "summary": ds["summary"],
        })
        email_lines.append(f"[{ds['title']}] ({ds['rowCount']} rows)")
        for lbl, val in ds["summaryPairs"]:
            email_lines.append(f"  - {lbl}: {val}")
        email_lines.append("")

    record_audit(
        db,
        "reports.automated_daily_generated",
        "generated_reports",
        day.isoformat(),
        actor_user_id,
        {"reports": list(daily_bundle), "recipients_count": len(recipients)},
    )
    db.commit()

    email_sent = False
    if recipients:
        body = "\n".join(email_lines) + "\nLog in to the Admin Portal (/admin) to download full PDF, Excel (.xlsx), or CSV files.\n"
        for addr in recipients:
            try:
                send_email(addr, f"CampusBite AI v2.0 Daily Reports — {day.isoformat()}", body)
                email_sent = True
            except Exception:
                pass

    return {
        "date": day.isoformat(),
        "generatedCount": len(generated_items),
        "reports": generated_items,
        "recipients": recipients,
        "emailDispatched": email_sent,
    }


# ==============================================================================
# API Endpoints (/api/v1/admin/reports/*)
# ==============================================================================
class DailyScheduleConfigIn(BaseModel):
    dailyReportEmails: str = Field(default="", max_length=450)


class GenerateDailyReportsIn(BaseModel):
    targetDate: date | None = None
    recipients: list[str] | None = None


@router.get("/catalog")
def get_reports_catalog_v1(
    request: Request,
    _: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    cfg = get_settings()
    emails_cfg = get_app_setting(db, "daily_report_emails", cfg.automated_daily_report_emails)
    recent = db.scalars(select(GeneratedReport).order_by(GeneratedReport.created_at.desc()).limit(30)).all()
    return ok(
        request,
        {
            "reportTypes": [
                {"reportType": k, **v}
                for k, v in REPORT_DEFINITIONS.items()
            ],
            "supportedFormats": ["pdf", "xlsx", "csv"],
            "dailyReportEmails": emails_cfg,
            "smtpConfigured": bool(cfg.smtp_host),
            "recentGeneratedReports": [
                {
                    "id": r.id,
                    "reportType": r.report_type,
                    "periodLabel": r.period_label,
                    "startDate": r.start_date.isoformat(),
                    "endDate": r.end_date.isoformat(),
                    "format": r.format,
                    "rowCount": r.row_count,
                    "emailedTo": r.emailed_to,
                    "summary": parse_json_safe(r.summary_json, {}),
                    "createdAt": as_utc(r.created_at),
                }
                for r in recent
            ],
        },
    )


@router.put("/schedule")
def update_daily_report_schedule_v1(
    payload: DailyScheduleConfigIn,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    cleaned = ",".join(e.strip() for e in payload.dailyReportEmails.split(",") if e.strip())
    set_app_setting(db, "daily_report_emails", cleaned)
    record_audit(db, "admin.report_schedule_updated", "settings", "daily_report_emails", admin.id, {"emails": cleaned})
    db.commit()
    return ok(request, {"dailyReportEmails": cleaned})


@router.get("/preview")
def preview_report_v1(
    request: Request,
    reportType: str = Query(default="daily_sales"),
    startDate: date | None = None,
    endDate: date | None = None,
    counterId: int | None = Query(default=None, gt=0),
    _: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    ds = build_report_dataset(db, reportType, start_date=startDate, end_date=endDate, counter_id=counterId)
    return ok(
        request,
        {
            "reportType": ds["reportType"],
            "title": ds["title"],
            "description": ds["description"],
            "startDate": ds["startDate"],
            "endDate": ds["endDate"],
            "summary": ds["summary"],
            "columns": ds["columns"],
            "rows": ds["rows"][:250],
            "rowCount": ds["rowCount"],
        },
    )


@router.get("/download")
def download_report_v1(
    reportType: str = Query(default="daily_sales"),
    format: str = Query(default="pdf", pattern="^(pdf|xlsx|csv)$"),
    startDate: date | None = None,
    endDate: date | None = None,
    counterId: int | None = Query(default=None, gt=0),
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    ds = build_report_dataset(db, reportType, start_date=startDate, end_date=endDate, counter_id=counterId)
    fmt = format.lower()

    rec = GeneratedReport(
        report_type=ds["reportType"],
        period_label=f"{ds['startDate']}_{ds['endDate']}",
        start_date=date.fromisoformat(ds["startDate"]),
        end_date=date.fromisoformat(ds["endDate"]),
        format=fmt,
        row_count=ds["rowCount"],
        summary_json=json.dumps({k: str(v) for k, v in ds["summaryPairs"]}, separators=(",", ":")),
        actor_user_id=admin.id,
    )
    db.add(rec)
    db.commit()

    filename_base = f"campusbite-{ds['reportType']}-{ds['startDate']}-to-{ds['endDate']}"

    if fmt == "pdf":
        body = render_pdf_document(ds["title"], ds["subtitle"], ds["summaryPairs"], ds["columns"], ds["rows"])
        media = "application/pdf"
        ext = "pdf"
    elif fmt == "xlsx":
        body = render_xlsx_workbook(ds["title"], ds["title"], ds["subtitle"], ds["summaryPairs"], ds["columns"], ds["rows"])
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ext = "xlsx"
    else:
        body = render_csv_bytes(ds["columns"], ds["rows"])
        media = "text/csv; charset=utf-8"
        ext = "csv"

    return Response(
        content=body,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{filename_base}.{ext}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/generate-daily", status_code=201)
def trigger_daily_reports_v1(
    request: Request,
    background: BackgroundTasks,
    payload: GenerateDailyReportsIn | None = None,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    result = generate_daily_reports_job(
        db,
        target_date=payload.targetDate if payload else None,
        email_recipients=payload.recipients if payload else None,
        actor_user_id=admin.id,
    )
    return ok(request, result, status_code=201)
