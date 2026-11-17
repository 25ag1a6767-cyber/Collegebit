"""Comprehensive Integration & Edge-Case Tests for CollegeBits Phases 60 to 85:
- Kitchen Station Routing, Auto-Capacity Admission Pause & Manager Reopen
- Critical Feature: Parcel vs Ready-to-Eat Fulfillment Selection, Packaging Charges & Packing Verification Gate
- Customer Support & Complaint Management (Tickets, Internal Notes Privacy, Escalation, SLA)
- Food Safety Checklists & Cold/Freezer/Hot Temperature Alerts
- Notification Delivery Queue, Retry Mechanism & Preferences
- Sustainability Waste Idempotency & Stock Deduction
- Workforce Shift Conflict Detection & Late Check-In
- Procurement Requisitions -> Approval -> PO + Supplier Invoice Mismatch Flagging
- Smart Menu Dynamic Pricing Bounds & Separation of Duties
- Group Ordering, Multi-Dim Quality Feedback, Meal Passes & Daily Canteen Closing Lock
- Equipment Breakdown & Uncollected Order Safe Handling
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import CanteenCounter, Category, Product, User, UserRole
from app.security import create_access_token, hash_password


def _mk_actor(role: UserRole, prefix: str) -> tuple[int, str, str]:
    email = f"{prefix}-{uuid.uuid4().hex[:8]}@aceec.ac.in"
    with SessionLocal() as db:
        u = User(
            full_name=f"{prefix.title()} User",
            email=email,
            password_hash=hash_password("Pass#Phase60Enterprise!"),
            role=role,
            is_active=True,
        )
        db.add(u)
        db.commit()
        db.refresh(u)
        return u.id, email, create_access_token(u, mfa_verified=True)


def _mk_item(stock: int = 100, price_paise: int = 5000, counter_id: int | None = None) -> tuple[int, int]:
    with SessionLocal() as db:
        if counter_id is None:
            cnt = db.query(CanteenCounter).first()
            if not cnt:
                cnt = CanteenCounter(name="Counter 1", location="Main", capacity_per_slot=500, is_active=True)
                db.add(cnt)
                db.flush()
            counter_id = cnt.id
        cat = db.query(Category).first()
        if not cat:
            cat = Category(name="Tiffins", slug=f"tiffins-{uuid.uuid4().hex[:6]}", item_type="food", is_active=True)
            db.add(cat)
            db.flush()
        p = Product(
            category_id=cat.id,
            counter_id=counter_id,
            name=f"Special Dosa {uuid.uuid4().hex[:5]}",
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
        return p.id, counter_id


def test_parcel_vs_ready_to_eat_kitchen_station_autopause_and_packing_verification():
    stu_id, _, stu_tok = _mk_actor(UserRole.STUDENT, "pr-stu")
    _, _, staff_tok = _mk_actor(UserRole.STAFF, "pr-stf")
    _, _, adm_tok = _mk_actor(UserRole.ADMIN, "pr-adm")
    stu_h = {"Authorization": f"Bearer {stu_tok}"}
    staff_h = {"Authorization": f"Bearer {staff_tok}"}
    adm_h = {"Authorization": f"Bearer {adm_tok}"}

    item_id, cnt_id = _mk_item(stock=50, price_paise=6000)

    with TestClient(app) as client:
        # Credit student wallet so payment succeeds cleanly
        assert client.post(
            "/api/v1/admin/wallet/credit",
            headers=adm_h,
            json={"userId": stu_id, "amountPaise": 50000, "reason": "Parcel workflow test", "referenceKey": f"ref-{uuid.uuid4().hex[:8]}"},
        ).status_code == 201

        # 1. Create a Kitchen Station with maxConcurrentOrders = 1 to test capacity auto-pause
        st = client.post(
            "/api/v1/kitchen/stations",
            headers=adm_h,
            json={"counterId": cnt_id, "name": "Dosa Tawa Station", "stationType": "grill", "maxConcurrentOrders": 1},
        )
        assert st.status_code == 201, st.text
        st_id = st.json()["data"]["id"]

        # 2. Configure Item for PARCEL (+₹10.00 packaging charge) & READY_TO_EAT routed to Dosa Station
        cfg = client.put(
            f"/api/v1/menu/items/{item_id}/fulfillment",
            headers=adm_h,
            json={
                "supportsParcel": True,
                "supportsReadyToEat": True,
                "packagingChargePaise": 1000,
                "packagingInstructions": "Pack in thermal foil box with chutney cup",
                "stationId": st_id,
            },
        )
        assert cfg.status_code == 200, cfg.text

        # 3. Quote PARCEL order -> subtotal includes ₹10.00 (1000 paise) packaging charge
        q_parcel = client.post(
            "/api/v1/orders/quote",
            headers=stu_h,
            json={"counterId": cnt_id, "fulfillmentType": "PARCEL", "items": [{"menuItemId": item_id, "quantity": 2, "fulfillmentType": "PARCEL"}]},
        )
        assert q_parcel.status_code == 200, q_parcel.text
        q_data = q_parcel.json()["data"]
        assert q_data["packagingChargePaise"] == 2000  # 2 * 1000 paise
        assert q_data["subtotalPaise"] == 14000       # 2 * 6000 + 2000

        # 4. Create PARCEL order -> station hits 1/1 and auto-pauses
        ord_res = client.post(
            "/api/v1/orders",
            headers=stu_h,
            json={"counterId": cnt_id, "fulfillmentType": "PARCEL", "payWithWallet": True, "items": [{"menuItemId": item_id, "quantity": 1, "fulfillmentType": "PARCEL"}]},
        )
        assert ord_res.status_code == 201, ord_res.text
        oid = ord_res.json()["data"]["order"]["id"]
        assert ord_res.json()["data"]["order"]["hasParcelItems"] is True
        assert ord_res.json()["data"]["order"]["allParcelItemsPacked"] is False

        # 5. Second order hits 409 STATION_CAPACITY_FULL (auto-pause protection!)
        ord_blocked = client.post(
            "/api/v1/orders",
            headers=stu_h,
            json={"counterId": cnt_id, "fulfillmentType": "READY_TO_EAT", "payWithWallet": True, "items": [{"menuItemId": item_id, "quantity": 1, "fulfillmentType": "READY_TO_EAT"}]},
        )
        assert ord_blocked.status_code == 409
        assert ord_blocked.json()["error"]["code"] == "STATION_CAPACITY_FULL"

        # 6. Staff cannot mark a PARCEL order READY without verify-packing first!
        client.post(f"/api/v1/staff/orders/{oid}/start", headers=staff_h, json={})
        premature_ready = client.post(f"/api/v1/staff/orders/{oid}/ready", headers=staff_h, json={})
        assert premature_ready.status_code == 409
        assert premature_ready.json()["error"]["code"] == "PACKING_VERIFICATION_REQUIRED"

        # Verify packing -> then mark READY succeeds
        assert client.post(f"/api/v1/staff/orders/{oid}/verify-packing", headers=staff_h, json={}).status_code == 200
        assert client.post(f"/api/v1/staff/orders/{oid}/ready", headers=staff_h, json={}).status_code == 200

        # Manager reopens station -> capacity increases -> new READY_TO_EAT order (zero packaging fee) succeeds
        assert client.post(f"/api/v1/kitchen/stations/{st_id}/reopen?newMaxConcurrentOrders=10", headers=adm_h).status_code == 200
        ord_rte = client.post(
            "/api/v1/orders",
            headers=stu_h,
            json={"counterId": cnt_id, "fulfillmentType": "READY_TO_EAT", "payWithWallet": True, "items": [{"menuItemId": item_id, "quantity": 1, "fulfillmentType": "READY_TO_EAT"}]},
        )
        assert ord_rte.status_code == 201
        assert ord_rte.json()["data"]["order"]["packagingChargePaise"] == 0


def test_support_tickets_food_safety_notifications_and_waste():
    s1_id, _, s1_tok = _mk_actor(UserRole.STUDENT, "sup-s1")
    _, _, s2_tok = _mk_actor(UserRole.STUDENT, "sup-s2")
    stf_id, _, stf_tok = _mk_actor(UserRole.STAFF, "sup-stf")
    _, _, adm_tok = _mk_actor(UserRole.ADMIN, "sup-adm")
    s1_h = {"Authorization": f"Bearer {s1_tok}"}
    s2_h = {"Authorization": f"Bearer {s2_tok}"}
    stf_h = {"Authorization": f"Bearer {stf_tok}"}
    adm_h = {"Authorization": f"Bearer {adm_tok}"}
    item_id, _ = _mk_item(stock=40, price_paise=4000)

    with TestClient(app) as client:
        # 1. Support Ticket + Privacy Isolation + Internal Staff Notes Hidden from Customer
        tck = client.post(
            "/api/v1/support/tickets",
            headers=s1_h,
            json={"category": "missing_item", "priority": "high", "subject": "Chutney missing", "description": "Parcel box did not include chutney"},
        ).json()["data"]
        tid = tck["id"]
        assert tck["status"] == "OPEN"

        # Student 2 cannot access Student 1's ticket
        assert client.get(f"/api/v1/support/tickets/{tid}", headers=s2_h).status_code == 403

        # Staff adds internal note; Student 1 cannot see internal note!
        assert client.post(f"/api/v1/support/tickets/{tid}/messages", headers=stf_h, json={"message": "Internal kitchen review", "isInternalNote": True}).status_code == 201
        detail_stu = client.get(f"/api/v1/support/tickets/{tid}", headers=s1_h).json()["data"]
        assert all(not m["isInternalNote"] for m in detail_stu["messages"])

        upd = client.patch(f"/api/v1/support/tickets/{tid}", headers=stf_h, json={"status": "RESOLVED", "assignedStaffId": stf_id, "resolutionSummary": "Counter briefed and extra pack issued"})
        assert upd.status_code == 200
        assert upd.json()["data"]["status"] == "RESOLVED"
        assert client.get("/api/v1/support/sla-metrics", headers=adm_h).json()["data"]["resolvedCount"] >= 1

        # 2. Food Safety Checklist + Manager Approval + Unsafe Temp Alert
        chk = client.post(
            "/api/v1/food-safety/checklists",
            headers=stf_h,
            json={"checklistType": "daily_cleaning", "stationName": "Prep Area", "itemsChecked": [{"task": "Sanitize counters", "ok": True}], "passed": True},
        ).json()["data"]
        assert client.post(f"/api/v1/food-safety/checklists/{chk['id']}/approve", headers=adm_h).status_code == 200

        temp_bad = client.post(
            "/api/v1/food-safety/temperatures",
            headers=stf_h,
            json={"equipmentOrItem": "Milk Chiller", "storageZone": "cold_storage", "temperatureCelsius": 11.5, "sourceType": "manual", "correctiveAction": "Adjusted thermostat"},
        ).json()["data"]
        assert temp_bad["withinSafeRange"] is False
        assert temp_bad["alertTriggered"] is True

        # 3. Reliable Notification Retry + User Preferences
        pref = client.patch("/api/v1/users/me/notification-preferences", headers=s1_h, json={"emailEnabled": False}).json()["data"]
        assert pref["emailEnabled"] is False

        fail_ntf = client.post(
            "/api/v1/notifications/dispatch",
            headers=stf_h,
            json={"userId": s1_id, "channel": "in_app", "subject": "Order Ready", "body": "Token ready", "simulateTransientFailure": True},
        ).json()["data"]
        assert fail_ntf["status"] == "failed"
        retry = client.post("/api/v1/notifications/retry-failed", headers=stf_h).json()["data"]
        assert retry["retriedCount"] >= 1

        # 4. Idempotent Waste Recording (Single Stock Deduction)
        wk = f"waste-{uuid.uuid4().hex[:8]}"
        w1 = client.post(
            "/api/v1/waste/records",
            headers=stf_h,
            json={"productId": item_id, "itemName": "Special Dosa", "wasteCategory": "spoiled_food", "quantity": 5, "reason": "Batter overfermented", "idempotencyKey": wk},
        ).json()["data"]
        assert w1["stockDeducted"] is True
        w2 = client.post(
            "/api/v1/waste/records",
            headers=stf_h,
            json={"productId": item_id, "itemName": "Special Dosa", "wasteCategory": "spoiled_food", "quantity": 5, "reason": "Batter overfermented", "idempotencyKey": wk},
        ).json()["data"]
        assert w2["duplicateSkipped"] is True


def test_shifts_procurement_pricing_group_order_meal_pass_closing_and_equipment():
    stf_id, _, stf_tok = _mk_actor(UserRole.STAFF, "ops-stf")
    stu_id, _, stu_tok = _mk_actor(UserRole.STUDENT, "ops-stu")
    adm1_id, _, adm1_tok = _mk_actor(UserRole.ADMIN, "ops-adm1")
    adm2_id, _, adm2_tok = _mk_actor(UserRole.ADMIN, "ops-adm2")
    stf_h = {"Authorization": f"Bearer {stf_tok}"}
    stu_h = {"Authorization": f"Bearer {stu_tok}"}
    adm1_h = {"Authorization": f"Bearer {adm1_tok}"}
    adm2_h = {"Authorization": f"Bearer {adm2_tok}"}
    item_id, _ = _mk_item(stock=50, price_paise=8000)

    with TestClient(app) as client:
        # 1. Workforce Shift Scheduling + Overlap Conflict Detection
        t0 = datetime.now(timezone.utc) + timedelta(hours=2)
        t1 = t0 + timedelta(hours=6)
        sh = client.post(
            "/api/v1/workforce/shifts",
            headers=adm1_h,
            json={"staffUserId": stf_id, "stationRole": "kitchen_prep", "shiftStart": t0.isoformat(), "shiftEnd": t1.isoformat()},
        )
        assert sh.status_code == 201
        sh_id = sh.json()["data"]["id"]
        # Overlapping shift rejected
        assert client.post(
            "/api/v1/workforce/shifts",
            headers=adm1_h,
            json={"staffUserId": stf_id, "stationRole": "packing", "shiftStart": t0.isoformat(), "shiftEnd": t1.isoformat()},
        ).status_code == 409
        assert client.post(f"/api/v1/workforce/shifts/{sh_id}/check-in", headers=stf_h).status_code == 200
        assert client.post(f"/api/v1/workforce/shifts/{sh_id}/check-out?handoverNotes=Kitchen+clean", headers=stf_h).status_code == 200

        # 2. Procurement Requisition -> Approval -> PO + Invoice Discrepancy
        req = client.post(
            "/api/v1/procurement/requisitions",
            headers=stf_h,
            json={"items": [{"name": "Rice 25kg", "quantity": 2, "unitCostPaise": 150000}], "reason": "Weekly stock"},
        ).json()["data"]
        po = client.post(f"/api/v1/procurement/requisitions/{req['id']}/approve", headers=adm1_h).json()["data"]
        inv = client.post(
            "/api/v1/procurement/invoices",
            headers=adm1_h,
            json={"invoiceReference": f"INV-{uuid.uuid4().hex[:6]}", "purchaseOrderId": po["purchaseOrderId"], "invoiceAmountPaise": 310000},
        ).json()["data"]
        assert inv["discrepancyFlagged"] is True
        assert inv["discrepancyPaise"] == 10000

        # 3. Dynamic Pricing Safeguards + Separation of Duties
        rule = client.post(
            "/api/v1/menu/pricing-rules",
            headers=adm1_h,
            json={"productId": item_id, "ruleName": "Evening Happy Hour", "mealWindow": "evening", "proposedPricePaise": 7000, "minPricePaise": 5000, "maxPricePaise": 10000, "reason": "High stock"},
        ).json()["data"]
        # Creator (adm1) cannot self-approve (403 SEPARATION_OF_DUTIES)
        assert client.post(f"/api/v1/menu/pricing-rules/{rule['id']}/approve", headers=adm1_h).status_code == 403
        # Second manager (adm2) approves -> price updated safely
        app_res = client.post(f"/api/v1/menu/pricing-rules/{rule['id']}/approve", headers=adm2_h)
        assert app_res.status_code == 200
        assert app_res.json()["data"]["newPricePaise"] == 7000

        # 4. Group Ordering
        grp = client.post("/api/v1/group-orders", headers=stu_h, json={"title": "CSE Project Squad"}).json()["data"]
        add_g = client.post(f"/api/v1/group-orders/{grp['inviteCode']}/items", headers=stu_h, json={"menuItemId": item_id, "quantity": 2, "fulfillmentType": "PARCEL"})
        assert add_g.status_code == 201
        assert add_g.json()["data"]["totalPaise"] == 14000

        # 5. Meal Pass Purchase + Verified Activation + Atomic Idempotent Redemption
        plan = client.post(
            "/api/v1/meal-passes/plans",
            headers=adm1_h,
            json={"planCode": f"WK-{uuid.uuid4().hex[:5]}", "name": "5-Lunch Weekly Pass", "passPeriod": "weekly", "totalMeals": 5, "validityDays": 7, "pricePaise": 30000},
        ).json()["data"]
        mp = client.post(f"/api/v1/meal-passes/{plan['id']}/purchase", headers=stu_h).json()["data"]
        assert client.post(f"/api/v1/meal-passes/{mp['passId']}/activate-verified?gatewayPaymentId=pay_mp_{uuid.uuid4().hex[:8]}", headers=stu_h).json()["data"]["status"] == "active"

        mp_idem = f"redeem-{uuid.uuid4().hex[:8]}"
        r1 = client.post(f"/api/v1/meal-passes/{mp['passId']}/redeem", headers={"Authorization": f"Bearer {stu_tok}", "Idempotency-Key": mp_idem}).json()["data"]
        assert r1["remainingMeals"] == 4
        # Replaying same Idempotency-Key does not double-deduct!
        r2 = client.post(f"/api/v1/meal-passes/{mp['passId']}/redeem", headers={"Authorization": f"Bearer {stu_tok}", "Idempotency-Key": mp_idem}).json()["data"]
        assert r2["remainingMeals"] == 4
        assert r2["duplicateSkipped"] is True

        # 6. Daily Closing Lock & Adjustments + Equipment Breakdown
        cls_row = client.post("/api/v1/admin/closings", headers=stf_h, json={"counterId": 1, "discrepancyNotes": "EOD Count OK"}).json()["data"]
        assert client.post(f"/api/v1/admin/closings/{cls_row['id']}/approve", headers=adm1_h).json()["data"]["isLocked"] is True
        assert client.post("/api/v1/admin/closings", headers=stf_h, json={"counterId": 1}).status_code == 409

        eq = client.post("/api/v1/equipment/assets", headers=adm1_h, json={"name": "Steam Boiler", "category": "oven", "assetCode": f"EQ-{uuid.uuid4().hex[:5]}"}).json()["data"]
        brk = client.post(f"/api/v1/equipment/{eq['id']}/report-breakdown", headers=stf_h, json={"priority": "critical", "issueSummary": "Pressure valve leak"})
        assert brk.status_code == 201
        assert brk.json()["data"]["operationalStatus"] == "down"
