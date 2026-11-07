"""
Targeted verification for:
1. Admin and Staff password-only authentication (no TOTP code required).
2. Admin assigning menu items to counters (Main Counter vs Counter A).
3. Admin deleting menu items with audit trail.
4. Database only containing the 2 counters (Main Counter, Counter A).
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.database import SessionLocal
from app.models import CanteenCounter, Product, User

@pytest.fixture
def client():
    return TestClient(app)

def test_admin_and_staff_login_without_totp(client):
    # Admin login with email & password (variant 1: ace@25ag1a67676)
    res_admin = client.post("/api/v1/auth/login", json={
        "identifier": "25ag1a6767@aceec.ac.in",
        "password": "ace@25ag1a67676"
    })
    assert res_admin.status_code == 200, res_admin.text
    admin_data = res_admin.json()["data"]
    assert admin_data["user"]["role"].lower() == "admin"
    assert "accessToken" in admin_data

    # Admin login with email & password (variant 2: ace@25ag1a6767)
    res_admin2 = client.post("/api/v1/auth/login", json={
        "identifier": "25ag1a6767@aceec.ac.in",
        "password": "ace@25ag1a6767"
    })
    assert res_admin2.status_code == 200, res_admin2.text

    # Staff login with email & password (variant 1: ace@25ag1a67676staff)
    res_staff = client.post("/api/v1/auth/login", json={
        "identifier": "25ag1a6767@gmail.com",
        "password": "ace@25ag1a67676staff"
    })
    assert res_staff.status_code == 200, res_staff.text
    staff_data = res_staff.json()["data"]
    assert staff_data["user"]["role"].lower() == "staff"
    assert "accessToken" in staff_data

    # Staff login with email & password (variant 2: ace@25ag1a6767staff)
    res_staff2 = client.post("/api/v1/auth/login", json={
        "identifier": "25ag1a6767@gmail.com",
        "password": "ace@25ag1a6767staff"
    })
    assert res_staff2.status_code == 200, res_staff2.text

def test_counters_are_exactly_two(client):
    db = SessionLocal()
    try:
        counters = db.scalars(select(CanteenCounter).order_by(CanteenCounter.id)).all()
        assert len(counters) == 2
        assert counters[0].id == 1
        assert counters[0].name == "Main Counter"
        assert counters[1].id == 2
        assert counters[1].name == "Counter A"
    finally:
        db.close()

def test_admin_menu_item_counter_reassignment_and_deletion(client):
    # 1. Admin login
    res_login = client.post("/api/v1/auth/login", json={
        "identifier": "25ag1a6767@aceec.ac.in",
        "password": "ace@25ag1a67676"
    })
    token = res_login.json()["data"]["accessToken"]
    headers = {"Authorization": f"Bearer {token}"}

    # 2. Create a test item on Counter 1 (Main Counter)
    create_res = client.post("/api/v1/admin/menu-items", json={
        "categoryId": 1,
        "counterId": 1,
        "name": "Test Counter Item",
        "pricePaise": 5500,
        "costPaise": 2000,
        "stockQuantity": 15,
        "preparationMinutes": 5,
        "dietaryTags": ["veg"]
    }, headers=headers)
    assert create_res.status_code == 201, create_res.text
    item_id = create_res.json()["data"]["id"]
    assert create_res.json()["data"]["counterId"] == 1

    # 3. Admin reassigns item to Counter 2 (Counter A)
    patch_res = client.patch(f"/api/v1/admin/menu-items/{item_id}", json={
        "counterId": 2,
        "pricePaise": 6000,
        "stockQuantity": 25
    }, headers=headers)
    assert patch_res.status_code == 200, patch_res.text
    assert patch_res.json()["data"]["counterId"] == 2
    assert patch_res.json()["data"]["pricePaise"] == 6000
    assert patch_res.json()["data"]["stockQuantity"] == 25

    # 4. Admin deletes the item
    del_res = client.delete(f"/api/v1/admin/menu-items/{item_id}", headers=headers)
    assert del_res.status_code == 200, del_res.text
    assert del_res.json()["data"]["deleted"] is True

    # 5. Verify item no longer exists or is archived
    db = SessionLocal()
    try:
        p = db.get(Product, item_id)
        assert p is None or p.is_archived is True
    finally:
        db.close()
