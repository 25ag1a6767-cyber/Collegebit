from __future__ import annotations

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import (
    AuditEvent,
    CanteenCounter,
    Category,
    Product,
    User,
    UserNotificationPreference,
    UserRole,
)
from app.modules.rewards_wallet import record_wallet_movement
from app.security import create_access_token, hash_password

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_alembic_0027_free_notification_channels_migration(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    import sqlite3

    db_file = tmp_path / "mig_0027.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    get_settings.cache_clear()
    try:
        cfg = Config(str(REPO_ROOT / "backend" / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "backend" / "alembic"))
        command.upgrade(cfg, "head")

        conn = sqlite3.connect(db_file)
        try:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(user_notification_preferences)").fetchall()
            }
            assert {"whatsapp_enabled", "ntfy_topic", "telegram_chat_id", "telegram_link_token"} <= cols
        finally:
            conn.close()

        command.downgrade(cfg, "0026_expo_mobile_push")
        command.upgrade(cfg, "head")
    finally:
        get_settings.cache_clear()


def test_free_channels_ntfy_telegram_whatsapp_android_sms_and_staff_links(monkeypatch):
    posted: list[dict] = []

    class FakeResp:
        def __init__(self, status_code: int, payload: dict | None = None):
            self.status_code = status_code
            self._payload = payload or {}

        def json(self):
            return self._payload

    def fake_httpx_post(url, **kwargs):
        posted.append({"url": url, **kwargs})
        return FakeResp(200, {"ok": True})

    def fake_httpx_get(url, **kwargs):
        # Return matching /start <token> message for Telegram verify
        token_val = fake_httpx_get.expected_token
        return FakeResp(
            200,
            {
                "ok": True,
                "result": [
                    {"message": {"text": f"/start {token_val}", "chat": {"id": 99887766}}}
                ],
            },
        )

    fake_httpx_get.expected_token = ""

    import httpx
    monkeypatch.setattr(httpx, "post", fake_httpx_post)
    monkeypatch.setattr(httpx, "get", fake_httpx_get)

    monkeypatch.setenv("NTFY_ENABLED", "true")
    monkeypatch.setenv("NTFY_BASE_URL", "https://ntfy.example.test")
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-test-token")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "CollegeBitsTestBot")
    monkeypatch.setenv("WHATSAPP_CLOUD_ENABLED", "true")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "1234567890")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "wa-test-token")
    monkeypatch.setenv("CANTEEN_WHATSAPP_NUMBER", "+919876500000")
    monkeypatch.setenv("SMS_ENABLED", "true")
    monkeypatch.setenv("SMS_WEBHOOK_PRESET", "android_sms_gate")
    monkeypatch.setenv("SMS_WEBHOOK_URL", "https://sms-gate.example.test/message")
    monkeypatch.setenv("SMS_WEBHOOK_USERNAME", "canteen")
    monkeypatch.setenv("SMS_WEBHOOK_PASSWORD", "simpass")
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "canteen_open_days", "0,1,2,3,4,5,6")
    monkeypatch.setattr(get_settings(), "canteen_open_time", "")
    monkeypatch.setattr(get_settings(), "canteen_close_time", "")

    uid = uuid.uuid4().hex[:8]
    raw_phone10 = f"981{uuid.uuid4().int % 10**7:07d}"
    try:
        with TestClient(app) as client:
            assert client.get("/api/ready").status_code == 200
            with SessionLocal() as db:
                student = User(
                    full_name="Free Alerts Student",
                    email=f"free-{uid}@college.edu",
                    phone=raw_phone10,  # 10 digits without +91 to test E.164 normalization
                    password_hash=hash_password("FreePass#12345"),
                    role=UserRole.STUDENT,
                )
                staff = User(
                    full_name="Free Counter Staff",
                    email=f"freestf-{uid}@college.edu",
                    password_hash=hash_password("StaffFree#12345"),
                    role=UserRole.STAFF,
                )
                counter = db.query(CanteenCounter).first() or CanteenCounter(
                    name=f"FreeCounter-{uid}", location="GF", is_active=True, capacity_per_slot=30
                )
                if not counter.id:
                    db.add(counter)
                    db.flush()
                cat = db.query(Category).first() or Category(
                    name=f"FreeCat-{uid}", slug=f"freecat-{uid}", item_type="food", is_active=True
                )
                if not cat.id:
                    db.add(cat)
                    db.flush()
                db.add_all([student, staff])
                db.flush()
                record_wallet_movement(db, student.id, 50000, "admin_credit", "Free test credit", f"free-{uid}", actor_user_id=staff.id)
                prod = Product(
                    name=f"FreePoha-{uid}",
                    description="Warm poha",
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
                ctr_id, prod_id, stu_id = counter.id, prod.id, student.id

            # 1. Opt in WhatsApp + SMS preferences
            pref_res = client.patch(
                "/api/v1/users/me/notification-preferences",
                headers={"Authorization": f"Bearer {stu_tok}"},
                json={"whatsappEnabled": True, "smsEnabled": True},
            )
            assert pref_res.status_code == 200
            assert pref_res.json()["data"]["whatsappEnabled"] is True

            # 2. Enable ntfy ($0 phone push)
            ntfy_res = client.post("/api/v1/push/ntfy/enable", headers={"Authorization": f"Bearer {stu_tok}"})
            assert ntfy_res.status_code == 200
            ntfy_topic = ntfy_res.json()["data"]["topic"]
            assert ntfy_topic.startswith("cb-")

            # 3. Pair Telegram ($0 bot push)
            tg_start = client.post("/api/v1/push/telegram/link-start", headers={"Authorization": f"Bearer {stu_tok}"})
            assert tg_start.status_code == 200
            start_url = tg_start.json()["data"]["startUrl"]
            fake_httpx_get.expected_token = start_url.split("start=")[1]
            tg_ver = client.post("/api/v1/push/telegram/verify", headers={"Authorization": f"Bearer {stu_tok}"})
            assert tg_ver.status_code == 200
            assert tg_ver.json()["data"]["connected"] is True

            # 4. Inspect free channels overview
            ch = client.get("/api/v1/push/free-channels", headers={"Authorization": f"Bearer {stu_tok}"})
            assert ch.status_code == 200
            ch_data = ch.json()["data"]
            assert ch_data["ntfy"]["connected"] is True
            assert ch_data["telegram"]["connected"] is True
            assert ch_data["whatsapp"]["canteenNumber"] == "919876500000"
            assert "wa.me/919876500000" in ch_data["whatsapp"]["canteenChatUrl"]

            # 5. Send explicit test alert -> hits ntfy, telegram, whatsapp cloud, android_sms_gate
            test_res = client.post("/api/v1/users/me/notifications/test", headers={"Authorization": f"Bearer {stu_tok}"})
            assert test_res.status_code == 200
            delivery = test_res.json()["data"]["delivery"]
            assert delivery["ntfySent"] is True
            assert delivery["telegramSent"] is True
            assert delivery["whatsappSent"] is True
            assert delivery["smsSent"] is True

            ntfy_calls = [p for p in posted if ntfy_topic in p["url"]]
            tg_calls = [p for p in posted if "api.telegram.org" in p["url"] and p.get("json", {}).get("chat_id") == "99887766"]
            wa_calls = [p for p in posted if "graph.facebook.com" in p["url"]]
            sms_calls = [p for p in posted if "sms-gate.example.test" in p["url"]]
            assert ntfy_calls
            assert tg_calls
            assert wa_calls[-1]["json"]["to"] == f"91{raw_phone10}"
            assert sms_calls[-1]["json"]["phoneNumbers"] == [f"+91{raw_phone10}"]
            assert sms_calls[-1]["auth"] == ("canteen", "simpass")

            # 6. Place order & test Staff 1-tap free WhatsApp/SMS links + audit log
            order_res = client.post(
                "/api/v1/orders",
                headers={"Authorization": f"Bearer {stu_tok}"},
                json={"counterId": ctr_id, "fromCart": False, "payWithWallet": True, "items": [{"menuItemId": prod_id, "quantity": 1}]},
            )
            assert order_res.status_code == 201
            order_id = order_res.json()["data"]["order"]["id"]
            assert client.post(f"/api/v1/staff/orders/{order_id}/accept", headers={"Authorization": f"Bearer {stf_tok}"}, json={}).status_code == 200
            assert client.post(f"/api/v1/staff/orders/{order_id}/ready", headers={"Authorization": f"Bearer {stf_tok}"}, json={}).status_code == 200

            links = client.get(
                f"/api/v1/staff/orders/{order_id}/free-notify-links",
                headers={"Authorization": f"Bearer {stf_tok}"},
            )
            assert links.status_code == 200
            link_data = links.json()["data"]
            assert link_data["phonePresent"] is True
            assert link_data["whatsappUrl"].startswith(f"https://wa.me/91{raw_phone10}?text=")
            assert link_data["smsUrl"].startswith(f"sms:+91{raw_phone10}?body=")
            assert "READY for pickup" in link_data["messageText"]

            # Student cannot invoke staff notify-links endpoint
            assert (
                client.get(
                    f"/api/v1/staff/orders/{order_id}/free-notify-links",
                    headers={"Authorization": f"Bearer {stu_tok}"},
                ).status_code
                == 403
            )

            with SessionLocal() as db:
                audit_row = (
                    db.query(AuditEvent)
                    .filter(AuditEvent.action == "staff.free_notify_links", AuditEvent.entity_id == str(order_id))
                    .first()
                )
                assert audit_row is not None

            # 7. Disconnect ntfy & Telegram cleanly
            assert client.delete("/api/v1/push/ntfy", headers={"Authorization": f"Bearer {stu_tok}"}).status_code == 200
            assert client.delete("/api/v1/push/telegram", headers={"Authorization": f"Bearer {stu_tok}"}).status_code == 200
            with SessionLocal() as db:
                pref = db.query(UserNotificationPreference).filter(UserNotificationPreference.user_id == stu_id).first()
                assert pref.ntfy_topic is None and pref.telegram_chat_id is None
    finally:
        get_settings.cache_clear()
