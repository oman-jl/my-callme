#!/usr/bin/env bash
# Start the my-callme server.
#   ./run.sh                     -> 127.0.0.1:8800 (mock provider)
#   MYCALLME_PORT=8899 ./run.sh
# Twilio live mode (export before running):
#   TWILIO_ACCOUNT_SID=... TWILIO_AUTH_TOKEN=... TWILIO_FROM=+1...
#   MYCALLME_PUBLIC_BASE=https://your-tunnel.trycloudflare.com
set -euo pipefail
cd "$(dirname "$0")"
PORT="${MYCALLME_PORT:-8800}"
# Isolated state: this clone never touches the real call-me's ~/.aiphone.
export AIPHONE_DIR="${MYCALLME_STATE_DIR:-$PWD/.state}"
mkdir -p "$AIPHONE_DIR"
exec python3 server.py
