# CollegeBits — Backup, 4-Day Hybrid Retention & Disaster Recovery Runbook

## 1. Backup Architecture (7 Daily · 4 Weekly · 3 Monthly Encrypted GFS)

CollegeBits protects all financial, inventory, user, and audit data through [`backend/app/modules/retention_backup.py`](../backend/app/modules/retention_backup.py) and [`scripts/backup_manager.py`](../scripts/backup_manager.py):

- **Schedule & GFS Rotation:**
  - **Daily (`backups/gfs/daily/`):** Retains the most recent **7 daily** encrypted snapshots.
  - **Weekly (`backups/gfs/weekly/`):** Automatically promotes Sunday snapshots (or initializes the first weekly snapshot) and retains **4 weekly** backups.
  - **Monthly (`backups/gfs/monthly/`):** Automatically promotes 1st-of-month snapshots and retains **3 monthly** backups.
- **Authenticated Encryption:** Every snapshot is encrypted at rest using **Fernet (`AES-128-CBC` + `HMAC-SHA256`)** with keys derived via `PBKDF2-HMAC-SHA256` (310,000 iterations) from `BACKUP_ENCRYPTION_KEY`.
- **Tamper-Evident Manifests:** Every `.enc` file is paired with a `.manifest.json` storing both `rawSha256` and `encryptedSha256`. Any single-bit corruption or tampering fails `verify_encrypted_backup()`.
- **Independent Secondary Off-Host Copy:** Set `BACKUP_SECONDARY_DIR` in `.env` to a separate NAS share, external USB volume, or mounted campus storage path (e.g., `/mnt/campus-nas/campusbite-backups` or `D:\CampusBiteBackups`). Every backup automatically replicates both `.enc` and `.manifest.json` to secondary storage.
- **PostgreSQL Continuous WAL Archiving (PITR):** [`deploy/postgres-college/postgresql-college.conf`](../deploy/postgres-college/postgresql-college.conf) enables `archive_mode = on` and `wal_level = replica` for point-in-time recovery between daily base snapshots.

---

## 2. Four-Day Hybrid Data Retention Policy & Backup-Health Safety Gate

Configured via `TEMP_RETENTION_DAYS=4` and executable from **Admin Console → Tab 9** or `python scripts/run_retention_cleanup.py`:

### A. Mandatory Pre-Cleanup Safety Gate
Before deleting a single row, `run_four_day_retention_cleanup()` calls `check_backup_health()`:
1. Verifies that a recent encrypted backup exists within `BACKUP_MAX_AGE_HOURS` (default `36` hours).
2. Re-verifies the ciphertext SHA-256, decrypts the payload in memory, and verifies the plaintext SHA-256.
3. **If backup verification fails or is missing, cleanup immediately halts (`status = "aborted_unhealthy_backup"`) and zero rows are deleted.**

### B. Hybrid Rollup Archival
Before pruning temporary tables, historical daily totals (orders, paid orders, gross/discount/tax/net revenue, COGS, expenses, wastage, refunds, net profit) are upserted into `daily_sales_archives`.

### C. Strict Separation of Temporary vs Permanent Records

| Purged After 4 Days (Temporary Operational Artifacts ONLY) | NEVER Deleted (Protected Permanent Records) |
| :--- | :--- |
| Expired or revoked `refresh_sessions` | `orders`, `order_items`, `order_status_history` |
| Expired or used `pickup_tokens` on non-active orders | `payments`, `refunds`, `payment_webhook_events` |
| Completed/expired `checkout_idempotency` keys | `pickups` (verified pickup audit records) |
| Delivered `outbox_events` | `users` (students, staff, admins) |
| Read non-financial `notifications` | `inventory_movements`, `stock_movements`, `product_batches` |
| Diagnostic `temporary_api_logs` & hash-only `ai_interactions` | `suppliers`, `purchase_orders`, `operational_expenses` |
| Temporary export scratch files (`backend/data/tmp_exports`) | `daily_sales_archives`, `audit_events` |

---

## 3. Routine CLI & Admin Console Operations

### Create an Encrypted GFS Backup Manually
```powershell
$env:PYTHONPATH = "backend"
.\.venv\Scripts\python.exe scripts\backup_manager.py gfs-backup
```

### Run a Non-Destructive Restore Verification Drill
Decrypts the latest `.enc` snapshot into an isolated temporary directory, verifies SHA-256 checksums, runs `PRAGMA integrity_check` (SQLite) or `pg_restore --list` catalog verification (PostgreSQL), marks `restore_tested = True` in `backup_runs`, and deletes the temporary scratch copy without touching live data:
```powershell
$env:PYTHONPATH = "backend"
.\.venv\Scripts\python.exe scripts\backup_manager.py restore-drill
```

### Preview or Execute 4-Day Temporary Cleanup
```powershell
$env:PYTHONPATH = "backend"
.\.venv\Scripts\python.exe scripts\run_retention_cleanup.py --dry-run
.\.venv\Scripts\python.exe scripts\run_retention_cleanup.py
```

---

## 4. Emergency Full Database Restore Procedure

1. **Place Canteen in Maintenance Mode** (or stop the `app` service so no new writes arrive):
   ```bash
   docker compose stop app
   ```
2. **Locate the Latest Verified `.enc` Snapshot** in `backups/gfs/daily/` (or `BACKUP_SECONDARY_DIR` if the primary disk failed).
3. **Decrypt & Verify Checksums:**
   ```python
   from pathlib import Path
   from app.modules.retention_backup import verify_encrypted_backup, _derive_backup_fernet

   enc_path = Path("backups/gfs/daily/<latest-file>.enc")
   meta = verify_encrypted_backup(enc_path)
   assert meta["verified"] is True
   raw_bytes = _derive_backup_fernet().decrypt(enc_path.read_bytes())
   Path("restored_dump.bin").write_bytes(raw_bytes)
   ```
4. **Restore into PostgreSQL 16 (Custom Format Dump):**
   ```bash
   pg_restore --clean --if-exists --no-owner -U campusbite_app -d campusbite_prod restored_dump.bin
   rm restored_dump.bin
   ```
   *(For local SQLite development environments, copy `restored_dump.bin` over `backend/ace_canteen.db`.)*
5. **Run Schema Head Verification & Restart Application:**
   ```bash
   PYTHONPATH=backend alembic -c backend/alembic.ini upgrade head
   docker compose start app
   ```
6. **Verify `/api/health` and Admin Reconciliation (`/admin` → Tab 7).**
