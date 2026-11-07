#!/usr/bin/env bash
# CampusBite AI v2.0 — Zero-Subscription College-Owned PostgreSQL + PgBouncer + Redis + Nginx Installer (Ubuntu/Debian)
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root (sudo bash deploy/postgres-college/install_college_postgres_ubuntu.sh)"
  exit 1
fi

APP_DB_PASS="${CAMPUSBITE_DB_PASSWORD:-}"
if [[ ${#APP_DB_PASS} -lt 24 ]]; then
  echo "ERROR: Export CAMPUSBITE_DB_PASSWORD (at least 24 chars) before running."
  exit 1
fi

apt-get update
apt-get install -y postgresql postgresql-contrib pgbouncer redis-server nginx python3-venv python3-pip

mkdir -p /var/backups/campusbite-wal /var/backups/campusbite-primary /mnt/college-nas/campusbite-backups
chown -R postgres:postgres /var/backups/campusbite-wal
chmod 700 /var/backups/campusbite-wal

cp deploy/postgres-college/postgresql-college.conf /etc/postgresql/*/main/conf.d/99-campusbite.conf
systemctl restart postgresql

sudo -u postgres psql -v app_password="'${APP_DB_PASS}'" -f deploy/postgres-college/init_campusbite_db.sql

systemctl enable --now postgresql pgbouncer redis-server nginx
echo "Self-hosted College PostgreSQL 16 + PgBouncer + Redis + Nginx ready (zero paid database subscription required)."
