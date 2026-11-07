"""CampusBite AI v2.0 — Four-Day Hybrid Data Retention & Encrypted GFS Backup / Disaster Recovery Module.

Implements:
  1. Encrypted GFS Backups (7 daily, 4 weekly, 3 monthly) with Fernet authenticated encryption,
     SHA-256 manifests, off-host secondary storage copy, and non-destructive restore-drill verification.
  2. Hybrid Archival into permanent `daily_sales_archives` rollups.
  3. Four-Day Temporary Data Cleanup strictly gated on verified backup health:
     - Deletes ONLY eligible 4-day temporary records (expired sessions, expired QR tokens, old idempotency keys,
       delivered outbox events, old read non-financial notifications, temporary API logs, old AI telemetry hashes,
       expired temp export files).
     - NEVER deletes orders, payments, refunds, receipts, customer accounts, inventory movements, suppliers,
       purchase orders, or audit logs.
     - Automatically HALTS destructive cleanup if backup health verification fails.
"""
from __future__ import annotations

import base64
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
from typing import Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.database import IS_POSTGRES, IS_SQLITE, SQLALCHEMY_DATABASE_URL, get_db
from app.envelope import ApiError, ok
from app.models import (
    AIInteraction,
    BackupRun,
    CheckoutIdempotency,
    DailySalesArchive,
    InventoryMovement,
    InventoryMovementType,
    Notification,
    OperationalExpense,
    Order,
    OrderStatus,
    OutboxEvent,
    PaymentStatus,
    PickupToken,
    RefreshSession,
    Refund,
    RetentionCleanupRun,
    StockMovement,
    TemporaryApiLog,
    User,
    rupees_to_paise,
)
from app.modules.common import as_utc, parse_json_safe, record_audit
from app.security import require_roles

logger = logging.getLogger("ace_canteen.retention_backup")
router = APIRouter(prefix="/admin", tags=["v1-admin-retention-backup"])

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TMP_EXPORTS_DIR = PROJECT_ROOT / "backend" / "data" / "tmp_exports"

PROTECTED_BUSINESS_TABLES = [
    "users",
    "orders",
    "order_items",
    "order_status_history",
    "payments",
    "refunds",
    "payment_webhook_events",
    "pickups",
    "inventory_movements",
    "stock_movements",
    "suppliers",
    "purchase_orders",
    "product_batches",
    "batch_reservations",
    "operational_expenses",
    "daily_sales_archives",
    "audit_events",
]

TEMPORARY_FOUR_DAY_CATEGORIES = [
    "refresh_sessions (expired or revoked >= retention_days)",
    "pickup_tokens (expired or used >= retention_days on non-active orders)",
    "checkout_idempotency (completed/expired >= retention_days)",
    "outbox_events (delivered >= retention_days)",
    "notifications (read non-financial alerts >= retention_days)",
    "temporary_api_logs (diagnostic logs >= retention_days)",
    "ai_interactions (hash-only telemetry >= retention_days)",
    "tmp_exports (temporary export files >= retention_days)",
]

def _resolve_dir(configured: str) -> Path:
    p = Path(configured)
    return p if p.is_absolute() else (PROJECT_ROOT / p)

def _derive_backup_fernet(explicit_key: str | None = None) -> Fernet:
    cfg = get_settings()
    secret_material = (explicit_key or cfg.backup_encryption_key or cfg.secret_key).encode("utf-8")
    derived = hashlib.pbkdf2_hmac("sha256", secret_material, b"campusbite-v2-backup-fernet-salt", 310_000, dklen=32)
    return Fernet(base64.urlsafe_b64encode(derived))

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def _sqlite_path_from_url(db_url: str) -> Path:
    raw = db_url.replace("sqlite:///", "", 1)
    if raw == ":memory:":
        raise ValueError("In-memory SQLite database cannot be backed up to disk.")
    p = Path(unquote(raw))
    return p if p.is_absolute() else (PROJECT_ROOT / p)

