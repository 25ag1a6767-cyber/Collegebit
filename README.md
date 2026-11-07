# CampusBite AI v2.0 — Complete College Canteen Ecosystem (ACE Canteen)

Production-ready pre-order, verified Razorpay UPI payment, single-use QR pickup, live kitchen queue, and AI business intelligence platform for college canteens — connected to **one centralized FastAPI backend and PostgreSQL database**.

## 1. Three Dedicated Applications Connected to One Shared Backend & Database

| Interface | Entrypoint / Codebase | Target Users & Key Capabilities |
| :--- | :--- | :--- |
| **A. Customer Application (Android + Optional PWA)** | `mobile/` (Expo React Native Android) & `GET /` (`frontend/index.html`) | Registration/login, dietary & allergen profile, menu categories/search/filters, cart, checkout, Razorpay UPI, live order tracking, single-use QR pickup, digital tax receipts (JSON + PDF), push & WebSocket alerts, promo offers, ratings/feedback, AI food recommendations & concierge. |
| **B. Staff Operations Website** | `GET /staff` (`frontend/staff.html`, `frontend/js/staff-portal.js`) | Secure staff login, live kitchen order queue, **accept & reject** orders (with stock restoration & auto-refund), preparation status updates, ready-for-pickup alerts, camera QR scanning & manual code verification, menu availability toggles, permission-scoped inventory/wastage, completed order history, daily staff performance & throughput graphs. |
| **C. Enterprise Admin & BI Website** | `GET /admin` (`frontend/admin.html`, `frontend/js/admin-portal.js`) | **10 KPI cards**, **16 interactive SVG charts**, **11 filters**, **4 period comparisons**, **14 automated reports (PDF / Excel `.xlsx` / CSV)**, **12 AI BI modules** (`ACTUAL` vs `PREDICTION` + Menu Engineering Matrix), student/staff/admin accounts, granular staff permissions, menu/pricing/unit costs, suppliers, purchase orders, expenses, payment reconciliation & refunds, customer feedback, operating hours, counters, DB admin tools, encrypted 7D/4W/3M GFS backups, 4-day safe temporary cleanup, security monitoring, and audit logs. |

---

## 2. Quickstart (Windows PowerShell)

```powershell
py -3 -m venv .venv
.\.venv\Scripts\pip install -r backend\requirements.txt
copy .env.example .env

$env:PYTHONPATH = "backend"
.\.venv\Scripts\python.exe -m alembic -c backend\alembic.ini upgrade head
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000
```

- **Customer Web / PWA:** http://127.0.0.1:8000/
- **Staff Website:** http://127.0.0.1:8000/staff
- **Admin & BI Website:** http://127.0.0.1:8000/admin
- **Live TV Token Board:** http://127.0.0.1:8000/display

### Default Demo Credentials (Local Testing)
| Portal | Role | Email / Login ID | Password |
|---|---|---|---|
| **Admin Portal** (`/admin`) | Administrator | `25ag1a6767@aceec.ac.in` | `ace@25ag1a67676` |
| **Staff Portal** (`/staff`) | Kitchen Staff | `25ag1a6767@gmail.com` | `ace@25ag1a67676staff` |
| **Customer App / PWA** (`/`) | Student | `student.dev@example.edu` | `Student#2026!` |

*(Or run `START_LOCAL_DEV.bat` for local SQLite development with auto-seeded local-only demo accounts.)*

---

## 3. Zero-Cost Self-Hosted College PostgreSQL & Optional Neon Free Portability

- **Primary (Free Self-Hosted College PostgreSQL 16 + PgBouncer):**
  - Ubuntu Linux: `sudo bash deploy/postgres-college/install_college_postgres_ubuntu.sh`
  - Windows Server: `powershell -ExecutionPolicy Bypass -File .\scripts\install_college_postgres_windows.ps1`
  - Docker Compose Stack: `docker compose up -d --build`
- **Optional Cloud Portability (Neon Free PostgreSQL):**
  - Copy `.env.neon.example` to `.env` and set `DATABASE_URL` to your Neon pooled connection string (`DB_USE_PGBOUNCER_TRANSACTION_POOL=true`).

---

## 4. Documentation & Operational Guides

- [Complete Installation & Deployment Guide](docs/INSTALLATION-GUIDE.md)
- [Backup, 4-Day Hybrid Retention & Disaster Recovery Runbook](docs/DISASTER-RECOVERY.md)
- [Concurrency Verification, Load Benchmark & Capacity Sizing](docs/CAPACITY-AND-LOAD-TEST.md)
- [Go-Live Checklist](docs/GO-LIVE.md)

---

## 5. Verification & Release Gate

```powershell
$env:PYTHONPATH = "backend"
.\.venv\Scripts\python.exe scripts\release_gate.py
.\.venv\Scripts\python.exe scripts\run_concurrency_load_test.py
```
