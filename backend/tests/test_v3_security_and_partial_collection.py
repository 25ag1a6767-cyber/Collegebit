"""CampusBite AI v3.0 Security, Stored XSS Immunity & Partial Pickup Handover Verification Suite."""
from __future__ import annotations

import json
from datetime import datetime, timezone
import uuid
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
    Product,
    User,
    UserRole,
)
from app.security import create_access_token, hash_password


def test_stored_xss_sanitization_and_injection_immunity():
    """Verify that malicious script tags stored in names or customizations cannot cause injection."""
    client = TestClient(app)
    db = SessionLocal()
    try:
        # Ensure counter
        counter = db.scalar(select(CanteenCounter).where(CanteenCounter.is_active == True))
        if not counter:
            counter = CanteenCounter(name="V3 Main Counter", location="Ground Floor", is_active=True, capacity_per_slot=20)
            db.add(counter)
            db.flush()

        cat = db.scalar(select(Category).where(Category.is_active == True))
        if not cat:
            cat = Category(name="V3 Snacks", is_active=True)
            db.add(cat)
            db.flush()

        p1 = Product(
            category_id=cat.id,
            name=f"Crispy Samosa {uuid.uuid4().hex[:6]}",
            description="Fresh hot potato samosa",
            price=20.0,
            price_paise=2000,
            stock_quantity=100,
            counter_id=counter.id,
            is_available=True,
            is_archived=False,
        )
        staff = User(
            full_name="V3 Sec Staff",
            email=f"v3-staff-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("staffSecret123"),
            role=UserRole.STAFF,
            is_active=True,
            staff_permissions_json=json.dumps({"canVerifyPickup": True, "canManageOrders": True}),
        )
        db.add_all([p1, staff])
        db.commit()

        xss_payload = "<script>alert('pwned')</script><img src=x onerror=alert(1)>"
        stu_email = f"alice-xss-{uuid.uuid4().hex[:8]}@example.com"
        
        # Register student with XSS payload in name
        reg_resp = client.post(
            "/api/v1/auth/register",
            json={
                "fullName": f"Alice {xss_payload}",
                "email": stu_email,
                "password": "Password123!",
                "phone": "9876543210",
            },
        )
        assert reg_resp.status_code == 201
        user_data = reg_resp.json()["data"]["user"]
        assert user_data["email"] == stu_email

        # Login as student
        login_resp = client.post(
            "/api/v1/auth/login",
            json={"identifier": stu_email, "password": "Password123!"},
        )
        assert login_resp.status_code == 200
        token = login_resp.json()["data"]["accessToken"]

        # Fetch active slot for counter
        slot_resp = client.get(f"/api/v1/pickup-slots?counterId={counter.id}")
        assert slot_resp.status_code == 200
        valid_slot = slot_resp.json()["data"]["slots"][0]["start"]

        # Place order directly via POST /api/v1/orders with valid pickup slot
        order_resp = client.post(
            "/api/v1/orders",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "counterId": counter.id,
                "pickupSlot": valid_slot,
                "items": [
                    {
                        "menuItemId": p1.id,
                        "quantity": 1,
                        "customizations": {"note": xss_payload, "spice": "extra"},
                    }
                ],
            },
        )
        assert order_resp.status_code == 201
        order_id = order_resp.json()["data"]["order"]["id"]

        # Mark paid and ready
        order = db.get(Order, order_id)
        order.payment_status = PaymentStatus.PAID
        order.status = OrderStatus.READY
        order.ready_at = datetime.now(timezone.utc)
        db.commit()

        # Staff inspects order
        staff_token = create_access_token(staff)
        inspect_resp = client.post(
            "/api/v1/pickups/inspect",
            headers={"Authorization": f"Bearer {staff_token}"},
            json={"orderId": order_id},
        )
        assert inspect_resp.status_code == 200
        data = inspect_resp.json()["data"]
        # Check that student name and customizations are present safely as raw text
        assert f"Alice {xss_payload}" in data["customer"]["fullName"]
        assert data["items"][0]["customizations"]["note"] == xss_payload
    finally:
        db.close()


