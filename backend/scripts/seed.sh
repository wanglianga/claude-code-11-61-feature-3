#!/usr/bin/env bash
# Reset the demo schema and reseed fictional data (local development helper).
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-.venv/bin/python}"
"$PYTHON" - <<'PY'
from app.db import drop_schema, init_db, SessionLocal
from app.seed import seed, wait_for_db, demo_state

wait_for_db()
drop_schema()
init_db()
with SessionLocal() as db:
    seed(db)
print("seed done:", demo_state())
PY
