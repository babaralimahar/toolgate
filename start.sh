#!/bin/sh
# Container start for single-service hosts (Render free has no pre-deploy hook).
set -e
alembic upgrade head  # idempotent
exec uvicorn toolgate.main:create_app --factory --host 0.0.0.0 --port "${PORT:-8000}" --no-access-log --timeout-graceful-shutdown 20
