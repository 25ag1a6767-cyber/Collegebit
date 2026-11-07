# Phase 48 — account recovery, forced password change, durable sessions, deploy fixes

## Added
- `POST /api/auth/forgot-password` — emails a single-use reset link (valid `PASSWORD_RESET_MINUTES`, default 30). Always returns the same response, whether or not the email exists. Rate-limited per IP+email.
- `POST /api/auth/reset-password` — sets a new password from the token, signs out every existing session, writes an audit event. The link stops working once used because the token is bound to the current password hash.
- Reset link format is `PUBLIC_BASE_URL/#reset=<token>`; the token is in the URL fragment and removed from the address bar immediately.
- `users.must_change_password` (migration `0014`). Staff/admin accounts created by an admin must change the temporary password: until they do, staff/admin endpoints return 403 and the UI sends them to the password form.
- The customer app retains its existing `localStorage` session behavior. The staff portal uses `sessionStorage` and rotating refresh tokens, so the session survives a page reload in the same tab but is cleared when that tab closes or the user signs out.
- Optional SMTP settings (`SMTP_*`, `PUBLIC_BASE_URL`). The public forgot-password flow always returns a generic response; without SMTP, development logs its reset link and production logs a warning without sending.
- The staff portal uses the same-origin `/api/v1` routes. In the Vercel deployment, rewrites send page and API requests to the same app service; no separate browser API URL or cross-origin CORS setting is needed. Use `CORS_ORIGINS` only when intentionally serving the frontend from a different origin.
- `/api/health` is a liveness check. `/api/ready` checks the database and, in production, the shared Redis rate limiter required by authentication. A missing or unreachable production Redis endpoint returns `503` instead of reporting the service ready.
- Production staff sign-in is restricted to active `staff` and `admin` roles. Unknown identifiers and incorrect passwords return the same generic response. Inactive accounts receive a status message only after the password is verified.
- The staff portal now supports MFA challenges, forced changes of temporary passwords, and same-device token refresh. Access and rotating refresh tokens are held in `sessionStorage`; they are not cookies. Logout revokes the refresh session and clears browser storage.
- An administrator creates staff in **Admin → Users** by selecting the staff role, entering an initial password of at least 12 characters, and assigning only the required counters. New staff must change that password before using staff tools. Configure permissions separately; creating a staff account does not promote it to administrator.
- An administrator can select **Email Reset Link** for an active staff account. The link is single-use, time-limited, and sent to the account email; it does not set or return a password. Delivery is checked before the API reports success; a missing or failing SMTP service returns `503`. Configure production `SMTP_*` settings and the correct HTTPS `PUBLIC_BASE_URL` before using this action. Staff can also request a reset from `/staff`; reset responses do not disclose whether an email exists.
- Never run `scripts/seed_dev_demo.py` against production. The script refuses production/staging modes and non-SQLite databases. Do not copy local development accounts into the production database.
- No migration is required for these login and reset changes. The existing user, token-version, forced-password-change, and refresh-session schema is reused.

## Fixed
- **Fresh databases could not be migrated.** Migration `0001` creates tables from the current models, so `0012`, `0013` (and `0014`) failed with "table already exists" on a new database — which is what the Docker `migrate` job runs. They are now idempotent, and a regression test and a CI step cover it.
- Docker image now runs uvicorn with `--proxy-headers`; behind nginx, login throttling previously saw only the proxy's IP and HTTPS/HSTS was not detected.
- `create_admin` documented 12 characters but accepted 8; it now enforces 12, bumps the session version when promoting an existing user, and no longer calls `create_all` on non-SQLite databases.
- Production settings now require an `https://` `PUBLIC_BASE_URL`; the preflight checks it and warns when SMTP is missing.
- Removed 41 accidental duplicate `(2)` files and stale caches from the upload.

## Tests
`python -m pytest backend/tests -q` — includes `test_phase48_account_recovery.py`.

Run the browser checks with `npm run test:e2e`; staff portal tests cover service status, network interruption, login error messaging, refresh rotation, and logout.
