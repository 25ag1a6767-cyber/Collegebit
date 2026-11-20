from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import (
    Category,
    Order,
    OrderItem,
    OrderStatus,
    PaymentStatus,
    Product,
    User,
    UserRole,
)
from app.security import create_access_token, hash_password

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_user(role: UserRole, prefix: str) -> tuple[int, str, str]:
    email = f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"
    with SessionLocal() as db:
        u = User(
            full_name=f"{prefix.title()} User",
            email=email,
            password_hash=hash_password("RegressionPass-2026!"),
            role=role,
            is_active=True,
        )
        db.add(u)
        db.commit()
        db.refresh(u)
        token = create_access_token(u)
        return u.id, email, token


def test_lowercase_legacy_enum_rows_do_not_crash_endpoints():
    """Regression: legacy lowercase enum strings ('paid', 'cancelled', 'student') must decode cleanly across portals."""
    stu_id, _, stu_tok = _make_user(UserRole.STUDENT, "enum-stu")
    _, _, staff_tok = _make_user(UserRole.STAFF, "enum-stf")
    _, _, adm_tok = _make_user(UserRole.ADMIN, "enum-adm")

    suffix = uuid.uuid4().hex[:6]
    with SessionLocal() as db:
        cat = Category(name=f"Enum Cat {suffix}", slug=f"enum-cat-{suffix}")
        db.add(cat)
        db.flush()
        prod = Product(
            category_id=cat.id,
            name=f"Enum Samosa {suffix}",
            price=25,
            price_paise=2500,
            stock_quantity=20,
            low_stock_threshold=3,
            is_available=True,
        )
        db.add(prod)
        db.flush()
        order = Order(
            user_id=stu_id,
            status=OrderStatus.CANCELLED,
            payment_status=PaymentStatus.PAID,
            subtotal=25,
            gateway_order_id=f"gw_enum_{suffix}",
        )
        db.add(order)
        db.flush()
        db.add(
            OrderItem(
                order_id=order.id,
                product_id=prod.id,
                product_name=prod.name,
                unit_price=25,
                quantity=1,
                line_total=25,
            )
        )
        db.commit()
        order_id = order.id

        # Force lowercase values directly in SQL like buggy migration 0023 previously wrote.
        db.execute(
            text("UPDATE orders SET status = 'cancelled', payment_status = 'paid' WHERE id = :oid"),
            {"oid": order_id},
        )
        db.execute(text("UPDATE users SET role = 'student' WHERE id = :uid"), {"uid": stu_id})
        db.commit()

    stu_h = {"Authorization": f"Bearer {stu_tok}"}
    staff_h = {"Authorization": f"Bearer {staff_tok}"}
    adm_h = {"Authorization": f"Bearer {adm_tok}"}

    with TestClient(app) as client:
        for method, path, headers, kwargs in [
            ("GET", "/api/orders/mine", stu_h, {}),
            ("GET", "/api/v1/staff/orders/history", staff_h, {}),
            ("GET", "/api/v1/admin/analytics/dashboard?preset=last_30_days", adm_h, {}),
            ("GET", "/api/v1/admin/reports/preview?reportType=payment_incidents", adm_h, {}),
            ("GET", "/api/v1/admin/reports/preview?reportType=profit_expense", adm_h, {}),
            ("GET", "/api/v1/admin/payments/reconciliation", adm_h, {}),
            ("GET", "/api/v1/admin/orders", adm_h, {}),
            ("POST", "/api/v1/admin/retention/run", adm_h, {"json": {"retentionDays": 4, "dryRun": True}}),
        ]:
            res = client.request(method, path, headers=headers, **kwargs)
            assert res.status_code == 200, f"{method} {path} returned {res.status_code}: {res.text}"


