#!/usr/bin/env sh
set -eu

DB_HOST="${DB_HOST:-${PGHOST:-postgres}}"
DB_PORT="${DB_PORT:-${PGPORT:-5432}}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-60}"
UVICORN_RELOAD="${UVICORN_RELOAD:-false}"

if [ -z "${DATABASE_URL:-}" ] && [ -n "${PGHOST:-}" ] && [ -n "${PGDATABASE:-}" ] && [ -n "${PGUSER:-}" ] && [ -n "${PGPASSWORD:-}" ]; then
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

if python <<'PY'
import os

from sqlalchemy import create_engine, text

database_url = os.environ["DATABASE_URL"]
engine = create_engine(database_url, future=True)

with engine.connect() as connection:
    alembic_version = connection.execute(text("select to_regclass('public.alembic_version')")).scalar()
    synthetic_runs = connection.execute(text("select to_regclass('public.synthetic_runs')")).scalar()

if alembic_version is None and synthetic_runs is not None:
    raise SystemExit(0)

raise SystemExit(1)
PY
then
  echo "existing schema detected without alembic version table; stamping head"
  alembic stamp head
fi

alembic upgrade head
if [ "$UVICORN_RELOAD" = "true" ]; then
  exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
