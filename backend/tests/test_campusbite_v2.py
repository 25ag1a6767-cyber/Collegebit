"""End-to-end QA & regression suite for CampusBite AI v2.0.

Verifies:
  1. Three dedicated portals (/, /staff, /admin) + strict CSP compliance + strict RBAC isolation.
  2. Granular staff permissions, staff order rejection (with stock restoration & auto-refund), history & performance.
  3. Customer digital receipts (JSON & PDF 1.4) with cross-student privacy isolation + /users/me/change-password.
  4. Suppliers, Purchase Orders -> automated FEFO batch/stock/expense receiving, Operational Expenses, Admin order override & Feedback.
  5. Advanced Admin BI Dashboard: all 10 KPI cards, all 16 interactive graph datasets, 11 filters & 4 period comparisons.
  6. All 14 automated report types in JSON preview + downloadable PDF 1.4, OpenXML Excel (.xlsx), and CSV + daily runner.
  7. Encrypted GFS Backups (7D/4W/3M), secondary copy, restore drill, tamper detection & 4-Day Hybrid Data Retention safety gate.
  8. 12-Module AI Business Intelligence Suite (ACTUAL vs PREDICTION + Menu Engineering), DB Admin, Security Monitor & Rate Limiting.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import re
import uuid
import zipfile

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.database import SessionLocal
from app.main import _api_rate_buckets, app
from app.models import (
    AIInteraction,
    AuditEvent,
    CanteenCounter,
    Category,
    DailySalesArchive,
    Order,
    OrderStatus,
    OutboxEvent,
    Payment,
    PaymentStatus,
    PickupToken,
    Product,
    ProductBatch,
    RefreshSession,
    Refund,
    TemporaryApiLog,
    User,
    UserCounterAssignment,
    UserRole,
)
from app.modules.retention_backup import verify_encrypted_backup
from app.security import hash_password


def _mk_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}@campusbite.edu"


def _mk_phone() -> str:
    return f"+918{str(uuid.uuid4().int)[:9]}"


def _seed_user(role: UserRole, prefix: str, counter_id: int | None = None) -> tuple[int, str, str]:
    email = _mk_email(prefix)
    pw = "CampusBite-V2-Pass#2026!"
    with SessionLocal() as db:
        u = User(
            full_name=f"V2 {prefix.title()}",
            email=email,
            phone=_mk_phone(),
            password_hash=hash_password(pw),
            role=role,
            is_active=True,
            email_verified_at=datetime.now(timezone.utc),
        )
        db.add(u)
        db.flush()
        if counter_id is not None:
            db.add(UserCounterAssignment(user_id=u.id, counter_id=counter_id))
        db.commit()
        return u.id, email, pw


def _login(client: TestClient, email: str, password: str) -> str:
    r = client.post("/api/v1/auth/login", json={"identifier": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["data"]["accessToken"]


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _ensure_open_counter_and_item(price_paise: int = 8000, cost_paise: int = 3500, stock: int = 30) -> tuple[int, int]:
    with SessionLocal() as db:
        counter = db.scalar(select(CanteenCounter).order_by(CanteenCounter.id.asc()).limit(1))
        if counter is None:
            counter = CanteenCounter(name="V2 Main Counter", location="Ground Floor", capacity_per_slot=50, opening_hours="00:00-23:59", is_active=True)
            db.add(counter)
            db.flush()
        else:
            counter.opening_hours = "00:00-23:59"
            counter.is_active = True

        cat = db.scalar(select(Category).where(Category.is_active.is_(True)).limit(1))
        if cat is None:
            cat = Category(name=f"V2 Specials {uuid.uuid4().hex[:4]}", slug=f"v2-spec-{uuid.uuid4().hex[:6]}", item_type="food", is_active=True)
            db.add(cat)
            db.flush()

        prod = Product(
            category_id=cat.id,
            counter_id=counter.id,
            name=f"V2 Masala Bowl {uuid.uuid4().hex[:6]}",
            description="Freshly cooked rice bowl",
            item_type="food",
            price=price_paise / 100,
            price_paise=price_paise,
            cost_paise=cost_paise,
            stock_quantity=stock,
            low_stock_threshold=5,
            preparation_minutes=8,
            is_available=True,
        )
        db.add(prod)
        db.commit()
        return counter.id, prod.id


def _mark_order_paid_direct(order_id: int) -> int:
    with SessionLocal() as db:
        o = db.get(Order, order_id)
        assert o is not None
        o.payment_status = PaymentStatus.PAID
        o.status = OrderStatus.CONFIRMED
        o.gateway_order_id = f"order_{uuid.uuid4().hex[:12]}"
        o.gateway_payment_id = f"pay_{uuid.uuid4().hex[:12]}"
        pay = Payment(
            order_id=o.id,
            provider="razorpay",
            provider_order_id=o.gateway_order_id,
            provider_payment_id=o.gateway_payment_id,
            amount_paise=o.effective_total_paise,
            currency="INR",
            status="SUCCESS",
        )
        db.add(pay)
        db.commit()
        return pay.id


def test_three_portals_and_strict_csp_compliance():
    with TestClient(app) as client:
        r_cust = client.get("/")
        assert r_cust.status_code == 200
        assert "ACE Canteen" in r_cust.text
        assert "/staff" in r_cust.text
        assert "/admin" in r_cust.text

        r_staff = client.get("/staff")
        assert r_staff.status_code == 200
        assert "Staff Kitchen Portal" in r_staff.text
        assert "camera=(self)" in r_staff.headers.get("Permissions-Policy", "")
        assert not re.search(r'\sstyle\s*=', r_staff.text, re.IGNORECASE)

        r_admin = client.get("/admin")
        assert r_admin.status_code == 200
        assert "Enterprise Administration" in r_admin.text
        assert not re.search(r'\sstyle\s*=', r_admin.text, re.IGNORECASE)

        for asset in ("/js/charts-v2.js", "/js/staff-portal.js", "/js/admin-portal.js", "/css/staff-portal.css", "/css/admin-portal.css"):
            res = client.get(asset)
            assert res.status_code == 200, asset

        # Verify strict backend RBAC: student and staff cannot access /api/v1/admin/*
        _, stu_email, stu_pw = _seed_user(UserRole.STUDENT, "rbac-stu")
        _, stf_email, stf_pw = _seed_user(UserRole.STAFF, "rbac-stf")
        stu_tok = _login(client, stu_email, stu_pw)
        stf_tok = _login(client, stf_email, stf_pw)

        for tok in (stu_tok, stf_tok):
            assert client.get("/api/v1/admin/analytics/dashboard", headers=_hdr(tok)).status_code == 403
            assert client.get("/api/v1/admin/backups", headers=_hdr(tok)).status_code == 403
            assert client.get("/api/v1/admin/database/overview", headers=_hdr(tok)).status_code == 403


def test_staff_reject_permissions_history_and_performance():
    counter_id, item_id = _ensure_open_counter_and_item(stock=20)
    _, admin_email, admin_pw = _seed_user(UserRole.ADMIN, "perm-adm")
    staff_id, staff_email, staff_pw = _seed_user(UserRole.STAFF, "perm-stf", counter_id=counter_id)
    _, stu_email, stu_pw = _seed_user(UserRole.STUDENT, "perm-stu")

    with TestClient(app) as client:
        adm_tok = _login(client, admin_email, admin_pw)
        stf_tok = _login(client, staff_email, staff_pw)
        stu_tok = _login(client, stu_email, stu_pw)

        # Place order & mark paid
        ord_res = client.post(
            "/api/v1/orders",
            headers=_hdr(stu_tok),
            json={"counterId": counter_id, "items": [{"menuItemId": item_id, "quantity": 2}]},
        )
        assert ord_res.status_code == 201, ord_res.text
        order_id = ord_res.json()["data"]["order"]["id"]
        _mark_order_paid_direct(order_id)

        # Admin disables canRejectOrders for this staff member
        perm_res = client.put(
            f"/api/v1/admin/users/{staff_id}/permissions",
            headers=_hdr(adm_tok),
            json={
                "canAcceptOrders": True,
                "canRejectOrders": False,
                "canUpdatePrep": True,
                "canVerifyPickup": True,
                "canManageAvailability": True,
                "canManageInventory": True,
            },
        )
        assert perm_res.status_code == 200
        assert perm_res.json()["data"]["staffPermissions"]["canRejectOrders"] is False

        # Staff attempt to reject must be denied with 403
        denied = client.post(
            f"/api/v1/staff/orders/{order_id}/reject",
            headers=_hdr(stf_tok),
            json={"reason": "Tandoor oven maintenance"},
        )
        assert denied.status_code == 403

        # Re-enable canRejectOrders -> reject succeeds, restores stock from 18 back to 20, initiates refund
        client.put(
            f"/api/v1/admin/users/{staff_id}/permissions",
            headers=_hdr(adm_tok),
            json={
                "canAcceptOrders": True,
                "canRejectOrders": True,
                "canUpdatePrep": True,
                "canVerifyPickup": True,
                "canManageAvailability": True,
                "canManageInventory": True,
            },
        )
        rej = client.post(
            f"/api/v1/staff/orders/{order_id}/reject",
            headers=_hdr(stf_tok),
            json={"reason": "Ingredient exhausted during rush"},
        )
        assert rej.status_code == 200, rej.text
        rej_data = rej.json()["data"]
        assert rej_data["order"]["status"].upper() == "REFUND_PENDING"
        assert rej_data["refund"] is not None

        with SessionLocal() as db:
            p = db.get(Product, item_id)
            assert p.stock_quantity == 20

        # Verify staff history & performance endpoints
        hist = client.get("/api/v1/staff/orders/history", headers=_hdr(stf_tok))
        assert hist.status_code == 200
        assert any(o["id"] == order_id for o in hist.json()["data"]["items"])

        perf = client.get("/api/v1/staff/performance", headers=_hdr(stf_tok))
        assert perf.status_code == 200
        assert "myStatsToday" in perf.json()["data"]
        assert "counterStatsToday" in perf.json()["data"]


def test_customer_digital_receipts_and_password_change():
    counter_id, item_id = _ensure_open_counter_and_item()
    _, stu1_email, stu1_pw = _seed_user(UserRole.STUDENT, "rcpt-s1")
    _, stu2_email, stu2_pw = _seed_user(UserRole.STUDENT, "rcpt-s2")

    with TestClient(app) as client:
        t1 = _login(client, stu1_email, stu1_pw)
        t2 = _login(client, stu2_email, stu2_pw)

        ord_res = client.post(
            "/api/v1/orders",
            headers=_hdr(t1),
            json={"counterId": counter_id, "items": [{"menuItemId": item_id, "quantity": 1}]},
        )
        order_id = ord_res.json()["data"]["order"]["id"]
        _mark_order_paid_direct(order_id)

        # JSON receipt
        rj = client.get(f"/api/v1/orders/{order_id}/receipt?format=json", headers=_hdr(t1))
        assert rj.status_code == 200
        rcpt = rj.json()["data"]
        assert rcpt["receiptNumber"].startswith("RCPT-")
        assert rcpt["orderId"] == order_id
        assert len(rcpt["items"]) == 1

        # PDF receipt
        rpdf = client.get(f"/api/v1/orders/{order_id}/receipt?format=pdf", headers=_hdr(t1))
        assert rpdf.status_code == 200
        assert rpdf.headers["content-type"].startswith("application/pdf")
        assert rpdf.content.startswith(b"%PDF-1.4")

        # Cross-student isolation: student 2 cannot access student 1's receipt
        forbidden = client.get(f"/api/v1/orders/{order_id}/receipt?format=json", headers=_hdr(t2))
        assert forbidden.status_code in {403, 404}

        # Password rotation via /api/v1/users/me/change-password
        new_pw = "Updated-Student-Pass#2026!"
        cp = client.post(
            "/api/v1/users/me/change-password",
            headers=_hdr(t1),
            json={"currentPassword": stu1_pw, "newPassword": new_pw},
        )
        assert cp.status_code == 200
        new_tok = cp.json()["data"]["accessToken"]
        # Old token invalidated via token_version increment
        assert client.get("/api/v1/users/me", headers=_hdr(t1)).status_code == 401
        assert client.get("/api/v1/users/me", headers=_hdr(new_tok)).status_code == 200


def test_suppliers_purchase_orders_expenses_and_admin_ops():
    counter_id, item_id = _ensure_open_counter_and_item(stock=10)
    _, admin_email, admin_pw = _seed_user(UserRole.ADMIN, "proc-adm")
    _, stu_email, stu_pw = _seed_user(UserRole.STUDENT, "proc-stu")

    with TestClient(app) as client:
        adm_tok = _login(client, admin_email, admin_pw)
        stu_tok = _login(client, stu_email, stu_pw)

        # 1. Create Supplier
        sup_res = client.post(
            "/api/v1/admin/suppliers",
            headers=_hdr(adm_tok),
            json={
                "name": f"Campus Agro {uuid.uuid4().hex[:6]}",
                "contactName": "Meera Rao",
                "phone": "9880011223",
                "email": "supply@campusagro.in",
            },
        )
        assert sup_res.status_code == 201, sup_res.text
        supplier_id = sup_res.json()["data"]["id"]

        # 2. Issue Purchase Order for 15 units of menu item @ ₹32.00 (3200 paise)
        po_res = client.post(
            "/api/v1/admin/purchase-orders",
            headers=_hdr(adm_tok),
            json={
                "supplierId": supplier_id,
                "lines": [{"menuItemId": item_id, "quantity": 15, "unitCostPaise": 3200}],
                "notes": "Morning replenishment",
            },
        )
        assert po_res.status_code == 201, po_res.text
        po_id = po_res.json()["data"]["id"]
        assert po_res.json()["data"]["totalCostPaise"] == 48000

        # 3. Receive PO -> creates ProductBatch, increments stock 10 -> 25, logs OperationalExpense
        rec_res = client.post(f"/api/v1/admin/purchase-orders/{po_id}/receive", headers=_hdr(adm_tok))
        assert rec_res.status_code == 200, rec_res.text
        assert rec_res.json()["data"]["purchaseOrder"]["status"] == "RECEIVED"
        assert rec_res.json()["data"]["batchesCreated"] == 1

        with SessionLocal() as db:
            prod = db.get(Product, item_id)
            assert prod.stock_quantity == 25
            assert prod.cost_paise == 3200
            batches = db.scalars(select(ProductBatch).where(ProductBatch.product_id == item_id)).all()
            assert len(batches) >= 1

        # 4. Manual Expense Create & List
        exp_res = client.post(
            "/api/v1/admin/expenses",
            headers=_hdr(adm_tok),
            json={"category": "utilities", "title": "Kitchen LPG Cylinder", "amountPaise": 185000},
        )
        assert exp_res.status_code == 201
        exp_id = exp_res.json()["data"]["id"]
        exp_list = client.get("/api/v1/admin/expenses", headers=_hdr(adm_tok))
        assert exp_list.status_code == 200
        assert any(e["id"] == exp_id for e in exp_list.json()["data"]["items"])

        # 5. Complete Order lifecycle via Admin Override + Student Feedback
        ord_res = client.post(
            "/api/v1/orders",
            headers=_hdr(stu_tok),
            json={"counterId": counter_id, "items": [{"menuItemId": item_id, "quantity": 1}]},
        )
        oid = ord_res.json()["data"]["order"]["id"]
        _mark_order_paid_direct(oid)

        for st in ("accepted", "preparing", "ready", "collected"):
            ov = client.post(
                f"/api/v1/admin/orders/{oid}/status",
                headers=_hdr(adm_tok),
                json={"status": st, "reason": f"Admin advancing to {st}"},
            )
            assert ov.status_code == 200, ov.text

        rv = client.post(
            f"/api/v1/orders/{oid}/review",
            headers=_hdr(stu_tok),
            json={"rating": 5, "comment": "Super fast pre-break service!"},
        )
        assert rv.status_code in {200, 201}

        fb = client.get("/api/v1/admin/feedback", headers=_hdr(adm_tok))
        assert fb.status_code == 200
        assert fb.json()["data"]["totalReviews"] >= 1


def test_admin_bi_dashboard_10_cards_16_graphs_filters_and_comparisons():
    counter_id, item_id = _ensure_open_counter_and_item(price_paise=9000, cost_paise=4000, stock=30)
    _, admin_email, admin_pw = _seed_user(UserRole.ADMIN, "bi-adm")
    _, stu_email, stu_pw = _seed_user(UserRole.STUDENT, "bi-stu")

    with TestClient(app) as client:
        adm_tok = _login(client, admin_email, admin_pw)
        stu_tok = _login(client, stu_email, stu_pw)

        ord_res = client.post(
            "/api/v1/orders",
            headers=_hdr(stu_tok),
            json={"counterId": counter_id, "items": [{"menuItemId": item_id, "quantity": 2}]},
        )
        oid = ord_res.json()["data"]["order"]["id"]
        _mark_order_paid_direct(oid)

        res = client.get(
            f"/api/v1/admin/analytics/dashboard?preset=last_30_days&counterId={counter_id}&productId={item_id}&compare=today_vs_yesterday",
            headers=_hdr(adm_tok),
        )
        assert res.status_code == 200, res.text
        data = res.json()["data"]
        assert data["dataKind"] == "ACTUAL"

        # Verify all 10 required KPI card fields exist and reflect real paid order
        cards = data["cards"]
        for key in (
            "todayRevenuePaise",
            "todayTotalOrders",
            "successfulPayments",
            "failedPayments",
            "pendingOrders",
            "completedOrders",
            "cancelledOrders",
            "averageOrderValuePaise",
            "activeCustomers",
            "estimatedProfitPaise",
        ):
            assert key in cards, f"Missing KPI card: {key}"
        assert cards["todayRevenuePaise"] >= 18000
        assert cards["estimatedProfitPaise"] == cards["filteredRevenuePaise"] - cards["totalExpensesPaise"]

        # Verify all 4 required period comparisons
        for cmp_key in ("today_vs_yesterday", "this_week_vs_last_week", "this_month_vs_last_month", "this_year_vs_last_year"):
            assert cmp_key in data["comparisons"]

        # Verify all 16 required interactive graph series
        expected_16_graphs = {
            "dailySales",
            "weeklySales",
            "monthlySales",
            "yearlySales",
            "topSellingFoods",
            "leastSellingFoods",
            "categorySalesPie",
            "peakOrderingHours",
            "customerPurchaseTrends",
            "paymentSuccessFailure",
            "inventoryConsumption",
            "foodWastage",
            "staffPerformance",
            "counterWiseSales",
            "revenueVsExpenses",
            "profitability",
        }
        assert expected_16_graphs.issubset(set(data["graphs"].keys()))


def test_all_14_reports_pdf_xlsx_csv_and_daily_runner():
    _, admin_email, admin_pw = _seed_user(UserRole.ADMIN, "rep-adm")

    expected_14_types = [
        "daily_sales",
        "daily_orders",
        "daily_payments",
        "weekly_sales",
        "monthly_financial",
        "yearly_performance",
        "best_selling_products",
        "least_selling_products",
        "inventory",
        "supplier_purchase",
        "staff_performance",
        "customer_activity",
        "refunds",
        "profit_expense",
    ]

    with TestClient(app) as client:
        adm_tok = _login(client, admin_email, admin_pw)

        cat = client.get("/api/v1/admin/reports/catalog", headers=_hdr(adm_tok))
        assert cat.status_code == 200
        catalog_types = [r["reportType"] for r in cat.json()["data"]["reportTypes"]]
        assert set(expected_14_types) == set(catalog_types)

        # Preview all 14 report types
        for rt in expected_14_types:
            pr = client.get(f"/api/v1/admin/reports/preview?reportType={rt}", headers=_hdr(adm_tok))
            assert pr.status_code == 200, f"Preview failed for {rt}: {pr.text}"
            assert len(pr.json()["data"]["columns"]) >= 3

        # Download PDF, Excel (.xlsx) & CSV and validate binary file formats
        pdf_res = client.get("/api/v1/admin/reports/download?reportType=monthly_financial&format=pdf", headers=_hdr(adm_tok))
        assert pdf_res.status_code == 200
        assert pdf_res.content.startswith(b"%PDF-1.4")

        xlsx_res = client.get("/api/v1/admin/reports/download?reportType=profit_expense&format=xlsx", headers=_hdr(adm_tok))
        assert xlsx_res.status_code == 200
        with zipfile.ZipFile(io.BytesIO(xlsx_res.content), "r") as zf:
            names = set(zf.namelist())
            assert "[Content_Types].xml" in names
            assert "xl/worksheets/sheet1.xml" in names

        csv_res = client.get("/api/v1/admin/reports/download?reportType=daily_sales&format=csv", headers=_hdr(adm_tok))
        assert csv_res.status_code == 200
        assert "Date" in csv_res.text

        # Configure daily schedule & run automated daily job
        sch = client.put(
            "/api/v1/admin/reports/schedule",
            headers=_hdr(adm_tok),
            json={"dailyReportEmails": "dean@campusbite.edu, accounts@campusbite.edu"},
        )
        assert sch.status_code == 200

        gen = client.post("/api/v1/admin/reports/generate-daily", headers=_hdr(adm_tok), json={})
        assert gen.status_code == 201
        assert gen.json()["data"]["generatedCount"] >= 4


def test_encrypted_gfs_backups_tamper_detection_and_four_day_retention_safety(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = get_settings()
    primary_dir = tmp_path / "primary_gfs"
    secondary_dir = tmp_path / "secondary_offhost"
    monkeypatch.setattr(cfg, "backup_primary_dir", str(primary_dir))
    monkeypatch.setattr(cfg, "backup_secondary_dir", str(secondary_dir))

    counter_id, item_id = _ensure_open_counter_and_item()
    _, admin_email, admin_pw = _seed_user(UserRole.ADMIN, "gov-adm")
    stu_id, stu_email, stu_pw = _seed_user(UserRole.STUDENT, "gov-stu")

    with TestClient(app) as client:
        adm_tok = _login(client, admin_email, admin_pw)
        stu_tok = _login(client, stu_email, stu_pw)

        # Create a real paid permanent order
        ord_res = client.post(
            "/api/v1/orders",
            headers=_hdr(stu_tok),
            json={"counterId": counter_id, "items": [{"menuItemId": item_id, "quantity": 1}]},
        )
        perm_order_id = ord_res.json()["data"]["order"]["id"]
        perm_payment_id = _mark_order_paid_direct(perm_order_id)

        # Seed >5-day-old temporary operational records
        six_days_ago = datetime.now(timezone.utc) - timedelta(days=6)
        with SessionLocal() as db:
            old_log = TemporaryApiLog(request_id="req-old-1", method="GET", path="/api/health", status_code=200, duration_ms=4, created_at=six_days_ago)
            old_outbox = OutboxEvent(topic="order.ready", payload_json="{}", delivered_at=six_days_ago, created_at=six_days_ago)
            old_ai = AIInteraction(user_id=stu_id, feature="concierge", model_version="test", input_hash="a" * 64, created_at=six_days_ago)
            old_sess = RefreshSession(
                user_id=stu_id,
                family_id=uuid.uuid4().hex,
                token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                expires_at=six_days_ago,
                revoked_at=six_days_ago,
                created_at=six_days_ago,
            )
            db.add_all([old_log, old_outbox, old_ai, old_sess])
            db.commit()
            old_log_id = old_log.id
            orders_before = db.scalar(select(func.count(Order.id)))
            payments_before = db.scalar(select(func.count(Payment.id)))
            audits_before = db.scalar(select(func.count(AuditEvent.id)))

        # 1. SAFETY GATE TEST: Without a backup in primary_dir, cleanup MUST HALT!
        halted = client.post(
            "/api/v1/admin/retention/run",
            headers=_hdr(adm_tok),
            json={"retentionDays": 4, "dryRun": False},
        )
        assert halted.status_code == 200
        assert halted.json()["data"]["status"] == "aborted_unhealthy_backup"
        with SessionLocal() as db:
            assert db.get(TemporaryApiLog, old_log_id) is not None

        # 2. CREATE ENCRYPTED GFS BACKUP -> verify primary + secondary copy + restore drill
        bkp_res = client.post("/api/v1/admin/backups/run", headers=_hdr(adm_tok))
        assert bkp_res.status_code == 201, bkp_res.text
        bkp_data = bkp_res.json()["data"]
        enc_path = Path(bkp_data["primaryPath"])
        sec_path = Path(bkp_data["secondaryPath"])
        assert enc_path.is_file()
        assert sec_path.is_file()
        assert enc_path.read_bytes() == sec_path.read_bytes()

        drill = client.post("/api/v1/admin/backups/restore-test", headers=_hdr(adm_tok), json={})
        assert drill.status_code == 200, drill.text
        assert drill.json()["data"]["restoreDrillPassed"] is True

        # Tamper test on a copy: flipping 1 byte MUST fail integrity verification
        tampered = tmp_path / "tampered.enc"
        raw_enc = bytearray(enc_path.read_bytes())
        raw_enc[-10] ^= 0xFF
        tampered.write_bytes(bytes(raw_enc))
        (tmp_path / "tampered.enc.json").write_bytes(
            enc_path.with_name(enc_path.name + ".json").read_bytes()
        )
        with pytest.raises(ValueError):
            verify_encrypted_backup(tampered)

        # 3. NOW RUN 4-DAY RETENTION CLEANUP -> temporary >4d rows purged, permanent rows 100% preserved!
        clean_res = client.post(
            "/api/v1/admin/retention/run",
            headers=_hdr(adm_tok),
            json={"retentionDays": 4, "dryRun": False},
        )
        assert clean_res.status_code == 200, clean_res.text
        cdata = clean_res.json()["data"]
        assert cdata["status"] == "completed"
        assert cdata["backupHealthy"] is True
        assert cdata["deletedCounts"]["temporary_api_logs"] >= 1
        assert cdata["deletedCounts"]["delivered_outbox_events"] >= 1
        assert cdata["deletedCounts"]["temporary_ai_telemetry"] >= 1
        assert cdata["deletedCounts"]["expired_refresh_sessions"] >= 1

        with SessionLocal() as db:
            assert db.get(TemporaryApiLog, old_log_id) is None
            assert db.get(Order, perm_order_id) is not None
            assert db.get(Payment, perm_payment_id) is not None
            assert db.scalar(select(func.count(Order.id))) == orders_before
            assert db.scalar(select(func.count(Payment.id))) == payments_before
            assert db.scalar(select(func.count(AuditEvent.id))) >= audits_before
            assert db.scalar(select(func.count(DailySalesArchive.id))) >= 1


def test_ai_bi_12_modules_db_admin_security_and_rate_limiting(monkeypatch: pytest.MonkeyPatch):
    _, admin_email, admin_pw = _seed_user(UserRole.ADMIN, "aibi-adm")
    stu_id, _, _ = _seed_user(UserRole.STUDENT, "aibi-stu")

    with TestClient(app) as client:
        adm_tok = _login(client, admin_email, admin_pw)

        # 1. 12-Module AI BI Suite
        ai_res = client.get("/api/v1/ai/bi/dashboard?days=30", headers=_hdr(adm_tok))
        assert ai_res.status_code == 200, ai_res.text
        ai = ai_res.json()["data"]
        for key in (
            "dailyBusinessSummary",
            "foodDemandPredictions",
            "salesForecast7Day",
            "popularFoods",
            "slowSellingFoods",
            "peakHourPredictions",
            "ingredientDemandEstimates",
            "inventoryReorderRecommendations",
            "foodWastageAnalysis",
            "salesAnomalies",
            "customerPurchasingTrends",
            "menuEngineeringMatrix",
        ):
            assert key in ai, f"Missing AI BI module: {key}"
        assert len(ai["salesForecast7Day"]) == 7
        assert all(d["dataKind"] == "PREDICTION" for d in ai["salesForecast7Day"])
        assert all(m["dataKind"] == "ACTUAL" and m["quadrant"] in {"STAR", "PLOWHORSE", "PUZZLE", "DOG"} for m in ai["menuEngineeringMatrix"])

        # 2. Database Administration Diagnostics & ANALYZE
        db_ov = client.get("/api/v1/admin/database/overview", headers=_hdr(adm_tok))
        assert db_ov.status_code == 200
        assert db_ov.json()["data"]["totalTables"] >= 25
        db_an = client.post("/api/v1/admin/database/analyze", headers=_hdr(adm_tok))
        assert db_an.status_code == 200
        assert db_an.json()["data"]["integrityStatus"] == "ok"

        # 3. Security Monitor & Session Revocation
        sec = client.get("/api/v1/admin/security/overview", headers=_hdr(adm_tok))
        assert sec.status_code == 200
        assert "summary24h" in sec.json()["data"]

        rev = client.post(f"/api/v1/admin/security/users/{stu_id}/revoke-sessions", headers=_hdr(adm_tok))
        assert rev.status_code == 200

        # 4. General API Rate Limiter Enforcement
        cfg = get_settings()
        monkeypatch.setattr(cfg, "api_rate_limit_per_minute", 30)
        _api_rate_buckets.clear()
        statuses = [
            client.get("/api/v1/counters", headers={"X-Enforce-Rate-Limit": "1"}).status_code
            for _ in range(32)
        ]
        _api_rate_buckets.clear()
        assert 429 in statuses