def _dump_raw_database_bytes(db_url: str | None = None) -> tuple[bytes, str]:
    """Capture consistent raw snapshot bytes and return (raw_bytes, engine_name)."""
    target_url = db_url or SQLALCHEMY_DATABASE_URL
    if target_url.startswith("sqlite:///"):
        src = _sqlite_path_from_url(target_url)
        if not src.is_file():
            raise FileNotFoundError(f"SQLite database file not found: {src}")
        with tempfile.TemporaryDirectory() as td:
            snap = Path(td) / "snapshot.db"
            src_conn = sqlite3.connect(f"file:{src.resolve().as_posix()}?mode=ro", uri=True)
            dst_conn = sqlite3.connect(snap)
            try:
                src_conn.backup(dst_conn)
                dst_conn.commit()
            finally:
                dst_conn.close()
                src_conn.close()
            chk = sqlite3.connect(f"file:{snap.resolve().as_posix()}?mode=ro", uri=True)
            try:
                res = chk.execute("PRAGMA integrity_check").fetchone()
                if not res or res[0] != "ok":
                    raise RuntimeError(f"SQLite snapshot failed integrity check: {res}")
            finally:
                chk.close()
            return snap.read_bytes(), "sqlite"

    if target_url.startswith(("postgresql://", "postgres://", "postgresql+psycopg://")):
        pg_url = (
            target_url.replace("postgresql+psycopg://", "postgresql://", 1)
            .replace("postgres://", "postgresql://", 1)
        )
        with tempfile.TemporaryDirectory() as td:
            dump_file = Path(td) / "dump.sql"
            proc = subprocess.run(
                ["pg_dump", "--no-owner", "--no-privileges", "--dbname", pg_url, "--file", str(dump_file)],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError("pg_dump failed; verify PostgreSQL client tools and connectivity.")
            raw = dump_file.read_bytes()
            if len(raw) < 100 or (b"PostgreSQL database dump" not in raw[:4096] and b"CREATE " not in raw[:4096]):
                raise RuntimeError("pg_dump output failed structural validation.")
            return raw, "postgresql"

    raise ValueError("Unsupported database URL scheme for backup.")

def _prune_tier(tier_dir: Path, keep_count: int) -> list[str]:
    if not tier_dir.exists():
        return []
    enc_files = sorted(tier_dir.glob("campusbite-*.enc"), key=lambda p: p.name, reverse=True)
    removed: list[str] = []
    for stale in enc_files[keep_count:]:
        manifest = stale.with_name(stale.name + ".json")
        stale.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        removed.append(stale.name)
    return removed

def create_encrypted_gfs_backup(
    db: Session | None = None,
    *,
    db_url: str | None = None,
    primary_dir: Path | None = None,
    secondary_dir: Path | None = None,
    encryption_key: str | None = None,
    actor_user_id: int | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Create an encrypted GFS backup (7 daily, 4 weekly, 3 monthly) + off-host copy + manifest."""
    cfg = get_settings()
    now = now_utc or datetime.now(timezone.utc)
    p_root = primary_dir or _resolve_dir(cfg.backup_primary_dir)
    s_root = secondary_dir or (_resolve_dir(cfg.backup_secondary_dir) if cfg.backup_secondary_dir else None)

    raw_bytes, engine_name = _dump_raw_database_bytes(db_url)
    raw_sha256 = _sha256_bytes(raw_bytes)
    fernet = _derive_backup_fernet(encryption_key)
    encrypted_bytes = fernet.encrypt(raw_bytes)
    enc_sha256 = _sha256_bytes(encrypted_bytes)

    stamp = now.strftime("%Y%m%d-%H%M%S-%f")
    ext = "db" if engine_name == "sqlite" else "sql"
    base_filename = f"campusbite-{engine_name}-{stamp}.{ext}.enc"

    tiers_written: list[str] = ["daily"]
    iso_year, iso_week, _ = now.isocalendar()
    weekly_dir = p_root / "weekly"
    monthly_dir = p_root / "monthly"
    weekly_marker = f"W{iso_year}{iso_week:02d}"
    monthly_marker = f"M{now.strftime('%Y%m')}"

    if now.weekday() == 6 or not list(weekly_dir.glob(f"*{weekly_marker}*.enc")):
        tiers_written.append("weekly")
    if now.day == 1 or not list(monthly_dir.glob(f"*{monthly_marker}*.enc")):
        tiers_written.append("monthly")

    primary_daily_path: Path | None = None
    secondary_daily_path: Path | None = None
    pruned: dict[str, list[str]] = {}

    for tier in tiers_written:
        tag = f"-{weekly_marker}" if tier == "weekly" else (f"-{monthly_marker}" if tier == "monthly" else "")
        fname = base_filename.replace(".enc", f"{tag}.enc")
        t_dir = p_root / tier
        t_dir.mkdir(parents=True, exist_ok=True)
        dest = t_dir / fname
        dest.write_bytes(encrypted_bytes)
        manifest_payload = {
            "format_version": 2,
            "application": "CampusBite AI v2.0",
            "created_at": now.isoformat(),
            "tier": tier,
            "database_engine": engine_name,
            "encrypted": True,
            "cipher": "Fernet-AES128CBC-HMACSHA256-PBKDF2",
            "file": fname,
            "encrypted_size_bytes": len(encrypted_bytes),
            "encrypted_sha256": enc_sha256,
            "plaintext_size_bytes": len(raw_bytes),
            "plaintext_sha256": raw_sha256,
        }
        dest.with_name(fname + ".json").write_text(json.dumps(manifest_payload, indent=2) + "\n", encoding="utf-8")
        if tier == "daily":
            primary_daily_path = dest

        if s_root is not None:
            st_dir = s_root / tier
            st_dir.mkdir(parents=True, exist_ok=True)
            s_dest = st_dir / fname
            shutil.copy2(dest, s_dest)
            shutil.copy2(dest.with_name(fname + ".json"), s_dest.with_name(fname + ".json"))
            if tier == "daily":
                secondary_daily_path = s_dest

    pruned["daily"] = _prune_tier(p_root / "daily", cfg.backup_keep_daily)
    pruned["weekly"] = _prune_tier(p_root / "weekly", cfg.backup_keep_weekly)
    pruned["monthly"] = _prune_tier(p_root / "monthly", cfg.backup_keep_monthly)
    if s_root is not None:
        _prune_tier(s_root / "daily", cfg.backup_keep_daily)
        _prune_tier(s_root / "weekly", cfg.backup_keep_weekly)
        _prune_tier(s_root / "monthly", cfg.backup_keep_monthly)

    assert primary_daily_path is not None
    verify_info = verify_encrypted_backup(primary_daily_path, encryption_key=encryption_key)
    independent_offhost = bool(s_root is not None and s_root.resolve() != p_root.resolve())

    result = {
        "createdAt": now.isoformat(),
        "databaseEngine": engine_name,
        "tiersWritten": tiers_written,
        "primaryPath": str(primary_daily_path),
        "secondaryPath": str(secondary_daily_path) if secondary_daily_path else None,
        "independentSecondaryConfigured": independent_offhost,
        "encryptedSizeBytes": len(encrypted_bytes),
        "sha256": enc_sha256,
        "verified": bool(verify_info["verified"]),
        "prunedOldBackups": pruned,
    }

    if db is not None:
        run_row = BackupRun(
            created_at=now,
            tier=",".join(tiers_written),
            database_engine=engine_name,
            primary_path=str(primary_daily_path),
            secondary_path=str(secondary_daily_path) if secondary_daily_path else None,
            size_bytes=len(encrypted_bytes),
            sha256=enc_sha256,
            encrypted=True,
            verified=True,
            restore_tested=False,
            status="completed",
            detail=f"Tiers: {','.join(tiers_written)}; offhost={independent_offhost}",
            actor_user_id=actor_user_id,
        )
        db.add(run_row)
        record_audit(
            db,
            "backup.gfs_created",
            "backup",
            primary_daily_path.name,
            actor_user_id,
            {"tiers": tiers_written, "bytes": len(encrypted_bytes), "offhost": independent_offhost},
        )
        db.commit()
        result["backupRunId"] = run_row.id

    return result

def verify_encrypted_backup(backup_enc_path: Path, *, encryption_key: str | None = None) -> dict[str, Any]:
    """Verify manifest, encrypted SHA-256, authenticated Fernet decryption, and plaintext SHA-256."""
    manifest_path = backup_enc_path.with_name(backup_enc_path.name + ".json")
    if not backup_enc_path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError(f"Backup or manifest missing for {backup_enc_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    enc_bytes = backup_enc_path.read_bytes()
    if len(enc_bytes) != int(manifest.get("encrypted_size_bytes", -1)):
        raise ValueError("Encrypted backup size mismatch against manifest.")
    if _sha256_bytes(enc_bytes) != manifest.get("encrypted_sha256"):
        raise ValueError("Encrypted backup SHA-256 checksum mismatch.")
    fernet = _derive_backup_fernet(encryption_key)
    try:
        plaintext = fernet.decrypt(enc_bytes)
    except InvalidToken as exc:
        raise ValueError("Encrypted backup failed authenticated decryption (wrong key or tampered ciphertext).") from exc
    if _sha256_bytes(plaintext) != manifest.get("plaintext_sha256"):
        raise ValueError("Decrypted payload SHA-256 mismatch.")
    return {"verified": True, "manifest": manifest, "plaintextBytes": len(plaintext)}

def run_restore_drill(
    backup_enc_path: Path,
    *,
    db: Session | None = None,
    encryption_key: str | None = None,
    actor_user_id: int | None = None,
) -> dict[str, Any]:
    """Non-destructive monthly/on-demand restore verification drill into isolated temporary storage."""
    manifest_path = backup_enc_path.with_name(backup_enc_path.name + ".json")
    if not backup_enc_path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError("Encrypted backup or manifest not found.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fernet = _derive_backup_fernet(encryption_key)
    plaintext = fernet.decrypt(backup_enc_path.read_bytes())
    if _sha256_bytes(plaintext) != manifest.get("plaintext_sha256"):
        raise ValueError("Plaintext checksum mismatch during restore drill.")

    engine_name = manifest.get("database_engine", "sqlite")
    details: dict[str, Any] = {"engine": engine_name, "file": backup_enc_path.name}

    with tempfile.TemporaryDirectory() as td:
        if engine_name == "sqlite":
            restored_db = Path(td) / "restore_drill.db"
            restored_db.write_bytes(plaintext)
            conn = sqlite3.connect(f"file:{restored_db.resolve().as_posix()}?mode=ro", uri=True)
            try:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()
                if not integrity or integrity[0] != "ok":
                    raise RuntimeError(f"Restored SQLite failed integrity_check: {integrity}")
                tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                details["tableCount"] = len(tables)
                details["integrityCheck"] = "ok"
            finally:
                conn.close()
        else:
            sql_text = plaintext.decode("utf-8", errors="replace")
            create_count = sql_text.count("CREATE TABLE")
            if create_count == 0 and "PostgreSQL database dump" not in sql_text[:4096]:
                raise RuntimeError("Decrypted PostgreSQL dump did not contain expected DDL structures.")
            details["createTableStatements"] = create_count
            details["integrityCheck"] = "sql_structure_verified"

    if db is not None:
        row = db.scalar(select(BackupRun).where(BackupRun.primary_path == str(backup_enc_path)).order_by(BackupRun.id.desc()))
        if row is not None:
            row.restore_tested = True
            row.verified = True
        record_audit(db, "backup.restore_drill_passed", "backup", backup_enc_path.name, actor_user_id, details)
        db.commit()

    return {"restoreDrillPassed": True, **details}

def check_backup_health(
    *,
    primary_dir: Path | None = None,
    max_age_hours: int | None = None,
    encryption_key: str | None = None,
    now_utc: datetime | None = None,
) -> tuple[bool, str]:
    """Verify a recent, cryptographically valid daily backup exists before allowing 4-day cleanup."""
    cfg = get_settings()
    now = now_utc or datetime.now(timezone.utc)
    p_root = primary_dir or _resolve_dir(cfg.backup_primary_dir)
    max_hours = max_age_hours if max_age_hours is not None else cfg.backup_max_age_hours

    daily_dir = p_root / "daily"
    if not daily_dir.exists():
        return False, f"Primary daily backup directory '{daily_dir}' does not exist."
    candidates = sorted(daily_dir.glob("campusbite-*.enc"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return False, "No encrypted daily backup files found."

    latest = candidates[0]
    try:
        verified = verify_encrypted_backup(latest, encryption_key=encryption_key)
        created_raw = verified["manifest"].get("created_at")
        created_dt = datetime.fromisoformat(created_raw)
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)
        age_hours = (now - created_dt).total_seconds() / 3600.0
        if age_hours > max_hours:
            return False, f"Latest verified backup '{latest.name}' is {age_hours:.1f}h old (exceeds {max_hours}h threshold)."
        return True, f"Healthy verified backup '{latest.name}' ({age_hours:.1f}h old)."
    except Exception as exc:
        return False, f"Latest backup '{latest.name}' failed verification: {exc}"

def archive_daily_sales_rollups(db: Session, *, up_to_date: date | None = None) -> int:
    """Upsert permanent aggregated daily financial & operational statistics into `daily_sales_archives`."""
    tz = ZoneInfo(get_settings().canteen_timezone)
    target_end = up_to_date or datetime.now(tz).date()

    orders = db.scalars(select(Order).options(joinedload(Order.items).joinedload(Order.items.property.mapper.class_.product))).unique().all()
    refunds = db.scalars(select(Refund).where(Refund.status == "PROCESSED")).all()
    prod_waste = db.scalars(select(InventoryMovement).options(joinedload(InventoryMovement.product)).where(InventoryMovement.movement_type == InventoryMovementType.WASTE)).all()
    ing_waste = db.scalars(select(StockMovement).options(joinedload(StockMovement.ingredient)).where(StockMovement.movement_type == "wastage")).all()
    op_expenses = db.scalars(select(OperationalExpense)).all()

    dates_seen: set[date] = set()
    orders_by_date: dict[date, list[Order]] = {}
    for o in orders:
        dt = as_utc(o.created_at)
        d = dt.astimezone(tz).date() if dt else target_end
        if d <= target_end:
            dates_seen.add(d)
            orders_by_date.setdefault(d, []).append(o)

    refund_by_date: dict[date, int] = {}
    for r in refunds:
        dt = as_utc(r.created_at)
        d = dt.astimezone(tz).date() if dt else target_end
        if d <= target_end:
            dates_seen.add(d)
            refund_by_date[d] = refund_by_date.get(d, 0) + int(r.amount_paise or rupees_to_paise(r.amount))

    waste_by_date: dict[date, int] = {}
    for mv in prod_waste:
        dt = as_utc(mv.created_at)
        d = dt.astimezone(tz).date() if dt else target_end
        if d <= target_end:
            dates_seen.add(d)
            unit_cost_p = (mv.product.cost_paise if mv.product and mv.product.cost_paise else (mv.product.effective_price_paise if mv.product else 0))
            waste_by_date[d] = waste_by_date.get(d, 0) + abs(mv.quantity_delta) * int(unit_cost_p)

    for mv in ing_waste:
        dt = as_utc(mv.created_at)
        d = dt.astimezone(tz).date() if dt else target_end
        if d <= target_end and mv.ingredient and mv.ingredient.cost_per_unit is not None:
            dates_seen.add(d)
            loss_p = rupees_to_paise(abs(Decimal(mv.quantity_delta)) * Decimal(mv.ingredient.cost_per_unit))
            waste_by_date[d] = waste_by_date.get(d, 0) + loss_p

    exp_by_date: dict[date, int] = {}
    for ex in op_expenses:
        if ex.expense_date <= target_end:
            dates_seen.add(ex.expense_date)
            exp_by_date[ex.expense_date] = exp_by_date.get(ex.expense_date, 0) + int(ex.amount_paise)

    upserted = 0
    for d in sorted(dates_seen):
        day_orders = orders_by_date.get(d, [])
        paid = [o for o in day_orders if o.payment_status == PaymentStatus.PAID]
        completed = [o for o in day_orders if o.status in {OrderStatus.COMPLETED, OrderStatus.COLLECTED}]
        cancelled = [o for o in day_orders if o.status in {OrderStatus.CANCELLED, OrderStatus.REFUND_PENDING}]
        uniq_cust = len({o.user_id for o in paid})

        gross_p = sum(int(o.subtotal_paise or rupees_to_paise(o.subtotal)) for o in paid)
        disc_p = sum(int(o.discount_paise or rupees_to_paise(o.discount_amount)) for o in paid)
        tax_p = sum(int(o.tax_paise or rupees_to_paise(o.tax_amount)) for o in paid)
        net_p = sum(int(o.effective_total_paise) for o in paid)

        cogs_p = 0
        for o in paid:
            for it in o.items:
                p_cost = int(it.product.cost_paise) if (it.product and it.product.cost_paise) else 0
                cogs_p += p_cost * it.quantity

        ref_p = refund_by_date.get(d, 0)
        wst_p = waste_by_date.get(d, 0)
        opex_p = exp_by_date.get(d, 0)
        profit_p = net_p - tax_p - ref_p - cogs_p - wst_p - opex_p

        row = db.scalar(select(DailySalesArchive).where(DailySalesArchive.archive_date == d, DailySalesArchive.counter_key == 0))
        if row is None:
            row = DailySalesArchive(archive_date=d, counter_id=None, counter_key=0)
            db.add(row)
        row.total_orders = len(day_orders)
        row.paid_orders = len(paid)
        row.completed_orders = len(completed)
        row.cancelled_orders = len(cancelled)
        row.unique_customers = uniq_cust
        row.gross_revenue_paise = gross_p
        row.discount_paise = disc_p
        row.tax_paise = tax_p
        row.net_revenue_paise = net_p
        row.refund_paise = ref_p
        row.cogs_paise = cogs_p
        row.wastage_loss_paise = wst_p
        row.operating_expense_paise = opex_p
        row.estimated_profit_paise = profit_p
        upserted += 1

    db.flush()
    return upserted

def run_four_day_retention_cleanup(
    db: Session,
    *,
    retention_days: int | None = None,
    dry_run: bool = False,
    primary_backup_dir: Path | None = None,
    encryption_key: str | None = None,
    actor_user_id: int | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Execute the 4-day hybrid retention policy safely.

    1. Archives daily business rollups into `daily_sales_archives`.
    2. Verifies backup health — if backup health fails, HALTS destructive cleanup immediately.
    3. Deletes ONLY eligible temporary records older than `retention_days` (default 4 days),
       strictly protecting active orders and all permanent financial/business/audit records.
    """
    cfg = get_settings()
    now = now_utc or datetime.now(timezone.utc)
    days = retention_days if retention_days is not None else cfg.temp_retention_days
    cutoff = now - timedelta(days=days)

    run_log = RetentionCleanupRun(
        started_at=now,
        retention_days=days,
        status="running",
        actor_user_id=actor_user_id,
    )
    db.add(run_log)
    db.flush()

    try:
        archived_days = archive_daily_sales_rollups(db, up_to_date=now.date())
        backup_ok, backup_msg = check_backup_health(
            primary_dir=primary_backup_dir,
            encryption_key=encryption_key,
            now_utc=now,
        )
        run_log.archived_days_count = archived_days
        run_log.backup_healthy = backup_ok
        run_log.backup_detail = backup_msg[:255]

        active_order_ids_subq = select(Order.id).where(
            Order.status.in_([
                OrderStatus.PENDING,
                OrderStatus.CONFIRMED,
                OrderStatus.ACCEPTED,
                OrderStatus.PREPARING,
                OrderStatus.READY,
            ])
        )

        sess_cond = or_(
            RefreshSession.expires_at < cutoff,
            (RefreshSession.revoked_at.is_not(None)) & (RefreshSession.revoked_at < cutoff),
        )
        qr_cond = (
            or_(
                PickupToken.expires_at < cutoff,
                (PickupToken.used_at.is_not(None)) & (PickupToken.used_at < cutoff),
            )
            & PickupToken.order_id.notin_(active_order_ids_subq)
        )
        idem_cond = (CheckoutIdempotency.created_at < cutoff) & (CheckoutIdempotency.state != "processing")
        outbox_cond = (OutboxEvent.delivered_at.is_not(None)) & (OutboxEvent.delivered_at < cutoff)
        notif_cond = (
            (Notification.is_read.is_(True))
            & (Notification.created_at < cutoff)
            & (Notification.notification_type.notin_(["payment", "payment_alert", "refund"]))
        )
        api_log_cond = TemporaryApiLog.created_at < cutoff
        ai_cond = AIInteraction.created_at < cutoff

        eligible_counts = {
            "expired_refresh_sessions": int(db.scalar(select(func.count(RefreshSession.id)).where(sess_cond)) or 0),
            "expired_pickup_tokens": int(db.scalar(select(func.count(PickupToken.id)).where(qr_cond)) or 0),
            "expired_checkout_idempotency": int(db.scalar(select(func.count(CheckoutIdempotency.id)).where(idem_cond)) or 0),
            "delivered_outbox_events": int(db.scalar(select(func.count(OutboxEvent.id)).where(outbox_cond)) or 0),
            "expired_read_notifications": int(db.scalar(select(func.count(Notification.id)).where(notif_cond)) or 0),
            "temporary_api_logs": int(db.scalar(select(func.count(TemporaryApiLog.id)).where(api_log_cond)) or 0),
            "temporary_ai_telemetry": int(db.scalar(select(func.count(AIInteraction.id)).where(ai_cond)) or 0),
            "temporary_export_files": 0,
        }

        expired_files: list[Path] = []
        if TMP_EXPORTS_DIR.exists():
            for f in TMP_EXPORTS_DIR.iterdir():
                if f.is_file() and datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc) < cutoff:
                    expired_files.append(f)
        eligible_counts["temporary_export_files"] = len(expired_files)

        if dry_run:
            run_log.status = "dry_run"
            run_log.deleted_counts_json = json.dumps(eligible_counts)
            run_log.completed_at = datetime.now(timezone.utc)
            db.commit()
            return {
                "runId": run_log.id,
                "status": "dry_run",
                "retentionDays": days,
                "cutoffUtc": cutoff.isoformat(),
                "backupHealthy": backup_ok,
                "backupDetail": backup_msg,
                "archivedDaysCount": archived_days,
                "eligibleCounts": eligible_counts,
                "deletedCounts": {},
                "protectedBusinessTables": PROTECTED_BUSINESS_TABLES,
            }

        if not backup_ok:
            run_log.status = "aborted_unhealthy_backup"
            run_log.deleted_counts_json = json.dumps({})
            run_log.error_message = f"Destructive cleanup halted: {backup_msg}"[:500]
            run_log.completed_at = datetime.now(timezone.utc)
            record_audit(
                db,
                "retention.aborted_unhealthy_backup",
                "retention",
                run_log.id,
                actor_user_id,
                {"backup_detail": backup_msg, "eligible_skipped": eligible_counts},
            )
            db.commit()
            return {
                "runId": run_log.id,
                "status": "aborted_unhealthy_backup",
                "retentionDays": days,
                "cutoffUtc": cutoff.isoformat(),
                "backupHealthy": False,
                "backupDetail": backup_msg,
                "archivedDaysCount": archived_days,
                "eligibleCounts": eligible_counts,
                "deletedCounts": {},
                "protectedBusinessTables": PROTECTED_BUSINESS_TABLES,
            }

        deleted = {
            "expired_refresh_sessions": db.execute(delete(RefreshSession).where(sess_cond)).rowcount or 0,
            "expired_pickup_tokens": db.execute(delete(PickupToken).where(qr_cond)).rowcount or 0,
            "expired_checkout_idempotency": db.execute(delete(CheckoutIdempotency).where(idem_cond)).rowcount or 0,
            "delivered_outbox_events": db.execute(delete(OutboxEvent).where(outbox_cond)).rowcount or 0,
            "expired_read_notifications": db.execute(delete(Notification).where(notif_cond)).rowcount or 0,
            "temporary_api_logs": db.execute(delete(TemporaryApiLog).where(api_log_cond)).rowcount or 0,
            "temporary_ai_telemetry": db.execute(delete(AIInteraction).where(ai_cond)).rowcount or 0,
            "temporary_export_files": 0,
        }
        removed_files = 0
        for f in expired_files:
            f.unlink(missing_ok=True)
            removed_files += 1
        deleted["temporary_export_files"] = removed_files

        run_log.status = "completed"
        run_log.deleted_counts_json = json.dumps(deleted)
        run_log.completed_at = datetime.now(timezone.utc)
        record_audit(
            db,
            "retention.cleanup_completed",
            "retention",
            run_log.id,
            actor_user_id,
            {"retention_days": days, "archived_days": archived_days, "deleted": deleted},
        )
        db.commit()
        return {
            "runId": run_log.id,
            "status": "completed",
            "retentionDays": days,
            "cutoffUtc": cutoff.isoformat(),
            "backupHealthy": True,
            "backupDetail": backup_msg,
            "archivedDaysCount": archived_days,
            "deletedCounts": deleted,
            "protectedBusinessTables": PROTECTED_BUSINESS_TABLES,
        }
    except Exception as exc:
        db.rollback()
        logger.exception("Retention cleanup failed")
        fail_row = RetentionCleanupRun(
            started_at=now,
            completed_at=datetime.now(timezone.utc),
            retention_days=days,
            status="failed",
            backup_healthy=False,
            error_message=str(exc)[:500],
            actor_user_id=actor_user_id,
        )
        db.add(fail_row)
        db.commit()
        raise

class RetentionRunIn(BaseModel):
    retentionDays: int | None = Field(default=None, ge=1, le=365)
    dryRun: bool = False

class RestoreDrillIn(BaseModel):
    backupFilePath: str | None = Field(default=None, max_length=500)

@router.get("/retention/status")
def get_retention_status_v1(
    request: Request,
    _: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    cfg = get_settings()
    backup_ok, backup_msg = check_backup_health()
    runs = db.scalars(select(RetentionCleanupRun).order_by(RetentionCleanupRun.id.desc()).limit(20)).all()
    archives = db.scalars(select(DailySalesArchive).where(DailySalesArchive.counter_key == 0).order_by(DailySalesArchive.archive_date.desc()).limit(30)).all()
    return ok(
        request,
        {
            "tempRetentionDays": cfg.temp_retention_days,
            "backupHealthGateActive": True,
            "backupHealthy": backup_ok,
            "backupDetail": backup_msg,
            "temporaryCategories": TEMPORARY_FOUR_DAY_CATEGORIES,
            "protectedBusinessTables": PROTECTED_BUSINESS_TABLES,
            "recentCleanupRuns": [
                {
                    "id": r.id,
                    "startedAt": as_utc(r.started_at),
                    "completedAt": as_utc(r.completed_at),
                    "retentionDays": r.retention_days,
                    "status": r.status,
                    "backupHealthy": r.backup_healthy,
                    "backupDetail": r.backup_detail,
                    "archivedDaysCount": r.archived_days_count,
                    "deletedCounts": parse_json_safe(r.deleted_counts_json, {}),
                    "errorMessage": r.error_message,
                }
                for r in runs
            ],
            "dailySalesArchives": [
                {
                    "date": a.archive_date.isoformat(),
                    "totalOrders": a.total_orders,
                    "paidOrders": a.paid_orders,
                    "netRevenuePaise": a.net_revenue_paise,
                    "estimatedProfitPaise": a.estimated_profit_paise,
                }
                for a in archives
            ],
        },
    )

@router.post("/retention/run")
def trigger_retention_run_v1(
    payload: RetentionRunIn,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    res = run_four_day_retention_cleanup(
        db,
        retention_days=payload.retentionDays,
        dry_run=payload.dryRun,
        actor_user_id=admin.id,
    )
    return ok(request, res)

@router.get("/backups")
def list_backups_v1(
    request: Request,
    _: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    cfg = get_settings()
    p_root = _resolve_dir(cfg.backup_primary_dir)
    s_root = _resolve_dir(cfg.backup_secondary_dir) if cfg.backup_secondary_dir else None
    inventory: dict[str, list[dict[str, Any]]] = {"daily": [], "weekly": [], "monthly": []}
    for tier in ("daily", "weekly", "monthly"):
        t_dir = p_root / tier
        if t_dir.exists():
            for enc in sorted(t_dir.glob("campusbite-*.enc"), key=lambda p: p.name, reverse=True):
                inventory[tier].append({
                    "file": enc.name,
                    "path": str(enc),
                    "sizeBytes": enc.stat().st_size,
                    "modifiedAt": datetime.fromtimestamp(enc.stat().st_mtime, tz=timezone.utc).isoformat(),
                })
    runs = db.scalars(select(BackupRun).order_by(BackupRun.id.desc()).limit(25)).all()
    backup_ok, backup_msg = check_backup_health()
    return ok(
        request,
        {
            "policy": {
                "keepDaily": cfg.backup_keep_daily,
                "keepWeekly": cfg.backup_keep_weekly,
                "keepMonthly": cfg.backup_keep_monthly,
                "maxHealthyAgeHours": cfg.backup_max_age_hours,
                "primaryDir": str(p_root),
                "secondaryDir": str(s_root) if s_root else None,
                "independentSecondaryConfigured": bool(s_root and s_root.resolve() != p_root.resolve()),
            },
            "backupHealthy": backup_ok,
            "backupDetail": backup_msg,
            "inventory": inventory,
            "recentRuns": [
                {
                    "id": r.id,
                    "createdAt": as_utc(r.created_at),
                    "tier": r.tier,
                    "databaseEngine": r.database_engine,
                    "primaryPath": r.primary_path,
                    "secondaryPath": r.secondary_path,
                    "sizeBytes": r.size_bytes,
                    "sha256": r.sha256,
                    "encrypted": r.encrypted,
                    "verified": r.verified,
                    "restoreTested": r.restore_tested,
                    "status": r.status,
                }
                for r in runs
            ],
        },
    )

@router.post("/backups/run", status_code=201)
def trigger_gfs_backup_v1(
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    try:
        res = create_encrypted_gfs_backup(db, actor_user_id=admin.id)
    except Exception as exc:
        raise ApiError(500, "BACKUP_FAILED", f"Encrypted GFS backup failed: {exc}")
    return ok(request, res, status_code=201)

@router.post("/backups/restore-test")
def trigger_restore_drill_v1(
    payload: RestoreDrillIn,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    cfg = get_settings()
    p_root = _resolve_dir(cfg.backup_primary_dir)
    if payload.backupFilePath:
        candidate = Path(payload.backupFilePath)

        resolved = candidate.resolve()
        if p_root.resolve() not in resolved.parents:
            raise ApiError(403, "INVALID_BACKUP_PATH", "Restore drill path must reside inside the configured primary backup directory.")
    else:
        daily_files = sorted((p_root / "daily").glob("campusbite-*.enc"), key=lambda p: p.name, reverse=True) if (p_root / "daily").exists() else []
        if not daily_files:
            raise ApiError(404, "NO_BACKUPS_FOUND", "Run an encrypted backup first before executing a restore drill.")
        resolved = daily_files[0]

    try:
        res = run_restore_drill(resolved, db=db, actor_user_id=admin.id)
    except Exception as exc:
        raise ApiError(500, "RESTORE_DRILL_FAILED", f"Restore verification drill failed: {exc}")
    return ok(request, res)
