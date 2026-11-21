"""Regression and coverage suite for Phase 1-10 audit repairs and previously untested core modules."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import json
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import (
    BulkMealToken,
    BulkOrder,
    BulkOrderPayment,
    BulkOrderRecipient,
    CanteenCounter,
    Category,
    Discount,
    KitchenStation,
    MealPassPlan,
    Order,
    OrderItem,
    OrderStatus,
    PaymentIncident,
    PaymentStatus,
    Product,
    StaffShift,
    StudentMealPass,
    User,
    UserCounterAssignment,
    UserRole,
)
from app.modules.reporting_engine import render_csv_bytes
from app.security import create_access_token, hash_password


def _mk_user(role: UserRole, *, is_active: bool = True, password: str = "AuditPass-2026!") -> tuple[int, str, dict[str, str]]:
    tag = uuid.uuid4().hex[:8]
    email = f"audit-{role.value}-{tag}@aceec.ac.in"
    with SessionLocal() as db:
        u = User(
            full_name=f"Audit {role.value.title()} {tag}",
            email=email,
            password_hash=hash_password(password),
            role=role,
            is_active=is_active,
            email_verified_at=datetime.now(timezone.utc),
        )
        db.add(u)
        db.commit()
        db.refresh(u)
        tok = create_access_token(u, mfa_verified=True)
        return u.id, email, {"Authorization": f"Bearer {tok}"}


def _mk_product(stock: int = 20, price_paise: int = 5000, counter_id: int | None = None) -> tuple[int, int]:
    tag = uuid.uuid4().hex[:6]
    with SessionLocal() as db:
        cnt = db.get(CanteenCounter, counter_id) if counter_id else db.scalar(select(CanteenCounter).limit(1))
        if cnt is None:
            cnt = CanteenCounter(name=f"Main-{tag}", location="Block A", capacity_per_slot=30, is_active=True)
            db.add(cnt)
            db.flush()
        cat = db.scalar(select(Category).where(Category.is_active.is_(True)).limit(1))
        if cat is None:
            cat = Category(name=f"Snacks-{tag}", slug=f"snacks-{tag}", item_type="food", is_active=True)
            db.add(cat)
            db.flush()
        prod = Product(
            category_id=cat.id,
            counter_id=cnt.id,
            name=f"Audit Dosa {tag}",
            item_type="food",
            price=Decimal(price_paise) / Decimal("100"),
            price_paise=price_paise,
            stock_quantity=stock,
            is_available=True,
            is_archived=False,
        )
        db.add(prod)
        db.commit()
        db.refresh(prod)
        return prod.id, cnt.id


def test_cart_favorites_wallet_order_qr_single_use_and_no_refund_incident(monkeypatch):
    stu_id, _, stu_h = _mk_user(UserRole.STUDENT)
    _, _, stf_h = _mk_user(UserRole.STAFF)
    _, _, adm_h = _mk_user(UserRole.ADMIN)
    prod_id, cnt_id = _mk_product(stock=15, price_paise=4000)

    with TestClient(app) as client:
        # Favorites CRUD
        assert client.post(f"/api/v1/favorites/{prod_id}", headers=stu_h).status_code == 201
        favs = client.get("/api/v1/favorites", headers=stu_h).json()["data"]["items"]
        assert any(it["id"] == prod_id for it in favs)
        assert client.delete(f"/api/v1/favorites/{prod_id}", headers=stu_h).status_code == 200

        # Cart CRUD + pickup slots
        put_res = client.put(
            f"/api/v1/cart/items/{prod_id}",
            headers=stu_h,
            json={"quantity": 2, "fulfillmentType": "READY_TO_EAT"},
        )
        assert put_res.status_code == 200
        assert put_res.json()["data"]["items"][0]["quantity"] == 2
        patch_res = client.patch("/api/v1/cart", headers=stu_h, json={"notes": "Less oil"})
        assert patch_res.status_code == 200
        assert client.get("/api/v1/cart", headers=stu_h).json()["data"]["notes"] == "Less oil"
        assert client.get("/api/v1/pickup-slots", headers=stu_h).status_code == 200
        assert client.get("/api/v1/pickup-slots/windows", headers=stu_h).status_code == 200

        # Create order from cart (unpaid)
        ord_res = client.post("/api/v1/orders", headers=stu_h, json={"fromCart": True, "counterId": cnt_id})
        assert ord_res.status_code == 201, ord_res.text
        order = ord_res.json()["data"]["order"]
        order_id = order["id"]
        total_paise = order["totalPaise"]

        # Insufficient wallet balance rejected cleanly
        fail_wal = client.post(f"/api/v1/orders/{order_id}/pay-with-wallet", headers=stu_h)
        assert fail_wal.status_code == 409

        # Admin credits prepaid wallet, student pays order with wallet
        credit_res = client.post(
            "/api/v1/admin/wallet/credit",
            headers=adm_h,
            json={
                "userId": stu_id,
                "amountPaise": total_paise * 2 + 5000,
                "reason": "Audit prepaid topup",
                "referenceKey": f"cred-{uuid.uuid4().hex[:10]}",
            },
        )
        assert credit_res.status_code == 201
        paid_res = client.post(f"/api/v1/orders/{order_id}/pay-with-wallet", headers=stu_h)
        assert paid_res.status_code == 200, paid_res.text
        assert paid_res.json()["data"]["paymentStatus"] == "paid"

        # Student cannot cancel paid order (strict no-refunds policy)
        stu_cancel = client.post(f"/api/v1/orders/{order_id}/cancel", headers=stu_h, json={"reason": "Changed mind"})
        assert stu_cancel.status_code == 409

        # Student mints pickup QR, Staff verifies ONCE via /api/v1/pickup/verify fallback (pk_...)
        qr_res = client.post(f"/api/v1/orders/{order_id}/pickup-token", headers=stu_h)
        assert qr_res.status_code == 200, qr_res.text
        pk_tok = qr_res.json()["data"]["pickupToken"]

        # Move to READY before collection
        assert client.post(f"/api/v1/staff/orders/{order_id}/accept", headers=stf_h).status_code == 200
        assert client.post(f"/api/v1/staff/orders/{order_id}/start", headers=stf_h).status_code == 200
        assert client.post(f"/api/v1/staff/orders/{order_id}/ready", headers=stf_h).status_code == 200

        # Also test /api/v1/pickup/verify fallback with pk_... token!
        col_res = client.post("/api/v1/pickup/verify", headers=stf_h, json={"pickupToken": pk_tok})
        assert col_res.status_code == 200, col_res.text

        # Duplicate QR replay blocked!
        dup_res = client.post("/api/v1/pickup/verify", headers=stf_h, json={"pickupToken": pk_tok})
        assert dup_res.status_code == 409

        # Create a 2nd paid order and test Admin cancel -> PaymentIncident recorded
        ord2_res = client.post(
            "/api/v1/orders",
            headers=stu_h,
            json={"items": [{"menuItemId": prod_id, "quantity": 1}], "counterId": cnt_id, "payWithWallet": True},
        )
        if ord2_res.status_code != 201:
            ord2_res = client.post(
                "/api/v1/orders",
                headers=stu_h,
                json={"items": [{"menuItemId": prod_id, "quantity": 1}], "counterId": cnt_id},
            )
            oid2 = ord2_res.json()["data"]["order"]["id"]
            assert client.post(f"/api/v1/orders/{oid2}/pay-with-wallet", headers=stu_h).status_code == 200
        else:
            oid2 = ord2_res.json()["data"]["order"]["id"]

        ov = client.post(
            f"/api/v1/admin/orders/{oid2}/status",
            headers=adm_h,
            json={"status": "cancelled", "reason": "Equipment fault"},
        )
        assert ov.status_code == 200, ov.text
        assert ov.json()["data"]["incidentFlagged"] is True
        with SessionLocal() as db:
            inc = db.scalar(select(PaymentIncident).where(PaymentIncident.order_id == oid2))
            assert inc is not None and inc.incident_type == "admin_cancelled_paid_order"


def test_bulk_menu_cancelled_bulk_qr_payment_auth_and_csv_injection():
    _, _, stu1_h = _mk_user(UserRole.STUDENT)
    stu2_id, _, stu2_h = _mk_user(UserRole.STUDENT)
    _, _, adm_h = _mk_user(UserRole.ADMIN)
    _, _, stf_h = _mk_user(UserRole.STAFF)
    prod_id, cnt_id = _mk_product(stock=50, price_paise=3000)

    # CSV formula injection neutralization
    csv_bytes = render_csv_bytes(["Col", "=BAD"], [["=CMD()", "+1+1", "-2+3", "@SUM(A1)", "Normal"]]).decode("utf-8")
    assert "'=BAD" in csv_bytes
    assert "'=CMD()" in csv_bytes
    assert "'+1+1" in csv_bytes
    assert "'-2+3" in csv_bytes
    assert "'@SUM(A1)" in csv_bytes

    with TestClient(app) as client:
        # Fixed GET /api/v1/menu alias (formerly crashed with prep_time_minutes AttributeError)
        menu_res = client.get("/api/v1/menu", headers=stu1_h)
        assert menu_res.status_code == 200, menu_res.text
        assert any("prepTimeMinutes" in it for it in menu_res.json()["data"]["items"])

        # Seed a bulk order owned by stu2 with cancelled status & an active token + unverified payment
        gw_ord = f"order_blk_{uuid.uuid4().hex[:10]}"
        raw_tok = f"bmt_{uuid.uuid4().hex}"
        tok_hash = hashlib.sha256(raw_tok.encode("utf-8")).hexdigest()
        with SessionLocal() as db:
            bo = BulkOrder(
                bulk_order_number=f"BLK-{uuid.uuid4().hex[:6].upper()}",
                coordinator_id=stu2_id,
                counter_id=cnt_id,
                department_name="Dept Audit",
                event_title="Cancelled Fest",
                pickup_date=date.today(),
                pickup_slot="12:45-13:00",
                payment_mode="centralized",
                distribution_mode="direct_student",
                status="cancelled",
                payment_status="paid",
                total_paise=3000,
                paid_paise=3000,
            )
            db.add(bo)
            db.flush()
            rec = BulkOrderRecipient(
                bulk_order_id=bo.id,
                recipient_identifier="24AUDIT01",
                full_name="Audit Recipient",
                payment_status="paid",
                pickup_status="pending",
                amount_paise=3000,
            )
            db.add(rec)
            db.flush()
            db.add(
                BulkMealToken(
                    bulk_order_id=bo.id,
                    recipient_id=rec.id,
                    token_hash=tok_hash,
                    key_nonce="n",
                    distribution_mode="direct_student",
                    status="active",
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
                )
            )
            db.add(
                BulkOrderPayment(
                    bulk_order_id=bo.id,
                    payer_user_id=stu2_id,
                    provider="razorpay",
                    idempotency_key=f"idem-{uuid.uuid4().hex[:10]}",
                    provider_order_id=gw_ord,
                    amount_paise=3000,
                    currency="INR",
                    status="CREATED",
                )
            )
            bo_id = bo.id
            db.commit()

        # stu1 cannot verify stu2's bulk payment!
        unauth_v = client.post(
            f"/api/v1/bulk-orders/{bo_id}/payments/verify",
            headers=stu1_h,
            json={"razorpayOrderId": gw_ord, "razorpayPaymentId": "pay_unauthorized", "razorpaySignature": "sig_unauthorized_123"},
        )
        assert unauth_v.status_code == 403

        # Cancelled bulk order QR cannot be collected!
        can_scan = client.post("/api/v1/pickup/verify", headers=stf_h, json={"qrToken": raw_tok, "counterId": cnt_id})
        assert can_scan.status_code == 409


def test_waste_stock_decrement_shift_idor_meal_pass_and_station_capacity():
    stu1_id, _, stu1_h = _mk_user(UserRole.STUDENT)
    stu2_id, _, stu2_h = _mk_user(UserRole.STUDENT)
    stf1_id, _, stf1_h = _mk_user(UserRole.STAFF)
    stf2_id, _, stf2_h = _mk_user(UserRole.STAFF)
    adm_id, _, adm_h = _mk_user(UserRole.ADMIN)
    prod_id, cnt_id = _mk_product(stock=12, price_paise=2500)

    with TestClient(app) as client:
        # Waste deduction actually updates Product.stock_quantity
        w_res = client.post(
            "/api/v1/waste/records",
            headers=stf1_h,
            json={
                "productId": prod_id,
                "itemName": "Audit Dosa",
                "wasteCategory": "spoiled_food",
                "quantity": 4,
                "reason": "Batter spoiled",
                "idempotencyKey": f"w-{uuid.uuid4().hex[:8]}",
            },
        )
        assert w_res.status_code == 201, w_res.text
        with SessionLocal() as db:
            p = db.get(Product, prod_id)
            assert p.stock_quantity == 8

        # Shift check-out IDOR prevention
        with SessionLocal() as db:
            sh = StaffShift(
                staff_user_id=stf1_id,
                station_role="kitchen_prep",
                shift_start=datetime.now(timezone.utc) - timedelta(hours=1),
                shift_end=datetime.now(timezone.utc) + timedelta(hours=5),
                status="scheduled",
                created_by_manager_id=adm_id,
            )
            db.add(sh)
            # Also assign stf2 ONLY to another counter to verify counter isolation
            other_cnt = CanteenCounter(name=f"Other-{uuid.uuid4().hex[:5]}", location="Block Z", capacity_per_slot=10, is_active=True)
            db.add(other_cnt)
            db.flush()
            st_other = KitchenStation(counter_id=other_cnt.id, name=f"St-{uuid.uuid4().hex[:5]}", station_type="general", max_concurrent_orders=5, auto_paused=True, is_active=True)
            db.add(st_other)
            db.add(UserCounterAssignment(user_id=stf2_id, counter_id=other_cnt.id))
            db.commit()
            sh_id, st_other_id = sh.id, st_other.id

        assert client.post(f"/api/v1/workforce/shifts/{sh_id}/check-out", headers=stf2_h).status_code == 403
        assert client.post(f"/api/v1/workforce/shifts/{sh_id}/check-out", headers=stf1_h).status_code == 200

        # Counter isolation on reopen_kitchen_station_v1
        with SessionLocal() as db:
            st_main = KitchenStation(counter_id=cnt_id, name=f"MainSt-{uuid.uuid4().hex[:5]}", station_type="grill", max_concurrent_orders=2, auto_paused=True, is_active=True)
            db.add(st_main)
            db.commit()
            st_main_id = st_main.id
        assert client.post(f"/api/v1/kitchen/stations/{st_main_id}/reopen", headers=stf2_h).status_code == 403
        assert client.post(f"/api/v1/kitchen/stations/{st_other_id}/reopen", headers=stf2_h).status_code == 200

        # Meal pass: signature rejection on bad sig, ownership & expiry on redeem
        with SessionLocal() as db:
            plan = MealPassPlan(plan_code=f"AUD{uuid.uuid4().hex[:4].upper()}", name="Audit Lunch", pass_period="weekly", total_meals=5, validity_days=7, price_paise=10000, is_active=True)
            db.add(plan)
            db.flush()
            exp_pass = StudentMealPass(
                user_id=stu1_id,
                plan_id=plan.id,
                status="active",
                remaining_meals=3,
                expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            )
            ok_pass = StudentMealPass(
                user_id=stu1_id,
                plan_id=plan.id,
                status="payment_pending",
                gateway_order_id="order_mp_sigtest",
                remaining_meals=5,
                expires_at=datetime.now(timezone.utc) + timedelta(days=5),
            )
            db.add_all([exp_pass, ok_pass])
            db.commit()
            exp_pass_id, ok_pass_id = exp_pass.id, ok_pass.id

        # Bad HMAC rejected even when gatewaySignature passed
        bad_act = client.post(
            f"/api/v1/meal-passes/{ok_pass_id}/activate-verified?gatewayPaymentId=pay_12345&gatewaySignature=invalidsig",
            headers=stu1_h,
        )
        assert bad_act.status_code in (400, 503)

        # Peer student cannot redeem stu1's expired or valid pass (403)
        assert client.post(f"/api/v1/meal-passes/{exp_pass_id}/redeem", headers=stu2_h).status_code == 403
        # Owner cannot redeem expired pass (409)
        assert client.post(f"/api/v1/meal-passes/{exp_pass_id}/redeem", headers=stu1_h).status_code == 409


def test_zero_total_order_coupon_restore_webhook_list_mobileconfig_and_auth_fixes(monkeypatch):
    stu_id, stu_email, stu_h = _mk_user(UserRole.STUDENT)
    adm_id, _, adm_h = _mk_user(UserRole.ADMIN)
    prod_id, cnt_id = _mk_product(stock=10, price_paise=5000)

    # 1. Order.effective_total_paise handles legitimate ₹0.00 discounted orders!
    with SessionLocal() as db:
        zero_ord = Order(
            user_id=stu_id,
            counter_id=cnt_id,
            status=OrderStatus.PENDING,
            payment_status=PaymentStatus.PENDING,
            subtotal=Decimal("50.00"),
            discount_amount=Decimal("50.00"),
            final_total=Decimal("0.00"),
        )
        db.add(zero_ord)
        db.commit()
        db.refresh(zero_ord)
        assert zero_ord.effective_total_paise == 0

        # Also test unpaid coupon restoration when cancelled
        code = f"FREE100_{uuid.uuid4().hex[:4].upper()}"
        disc = Discount(code=code, description="50% off", discount_percent=Decimal("50.00"), usage_limit=2, is_active=True)
        db.add(disc)
        db.commit()

    sent_emails: list[tuple[str, str, str]] = []
    monkeypatch.setattr("app.modules.saas_admin_governance_v3.send_email", lambda to, subj, body: sent_emails.append((to, subj, body)))

    with TestClient(app) as client:
        # 2. Create unpaid order with coupon -> usage_count becomes 1 -> cancel -> usage_count drops back to 0!
        o_res = client.post(
            "/api/v1/orders",
            headers=stu_h,
            json={"items": [{"menuItemId": prod_id, "quantity": 1}], "counterId": cnt_id, "couponCode": code},
        )
        assert o_res.status_code == 201, o_res.text
        oid = o_res.json()["data"]["order"]["id"]
        with SessionLocal() as db:
            assert db.scalar(select(Discount).where(Discount.code == code)).usage_count == 1
        assert client.post(f"/api/v1/orders/{oid}/cancel", headers=stu_h, json={"reason": "Retry"}).status_code == 200
        with SessionLocal() as db:
            assert db.scalar(select(Discount).where(Discount.code == code)).usage_count == 0

        # 3. Non-dict JSON webhook returns 400 cleanly instead of 500
        cfg = get_settings()
        wh_secret = cfg.razorpay_webhook_secret or "audit_wh_secret"
        monkeypatch.setattr(cfg, "razorpay_webhook_secret", wh_secret)
        body_bytes = json.dumps([1, 2, 3]).encode("utf-8")
        sig = hmac.new(wh_secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
        wh_res = client.post(
            "/api/v1/payments/webhooks",
            content=body_bytes,
            headers={"Content-Type": "application/json", "X-Razorpay-Signature": sig},
        )
        assert wh_res.status_code == 400

        # 4. .mobileconfig sanitizes malicious Host header
        mc_res = client.get("/download/ios.mobileconfig", headers={"Host": 'evil.com"><script>alert(1)</script>'})
        assert mc_res.status_code == 200
        assert "<script>" not in mc_res.text and "evil.com" not in mc_res.text

        # 5. Deactivated user blocked from Guest/Role OTP verification
        inact_id, inact_email, _ = _mk_user(UserRole.STUDENT, is_active=False)
        req_otp = client.post("/api/v1/auth/guest-otp/request", json={"phoneOrEmail": inact_email, "roleCategory": "faculty"})
        assert req_otp.status_code == 200, req_otp.text
        code_otp = req_otp.json()["data"].get("sandboxOtpPreview")
        assert code_otp
        ver_otp = client.post(
            "/api/v1/auth/guest-otp/verify",
            json={"phoneOrEmail": inact_email, "otpCode": code_otp, "fullName": "Inactive"},
        )
        assert ver_otp.status_code == 403

        # 6. /api/v1/auth/password/forgot actually queues email & reset works
        fp = client.post("/api/v1/auth/password/forgot", json={"email": stu_email})
        assert fp.status_code == 200
        rst_tok = fp.json()["data"].get("sandboxResetToken")
        assert len(sent_emails) >= 1 and sent_emails[-1][0] == stu_email
        if rst_tok:
            rp = client.post("/api/v1/auth/password/reset", json={"resetToken": rst_tok, "newPassword": "NewAuditPass#2026!"})
            assert rp.status_code == 200

        # 7. Invitation acceptance for an EXISTING user must NOT overwrite their password!
        from app.models import AdminInvitation, SaasTenant
        with SessionLocal() as db:
            tenant = SaasTenant(tenant_slug=f"t-{uuid.uuid4().hex[:6]}", college_name="Audit College", status="active", plan_tier="ENTERPRISE")
            db.add(tenant)
            db.flush()
            inv_tok = f"inv_{tenant.id}_{uuid.uuid4().hex}"
            db.add(
                AdminInvitation(
                    tenant_id=tenant.id,
                    email=stu_email,
                    full_name="Existing User",
                    tenant_role="college_admin",
                    token_hash=hashlib.sha256(inv_tok.encode("utf-8")).hexdigest(),
                    status="pending",
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
                    invited_by_user_id=adm_id,
                )
            )
            db.commit()
        acc = client.post("/api/v1/saas/invitations/accept", json={"inviteToken": inv_tok, "password": "AttackerNewPassword#123!"})
        assert acc.status_code == 200
        # Verify password stayed NewAuditPass#2026! and was NOT overwritten by AttackerNewPassword#123!
        assert client.post("/api/v1/auth/login", json={"identifier": stu_email, "password": "AttackerNewPassword#123!"}).status_code == 401
        assert client.post("/api/v1/auth/login", json={"identifier": stu_email, "password": "NewAuditPass#2026!"}).status_code == 200
