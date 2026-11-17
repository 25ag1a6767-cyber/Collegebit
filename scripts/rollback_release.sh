#!/usr/bin/env bash
# Safe Release Rollback Script for ACEEC Physical Server Deployment
set -euo pipefail

PREV_TAG="${1:-HEAD~1}"
DB_ALEMBIC_TARGET="${2:-}"

echo "[CollegeBits Rollback] Checking out application release: ${PREV_TAG}"
git fetch --all --tags
git checkout "${PREV_TAG}"

if [[ -n "${DB_ALEMBIC_TARGET}" ]]; then
  echo "[CollegeBits Rollback] Running Alembic downgrade to ${DB_ALEMBIC_TARGET}"
  docker compose -f deploy/docker-compose.production-3k.yml run --rm api-1 \
    python -m alembic -c backend/alembic.ini downgrade "${DB_ALEMBIC_TARGET}"
fi

echo "[CollegeBits Rollback] Rebuilding and restarting stateless API containers..."
docker compose -f deploy/docker-compose.production-3k.yml up -d --build api-1 api-2 api-3 api-4

echo "[CollegeBits Rollback] Waiting for /api/ready health verification..."
sleep 5
curl -fsS http://127.0.0.1:8000/api/ready
echo ""
echo "[CollegeBits Rollback] Completed cleanly."
