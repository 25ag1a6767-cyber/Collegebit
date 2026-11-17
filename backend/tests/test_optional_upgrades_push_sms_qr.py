from __future__ import annotations

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import (
    CanteenCounter,
    Category,
    DevicePushToken,
    Product,
    User,
    UserNotificationPreference,
    UserRole,
)
from app.modules.rewards_wallet import record_wallet_movement
from app.security import create_access_token, hash_password

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_alembic_0026_migration_creates_device_push_tokens(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    import sqlite3

    db_file = tmp_path / "mig_0026.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    get_settings.cache_clear()
    try:
        cfg = Config(str(REPO_ROOT / "backend" / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "backend" / "alembic"))
        command.upgrade(cfg, "head")

        conn = sqlite3.connect(db_file)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            assert "device_push_tokens" in tables
        finally:
            conn.close()

        command.downgrade(cfg, "0025_phase89_enterprise_intelligence")
        command.upgrade(cfg, "head")
    finally:
        get_settings.cache_clear()


def test_expo_device_push_register_send_prune_and_sms_webhook(monkeypatch):
    expo_posts: list[dict] = []
    sms_posts: list[dict] = []

    class FakeResp:
        def __init__(self, status_code: int, payload: dict):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    def fake_httpx_post(url, **kwargs):
        if "exp.host" in url:
            expo_posts.append({"url": url, **kwargs})
            msgs = kwargs.get("json") or []
            return FakeResp(
                200,
                {
                    "data": [
                        {"status": "error", "details": {"error": "DeviceNotRegistered"}}
                        if "DEAD" in m["to"]
                        else {"status": "ok"}
                        for m in msgs
                    ]
                },
            )
        sms_posts.append({"url": url, **kwargs})
        return FakeResp(200, {"accepted": True})

    import httpx
    monkeypatch.setattr(httpx, "post", fake_httpx_post)
    monkeypatch.setenv("SMS_ENABLED", "true")
    monkeypatch.setenv("SMS_WEBHOOK_URL", "https://sms.campus.example/send")
    monkeypatch.setenv("SMS_WEBHOOK_BEARER_TOKEN", "sms-test-token")
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "canteen_open_days", "0,1,2,3,4,5,6")
    monkeypatch.setattr(get_settings(), "canteen_open_time", "")
    monkeypatch.setattr(get_settings(), "canteen_close_time", "")

    uid = uuid.uuid4().hex[:8]
    try:
        with TestClient(app) as client:
            assert client.get("/api/ready").status_code == 200
            with SessionLocal() as db:
                student = User(
                    full_name="Mobile Student",
                    email=f"mob-{uid}@college.edu",
                    phone="+919876543210",
                    password_hash=hash_password("MobilePass#12345"),
                    role=UserRole.STUDENT,
                )
                staff = User(
                    full_name="Mobile Staff",
                    email=f"mobstf-{uid}@college.edu",
                    password_hash=hash_password("StaffPass#12345"),
                    role=UserRole.STAFF,
                )
                counter = db.query(CanteenCounter).first() or CanteenCounter(
                    name=f"MobCounter-{uid}", location="GF", is_active=True, capacity_per_slot=30
                )
                if not counter.id:
                    db.add(counter)
                    db.flush()
                cat = db.query(Category).first() or Category(
                    name=f"MobCat-{uid}", slug=f"mobcat-{uid}", item_type="food", is_active=True
                )
                if not cat.id:
                    db.add(cat)
                    db.flush()
                db.add_all([student, staff])
                db.flush()
                db.add(UserNotificationPreference(user_id=student.id, sms_enabled=True, web_push_enabled=True))
                record_wallet_movement(db, student.id, 50000, "admin_credit", "Mobile push wallet credit", f"mob-{uid}", actor_user_id=staff.id)
                prod = Product(
                    name=f"MobIdli-{uid}",
                    description="Steamed idli",
                    category_id=cat.id,
                    counter_id=counter.id,
                    price=25,
                    price_paise=2500,
                    stock_quantity=10,
                    is_available=True,
                )
                db.add(prod)
                db.commit()
                db.refresh(student)
                db.refresh(staff)
                db.refresh(prod)
                stu_tok = create_access_token(student)
                stf_tok = create_access_token(staff)
                stu_id, prod_id, ctr_id = student.id, prod.id, counter.id

            # Reject malformed token
            bad = client.post(
                "/api/v1/push/device/register",
                headers={"Authorization": f"Bearer {stu_tok}"},
                json={"expoPushToken": "not-an-expo-token", "platform": "android"},
            )
            assert bad.status_code == 422

            live_tok = f"ExponentPushToken[live-{uid}]"
            dead_tok = f"ExponentPushToken[DEAD-{uid}]"
            assert client.post(
                "/api/v1/push/device/register",
                headers={"Authorization": f"Bearer {stu_tok}"},
                json={"expoPushToken": live_tok, "platform": "android", "appVersion": "2.0.0"},
            ).status_code == 201
            assert client.post(
                "/api/v1/push/device/register",
                headers={"Authorization": f"Bearer {stu_tok}"},
                json={"expoPushToken": dead_tok, "platform": "ios"},
            ).status_code == 201

            order_res = client.post(
                "/api/v1/orders",
                headers={"Authorization": f"Bearer {stu_tok}"},
                json={"counterId": ctr_id, "fromCart": False, "payWithWallet": True, "items": [{"menuItemId": prod_id, "quantity": 1}]},
            )
            assert order_res.status_code == 201, order_res.text
            order_id = order_res.json()["data"]["order"]["id"]

            assert client.post(f"/api/v1/staff/orders/{order_id}/accept", headers={"Authorization": f"Bearer {stf_tok}"}, json={}).status_code == 200
            assert client.post(f"/api/v1/staff/orders/{order_id}/ready", headers={"Authorization": f"Bearer {stf_tok}"}, json={}).status_code == 200

            assert len(expo_posts) >= 2
            assert any("Ready" in m["title"] or "ready" in m["title"].lower() for call in expo_posts for m in call["json"])
            assert len(sms_posts) >= 2
            assert sms_posts[-1]["json"]["to"] == "+919876543210"
            assert sms_posts[-1]["headers"]["Authorization"] == "Bearer sms-test-token"

            # When student disables SMS preference, SMS webhook is skipped cleanly
            before_sms_count = len(sms_posts)
            assert client.patch(
                "/api/v1/users/me/notification-preferences",
                headers={"Authorization": f"Bearer {stu_tok}"},
                json={"smsEnabled": False},
            ).status_code == 200
            order2 = client.post(
                "/api/v1/orders",
                headers={"Authorization": f"Bearer {stu_tok}"},
                json={"counterId": ctr_id, "fromCart": False, "payWithWallet": True, "items": [{"menuItemId": prod_id, "quantity": 1}]},
            )
            assert order2.status_code == 201
            oid2 = order2.json()["data"]["order"]["id"]
            assert client.post(f"/api/v1/staff/orders/{oid2}/accept", headers={"Authorization": f"Bearer {stf_tok}"}, json={}).status_code == 200
            assert len(sms_posts) == before_sms_count

            # Dead token was pruned automatically while live token stayed intact
            with SessionLocal() as db:
                remaining = {r.expo_push_token for r in db.query(DevicePushToken).filter_by(user_id=stu_id).all()}
                assert live_tok in remaining
                assert dead_tok not in remaining

            # Explicit unregister works cleanly
            unreg = client.delete(
                "/api/v1/push/device/register",
                params={"expoPushToken": live_tok},
                headers={"Authorization": f"Bearer {stu_tok}"},
            )
            assert unreg.status_code == 200
            assert unreg.json()["data"]["registered"] is False
    finally:
        get_settings.cache_clear()


def test_staff_portal_serves_self_hosted_jsqr_decoder():
    with TestClient(app) as client:
        staff = client.get("/staff")
        assert staff.status_code == 200
        assert '<script src="/js/vendor/jsQR.js" defer></script>' in staff.text
        vendor = client.get("/js/vendor/jsQR.js")
        assert vendor.status_code == 200
        assert "jsQR" in vendor.text
        portal_js = client.get("/js/staff-portal.js").text
        assert "window.jsQR" in portal_js
