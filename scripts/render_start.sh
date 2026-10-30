#!/usr/bin/env sh
# Render start command: apply pending migrations and the catalog seed (idempotent, advisory-locked),
# then serve the API.
set -eu
python scripts/setup_db.py
exec python -m uvicorn habitquest_backend.main:app --host 0.0.0.0 --port "${PORT:-8000}"
