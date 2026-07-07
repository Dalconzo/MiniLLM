#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${HOME_MCP_ROOT:-$HOME/MiniLLM}"
PORT="${HOME_MCP_PORT:-8765}"
URL_FILE="${HOME_MCP_TUNNEL_URL_FILE:-$ROOT_DIR/data/home_mcp/current_tunnel_url.txt}"
LOG_FILE="${HOME_MCP_TUNNEL_LOG_FILE:-$ROOT_DIR/data/logs/home_mcp_tunnel.log}"
HEALTH_URL="http://127.0.0.1:${PORT}/health"
PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

mkdir -p "$(dirname "$URL_FILE")" "$(dirname "$LOG_FILE")"

for _ in $(seq 1 60); do
  if curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null 2>&1; then
  echo "home-mcp health check failed at $HEALTH_URL" >&2
  exit 1
fi

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared is not installed or not on PATH" >&2
  exit 1
fi

: > "$LOG_FILE"

cloudflared tunnel --url "http://127.0.0.1:${PORT}" --no-autoupdate >>"$LOG_FILE" 2>&1 &
cloud_pid=$!

cleanup() {
  kill "$cloud_pid" 2>/dev/null || true
  wait "$cloud_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

(
  while kill -0 "$cloud_pid" 2>/dev/null; do
    url="$(grep -oE 'https://[A-Za-z0-9.-]+\.trycloudflare\.com' "$LOG_FILE" | tail -n 1 || true)"
    if [[ -n "$url" ]]; then
      printf '%s\n' "$url" > "$URL_FILE"
    fi
    sleep 2
  done
) &
watcher_pid=$!

wait "$cloud_pid"
status=$?
kill "$watcher_pid" 2>/dev/null || true
wait "$watcher_pid" 2>/dev/null || true
exit "$status"
