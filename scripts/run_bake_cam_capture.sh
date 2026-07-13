#!/bin/zsh
set -u

if [ "$#" -ne 3 ]; then
  echo "usage: run_bake_cam_capture.sh <session-id> <offset-label> <target-dir>" >&2
  exit 64
fi

SESSION_ID="$1"
OFFSET_LABEL="$2"
TARGET_DIR="$3"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LAGENT="$ROOT_DIR/.venv/bin/lagent"
PYTHON="$ROOT_DIR/.venv/bin/python"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
SAFE_OFFSET="$(printf '%s' "$OFFSET_LABEL" | tr '+:' '__' | tr -cd 'A-Za-z0-9._-')"
LOG_DIR="$TARGET_DIR/logs"
CAPTURE_JSON="$LOG_DIR/${STAMP}_${SAFE_OFFSET}_capture.json"
CAPTURE_ERR="$LOG_DIR/${STAMP}_${SAFE_OFFSET}_capture.err"

mkdir -p "$TARGET_DIR" "$LOG_DIR"
cd "$ROOT_DIR" || exit 1

if ! "$LAGENT" bake-cam capture-now --session "$SESSION_ID" --device DavesDev --camera main --json > "$CAPTURE_JSON.tmp" 2> "$CAPTURE_ERR"; then
  mv "$CAPTURE_JSON.tmp" "$CAPTURE_JSON" 2>/dev/null || true
  "$LAGENT" bake-cam sync --session "$SESSION_ID" --json > "$LOG_DIR/${STAMP}_${SAFE_OFFSET}_sync_after_failure.json" 2>> "$CAPTURE_ERR" || true
  exit 1
fi

mv "$CAPTURE_JSON.tmp" "$CAPTURE_JSON"

IMAGE_PATH="$("$PYTHON" - "$CAPTURE_JSON" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    payload = json.load(handle)
print(payload["capture"]["local_path"])
PY
)"

CAPTURE_ID="$("$PYTHON" - "$CAPTURE_JSON" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    payload = json.load(handle)
print(payload["capture"]["capture_id"])
PY
)"

DEST_BASE="$TARGET_DIR/day7_${SAFE_OFFSET}_${STAMP}_${CAPTURE_ID}"
cp "$IMAGE_PATH" "$DEST_BASE.jpg"

META_PATH="${IMAGE_PATH:r}.json"
if [ -f "$META_PATH" ]; then
  cp "$META_PATH" "$DEST_BASE.json"
fi

cat > "$TARGET_DIR/latest.json" <<EOF
{
  "offset_label": "$OFFSET_LABEL",
  "capture_id": "$CAPTURE_ID",
  "image_path": "$DEST_BASE.jpg",
  "metadata_path": "$DEST_BASE.json",
  "capture_json": "$CAPTURE_JSON",
  "updated_at": "$STAMP"
}
EOF