def test_alembic_0028_normalizes_lowercase_enum_rows(tmp_path, monkeypatch):
    """Regression: migration 0028 upgrades legacy lowercase orders/users values to uppercase names."""
    from alembic import command
    from alembic.config import Config

    db_file = tmp_path / "mig_0028.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    get_settings.cache_clear()
    try:
        cfg = Config(str(REPO_ROOT / "backend" / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "backend" / "alembic"))
        command.upgrade(cfg, "0027_free_notification_channels")

        with sqlite3.connect(db_file) as conn:
            conn.execute(
                "INSERT INTO users (full_name, email, password_hash, role, is_active, token_version, created_at) "
                "VALUES ('Row User', 'row0028@example.com', 'x', 'student', 1, 0, '2026-01-01T00:00:00Z')"
            )
            uid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO orders (user_id, status, payment_status, subtotal, created_at, updated_at) "
                "VALUES (?, 'cancelled', 'paid', 25.00, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
                (uid,),
            )
            conn.commit()

        command.upgrade(cfg, "head")

        with sqlite3.connect(db_file) as conn:
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0029_user_allergen_preferences"
            role = conn.execute("SELECT role FROM users WHERE id = ?", (uid,)).fetchone()[0]
            status, pstatus = conn.execute("SELECT status, payment_status FROM orders WHERE user_id = ?", (uid,)).fetchone()
            assert role == "STUDENT"
            assert status == "CANCELLED"
            assert pstatus == "PAID"

        command.downgrade(cfg, "0026_expo_mobile_push")
        command.upgrade(cfg, "head")
        with sqlite3.connect(db_file) as conn:
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0029_user_allergen_preferences"
    finally:
        get_settings.cache_clear()


def test_ai_recommendations_max_budget_paise_and_cart_customizations_dict():
    """Verify maxBudgetPaise filters AI recommendations and cart item PUT accepts empty dict."""
    _, _, stu_tok = _make_user(UserRole.STUDENT, "ai-cart-stu")
    suffix = uuid.uuid4().hex[:6]
    with SessionLocal() as db:
        cat = Category(name=f"Filter Cat {suffix}", slug=f"filter-cat-{suffix}")
        db.add(cat)
        db.flush()
        cheap = Product(
            category_id=cat.id,
            name=f"Filter Bun {suffix}",
            price=20,
            price_paise=2000,
            stock_quantity=10,
            low_stock_threshold=2,
            is_available=True,
        )
        pricey = Product(
            category_id=cat.id,
            name=f"Filter Thali {suffix}",
            price=180,
            price_paise=18000,
            stock_quantity=10,
            low_stock_threshold=2,
            is_available=True,
        )
        db.add_all([cheap, pricey])
        db.commit()
        cheap_id = cheap.id
        pricey_id = pricey.id

    stu_h = {"Authorization": f"Bearer {stu_tok}"}
    with TestClient(app) as client:
        rec_res = client.get("/api/v1/ai/recommendations?maxBudgetPaise=3000", headers=stu_h)
        assert rec_res.status_code == 200, rec_res.text
        rec_items = rec_res.json()["data"].get("recommendations") or rec_res.json()["data"].get("items") or []
        for entry in rec_items:
            price_paise = entry.get("pricePaise")
            if price_paise is not None:
                assert price_paise <= 3000
        assert all(entry.get("menuItemId", entry.get("id")) != pricey_id for entry in rec_items)

        cart_res = client.put(
            f"/api/v1/cart/items/{cheap_id}",
            json={"quantity": 1, "customizations": {}},
            headers=stu_h,
        )
        assert cart_res.status_code == 200, cart_res.text


def test_allergen_preferences_persist_and_filter_menu_and_recommendations():
    """Verify PATCH /users/me saves allergenPreferences, GET /users/me returns them, and menu/AI filter them."""
    _, _, stu_tok = _make_user(UserRole.STUDENT, "allergen-stu")
    suffix = uuid.uuid4().hex[:6]
    with SessionLocal() as db:
        cat = Category(name=f"Allergen Cat {suffix}", slug=f"allergen-cat-{suffix}")
        db.add(cat)
        db.flush()
        peanut_item = Product(
            category_id=cat.id,
            name=f"Peanut Barfi {suffix}",
            price=30,
            price_paise=3000,
            stock_quantity=10,
            low_stock_threshold=2,
            is_available=True,
            allergen_tags="peanuts",
        )
        safe_item = Product(
            category_id=cat.id,
            name=f"Plain Idli {suffix}",
            price=30,
            price_paise=3000,
            stock_quantity=10,
            low_stock_threshold=2,
            is_available=True,
        )
        db.add_all([peanut_item, safe_item])
        db.commit()
        peanut_id = peanut_item.id
        safe_id = safe_item.id

    stu_h = {"Authorization": f"Bearer {stu_tok}"}
    with TestClient(app) as client:
        patch_res = client.patch(
            "/api/v1/users/me",
            json={"dietaryPreference": "vegetarian", "allergenPreferences": ["Peanuts", "Dairy"]},
            headers=stu_h,
        )
        assert patch_res.status_code == 200, patch_res.text
        assert patch_res.json()["data"]["allergenPreferences"] == ["peanuts", "dairy"]

        me_res = client.get("/api/v1/users/me", headers=stu_h)
        assert me_res.status_code == 200
        assert me_res.json()["data"]["allergenPreferences"] == ["peanuts", "dairy"]

        # Both singular excludeAllergen and plural excludeAllergens filter out matching allergens
        for qparam in ("excludeAllergen=peanuts", "excludeAllergens=peanuts"):
            menu_res = client.get(f"/api/v1/menu/items?{qparam}", headers=stu_h)
            assert menu_res.status_code == 200
            menu_ids = [it["id"] for it in menu_res.json()["data"]["items"]]
            assert peanut_id not in menu_ids
            assert safe_id in menu_ids

        # AI recommendations automatically respect saved user allergenPreferences
        ai_res = client.get("/api/v1/ai/recommendations?limit=20", headers=stu_h)
        assert ai_res.status_code == 200
        rec_items = ai_res.json()["data"].get("recommendations") or ai_res.json()["data"].get("items") or []
        rec_ids = [entry["item"]["id"] if "item" in entry else entry["id"] for entry in rec_items]
        assert peanut_id not in rec_ids


