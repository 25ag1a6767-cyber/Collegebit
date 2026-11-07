"""Real-time WebSocket hub, durable event outbox dispatcher and Web Push sender."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
from threading import Lock
from typing import Any

from fastapi import WebSocket
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import OutboxEvent, PushSubscription, User, UserCounterAssignment, UserRole

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

def send_web_push_to_user(db: Session, user_id: int, title: str, body: str, data: dict[str, Any] | None = None) -> int:
    """Deliver Web Push notifications when VAPID credentials are configured."""
    cfg = get_settings()
    if not (cfg.vapid_public_key and cfg.vapid_private_key):
        return 0
    subs = db.scalars(select(PushSubscription).where(PushSubscription.user_id == user_id)).all()
    if not subs:
        return 0
    try:
        from pywebpush import WebPushException, webpush
    except Exception:
        return 0
    sent = 0
    message = json.dumps({"title": title, "body": body, "data": data or {}})
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
