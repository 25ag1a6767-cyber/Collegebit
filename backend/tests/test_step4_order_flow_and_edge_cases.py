"""Step 4 automated suite: full order lifecycle, rejection stock restore, out-of-stock, concurrency race, QR errors & WS reconnect."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import threading
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
import app.main as main
from app.main import app
from app.models import (
    CanteenCounter,
    Category,
    InventoryMovement,
    InventoryMovementType,
    PaymentIncident,
    PickupToken,
    Product,
    User,
    UserRole,
)
from app.security import create_access_token, hash_opaque_token, hash_password
from app.modules.rewards_wallet import record_wallet_movement


def _mk_user(role: UserRole, *, wallet_paise: int = 0) -> tuple[int, str, str]:
    tag = uuid.uuid4().hex[:8]
    email = f"flow-{role.value}-{tag}@college.edu"
    pw = "OrderFlowPass-2026!"
    with SessionLocal() as db:
        u = User(full_name=f"Flow {role.value}", email=email, password_hash=hash_password(pw), role=role, is_active=True)
        db.add(u)
        db.flush()
        if wallet_paise > 0:
            record_wallet_movement(db, u.id, wallet_paise, "admin_credit", "Step 4 test credit", f"step4-{tag}", actor_user_id=u.id)
        db.commit()
        db.refresh(u)
        return u.id, email, pw


def _auth(user_id: int) -> dict[str, str]:
    with SessionLocal() as db:
        u = db.get(User, user_id)
        return {"Authorization": f"Bearer {create_access_token(u, mfa_verified=True)}"}


def _mk_counter_and_product(stock: int = 10, price_paise: int = 2500) -> tuple[int, int]:
    with SessionLocal() as db:
        counter = db.scalar(select(CanteenCounter).limit(1))
        if counter is None:
            counter = CanteenCounter(name="Flow Counter", location="GF", is_active=True, capacity_per_slot=30)
            db.add(counter)
            db.flush()
        cat = db.scalar(select(Category).limit(1))
        if cat is None:
            cat = Category(name="FlowCat", slug=f"flowcat-{uuid.uuid4().hex[:6]}", item_type="food", is_active=True)
            db.add(cat)
            db.flush()
        p = Product(
            category_id=cat.id,
            counter_id=counter.id,
            name=f"Flow Dosa {uuid.uuid4().hex[:6]}",
            item_type="food",
            price=price_paise / 100,
            price_paise=price_paise,
            stock_quantity=stock,
            is_available=stock > 0,
        )
        db.add(p)
        db.commit()
        db.refresh(p)
        return counter.id, p.id


def test_full_order_lifecycle_register_cart_pickup_receipt(monkeypatch):
    main._login_attempts.clear()
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "canteen_open_days", "0,1,2,3,4,5,6")
    monkeypatch.setattr(get_settings(), "canteen_open_time", "")
    monkeypatch.setattr(get_settings(), "canteen_close_time", "")

    counter_id, prod_id = _mk_counter_and_product(stock=5, price_paise=3000)
    staff_id, _, _ = _mk_user(UserRole.STAFF)
    staff_h = _auth(staff_id)

    tag = uuid.uuid4().hex[:8]
    stu_email = f"student-{tag}@college.edu"
    stu_pw = "StudentOrderFlow-2026!"

    with TestClient(app) as client:
        # 1. Register & login
        reg = client.post("/api/v1/auth/register", json={"fullName": "Step4 Student", "email": stu_email, "password": stu_pw})
        assert reg.status_code == 201, reg.text
        stu_id = reg.json()["data"]["user"]["id"]
        with SessionLocal() as db:
            record_wallet_movement(db, stu_id, 10000, "admin_credit", "Wallet load", f"w-{tag}", actor_user_id=staff_id)
            db.commit()
        login = client.post("/api/v1/auth/login", json={"identifier": stu_email, "password": stu_pw})
        assert login.status_code == 200
        stu_h = {"Authorization": f"Bearer {login.json()['data']['accessToken']}"}

        # 2. Browse menu & add to cart
        menu = client.get("/api/v1/menu/items", params={"search": "Flow Dosa"})
        assert menu.status_code == 200 and any(it["id"] == prod_id for it in menu.json()["data"]["items"])
        cart_put = client.put(f"/api/v1/cart/items/{prod_id}", headers=stu_h, json={"quantity": 2})
        assert cart_put.status_code == 200, cart_put.text

        # 3. Place paid wallet order from cart
        placed = client.post("/api/v1/orders", headers=stu_h, json={"counterId": counter_id, "fromCart": True, "payWithWallet": True})
        assert placed.status_code == 201, placed.text
        order = placed.json()["data"]["order"]
        oid = order["id"]
        assert order["paymentStatus"] == "SUCCESS"

        # 4. Staff accepts -> prepares -> marks ready
        assert client.post(f"/api/v1/staff/orders/{oid}/accept", headers=staff_h, json={}).status_code == 200
        assert client.post(f"/api/v1/staff/orders/{oid}/start", headers=staff_h, json={}).status_code == 200
        assert client.post(f"/api/v1/staff/orders/{oid}/ready", headers=staff_h, json={}).status_code == 200

        # 5. Student mints QR -> staff inspects & completes pickup handover
        mint = client.post(f"/api/v1/orders/{oid}/pickup-token", headers=stu_h)
        assert mint.status_code == 200, mint.text
        pk_tok = mint.json()["data"]["pickupToken"]

        inspected = client.post("/api/v1/pickups/inspect", headers=staff_h, json={"pickupToken": pk_tok})
        assert inspected.status_code == 200 and inspected.json()["data"]["eligibleForCollection"] is True

        picked = client.post("/api/v1/pickups/verify", headers=staff_h, json={"pickupToken": pk_tok})
        assert picked.status_code == 200
        assert picked.json()["data"]["order"]["status"] == "COLLECTED"

        # 6. Digital JSON & PDF receipt
        rcpt_json = client.get(f"/api/v1/orders/{oid}/receipt?format=json", headers=stu_h)
        assert rcpt_json.status_code == 200
        assert rcpt_json.json()["data"]["orderId"] == oid
        assert rcpt_json.json()["data"]["paymentStatus"] == "PAID"

        rcpt_pdf = client.get(f"/api/v1/orders/{oid}/receipt?format=pdf", headers=stu_h)
        assert rcpt_pdf.status_code == 200
        assert rcpt_pdf.content.startswith(b"%PDF-")


def test_staff_reject_restores_stock_and_flags_incident(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "canteen_open_days", "0,1,2,3,4,5,6")
    monkeypatch.setattr(get_settings(), "canteen_open_time", "")
    monkeypatch.setattr(get_settings(), "canteen_close_time", "")

    counter_id, prod_id = _mk_counter_and_product(stock=2, price_paise=2500)
    stu_id, _, _ = _mk_user(UserRole.STUDENT, wallet_paise=10000)
    staff_id, _, _ = _mk_user(UserRole.STAFF)
    stu_h, staff_h = _auth(stu_id), _auth(staff_id)

    with TestClient(app) as client:
        placed = client.post(
            "/api/v1/orders",
            headers=stu_h,
            json={"counterId": counter_id, "items": [{"menuItemId": prod_id, "quantity": 2}], "payWithWallet": True},
        )
        assert placed.status_code == 201, placed.text
        oid = placed.json()["data"]["order"]["id"]

        with SessionLocal() as db:
            p_after = db.get(Product, prod_id)
            assert p_after.stock_quantity == 0
            assert p_after.is_available is False

        rej = client.post(f"/api/v1/staff/orders/{oid}/reject", headers=staff_h, json={"reason": "Burner maintenance issue"})
        assert rej.status_code == 200, rej.text
        assert rej.json()["data"]["order"]["rawStatus"] == "cancelled"
        assert rej.json()["data"]["incidentFlagged"] is True

        with SessionLocal() as db:
            p_restored = db.get(Product, prod_id)
            assert p_restored.stock_quantity == 2
            assert p_restored.is_available is True
            rel = db.scalar(
                select(InventoryMovement).where(
                    InventoryMovement.order_id == oid,
                    InventoryMovement.movement_type == InventoryMovementType.RELEASE,
                )
            )
            inc = db.scalar(select(PaymentIncident).where(PaymentIncident.order_id == oid))
            assert rel is not None and rel.quantity_delta == 2
            assert inc is not None and inc.status == "open"


def test_out_of_stock_and_simultaneous_last_item_race_and_qr_errors(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "canteen_open_days", "0,1,2,3,4,5,6")
    monkeypatch.setattr(get_settings(), "canteen_open_time", "")
    monkeypatch.setattr(get_settings(), "canteen_close_time", "")

    counter_id, zero_prod_id = _mk_counter_and_product(stock=0, price_paise=2000)
    _, last_prod_id = _mk_counter_and_product(stock=1, price_paise=2000)

    stu1, _, _ = _mk_user(UserRole.STUDENT, wallet_paise=10000)
    stu2, _, _ = _mk_user(UserRole.STUDENT, wallet_paise=10000)
    staff_id, _, _ = _mk_user(UserRole.STAFF)
    h1, h2, h_staff = _auth(stu1), _auth(stu2), _auth(staff_id)

    with TestClient(app) as client:
        # 1. Explicit out-of-stock fails cleanly with 409 without charging wallet
        w_before = client.get("/api/v1/wallet/me", headers=h1).json()["data"]["balancePaise"]
        oos = client.post(
            "/api/v1/orders",
            headers=h1,
            json={"counterId": counter_id, "items": [{"menuItemId": zero_prod_id, "quantity": 1}], "payWithWallet": True},
        )
        assert oos.status_code == 409
        w_after = client.get("/api/v1/wallet/me", headers=h1).json()["data"]["balancePaise"]
        assert w_after == w_before

        # 2. Simultaneous race for last 1 item: strictly 1 wins, stock never goes negative
        barrier = threading.Barrier(2)

        def _race_order(hdr):
            with TestClient(app) as race_client:
                barrier.wait(timeout=5)
                return race_client.post(
                    "/api/v1/orders",
                    headers=hdr,
                    json={"counterId": counter_id, "items": [{"menuItemId": last_prod_id, "quantity": 1}], "payWithWallet": True},
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            r1, r2 = pool.map(_race_order, [h1, h2])

        statuses = sorted([r1.status_code, r2.status_code])
        assert statuses == [201, 409], (r1.status_code, r1.text, r2.status_code, r2.text)

        with SessionLocal() as db:
            final_prod = db.get(Product, last_prod_id)
            assert final_prod.stock_quantity == 0

        # Winner prepares & verifies QR expiry / reuse + WS reconnect
        winner_res = r1 if r1.status_code == 201 else r2
        winner_hdr = h1 if r1.status_code == 201 else h2
        oid = winner_res.json()["data"]["order"]["id"]

        assert client.post(f"/api/v1/staff/orders/{oid}/accept", headers=h_staff, json={}).status_code == 200
        assert client.post(f"/api/v1/staff/orders/{oid}/ready", headers=h_staff, json={}).status_code == 200

        tok_exp = client.post(f"/api/v1/orders/{oid}/pickup-token", headers=winner_hdr).json()["data"]["pickupToken"]
        with SessionLocal() as db:
            pt = db.scalar(select(PickupToken).where(PickupToken.token_hash == hash_opaque_token(tok_exp)))
            pt.expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
            db.commit()
        assert client.post("/api/v1/pickups/verify", headers=h_staff, json={"pickupToken": tok_exp}).status_code == 410

        tok_ok = client.post(f"/api/v1/orders/{oid}/pickup-token", headers=winner_hdr).json()["data"]["pickupToken"]
        assert client.post("/api/v1/pickups/verify", headers=h_staff, json={"pickupToken": tok_ok}).status_code == 200
        assert client.post("/api/v1/pickups/verify", headers=h_staff, json={"pickupToken": tok_ok}).status_code == 409


def _recv_topic(ws, target_topic: str, max_frames: int = 20) -> dict:
    for _ in range(max_frames):
        msg = ws.receive_json()
        if msg.get("topic") == target_topic:
            return msg
    raise AssertionError(f"Did not receive WebSocket topic {target_topic!r} within {max_frames} frames")


def test_websocket_reconnect_delivers_events():
    stu_id, _, _ = _mk_user(UserRole.STUDENT)
    stu_tok = _auth(stu_id)["Authorization"].split(" ", 1)[1]

    with TestClient(app) as client:
        # First connection opens, authenticates by message frame, then disconnects
        with client.websocket_connect("/api/v1/ws") as ws1:
            assert ws1.receive_json()["topic"] == "connected"
            ws1.send_json({"type": "auth", "token": stu_tok})
            assert _recv_topic(ws1, "authenticated")["userId"] == stu_id

        # Second (reconnected) session receives live ping/pong and re-authenticates cleanly
        with client.websocket_connect("/api/v1/ws") as ws2:
            assert ws2.receive_json()["topic"] == "connected"
            ws2.send_json({"type": "auth", "token": stu_tok})
            auth_msg = _recv_topic(ws2, "authenticated")
            assert auth_msg["userId"] == stu_id
            ws2.send_json({"type": "ping"})
            assert _recv_topic(ws2, "pong")["topic"] == "pong"
