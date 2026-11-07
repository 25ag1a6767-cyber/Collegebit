# ACE Canteen — Live Production Deployment & Operations Guide

Follow these steps in order when deploying ACE Canteen to serve live campus breaks.

## 0. Merchant & Infrastructure Prerequisites
- **Razorpay Merchant Account:** Complete business KYC and enable **UPI Intent / Collect** + **Webhooks** + **Refunds API**.
- **PostgreSQL 15+ & Redis (optional):** Managed PostgreSQL for transactional stock reservations, payments, and outbox events; Redis recommended for multi-worker throttling.
- **HTTPS Domain & SMTP:** Public TLS domain (e.g. `https://canteen.yourcampus.edu`) and transactional SMTP for password resets, email verification, and payment receipts.

## 1. Environment & Preflight Validation
```bash
cp .env.production.example .env
# Populate SECRET_KEY (48+ random chars), DATABASE_URL, PUBLIC_BASE_URL,
# RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, RAZORPAY_WEBHOOK_SECRET,
# GEMINI_API_KEY (optional), VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY (optional), CORS_ORIGINS
python3 scripts/deployment_preflight.py --require-live-payment
```
Never enable `SEED_DEMO_DATA` or `AUTO_CREATE_TABLES` in production (`ENVIRONMENT=production` enforces this at startup).

## 2. Database Migration & Initial Administrator Bootstrap
```bash
docker compose --profile ops run --rm migrate   # Runs: alembic upgrade head
docker compose up -d --build app db
docker compose run --rm app python -m app.create_admin --email admin@yourcampus.edu --name "Canteen Administrator"
curl -fsS http://127.0.0.1:8000/api/ready
```
1. Sign in as the administrator and immediately enroll **Two-Factor Authentication (TOTP 2FA)** under **Profile, Dietary & Security → Setup 2FA / MFA**.
2. Create campus counters (**A02**), invite counter staff accounts, and bind each staff member to their authorized counter IDs (`PUT /api/v1/admin/users/{id}/counters`).

## 3. Reverse Proxy, TLS & WebSockets
Copy `deploy/nginx-https-example.conf`, configure Let's Encrypt TLS (`certbot --nginx`), and ensure WebSocket upgrade headers (`Upgrade` and `Connection "upgrade"`) are forwarded for `/api/v1/ws` and `/ws/events`. Set `HTTPS_TERMINATED=true` only after verifying HTTPS.

## 4. Razorpay UPI, Webhooks & Refunds
1. Configure Razorpay webhook URL: `https://canteen.yourcampus.edu/api/v1/webhooks/payments/razorpay` (legacy `/api/payments/webhook` also remains active) with `RAZORPAY_WEBHOOK_SECRET`.
2. Subscribe to `payment.captured`, `payment.failed`, `refund.processed`, and `refund.failed`.
3. **Late-Capture Protection:** If a `payment.captured` webhook arrives after an unpaid reservation expired or was cancelled, the backend automatically flags `late_capture_flagged=true`, moves the order to `REFUND_PENDING`, and initiates a gateway refund so students never lose money on unreserved stock.
4. **Authorized Refunds:** Administrators can issue full or partial refunds directly from **A04 · Authorized Gateway Refund** (`POST /api/v1/payments/{id}/refunds`) and audit discrepancies via `/api/v1/admin/payments/reconciliation`.

## 5. AI Services & Privacy Controls
- **Grounded Menu Concierge (`/api/v1/ai/assistant`):** Uses `GEMINI_API_KEY` (`gemini-2.5-flash` by default) with strict post-generation verification against live in-stock menu rows, budget caps, and allergen tags, falling back automatically to deterministic verified search if Gemini is unconfigured or unreachable. Never sends student PII to external AI APIs, and never adds items to carts without explicit student confirmation.
- **Demand Forecasting (`/api/v1/ai/demand-forecasts`):** Computes weekday + slot weighted moving averages with empirical `[lowerBound, upperBound]` intervals, low-sample warnings, staff overrides, and genuine rolling backtest `MAE` / `WAPE`.

## 6. Customer Apps for Android & iPhone (`mobile/`)
The staff/admin website is served at `/`, `/staff`, and `/admin`. The installable customer PWA is served separately at `/customer/`.

Build the Expo React Native customer app for Android and iPhone using the production HTTPS API configured in `mobile/eas.json`:
```bash
cd mobile
npm install
npx eas build --platform android --profile production
npx eas build --platform ios --profile production
```
The Android and iOS installs require the EAS build outputs and corresponding distribution setup. Until those builds are distributed, students can install the PWA from `https://app-sai-e3e1.vercel.app/customer/` (Android: browser install option; iPhone: Safari Share → Add to Home Screen).

## 7. Backups, Monitoring & Campus Pilot
- Run `./scripts/backup_db.sh` daily to off-host encrypted storage and verify a restore drill before opening campus-wide.
- Monitor `/api/ready` and authenticated `/api/metrics` (`Bearer <METRICS_TOKEN>`).
- Run a 1-week single-counter live pilot before enabling all campus counters.
