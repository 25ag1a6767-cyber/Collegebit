"""Step 2 regression tests: production config, CORS, cookie/token security, and zero dev defaults."""
import os
import uuid
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from app.config import Settings
from app.database import SessionLocal
import app.main as main
from app.main import app
from app.models import User, UserRole
from app.security import hash_password
from app.seed import seed_demo_data


def _prod_kwargs(**overrides):
    data = {
        "environment": "production",
        "secret_key": "prod-only-unique-secret-key-at-least-32-chars-0001",
        "auto_create_tables": False,
        "seed_demo_data": False,
        "public_base_url": "https://canteen.college.edu",
        "cors_origins": "https://canteen.college.edu",
    }
    data.update(overrides)
    return data


def test_production_settings_reject_debug_docs_and_insecure_cors():
    s = Settings(_env_file=None, **_prod_kwargs())
    assert s.docs_enabled is False

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **_prod_kwargs(expose_api_docs=True))

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **_prod_kwargs(cors_origins="*"))

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **_prod_kwargs(cors_origins="http://canteen.college.edu"))


def test_seed_demo_data_refuses_production(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "prod-only-unique-secret-key-at-least-32-chars-0001")
    monkeypatch.setenv("AUTO_CREATE_TABLES", "false")
    monkeypatch.setenv("SEED_DEMO_DATA", "false")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://canteen.college.edu")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        with SessionLocal() as db:
            with pytest.raises(RuntimeError, match="forbidden in production"):
                seed_demo_data(db)
    finally:
        get_settings.cache_clear()


def test_login_has_no_hardcoded_fallback_backdoors_and_rate_limits_admin_emails():
    main._login_attempts.clear()
    unique = uuid.uuid4().hex[:8]
    with TestClient(app) as client:
        # Fallback credentials must never authenticate or auto-create accounts
        for ident, pw in [
            ("admin", "123456"),
            ("admin", "admin123"),
            ("staff", "staff123"),
            ("25ag1a6767", "ace12345"),
            (f"nobody-{unique}@college.edu", "25ag1a6767"),
        ]:
            res = client.post("/api/v1/auth/login", json={"identifier": ident, "password": pw})
            assert res.status_code == 401, res.text

        # Emails containing "admin" or "staff" must not bypass login rate limiting
        target_email = f"admin-{unique}@college.edu"
        with SessionLocal() as db:
            db.add(
                User(
                    full_name="Rate Limit Admin",
                    email=target_email,
                    password_hash=hash_password("RealAdminPass-123!"),
                    role=UserRole.ADMIN,
                    is_active=True,
                )
            )
            db.commit()

        for _ in range(5):
            r = client.post("/api/auth/login", json={"email": target_email, "password": "wrong-password-123"})
            assert r.status_code == 401
        blocked = client.post("/api/auth/login", json={"email": target_email, "password": "wrong-password-123"})
        assert blocked.status_code == 429


def test_refresh_cookie_security_attributes_and_csrf_origin_check():
    main._login_attempts.clear()
    email = f"cookie-student-{uuid.uuid4().hex[:8]}@college.edu"
    pw = "StudentCookiePass-123!"
    with SessionLocal() as db:
        db.add(
            User(
                full_name="Cookie Student",
                email=email,
                password_hash=hash_password(pw),
                role=UserRole.STUDENT,
                is_active=True,
            )
        )
        db.commit()

    with TestClient(app) as client:
        login_res = client.post("/api/v1/auth/login", json={"identifier": email, "password": pw})
        assert login_res.status_code == 200, login_res.text
        set_cookie = login_res.headers.get("set-cookie", "")
        assert "cb_refresh_token=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=lax" in set_cookie or "SameSite=Lax" in set_cookie
        assert "Path=/api/v1/auth" in set_cookie
        access = login_res.json()["data"]["accessToken"]

        # Reject cross-origin cookie refresh attempts
        evil = client.post(
            "/api/v1/auth/refresh",
            json={},
            headers={"Origin": "https://evil.example.com"},
        )
        assert evil.status_code == 403

        # Same-origin cookie refresh rotates cleanly even without body token
        refreshed = client.post("/api/v1/auth/refresh", json={})
        assert refreshed.status_code == 200, refreshed.text
        new_access = refreshed.json()["data"]["accessToken"]

        # Logout revokes session and clears cookie
        out = client.post(
            "/api/v1/auth/logout",
            json={},
            headers={"Authorization": f"Bearer {new_access or access}"},
        )
        assert out.status_code == 200
        clear_cookie = out.headers.get("set-cookie", "")
        assert 'cb_refresh_token=""' in clear_cookie or "Max-Age=0" in clear_cookie


def test_create_admin_cli_initializes_admin_without_dev_defaults(tmp_path):
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "fresh_prod.db"
    env = {
        **os.environ,
        "PYTHONPATH": str(root / "backend"),
        "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
        "ENVIRONMENT": "production",
        "AUTO_CREATE_TABLES": "false",
        "SEED_DEMO_DATA": "false",
        "EXPOSE_API_DOCS": "false",
        "PUBLIC_BASE_URL": "https://canteen.college.edu",
        "SECRET_KEY": "prod-only-unique-secret-key-at-least-32-chars-0001",
    }
    mig = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(root / "backend" / "alembic.ini"), "upgrade", "head"],
        env=env,
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    assert mig.returncode == 0, mig.stderr

    adm = subprocess.run(
        [sys.executable, "-m", "app.create_admin", "--email", "boss@college.edu", "--name", "Boss Admin", "--password-stdin"],
        input="StrongAdminPass-2026!\n",
        env=env,
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    assert adm.returncode == 0, adm.stderr
    with sqlite3.connect(db_path) as con:
        rows = con.execute("SELECT email, role, is_active, must_change_password FROM users").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "boss@college.edu"
        assert rows[0][1].lower() == "admin"
        assert rows[0][2] == 1
        assert rows[0][3] == 0
