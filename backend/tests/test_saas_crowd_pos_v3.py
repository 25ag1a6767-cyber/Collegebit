"""Comprehensive verification for CollegeBits 3.0 Crowd Reduction, Smart Pickup Slots,
Pre-Packing Labels, Strict Online Payment Policy, Guest/Faculty OTP, Walk-In Staff POS,
Daily Financial Settlements, and Multi-College SaaS Tenant Isolation (College A vs College B).
"""
from __future__ import annotations

import uuid
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import CanteenCounter, Category, Product, User, UserRole
from app.security import create_access_token, hash_password


def _mk_user(role: UserRole, prefix: str) -> tuple[int, str, str]:
    email = f"{prefix}-{uuid.uuid4().hex[:8]}@aceec.ac.in"
    with SessionLocal() as db:
        u = User(
            full_name=f"{prefix.title()} User",
            email=email,
            password_hash=hash_password("Pass#CollegeBits3!"),
            role=role,
            is_active=True,
        )
        db.add(u)
        db.commit()
        db.refresh(u)
        return u.id, email, create_access_token(u, mfa_verified=True)


def _mk_product(stock: int = 200, price_paise: int = 5000) -> int:
    with SessionLocal() as db:
        cnt = db.query(CanteenCounter).first()
        if not cnt:
            cnt = CanteenCounter(name="Counter 1", location="Ground Floor", capacity_per_slot=300, is_active=True)
            db.add(cnt)
            db.flush()
        cat = db.query(Category).first()
        if not cat:
            cat = Category(name="Meals", slug=f"meals-{uuid.uuid4().hex[:6]}", item_type="food", is_active=True)
            db.add(cat)
            db.flush()
        p = Product(
            category_id=cat.id,
            counter_id=cnt.id,
            name=f"Masala Dosa {uuid.uuid4().hex[:4]}",
            item_type="food",
            price=price_paise / 100,
            price_paise=price_paise,
            cost_paise=2000,
            stock_quantity=stock,
            is_available=True,
        )
        db.add(p)
        db.commit()
        db.refresh(p)
        return p.id


def test_crowd_reduction_pickup_slots_overbooking_prepacking_and_browser_qr():
    _, _, stu_tok = _mk_user(UserRole.STUDENT, "slot-stu")
    _, _, staff_tok = _mk_user(UserRole.STAFF, "slot-stf")
    _, _, adm_tok = _mk_user(UserRole.ADMIN, "slot-adm")

    stu_h = {"Authorization": f"Bearer {stu_tok}"}
    staff_h = {"Authorization": f"Bearer {staff_tok}"}
    adm_h = {"Authorization": f"Bearer {adm_tok}"}

    with TestClient(app) as client:
        # 1. Browser-first QR ordering poster (no mandatory app installation)
        qr_res = client.get("/api/v1/qr/entrance-poster")
        assert qr_res.status_code == 200
        qr_data = qr_res.json()["data"]
        assert qr_data["noAppInstallRequired"] is True
        assert len(qr_data["placements"]) >= 4

        # 2. Admin configures a pickup slot with maxCapacity = 40
        slot_cfg = client.post(
            "/api/v1/admin/pickup-slots",
            headers=adm_h,
            json={"counterId": 1, "slotLabel": "12:15-12:30", "maxCapacity": 40, "isActive": True},
        )
        assert slot_cfg.status_code == 200, slot_cfg.text
        slot_id = slot_cfg.json()["data"]["id"]

        # 3. Student books 40 meals -> succeeds; 41st meal -> rejected with 409 SLOT_CAPACITY_EXCEEDED
        book_ok = client.post("/api/v1/pickup-slots/book", headers=stu_h, json={"slotId": slot_id, "quantity": 40})
        assert book_ok.status_code == 200
        assert book_ok.json()["data"]["availableCapacity"] == 0

        book_over = client.post("/api/v1/pickup-slots/book", headers=stu_h, json={"slotId": slot_id, "quantity": 1})
        assert book_over.status_code == 409
        assert book_over.json()["error"]["code"] == "SLOT_CAPACITY_EXCEEDED"

        # 4. Pre-packing list & Crowd analytics dashboard
        pack_res = client.get("/api/v1/kitchen/packing-list", headers=staff_h)
        assert pack_res.status_code == 200
        assert "counter1IndividualLabels" in pack_res.json()["data"]
        assert "counter2BulkCrateLabels" in pack_res.json()["data"]

        crowd_res = client.get("/api/v1/admin/crowd-analytics", headers=adm_h)
        assert crowd_res.status_code == 200
        c_data = crowd_res.json()["data"]
        assert isinstance(c_data["studentsExpectedPerSlot"], list)
        assert "counterUtilization" in c_data


