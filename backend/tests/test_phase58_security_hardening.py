"""Regression tests for production security hardening added in Phase 58."""
import pytest
from fastapi import HTTPException

import app.main as main


def test_production_rate_limiter_fails_closed_without_redis(monkeypatch):
    monkeypatch.setattr(main, "IS_PRODUCTION", True)
    monkeypatch.setattr(main.settings, "redis_url", "")
    monkeypatch.setattr(main.settings, "require_shared_rate_limiter", True)
    monkeypatch.setattr(main, "_redis", None)
    with pytest.raises(HTTPException) as error:
        main._check_login_rate("phase58-test-no-redis")
    assert error.value.status_code == 503


def test_production_single_worker_without_redis_falls_back_to_memory(monkeypatch):
    import uuid
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main, "IS_PRODUCTION", True)
    monkeypatch.setattr(main.settings, "redis_url", "")
    monkeypatch.setattr(main.settings, "require_shared_rate_limiter", False)
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    monkeypatch.setattr(main, "_redis", None)
    assert main._check_login_rate("phase58-test-single-worker") == 0

    email = f"prod-single-{uuid.uuid4().hex[:8]}@college.edu"
    with TestClient(main.app) as client:
        reg = client.post(
            "/api/auth/register",
            json={"full_name": "Prod Single Student", "email": email, "password": "ProdPassword!2026"},
        )
        assert reg.status_code == 201, reg.text
        login = client.post("/api/auth/login", json={"email": email, "password": "ProdPassword!2026"})
        assert login.status_code == 200, login.text
        assert login.json()["access_token"]


def test_production_info_endpoint_does_not_disclose_framework_or_database(monkeypatch):
    monkeypatch.setattr(main, "IS_PRODUCTION", True)
    import asyncio
    result = asyncio.run(main.info())
    assert result == {"name": main.settings.app_name, "status": "online"}
    assert "database" not in result
    assert "backend" not in result
    assert "version" not in result


def test_development_info_endpoint_keeps_diagnostics(monkeypatch):
    monkeypatch.setattr(main, "IS_PRODUCTION", False)
    import asyncio
    result = asyncio.run(main.info())
    assert "database" in result
    assert "backend" in result