def test_partial_collection_and_double_collection_prevention():
    """Test item-level partial collection workflow and prevention of double-collection."""
    client = TestClient(app)
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        counter = db.scalar(select(CanteenCounter).where(CanteenCounter.is_active == True))
        if not counter:
            counter = CanteenCounter(name="V3 Main Counter", location="Ground Floor", is_active=True, capacity_per_slot=20)
            db.add(counter)
            db.flush()

        cat = db.scalar(select(Category).where(Category.is_active == True))
        if not cat:
            cat = Category(name="V3 Snacks", is_active=True)
            db.add(cat)
            db.flush()

        p1 = Product(
            category_id=cat.id,
            name=f"V3 Samosa {uuid.uuid4().hex[:6]}",
            price=20.0,
            price_paise=2000,
            stock_quantity=100,
            counter_id=counter.id,
            is_available=True,
            is_archived=False,
        )
        p2 = Product(
            category_id=cat.id,
            name=f"V3 Chai {uuid.uuid4().hex[:6]}",
            price=15.0,
            price_paise=1500,
            stock_quantity=100,
            counter_id=counter.id,
            is_available=True,
            is_archived=False,
        )
        staff = User(
            full_name="V3 Handover Staff",
            email=f"v3-handover-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("staffSecret123"),
            role=UserRole.STAFF,
            is_active=True,
            staff_permissions_json=json.dumps({"canVerifyPickup": True, "canManageOrders": True}),
        )
        student = User(
            full_name="Bob Student",
            email=f"bob-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("Pass123!"),
            role=UserRole.STUDENT,
            is_active=True,
        )
        db.add_all([p1, p2, staff, student])
        db.flush()

        # Create order with 2 items
        order = Order(
            user_id=student.id,
            counter_id=counter.id,
            status=OrderStatus.READY,
            payment_status=PaymentStatus.PAID,
            token_date=now.date().isoformat(),
            token_number=901,
            subtotal=35.0,
            subtotal_paise=3500,
            final_total=35.0,
            total_paise=3500,
            created_at=now,
            ready_at=now,
        )
        db.add(order)
        db.flush()

        item1 = OrderItem(
            order_id=order.id,
            product_id=p1.id,
            product_name=p1.name,
            quantity=2,
            unit_price=20.0,
            unit_price_paise=2000,
            line_total=40.0,
            line_total_paise=4000,
            is_collected=False,
        )
        item2 = OrderItem(
            order_id=order.id,
            product_id=p2.id,
            product_name=p2.name,
            quantity=1,
            unit_price=15.0,
            unit_price_paise=1500,
            line_total=15.0,
            line_total_paise=1500,
            is_collected=False,
        )
        db.add_all([item1, item2])
        db.commit()

        staff_token = create_access_token(staff)

        # Step 1: Staff inspects the order
        inspect1 = client.post(
            "/api/v1/pickups/inspect",
            headers={"Authorization": f"Bearer {staff_token}"},
            json={"orderId": order.id},
        )
        assert inspect1.status_code == 200
        res1 = inspect1.json()["data"]
        assert res1["eligibleForCollection"] is True
        assert len(res1["items"]) == 2
        assert res1["items"][0]["isCollected"] is False
        assert res1["items"][1]["isCollected"] is False

        # Step 2: Staff hands over item 1 only (Partial collection)
        collect_part = client.post(
            "/api/v1/pickups/verify",
            headers={"Authorization": f"Bearer {staff_token}"},
            json={"orderId": order.id, "itemIds": [item1.id]},
        )
        assert collect_part.status_code == 200
        part_order = collect_part.json()["data"]["order"]
        assert part_order["rawStatus"] == "partially_collected"
        assert part_order["status"] == "PARTIALLY_COLLECTED"

        # Verify in DB
        db.refresh(order)
        db.refresh(item1)
        db.refresh(item2)
        assert order.status == OrderStatus.PARTIALLY_COLLECTED
        assert item1.is_collected is True
        assert item2.is_collected is False

        # Step 3: Staff re-inspects order -> Still eligible for remaining item
        inspect2 = client.post(
            "/api/v1/pickups/inspect",
            headers={"Authorization": f"Bearer {staff_token}"},
            json={"orderId": order.id},
        )
        assert inspect2.status_code == 200
        res2 = inspect2.json()["data"]
        assert res2["eligibleForCollection"] is True
        assert res2["collection"]["status"] == "partially_collected"
        assert res2["items"][0]["isCollected"] is True
        assert res2["items"][1]["isCollected"] is False

        # Step 4: Staff hands over item 2 (Completing the collection)
        collect_full = client.post(
            "/api/v1/pickups/verify",
            headers={"Authorization": f"Bearer {staff_token}"},
            json={"orderId": order.id, "itemIds": [item2.id]},
        )
        assert collect_full.status_code == 200
        full_order = collect_full.json()["data"]["order"]
        assert full_order["rawStatus"] == "collected"
        assert full_order["status"] == "COLLECTED"

        db.refresh(order)
        db.refresh(item2)
        assert order.status == OrderStatus.COLLECTED
        assert item2.is_collected is True
        assert order.collected_at is not None

        # Step 5: Attempting to collect again returns 409 ORDER_ALREADY_COLLECTED
        double_collect = client.post(
            "/api/v1/pickups/verify",
            headers={"Authorization": f"Bearer {staff_token}"},
            json={"orderId": order.id},
        )
        assert double_collect.status_code == 409
        assert double_collect.json()["error"]["code"] == "ORDER_ALREADY_COLLECTED"
    finally:
        db.close()
