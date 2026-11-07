# CampusBite AI v2.0 — Complete Installation & Deployment Guide

CampusBite AI v2.0 is a centralized college canteen platform connecting **three dedicated client applications** to **one shared FastAPI backend and PostgreSQL database**:

1. **Customer Android Application (`mobile/`)** + **Optional Installable Customer PWA (`GET /`)**
2. **Dedicated Staff Operations Website (`GET /staff`)**
3. **Dedicated Enterprise Administration & BI Website (`GET /admin`)**

---

## 1. Zero-Cost Self-Hosted College PostgreSQL Setup (Primary Recommended)

CampusBite AI v2.0 is architected to run **100% free on college-owned hardware** (Ubuntu Linux server or Windows Server/PC) with zero recurring cloud database fees.

### Option A — Ubuntu Linux College Server (PostgreSQL 16 + PgBouncer + WAL Archiving)

```bash
sudo bash deploy/postgres-college/install_college_postgres_ubuntu.sh
```

This automated script:
- Installs `postgresql-16` and `pgbouncer`.
- Creates the `campusbite_app` role and `campusbite_prod` database (`deploy/postgres-college/init_campusbite_db.sql`).
- Applies college break-time memory/WAL tuning (`deploy/postgres-college/postgresql-college.conf`).
- Configures PgBouncer transaction pooling on port `6432` (`deploy/pgbouncer/pgbouncer.ini`) so 500–2,000+ concurrent student connections multiplex cleanly onto 40 PostgreSQL backend workers.

### Option B — Windows College Server / Lab Host

Run in an Administrator PowerShell window:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_college_postgres_windows.ps1
```

### Option C — Docker Compose Full College Stack (PostgreSQL 16 + PgBouncer + Redis + App + Automated Backups)

```bash
cp .env.production.example .env
# Edit .env with your SECRET_KEY, POSTGRES_PASSWORD, BACKUP_ENCRYPTION_KEY, and Razorpay keys
docker compose up -d --build
```

Services launched:
- `postgres`: PostgreSQL 16 Alpine with persistent volume and continuous WAL archiving
- `pgbouncer`: Transaction-mode connection pooler (`:6432`)
- `redis`: Rate-limit, cache, and real-time pub/sub broker (`:6379`)
- `app`: Multi-worker Uvicorn FastAPI server serving `/`, `/staff`, `/admin`, and `/api/v1/*` (`:8000`)
- `scheduler`: Automated daily encrypted GFS backup, 4-day safe temporary cleanup, and daily report runner

---

## 2. Optional Free Cloud Portability — Neon Free PostgreSQL

CampusBite AI v2.0 is 100% standard SQLAlchemy 2.0 + Psycopg 3 + Alembic. To run against **Neon Free PostgreSQL** (for cloud demos or off-campus hosting) without changing a single line of application code:

1. Copy `.env.neon.example` to `.env`.
2. Paste your Neon pooled connection string (`-pooler` hostname with `?sslmode=require`) into `DATABASE_URL` and set `DB_USE_PGBOUNCER_TRANSACTION_POOL=true`.
3. Run migrations:
   ```bash
   PYTHONPATH=backend alembic -c backend/alembic.ini upgrade head
   ```

> **Honest Free-Tier Note:** Neon Free enforces storage (~0.5 GB) and compute-hour quotas, whereas the self-hosted College PostgreSQL deployment in Section 1 has **zero external quotas** and LAN-local sub-millisecond latency.

---

## 3. Local Development & Verification Setup (Windows / Linux / macOS)

### Prerequisites
- Python 3.11+ (tested on Python 3.13)
- Node.js 20+ & npm (for mobile TypeScript checks and JS syntax gates)

### Step-by-Step Quickstart (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt

$env:PYTHONPATH = "backend"
alembic -c backend\alembic.ini upgrade head

# Start the unified backend + 3 web portals
uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000 --reload
```

### Portal URLs
- **Customer Web / Installable PWA:** http://127.0.0.1:8000/
- **Staff Kitchen & QR Operations Website:** http://127.0.0.1:8000/staff
- **Enterprise Admin & BI Analytics Website:** http://127.0.0.1:8000/admin
- **OpenAPI / Swagger Specification:** http://127.0.0.1:8000/docs
- **Health & Readiness Probe:** http://127.0.0.1:8000/api/health

---

## 4. Customer Android Application (`mobile/`) — Development, APK & AAB Builds

### Running on an Android Phone or Emulator
```powershell
cd mobile
npm install
# Point EXPO_PUBLIC_API_URL to your college server LAN IP (or 10.0.2.2 for Android Emulator)
$env:EXPO_PUBLIC_API_URL = "http://192.168.1.50:8000"
npx expo start --android
```

### Generating Installable `.apk` and Play Store `.aab` Releases

1. **Automated Local Gradle Build (requires JDK 17 + Android SDK `ANDROID_HOME`):**
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\build_android_apk.ps1
   ```
   Outputs:
   - `mobile/android/app/build/outputs/apk/release/app-release.apk`
   - `mobile/android/app/build/outputs/bundle/release/app-release.aab`

2. **Cloud Build via Expo EAS (no local Android Studio required):**
   ```bash
   cd mobile
   npx eas-cli build --platform android --profile preview      # Produces .apk
   npx eas-cli build --platform android --profile production   # Produces .aab
   ```

3. **GitHub Actions Automated CI Build:**
   Push to GitHub and run workflow `.github/workflows/android-build.yml` (`workflow_dispatch`) to compile and download signed/unsigned `.apk` and `.aab` artifacts automatically.

---

## 5. Required External Credentials for Live Production Integrations

| Integration | Environment Variables | Fallback Behavior When Unconfigured |
| :--- | :--- | :--- |
| **Razorpay UPI Gateway** | `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` | Returns explicit `503 PAYMENT_GATEWAY_NOT_CONFIGURED` on live UPI intent creation; never fakes live bank settlements |
| **SMTP Email Delivery** | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM` | Logs verification links & daily report summaries locally while recording `GeneratedReport` in DB |
| **Gemini AI Provider** | `GEMINI_API_KEY` | Uses deterministic grounded PostgreSQL statistical & rule-based AI engine (`weekday-slot-wma-v1`) with zero downtime |
| **Web Push VAPID** | `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY` | Live WebSocket push (`ws/v1/events`) and in-app notification center remain 100% active |
