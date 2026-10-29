#!/usr/bin/env bash
# Local development server. Production runs through systemd (mailapi.service).
set -euo pipefail
cd "$(dirname "$0")"

if [ -z "${MAILAPI_KEY:-}" ]; then
  export MAILAPI_KEY=dev-key
  echo "MAILAPI_KEY not set; using development key 'dev-key'" >&2
fi
export MAILAPI_DB="${MAILAPI_DB:-$PWD/data/mailapi.db}"
export MAILAPI_SMTP_HOST="${MAILAPI_SMTP_HOST:-127.0.0.1}"
export MAILAPI_SMTP_PORT="${MAILAPI_SMTP_PORT:-25}"

exec "${PYTHON:-python3}" -m uvicorn main:app --host 127.0.0.1 --port "${PORT:-8000}" --reload "$@"
