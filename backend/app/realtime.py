"""Real-time WebSocket hub, durable event outbox dispatcher and Web Push sender."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import re
from threading import Lock
from typing import Any

from fastapi import WebSocket
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    DevicePushToken,
    OutboxEvent,
    PushSubscription,
    User,
    UserCounterAssignment,
    UserNotificationPreference,
    UserRole,
)

logger = logging.getLogger("ace_canteen.realtime")


@dataclass
class ConnectedClient:
    websocket: WebSocket
    user_id: int | None = None
    role: str = "public"
    counter_ids: set[int] = field(default_factory=set)


class RealtimeHub:
    """In-process WebSocket pub/sub hub backed by the database outbox."""

    def __init__(self) -> None:
        self._clients: list[ConnectedClient] = []
        self._lock = Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, client: ConnectedClient) -> None:
        await client.websocket.accept()
        with self._lock:
            self._clients.append(client)

    def disconnect(self, websocket: WebSocket) -> None:
        with self._lock:
            self._clients = [c for c in self._clients if c.websocket is not websocket]

    def update_client(self, client: ConnectedClient) -> None:
        with self._lock:
            self._clients = [c for c in self._clients if c.websocket is not client.websocket]
            self._clients.append(client)

    async def broadcast_event(
        self,
        topic: str,
        payload: dict[str, Any],
        *,
        counter_id: int | None = None,
        target_user_id: int | None = None,
    ) -> int:
        envelope = {
            "topic": topic,
            "counterId": counter_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": payload,
        }
        with self._lock:
            snapshot = list(self._clients)
        delivered = 0
        stale: list[WebSocket] = []
        for client in snapshot:
            if not self._authorized_for_event(client, topic, counter_id, target_user_id):
                continue
            try:
                await client.websocket.send_json(envelope)
                delivered += 1
            except Exception:
                stale.append(client.websocket)
        for ws in stale:
            self.disconnect(ws)
        return delivered

    @staticmethod
    def _authorized_for_event(
        client: ConnectedClient,
        topic: str,
        counter_id: int | None,
        target_user_id: int | None,
    ) -> bool:
        if topic == "queue.public":
            return True
        if client.user_id is None:
            return False
        if client.role == UserRole.ADMIN.value:
            if target_user_id is not None and topic.startswith("notification."):
                return client.user_id == target_user_id
            return True
        if client.role == UserRole.STAFF.value:
            if target_user_id is not None and client.user_id == target_user_id:
                return True
            if topic.startswith(("kitchen.", "order.", "inventory.")):
                if not client.counter_ids or counter_id is None:
                    return True
                return counter_id in client.counter_ids
            return False
        # Student: strictly own user-targeted events only.
        return target_user_id is not None and client.user_id == target_user_id

    def notify_flush(self) -> None:
        """Schedule asynchronous outbox delivery from sync or async callers."""
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(flush_outbox_async(), loop)
            except Exception:
                logger.debug("Could not schedule async outbox flush", exc_info=True)


hub = RealtimeHub()


def stage_outbox_event(
    db: Session,
    topic: str,
    payload: dict[str, Any],
    *,
    counter_id: int | None = None,
    target_user_id: int | None = None,
) -> OutboxEvent:
    """Stage an event in the current DB transaction so notifications never outrun commits."""
    row = OutboxEvent(
        topic=topic[:60],
        counter_id=counter_id,
        target_user_id=target_user_id,
        payload_json=json.dumps(payload, separators=(",", ":"), default=str),
    )
    db.add(row)
    return row


def _user_pref(db: Session, user_id: int) -> UserNotificationPreference | None:
    return db.scalar(select(UserNotificationPreference).where(UserNotificationPreference.user_id == user_id))


def send_web_push_to_user(db: Session, user_id: int, title: str, body: str, data: dict[str, Any] | None = None) -> int:
    """Deliver Web Push notifications when VAPID credentials are configured."""
    cfg = get_settings()
    if not (cfg.vapid_public_key and cfg.vapid_private_key):
        return 0
    pref = _user_pref(db, user_id)
    if pref is not None and not pref.web_push_enabled:
        return 0
    subs = db.scalars(select(PushSubscription).where(PushSubscription.user_id == user_id)).all()
    if not subs:
        return 0
    try:
        from pywebpush import WebPushException, webpush
    except Exception:
        return 0
    sent = 0
    payload_data = dict(data or {})
    target_url = str(payload_data.get("url") or "/customer/#my-orders-section")
    notif_tag = str(payload_data.get("tag") or "canteen-order-update")
    message = json.dumps({
        "title": title,
        "body": body,
        "url": target_url,
        "tag": notif_tag,
        "data": payload_data,
    })
    for sub in subs:
        try:
            webpush(
                subscription_info={"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
                data=message,
                vapid_private_key=cfg.vapid_private_key,
                vapid_claims={"sub": cfg.vapid_subject},
                timeout=5,
            )
            sent += 1
        except WebPushException as exc:
            resp = getattr(exc, "response", None)
            if resp is not None and getattr(resp, "status_code", None) in (404, 410):
                db.delete(sub)
        except Exception:
            logger.debug("Web push delivery failed", exc_info=True)
    return sent


def send_expo_push_to_user(db: Session, user_id: int, title: str, body: str, data: dict[str, Any] | None = None) -> int:
    """Deliver closed-app mobile notifications through the Expo Push API when device tokens exist."""
    cfg = get_settings()
    if not cfg.expo_push_enabled:
        return 0
    pref = _user_pref(db, user_id)
    if pref is not None and not pref.web_push_enabled:
        return 0
    tokens = db.scalars(select(DevicePushToken).where(DevicePushToken.user_id == user_id)).all()
    if not tokens:
        return 0
    try:
        import httpx
    except Exception:
        return 0

    messages = [
        {
            "to": row.expo_push_token,
            "sound": "default",
            "title": title[:120],
            "body": body[:240],
            "channelId": "orders",
            "data": data or {},
        }
        for row in tokens
    ]
    sent = 0
    try:
        resp = httpx.post(
            "https://exp.host/--/api/v2/push/send",
            json=messages,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=3.0,
        )
        if resp.status_code == 200:
            tickets = (resp.json() or {}).get("data") or []
            if isinstance(tickets, list):
                for row, ticket in zip(tokens, tickets):
                    status_str = (ticket or {}).get("status")
                    err_code = ((ticket or {}).get("details") or {}).get("error")
                    if status_str == "ok":
                        sent += 1
                    elif err_code == "DeviceNotRegistered":
                        db.delete(row)
    except Exception:
        logger.debug("Expo push delivery failed", exc_info=True)
    return sent


def normalize_e164_phone(raw_phone: str | None, default_country_code: str = "91") -> tuple[str, str]:
    """Normalize a student/canteen phone number to (E.164 with '+', digits-only)."""
    raw = (raw_phone or "").strip()
    if not raw:
        return ("", "")
    cc = re.sub(r"\D", "", default_country_code or "91") or "91"
    stripped = re.sub(r"[\s\-()]", "", raw)
    if stripped.startswith("+"):
        digits = re.sub(r"\D", "", stripped[1:])
    elif stripped.startswith("00"):
        digits = re.sub(r"\D", "", stripped[2:])
    else:
        digits = re.sub(r"\D", "", stripped)
        if len(digits) == 10:
            digits = cc + digits
        elif len(digits) == 11 and digits.startswith("0"):
            digits = cc + digits[1:]
    if len(digits) < 8 or len(digits) > 15:
        return ("", "")
    return (f"+{digits}", digits)


def _post_json(url: str, *, json_body: dict[str, Any] | list[Any] | None = None, content: bytes | None = None, headers: dict[str, str] | None = None, auth: tuple[str, str] | None = None) -> Any | None:
    try:
        import httpx
        kwargs: dict[str, Any] = {"headers": headers or {}, "timeout": 3.0}
        if json_body is not None:
            kwargs["json"] = json_body
        if content is not None:
            kwargs["content"] = content
        if auth is not None:
            kwargs["auth"] = auth
        return httpx.post(url, **kwargs)
    except Exception:
        logger.debug("External HTTP notification post failed", exc_info=True)
        return None


def send_sms_to_user(
    db: Session,
    user_id: int,
    title: str,
    body: str,
    *,
    notification_type: str = "order_status",
) -> bool:
    """Optional SMS / Android SMS Gateway webhook bridge (fires when configured & user opted in)."""
    cfg = get_settings()
    webhook_url = (cfg.sms_webhook_url or "").strip()
    if not cfg.sms_enabled or not webhook_url:
        return False
    if notification_type != "order_status":
        return False
    if cfg.environment.lower() in {"prod", "production"} and not webhook_url.startswith("https://"):
        return False
    if not webhook_url.startswith(("https://", "http://")):
        return False

    pref = _user_pref(db, user_id)
    if pref is None or not pref.sms_enabled:
        return False
    user = db.get(User, user_id)
    e164, _ = normalize_e164_phone(user.phone if user else None, cfg.canteen_default_country_code)
    if not e164:
        return False

    text = f"{title}: {body}"[:320]
    preset = (cfg.sms_webhook_preset or "generic").strip().lower()
    payload: dict[str, Any]
    if preset == "android_sms_gate":
        payload = {"message": text, "textMessage": {"text": text}, "phoneNumbers": [e164]}
    else:
        payload = {"to": e164, "message": text, "eventType": notification_type}

    headers = {"Content-Type": "application/json"}
    if cfg.sms_webhook_bearer_token.strip():
        headers["Authorization"] = f"Bearer {cfg.sms_webhook_bearer_token.strip()}"
    auth = (
        (cfg.sms_webhook_username.strip(), cfg.sms_webhook_password)
        if cfg.sms_webhook_username.strip() and not cfg.sms_webhook_bearer_token.strip()
        else None
    )
    resp = _post_json(webhook_url, json_body=payload, headers=headers, auth=auth)
    return bool(resp is not None and 200 <= resp.status_code < 300)


def send_ntfy_push_to_user(
    db: Session,
    user_id: int,
    title: str,
    body: str,
) -> bool:
    """Deliver $0 closed-app phone push notifications through ntfy.sh or self-hosted ntfy."""
    cfg = get_settings()
    base = (cfg.ntfy_base_url or "https://ntfy.sh").strip().rstrip("/")
    if not cfg.ntfy_enabled or not base:
        return False
    if cfg.environment.lower() in {"prod", "production"} and not base.startswith("https://"):
        return False
    if not base.startswith(("https://", "http://")):
        return False
    pref = _user_pref(db, user_id)
    if pref is None or not pref.web_push_enabled or not (pref.ntfy_topic or "").strip():
        return False

    topic = pref.ntfy_topic.strip()
    headers = {
        "Title": title[:120],
        "Priority": "high",
        "Tags": "bell,fork_and_knife",
    }
    if (cfg.public_base_url or "").startswith(("https://", "http://")):
        headers["Click"] = f"{cfg.public_base_url.rstrip('/')}/customer/#my-orders-section"
    resp = _post_json(f"{base}/{topic}", content=body[:500].encode("utf-8"), headers=headers)
    return bool(resp is not None and 200 <= resp.status_code < 300)


def send_telegram_to_user(
    db: Session,
    user_id: int,
    title: str,
    body: str,
) -> bool:
    """Deliver $0 unlimited iOS & Android alerts via Telegram Bot API."""
    cfg = get_settings()
    token = (cfg.telegram_bot_token or "").strip()
    if not cfg.telegram_enabled or not token:
        return False
    pref = _user_pref(db, user_id)
    if pref is None or not pref.web_push_enabled or not (pref.telegram_chat_id or "").strip():
        return False

    resp = _post_json(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json_body={
            "chat_id": pref.telegram_chat_id.strip(),
            "text": f"{title}\n{body}"[:1000],
        },
        headers={"Content-Type": "application/json"},
    )
    return bool(resp is not None and 200 <= resp.status_code < 300)


def send_whatsapp_cloud_to_user(
    db: Session,
    user_id: int,
    title: str,
    body: str,
    *,
    notification_type: str = "order_status",
) -> bool:
    """Optional official Meta WhatsApp Cloud API adapter (free service replies within 24h window)."""
    cfg = get_settings()
    phone_id = (cfg.whatsapp_phone_number_id or "").strip()
    token = (cfg.whatsapp_access_token or "").strip()
    if not cfg.whatsapp_cloud_enabled or not phone_id or not token:
        return False
    if notification_type != "order_status":
        return False
    pref = _user_pref(db, user_id)
    if pref is None or not pref.whatsapp_enabled:
        return False
    user = db.get(User, user_id)
    _, digits = normalize_e164_phone(user.phone if user else None, cfg.canteen_default_country_code)
    if not digits:
        return False

    ver = (cfg.whatsapp_graph_version or "v21.0").strip() or "v21.0"
    resp = _post_json(
        f"https://graph.facebook.com/{ver}/{phone_id}/messages",
        json_body={
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": digits,
            "type": "text",
            "text": {"preview_url": False, "body": f"{title}\n{body}"[:1024]},
        },
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    return bool(resp is not None and 200 <= resp.status_code < 300)


def dispatch_external_alerts(
    db: Session,
    user_id: int,
    title: str,
    body: str,
    *,
    notification_type: str = "order_status",
    payload_data: dict[str, Any] | None = None,
) -> dict[str, int | bool]:
    """Fan out notifications across all configured free mobile, messaging, and SMS channels."""
    return {
        "webPushSent": send_web_push_to_user(db, user_id, title, body, payload_data),
        "expoPushSent": send_expo_push_to_user(db, user_id, title, body, payload_data),
        "ntfySent": send_ntfy_push_to_user(db, user_id, title, body),
        "telegramSent": send_telegram_to_user(db, user_id, title, body),
        "whatsappSent": send_whatsapp_cloud_to_user(db, user_id, title, body, notification_type=notification_type),
        "smsSent": send_sms_to_user(db, user_id, title, body, notification_type=notification_type),
    }


async def flush_outbox_async(limit: int = 100) -> int:
    """Deliver pending outbox rows to connected WebSockets and mark them delivered."""
    processed = 0
    with SessionLocal() as db:
        pending = db.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.delivered_at.is_(None))
            .order_by(OutboxEvent.id.asc())
            .limit(limit)
        ).all()
        if not pending:
            return 0
        now = datetime.now(timezone.utc)
        for ev in pending:
            try:
                payload = json.loads(ev.payload_json or "{}")
            except ValueError:
                payload = {}
            await hub.broadcast_event(
                ev.topic,
                payload,
                counter_id=ev.counter_id,
                target_user_id=ev.target_user_id,
            )
            ev.delivered_at = now
            processed += 1
        db.commit()
    return processed


def build_ws_client_for_user(db: Session, user: User | None, websocket: WebSocket) -> ConnectedClient:
    if user is None:
        return ConnectedClient(websocket=websocket, user_id=None, role="public")
    counter_ids: set[int] = set()
    if user.role == UserRole.STAFF:
        rows = db.scalars(select(UserCounterAssignment.counter_id).where(UserCounterAssignment.user_id == user.id)).all()
        counter_ids = {int(cid) for cid in rows}
    return ConnectedClient(
        websocket=websocket,
        user_id=user.id,
        role=user.role.value,
        counter_ids=counter_ids,
    )
