#!/bin/zsh
set -u

if [ "$#" -ne 5 ]; then
  echo "usage: run_bake_cam_launchd_job.sh <label> <plist-path> <session-id> <offset-label> <target-dir>" >&2
  exit 64
fi

LABEL="$1"
PLIST_PATH="$2"
SESSION_ID="$3"
OFFSET_LABEL="$4"
TARGET_DIR="$5"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
UID_VALUE="$(id -u)"

"$SCRIPT_DIR/run_bake_cam_capture.sh" "$SESSION_ID" "$OFFSET_LABEL" "$TARGET_DIR"
STATUS="$?"

launchctl bootout "gui/$UID_VALUE" "$PLIST_PATH" >/dev/null 2>&1 || true
rm -f "$PLIST_PATH"

echo "$LABEL exited with status $STATUS"
exit "$STATUS"
