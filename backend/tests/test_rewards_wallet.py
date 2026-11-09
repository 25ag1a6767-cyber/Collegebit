from __future__ import annotations

from datetime import datetime, timezone
import json
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    CanteenCounter,
    Category,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    Product,
    RewardTransaction,
    User,
    UserRole,
    WalletTransaction,
)
from app.security import create_access_token, hash_password


def _account(role: UserRole) -> tuple[int, str]:
    suffix = uuid.uuid4().hex[:12]
    with SessionLocal() as db:
        user = User(
            full_name=f"Rewards {role.value}",
            email=f"rewards-{role.value}-{suffix}@example.edu",
            password_hash=hash_password("Rewards-Test-Password-123!"),
            role=role,
            is_active=True,
            email_verified_at=datetime.now(timezone.utc),
        )
        db.add(user)
        db.commit()
        return user.id, create_access_token(user)


def _product() -> int:
    suffix = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        counter = CanteenCounter(
            name=f"Rewards Counter {suffix}",
            location="Test",
            is_active=True,
            capacity_per_slot=20,
            opening_hours="00:00-23:59",
        )
        category = Category(name=f"Rewards Category {suffix}", slug=f"rewards-{suffix}", item_type="food", is_active=True)
        db.add_all([counter, category])
        db.flush()
        product = Product(
            category_id=category.id,
            name=f"Rewards Meal {suffix}",
            item_type="food",
            price=20,
            price_paise=2000,
            stock_quantity=20,
            low_stock_threshold=0,
            is_available=True,
            is_archived=False,
        )
        db.add(product)
        db.commit()
        return product.id


def test_prepaid_credit_wallet_order_and_internal_refund():
    student_id, student_token = _account(UserRole.STUDENT)
    _, admin_token = _account(UserRole.ADMIN)
    product_id = _product()
    client = TestClient(app)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    student_headers = {"Authorization": f"Bearer {student_token}"}

    credit = client.post(
        "/api/v1/admin/wallet/credit",
        headers=admin_headers,
        json={
            "userId": student_id,
            "amountPaise": 5000,
            "reason": "Verified prepaid meal plan receipt",
            "referenceKey": f"receipt-{uuid.uuid4().hex[:12]}",
        },
    )
    assert credit.status_code == 201, credit.text
    assert credit.json()["data"]["balancePaise"] == 5000

    order_response = client.post(
        "/api/v1/orders",
        headers=student_headers,
        json={
            "items": [{"menuItemId": product_id, "quantity": 1}],
            "payWithWallet": True,
        },
    )
    assert order_response.status_code == 201, order_response.text
    order = order_response.json()["data"]["order"]
    assert order["paymentStatus"] == "SUCCESS"
    assert order["totalPaise"] == 2000

    wallet = client.get("/api/v1/wallet/me", headers=student_headers)
    assert wallet.status_code == 200
    assert wallet.json()["data"]["balancePaise"] == 3000

    cancelled = client.post(
        f"/api/v1/orders/{order['id']}/cancel",
        headers=student_headers,
        json={"reason": "Test cancellation"},
    )
    assert cancelled.status_code == 200, cancelled.text
    wallet_after_refund = client.get("/api/v1/wallet/me", headers=student_headers)
    assert wallet_after_refund.json()["data"]["balancePaise"] == 5000

    with SessionLocal() as db:
        saved_order = db.get(Order, order["id"])
        assert saved_order is not None
        assert saved_order.payment_status == PaymentStatus.REFUNDED
        assert db.scalar(select(Payment).where(Payment.order_id == saved_order.id)).provider == "campusbite_wallet"
        assert db.scalar(select(WalletTransaction).where(WalletTransaction.order_id == saved_order.id, WalletTransaction.transaction_type == "refund")) is not None