def test_guest_and_faculty_otp_strict_online_payment_methods_walkin_pos_and_settlements():
    _, _, staff_tok = _mk_user(UserRole.STAFF, "pos-stf")
    _, _, adm_tok = _mk_user(UserRole.ADMIN, "pos-adm")
    prod_id = _mk_product(stock=100, price_paise=10000)

    staff_h = {"Authorization": f"Bearer {staff_tok}"}
    adm_h = {"Authorization": f"Bearer {adm_tok}"}

    with TestClient(app) as client:
        # 1. Strict Online Payment Policy: only UPI & Approved Wallet allowed; cash/cards/netbanking/EMI prohibited
        pm_res = client.get("/api/v1/payments/methods")
        assert pm_res.status_code == 200
        pm_data = pm_res.json()["data"]
        assert pm_data["mandatoryOnlinePayment"] is True
        assert pm_data["strictNoRefundPolicy"] is True
        allowed_codes = [m["code"] for m in pm_data["allowedMethods"]]
        assert allowed_codes == ["upi", "prepaid_wallet"]
        assert "cash_at_counter" in pm_data["prohibitedMethods"]
        assert "credit_card" in pm_data["prohibitedMethods"]
        assert "net_banking" in pm_data["prohibitedMethods"]

        # 2. Guest 10-digit Indian mobile validation & OTP verification
        bad_guest = client.post(
            "/api/v1/auth/guest-otp/request",
            json={"phoneOrEmail": "12345", "roleCategory": "guest", "fullName": "Visitor"},
        )
        assert bad_guest.status_code == 422
        assert bad_guest.json()["error"]["code"] == "INVALID_INDIAN_MOBILE"

        valid_phone = f"98765{str(uuid.uuid4().int)[:5]}"
        otp_req = client.post(
            "/api/v1/auth/guest-otp/request",
            json={"phoneOrEmail": valid_phone, "roleCategory": "guest", "fullName": "Parent Visitor"},
        )
        assert otp_req.status_code == 200, otp_req.text
        otp_code = otp_req.json()["data"]["sandboxOtpPreview"]
        assert otp_code and len(otp_code) == 6

        otp_ver = client.post(
            "/api/v1/auth/guest-otp/verify",
            json={"phoneOrEmail": valid_phone, "otpCode": otp_code, "fullName": "Parent Visitor"},
        )
        assert otp_ver.status_code == 200, otp_ver.text
        assert otp_ver.json()["data"]["roleCategory"] == "GUEST"
        assert otp_ver.json()["data"]["pricingTier"] == "GUEST_PRICING"

        # 3. Walk-In Staff POS: calculates campus vs guest pricing on backend & updates daily settlement
        pos_stu = client.post(
            "/api/v1/pos/walkin-orders",
            headers=staff_h,
            json={"counterId": 1, "customerCategory": "student", "paymentMethod": "cash", "items": [{"menuItemId": prod_id, "quantity": 1}]},
        )
        assert pos_stu.status_code == 201
        assert pos_stu.json()["data"]["totalPaise"] == 10000  # Base campus price ₹100.00

        pos_guest = client.post(
            "/api/v1/pos/walkin-orders",
            headers=staff_h,
            json={"counterId": 1, "customerCategory": "guest", "paymentMethod": "cash", "items": [{"menuItemId": prod_id, "quantity": 1}]},
        )
        assert pos_guest.status_code == 201
        assert pos_guest.json()["data"]["totalPaise"] == 11500  # Guest price with 15% markup (₹115.00)

        settlement = client.get("/api/v1/admin/settlements/daily", headers=adm_h)
        assert settlement.status_code == 200
        s_data = settlement.json()["data"]
        assert s_data["walkinCashCollectionsPaise"] >= 21500
        assert s_data["netVendorSettlementPaise"] >= 21500


