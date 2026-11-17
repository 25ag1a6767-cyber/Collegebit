# CollegeBits / ACE Canteen — Production Deployment, HTTPS & Backup Runbook

This runbook covers exact, reproducible steps from a clean repository clone for:
1. Clean-clone `docker compose up -d --build`
2. **(a) College Ubuntu Server** (self-hosted PostgreSQL 16 + Redis + Nginx + Let's Encrypt HTTPS)
3. **(b) Cloud VM with Neon Postgres** (Ubuntu VM + Neon pooled PostgreSQL + Redis + Nginx + Let's Encrypt HTTPS)
4. **Automated Encrypted Backups & Tested Restore Procedure**

---

## 0. Clean-Clone Docker Compose Launch (`docker compose up -d --build`)

From a fresh `git clone`:

```bash
git clone <your-repo-url> collegebits && cd collegebits

# 1. Create production environment file (never commit .env)
cp .env.production.example .env

# 2. Generate required cryptographic secrets and passwords
python3 - <<'PY'
import secrets
print("SECRET_KEY=" + secrets.token_urlsafe(48))
print("METRICS_TOKEN=" + secrets.token_urlsafe(32))
print("BACKUP_ENCRYPTION_KEY=" + secrets.token_urlsafe(48))
print("POSTGRES_PASSWORD=" + secrets.token_urlsafe(32))
PY
```

Edit `.env` and fill in:
- `SECRET_KEY`, `METRICS_TOKEN`, `BACKUP_ENCRYPTION_KEY`, `POSTGRES_PASSWORD` (same password as inside `DATABASE_URL`)
- `PUBLIC_BASE_URL=https://canteen.yourcollege.edu`
- `CORS_ORIGINS=https://canteen.yourcollege.edu`
- Razorpay credentials (`RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`)
- Optional Web Push VAPID keys (`VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT`)

Then validate, build, migrate, and boot cleanly:

```bash
# 3. Run production safety preflight before starting
set -a && source .env && set +a
python3 scripts/deployment_preflight.py --require-payment

# 4. Build images, wait for PostgreSQL health, run `alembic upgrade head` via `migrate`, and start `app`
docker compose up -d --build

# 5. Create the initial Administrator account (interactive or via flags; no default passwords ship)
docker compose exec app python -m app.create_admin \
  --email admin@yourcollege.edu \
  --name "Canteen Administrator"

# 6. Verify readiness (loopback only — reverse proxy handles public TLS)
curl -fsS http://127.0.0.1:8000/api/ready
```

---

## 1. Option (a): College Ubuntu Server Deployment (HTTPS + Custom Domain)

Recommended for zero ongoing cloud DB cost on campus LAN or public campus DNS (Ubuntu 22.04/24.04 LTS):

### Step 1 — System Packages, Docker & Firewall
```bash
sudo apt-get update && sudo apt-get upgrade -y
sudo apt-get install -y ca-certificates curl git nginx certbot python3-certbot-nginx postgresql-client ufw

# Install Docker Engine + Compose plugin (skip if running native systemd + Ubuntu PostgreSQL 16)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
newgrp docker

# Lock down firewall: allow only SSH, HTTP (for ACME challenge), and HTTPS
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

*(Alternative without Docker: run `sudo CAMPUSBITE_DB_PASSWORD='<32+ char password>' bash deploy/postgres-college/install_college_postgres_ubuntu.sh`, install Python 3.12 venv + `pip install -r backend/requirements.txt`, run `PYTHONPATH=backend alembic -c backend/alembic.ini upgrade head`, and run `uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 --proxy-headers` under a `systemd` unit.)*

### Step 2 — Configure Domain & Reverse Proxy TLS (`Nginx + Let's Encrypt`)
Point your college DNS `A` record (e.g. `canteen.yourcollege.edu`) to the Ubuntu server's public IP, then:

```bash
sudo cp deploy/nginx-https-example.conf /etc/nginx/sites-available/collegebits
sudo sed -i 's/canteen\.example\.edu/canteen.yourcollege.edu/g' /etc/nginx/sites-available/collegebits
sudo ln -sf /etc/nginx/sites-available/collegebits /etc/nginx/sites-enabled/collegebits
sudo rm -f /etc/nginx/sites-enabled/default

# Temporarily obtain TLS cert (or obtain cert first with `certbot certonly --nginx -d canteen.yourcollege.edu`)
sudo certbot certonly --nginx -d canteen.yourcollege.edu --non-interactive --agree-tos -m it-admin@yourcollege.edu
sudo nginx -t && sudo systemctl reload nginx

# Verify auto-renewal timer
sudo systemctl status certbot.timer
```

Ensure `.env` has `HTTPS_TERMINATED=true`, `PUBLIC_BASE_URL=https://canteen.yourcollege.edu`, and `CORS_ORIGINS=https://canteen.yourcollege.edu`. The Nginx config forwards WebSocket `Upgrade`/`Connection` headers for `/api/v1/ws` and sets strict `no-cache` headers on `/sw.js`.

### Step 3 — Start Stack & Verify HTTPS
```bash
docker compose up -d --build
docker compose exec app python -m app.create_admin --email admin@yourcollege.edu --name "Canteen Administrator"
curl -fsS https://canteen.yourcollege.edu/api/ready
```

---

## 2. Option (b): Cloud VM + Neon PostgreSQL Deployment (HTTPS + Custom Domain)

Use this when running a small Ubuntu cloud VM (1–2 vCPU) backed by managed **Neon PostgreSQL**:

### Step 1 — Create Neon Database & Collect Two URLs
In your Neon dashboard:
1. Create a database `ace_canteen` and role `ace_canteen`.
2. Copy **Pooled connection string** (`-pooler` hostname with `?sslmode=require`) for the app runtime.
3. Copy **Direct (unpooled) connection string** for `alembic upgrade head` and `pg_dump` backups.

### Step 2 — Provision Cloud Ubuntu VM (Redis + App + Nginx + Certbot)
```bash
sudo apt-get update && sudo apt-get install -y python3-venv python3-pip python3-dev libpq-dev postgresql-client redis-server nginx certbot python3-certbot-nginx ufw git
sudo systemctl enable --now redis-server nginx
sudo ufw allow OpenSSH && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw enable

git clone <your-repo-url> /opt/collegebits && cd /opt/collegebits
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r backend/requirements.txt

cp .env.production.example .env
# Edit .env:
#   ENVIRONMENT=production
#   PUBLIC_BASE_URL=https://canteen.yourdomain.com
#   CORS_ORIGINS=https://canteen.yourdomain.com
#   HTTPS_TERMINATED=true
#   AUTO_CREATE_TABLES=false
#   SEED_DEMO_DATA=false
#   DATABASE_URL=postgresql+psycopg://USER:PASS@ep-xxx-pooler.REGION.aws.neon.tech/ace_canteen?sslmode=require
#   DB_USE_PGBOUNCER_TRANSACTION_POOL=true
#   REDIS_URL=redis://127.0.0.1:6379/0
#   SECRET_KEY=... METRICS_TOKEN=... BACKUP_ENCRYPTION_KEY=...
```

### Step 3 — Run Alembic Migrations Against Direct Neon URL & Create Admin
```bash
set -a && source .env && set +a
PYTHONPATH=backend DATABASE_URL="postgresql+psycopg://USER:PASS@ep-xxx.REGION.aws.neon.tech/ace_canteen?sslmode=require" \
  ./.venv/bin/alembic -c backend/alembic.ini upgrade head

./.venv/bin/python -m pip list >/dev/null
PYTHONPATH=backend ./.venv/bin/python -m app.create_admin --email admin@yourdomain.com --name "Canteen Admin"
python3 scripts/deployment_preflight.py --require-payment
```

### Step 4 — Create systemd Service (`/etc/systemd/system/collegebits.service`)
```ini
[Unit]
Description=CollegeBits FastAPI Service
After=network-online.target redis-server.service

[Service]
User=www-data
Group=www-data
WorkingDirectory=/opt/collegebits
EnvironmentFile=/opt/collegebits/.env
Environment=PYTHONPATH=/opt/collegebits/backend
ExecStart=/opt/collegebits/.venv/bin/uvicorn app.main:app --app-dir /opt/collegebits/backend --host 127.0.0.1 --port 8000 --proxy-headers --workers 2
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Enable & verify behind Nginx HTTPS (same Nginx template as Option (a)):
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now collegebits
sudo cp deploy/nginx-https-example.conf /etc/nginx/sites-available/collegebits
sudo sed -i 's/canteen\.example\.edu/canteen.yourdomain.com/g' /etc/nginx/sites-available/collegebits
sudo ln -sf /etc/nginx/sites-available/collegebits /etc/nginx/sites-enabled/collegebits
sudo certbot --nginx -d canteen.yourdomain.com --non-interactive --agree-tos -m ops@yourdomain.com
sudo nginx -t && sudo systemctl reload nginx
curl -fsS https://canteen.yourdomain.com/api/ready
```

---

## 3. Automated Backups & Tested Restore Procedure

### Enable Automated Cron Backups (Daily Encrypted GFS + Weekly Restore Drill)
Set `BACKUP_ENCRYPTION_KEY` and `BACKUP_SECONDARY_DIR=/mnt/campus-nas/collegebits-backups` in `.env`, then run:

```bash
sudo bash scripts/install_backup_cron_ubuntu.sh /opt/collegebits
```

If deploying with `docker compose`, schedule host cron against the container/DB instead:
```bash
# Daily encrypted GFS snapshot via compose app container (02:15)
15 2 * * * root cd /opt/collegebits && docker compose exec -T app python scripts/backup_manager.py gfs-backup >> /var/log/collegebits-backup.log 2>&1
# Weekly restore verification drill (Sunday 03:00)
0 3 * * 0 root cd /opt/collegebits && docker compose exec -T app python scripts/backup_manager.py restore-drill >> /var/log/collegebits-backup.log 2>&1
```

### Manual Backup & Non-Destructive Restore Drill Verification
```bash
# 1. Create consistent backup (PostgreSQL custom/SQL or SQLite online snapshot) + SHA-256 manifest
PYTHONPATH=backend ./.venv/bin/python scripts/backup_manager.py backup --database-url "$DATABASE_URL" --output-dir backups

# 2. Verify checksum & dump validity
PYTHONPATH=backend ./.venv/bin/python scripts/backup_manager.py verify backups/<backup-file>

# 3. Encrypted GFS backup + automatic off-host secondary replication + decrypt/catalog restore drill
PYTHONPATH=backend ./.venv/bin/python scripts/backup_manager.py gfs-backup
PYTHONPATH=backend ./.venv/bin/python scripts/backup_manager.py restore-drill
```

### Tested End-to-End Restore Procedure (Before Go-Live)
Always test a full restore into an isolated target database (never overwrite a healthy live DB without a safety snapshot first):

1. **PostgreSQL restore into a scratch test database:**
   ```bash
   # Decrypt an encrypted .enc GFS snapshot cleanly (verified against its .enc.json SHA-256 manifest)
   PYTHONPATH=backend ./.venv/bin/python - <<'PY'
   from pathlib import Path
   from app.modules.retention_backup import verify_encrypted_backup, _derive_backup_fernet
   enc = sorted(Path("backups/gfs/daily").glob("campusbite-*.enc"))[-1]
   assert verify_encrypted_backup(enc)["verified"] is True
   Path("restore_test.sql").write_bytes(_derive_backup_fernet().decrypt(enc.read_bytes()))
   print("Decrypted:", enc)
   PY

   # Restore into a separate verification DB and inspect row counts
   createdb -U ace_canteen ace_canteen_restore_test
   psql -v ON_ERROR_STOP=1 -U ace_canteen -d ace_canteen_restore_test -f restore_test.sql
   psql -U ace_canteen -d ace_canteen_restore_test -c "SELECT COUNT(*) AS users FROM users; SELECT COUNT(*) AS orders FROM orders;"
   dropdb -U ace_canteen ace_canteen_restore_test
   rm -f restore_test.sql
   ```
2. **SQLite restore (local/staging verification):**
   ```bash
   # Restores verified snapshot atomically after archiving previous target as *.pre-restore-*
   PYTHONPATH=backend ./.venv/bin/python scripts/backup_manager.py restore-sqlite backups/<file>.db --destination /tmp/restore-verify.db --confirm
   ```
