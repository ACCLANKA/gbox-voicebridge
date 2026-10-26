#!/usr/bin/env bash
# Bring the ESP32 live end-to-end (one command). Run on the bench machine.
#   ./scripts/hermes-kickoff.sh /path/to/repo /path/to/voicebridge.bundle
set -euo pipefail
REPO="${1:-/opt/voicebridge}"
BUNDLE="${2:-$REPO/voicebridge.bundle}"
PORT="${PORT:-/dev/ttyUSB0}"
SERIAL="${SERIAL:-GBX-BENCH01}"
BASE="${BASE:-http://127.0.0.1:8000}"
KEY="${VOICEBRIDGE_KEY:-}"

echo "== 1. re-sync bundle =="
git -C "$REPO" fetch "$BUNDLE" master:refs/remotes/bundle/master
git -C "$REPO" merge --ff-only refs/remotes/bundle/master
git -C "$REPO" log --oneline -1

echo "== 2. ops channel =="
curl -s --max-time 10 "$BASE/v1/ops/ping" || echo "ops ping failed (deploy /v1/ops first)"
[ -n "$KEY" ] || echo "!! VOICEBRIDGE_KEY not set - mint an ops key, see HERMES-COMMANDS.md"

echo "== 3. bring the board live =="
python3 "$REPO/tools/bring_up.py" --port "$PORT" \
  --app "$REPO/firmware/build/handoff/voicebridge-app.bin" \
  --pt "$REPO/firmware/build/handoff/voicebridge-partition-table.bin" \
  --expect-sha f91841605eabc64a68cc908f60bc56913bdca7bd823c5e5c08f1ed50d7a56058 \
  --base "$BASE" --key "$KEY" --serial "$SERIAL"
