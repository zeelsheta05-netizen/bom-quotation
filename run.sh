#!/usr/bin/env bash
# Start the BOM Quotation server on http://localhost:8000
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
if [ -f .env ]; then set -a; . ./.env; set +a; fi
exec .venv/bin/uvicorn backend.app:app --host 127.0.0.1 --port "${PORT:-8000}" "$@"
