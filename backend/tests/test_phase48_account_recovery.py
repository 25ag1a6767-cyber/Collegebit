"""Phase 48: password reset by email token, forced temporary-password change."""
import uuid
from fastapi.testclient import TestClient
from sqlalchemy import select
import app.modules.admin_portal as admin_portal
from app.main import app, _login_attempts
from app.database import SessionLocal
from app.models import User, UserRole, AuditEvent
from app.security import hash_password, create_password_reset_token

PASSWORD = "Phase48-secure-password!"

def _make_user(role="student"):
    email = f"phase48-{role}-{uuid.uuid4().hex[:10]}@example.edu"
    with SessionLocal() as db:
        user = User(full_name=f"Phase 48 {role}", email=email, password_hash=hash_password(PASSWORD), role=UserRole(role), is_active=True)
        db.add(user)
        db.commit()
        return user.id, email

def _login(client, email, password=PASSWORD):
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}

def _token_for(email):
    with SessionLocal() as db:
        return create_password_reset_token(db.scalar(select(User).where(User.email == email)))

def test_forgot_password_response_does_not_reveal_whether_email_exists():
    _login_attempts.clear()
    with TestClient(app) as client:
        _, email = _make_user()
        known = client.post("/api/auth/forgot-password", json={"email": email})
        unknown = client.post("/api/auth/forgot-password", json={"email": f"nobody-{uuid.uuid4().hex[:8]}@example.edu"})
        assert known.status_code == unknown.status_code == 200
        assert known.json() == unknown.json()

def test_forgot_password_is_rate_limited():
    _login_attempts.clear()
    with TestClient(app) as client:
        email = f"limit-{uuid.uuid4().hex[:8]}@example.edu"
        codes = [client.post("/api/auth/forgot-password", json={"email": email}).status_code for _ in range(7)]
        assert 429 in codes and codes[0] == 200

def test_reset_token_sets_new_password_once_and_signs_out_old_sessions():
    _login_attempts.clear()
    with TestClient(app) as client:
        _, email = _make_user()
        old_headers = _login(client, email)
        token = _token_for(email)
        new_password = "Brand-new-password-48!"
        assert client.post("/api/auth/reset-password", json={"token": token, "new_password": new_password}).status_code == 200
        assert client.post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code == 401
        assert client.post("/api/auth/login", json={"email": email, "password": new_password}).status_code == 200
        assert client.get("/api/auth/me", headers=old_headers).status_code == 401

        again = client.post("/api/auth/reset-password", json={"token": token, "new_password": "Another-password-48!!"})
        assert again.status_code == 400
        with SessionLocal() as db:
            assert db.scalar(select(AuditEvent).where(AuditEvent.action == "user.password_reset")) is not None

def test_reset_rejects_garbage_token_and_disabled_account():
    _login_attempts.clear()
    with TestClient(app) as client:
        bad = client.post("/api/auth/reset-password", json={"token": "x" * 40, "new_password": "Valid-password-12345"})
        assert bad.status_code == 400
        _, email = _make_user()
        token = _token_for(email)
        with SessionLocal() as db:
            db.scalar(select(User).where(User.email == email)).is_active = False
            db.commit()
        assert client.post("/api/auth/reset-password", json={"token": token, "new_password": "Valid-password-12345"}).status_code == 400

def test_new_staff_must_change_temporary_password_before_using_staff_tools():
    _login_attempts.clear()
    with TestClient(app) as client:
        _, admin_email = _make_user("admin")
        admin = _login(client, admin_email)
        staff_email = f"temp-staff-{uuid.uuid4().hex[:8]}@example.edu"
        created = client.post("/api/admin/users", headers=admin, json={"full_name": "Temp Staff", "email": staff_email, "role": "staff", "temporary_password": "Temporary-password-48!"})
        assert created.status_code == 201 and created.json()["must_change_password"] is True
        staff = _login(client, staff_email, "Temporary-password-48!")
        assert client.get("/api/staff/dashboard", headers=staff).status_code == 403
        assert client.get("/api/auth/me", headers=staff).json()["must_change_password"] is True
        changed = client.post("/api/auth/change-password", headers=staff, json={"current_password": "Temporary-password-48!", "new_password": "My-own-staff-password-48!"})
        assert changed.status_code == 200
        staff = _login(client, staff_email, "My-own-staff-password-48!")
        assert client.get("/api/staff/dashboard", headers=staff).status_code == 200
        assert client.get("/api/auth/me", headers=staff).json()["must_change_password"] is False

