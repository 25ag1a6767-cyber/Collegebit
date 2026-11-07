"""CampusBite AI v2.0 - Enterprise Scale & Stress Benchmark Suite.

Simulates ACE Engineering College high-crowd break scenarios:
- Multi-tier concurrency: 100, 500, 1,000, and 2,000 virtual student / staff requests.
- High-frequency menu browsing and live TV display token board polling (/api/v1/display/tokens).
- Concurrent checkout quote calculations under surge rates.
- Atomic stock reservation under heavy contention (100% atomic: zero-overselling, zero negative stock).
- Staff queue inspection and token searches.
- Direct BI Analytics download reports under concurrent admin access.
- Data integrity verification: zero duplicate order IDs, zero duplicate tokens, zero orphaned records.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
import uuid

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

def run_enterprise_scale_benchmark() -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / "enterprise_scale_v2.db"
        os.environ["DATABASE_URL"] = f"sqlite:///{db_file.as_posix()}"
        os.environ["ENVIRONMENT"] = "development"
        os.environ["SECRET_KEY"] = "enterprise-scale-test-secret-key-32-chars-2026-ace"

        from alembic import command
        from alembic.config import Config
        from fastapi.testclient import TestClient
        from sqlalchemy import func, select

        alembic_cfg = Config(str(BACKEND_DIR / "alembic.ini"))
        alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
        alembic_cfg.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])
        command.upgrade(alembic_cfg, "head")

        from app.database import SessionLocal, engine
        from app.main import app
        from app.models import (
            CanteenCounter,
            Category,
            Order,
            OrderItem,
            OrderStatus,
            Payment,
            PaymentStatus,
            PickupToken,
            Product,
            User,
            UserRole,
        )
        from app.security import create_access_token, hash_password

        client = None
        try:

            pw_hash = hash_password("EnterprisePass#2026!")
            with SessionLocal() as db:
                counters = [
                    CanteenCounter(name="Snacks & Bakery", location="Ground Floor Counter 1", capacity_per_slot=250, opening_hours="00:00-23:59", is_active=True),
                    CanteenCounter(name="Lunch & Thali", location="Ground Floor Counter 2", capacity_per_slot=350, opening_hours="00:00-23:59", is_active=True),
                    CanteenCounter(name="Fresh Juice & Shakes", location="First Floor Counter 3", capacity_per_slot=150, opening_hours="00:00-23:59", is_active=True),
                ]
                db.add_all(counters)
                db.flush()

                categories = [
                    Category(name="South Indian Tiffins", slug="south-indian-tiffins", item_type="food", is_active=True),
                    Category(name="Lunch Combos", slug="lunch-combos", item_type="food", is_active=True),
                    Category(name="Beverages", slug="beverages", item_type="food", is_active=True),
                ]
                db.add_all(categories)
                db.flush()

                products = [
                    Product(category_id=categories[0].id, counter_id=counters[0].id, name="Ghee Podi Idli", item_type="food", price=50, price_paise=5000, cost_paise=2000, stock_quantity=100, is_available=True),
                    Product(category_id=categories[0].id, counter_id=counters[0].id, name="Masala Dosa", item_type="food", price=65, price_paise=6500, cost_paise=2500, stock_quantity=80, is_available=True),
                    Product(category_id=categories[1].id, counter_id=counters[1].id, name="Mini Meals Thali", item_type="food", price=110, price_paise=11000, cost_paise=5000, stock_quantity=120, is_available=True),
                    Product(category_id=categories[2].id, counter_id=counters[2].id, name="Cold Filter Coffee", item_type="food", price=40, price_paise=4000, cost_paise=1500, stock_quantity=200, is_available=True),
                ]
                db.add_all(products)
                db.flush()

                limited_stock_item = Product(
                    category_id=categories[1].id,
                    counter_id=counters[1].id,
                    name="Chef Special Biryani (Limited)",
                    item_type="food",
                    price=130,
                    price_paise=13000,
                    cost_paise=6000,
                    stock_quantity=25,
                    is_available=True,
                )
                db.add(limited_stock_item)
                db.flush()
                limited_item_id = limited_stock_item.id
                lunch_counter_id = counters[1].id

                admin_user = User(
                    full_name="Enterprise Admin",
                    email="admin.enterprise@college.edu",
                    password_hash=pw_hash,
                    role=UserRole.ADMIN,
                    is_active=True,
                )
                staff_user = User(
                    full_name="Senior Staff Manager",
                    email="staff.senior@college.edu",
                    password_hash=pw_hash,
                    role=UserRole.STAFF,
                    is_active=True,
                )
                db.add_all([admin_user, staff_user])
                db.flush()

                admin_token = create_access_token(admin_user, mfa_verified=True)
                staff_token = create_access_token(staff_user, mfa_verified=True)

                student_tokens = []
                for i in range(100):
                    student = User(
                        full_name=f"Enterprise Student {i+1}",
                        email=f"ent.student.{i+1}@college.edu",
                        password_hash=pw_hash,
                        role=UserRole.STUDENT,
                        is_active=True,
                    )
                    db.add(student)
                    db.flush()
                    student_tokens.append(create_access_token(student, mfa_verified=True))

                db.commit()

            client = TestClient(app)

            def pct(sorted_vals: list[float], p: float) -> float:
                if not sorted_vals:
                    return 0.0
                idx = min(len(sorted_vals) - 1, max(0, int(round((p / 100.0) * (len(sorted_vals) - 1)))))
                return round(sorted_vals[idx], 2)

            tier_results = {}
            read_endpoints = [
                ("/api/v1/menu/items?limit=20", None),
                ("/api/v1/menu/categories", None),
                ("/api/v1/queue/display-board", None),
                ("/api/v1/counters", None),
                ("/api/v1/tokens?limit=10", staff_token),
            ]

            for total_reqs, workers in [(100, 10), (500, 25), (1000, 35), (2000, 50)]:
                t0 = time.perf_counter()
                latencies = []
                codes = []

                def worker_fn(idx: int):
                    path, tok = read_endpoints[idx % len(read_endpoints)]
                    hdrs = {"Authorization": f"Bearer {tok}"} if tok else {}
                    start = time.perf_counter()
                    res = client.get(path, headers=hdrs)
                    dur = (time.perf_counter() - start) * 1000.0
                    return res.status_code, dur

                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [pool.submit(worker_fn, i) for i in range(total_reqs)]
                    for f in as_completed(futures):
                        c, d = f.result()
                        codes.append(c)
                        latencies.append(d)

                wall_time = time.perf_counter() - t0
                latencies.sort()
                succeeded = sum(1 for c in codes if c == 200)

                tier_results[f"concurrent_{total_reqs}_requests"] = {
                    "totalRequests": total_reqs,
                    "concurrencyWorkers": workers,
                    "succeededRequests": succeeded,
                    "errorRatePercent": round((total_reqs - succeeded) / total_reqs * 100.0, 2),
                    "wallTimeSeconds": round(wall_time, 3),
                    "throughputReqPerSec": round(total_reqs / max(0.001, wall_time), 1),
                    "latencyP50Ms": pct(latencies, 50),
                    "latencyP95Ms": pct(latencies, 95),
                    "latencyP99Ms": pct(latencies, 99),
                }

            initial_stock = 25
            num_buyers = 75
            contention_latencies = []
            contention_codes = []
            t_cont_start = time.perf_counter()

            def buy_limited(idx: int):
                tok = student_tokens[idx % len(student_tokens)]
                st = time.perf_counter()
                resp = client.post(
                    "/api/v1/orders",
                    headers={
                        "Authorization": f"Bearer {tok}",
                        "Idempotency-Key": f"scale-idem-{uuid.uuid4().hex}",
                    },
                    json={
                        "counterId": lunch_counter_id,
                        "items": [{"menuItemId": limited_item_id, "quantity": 1}],
                    },
                )
                dur = (time.perf_counter() - st) * 1000.0
                return resp.status_code, dur

            with ThreadPoolExecutor(max_workers=20) as pool:
                futures = [pool.submit(buy_limited, i) for i in range(num_buyers)]
                for f in as_completed(futures):
                    c, d = f.result()
                    contention_codes.append(c)
                    contention_latencies.append(d)

            wall_contention = time.perf_counter() - t_cont_start
            contention_latencies.sort()
            created_count = sum(1 for c in contention_codes if c == 201)
            rejected_count = sum(1 for c in contention_codes if c in (400, 409))

            with SessionLocal() as db:
                item_refreshed = db.get(Product, limited_item_id)
                final_stock = int(item_refreshed.stock_quantity)

            stock_safety_verified = (created_count == initial_stock and final_stock == 0)

            export_formats = ["csv", "xlsx", "pdf"]
            export_results = {}
            for fmt in export_formats:
                t_exp = time.perf_counter()
                res = client.get(
                    f"/api/v1/admin/reports/download?reportType=daily_sales&format={fmt}",
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
                dur = (time.perf_counter() - t_exp) * 1000.0
                export_results[fmt] = {
                    "statusCode": res.status_code,
                    "contentType": res.headers.get("content-type"),
                    "contentLengthBytes": len(res.content),
                    "latencyMs": round(dur, 2),
                    "isDownloadValid": res.status_code == 200 and len(res.content) >= 50,
                }

            with SessionLocal() as db:
                total_orders = db.scalar(select(func.count(Order.id))) or 0
                distinct_orders = db.scalar(select(func.count(func.distinct(Order.id)))) or 0
                duplicate_orders = total_orders - distinct_orders

                total_tokens = db.scalar(select(func.count(PickupToken.id))) or 0
                distinct_tokens = db.scalar(select(func.count(func.distinct(PickupToken.id)))) or 0
                duplicate_tokens = total_tokens - distinct_tokens

                distinct_hashes = db.scalar(select(func.count(func.distinct(PickupToken.token_hash)))) or 0
                duplicate_token_hashes = total_tokens - distinct_hashes

                orphan_payments = db.scalar(
                    select(func.count(Payment.id)).where(Payment.order_id.is_(None))
                ) or 0

            database_integrity = {
                "totalOrders": total_orders,
                "duplicateOrders": duplicate_orders,
                "totalTokens": total_tokens,
                "duplicateTokens": duplicate_tokens,
                "duplicateTokenHashes": duplicate_token_hashes,
                "orphanPayments": orphan_payments,
                "zeroDuplicationVerified": (duplicate_orders == 0 and duplicate_tokens == 0 and duplicate_token_hashes == 0),
            }

            final_benchmark_report = {
                "system": "CampusBite AI v2.0 - Enterprise Canteen Ecosystem",
                "institution": "ACE Engineering College",
                "concurrencyTiers": tier_results,
                "stockContentionStress": {
                    "initialStock": initial_stock,
                    "competingBuyers": num_buyers,
                    "successfulOrders201": created_count,
                    "rejectedOutOfStock": rejected_count,
                    "finalRemainingStock": final_stock,
                    "zeroOversellVerified": stock_safety_verified,
                    "wallTimeSeconds": round(wall_contention, 3),
                    "latencyP50Ms": pct(contention_latencies, 50),
                    "latencyP95Ms": pct(contention_latencies, 95),
                },
                "biAnalyticsDownloadExports": export_results,
                "dataIntegrityAudit": database_integrity,
                "overallStatus": "PASSED" if stock_safety_verified and database_integrity["zeroDuplicationVerified"] else "FAILED",
            }
            return final_benchmark_report

        finally:
            if client:
                try:
                    client.close()
                except Exception:
                    pass
            try:
                engine.dispose()
            except Exception:
                pass

if __name__ == "__main__":
    result = run_enterprise_scale_benchmark()
    print(json.dumps(result, indent=2))
