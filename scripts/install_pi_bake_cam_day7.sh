#!/usr/bin/env bash
set -euo pipefail
umask 077

require_env() {
  local name=$1
  [[ -n "${!name:-}" ]] || {
    printf 'ERROR required environment variable is unset: %s\n' "$name" >&2
    exit 64
  }
}

for name in \
  PI_TARGET BAKE_CAM_REMOTE_BASE BAKE_CAM_REMOTE_HOST BAKE_CAM_REMOTE_USER \
  BAKE_CAM_REMOTE_DIR BAKE_CAM_SCHEDULE_FILE BAKE_CAM_BATCH_ID BAKE_CAM_FEEDING_ID \
  BAKE_CAM_FEEDING_TIME_LOCAL BAKE_CAM_SESSION_ID; do
  require_env "$name"
done

[[ "$PI_TARGET" =~ ^[A-Za-z0-9._@:-]+$ ]] || { printf 'ERROR PI_TARGET contains unsafe characters\n' >&2; exit 64; }
[[ "$BAKE_CAM_REMOTE_HOST" =~ ^[A-Za-z0-9._:-]+$ ]] || { printf 'ERROR BAKE_CAM_REMOTE_HOST is invalid\n' >&2; exit 64; }
[[ "$BAKE_CAM_REMOTE_USER" =~ ^[A-Za-z0-9._-]+$ ]] || { printf 'ERROR BAKE_CAM_REMOTE_USER is invalid\n' >&2; exit 64; }
for path in "$BAKE_CAM_REMOTE_BASE" "$BAKE_CAM_REMOTE_DIR"; do
  [[ "$path" == /* && "$path" =~ ^[A-Za-z0-9_./-]+$ && "$path" != *".."* ]] || {
    printf 'ERROR remote paths must be absolute and contain no traversal or whitespace: %s\n' "$path" >&2
    exit 64
  }
done
[[ -f "$BAKE_CAM_SCHEDULE_FILE" && ! -L "$BAKE_CAM_SCHEDULE_FILE" ]] || {
  printf 'ERROR BAKE_CAM_SCHEDULE_FILE must be a regular, non-symlink file\n' >&2
  exit 64
}
for value in "$BAKE_CAM_BATCH_ID" "$BAKE_CAM_FEEDING_ID" "$BAKE_CAM_SESSION_ID"; do
  [[ "$value" =~ ^[A-Za-z0-9._-]+$ ]] || { printf 'ERROR metadata IDs contain unsafe characters\n' >&2; exit 64; }
done
[[ "$BAKE_CAM_FEEDING_TIME_LOCAL" =~ ^[0-9T:+-]+$ ]] || {
  printf 'ERROR BAKE_CAM_FEEDING_TIME_LOCAL must be an ISO-like timestamp\n' >&2
  exit 64
}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNNER="$SCRIPT_DIR/pi_bake_cam_day7_runner.sh"

ssh -o BatchMode=yes -o ConnectTimeout=12 "$PI_TARGET" \
  "mkdir -p '$BAKE_CAM_REMOTE_BASE/spool' '$BAKE_CAM_REMOTE_BASE/done' '$BAKE_CAM_REMOTE_BASE/logs'"
scp -O -q -o BatchMode=yes -o ConnectTimeout=12 "$RUNNER" "$PI_TARGET:$BAKE_CAM_REMOTE_BASE/runner.sh"
scp -O -q -o BatchMode=yes -o ConnectTimeout=12 \
  "$BAKE_CAM_SCHEDULE_FILE" "$PI_TARGET:$BAKE_CAM_REMOTE_BASE/schedule.tsv"

ssh -o BatchMode=yes -o ConnectTimeout=12 "$PI_TARGET" "chmod 700 '$BAKE_CAM_REMOTE_BASE/runner.sh'; \
tmpcron=\$(mktemp); \
crontab -l 2>/dev/null | grep -v '$BAKE_CAM_REMOTE_BASE/runner.sh' > \$tmpcron || true; \
cat >> \$tmpcron <<'EOF'
* * * * * BAKE_CAM_BASE_DIR='$BAKE_CAM_REMOTE_BASE' BAKE_CAM_REMOTE_HOST='$BAKE_CAM_REMOTE_HOST' BAKE_CAM_REMOTE_USER='$BAKE_CAM_REMOTE_USER' BAKE_CAM_REMOTE_DIR='$BAKE_CAM_REMOTE_DIR' BAKE_CAM_BATCH_ID='$BAKE_CAM_BATCH_ID' BAKE_CAM_FEEDING_ID='$BAKE_CAM_FEEDING_ID' BAKE_CAM_FEEDING_TIME_LOCAL='$BAKE_CAM_FEEDING_TIME_LOCAL' BAKE_CAM_SESSION_ID='$BAKE_CAM_SESSION_ID' '$BAKE_CAM_REMOTE_BASE/runner.sh'
EOF
crontab \$tmpcron; rm -f \$tmpcron; crontab -l | tail -5"

printf 'Installed Pi-side bake-cam runner on %s using explicit session configuration\n' "$PI_TARGET"
