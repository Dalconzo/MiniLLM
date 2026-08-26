#!/usr/bin/env bash
set -u
umask 077

require_env() {
  local name=$1
  if [[ -z "${!name:-}" ]]; then
    printf 'ERROR required environment variable is unset: %s\n' "$name" >&2
    exit 64
  fi
}

for name in \
  BAKE_CAM_BASE_DIR BAKE_CAM_REMOTE_USER BAKE_CAM_REMOTE_HOST BAKE_CAM_REMOTE_DIR \
  BAKE_CAM_BATCH_ID BAKE_CAM_FEEDING_ID BAKE_CAM_FEEDING_TIME_LOCAL BAKE_CAM_SESSION_ID; do
  require_env "$name"
done

BASE_DIR="$BAKE_CAM_BASE_DIR"
SPOOL_DIR="$BASE_DIR/spool"
DONE_DIR="$BASE_DIR/done"
LOG_DIR="$BASE_DIR/logs"
SCHEDULE_FILE="$BASE_DIR/schedule.tsv"
REMOTE_USER="$BAKE_CAM_REMOTE_USER"
REMOTE_HOST="$BAKE_CAM_REMOTE_HOST"
REMOTE_DIR="$BAKE_CAM_REMOTE_DIR"
DEVICE="${BAKE_CAM_DEVICE:-/dev/video0}"

[[ "$BASE_DIR" == /* && "$BASE_DIR" =~ ^[A-Za-z0-9_./-]+$ && "$BASE_DIR" != *".."* ]] || {
  printf 'ERROR BAKE_CAM_BASE_DIR is unsafe\n' >&2
  exit 64
}
[[ "$REMOTE_DIR" == /* && "$REMOTE_DIR" =~ ^[A-Za-z0-9_./-]+$ && "$REMOTE_DIR" != *".."* ]] || {
  printf 'ERROR BAKE_CAM_REMOTE_DIR is unsafe\n' >&2
  exit 64
}
[[ "$REMOTE_USER" =~ ^[A-Za-z0-9._-]+$ && "$REMOTE_HOST" =~ ^[A-Za-z0-9._:-]+$ ]] || {
  printf 'ERROR remote identity is unsafe\n' >&2
  exit 64
}
for value in "$BAKE_CAM_BATCH_ID" "$BAKE_CAM_FEEDING_ID" "$BAKE_CAM_SESSION_ID"; do
  [[ "$value" =~ ^[A-Za-z0-9._-]+$ ]] || { printf 'ERROR metadata ID is unsafe\n' >&2; exit 64; }
done
[[ "$BAKE_CAM_FEEDING_TIME_LOCAL" =~ ^[0-9T:+-]+$ ]] || {
  printf 'ERROR BAKE_CAM_FEEDING_TIME_LOCAL is unsafe\n' >&2
  exit 64
}
[[ "$DEVICE" =~ ^/dev/video[0-9]+$ ]] || { printf 'ERROR BAKE_CAM_DEVICE is unsafe\n' >&2; exit 64; }

mkdir -p "$SPOOL_DIR" "$DONE_DIR" "$LOG_DIR"

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$LOG_DIR/runner.log"
}

capture_due() {
  local now_epoch
  now_epoch="$(date +%s)"

  while IFS=$'\t' read -r offset due_epoch due_local; do
    [ -z "${offset:-}" ] && continue
    case "$offset" in \#*) continue ;; esac

    if [[ ! "$offset" =~ ^t\+[0-9]+[hms]$ || ! "$due_epoch" =~ ^[0-9]+$ || ! "$due_local" =~ ^[0-9T:+-]+$ ]]; then
      log "invalid_schedule_row offset=$offset"
      continue
    fi

    local safe_offset
    safe_offset="$(printf '%s' "$offset" | tr '+:' '__' | tr -cd 'A-Za-z0-9._-')"
    local marker="$DONE_DIR/$safe_offset.captured"
    if [ -f "$marker" ] || [ "$now_epoch" -lt "$due_epoch" ]; then
      continue
    fi

    local stamp image meta
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    image="$SPOOL_DIR/capture_${safe_offset}_${stamp}.jpg"
    meta="$SPOOL_DIR/capture_${safe_offset}_${stamp}.json"

    log "capture_due offset=$offset due_local=$due_local image=$image"
    if timeout 20s ffmpeg -hide_banner -loglevel error -y -f v4l2 -input_format mjpeg -video_size 1280x720 -i "$DEVICE" -frames:v 1 "$image" \
      || timeout 20s ffmpeg -hide_banner -loglevel error -y -f v4l2 -video_size 640x480 -i "$DEVICE" -frames:v 1 "$image"; then
      cat > "$meta" <<EOF
{
  "activity_type": "starter_feeding",
  "batch_id": "$BAKE_CAM_BATCH_ID",
  "captured_at_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "device": "$(hostname | tr -cd 'A-Za-z0-9._-')",
  "feeding_id": "$BAKE_CAM_FEEDING_ID",
  "feeding_time_local": "$BAKE_CAM_FEEDING_TIME_LOCAL",
  "image_path": "$image",
  "offset_label": "$offset",
  "scheduled_for_local": "$due_local",
  "session_id": "$BAKE_CAM_SESSION_ID",
  "upload_status": "pending"
}
EOF
      chmod 600 "$image" "$meta"
      touch "$marker"
      log "capture_ok offset=$offset image=$image"
    else
      rm -f "$image" "$meta"
      log "capture_failed offset=$offset"
    fi
  done < "$SCHEDULE_FILE"
}

upload_spool() {
  shopt -s nullglob
  for image in "$SPOOL_DIR"/*.jpg; do
    local meta base
    meta="${image%.jpg}.json"
    base="$(basename "$image")"
    log "upload_attempt image=$image remote=$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/$base"
    if scp -O -q \
      -o BatchMode=yes \
      -o ConnectTimeout=12 \
      -o ServerAliveInterval=5 \
      -o ServerAliveCountMax=2 \
      "$image" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/$base" \
      && { [ ! -f "$meta" ] || scp -O -q -o BatchMode=yes -o ConnectTimeout=12 "$meta" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/$(basename "$meta")"; }; then
      mv "$image" "$DONE_DIR/$base"
      [ ! -f "$meta" ] || mv "$meta" "$DONE_DIR/$(basename "$meta")"
      log "upload_ok image=$base"
    else
      log "upload_failed image=$base"
    fi
  done
}

if [ ! -f "$SCHEDULE_FILE" ]; then
  log "missing_schedule_file path=$SCHEDULE_FILE"
  exit 1
fi

capture_due
upload_spool