def test_notification_preferences_and_free_channels_and_mobile_contracts(monkeypatch):
    """Verify idempotent notification pref lookup, rate-limit flag, pickup slots, and mobile endpoint payloads."""
    from starlette.requests import Request as StarletteRequest
    from app.main import _check_general_api_rate_limit, _api_rate_buckets
    from app.modules.common import get_or_create_notification_pref

    stu_id, _, stu_tok = _make_user(UserRole.STUDENT, "mob-stu")
    stu_h = {"Authorization": f"Bearer {stu_tok}"}

    # Helper recovers cleanly from existing row or concurrent calls
    with SessionLocal() as db:
        p1 = get_or_create_notification_pref(db, stu_id)
        db.commit()
        p2 = get_or_create_notification_pref(db, stu_id)
        db.commit()
        assert p1.id == p2.id

    # Rate limit middleware respects rate_limit_enabled=False
    cfg = get_settings()
    monkeypatch.setattr(cfg, "rate_limit_enabled", False)
    _api_rate_buckets["198.51.100.77"].extend([1e9] * 500)
    fake_req = StarletteRequest(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/menu/items",
            "headers": [],
            "client": ("198.51.100.77", 12345),
        }
    )
    assert _check_general_api_rate_limit(fake_req) == 0
    _api_rate_buckets.pop("198.51.100.77", None)

    monkeypatch.setattr(cfg, "canteen_open_time", "00:00")
    monkeypatch.setattr(cfg, "canteen_close_time", "23:59")
    monkeypatch.setattr(cfg, "canteen_open_days", "0,1,2,3,4,5,6")

    with TestClient(app) as client:
        _, _, brand_new_tok = _make_user(UserRole.STUDENT, "brandnew-pref")
        bn_h = {"Authorization": f"Bearer {brand_new_tok}"}
        assert client.get("/api/v1/users/me/notification-preferences", headers=bn_h).status_code == 200
        assert client.get("/api/v1/push/free-channels", headers=bn_h).status_code == 200

        slots_res = client.get("/api/v1/pickup-slots", headers=stu_h)
        assert slots_res.status_code == 200, slots_res.text
        slots = slots_res.json()["data"]["slots"]
        assert len(slots) > 0
        assert "slot" in slots[0] and "startUtc" in slots[0] and "remaining" in slots[0]

        counters_res = client.get("/api/v1/counters", headers=stu_h)
        assert counters_res.status_code == 200
        c0 = counters_res.json()["data"]["items"][0]
        assert "approximateWaitMinutes" in c0 and "activeQueueOrders" in c0