def test_multitenant_saas_isolation_college_a_vs_college_b_entitlements_and_suspension():
    _, _, super_adm_tok = _mk_user(UserRole.ADMIN, "saas-super")
    adm_a_id, _, adm_a_tok = _mk_user(UserRole.STUDENT, "admin-college-a")
    adm_b_id, _, adm_b_tok = _mk_user(UserRole.STUDENT, "admin-college-b")

    super_h = {"Authorization": f"Bearer {super_adm_tok}"}
    col_a_h = {"Authorization": f"Bearer {adm_a_tok}"}
    col_b_h = {"Authorization": f"Bearer {adm_b_tok}"}

    with TestClient(app) as client:
        # 1. Provision College A (STARTER plan: max 1 campus) and College B (ENTERPRISE plan: max 20 campuses)
        slug_a = f"college-a-{uuid.uuid4().hex[:6]}"
        slug_b = f"college-b-{uuid.uuid4().hex[:6]}"

        t_a = client.post(
            "/api/v1/saas/tenants",
            headers=super_h,
            json={"tenantSlug": slug_a, "collegeName": "College A Institute", "planTier": "STARTER", "adminUserId": adm_a_id},
        ).json()["data"]
        t_b = client.post(
            "/api/v1/saas/tenants",
            headers=super_h,
            json={"tenantSlug": slug_b, "collegeName": "College B University", "planTier": "ENTERPRISE", "adminUserId": adm_b_id},
        ).json()["data"]

        tid_a, tid_b = t_a["id"], t_b["id"]

        # 2. Enforce Plan Entitlements on Backend: College A (STARTER) can create 1 campus, 2nd fails with 403
        camp_a1 = client.post(f"/api/v1/saas/tenants/{tid_a}/campuses", headers=col_a_h, json={"campusName": "Main Campus A", "city": "Hyderabad"})
        assert camp_a1.status_code == 201
        camp_a2 = client.post(f"/api/v1/saas/tenants/{tid_a}/campuses", headers=col_a_h, json={"campusName": "Second Campus A", "city": "Warangal"})
        assert camp_a2.status_code == 403
        assert camp_a2.json()["error"]["code"] == "PLAN_ENTITLEMENT_EXCEEDED"

        # 3. Configure Encrypted Merchant Gateway for College A and College B independently
        gw_a = client.put(
            f"/api/v1/saas/tenants/{tid_a}/gateway-config",
            headers=col_a_h,
            json={
                "merchantAccountId": "acc_college_a_001",
                "razorpayKeyId": "rzp_live_collegeA",
                "razorpayKeySecret": "sec_collegeA_secret_999",
                "razorpayWebhookSecret": "whsec_collegeA_999",
            },
        )
        assert gw_a.status_code == 200
        assert gw_a.json()["data"]["secretsEncryptedAtRest"] is True
        assert gw_a.json()["data"]["secretsDisplayed"] is False

        gw_b = client.put(
            f"/api/v1/saas/tenants/{tid_b}/gateway-config",
            headers=col_b_h,
            json={
                "merchantAccountId": "acc_college_b_002",
                "razorpayKeyId": "rzp_live_collegeB",
                "razorpayKeySecret": "sec_collegeB_secret_888",
                "razorpayWebhookSecret": "whsec_collegeB_888",
            },
        )
        assert gw_b.status_code == 200

        # 4. Complete 15-step onboarding and create orders in both tenants
        assert client.patch(f"/api/v1/saas/tenants/{tid_a}/onboarding", headers=col_a_h, json={"step": 15, "markCompleted": True}).status_code == 200
        assert client.patch(f"/api/v1/saas/tenants/{tid_b}/onboarding", headers=col_b_h, json={"step": 15, "markCompleted": True}).status_code == 200

        ord_a = client.post(
            f"/api/v1/saas/tenants/{tid_a}/orders",
            headers=col_a_h,
            json={"itemSummary": "College A Veg Thali x 1", "amountPaise": 7000},
        )
        assert ord_a.status_code == 201

        ord_b = client.post(
            f"/api/v1/saas/tenants/{tid_b}/orders",
            headers=col_b_h,
            json={"itemSummary": "College B Special Biryani x 1", "amountPaise": 12000},
        )
        assert ord_b.status_code == 201

        # 5. STRICT CROSS-TENANT ISOLATION TESTS (College A -> College B must be 403 TENANT_ISOLATION_VIOLATION)
        # College A cannot view College B orders
        cross_orders = client.get(f"/api/v1/saas/tenants/{tid_b}/orders", headers=col_a_h)
        assert cross_orders.status_code == 403
        assert cross_orders.json()["error"]["code"] == "TENANT_ISOLATION_VIOLATION"

        # College A cannot read College B gateway config
        cross_gw = client.get(f"/api/v1/saas/tenants/{tid_b}/gateway-config", headers=col_a_h)
        assert cross_gw.status_code == 403
        assert cross_gw.json()["error"]["code"] == "TENANT_ISOLATION_VIOLATION"

        # College A cannot export College B data
        cross_exp = client.get(f"/api/v1/saas/tenants/{tid_b}/export", headers=col_a_h)
        assert cross_exp.status_code == 403
        assert cross_exp.json()["error"]["code"] == "TENANT_ISOLATION_VIOLATION"

        # Forged X-Tenant-ID header is rejected even on own endpoint
        forged_hdr = client.get(
            f"/api/v1/saas/tenants/{tid_a}/orders",
            headers={**col_a_h, "X-Tenant-ID": str(tid_b)},
        )
        assert forged_hdr.status_code == 403
        assert forged_hdr.json()["error"]["code"] == "TENANT_ISOLATION_VIOLATION"

        # 6. Tenant Suspension preserves data and permits read-only export
        susp = client.post(f"/api/v1/saas/tenants/{tid_a}/suspend", headers=super_h)
        assert susp.status_code == 200
        assert susp.json()["data"]["status"] == "suspended"

        blocked_ord = client.post(
            f"/api/v1/saas/tenants/{tid_a}/orders",
            headers=col_a_h,
            json={"itemSummary": "Order while suspended", "amountPaise": 5000},
        )
        assert blocked_ord.status_code == 403
        assert blocked_ord.json()["error"]["code"] == "TENANT_SUSPENDED"

        # Read-only export still works while suspended so college retains data ownership
        exp_a = client.get(f"/api/v1/saas/tenants/{tid_a}/export", headers=col_a_h)
        assert exp_a.status_code == 200
        assert len(exp_a.json()["data"]["orders"]) == 1

        # Reactivate tenant
        react = client.post(f"/api/v1/saas/tenants/{tid_a}/reactivate", headers=super_h)
        assert react.status_code == 200
        assert react.json()["data"]["status"] == "active"
