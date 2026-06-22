#!/usr/bin/env sh
set -eu

DB_HOST="${DB_HOST:-${PGHOST:-postgres}}"
DB_PORT="${DB_PORT:-${PGPORT:-5432}}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-60}"

run_with_optional_datadog() {
  if [ -n "${APP_BUILD_VERSION:-}" ] && [ -z "${DD_VERSION:-}" ]; then
    export DD_VERSION="$APP_BUILD_VERSION"
  fi

  DATADOG_RUN_ENABLED="${DATADOG_RUN_ENABLED:-${DD_TRACE_ENABLED:-}}"
  if [ -z "$DATADOG_RUN_ENABLED" ]; then
    if [ -n "${DD_AGENT_HOST:-}" ] \
      || [ -n "${DD_TRACE_AGENT_URL:-}" ] \
      || [ -n "${DD_PROFILING_ENABLED:-}" ] \
      || [ -n "${DD_DYNAMIC_INSTRUMENTATION_ENABLED:-}" ] \
      || [ -n "${DD_LLMOBS_ENABLED:-}" ]; then
      DATADOG_RUN_ENABLED=true
    else
      DATADOG_RUN_ENABLED=false
    fi
  fi

  case "$DATADOG_RUN_ENABLED" in
    true | 1 | yes | on)
      ddtrace-run "$@"
      ;;
    *)
      "$@"
      ;;
  esac
}

if [ -n "${PGHOST:-}" ] && [ -n "${PGDATABASE:-}" ] && [ -n "${PGUSER:-}" ] && [ -n "${PGPASSWORD:-}" ]; then
  DATABASE_URL="$(python <<'PY'
from urllib.parse import quote_plus
import os

print(
    "postgresql+psycopg://{user}:{password}@{host}:{port}/{db}".format(
        user=quote_plus(os.environ["PGUSER"]),
        password=quote_plus(os.environ["PGPASSWORD"]),
        host=os.environ["PGHOST"],
        port=os.environ.get("PGPORT", "5432"),
        db=os.environ["PGDATABASE"],
    )
)
PY
)"
  export DATABASE_URL
fi

attempt=1
while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
  if python - "$DB_HOST" "$DB_PORT" <<'PY'
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
try:
    with socket.create_connection((host, port), timeout=1):
        raise SystemExit(0)
except OSError:
    raise SystemExit(1)
PY
  then
    break
  fi

  echo "waiting for postgres at ${DB_HOST}:${DB_PORT} (${attempt}/${MAX_ATTEMPTS})"
  attempt=$((attempt + 1))
  sleep 2
done

if [ "$attempt" -gt "$MAX_ATTEMPTS" ]; then
  echo "postgres did not become reachable in time" >&2
  exit 1
fi

alembic upgrade head

case "${SYNTHETIC_DAILY_ORDERS_ENABLED:-false}" in
  true | 1 | yes | on)
    ;;
  *)
    echo "daily synthetic orders disabled; set SYNTHETIC_DAILY_ORDERS_ENABLED=true to enable mutations"
    while :; do
      sleep "${SYNTHETIC_DAILY_DISABLED_SLEEP_SECONDS:-3600}"
    done
    ;;
esac

run_daily_order_refresh() {
  args="--max-days ${SYNTHETIC_DAILY_MAX_CATCHUP_DAYS:-14} --min-orders ${SYNTHETIC_DAILY_MIN_ORDERS:-25} --max-orders ${SYNTHETIC_DAILY_MAX_ORDERS:-220} --seed ${SYNTHETIC_DAILY_SEED:-20260313}"
  if [ -n "${SYNTHETIC_DAILY_BASE_ORDERS:-}" ]; then
    args="$args --base-orders ${SYNTHETIC_DAILY_BASE_ORDERS}"
  fi
  run_with_optional_datadog python -m app.daily_synthetic_orders $args
}

case "${SYNTHETIC_DAILY_RUN_ON_STARTUP:-true}" in
  true | 1 | yes | on)
    echo "daily synthetic orders running startup catch-up"
    run_daily_order_refresh
    ;;
  *)
    echo "daily synthetic orders startup catch-up disabled"
    ;;
esac

while :; do
  sleep_seconds="$(python <<'PY'
from datetime import datetime, timedelta, timezone
import os

hour = max(0, min(23, int(os.environ.get("SYNTHETIC_DAILY_RUN_HOUR_UTC", "8"))))
now = datetime.now(timezone.utc)
next_run = now.replace(hour=hour, minute=0, second=0, microsecond=0)
if next_run <= now:
    next_run += timedelta(days=1)
print(max(1, int((next_run - now).total_seconds())))
PY
)"
  echo "daily synthetic orders sleeping ${sleep_seconds}s"
  sleep "$sleep_seconds"

  run_daily_order_refresh
done
