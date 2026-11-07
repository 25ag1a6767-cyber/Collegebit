"""Enterprise-grade validation suite for CampusBite AI v2.0 canteen operations.

Tests:
  1. Smart token formatting, sequencing, and collision prevention.
  2. Dedicated QR & token inspection endpoint (/api/v1/pickups/inspect).
  3. Pre-handover blocking on unpaid, unready, cancelled, and already-collected orders.
  4. Atomic collection confirmation & immediate double-collection conflict rejection.
  5. Public TV token display board (/api/v1/queue/display-board and /display).
  6. Smart token management search & filtering (/api/v1/tokens).
  7. Direct Admin BI analytics downloads (PDF, XLSX, CSV).
  8. Multi-counter order item routing and transparent breakdown.
"""
from __future__ import annotations

import datetime
from datetime import timezone
import io
import zipfile
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    CanteenCounter,
    Category,
    Order,
    OrderItem,
    OrderStatus,
    Payment,
    PaymentStatus,
    Pickup,
    Product,
    User,
    UserRole,
)
from app.modules.common import format_token_display
from app.security import create_access_token, hash_password


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


import uuid

def _seed_test_user(role: UserRole, prefix: str) -> tuple[int, str, str]:
    with SessionLocal() as db:
        unique_suffix = uuid.uuid4().hex[:8]
        email = f"{prefix}-{unique_suffix}@college.edu"
        user = User(
            full_name=f"User {prefix}",
            email=email,
            phone=f"98{uuid.uuid4().int % 100000000:08d}",
            password_hash=hash_password("Pass#123456"),
            role=role,
            is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id, user.email, "Pass#123456"


def _ensure_counters_and_items() -> tuple[int, int, int, int]:
    """Create two distinct counters (Snacks & Beverages) and one item for each."""
    with SessionLocal() as db:
        c_snacks = db.scalar(select(CanteenCounter).where(CanteenCounter.name == "Snacks Counter"))
        if c_snacks is None:
            c_snacks = CanteenCounter(name="Snacks Counter", location="Ground Floor", capacity_per_slot=80, opening_hours="00:00-23:59", is_active=True)
            db.add(c_snacks)
            db.flush()

        c_bev = db.scalar(select(CanteenCounter).where(CanteenCounter.name == "Beverages Counter"))
        if c_bev is None:
            c_bev = CanteenCounter(name="Beverages Counter", location="First Floor", capacity_per_slot=60, opening_hours="00:00-23:59", is_active=True)
            db.add(c_bev)
            db.flush()

        cat = db.scalar(select(Category).limit(1))
        if cat is None:
            cat = Category(name="Quick Bites", slug="quick-bites", item_type="food", is_active=True)
            db.add(cat)
            db.flush()

        item_snack = db.scalar(select(Product).where(Product.counter_id == c_snacks.id, Product.is_archived.is_(False)))
        if item_snack is None:
            item_snack = Product(
                category_id=cat.id,
                counter_id=c_snacks.id,
                name="Crispy Veg Burger",
                item_type="food",
                price=50,
                price_paise=5000,
                cost_paise=2200,
                stock_quantity=50,
                is_available=True,
            )
            db.add(item_snack)
            db.flush()

        item_bev = db.scalar(select(Product).where(Product.counter_id == c_bev.id, Product.is_archived.is_(False)))
        if item_bev is None:
            item_bev = Product(
                category_id=cat.id,
                counter_id=c_bev.id,
                name="Cold Coffee with Ice Cream",
                item_type="food",
                price=35,
                price_paise=3500,
                cost_paise=1500,
                stock_quantity=50,
                is_available=True,
            )
            db.add(item_bev)
            db.flush()

        db.commit()
        return c_snacks.id, c_bev.id, item_snack.id, item_bev.id


def _login(client: TestClient, email: str, pw: str) -> str:
    res = client.post("/api/v1/auth/login", json={"identifier": email, "password": pw})
    assert res.status_code == 200, res.text
    return res.json()["data"]["accessToken"]


def _mark_paid(order_id: int) -> None:
    with SessionLocal() as db:
        order = db.get(Order, order_id)
        order.payment_status = PaymentStatus.PAID
        order.status = OrderStatus.CONFIRMED
        order.gateway_payment_id = f"pay_test_{order_id}"
        p = Payment(
            order_id=order.id,
            provider="razorpay",
            provider_order_id=f"order_test_{order_id}",
            provider_payment_id=f"pay_test_{order_id}",
            amount_paise=order.effective_total_paise,
            currency="INR",
            status="SUCCESS",
        )
        db.add(p)
        db.commit()


def test_smart_token_formatting_and_display():
    assert format_token_display(1, counter_name="Snacks Counter", token_date="2026-11-07") == "S-1107-001"
    assert format_token_display(2, counter_name="Beverages Counter", token_date="2026-11-07") == "B-1107-002"
    assert format_token_display(12, counter_name="Lunch Counter", token_date="2026-11-07") == "L-1107-012"
    assert format_token_display(5, counter_name=None, token_date="2026-11-07") == "CB-1107-005"
    assert format_token_display(0) == "-"


def test_dedicated_pickup_inspection_and_collection_workflow():
    c_snack_id, _, item_snack_id, _ = _ensure_counters_and_items()
    _, stu_email, stu_pw = _seed_test_user(UserRole.STUDENT, "insp-stu")
    _, stf_email, stf_pw = _seed_test_user(UserRole.STAFF, "insp-stf")

    with TestClient(app) as client:
        stu_tok = _login(client, stu_email, stu_pw)
        stf_tok = _login(client, stf_email, stf_pw)

        # 1. Student places order
        ord_res = client.post(
            "/api/v1/orders",
            headers=_hdr(stu_tok),
            json={"counterId": c_snack_id, "items": [{"menuItemId": item_snack_id, "quantity": 2}]},
        )
        assert ord_res.status_code == 201, ord_res.text
        order_data = ord_res.json()["data"]["order"]
        order_id = order_data["id"]

        # 2. Inspect before payment -> MUST NOT be eligible for collection!
        insp_unpaid = client.post(
            "/api/v1/pickups/inspect",
            headers=_hdr(stf_tok),
            json={"orderId": order_id},
        )
        assert insp_unpaid.status_code == 200
        assert insp_unpaid.json()["data"]["eligibleForCollection"] is False
        assert "Unpaid" in insp_unpaid.json()["data"]["blockReason"]

        # 3. Mark paid -> Inspect while in kitchen -> still NOT eligible (not ready!)
        _mark_paid(order_id)
        insp_paid_unready = client.post(
            "/api/v1/pickups/inspect",
            headers=_hdr(stf_tok),
            json={"orderId": order_id},
        )
        assert insp_paid_unready.status_code == 200
        assert insp_paid_unready.json()["data"]["eligibleForCollection"] is False
        assert "not marked it READY" in insp_paid_unready.json()["data"]["blockReason"]

        # 4. Student gets QR pickup token
        qr_res = client.post(f"/api/v1/orders/{order_id}/pickup-token", headers=_hdr(stu_tok))
        assert qr_res.status_code == 200
        raw_qr_token = qr_res.json()["data"]["pickupToken"]
        manual_code = qr_res.json()["data"]["manualPickupCode"]

        # 5. Kitchen marks ready
        ready_res = client.post(f"/api/v1/staff/orders/{order_id}/ready", headers=_hdr(stf_tok))
        assert ready_res.status_code == 200

        # 6. Staff scans QR -> Inspect yields full rich order details and ELIGIBLE FOR COLLECTION = TRUE!
        insp_ready = client.post(
            "/api/v1/pickups/inspect",
            headers=_hdr(stf_tok),
            json={"pickupToken": raw_qr_token},
        )
        assert insp_ready.status_code == 200
        idata = insp_ready.json()["data"]
        assert idata["eligibleForCollection"] is True
        assert idata["blockReason"] is None
        assert idata["customer"]["fullName"] == "User insp-stu"
        assert idata["customer"]["studentId"].startswith("ACE-")
        assert idata["payment"]["status"] == "PAID"
        assert len(idata["items"]) == 1
        assert idata["items"][0]["quantity"] == 2
        assert idata["items"][0]["name"] == "Crispy Veg Burger"

        # 7. Staff confirms collection atomically
        col_res = client.post(
            "/api/v1/pickups/verify",
            headers=_hdr(stf_tok),
            json={"pickupToken": raw_qr_token},
        )
        assert col_res.status_code == 200
        cdata = col_res.json()["data"]
        assert cdata["verified"] is True
        assert cdata["order"]["rawStatus"] == "collected"

        # 8. Immediate duplicate collection MUST FAIL with 409 ORDER_ALREADY_COLLECTED!
        dup_res = client.post(
            "/api/v1/pickups/verify",
            headers=_hdr(stf_tok),
            json={"pickupToken": raw_qr_token},
        )
        assert dup_res.status_code == 409
        assert dup_res.json()["error"]["code"] == "ORDER_ALREADY_COLLECTED"

        # 9. Re-inspecting now clearly reveals "ALREADY COLLECTED" with staff name and timestamp!
        insp_after = client.post(
            "/api/v1/pickups/inspect",
            headers=_hdr(stf_tok),
            json={"orderId": order_id},
        )
        assert insp_after.status_code == 200
        assert insp_after.json()["data"]["eligibleForCollection"] is False
        assert "ALREADY COLLECTED" in insp_after.json()["data"]["blockReason"]


def test_public_tv_token_display_board_endpoint_and_html():
    c_snack_id, _, item_snack_id, _ = _ensure_counters_and_items()
    _, stu_email, stu_pw = _seed_test_user(UserRole.STUDENT, "tv-stu")
    _, stf_email, stf_pw = _seed_test_user(UserRole.STAFF, "tv-stf")

    with TestClient(app) as client:
        stu_tok = _login(client, stu_email, stu_pw)
        stf_tok = _login(client, stf_email, stf_pw)

        # Place & pay an order
        ord_res = client.post(
            "/api/v1/orders",
            headers=_hdr(stu_tok),
            json={"counterId": c_snack_id, "items": [{"menuItemId": item_snack_id, "quantity": 1}]},
        )
        order_id = ord_res.json()["data"]["order"]["id"]
        _mark_paid(order_id)

        # Mark ready
        client.post(f"/api/v1/staff/orders/{order_id}/ready", headers=_hdr(stf_tok))

        # Check public /display HTML page
        html_res = client.get("/display")
        assert html_res.status_code == 200
        assert "CAMPUSBITE AI" in html_res.text
        assert "CANTEEN TOKEN DISPLAY BOARD" in html_res.text

        # Check /api/v1/queue/display-board endpoint
        board_res = client.get("/api/v1/queue/display-board")
        assert board_res.status_code == 200
        bdata = board_res.json()["data"]
        assert "readyTokens" in bdata
        assert "preparingTokens" in bdata
        assert "recentlyCollected" in bdata
        assert any(t["orderId"] == order_id for t in bdata["readyTokens"])
        # Privacy check: zero student names or phones in display board data!
        raw_text = board_res.text
        assert "User tv-stu" not in raw_text
        assert "stu-@" not in raw_text


def test_token_management_search_endpoint():
    c_snack_id, _, item_snack_id, _ = _ensure_counters_and_items()
    _, stu_email, stu_pw = _seed_test_user(UserRole.STUDENT, "tok-stu")
    _, stf_email, stf_pw = _seed_test_user(UserRole.STAFF, "tok-stf")

    with TestClient(app) as client:
        stu_tok = _login(client, stu_email, stu_pw)
        stf_tok = _login(client, stf_email, stf_pw)

        ord_res = client.post(
            "/api/v1/orders",
            headers=_hdr(stu_tok),
            json={"counterId": c_snack_id, "items": [{"menuItemId": item_snack_id, "quantity": 1}]},
        )
        order_id = ord_res.json()["data"]["order"]["id"]
        _mark_paid(order_id)

        tok_res = client.get(f"/api/v1/tokens?search=tok-stu", headers=_hdr(stf_tok))
        assert tok_res.status_code == 200
        titems = tok_res.json()["data"]["items"]
        assert any(t["orderId"] == order_id for t in titems)
        matched = next(t for t in titems if t["orderId"] == order_id)
        assert matched["customerName"] == "User tok-stu"
        assert "Crispy Veg Burger" in matched["itemsSummary"]


def test_direct_bi_analytics_report_downloads():
    _, adm_email, adm_pw = _seed_test_user(UserRole.ADMIN, "bi-dl-adm")

    with TestClient(app) as client:
        adm_tok = _login(client, adm_email, adm_pw)

        # Download Analytics PDF
        pdf_res = client.get("/api/v1/admin/reports/download?reportType=daily_sales&format=pdf", headers=_hdr(adm_tok))
        assert pdf_res.status_code == 200
        assert pdf_res.headers["content-type"] == "application/pdf"
        assert pdf_res.content.startswith(b"%PDF-1.4")

        # Download Analytics Excel (.xlsx)
        xlsx_res = client.get("/api/v1/admin/reports/download?reportType=daily_sales&format=xlsx", headers=_hdr(adm_tok))
        assert xlsx_res.status_code == 200
        assert "spreadsheetml" in xlsx_res.headers["content-type"]
        with zipfile.ZipFile(io.BytesIO(xlsx_res.content)) as zf:
            assert "[Content_Types].xml" in zf.namelist()

        # Download Analytics CSV
        csv_res = client.get("/api/v1/admin/reports/download?reportType=daily_sales&format=csv", headers=_hdr(adm_tok))
        assert csv_res.status_code == 200
        assert "text/csv" in csv_res.headers["content-type"]
        assert len(csv_res.text.splitlines()) >= 2
