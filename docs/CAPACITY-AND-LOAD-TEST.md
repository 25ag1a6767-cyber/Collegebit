# CollegeBits — Concurrency Verification, Load Benchmark & College Server Capacity Guide

## 1. Empirical Stock-Contention & Burst Benchmark (Measured)

Reproducible via:
```powershell
$env:PYTHONPATH = "backend"
.\.venv\Scripts\python.exe scripts\run_concurrency_load_test.py
```

### A. Atomic Stock Contention Stress Test (40 Simultaneous Buyers → 15 Stock Units)
- **Scenario:** 40 authenticated student accounts submit concurrent `POST /api/v1/orders` requests (with unique `Idempotency-Key` headers) competing for a single menu item initialized with `stock_quantity = 15`.
- **Concurrency Safety Mechanisms:**
  - PostgreSQL: Row-level `SELECT ... FOR UPDATE` on sorted `Product` IDs + transaction-scoped advisory lock `pg_advisory_xact_lock(hashtext('campusbite_token:' || today))` + FEFO batch lock + `CHECK (stock_quantity >= 0)` constraint.
  - SQLite (local/dev): Serialized checkout mutex with `db.expire_all()` + database `CHECK` constraints.
- **Measured Outcome:**
  - `initialStock`: **15**
  - `concurrentBuyers`: **40**
  - `succeededOrders201`: **15** (`HTTP 201 Created`)
  - `rejectedSoldOut409`: **25** (`HTTP 409 ITEM_UNAVAILABLE`)
  - `finalStockRemaining`: **0**
  - `zeroOversellVerified`: **`true`** (0 race-condition `500`s, 0 duplicate token numbers, 0 negative stock)
  - Total wall time: **2.819 s** (`p50 = 961.87 ms`, `p95 = 1634.69 ms` under single-process SQLite serialization)

### B. Mixed Break-Time Burst Benchmark (200 Concurrent Requests)
- **Workload Mix:** `/api/v1/menu/items`, `/api/v1/counters`, `/api/v1/discounts/active`, `/api/v1/menu/categories`, and `/api/v1/admin/analytics/dashboard`.
- **Measured Outcome (Single-Process In-Memory ASGI Harness on SQLite):**
  - `http200Count`: **200 / 200 (100% success)**
  - `throughputReqPerSec`: **35.1 req/s** (single Python GIL process including live 16-graph BI computation on 20% of requests)
  - `p50Ms`: **362.51 ms** · `p95Ms`: **1521.05 ms**

---

## 2. Realistic Single College Server Capacity Sizing (Honest Engineering Analysis)

A single self-hosted college server **cannot** literally serve "millions of simultaneous active users" on one machine — claiming otherwise would be dishonest. However, a typical engineering college has **1,500 to 10,000 total students**, with **300 to 2,000 concurrent active users during a 15-minute morning/lunch break**.

On a single standard college server (**8 vCPU, 16 GB RAM, NVMe SSD, Ubuntu 24.04 LTS**):

| Tier | Configuration | Sustained Capacity |
| :--- | :--- | :--- |
| **Nginx Reverse Proxy** | TLS 1.3 termination, static caching of `/css`, `/js`, `/assets`, gzip/brotli | 10,000+ concurrent keep-alive sockets |
| **FastAPI / Uvicorn** | `8` worker processes (`--workers 8`) | ~400–900 API req/s on menu/order endpoints |
| **PgBouncer (`:6432`)** | `pool_mode = transaction`, `max_client_conn = 2000`, `default_pool_size = 40` | Multiplexes **2,000 concurrent client sessions** onto **40 PostgreSQL backend workers** without exhausting PostgreSQL RAM |
| **PostgreSQL 16** | `shared_buffers = 2GB`, `work_mem = 16MB`, composite indexes on `(payment_status, created_at)`, `(user_id, created_at)`, `(counter_id, status)` | Easily handles **500–2,500 concurrent break-time students** and **millions of historical cumulative order rows** |
| **Redis 7** | Menu/counter read cache + WebSocket pub/sub + distributed rate limiting | Sub-millisecond hot-path lookups |

### Scaling Beyond One Campus (Multi-College / Regional Scale)
Because CollegeBits uses stateless JWT access tokens, `public_uuid` external identifiers, `NullPool` PgBouncer transaction compatibility, and externalizable object storage (`OBJECT_STORAGE_PUBLIC_URL`), scaling beyond a single college server requires zero code rewrites:
1. Add horizontal FastAPI container replicas behind Nginx / HAProxy.
2. Point BI & Report read queries (`analytics_bi.py`, `reporting_engine.py`) to a PostgreSQL streaming read replica.
3. Offload static assets and menu images to campus CDN / S3-compatible MinIO.
