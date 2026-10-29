#!/usr/bin/env sh
# Render start command: apply pending migrations (idempotent, advisory-locked), then serve the API.
# With RUN_WORKER_IN_API=true the planning worker polls on a thread inside this process.
set -eu
python scripts/setup_db.py
exec python -m uvicorn habitquest_backend.main:app --host 0.0.0.0 --port "${PORT:-8000}"
