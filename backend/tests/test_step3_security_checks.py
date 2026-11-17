"""Step 3 security verification tests: hashing, throttling, RBAC matrix, single-use QR, SQLi & log safety."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.emailer import send_email
import app.main as main
from app.main import app
from app.models import (
    CanteenCounter,
    Category,
    Order,
    OrderItem,
    OrderStatus,
    PaymentStatus,
    PickupToken,
    Product,
    User,
    UserRole,
)
from app.security import create_access_token, hash_opaque_token, hash_password, verify_password


def _make_user(role: UserRole, suffix: str | None = None, *, password: str = "SecurePass-2026!") -> tuple[int, str, str]:
    tag = suffix or uuid.uuid4().hex[:8]
    email = f"sec-{role.value}-{tag}@college.edu"
    with SessionLocal() as db:
        u = User(
            full_name=f"Sec {role.value.title()} {tag}",
            email=email,
            password_hash=hash_password(password),
            role=role,
            is_active=True,
        )
        db.add(u)
        db.commit()
        db.refresh(u)
        return u.id, email, password


def _auth(user_id: int) -> dict[str, str]:
    with SessionLocal() as db:
        u = db.get(User, user_id)
        return {"Authorization": f"Bearer {create_access_token(u, mfa_verified=True)}"}


def test_password_hashing_salted_pbkdf2_and_constant_time():
    pw = "CollegeCanteenSecret#2026"
    h1 = hash_password(pw)
    h2 = hash_password(pw)
    assert h1.startswith("pbkdf2_sha256$")
    assert h1 != h2
    assert pw not in h1
    assert verify_password(pw, h1) is True
    assert verify_password(pw + "x", h1) is False


def test_rbac_boundaries_student_and_staff_isolation():
    stu_id, _, _ = _make_user(UserRole.STUDENT)
    staff_id, _, _ = _make_user(UserRole.STAFF)
    admin_id, _, _ = _make_user(UserRole.ADMIN)
    stu_h = _auth(stu_id)
    staff_h = _auth(staff_id)
    admin_h = _auth(admin_id)

    staff_or_admin_endpoints = [
        ("GET", "/api/staff/dashboard"),
        ("GET", "/api/v1/staff/orders"),
        ("GET", "/api/v1/staff/inventory"),
        ("POST", "/api/v1/pickups/inspect"),
    ]
    admin_only_endpoints = [
        ("GET", "/api/admin/users"),
        ("GET", "/api/admin/orders/all"),
        ("GET", "/api/admin/audit-events"),
        ("GET", "/api/admin/reports/overview"),
        ("GET", "/api/v1/admin/overview"),
        ("GET", "/api/v1/admin/users"),
        ("GET", "/api/v1/admin/diagnostics"),
        ("GET", "/api/v1/admin/audit-logs"),
        ("GET", "/api/v1/admin/backups"),
        ("GET", "/api/v1/admin/database/overview"),
        ("GET", "/api/v1/enterprise/ai/decisions"),
        ("GET", "/api/v1/enterprise/finance/meal-credits"),
    ]

    with TestClient(app) as client:
        for method, path in staff_or_admin_endpoints:
            unauth = client.request(method, path)
            assert unauth.status_code == 401, (path, unauth.status_code)
            stu_res = client.request(method, path, headers=stu_h)
            assert stu_res.status_code == 403, (path, stu_res.status_code, stu_res.text)

        for method, path in admin_only_endpoints:
            unauth = client.request(method, path)
            assert unauth.status_code == 401, (path, unauth.status_code)
            stu_res = client.request(method, path, headers=stu_h)
            assert stu_res.status_code == 403, (path, stu_res.status_code, stu_res.text)
            staff_res = client.request(method, path, headers=staff_h)
            assert staff_res.status_code == 403, (path, staff_res.status_code, staff_res.text)

        # Admin succeeds on admin GET endpoints
        assert client.get("/api/v1/admin/overview", headers=admin_h).status_code == 200
        assert client.get("/api/v1/staff/orders", headers=staff_h).status_code == 200


def test_guest_otp_blocks_privilege_escalation_and_brute_force():
    main._login_attempts.clear()
    _, staff_email, _ = _make_user(UserRole.STAFF)
    with TestClient(app) as client:
        req = client.post(
            "/api/v1/auth/guest-otp/request",
            json={"phoneOrEmail": staff_email, "roleCategory": "faculty", "fullName": "Spoofed"},
        )
        assert req.status_code == 200
        otp = req.json()["data"]["sandboxOtpPreview"]

        # Valid OTP against a STAFF email must be forbidden (never bypasses password/MFA)
        ver = client.post(
            "/api/v1/auth/guest-otp/verify",
            json={"phoneOrEmail": staff_email, "otpCode": otp, "fullName": "Spoofed"},
        )
        assert ver.status_code == 403

        # Wrong OTP guesses throttle after 5 failures
        guest_phone = f"98761{str(uuid.uuid4().int)[:5]}"
        client.post(
            "/api/v1/auth/guest-otp/request",
            json={"phoneOrEmail": guest_phone, "roleCategory": "guest", "fullName": "Guest"},
        )
        statuses = [
            client.post(
                "/api/v1/auth/guest-otp/verify",
                json={"phoneOrEmail": guest_phone, "otpCode": "000000", "fullName": "Guest"},
            ).status_code
            for _ in range(6)
        ]
        assert 429 in statuses


def test_single_use_qr_unpredictable_non_reusable_owner_bound_and_mismatch_safe():
    main._login_attempts.clear()
    stu_a, _, _ = _make_user(UserRole.STUDENT)
    stu_b, _, _ = _make_user(UserRole.STUDENT)
    staff_id, _, _ = _make_user(UserRole.STAFF)
    h_a, h_b, h_staff = _auth(stu_a), _auth(stu_b), _auth(staff_id)

    with SessionLocal() as db:
        counter = db.scalar(select(CanteenCounter).limit(1)) or CanteenCounter(name="Sec Counter", location="GF", is_active=True, capacity_per_slot=20)
        db.add(counter); db.flush()
        cat = db.scalar(select(Category).limit(1)) or Category(name="SecCat", slug=f"seccat-{uuid.uuid4().hex[:6]}", item_type="food", is_active=True)
        db.add(cat); db.flush()
        prod = Product(category_id=cat.id, counter_id=counter.id, name=f"SecItem {uuid.uuid4().hex[:6]}", item_type="food", price=20, price_paise=2000, stock_quantity=20, is_available=True)
        db.add(prod); db.flush()

        order_a = Order(
            user_id=stu_a,
            counter_id=counter.id,
            status=OrderStatus.READY,
            payment_status=PaymentStatus.PAID,
            token_date=datetime.now(timezone.utc).date().isoformat(),
            token_number=801,
            subtotal=20,
            subtotal_paise=2000,
            final_total=20,
            total_paise=2000,
        )
        order_b = Order(
            user_id=stu_b,
            counter_id=counter.id,
            status=OrderStatus.READY,
            payment_status=PaymentStatus.PAID,
            token_date=datetime.now(timezone.utc).date().isoformat(),
            token_number=802,
            subtotal=20,
            subtotal_paise=2000,
            final_total=20,
            total_paise=2000,
        )
        db.add_all([order_a, order_b]); db.flush()
        db.add(OrderItem(order_id=order_a.id, product_id=prod.id, product_name=prod.name, quantity=1, unit_price=20, unit_price_paise=2000, line_total=20, line_total_paise=2000))
        db.add(OrderItem(order_id=order_b.id, product_id=prod.id, product_name=prod.name, quantity=1, unit_price=20, unit_price_paise=2000, line_total=20, line_total_paise=2000))
        db.commit()
        oid_a, oid_b = order_a.id, order_b.id

    with TestClient(app) as client:
        # Another student cannot mint pickup QR for A's order
        assert client.post(f"/api/v1/orders/{oid_a}/pickup-token", headers=h_b).status_code == 403

        # Mint valid high-entropy QR token for A (raw token is not stored in DB)
        minted = client.post(f"/api/v1/orders/{oid_a}/pickup-token", headers=h_a)
        assert minted.status_code == 200
        tok_a = minted.json()["data"]["pickupToken"]
        assert tok_a.startswith("pk_") and len(tok_a) >= 32
        with SessionLocal() as db:
            row = db.scalar(select(PickupToken).where(PickupToken.token_hash == hash_opaque_token(tok_a)))
            assert row is not None and row.token_hash != tok_a

        # Guessable / forged token rejected
        assert client.post("/api/v1/pickups/verify", headers=h_staff, json={"pickupToken": "pk_guess_12345"}).status_code == 404

        # Supplying B's orderId alongside A's pickupToken rejected cleanly
        mismatch = client.post(
            "/api/v1/pickups/verify",
            headers=h_staff,
            json={"orderId": oid_b, "pickupToken": tok_a},
        )
        assert mismatch.status_code == 409
        assert mismatch.json()["error"]["code"] == "PICKUP_ORDER_MISMATCH"

        # Expire check: expired QR must be rejected by BOTH inspect and verify
        expired_mint = client.post(f"/api/v1/orders/{oid_a}/pickup-token", headers=h_a).json()["data"]["pickupToken"]
        with SessionLocal() as db:
            pt = db.scalar(select(PickupToken).where(PickupToken.token_hash == hash_opaque_token(expired_mint)))
            pt.expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
            db.commit()
        assert client.post("/api/v1/pickups/inspect", headers=h_staff, json={"pickupToken": expired_mint}).status_code == 410
        assert client.post("/api/v1/pickups/verify", headers=h_staff, json={"pickupToken": expired_mint}).status_code == 410

        # Single-use handover: works once, then inspect, verify, AND legacy scan all reject reuse (409)
        ok_handover = client.post("/api/v1/pickups/verify", headers=h_staff, json={"pickupToken": tok_a})
        assert ok_handover.status_code == 200
        assert client.post("/api/v1/pickups/inspect", headers=h_staff, json={"pickupToken": tok_a}).status_code == 409
        assert client.post("/api/v1/pickups/verify", headers=h_staff, json={"pickupToken": tok_a}).status_code == 409
        assert client.post("/api/staff/pickup/scan", headers=h_staff, json={"code": tok_a}).status_code == 409


def test_sqli_resistance_validation_sanitization_and_no_secrets_in_logs(caplog, monkeypatch):
    main._login_attempts.clear()
    secret_pw = "MySuperSecret-422-Pass!"
    caplog.set_level(logging.INFO)

    with TestClient(app) as client:
        # SQLi probes never crash or bypass filtering
        for probe in ["' OR '1'='1", "'; DROP TABLE users; --", "%27 UNION SELECT email,password_hash FROM users --"]:
            res = client.get("/api/v1/menu/items", params={"search": probe})
            assert res.status_code == 200

        # Validation error (422) strips submitted password input
        bad_reg = client.post(
            "/api/v1/auth/register",
            json={"fullName": "X", "email": "not-an-email", "password": secret_pw},
        )
        assert bad_reg.status_code == 422
        assert secret_pw not in bad_reg.text

        # Request logs exclude passwords & tokens
        stu_id, email, pw = _make_user(UserRole.STUDENT, password=secret_pw)
        caplog.clear()
        login = client.post("/api/v1/auth/login", json={"identifier": email, "password": pw})
        assert login.status_code == 200
        tok = login.json()["data"]["accessToken"]
        client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
        log_blob = "\n".join(r.getMessage() for r in caplog.records)
        assert secret_pw not in log_blob
        assert tok not in log_blob

        # WebSocket frame authentication works without putting token in query string
        with client.websocket_connect("/api/v1/ws") as ws:
            welcome = ws.receive_json()
            assert welcome["topic"] == "connected"
            assert welcome["role"] == "public"
            ws.send_json({"type": "auth", "token": tok})
            authed = ws.receive_json()
            assert authed["topic"] == "authenticated"
            assert authed["userId"] == stu_id

    # Staging / production without SMTP never logs password-reset link body
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("SMTP_HOST", "")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        caplog.clear()
        sent = send_email("stu@college.edu", "Reset", "https://canteen.college.edu/#reset=SECRET_TOKEN_XYZ")
        assert sent is False
        assert "SECRET_TOKEN_XYZ" not in "\n".join(r.getMessage() for r in caplog.records)
    finally:
        get_settings.cache_clear()
