#!/bin/bash
set -u

BASE_DIR="${BAKE_CAM_BASE_DIR:-$HOME/bake_cam/day7}"
SPOOL_DIR="$BASE_DIR/spool"
DONE_DIR="$BASE_DIR/done"
LOG_DIR="$BASE_DIR/logs"
SCHEDULE_FILE="$BASE_DIR/schedule.tsv"
REMOTE_USER="${BAKE_CAM_REMOTE_USER:-daviddalconzo}"
REMOTE_HOST="${BAKE_CAM_REMOTE_HOST:-100.96.213.25}"
REMOTE_DIR="${BAKE_CAM_REMOTE_DIR:-/Users/daviddalconzo/MiniLLM/data/home_mcp/recipes/images/starters/day-7}"
DEVICE="${BAKE_CAM_DEVICE:-/dev/video0}"

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

    local marker="$DONE_DIR/$offset.captured"
    if [ -f "$marker" ]; then
      continue
    fi
    if [ "$now_epoch" -lt "$due_epoch" ]; then
      continue
    fi

    local stamp safe_offset image meta
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    safe_offset="$(printf '%s' "$offset" | tr '+:' '__' | tr -cd 'A-Za-z0-9._-')"
    image="$SPOOL_DIR/day7_${safe_offset}_${stamp}.jpg"
    meta="$SPOOL_DIR/day7_${safe_offset}_${stamp}.json"

    log "capture_due offset=$offset due_local=$due_local image=$image"
    if timeout 20s ffmpeg -hide_banner -loglevel error -y -f v4l2 -input_format mjpeg -video_size 1280x720 -i "$DEVICE" -frames:v 1 "$image" \
      || timeout 20s ffmpeg -hide_banner -loglevel error -y -f v4l2 -video_size 640x480 -i "$DEVICE" -frames:v 1 "$image"; then
      cat > "$meta" <<EOF
{
  "activity_type": "starter_feeding",
  "batch_id": "starter-day-7",
  "captured_at_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "device": "$(hostname)",
  "feeding_id": "day-7",
  "feeding_time_local": "2026-07-12T17:30:00-07:00",
  "image_path": "$image",
  "offset_label": "$offset",
  "scheduled_for_local": "$due_local",
  "session_id": "20260712-sourdough-starter-day-7-feeding-e30eac",
  "upload_status": "pending"
}
EOF
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
