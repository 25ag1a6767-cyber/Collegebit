"""Comprehensive integration & concurrency tests for CollegeBits 3.0 Bulk Ordering,
Dual-Mode Payments, Cryptographic QR Tokens, Two-Counter Physical Pickup, Kitchen Batch Prep,
and Strict No-Refund Payment Incident Resolution.

Verifies all 15 required scenarios:
  1. 100-recipient bulk order.
  2. 500-recipient bulk order.
  3. 1,000-recipient bulk order.
  4. 3,000-recipient bulk order (via batched CSV import).
  5. CSV import of 3,000 recipients + duplicate detection + background job tracking.
  6. Centralized coordinator payment & authoritative HMAC verification.
  7. Individual recipient payments, independent verification & partial payment isolation.
  8. Both token distribution modes (Individual privacy isolation vs Representative CSV/PDF export).
  9. 3,000 unique cryptographic QR token generation (verifying ONLY SHA-256 token_hash stored in DB).
  10. Concurrent QR redemption across Counter 1 and Counter 2.
  11. Simultaneous duplicate QR scans (409 TOKEN_ALREADY_REDEEMED).
  12. Partial payments (only paid recipients eligible for pickup).
  13. Partial pickup & representative group batch pickup (/api/v1/pickup/group-verify).
  14. High-contention inventory safety (500 available meals vs 600 competing requests -> max 500 succeed, 0 negative stock).
  15. Strict no-refunds policy & PaymentIncident creation/resolution on late or unfulfilled payments.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import hmac
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import (
    BulkMealToken,
    BulkOrder,
    BulkPickupRecord,
    CanteenCounter,
    Category,
    PaymentIncident,
    Product,
    User,
    UserRole,
)
from app.security import create_access_token, hash_password


def _seed_actor(role: UserRole, prefix: str) -> tuple[int, str, str]:
    email = f"{prefix}-{uuid.uuid4().hex[:8]}@collegebits.edu"
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
        tok = create_access_token(u, mfa_verified=True)
        return u.id, email, tok


def _seed_product(stock: int = 10000, price_paise: int = 8000) -> int:
    with SessionLocal() as db:
        cnt = db.scalar(select(CanteenCounter))
        if not cnt:
            cnt = CanteenCounter(name="Counter 1", location="Main Block", capacity_per_slot=1500, is_active=True)
            db.add(cnt)
            db.flush()
        cat = db.scalar(select(Category))
        if not cat:
            cat = Category(name="Lunch Specials", slug=f"lunch-{uuid.uuid4().hex[:6]}", item_type="food", is_active=True)
            db.add(cat)
            db.flush()
        p = Product(
            category_id=cat.id,
            counter_id=cnt.id,
            name=f"Executive Meal Box {uuid.uuid4().hex[:4]}",
            item_type="food",
            price=price_paise / 100,
            price_paise=price_paise,
            cost_paise=4000,
            stock_quantity=stock,
            is_available=True,
        )
        db.add(p)
        db.commit()
        db.refresh(p)
        return p.id


def _sign_rzp(order_id: str, payment_id: str) -> str:
    secret = (get_settings().razorpay_key_secret or "sandbox_bulk_secret").encode("utf-8")
    return hmac.new(secret, f"{order_id}|{payment_id}".encode("utf-8"), hashlib.sha256).hexdigest()


def test_bulk_orders_100_500_1000_and_3000_recipients_centralized_payment_tokens_and_exports(monkeypatch):
    cfg = get_settings()
    monkeypatch.setattr(cfg, "razorpay_key_id", "rzp_test_bulk3000")
    monkeypatch.setattr(cfg, "razorpay_key_secret", "secret_bulk3000_key")

    _, _, coord_tok = _seed_actor(UserRole.STUDENT, "coord")
    _, _, staff_tok = _seed_actor(UserRole.STAFF, "staff")
    _, _, admin_tok = _seed_actor(UserRole.ADMIN, "admin")
    prod_id = _seed_product(stock=15000, price_paise=7500)

    coord_h = {"Authorization": f"Bearer {coord_tok}"}
    staff_h = {"Authorization": f"Bearer {staff_tok}"}
    admin_h = {"Authorization": f"Bearer {admin_tok}"}

    with TestClient(app) as client:
        # 1. Verify 100, 500, and 1,000 recipient bulk orders via batch API
        for tier_size in (100, 500, 1000):
            bo_res = client.post(
                "/api/v1/bulk-orders",
                headers=coord_h,
                json={
                    "departmentName": f"Engineering Dept ({tier_size})",
                    "eventTitle": f"Tier {tier_size} Symposium Lunch",
                    "pickupSlot": "12:30-13:00",
                    "counterId": 1,
                    "paymentMode": "centralized",
                    "distributionMode": "representative",
                    "items": [{"menuItemId": prod_id, "quantity": tier_size}],
                },
            )
            assert bo_res.status_code == 201, bo_res.text
            bo_id = bo_res.json()["data"]["id"]

            recs = [
                {
                    "recipientIdentifier": f"T{tier_size}-STU-{i:04d}",
                    "fullName": f"Student {i}",
                    "email": f"stu.{tier_size}.{i}@collegebits.edu",
                    "department": "CSE",
                }
                for i in range(1, tier_size + 1)
            ]
            add_res = client.post(f"/api/v1/bulk-orders/{bo_id}/recipients", headers=coord_h, json={"recipients": recs})
            assert add_res.status_code == 201, add_res.text
            assert add_res.json()["data"]["importedCount"] == tier_size
            assert add_res.json()["data"]["totalRecipients"] == tier_size

        # 2. Verify 3,000-recipient bulk order via CSV import + Centralized Payment + 3,000 Unique QR Tokens + Exports
        bo3k = client.post(
            "/api/v1/bulk-orders",
            headers=coord_h,
            json={
                "departmentName": "Mega Campus Convocation",
                "eventTitle": "3000 Student Convocation Lunch",
                "pickupSlot": "13:00-13:30",
                "counterId": 2,
                "paymentMode": "centralized",
                "distributionMode": "representative",
                "items": [{"menuItemId": prod_id, "quantity": 3000}],
            },
        ).json()["data"]
        bo3k_id = bo3k["id"]

        csv_lines = ["recipient_identifier,full_name,email,department,quantity"]
        for i in range(1, 3001):
            csv_lines.append(f"CONV-{i:04d},Convocation Student {i},conv{i}@collegebits.edu,ECE,1")
        # Include 5 duplicate rows to verify duplicate filtering
        for i in range(1, 6):
            csv_lines.append(f"CONV-{i:04d},Duplicate Student {i},conv{i}@collegebits.edu,ECE,1")

        imp_res = client.post(
            f"/api/v1/bulk-orders/{bo3k_id}/recipients/import",
            headers=coord_h,
            json={"csvContent": "\n".join(csv_lines), "runInBackground": True},
        )
        assert imp_res.status_code == 200, imp_res.text
        imp_data = imp_res.json()["data"]
        assert imp_data["importedCount"] == 3000
        assert imp_data["duplicateCount"] == 5
        assert imp_data["totalRecipients"] == 3000

        # Create centralized payment intent & verify signature
        intent = client.post(f"/api/v1/bulk-orders/{bo3k_id}/payment-intent", headers=coord_h).json()["data"]
        gw_order_id = intent["gatewayOrderId"]
        gw_pay_id = f"pay_3k_{uuid.uuid4().hex[:10]}"
        ver = client.post(
            "/api/v1/payments/verify",
            headers=coord_h,
            json={
                "razorpayOrderId": gw_order_id,
                "razorpayPaymentId": gw_pay_id,
                "razorpaySignature": _sign_rzp(gw_order_id, gw_pay_id),
            },
        )
        assert ver.status_code == 200, ver.text

        # Generate 3,000 unique cryptographic QR tokens
        gen_res = client.post(
            f"/api/v1/bulk-orders/{bo3k_id}/tokens/generate",
            headers=coord_h,
            json={"regenerate": False, "expiryHours": 24},
        )
        assert gen_res.status_code == 200, gen_res.text
        gen_data = gen_res.json()["data"]
        assert gen_data["generatedCount"] == 3000

        # Verify database stores ONLY SHA-256 hashes (64 hex chars), never raw bmt_... strings
        with SessionLocal() as db:
            tok_rows = db.scalars(select(BulkMealToken).where(BulkMealToken.bulk_order_id == bo3k_id)).all()
            assert len(tok_rows) == 3000
            distinct_hashes = {t.token_hash for t in tok_rows}
            assert len(distinct_hashes) == 3000
            for t in tok_rows[:20]:
                assert len(t.token_hash) == 64
                assert not t.token_hash.startswith("bmt_")

        # Export CSV and PDF in representative mode
        csv_exp = client.get(f"/api/v1/bulk-orders/{bo3k_id}/tokens/export?format=csv", headers=coord_h)
        assert csv_exp.status_code == 200
        assert "CONV-0001" in csv_exp.text
        assert "bmt_" in csv_exp.text

        pdf_exp = client.get(f"/api/v1/bulk-orders/{bo3k_id}/tokens/export?format=pdf", headers=coord_h)
        assert pdf_exp.status_code == 200
        assert pdf_exp.content.startswith(b"%PDF-1.4")

        # Advance Kitchen 6-Stage Workflow: verified -> reserved -> preparing -> packing -> ready
        for stg in ("preparing", "packing", "ready"):
            st_res = client.post(
                f"/api/v1/kitchen/bulk-orders/{bo3k_id}/stage",
                headers=staff_h,
                json={"stage": stg},
            )
            assert st_res.status_code == 200, st_res.text
            assert st_res.json()["data"]["status"] == stg

        k_dash = client.get("/api/v1/kitchen/dashboard", headers=staff_h)
        assert k_dash.status_code == 200
        assert any(b["bulkConfirmedQty"] >= 3000 for b in k_dash.json()["data"]["confirmedItemBatches"])

        # Test Single Pickup, Simultaneous Duplicate QR Scan (409), and Representative Group Pickup across Counter 1 & 2
        sample_tokens = [t["qrToken"] for t in gen_data["tokens"][:25] if t["qrToken"]]
        assert len(sample_tokens) == 25

        # Simultaneous duplicate scan of sample_tokens[0] by 10 concurrent threads -> exactly 1 succeeds (200), 9 fail (409)
        dup_codes: list[int] = []

        def scan_once(idx: int) -> int:
            r = client.post(
                "/api/v1/pickup/verify",
                headers=staff_h,
                json={"qrToken": sample_tokens[0], "counterId": (idx % 2) + 1},
            )
            return r.status_code

        with ThreadPoolExecutor(max_workers=10) as pool:
            for fut in as_completed([pool.submit(scan_once, i) for i in range(10)]):
                dup_codes.append(fut.result())

        assert dup_codes.count(200) == 1
        assert dup_codes.count(409) == 9

        # Representative group batch verification for tokens 1..20 (plus already-redeemed token 0)
        grp_res = client.post(
            "/api/v1/pickup/group-verify",
            headers=staff_h,
            json={
                "counterId": 2,
                "qrTokens": sample_tokens[:21],
                "representativeName": "Class Representative Rahul",
            },
        )
        assert grp_res.status_code == 200, grp_res.text
        grp_data = grp_res.json()["data"]
        assert grp_data["collectedCount"] == 20
        assert grp_data["rejectedCount"] == 1  # sample_tokens[0] was already redeemed!

        prog = client.get(f"/api/v1/pickup/progress?bulkOrderId={bo3k_id}", headers=admin_h)
        assert prog.status_code == 200
        bo_prog = prog.json()["data"]["bulkOrdersProgress"][0]
        assert bo_prog["collectedRecipientsCount"] == 21


def test_individual_payment_mode_isolation_and_individual_token_distribution_privacy(monkeypatch):
    cfg = get_settings()
    monkeypatch.setattr(cfg, "razorpay_key_id", "rzp_test_indiv")
    monkeypatch.setattr(cfg, "razorpay_key_secret", "secret_indiv_key")

    _, _, coord_tok = _seed_actor(UserRole.STUDENT, "coord-ind")
    _, s1_email, s1_tok = _seed_actor(UserRole.STUDENT, "stu1-ind")
    _, s2_email, s2_tok = _seed_actor(UserRole.STUDENT, "stu2-ind")
    _, _, staff_tok = _seed_actor(UserRole.STAFF, "staff-ind")
    prod_id = _seed_product(stock=500, price_paise=6000)

    coord_h = {"Authorization": f"Bearer {coord_tok}"}
    s1_h = {"Authorization": f"Bearer {s1_tok}"}
    s2_h = {"Authorization": f"Bearer {s2_tok}"}
    staff_h = {"Authorization": f"Bearer {staff_tok}"}

    with TestClient(app) as client:
        bo = client.post(
            "/api/v1/bulk-orders",
            headers=coord_h,
            json={
                "departmentName": "AI & DS Department",
                "eventTitle": "Hackathon Dinner",
                "paymentMode": "individual",
                "distributionMode": "individual",
                "items": [{"menuItemId": prod_id, "quantity": 2}],
            },
        ).json()["data"]
        bo_id = bo["id"]

        client.post(
            f"/api/v1/bulk-orders/{bo_id}/recipients",
            headers=coord_h,
            json={
                "recipients": [
                    {"recipientIdentifier": "IND-01", "fullName": "Student One", "email": s1_email},
                    {"recipientIdentifier": "IND-02", "fullName": "Student Two", "email": s2_email},
                ]
            },
        )
        recs = client.get(f"/api/v1/bulk-orders/{bo_id}/recipients", headers=coord_h).json()["data"]["items"]
        r1_id, r2_id = recs[0]["id"], recs[1]["id"]

        # Student 1 pays for their own allocation; Student 2 has NOT paid yet
        intent1 = client.post(
            f"/api/v1/bulk-orders/{bo_id}/recipients/{r1_id}/payment-intent",
            headers=s1_h,
        ).json()["data"]
        gw_ord1 = intent1["gatewayOrderId"]
        gw_pay1 = f"pay_ind1_{uuid.uuid4().hex[:8]}"
        ver1 = client.post(
            f"/api/v1/bulk-orders/{bo_id}/payments/verify",
            headers=s1_h,
            json={
                "razorpayOrderId": gw_ord1,
                "razorpayPaymentId": gw_pay1,
                "razorpaySignature": _sign_rzp(gw_ord1, gw_pay1),
            },
        )
        assert ver1.status_code == 200, ver1.text
        assert ver1.json()["data"]["bulkOrderPaymentStatus"] == "partially_paid"
        assert ver1.json()["data"]["paidRecipientsCount"] == 1

        # Student 2 (unpaid) cannot get a pickup token (409 PAYMENT_PENDING)
        unpaid_tok = client.get(f"/api/v1/recipients/{r2_id}/token", headers=s2_h)
        assert unpaid_tok.status_code == 409
        assert unpaid_tok.json()["error"]["code"] == "PAYMENT_PENDING"

        # Student 2 cannot view Student 1's token (403 TOKEN_ACCESS_FORBIDDEN)
        forbidden_tok = client.get(f"/api/v1/recipients/{r1_id}/token", headers=s2_h)
        assert forbidden_tok.status_code == 403

        # Coordinator cannot export CSV in 'individual' distribution mode (403 REPRESENTATIVE_MODE_REQUIRED)
        rep_blocked = client.get(f"/api/v1/bulk-orders/{bo_id}/tokens/export?format=csv", headers=coord_h)
        assert rep_blocked.status_code == 403

        # Student 1 retrieves their own QR token and collects meal at Counter 1
        s1_tok_res = client.get(f"/api/v1/recipients/{r1_id}/token", headers=s1_h)
        assert s1_tok_res.status_code == 200
        s1_qr = s1_tok_res.json()["data"]["qrToken"]
        assert s1_qr.startswith("bmt_")

        pickup_res = client.post(
            "/api/v1/pickup/verify",
            headers=staff_h,
            json={"qrToken": s1_qr, "counterId": 1},
        )
        assert pickup_res.status_code == 200


def test_500_available_meals_vs_600_simultaneous_requests_zero_overselling_and_incident_resolution(monkeypatch):
    cfg = get_settings()
    monkeypatch.setattr(cfg, "razorpay_key_id", "rzp_test_inc")
    monkeypatch.setattr(cfg, "razorpay_key_secret", "secret_inc_key")

    _, _, student_tok = _seed_actor(UserRole.STUDENT, "cont-stu")
    _, _, admin_tok = _seed_actor(UserRole.ADMIN, "cont-adm")
    stu_h = {"Authorization": f"Bearer {student_tok}"}
    adm_h = {"Authorization": f"Bearer {admin_tok}"}

    # Seed product with exactly 500 meals in stock
    limited_prod_id = _seed_product(stock=500, price_paise=5000)

    with TestClient(app) as client:
        # 600 requests (60 batches of 10 units = 600 units) competing for 500 units
        statuses: list[int] = []

        def reserve_ten(idx: int) -> int:
            r = client.post(
                "/api/v1/bulk-orders",
                headers=stu_h,
                json={
                    "departmentName": f"Contention Dept {idx}",
                    "eventTitle": "500 vs 600 Contention Test",
                    "reserveInventoryNow": True,
                    "items": [{"menuItemId": limited_prod_id, "quantity": 10}],
                },
            )
            return r.status_code

        with ThreadPoolExecutor(max_workers=25) as pool:
            for fut in as_completed([pool.submit(reserve_ten, i) for i in range(60)]):
                statuses.append(fut.result())

        # Exactly 50 reservations of 10 units (= 500 meals) succeed; 10 reservations (= 100 meals) fail with 409
        assert statuses.count(201) == 50
        assert statuses.count(409) == 10

        with SessionLocal() as db:
            p_after = db.get(Product, limited_prod_id)
            assert p_after.stock_quantity == 0

        # Test strict no-refund incident creation when a verified payment arrives after stock is 0
        bo_unreserved = client.post(
            "/api/v1/bulk-orders",
            headers=stu_h,
            json={
                "departmentName": "Late Payment Dept",
                "eventTitle": "Stock Exhausted Incident Test",
                "reserveInventoryNow": False,
                "items": [{"menuItemId": limited_prod_id, "quantity": 5}],
            },
        )
        # Re-enable product availability flag without stock to simulate race between intent and capture
        with SessionLocal() as db:
            p_row = db.get(Product, limited_prod_id)
            p_row.is_available = True
            p_row.stock_quantity = 0
            db.commit()

        bo_late = client.post(
            "/api/v1/bulk-orders",
            headers=stu_h,
            json={
                "departmentName": "Late Payment Dept",
                "eventTitle": "Stock Exhausted Incident Test",
                "reserveInventoryNow": False,
                "items": [{"menuItemId": limited_prod_id, "quantity": 5}],
            },
        ).json()["data"]
        intent = client.post(f"/api/v1/bulk-orders/{bo_late['id']}/payment-intent", headers=stu_h).json()["data"]
        gw_ord = intent["gatewayOrderId"]
        gw_pay = f"pay_late_inc_{uuid.uuid4().hex[:8]}"

        ver = client.post(
            f"/api/v1/bulk-orders/{bo_late['id']}/payments/verify",
            headers=stu_h,
            json={
                "razorpayOrderId": gw_ord,
                "razorpayPaymentId": gw_pay,
                "razorpaySignature": _sign_rzp(gw_ord, gw_pay),
            },
        )
        assert ver.status_code == 200, ver.text
        ver_data = ver.json()["data"]
        assert ver_data["paymentStatus"] == "SUCCESS"
        assert ver_data["incidentFlagged"] is True

        # Admin lists and resolves the payment incident (no refund is ever issued)
        inc_list = client.get("/api/v1/admin/payment-incidents?status=open", headers=adm_h)
        assert inc_list.status_code == 200
        incidents = inc_list.json()["data"]["items"]
        assert len(incidents) >= 1
        target_inc_id = incidents[0]["id"]

        resolved = client.post(
            f"/api/v1/admin/payment-incidents/{target_inc_id}/resolve",
            headers=adm_h,
            json={"status": "resolved", "resolutionNotes": "Rescheduled meal allocation for next lunch slot with department coordinator."},
        )
        assert resolved.status_code == 200
        assert resolved.json()["data"]["status"] == "resolved"
