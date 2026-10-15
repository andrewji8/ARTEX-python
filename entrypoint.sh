"""entrypoint.sh — Docker entrypoint for ARTEX-python with supervisor integration."""

#!/usr/bin/env bash
set -e

# ---------------------------------------------------------------------------
# Wait for PostgreSQL to be ready
# ---------------------------------------------------------------------------
echo "Waiting for PostgreSQL at ${DATABASE_URL}..."
until PGPASSWORD="${DB_PASS:-artex_pass}" psql -h postgres -U "${DB_USER:-artex_user}" -d "${DB_NAME:-artex_db}" -c '\q' 2>/dev/null; do
    echo "Postgres is unavailable — sleeping"
    sleep 2
done
echo "PostgreSQL is up"

# ---------------------------------------------------------------------------
# Run Alembic migrations
# ---------------------------------------------------------------------------
echo "Running Alembic migrations..."
alembic upgrade head

# ---------------------------------------------------------------------------
# Determine whether to run supervisor or the app directly
# ---------------------------------------------------------------------------
if [ "${RUN_SUPERVISOR:-false}" = "true" ]; then
    echo "Starting supervisor mode — monitoring ${GITHUB_REPO} for updates"
    exec python supervisor.py \
        --docker-image "${DOCKER_IMAGE:-artex-python:latest}" \
        --repo "${GITHUB_REPO:-your-org/artex-python}" \
        --interval "${UPDATE_CHECK_INTERVAL:-300}" \
        --drain-timeout "${DRAIN_TIMEOUT:-120}"
else
    echo "Starting Uvicorn directly"
    exec uvicorn src.main:app \
        --host 0.0.0.0 \
        --port 8000 \
        --workers 4 \
        --log-level info
fi
