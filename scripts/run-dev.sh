#!/bin/bash
set -eu
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
npm --prefix "${PROJECT_DIR}/frontend" run build
exec python3 "${PROJECT_DIR}/backend/main.py" \
    --host 127.0.0.1 \
    --port "${MEIZANG_PORT:-8787}" \
    --database "${MEIZANG_DATABASE:-${PROJECT_DIR}/data/library.db}" \
    --static "${PROJECT_DIR}/frontend/dist"
