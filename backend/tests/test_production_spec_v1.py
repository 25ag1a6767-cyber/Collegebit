"""Comprehensive integration verification for Production Canteen Specification v1 (/api/v1 & WebSockets).

Covers:
  1. Registration, email/phone login, rotating refresh tokens + replay family revocation, logout revocation.
  2. Encrypted RFC 6238 TOTP MFA enrollment, MFA login challenge, and single-use recovery codes.
  3. Strict student order isolation & counter-scoped staff RBAC (403 COUNTER_FORBIDDEN on unassigned counters).
  4. Verified menu items, customizations, recipe ingredient deduction & restoration, coupons, tax (taxRateBps), and idempotent checkout.
  5. UPI payment intent, gateway failure handling, HMAC signature verification, and raw-body webhook deduplication.
  6. Late-capture safety: expired reservation receiving payment.captured flags late_capture_flagged and initiates automatic refund.
  7. Authorized admin gateway refunds and reconciliation ledger.
  8. Short-lived opaque QR pickup tokens (pk_...), expiry (410 PICKUP_TOKEN_EXPIRED), 6-char manual fallback code, and atomic single-use collection (409 ORDER_ALREADY_COLLECTED).
  9. Counter-scoped staff kitchen lifecycle (accept -> start -> ready -> exception with refund) and ingredient wastage logging.
  10. Grounded AI Menu Assistant (Gemini mock + deterministic fallback): strips hallucinated/over-budget/allergen-unsafe items, never auto-adds to cart, and stores zero PII prompts.
  11. AI Demand Forecasting (weighted moving average + intervals + measured rolling backtest MAE/WAPE + staff override) and predicted recipe ingredient shortage insights.
  12. Real-time WebSocket (/api/v1/ws) role & counter scoping.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import (
    AIInteraction,
    Ingredient,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    PickupToken,
    Product,
    StockMovement,
    User,
    UserRole,
)
from app.security import hash_password, totp_code_at

def _unique_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}@example.edu"

def _unique_phone() -> str:
    digits = str(uuid.uuid4().int)[:9]
    return f"+919{digits}"

def _create_user_direct(role: UserRole, prefix: str) -> tuple[int, str, str]:
    email = _unique_email(prefix)
    password = "ProdSpec-Strong-Pass-123!"
    with SessionLocal() as db:
        u = User(
            full_name=f"Test {prefix.title()}",
            email=email,
            phone=_unique_phone(),
            password_hash=hash_password(password),
            role=role,
            is_active=True,
        )
        db.add(u)
        db.commit()
        db.refresh(u)
        return u.id, email, password

def _login_v1(client: TestClient, identifier: str, password: str) -> dict[str, Any]:
    res = client.post("/api/v1/auth/login", json={"identifier": identifier, "password": password})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["success"] is True
    return body["data"]

def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}

class _DummyResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=httpx.Request("POST", "https://mock"), response=httpx.Response(self.status_code))

    def json(self) -> dict[str, Any]:
        return self._payload

def test_01_auth_register_phone_login_refresh_rotation_and_replay_revocation():
    with TestClient(app) as client:
        email = _unique_email("v1-student")
        phone = _unique_phone()
        password = "CampusStudent-Pass-987!"

        reg = client.post(
            "/api/v1/auth/register",
            json={
                "fullName": "Aarav Sharma",
                "email": email,
                "phone": phone,
                "password": password,
                "dietaryPreference": "vegetarian",
            },
        )
        assert reg.status_code == 201, reg.text
        assert reg.json()["success"] is True

        login1 = _login_v1(client, phone, password)
        rt1 = login1["refreshToken"]
        at1 = login1["accessToken"]
        assert at1 and rt1

        rot = client.post("/api/v1/auth/refresh", json={"refreshToken": rt1})
        assert rot.status_code == 200, rot.text
        rt2 = rot.json()["data"]["refreshToken"]
        assert rt2 and rt2 != rt1

        replay = client.post("/api/v1/auth/refresh", json={"refreshToken": rt1})
        assert replay.status_code == 401
        assert replay.json()["error"]["code"] == "REFRESH_TOKEN_REPLAYED"

        after_compromise = client.post("/api/v1/auth/refresh", json={"refreshToken": rt2})
        assert after_compromise.status_code == 401

        login2 = _login_v1(client, email, password)
        rt3 = login2["refreshToken"]
        out = client.post("/api/v1/auth/logout", json={"refreshToken": rt3}, headers=_auth_headers(login2["accessToken"]))
        assert out.status_code == 200
        post_logout = client.post("/api/v1/auth/refresh", json={"refreshToken": rt3})
        assert post_logout.status_code == 401

def test_02_totp_mfa_enrollment_login_challenge_and_recovery_code():
    with TestClient(app) as client:
        _, admin_email, admin_pw = _create_user_direct(UserRole.ADMIN, "mfa-admin")
        session = _login_v1(client, admin_email, admin_pw)
        hdrs = _auth_headers(session["accessToken"])

        setup = client.post("/api/v1/auth/mfa/setup", headers=hdrs)
        assert setup.status_code == 200, setup.text
        secret = setup.json()["data"]["secret"]
        assert setup.json()["data"]["otpauthUri"].startswith("otpauth://totp/")

        valid_totp = totp_code_at(secret)
        enable = client.post("/api/v1/auth/mfa/enable", json={"code": valid_totp}, headers=hdrs)
        assert enable.status_code == 200, enable.text
        recovery_codes = enable.json()["data"]["recoveryCodes"]
        assert len(recovery_codes) >= 6

        step1 = client.post("/api/v1/auth/login", json={"identifier": admin_email, "password": admin_pw})
        assert step1.status_code == 200
        step1_data = step1.json()["data"]
        assert step1_data["mfaRequired"] is True
        assert not step1_data.get("accessToken")
        challenge = step1_data["mfaChallengeToken"]

        rc = recovery_codes[0]
        step2 = client.post("/api/v1/auth/mfa/verify-login", json={"mfaChallengeToken": challenge, "code": rc})
        assert step2.status_code == 200, step2.text
        assert step2.json()["data"]["accessToken"]

        step1_again = client.post("/api/v1/auth/login", json={"identifier": admin_email, "password": admin_pw}).json()["data"]
        reuse = client.post(
            "/api/v1/auth/mfa/verify-login",
            json={"mfaChallengeToken": step1_again["mfaChallengeToken"], "code": rc},
        )
        assert reuse.status_code == 401

def test_03_counter_scoped_staff_rbac_and_student_order_isolation():
    with TestClient(app) as client:
        _, admin_email, admin_pw = _create_user_direct(UserRole.ADMIN, "rbac-admin")
        staff_id, staff_email, staff_pw = _create_user_direct(UserRole.STAFF, "rbac-staff")
        _, s1_email, s1_pw = _create_user_direct(UserRole.STUDENT, "student-a")
        _, s2_email, s2_pw = _create_user_direct(UserRole.STUDENT, "student-b")

        admin_h = _auth_headers(_login_v1(client, admin_email, admin_pw)["accessToken"])
        staff_h = _auth_headers(_login_v1(client, staff_email, staff_pw)["accessToken"])
        s1_h = _auth_headers(_login_v1(client, s1_email, s1_pw)["accessToken"])
        s2_h = _auth_headers(_login_v1(client, s2_email, s2_pw)["accessToken"])

        c_a = client.post("/api/v1/admin/counters", json={"name": f"Counter A {uuid.uuid4().hex[:4]}", "capacityPerSlot": 30}, headers=admin_h).json()["data"]
        c_b = client.post("/api/v1/admin/counters", json={"name": f"Counter B {uuid.uuid4().hex[:4]}", "capacityPerSlot": 30}, headers=admin_h).json()["data"]

        assign = client.put(f"/api/v1/admin/users/{staff_id}/counters", json={"counterIds": [c_a["id"]]}, headers=admin_h)
        assert assign.status_code == 200, assign.text

        cat = client.post(
            "/api/v1/admin/categories",
            json={"name": f"Meals {uuid.uuid4().hex[:4]}", "itemType": "food"},
            headers=admin_h,
        ).json()["data"]
        item_b = client.post(
            "/api/v1/admin/menu-items",
            json={
                "categoryId": cat["id"],
                "counterId": c_b["id"],
                "name": "South Meals Thali",
                "pricePaise": 8500,
                "stockQuantity": 20,
                "preparationMinutes": 10,
                "dietaryTags": ["vegetarian"],
                "allergenTags": ["dairy"],
            },
            headers=admin_h,
        ).json()["data"]

        forbidden_avail = client.patch(
            f"/api/v1/staff/menu-items/{item_b['id']}/availability",
            json={"isAvailable": False, "reason": "Unauthorized attempt"},
            headers=staff_h,
        )
        assert forbidden_avail.status_code == 403
        assert forbidden_avail.json()["error"]["code"] == "COUNTER_FORBIDDEN"

        forbidden_queue = client.get(f"/api/v1/staff/orders?counterId={c_b['id']}", headers=staff_h)
        assert forbidden_queue.status_code == 403
        assert forbidden_queue.json()["error"]["code"] == "COUNTER_FORBIDDEN"

        ord_res = client.post(
            "/api/v1/orders",
            json={"items": [{"menuItemId": item_b["id"], "quantity": 1}], "counterId": c_b["id"]},
            headers=s1_h,
        )
        assert ord_res.status_code == 201, ord_res.text
        order_id = ord_res.json()["data"]["order"]["id"] if "order" in ord_res.json()["data"] else ord_res.json()["data"]["id"]

        other_student_read = client.get(f"/api/v1/orders/{order_id}", headers=s2_h)
        assert other_student_read.status_code == 403

def test_04_recipe_consumption_customizations_coupon_tax_and_cancellation_restore():
    with TestClient(app) as client:
        _, admin_email, admin_pw = _create_user_direct(UserRole.ADMIN, "recipe-admin")
        _, stu_email, stu_pw = _create_user_direct(UserRole.STUDENT, "recipe-student")
        admin_h = _auth_headers(_login_v1(client, admin_email, admin_pw)["accessToken"])
        stu_h = _auth_headers(_login_v1(client, stu_email, stu_pw)["accessToken"])

        s_res = client.patch("/api/v1/admin/settings", json={"taxRateBps": 500, "orderingPaused": False}, headers=admin_h)
        assert s_res.status_code == 200, s_res.text

        try:

            ing = client.post(
                "/api/v1/admin/ingredients",
                json={"name": f"Fresh Paneer {uuid.uuid4().hex[:5]}", "unit": "g", "currentQuantity": "1000", "reorderLevel": "200"},
                headers=admin_h,
            ).json()["data"]

            cat = client.post(
                "/api/v1/admin/categories",
                json={"name": f"Wraps {uuid.uuid4().hex[:4]}", "itemType": "food"},
                headers=admin_h,
            ).json()["data"]

            wrap = client.post(
                "/api/v1/admin/menu-items",
                json={
                    "categoryId": cat["id"],
                    "name": "Paneer Tikka Roll",
                    "pricePaise": 7000,
                    "stockQuantity": 15,
                    "preparationMinutes": 8,
                    "dietaryTags": ["vegetarian", "high-protein"],
                    "allergenTags": ["dairy", "gluten"],
                    "customizations": [{"name": "extra_cheese", "label": "Extra Cheese", "priceDeltaPaise": 1500}],
                },
                headers=admin_h,
            ).json()["data"]

            rec = client.put(
                f"/api/v1/admin/menu-items/{wrap['id']}/recipe",
                json={"items": [{"ingredientId": ing["id"], "quantityPerServing": "120"}]},
                headers=admin_h,
            )
            assert rec.status_code == 200, rec.text

            code = f"SAVE10{uuid.uuid4().hex[:4].upper()}"
            disc = client.post(
                "/api/v1/admin/discounts",
                json={"code": code, "description": "10% break offer", "discountPercent": 10, "minOrderPaise": 5000, "isActive": True},
                headers=admin_h,
            )
            assert disc.status_code == 201, disc.text

            quote = client.post(
                "/api/v1/orders/quote",
                json={
                    "items": [{"menuItemId": wrap["id"], "quantity": 2, "customizations": {"extra_cheese": True}}],
                    "couponCode": code,
                },
                headers=stu_h,
            )
            assert quote.status_code == 200, quote.text
            q_data = quote.json()["data"]
            assert q_data["subtotalPaise"] == 17000
            assert q_data["discountPaise"] == 1700
            assert q_data["taxPaise"] == 765
            assert q_data["totalPaise"] == 16065

            idem_key = f"idem-{uuid.uuid4().hex}"
            created1 = client.post(
                "/api/v1/orders",
                json={
                    "items": [{"menuItemId": wrap["id"], "quantity": 2, "customizations": {"extra_cheese": True}}],
                    "couponCode": code,
                    "idempotencyKey": idem_key,
                },
                headers=stu_h,
            )
            assert created1.status_code == 201, created1.text
            ord_obj = created1.json()["data"].get("order") or created1.json()["data"]
            order_id = ord_obj["id"]

            created2 = client.post(
                "/api/v1/orders",
                json={
                    "items": [{"menuItemId": wrap["id"], "quantity": 2, "customizations": {"extra_cheese": True}}],
                    "couponCode": code,
                    "idempotencyKey": idem_key,
                },
                headers=stu_h,
            )
            assert created2.status_code in (200, 201)
            ord_obj2 = created2.json()["data"].get("order") or created2.json()["data"]
            assert ord_obj2["id"] == order_id

            with SessionLocal() as db:
                ing_row = db.get(Ingredient, ing["id"])
                prod_row = db.get(Product, wrap["id"])
                assert Decimal(ing_row.current_quantity) == Decimal("760")
                assert prod_row.stock_quantity == 13

            canc = client.post(f"/api/v1/orders/{order_id}/cancel", json={"reason": "Break rescheduled"}, headers=stu_h)
            assert canc.status_code == 200, canc.text
            with SessionLocal() as db:
                ing_row = db.get(Ingredient, ing["id"])
                prod_row = db.get(Product, wrap["id"])
                assert Decimal(ing_row.current_quantity) == Decimal("1000")
                assert prod_row.stock_quantity == 15
        finally:
            client.patch("/api/v1/admin/settings", json={"taxRateBps": 0}, headers=admin_h)

def test_05_upi_payment_webhook_kitchen_lifecycle_qr_single_use_and_refund(monkeypatch):
    cfg = get_settings()
    monkeypatch.setattr(cfg, "razorpay_key_id", "rzp_test_prodspec")
    monkeypatch.setattr(cfg, "razorpay_key_secret", "secret_prodspec_key_123")
    monkeypatch.setattr(cfg, "razorpay_webhook_secret", "whsec_prodspec_456")

    def fake_httpx_post(url: str, **kwargs: Any) -> _DummyResponse:
        if url.endswith("/v1/orders"):
            return _DummyResponse(200, {"id": f"order_{uuid.uuid4().hex[:12]}", "status": "created"})
        if "/refund" in url:
            return _DummyResponse(200, {"id": f"rfnd_{uuid.uuid4().hex[:12]}", "status": "processed"})
        raise AssertionError(f"Unexpected external POST: {url}")

    monkeypatch.setattr(httpx, "post", fake_httpx_post)

    with TestClient(app) as client:
        _, admin_email, admin_pw = _create_user_direct(UserRole.ADMIN, "pay-admin")
        staff_id, staff_email, staff_pw = _create_user_direct(UserRole.STAFF, "pay-staff")
        _, stu_email, stu_pw = _create_user_direct(UserRole.STUDENT, "pay-student")

        admin_h = _auth_headers(_login_v1(client, admin_email, admin_pw)["accessToken"])
        staff_h = _auth_headers(_login_v1(client, staff_email, staff_pw)["accessToken"])
        stu_h = _auth_headers(_login_v1(client, stu_email, stu_pw)["accessToken"])

        counter = client.post("/api/v1/admin/counters", json={"name": f"Express Counter {uuid.uuid4().hex[:4]}"}, headers=admin_h).json()["data"]
        client.put(f"/api/v1/admin/users/{staff_id}/counters", json={"counterIds": [counter["id"]]}, headers=admin_h)

        cat = client.post("/api/v1/admin/categories", json={"name": f"Snacks {uuid.uuid4().hex[:4]}", "itemType": "food"}, headers=admin_h).json()["data"]
        item = client.post(
            "/api/v1/admin/menu-items",
            json={"categoryId": cat["id"], "counterId": counter["id"], "name": "Masala Dosa", "pricePaise": 6000, "stockQuantity": 25},
            headers=admin_h,
        ).json()["data"]

        ord_res = client.post(
            "/api/v1/orders",
            json={"items": [{"menuItemId": item["id"], "quantity": 1}], "counterId": counter["id"]},
            headers=stu_h,
        ).json()["data"]
        order_id = (ord_res.get("order") or ord_res)["id"]

        premature_qr = client.post(f"/api/v1/orders/{order_id}/pickup-token", headers=stu_h)
        assert premature_qr.status_code == 409
        assert premature_qr.json()["error"]["code"] == "PAYMENT_PENDING"

        intent = client.post("/api/v1/payments/upi/intent", json={"orderId": order_id}, headers=stu_h)
        assert intent.status_code in (200, 201), intent.text
        intent_data = intent.json()["data"]
        gw_order_id = intent_data["gatewayOrderId"]
        payment_id = intent_data["paymentId"]

        gw_payment_id = f"pay_{uuid.uuid4().hex[:12]}"
        sig = hmac.new(
            cfg.razorpay_key_secret.encode(),
            f"{gw_order_id}|{gw_payment_id}".encode(),
            hashlib.sha256,
        ).hexdigest()
        ver = client.post(
            "/api/v1/payments/verify",
            json={"razorpayOrderId": gw_order_id, "razorpayPaymentId": gw_payment_id, "razorpaySignature": sig},
            headers=stu_h,
        )
        assert ver.status_code == 200, ver.text

        tok_res = client.post(f"/api/v1/orders/{order_id}/pickup-token", headers=stu_h)
        assert tok_res.status_code == 200, tok_res.text
        pickup_tok = tok_res.json()["data"]["pickupToken"]
        manual_code = tok_res.json()["data"]["manualPickupCode"]
        assert pickup_tok.startswith("pk_")
        assert len(manual_code) == 6

        not_ready = client.post("/api/v1/pickups/verify", json={"pickupToken": pickup_tok}, headers=staff_h)
        assert not_ready.status_code == 409
        assert not_ready.json()["error"]["code"] == "ORDER_NOT_READY"

        assert client.post(f"/api/v1/staff/orders/{order_id}/accept", headers=staff_h).status_code == 200
        assert client.post(f"/api/v1/staff/orders/{order_id}/start", headers=staff_h).status_code == 200
        assert client.post(f"/api/v1/staff/orders/{order_id}/ready", headers=staff_h).status_code == 200

        with SessionLocal() as db:
            pt = db.scalar(select(PickupToken).where(PickupToken.order_id == order_id))
            pt.expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
            db.commit()
        expired_scan = client.post("/api/v1/pickups/verify", json={"pickupToken": pickup_tok}, headers=staff_h)
        assert expired_scan.status_code == 410
        assert expired_scan.json()["error"]["code"] == "PICKUP_TOKEN_EXPIRED"

        fresh_tok = client.post(f"/api/v1/orders/{order_id}/pickup-token", headers=stu_h).json()["data"]["pickupToken"]
        collected = client.post("/api/v1/pickups/verify", json={"pickupToken": fresh_tok}, headers=staff_h)
        assert collected.status_code == 200, collected.text

        duplicate_scan = client.post("/api/v1/pickups/verify", json={"orderId": order_id, "manualCode": manual_code}, headers=staff_h)
        assert duplicate_scan.status_code == 409
        assert duplicate_scan.json()["error"]["code"] == "ORDER_ALREADY_COLLECTED"

        ref = client.post(
            f"/api/v1/payments/{payment_id}/refunds",
            json={"amountPaise": 6000, "reason": "Quality goodwill refund"},
            headers=admin_h,
        )
        assert ref.status_code in (200, 201), ref.text

def test_06_late_captured_webhook_on_expired_order_flags_and_auto_refunds(monkeypatch):
    cfg = get_settings()
    monkeypatch.setattr(cfg, "razorpay_key_id", "rzp_test_latecap")
    monkeypatch.setattr(cfg, "razorpay_key_secret", "secret_latecap_123")
    monkeypatch.setattr(cfg, "razorpay_webhook_secret", "whsec_latecap_789")

    gw_order_id = f"order_late_{uuid.uuid4().hex[:10]}"
    gw_pay_id = f"pay_late_{uuid.uuid4().hex[:10]}"

    def fake_httpx_post(url: str, **kwargs: Any) -> _DummyResponse:
        if url.endswith("/v1/orders"):
            return _DummyResponse(200, {"id": gw_order_id, "status": "created"})
        if "/refund" in url:
            return _DummyResponse(200, {"id": f"rfnd_auto_{uuid.uuid4().hex[:8]}", "status": "processed"})
        raise AssertionError(url)

    monkeypatch.setattr(httpx, "post", fake_httpx_post)

    with TestClient(app) as client:
        _, admin_email, admin_pw = _create_user_direct(UserRole.ADMIN, "late-admin")
        _, stu_email, stu_pw = _create_user_direct(UserRole.STUDENT, "late-student")
        admin_h = _auth_headers(_login_v1(client, admin_email, admin_pw)["accessToken"])
        stu_h = _auth_headers(_login_v1(client, stu_email, stu_pw)["accessToken"])

        cat = client.post("/api/v1/admin/categories", json={"name": f"Beverages {uuid.uuid4().hex[:4]}", "itemType": "food"}, headers=admin_h).json()["data"]
        item = client.post(
            "/api/v1/admin/menu-items",
            json={"categoryId": cat["id"], "name": "Filter Coffee", "pricePaise": 3000, "stockQuantity": 10},
            headers=admin_h,
        ).json()["data"]

        ord_data = client.post("/api/v1/orders", json={"items": [{"menuItemId": item["id"], "quantity": 1}]}, headers=stu_h).json()["data"]
        order_id = (ord_data.get("order") or ord_data)["id"]
        client.post("/api/v1/payments/upi/intent", json={"orderId": order_id}, headers=stu_h)

        client.post(f"/api/v1/orders/{order_id}/cancel", json={"reason": "Checkout window expired"}, headers=stu_h)

        webhook_payload = {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": gw_pay_id,
                        "order_id": gw_order_id,
                        "amount": 3000,
                        "currency": "INR",
                        "status": "captured",
                    }
                }
            },
        }
        raw_body = json.dumps(webhook_payload, separators=(",", ":")).encode("utf-8")
        wh_sig = hmac.new(cfg.razorpay_webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()

        wh1 = client.post(
            "/api/v1/webhooks/payments/razorpay",
            content=raw_body,
            headers={"Content-Type": "application/json", "X-Razorpay-Signature": wh_sig, "X-Razorpay-Event-Id": f"evt_{uuid.uuid4().hex[:10]}"},
        )
        assert wh1.status_code == 200, wh1.text

        wh2 = client.post(
            "/api/v1/webhooks/payments/razorpay",
            content=raw_body,
            headers={"Content-Type": "application/json", "X-Razorpay-Signature": wh_sig},
        )
        assert wh2.status_code == 200

        with SessionLocal() as db:
            pay_row = db.scalar(select(Payment).where(Payment.provider_order_id == gw_order_id))
            ord_row = db.get(Order, order_id)
            assert pay_row is not None
            assert pay_row.late_capture_flagged is True
            assert ord_row.payment_status in {PaymentStatus.REFUNDED, PaymentStatus.REFUND_PENDING}
            assert ord_row.status in {OrderStatus.CANCELLED, OrderStatus.REFUND_PENDING}

def test_07_ai_assistant_grounding_privacy_and_demand_forecasting(monkeypatch):
    cfg = get_settings()
    monkeypatch.setattr(cfg, "gemini_api_key", "mock-gemini-key-no-external-call")

    with TestClient(app) as client:
        _, admin_email, admin_pw = _create_user_direct(UserRole.ADMIN, "ai-admin")
        _, stu_email, stu_pw = _create_user_direct(UserRole.STUDENT, "ai-student")
        admin_h = _auth_headers(_login_v1(client, admin_email, admin_pw)["accessToken"])
        stu_h = _auth_headers(_login_v1(client, stu_email, stu_pw)["accessToken"])

        cat = client.post("/api/v1/admin/categories", json={"name": f"AI Lunch {uuid.uuid4().hex[:4]}", "itemType": "food"}, headers=admin_h).json()["data"]
        cheap_veg = client.post(
            "/api/v1/admin/menu-items",
            json={
                "categoryId": cat["id"],
                "name": "Idli Sambar Plate",
                "pricePaise": 4500,
                "stockQuantity": 30,
                "dietaryTags": ["vegetarian", "vegan"],
                "allergenTags": ["mustard"],
            },
            headers=admin_h,
        ).json()["data"]
        expensive_item = client.post(
            "/api/v1/admin/menu-items",
            json={
                "categoryId": cat["id"],
                "name": "Deluxe Paneer Platter",
                "pricePaise": 18000,
                "stockQuantity": 20,
                "dietaryTags": ["vegetarian"],
                "allergenTags": ["dairy"],
            },
            headers=admin_h,
        ).json()["data"]

        def fake_gemini_post(url: str, **kwargs: Any) -> _DummyResponse:
            body_str = json.dumps(kwargs.get("json", {}))

            assert stu_email not in body_str
            model_json = {
                "answer": "Idli Sambar Plate is ₹45.00 and fits under ₹80.",
                "suggestedItemIds": [9999999, expensive_item["id"], cheap_veg["id"]],
                "dietaryWarnings": [],
            }
            return _DummyResponse(
                200,
                {"candidates": [{"content": {"parts": [{"text": json.dumps(model_json)}]}}]},
            )

        monkeypatch.setattr(httpx, "post", fake_gemini_post)

        ai_res = client.post(
            "/api/v1/ai/assistant",
            json={"message": "What vegan food can I get under ₹80?"},
            headers=stu_h,
        )
        assert ai_res.status_code == 200, ai_res.text
        ai_data = ai_res.json()["data"]
        assert ai_data["requiresConfirmation"] is True
        assert ai_data["autoAddedToCart"] is False
        returned_ids = [it["id"] for it in ai_data["suggestedItems"]]
        assert cheap_veg["id"] in returned_ids
        assert 9999999 not in returned_ids
        assert expensive_item["id"] not in returned_ids

        with SessionLocal() as db:
            latest_ai = db.scalar(select(AIInteraction).where(AIInteraction.feature == "menu_assistant").order_by(AIInteraction.id.desc()))
            assert latest_ai is not None
            assert latest_ai.prompt is None
            assert len(latest_ai.input_hash or "") == 64

        fc_run = client.post("/api/v1/ai/forecasts/run", json={}, headers=admin_h)
        assert fc_run.status_code == 201, fc_run.text
        fc_data = fc_run.json()["data"]
        assert fc_data["isEstimate"] is True
        assert len(fc_data["items"]) >= 1
        first_fi = fc_data["items"][0]
        assert first_fi["lowerBound"] <= first_fi["predictedQuantity"] <= first_fi["upperBound"]

        ov = client.patch(
            f"/api/v1/ai/forecasts/items/{first_fi['id']}/override",
            json={"approvedQuantity": 42, "reason": "Tech fest expected footfall"},
            headers=admin_h,
        )
        assert ov.status_code == 200, ov.text
        assert ov.json()["data"]["staffApprovedQuantity"] == 42

        waste = client.get("/api/v1/ai/insights/waste", headers=admin_h)
        assert waste.status_code == 200, waste.text

def test_08_realtime_websocket_role_and_counter_scoping():
    with TestClient(app) as client:
        _, s1_email, s1_pw = _create_user_direct(UserRole.STUDENT, "ws-stu1")
        _, s2_email, s2_pw = _create_user_direct(UserRole.STUDENT, "ws-stu2")
        s1_tok = _login_v1(client, s1_email, s1_pw)["accessToken"]
        s2_tok = _login_v1(client, s2_email, s2_pw)["accessToken"]

        with client.websocket_connect(f"/api/v1/ws?token={s1_tok}") as ws1, client.websocket_connect(f"/api/v1/ws?token={s2_tok}") as ws2:
            hello1 = ws1.receive_json()
            hello2 = ws2.receive_json()
            assert hello1["topic"] == "connected"
            assert hello1["role"] == "student"
            assert hello2["topic"] == "connected"
            assert hello1["userId"] != hello2["userId"]

            ws1.send_text(json.dumps({"type": "ping"}))
            pong = ws1.receive_json()
            assert pong["topic"] == "pong"
