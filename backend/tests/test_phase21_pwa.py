from fastapi.testclient import TestClient
from app.main import app


def test_manifest_is_scoped_to_customer_app():
    with TestClient(app) as client:
        response = client.get("/manifest.webmanifest")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/manifest+json")
        payload = response.json()
        assert payload["id"] == "/customer/"
        assert payload["lang"] == "en"
        assert payload["name"] == "CollegeBits Customer"
        assert payload["short_name"] == "CollegeBits"
        assert payload["start_url"] == "/customer/"
        assert payload["scope"] == "/customer/"
        assert payload["display"] == "standalone"
        assert payload["orientation"] == "portrait"
        assert {icon["sizes"] for icon in payload["icons"]} == {"192x192", "512x512"}
        assert any(icon["sizes"] == "512x512" and icon.get("purpose") == "maskable" for icon in payload["icons"])
        assert client.get("/assets/icon-maskable-512.png").status_code == 200


def test_service_worker_is_customer_scoped_and_does_not_cache_api():
    with TestClient(app) as client:
        response = client.get("/sw.js")
        assert response.status_code == 200
        assert response.headers.get("service-worker-allowed") == "/customer/"
        assert 'url.pathname.startsWith("/api/")' in response.text
        assert 'url.pathname !== "/customer"' in response.text
        assert 'url.pathname !== "/customer/"' in response.text


def test_ios_pwa_meta_tags_opaque_apple_icon_splashes_and_safe_areas():
    import struct
    with TestClient(app) as client:
        html = client.get("/customer/").text
        assert "viewport-fit=cover" in html
        assert '<meta name="apple-mobile-web-app-capable" content="yes">' in html
        assert '<meta name="mobile-web-app-capable" content="yes">' in html
        assert '<meta name="apple-mobile-web-app-status-bar-style" content="default">' in html
        assert '<meta name="apple-mobile-web-app-title" content="CollegeBits">' in html
        assert '<link rel="apple-touch-icon" sizes="180x180" href="/assets/apple-touch-icon.png">' in html
        assert 'rel="apple-touch-startup-image"' in html

        icon = client.get("/assets/apple-touch-icon.png")
        assert icon.status_code == 200
        w, h, _depth, ctype = struct.unpack(">IIBB", icon.content[16:26])
        assert (w, h) == (180, 180)
        assert ctype == 2  # Opaque RGB (no alpha transparency)

        splash = client.get("/assets/splash/apple-splash-1179-2556.png")
        assert splash.status_code == 200

        css = client.get("/css/style.css").text + client.get("/css/v1-platform.css").text
        assert "env(safe-area-inset-top" in css
        assert "env(safe-area-inset-bottom" in css
        assert "overscroll-behavior-y: contain" in css
        assert "user-select: none" in css
        assert "min-height: 44px" in css
        assert "font-size: 16px !important" in css
        assert 'id="mobile-bottom-nav"' in html
        assert 'id="mobile-cart-badge"' in html
        assert "Add to Home Screen" in html
        assert 'id="pwa-update-banner"' in html
        assert 'id="pwa-offline-banner"' in html
        assert 'id="v1-live-toast"' in html


def test_pwa_step5_offline_page_update_flow_and_no_cache_rules():
    with TestClient(app) as client:
        offline = client.get("/offline.html")
        assert offline.status_code == 200
        assert "You are offline" in offline.text
        assert "<script" not in offline.text.lower()

        sw = client.get("/sw.js")
        assert sw.status_code == 200
        assert "no-cache" in sw.headers["cache-control"]
        assert "no-store" in sw.headers["cache-control"]
        assert '"/offline.html"' in sw.text
        assert '"/assets/icon-192.png"' in sw.text
        assert '"/assets/icon-maskable-512.png"' in sw.text
        assert '"/assets/apple-touch-icon.png"' in sw.text
        assert "SKIP_WAITING" in sw.text
        assert 'url.pathname.startsWith("/pickup/")' in sw.text
        assert 'url.pathname.startsWith("/qr/")' in sw.text
        assert 'request.headers.get("authorization")' in sw.text

        js = client.get("/js/app.js").text + client.get("/js/v1-platform.js").text
        assert "updatefound" in js
        assert "SKIP_WAITING" in js
        assert "const quiet = 4;" in js


