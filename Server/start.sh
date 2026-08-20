#!/usr/bin/env bash
# IoT Graduate — Server startup script
# Starts MediaMTX (Docker) then the aiohttp dashboard server.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── 1. MediaMTX via Docker Compose ───────────────────────────
echo "[start.sh] Starting MediaMTX..."
docker compose -f docker-compose.media.yml up -d
echo "[start.sh] MediaMTX started."

# ── 2. Python dashboard server ────────────────────────────────
echo "[start.sh] Checking/installing dependencies..."
pip install -q -r requirements.txt || true
echo "[start.sh] Starting dashboard server..."
exec python3 app.py "$@"
