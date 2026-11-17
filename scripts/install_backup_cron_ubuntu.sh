#!/usr/bin/env bash
# Install automated daily encrypted GFS backup, weekly restore drill, and safe 4-day retention cron jobs on Ubuntu.
set -euo pipefail

APP_DIR="${1:-$(pwd)}"
PYTHON_BIN="${APP_DIR}/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3)"
fi

CRON_FILE="/etc/cron.d/collegebits-backups"
LOG_FILE="/var/log/collegebits-backup.log"

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "Run with sudo: sudo bash scripts/install_backup_cron_ubuntu.sh [APP_DIR]" >&2
  exit 1
fi

touch "$LOG_FILE"
chmod 640 "$LOG_FILE"

cat > "$CRON_FILE" <<EOF
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Daily 02:15 IST encrypted GFS snapshot (7 daily / 4 weekly / 3 monthly + secondary off-host copy)
15 2 * * * root cd ${APP_DIR} && set -a && . ./.env && set +a && PYTHONPATH=backend ${PYTHON_BIN} scripts/backup_manager.py gfs-backup >> ${LOG_FILE} 2>&1

# Weekly Sunday 03:00 IST non-destructive encrypted backup decrypt + integrity drill
0 3 * * 0 root cd ${APP_DIR} && set -a && . ./.env && set +a && PYTHONPATH=backend ${PYTHON_BIN} scripts/backup_manager.py restore-drill >> ${LOG_FILE} 2>&1

# Daily 03:30 IST safe 4-day temporary data cleanup (halts automatically if backup health fails)
30 3 * * * root cd ${APP_DIR} && set -a && . ./.env && set +a && PYTHONPATH=backend ${PYTHON_BIN} scripts/run_retention_cleanup.py >> ${LOG_FILE} 2>&1
EOF

chmod 644 "$CRON_FILE"
echo "Installed cron schedule at ${CRON_FILE} (logs: ${LOG_FILE})"
