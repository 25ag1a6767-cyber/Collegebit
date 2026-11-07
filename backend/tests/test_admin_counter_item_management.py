"""Admin and staff login plus isolated counter/menu management coverage."""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import CanteenCounter, Category, Product, User, UserRole
from app.security import hash_password

PASSWORD = "counter-test-password-2026"


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def portal_data(client):
    suffix = uuid.uuid4().hex[:12]
    admin_email = f"admin-{suffix}@example.edu"
    staff_email = f"staff-{suffix}@example.edu"

    with SessionLocal() as db:
        counters = [
            CanteenCounter(name=f"Main Counter {suffix}", location="Ground Floor"),
            CanteenCounter(name=f"Counter A {suffix}", location="First Floor"),
        ]
        category = Category(name=f"Test Category {suffix}", slug=f"test-category-{suffix}", item_type="food", is_active=True)
        admin = User(
            full_name="Counter Test Admin",
            email=admin_email,
            password_hash=hash_password(PASSWORD),
            role=UserRole.ADMIN,
            is_active=True,
        )
        staff = User(
            full_name="Counter Test Staff",
            email=staff_email,
            password_hash=hash_password(PASSWORD),
            role=UserRole.STAFF,
            is_active=True,
        )
        db.add_all([*counters, category, admin, staff])
        db.commit()
        for item in [*counters, category, admin, staff]:
            db.refresh(item)
        return {
            "admin_email": admin_email,
            "staff_email": staff_email,
            "counter_ids": [counter.id for counter in counters],
            "counter_names": [counter.name for counter in counters],
            "category_id": category.id,
        }


def test_admin_and_staff_login_without_totp(client, portal_data):
    admin = client.post(
        "/api/v1/auth/login",
        json={"identifier": portal_data["admin_email"], "password": PASSWORD},
    )
    assert admin.status_code == 200, admin.text
    assert admin.json()["data"]["user"]["role"].lower() == "admin"
    assert admin.json()["data"]["accessToken"]

    staff = client.post(
        "/api/v1/auth/staff/login",
        json={"identifier": portal_data["staff_email"], "password": PASSWORD},
    )
    assert staff.status_code == 200, staff.text
    assert staff.json()["data"]["user"]["role"].lower() == "staff"
    assert staff.json()["data"]["accessToken"]


def test_counter_setup_is_isolated_from_unrelated_counters(client, portal_data):
    with SessionLocal() as db:
        counters = db.scalars(
            select(CanteenCounter).where(CanteenCounter.id.in_(portal_data["counter_ids"])).order_by(CanteenCounter.id)
        ).all()
    assert [counter.name for counter in counters] == portal_data["counter_names"]


def test_admin_can_reassign_and_archive_a_menu_item(client, portal_data):
    login = client.post(
        "/api/v1/auth/login",
        json={"identifier": portal_data["admin_email"], "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['data']['accessToken']}"}
    first_counter, second_counter = portal_data["counter_ids"]

    created = client.post(
        "/api/v1/admin/menu-items",
        headers=headers,
        json={
            "categoryId": portal_data["category_id"],
            "counterId": first_counter,
            "name": f"Test Counter Item {uuid.uuid4().hex[:8]}",
            "pricePaise": 5500,
            "costPaise": 2000,
            "stockQuantity": 15,
            "preparationMinutes": 5,
            "dietaryTags": ["veg"],
        },
    )
    assert created.status_code == 201, created.text
    item_id = created.json()["data"]["id"]
    assert created.json()["data"]["counterId"] == first_counter

    updated = client.patch(
        f"/api/v1/admin/menu-items/{item_id}",
        headers=headers,
        json={"counterId": second_counter, "pricePaise": 6000, "stockQuantity": 25},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["counterId"] == second_counter
    assert updated.json()["data"]["pricePaise"] == 6000
    assert updated.json()["data"]["stockQuantity"] == 25

    deleted = client.delete(f"/api/v1/admin/menu-items/{item_id}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["deleted"] is True
    with SessionLocal() as db:
        product = db.get(Product, item_id)
        assert product is None or product.is_archived is True