def test_pwa_step6_web_push_subscription_and_order_accepted_ready_delivery(monkeypatch):
    import json
    import sys
    import types
    import uuid
    from app.database import SessionLocal
    from app.models import CanteenCounter, Category, Product, PushSubscription, User, UserRole
    from app.modules.rewards_wallet import record_wallet_movement
    from app.security import create_access_token, hash_password

    delivered_pushes: list[dict] = []

    class FakeWebPushException(Exception):
        response = None

    def fake_webpush(**kwargs):
        delivered_pushes.append(kwargs)

    fake_mod = types.ModuleType("pywebpush")
    fake_mod.WebPushException = FakeWebPushException
    fake_mod.webpush = fake_webpush
    monkeypatch.setitem(sys.modules, "pywebpush", fake_mod)
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "test-vapid-pub-key")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "test-vapid-priv-key")
    from app.config import get_settings
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
                    full_name="Push Student",
                    email=f"push-stu-{uid}@college.edu",
                    password_hash=hash_password("StudentPass#12345"),
                    role=UserRole.STUDENT,
                )
                staff = User(
                    full_name="Push Staff",
                    email=f"push-stf-{uid}@college.edu",
                    password_hash=hash_password("StaffPass#12345"),
                    role=UserRole.STAFF,
                )
                counter = db.query(CanteenCounter).first() or CanteenCounter(
                    name=f"PushCounter-{uid}", location="GF", is_active=True, capacity_per_slot=30
                )
                if not counter.id:
                    db.add(counter)
                    db.flush()
                cat = db.query(Category).first() or Category(
                    name=f"PushCat-{uid}", slug=f"pushcat-{uid}", item_type="food", is_active=True
                )
                if not cat.id:
                    db.add(cat)
                    db.flush()
                db.add_all([student, staff])
                db.flush()
                record_wallet_movement(db, student.id, 50000, "admin_credit", "Push test wallet credit", f"push-{uid}", actor_user_id=staff.id)
                prod = Product(
                    name=f"PushPoha-{uid}",
                    description="Warm breakfast item",
                    category_id=cat.id,
                    counter_id=counter.id,
                    price=30,
                    price_paise=3000,
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

            # Subscribe user via API and verify persistence in push_subscriptions table
            sub_res = client.post(
                "/api/v1/push/subscribe",
                headers={"Authorization": f"Bearer {stu_tok}"},
                json={"endpoint": f"https://push.example/sub/{uid}", "keys": {"p256dh": "k1", "auth": "k2"}},
            )
            assert sub_res.status_code == 201
            with SessionLocal() as db:
                row = db.query(PushSubscription).filter_by(user_id=stu_id, endpoint=f"https://push.example/sub/{uid}").first()
                assert row is not None

            # Place order and trigger accepted + ready notifications
            order_res = client.post(
                "/api/v1/orders",
                headers={"Authorization": f"Bearer {stu_tok}"},
                json={"counterId": ctr_id, "fromCart": False, "payWithWallet": True, "items": [{"menuItemId": prod_id, "quantity": 1}]},
            )
            assert order_res.status_code == 201, order_res.text
            assert order_res.headers.get("cache-control") == "no-store"
            order_id = order_res.json()["data"]["order"]["id"]

            acc = client.post(f"/api/v1/staff/orders/{order_id}/accept", headers={"Authorization": f"Bearer {stf_tok}"}, json={})
            assert acc.status_code == 200, acc.text
            rdy = client.post(f"/api/v1/staff/orders/{order_id}/ready", headers={"Authorization": f"Bearer {stf_tok}"}, json={})
            assert rdy.status_code == 200, rdy.text

            bodies = [json.loads(p["data"]) for p in delivered_pushes]
            titles = [b["title"] for b in bodies]
            assert any("accepted" in t.lower() for t in titles)
            assert any("ready for pickup" in t.lower() for t in titles)
            assert all(b["url"] == "/customer/#my-orders-section" for b in bodies)

            # Orders and QR mint endpoints never allow caching
            qr = client.post(f"/api/v1/orders/{order_id}/pickup-token", headers={"Authorization": f"Bearer {stu_tok}"})
            assert qr.status_code == 200
            assert qr.headers.get("cache-control") == "no-store"

            # Unsubscribe cleans DB record
            unsub = client.delete(
                "/api/v1/push/subscribe",
                params={"endpoint": f"https://push.example/sub/{uid}"},
                headers={"Authorization": f"Bearer {stu_tok}"},
            )
            assert unsub.status_code == 200
            assert unsub.json()["data"]["subscribed"] is False
    finally:
        get_settings.cache_clear()


def test_pwa_step8_step9_camera_headers_and_bright_qr_css():
    with TestClient(app) as client:
        for route in ("/staff", "/staff/", "/customer", "/customer/"):
            res = client.get(route)
            assert res.status_code == 200
            assert "camera=(self)" in res.headers.get("permissions-policy", "")

        # Verify large bright QR wrapper rules are loaded cleanly
        css = client.get("/css/v1-platform.css").text
        assert ".v1-qr-modal-stage" in css
        assert "min(232px, 68vw)" in css
