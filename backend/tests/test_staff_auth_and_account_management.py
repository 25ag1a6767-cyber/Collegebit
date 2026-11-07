"""Tests for CampusBite AI Staff Portal Authentication, Connectivity & Admin Account Management."""
from __future__ import annotations

import json
import uuid
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import CanteenCounter, User, UserRole
from app.security import create_access_token, hash_password


def test_staff_login_and_me_endpoints():
    """Verify staff authentication, GET /me and GET /users/me availability."""
    client = TestClient(app)
    db = SessionLocal()
    try:
        staff_email = f"staff-{uuid.uuid4().hex[:8]}@example.com"
        staff = User(
            full_name="Chef Ramesh",
            email=staff_email,
            password_hash=hash_password("ChefSecret123!"),
            role=UserRole.STAFF,
            is_active=True,
            staff_permissions_json=json.dumps({"canVerifyPickup": True, "canManageInventory": True}),
        )
        db.add(staff)
        db.commit()

        # 1. Successful staff login
        resp = client.post(
            "/api/v1/auth/login",
            json={"identifier": staff_email, "password": "ChefSecret123!"},
        )
        assert resp.status_code == 200
        body = resp.json()["data"]
        assert body["user"]["email"] == staff_email
        assert body["user"]["role"] == "STAFF"
        token = body["accessToken"]

        # 2. Verify GET /api/v1/me
        me_resp = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert me_resp.status_code == 200
        assert me_resp.json()["data"]["email"] == staff_email
        assert me_resp.json()["data"]["role"] == "STAFF"

        # 3. Verify GET /api/v1/users/me
        users_me_resp = client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})
        assert users_me_resp.status_code == 200
        assert users_me_resp.json()["data"]["email"] == staff_email

        # 4. Incorrect password returns 401 INVALID_CREDENTIALS
        bad_pw_resp = client.post(
            "/api/v1/auth/login",
            json={"identifier": staff_email, "password": "WrongPassword!"},
        )
        assert bad_pw_resp.status_code == 401
        assert bad_pw_resp.json()["error"]["code"] == "INVALID_CREDENTIALS"

        # 5. Unknown email returns 401 INVALID_CREDENTIALS
        unknown_resp = client.post(
            "/api/v1/auth/login",
            json={"identifier": "nonexistent@example.com", "password": "Password123!"},
        )
        assert unknown_resp.status_code == 401
        assert unknown_resp.json()["error"]["code"] == "INVALID_CREDENTIALS"
    finally:
        db.close()


def test_inactive_staff_account_blocked():
    """Verify deactivated staff accounts receive 403 ACCOUNT_DISABLED."""
    client = TestClient(app)
    db = SessionLocal()
    try:
        inactive_email = f"inactive-staff-{uuid.uuid4().hex[:8]}@example.com"
        staff = User(
            full_name="Former Staff",
            email=inactive_email,
            password_hash=hash_password("ChefSecret123!"),
            role=UserRole.STAFF,
            is_active=False,
        )
        db.add(staff)
        db.commit()

        resp = client.post(
            "/api/v1/auth/login",
            json={"identifier": inactive_email, "password": "ChefSecret123!"},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "ACCOUNT_DISABLED"
    finally:
        db.close()


def test_admin_staff_account_creation_status_toggle_and_password_reset():
    """Verify admin can create staff, toggle active status, and reset passwords."""
    client = TestClient(app)
    db = SessionLocal()
    try:
        admin = User(
            full_name="Super Admin",
            email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("AdminSecret123!"),
            role=UserRole.ADMIN,
            is_active=True,
        )
        db.add(admin)
        db.commit()

        admin_token = create_access_token(admin)

        # 1. Admin creates a staff account
        new_staff_email = f"new-staff-{uuid.uuid4().hex[:8]}@example.com"
        create_resp = client.post(
            "/api/v1/admin/users",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "fullName": "Pooja Sharma",
                "email": new_staff_email,
                "role": "staff",
                "temporaryPassword": "TempPassword123!",
            },
        )
        assert create_resp.status_code == 201
        staff_id = create_resp.json()["data"]["id"]

        # Staff can log in with temporary password
        staff_login = client.post(
            "/api/v1/auth/login",
            json={"identifier": new_staff_email, "password": "TempPassword123!"},
        )
        assert staff_login.status_code == 200

        # 2. Admin deactivates staff account
        deact_resp = client.patch(
            f"/api/v1/admin/users/{staff_id}/status",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"isActive": False},
        )
        assert deact_resp.status_code == 200
        assert deact_resp.json()["data"]["isActive"] is False

        # Login is now blocked
        blocked_login = client.post(
            "/api/v1/auth/login",
            json={"identifier": new_staff_email, "password": "TempPassword123!"},
        )
        assert blocked_login.status_code == 403
        assert blocked_login.json()["error"]["code"] == "ACCOUNT_DISABLED"

        # 3. Admin re-activates staff account
        react_resp = client.patch(
            f"/api/v1/admin/users/{staff_id}/status",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"isActive": True},
        )
        assert react_resp.status_code == 200
        assert react_resp.json()["data"]["isActive"] is True

        # 4. Admin resets staff password
        reset_resp = client.post(
            f"/api/v1/admin/users/{staff_id}/reset-password",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"newPassword": "NewBrandPassword456!"},
        )
        assert reset_resp.status_code == 200
        assert reset_resp.json()["data"]["reset"] is True

        # Old password fails
        old_pw_login = client.post(
            "/api/v1/auth/login",
            json={"identifier": new_staff_email, "password": "TempPassword123!"},
        )
        assert old_pw_login.status_code == 401

        # New password succeeds
        new_pw_login = client.post(
            "/api/v1/auth/login",
            json={"identifier": new_staff_email, "password": "NewBrandPassword456!"},
        )
        assert new_pw_login.status_code == 200
    finally:
        db.close()