def test_reward_points_are_redeemed_server_side_and_restored_on_cancel():
    student_id, token = _account(UserRole.STUDENT)
    product_id = _product()
    with SessionLocal() as db:
        db.add(RewardTransaction(
            user_id=student_id,
            points_delta=100,
            reason="Test reward grant",
            reference_key=f"test-grant:{uuid.uuid4().hex[:12]}",
        ))
        db.commit()

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    quote = client.post(
        "/api/v1/orders/quote",
        headers=headers,
        json={
            "items": [{"menuItemId": product_id, "quantity": 1}],
            "loyaltyPointsRedeemed": 50,
        },
    )
    assert quote.status_code == 200, quote.text
    quote_data = quote.json()["data"]
    assert quote_data["loyaltyDiscountPaise"] == 500
    assert quote_data["totalPaise"] == 1500

    created = client.post(
        "/api/v1/orders",
        headers=headers,
        json={
            "items": [{"menuItemId": product_id, "quantity": 1}],
            "loyaltyPointsRedeemed": 50,
        },
    )
    assert created.status_code == 201, created.text
    order = created.json()["data"]["order"]
    assert order["totalPaise"] == 1500

    rewards = client.get("/api/v1/rewards/me", headers=headers)
    assert rewards.json()["data"]["pointsBalance"] == 50
    cancelled = client.post(
        f"/api/v1/orders/{order['id']}/cancel",
        headers=headers,
        json={"reason": "Test points restoration"},
    )
    assert cancelled.status_code == 200, cancelled.text
    restored = client.get("/api/v1/rewards/me", headers=headers)
    assert restored.json()["data"]["pointsBalance"] == 100


def test_admin_diagnostics_never_returns_secret_values():
    _, token = _account(UserRole.ADMIN)
    response = TestClient(app).get(
        "/api/v1/admin/diagnostics",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["secretsDisplayed"] is False
    assert "razorpay" in data["integrations"]
    assert "redis" in data["integrations"]
    assert "razorpay_key_secret" not in data
    assert "database_url" not in data


def test_reward_points_are_earned_only_after_verified_collection():
    student_id, student_token = _account(UserRole.STUDENT)
    _, admin_token = _account(UserRole.ADMIN)
    product_id = _product()
    with SessionLocal() as db:
        staff = User(
            full_name="Rewards Counter Staff",
            email=f"rewards-staff-{uuid.uuid4().hex[:12]}@example.edu",
            password_hash=hash_password("Rewards-Test-Password-123!"),
            role=UserRole.STAFF,
            is_active=True,
            staff_permissions_json=json.dumps({"canVerifyPickup": True}),
        )
        db.add(staff)
        db.commit()
        staff_token = create_access_token(staff)

    client = TestClient(app)
    credit = client.post(
        "/api/v1/admin/wallet/credit",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "userId": student_id,
            "amountPaise": 5000,
            "reason": "Verified prepaid plan",
            "referenceKey": f"collect-{uuid.uuid4().hex[:12]}",
        },
    )
    assert credit.status_code == 201, credit.text
    order_response = client.post(
        "/api/v1/orders",
        headers={"Authorization": f"Bearer {student_token}"},
        json={"items": [{"menuItemId": product_id, "quantity": 1}], "payWithWallet": True},
    )
    assert order_response.status_code == 201, order_response.text
    order_id = order_response.json()["data"]["order"]["id"]
    with SessionLocal() as db:
        order = db.get(Order, order_id)
        order.status = OrderStatus.READY
        order.ready_at = datetime.now(timezone.utc)
        db.commit()

    collected = client.post(
        "/api/v1/pickups/verify",
        headers={"Authorization": f"Bearer {staff_token}"},
        json={"orderId": order_id},
    )
    assert collected.status_code == 200, collected.text
    rewards = client.get(
        "/api/v1/rewards/me",
        headers={"Authorization": f"Bearer {student_token}"},
    )
    assert rewards.json()["data"]["pointsBalance"] == 2
    duplicate = client.post(
        "/api/v1/pickups/verify",
        headers={"Authorization": f"Bearer {staff_token}"},
        json={"orderId": order_id},
    )
    assert duplicate.status_code == 409
    rewards_after_retry = client.get(
        "/api/v1/rewards/me",
        headers={"Authorization": f"Bearer {student_token}"},
    )
    assert rewards_after_retry.json()["data"]["pointsBalance"] == 2