def test_admin_legacy_purchase_orders_get_and_hash_links_and_station_flags(monkeypatch):
    """Regressions: GET /api/admin/purchase-orders, /customer/#verify-email link, SUCCESS canCancel, station autoPaused."""
    from app.models import KitchenStation, Supplier

    _, stu_email, stu_tok = _make_user(UserRole.STUDENT, "reg-stu")
    _, _, stf_tok = _make_user(UserRole.STAFF, "reg-stf")
    _, _, adm_tok = _make_user(UserRole.ADMIN, "reg-adm")
    stu_h = {"Authorization": f"Bearer {stu_tok}"}
    stf_h = {"Authorization": f"Bearer {stf_tok}"}
    adm_h = {"Authorization": f"Bearer {adm_tok}"}

    suffix = uuid.uuid4().hex[:6]
    with SessionLocal() as db:
        stu = db.query(User).filter(User.email == stu_email).first()
        cat = Category(name=f"Reg Cat {suffix}", slug=f"reg-cat-{suffix}")
        db.add(cat)
        db.flush()
        prod = Product(
            category_id=cat.id,
            name=f"Reg Idli {suffix}",
            price=20,
            price_paise=2000,
            stock_quantity=25,
            low_stock_threshold=3,
            is_available=True,
        )
        sup = Supplier(name=f"Reg Supplier {suffix}")
        ks = KitchenStation(
            name=f"Reg Station {suffix}",
            counter_id=1,
            max_concurrent_orders=12,
            active_order_count=0,
            is_active=True,
            auto_paused=True,
        )
        db.add_all([prod, sup, ks])
        db.flush()

        paid_ord = Order(
            user_id=stu.id,
            counter_id=1,
            order_number=f"REG-PAID-{suffix}",
            status=OrderStatus.CONFIRMED,
            payment_status=PaymentStatus.PAID,
            subtotal=20,
            final_total=20,
            subtotal_paise=2000,
            total_paise=2000,
        )
        unpaid_ord = Order(
            user_id=stu.id,
            counter_id=1,
            order_number=f"REG-UNP-{suffix}",
            status=OrderStatus.PENDING,
            payment_status=PaymentStatus.PENDING,
            subtotal=20,
            final_total=20,
            subtotal_paise=2000,
            total_paise=2000,
        )
        db.add_all([paid_ord, unpaid_ord])
        db.commit()
        prod_id, sup_id, ks_id, paid_id, unpaid_id = prod.id, sup.id, ks.id, paid_ord.id, unpaid_ord.id

    sent_emails: list[tuple[str, str, str]] = []
    monkeypatch.setattr("app.main.send_email", lambda to, subj, body: sent_emails.append((to, subj, body)))
    monkeypatch.setattr("app.modules.auth_profile.send_email", lambda to, subj, body: sent_emails.append((to, subj, body)))

    with TestClient(app) as client:
        # 1. Legacy GET /api/admin/purchase-orders no longer returns 405
        po_create = client.post(
            "/api/admin/purchase-orders",
            headers=adm_h,
            json={"supplier_id": sup_id, "lines": [{"product_id": prod_id, "quantity": 5, "unit_cost": "10.00"}]},
        )
        assert po_create.status_code == 201, po_create.text
        po_list = client.get("/api/admin/purchase-orders", headers=adm_h)
        assert po_list.status_code == 200, po_list.text
        assert any(row["id"] == po_create.json()["id"] for row in po_list.json())

        # 2. Password reset and email verification URLs target /customer/#...
        assert client.post("/api/auth/forgot-password", json={"email": stu_email}).status_code == 200
        assert client.post("/api/v1/auth/send-verification", headers=stu_h).status_code == 200
        bodies = "\n".join(b for _, _, b in sent_emails)
        assert "/customer/#reset=" in bodies
        assert "/customer/#verify-email=" in bodies

        # 3. Paid order serializes as SUCCESS + canCancel=False; unpaid pending has canCancel=True
        mine = client.get("/api/v1/users/me/orders?limit=20", headers=stu_h)
        assert mine.status_code == 200
        by_id = {o["id"]: o for o in mine.json()["data"]["items"]}
        assert by_id[paid_id]["paymentStatus"] == "SUCCESS"
        assert by_id[paid_id]["canCancel"] is False
        assert by_id[unpaid_id]["canCancel"] is True

        # 4. Kitchen stations report autoPaused & activeOrderCount and reopen clears autoPaused
        st_list = client.get("/api/v1/kitchen/stations", headers=stf_h)
        assert st_list.status_code == 200
        found_ks = next(s for s in st_list.json()["data"]["items"] if s["id"] == ks_id)
        assert found_ks["autoPaused"] is True
        assert "activeOrderCount" in found_ks
        reopen = client.post(f"/api/v1/kitchen/stations/{ks_id}/reopen", headers=stf_h)
        assert reopen.status_code == 200
        assert reopen.json()["data"]["autoPaused"] is False

        # 5. Embedded Procurement & Demand endpoints match app.js expected keys + Mobile active discounts include id/title
        from datetime import date, timedelta
        batch_res = client.post(
            "/api/admin/inventory/batches",
            headers=adm_h,
            json={
                "product_id": prod_id,
                "supplier_id": sup_id,
                "batch_code": f"REG-B-{suffix}",
                "quantity": 6,
                "expiry_date": (date.today() + timedelta(days=10)).isoformat(),
            },
        )
        assert batch_res.status_code == 201, batch_res.text
        exp_res = client.get("/api/admin/inventory/expiry?days=30", headers=adm_h)
        assert exp_res.status_code == 200
        assert any(b["batch_code"] == f"REG-B-{suffix}" for b in exp_res.json()["batches"])

        rec_res = client.get("/api/admin/inventory/reconciliation", headers=adm_h)
        assert rec_res.status_code == 200
        rec_body = rec_res.json()
        assert "products_checked" in rec_body and "untracked_legacy_products" in rec_body
        assert any(row["product_id"] == prod_id and row["batch_stock"] is not None for row in rec_body["items"])

        # Paid order item on prod_id triggers demand analytics row with current_stock
        with SessionLocal() as db:
            db.add(
                OrderItem(
                    order_id=paid_id,
                    product_id=prod_id,
                    product_name=f"Reg Idli {suffix}",
                    unit_price=20,
                    quantity=2,
                    line_total=40,
                )
            )
            db.commit()
        dem_res = client.get("/api/admin/analytics/demand?days=30", headers=adm_h)
        assert dem_res.status_code == 200
        prods = dem_res.json()["products"]
        assert any(p["product_id"] == prod_id and "current_stock" in p and p["units_sold"] >= 2 for p in prods)

        disc_create = client.post(
            "/api/v1/admin/discounts",
            headers=adm_h,
            json={"code": f"REG_{suffix.upper()}", "description": "Reg Test Offer", "discountPercent": 10, "minOrderPaise": 500, "isActive": True},
        )
        assert disc_create.status_code in (200, 201), disc_create.text
        disc_active = client.get("/api/v1/discounts/active", headers=stu_h)
        assert disc_active.status_code == 200
        assert any(d["code"] == f"REG_{suffix.upper()}" and d.get("id") and d.get("title") for d in disc_active.json()["data"]["items"])


