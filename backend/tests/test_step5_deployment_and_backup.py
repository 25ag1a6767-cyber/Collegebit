from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.modules.retention_backup import (
    _derive_backup_fernet,
    create_encrypted_gfs_backup,
    run_restore_drill,
    verify_encrypted_backup,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_step5_compose_auto_migrates_and_hardens_app():
    compose_text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "profiles: [ops]" not in compose_text
    assert "service_completed_successfully" in compose_text
    assert 'AUTO_CREATE_TABLES: "false"' in compose_text
    assert 'SEED_DEMO_DATA: "false"' in compose_text
    assert "127.0.0.1:${APP_PORT:-8000}:8000" in compose_text

    dockerfile_text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "--workers ${WEB_CONCURRENCY:-1}" in dockerfile_text


def test_step5_nginx_config_has_websocket_and_pwa_headers():
    nginx_conf = (REPO_ROOT / "deploy" / "nginx-https-example.conf").read_text(encoding="utf-8")
    assert "location ~ ^/(api/v1/ws|ws/)" in nginx_conf
    assert "proxy_set_header Upgrade $http_upgrade;" in nginx_conf
    assert "proxy_set_header Connection $connection_upgrade;" in nginx_conf
    assert "location = /sw.js" in nginx_conf
    assert "no-cache, no-store, must-revalidate" in nginx_conf
    assert "Strict-Transport-Security" in nginx_conf


def test_step5_backup_scripts_and_cron_installer_are_valid():
    bkp_mgr = (REPO_ROOT / "scripts" / "backup_manager.py").read_text(encoding="utf-8")
    assert bkp_mgr.startswith("#!/usr/bin/env python3"), "Shebang must be on line 1 without stray characters"

    cron_sh = (REPO_ROOT / "scripts" / "install_backup_cron_ubuntu.sh").read_text(encoding="utf-8")
    assert "gfs-backup" in cron_sh
    assert "restore-drill" in cron_sh
    assert "run_retention_cleanup.py" in cron_sh

    runbook = (REPO_ROOT / "docs" / "DEPLOYMENT-RUNBOOK.md").read_text(encoding="utf-8")
    assert "docker compose up -d --build" in runbook
    assert "Neon" in runbook
    assert "certbot" in runbook


def test_step5_encrypted_gfs_backup_secondary_copy_and_restore_drill(tmp_path, monkeypatch):
    # Ensure TestClient boots DB cleanly first, then point backup dirs to tmp_path
    with TestClient(app) as client:
        assert client.get("/api/ready").status_code == 200

    primary_dir = tmp_path / "primary_gfs"
    secondary_dir = tmp_path / "secondary_offhost"
    monkeypatch.setenv("BACKUP_PRIMARY_DIR", str(primary_dir))
    monkeypatch.setenv("BACKUP_SECONDARY_DIR", str(secondary_dir))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "Step5-Test-Backup-Encryption-Key-32+Chars")

    from app.config import get_settings
    get_settings.cache_clear()
    try:
        with SessionLocal() as db:
            run = create_encrypted_gfs_backup(db)
            assert run["verified"] is True
            primary_file = Path(run["primaryPath"])
            secondary_file = Path(run["secondaryPath"])
            assert primary_file.is_file()
            assert secondary_file.is_file()
            assert primary_file.with_name(primary_file.name + ".json").is_file()
            assert secondary_file.with_name(secondary_file.name + ".json").is_file()

            meta = verify_encrypted_backup(primary_file)
            assert meta["verified"] is True

            # Decrypt payload into an isolated scratch file and verify restored rows are queryable
            fernet = _derive_backup_fernet(os.environ["BACKUP_ENCRYPTION_KEY"])
            decrypted_db = tmp_path / "restored_verify.db"
            decrypted_db.write_bytes(fernet.decrypt(primary_file.read_bytes()))
            conn = sqlite3.connect(decrypted_db)
            try:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                assert integrity == "ok"
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                assert {"users", "orders", "products", "push_subscriptions"}.issubset(tables)
            finally:
                conn.close()

            drill = run_restore_drill(primary_file, db=db)
            assert drill["restoreDrillPassed"] is True
            assert drill["integrityCheck"] == "ok"

            # Verify CLI wrapper scripts/backup_manager.py works for both gfs-backup & restore-drill
            import importlib.util
            import sys
            spec = importlib.util.spec_from_file_location("backup_manager_cli", REPO_ROOT / "scripts" / "backup_manager.py")
            bkm = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bkm)
            monkeypatch.setattr(sys, "argv", ["backup_manager.py", "gfs-backup"])
            assert bkm.main() == 0
            monkeypatch.setattr(sys, "argv", ["backup_manager.py", "restore-drill"])
            assert bkm.main() == 0

            # Verify tampering with encrypted snapshot is caught immediately
            data = bytearray(secondary_file.read_bytes())
            data[-1] ^= 0xFF
            secondary_file.write_bytes(bytes(data))
            try:
                verify_encrypted_backup(secondary_file)
            except Exception as exc:
                assert exc is not None
            else:
                raise AssertionError("Tampered secondary encrypted backup must fail verification")
    finally:
        get_settings.cache_clear()


def test_step5_alembic_postgres_widens_version_column_and_locks():
    env_text = (REPO_ROOT / "backend" / "alembic" / "env.py").read_text(encoding="utf-8")
    assert "_ensure_wide_alembic_version" in env_text
    assert "ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(128)" in env_text
    assert "pg_advisory_xact_lock" in env_text
    assert "version_num VARCHAR(128)" in env_text

    # Confirm revision 0025 (>32 chars) and every other revision fit inside VARCHAR(128)
    import re
    rev_ids = []
    for path in (REPO_ROOT / "backend" / "alembic" / "versions").glob("*.py"):
        m = re.search(r'^revision\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8"), re.M)
        if m:
            rev_ids.append(m.group(1))
    assert any(len(r) > 32 for r in rev_ids)
    assert all(len(r) <= 128 for r in rev_ids)