def test_staff_login_requires_staff_role_and_preserves_generic_credential_errors():
    _login_attempts.clear()
    with TestClient(app) as client:
        _, staff_email = _make_user("staff")
        _, student_email = _make_user("student")
        wrong = client.post("/api/v1/auth/staff/login", json={"identifier": staff_email.upper(), "password": "wrong-password"})
        unknown = client.post("/api/v1/auth/staff/login", json={"identifier": f"missing-{uuid.uuid4().hex[:8]}@example.edu", "password": PASSWORD})
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json()["error"]["code"] == unknown.json()["error"]["code"] == "INVALID_CREDENTIALS"
        assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]

        staff = client.post("/api/v1/auth/staff/login", json={"identifier": staff_email.upper(), "password": PASSWORD})
        assert staff.status_code == 200 and staff.json()["data"]["accessToken"]
        student = client.post("/api/v1/auth/staff/login", json={"identifier": student_email, "password": PASSWORD})
        assert student.status_code == 403
        assert student.json()["error"]["code"] == "STAFF_PORTAL_FORBIDDEN"

        _, inactive_email = _make_user("staff")
        with SessionLocal() as db:
            inactive = db.scalar(select(User).where(User.email == inactive_email))
            inactive.is_active = False
            db.commit()
        disabled = client.post("/api/v1/auth/staff/login", json={"identifier": inactive_email, "password": PASSWORD})
        assert disabled.status_code == 403
        assert disabled.json()["error"]["code"] == "ACCOUNT_DISABLED"

def test_legacy_login_rejects_former_fixed_credentials():
    _login_attempts.clear()
    former_credentials = [
        ("25ag1a6767@aceec.ac.in", "ace@25ag1a67676", "admin"),
        ("25ag1a6767@gmail.com", "ace@25ag1a67676staff", "staff"),
    ]
    stored_hashes = {}
    with SessionLocal() as db:
        for email, _, role in former_credentials:
            user = db.scalar(select(User).where(User.email == email))
            if user is None:
                user = User(full_name="Legacy Login Regression", email=email, password_hash=hash_password(PASSWORD), role=UserRole(role), is_active=True)
                db.add(user)
            else:
                user.password_hash = hash_password(PASSWORD)
                user.role = UserRole(role)
                user.is_active = True
            stored_hashes[email] = user.password_hash
        db.commit()

    with TestClient(app) as client:
        for email, old_password, _ in former_credentials:
            response = client.post("/api/auth/login", json={"email": email, "password": old_password})
            assert response.status_code == 401

    with SessionLocal() as db:
        for email, expected_hash in stored_hashes.items():
            assert db.scalar(select(User).where(User.email == email)).password_hash == expected_hash

def test_admin_password_reset_reports_email_delivery_result(monkeypatch):
    _login_attempts.clear()
    with TestClient(app) as client:
        _, admin_email = _make_user("admin")
        admin = _login(client, admin_email)
        staff_id, staff_email = _make_user("staff")
        sent = []
        monkeypatch.setattr(admin_portal, "send_email", lambda address, subject, body: sent.append((address, subject, body)) or True)

        response = client.post(f"/api/v1/admin/users/{staff_id}/reset-password", headers=admin, json={})
        assert response.status_code == 200
        assert response.json()["data"]["message"].endswith("has been sent.")
        assert sent and sent[0][0] == staff_email

        monkeypatch.setattr(admin_portal, "send_email", lambda *_: False)
        failed = client.post(f"/api/v1/admin/users/{staff_id}/reset-password", headers=admin, json={})
        assert failed.status_code == 503
        assert failed.json()["error"]["code"] == "EMAIL_UNAVAILABLE"

def test_admin_can_deactivate_and_reactivate_staff_and_revokes_old_access():
    _login_attempts.clear()
    with TestClient(app) as client:
        _, admin_email = _make_user("admin")
        admin = _login(client, admin_email)
        staff_id, staff_email = _make_user("staff")
        login = client.post("/api/v1/auth/staff/login", json={"identifier": staff_email, "password": PASSWORD})
        assert login.status_code == 200
        access = login.json()["data"]["accessToken"]

        deactivated = client.patch(f"/api/v1/admin/users/{staff_id}/status", headers=admin, json={"isActive": False})
        assert deactivated.status_code == 200
        assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {access}"}).status_code == 401
        denied = client.post("/api/v1/auth/staff/login", json={"identifier": staff_email, "password": PASSWORD})
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "ACCOUNT_DISABLED"

        reactivated = client.patch(f"/api/v1/admin/users/{staff_id}/status", headers=admin, json={"isActive": True})
        assert reactivated.status_code == 200
        assert client.post("/api/v1/auth/staff/login", json={"identifier": staff_email, "password": PASSWORD}).status_code == 200

def test_alembic_migrations_apply_cleanly_to_a_fresh_database(tmp_path):
    """Regression: a brand-new database (the Docker 'migrate' job) must reach head without errors."""
    import os, subprocess, sys, sqlite3
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    db_file = tmp_path / "fresh.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_file}", "PYTHONPATH": str(root / "backend")}
    result = subprocess.run([sys.executable, "-m", "alembic", "-c", str(root / "backend" / "alembic.ini"), "upgrade", "head"], env=env, cwd=str(root), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-1500:]
    with sqlite3.connect(db_file) as conn:
        assert conn.execute("select version_num from alembic_version").fetchone()[0] in ("0020_campusbite_v2", "0021_campusbite_v3")