def test_sqlite_startup_upgrades_alembic_0025_db_for_login(tmp_path, monkeypatch):
    """Regression: starting the app against a 0025 SQLite DB auto-upgrades to 0029 so login never 500s on missing columns."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine

    import app.main as main_mod

    db_file = tmp_path / "stale_0025.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    get_settings.cache_clear()
    cfg = Config(str(REPO_ROOT / "backend" / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "backend" / "alembic"))
    command.upgrade(cfg, "0025_phase89_enterprise_intelligence")

    tmp_engine = create_engine(f"sqlite:///{db_file.as_posix()}", future=True)
    try:
        monkeypatch.setattr(main_mod, "engine", tmp_engine)
        main_mod._sync_dev_sqlite_schema()
        with sqlite3.connect(db_file) as conn:
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0029_user_allergen_preferences"
            user_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
            assert "allergen_preferences_json" in user_cols
    finally:
        tmp_engine.dispose()
        get_settings.cache_clear()


def test_v1_login_refresh_and_mfa_challenge_envelope():
    """Verify V1 refresh uses 'success': True (consumed by staff-portal.js) and MFA returns mfaRequired + verify-login works."""
    from app.security import totp_code_at

    uid, email, _ = _make_user(UserRole.STAFF, "mfa-staff-reg")
    with TestClient(app) as client:
        login_res = client.post("/api/v1/auth/login", json={"identifier": email, "password": "RegressionPass-2026!"})
        assert login_res.status_code == 200, login_res.text
        tok = login_res.json()["data"]["accessToken"]
        rt = login_res.json()["data"]["refreshToken"]
        assert login_res.json()["success"] is True

        ref_res = client.post("/api/v1/auth/refresh", json={"refreshToken": rt})
        assert ref_res.status_code == 200, ref_res.text
        assert ref_res.json()["success"] is True
        assert ref_res.json()["data"]["accessToken"]

        setup = client.post("/api/v1/auth/mfa/setup", headers={"Authorization": f"Bearer {tok}"})
        assert setup.status_code == 200
        secret = setup.json()["data"]["secret"]
        enable = client.post(
            "/api/v1/auth/mfa/enable",
            headers={"Authorization": f"Bearer {tok}"},
            json={"code": totp_code_at(secret)},
        )
        assert enable.status_code == 200, enable.text

        chal_res = client.post("/api/v1/auth/login", json={"identifier": email, "password": "RegressionPass-2026!"})
        assert chal_res.status_code == 200
        chal_data = chal_res.json()["data"]
        assert chal_data["mfaRequired"] is True
        assert chal_data["mfaChallengeToken"]

        fin_res = client.post(
            "/api/v1/auth/mfa/verify-login",
            json={"mfaChallengeToken": chal_data["mfaChallengeToken"], "code": totp_code_at(secret)},
        )
        assert fin_res.status_code == 200, fin_res.text
        assert fin_res.json()["data"]["accessToken"]


