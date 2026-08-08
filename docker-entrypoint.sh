#!/bin/sh
set -eu

echo "applying migrations ..."
alembic upgrade head

# One worker by default. On a 512MB free-tier instance a second worker doubles
# the memory for no throughput gain -- every request here is bounded by
# Postgres, not by Python. Raise WEB_CONCURRENCY on real hardware.
exec uvicorn accord.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-1}" \
  --proxy-headers \
  --forwarded-allow-ips '*'
