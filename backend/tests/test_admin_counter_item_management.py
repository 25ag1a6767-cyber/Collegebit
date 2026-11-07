import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import CanteenCounter, Category, Product, User, UserRole
from app.security import hash_password


def create_user(role: UserRole) -> tuple[str, str]:
    email = f"{role.value}-{uuid.uuid4().hex}@example.edu"
    password = f"Test-only-{uuid.uuid4().hex}"
    with SessionLocal() as db:
        db.add(
            User(
                full_name=f"CI {role.value.title()}",
                email=email,
                password_hash=hash_password(password),
                role=role,
                is_active=True,
            )
        )
        db.commit()
    return email, password


def login(client: TestClient, email: str, password: str) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"identifier": email, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["accessToken"]


def test_admin_and_staff_login_with_registered_credentials():
    client = TestClient(app)
    admin_email, admin_password = create_user(UserRole.ADMIN)
    staff_email, staff_password = create_user(UserRole.STAFF)

    for email, password, expected_role in (
        (admin_email, admin_password, "ADMIN"),
        (staff_email, staff_password, "STAFF"),
    ):
        response = client.post(
            "/api/v1/auth/login",
            json={"identifier": email.upper(), "password": password},
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["user"]["role"] == expected_role
        assert data["accessToken"]


def test_counters_can_be_created_with_unique_names():
    suffix = uuid.uuid4().hex
    names = (f"Main Counter {suffix}", f"Counter A {suffix}")
    with SessionLocal() as db:
        db.add_all(
            [
                CanteenCounter(name=name, location="CI", is_active=True)
                for name in names
            ]
        )
        db.commit()
        counters = db.scalars(
            select(CanteenCounter).where(CanteenCounter.name.in_(names))
        ).all()
    assert {counter.name for counter in counters} == set(names)


def test_admin_can_reassign_and_delete_menu_item():
    client = TestClient(app)
    email, password = create_user(UserRole.ADMIN)
    token = login(client, email, password)
    suffix = uuid.uuid4().hex
    with SessionLocal() as db:
        category = Category(
            name=f"CI Category {suffix}",
            slug=f"ci-category-{suffix}",
            item_type="food",
            is_active=True,
        )
        counters = [
            CanteenCounter(name=f"CI Counter {suffix}-{index}", location="CI")
            for index in (1, 2)
        ]
        db.add_all([category, *counters])
        db.commit()
        category_id = category.id
        first_counter_id, second_counter_id = (counter.id for counter in counters)

    headers = {"Authorization": f"Bearer {token}"}
    created = client.post(
        "/api/v1/admin/menu-items",
        json={
            "categoryId": category_id,
            "counterId": first_counter_id,
            "name": f"CI Item {suffix}",
            "pricePaise": 5500,
            "costPaise": 2000,
            "stockQuantity": 15,
            "preparationMinutes": 5,
            "dietaryTags": ["veg"],
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    item_id = created.json()["data"]["id"]
    assert created.json()["data"]["counterId"] == first_counter_id

    updated = client.patch(
        f"/api/v1/admin/menu-items/{item_id}",
        json={
            "counterId": second_counter_id,
            "pricePaise": 6000,
            "stockQuantity": 25,
        },
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["counterId"] == second_counter_id
    assert updated.json()["data"]["pricePaise"] == 6000
    assert updated.json()["data"]["stockQuantity"] == 25

    deleted = client.delete(f"/api/v1/admin/menu-items/{item_id}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["deleted"] is True
    with SessionLocal() as db:
        product = db.get(Product, item_id)
        assert product is None or product.is_archived
