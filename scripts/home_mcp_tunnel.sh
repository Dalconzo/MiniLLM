#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${HOME_MCP_ROOT:-$HOME/MiniLLM}"
PORT="${HOME_MCP_PORT:-8765}"
PROFILE="${HOME_MCP_TUNNEL_PROFILE:-home-mcp}"
URL_FILE="${HOME_MCP_TUNNEL_URL_FILE:-$ROOT_DIR/data/home_mcp/current_tunnel_url.txt}"
LOG_FILE="${HOME_MCP_TUNNEL_LOG_FILE:-$ROOT_DIR/data/logs/home_mcp_tunnel.log}"
ENV_FILE="${HOME_MCP_TUNNEL_CLIENT_ENV_FILE:-$HOME/.config/tunnel-client/home-mcp.env}"
TUNNEL_ID="${HOME_MCP_TUNNEL_ID:-}"
TUNNEL_CLIENT_BIN="${HOME_MCP_TUNNEL_CLIENT_BIN:-$HOME/bin/tunnel-client}"
PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

mkdir -p "$(dirname "$URL_FILE")" "$(dirname "$LOG_FILE")"

HEALTH_URL="http://127.0.0.1:${PORT}/health"

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

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

if [[ -z "${CONTROL_PLANE_API_KEY:-}" ]]; then
  echo "CONTROL_PLANE_API_KEY is missing; create $ENV_FILE or set the environment variable before starting the tunnel-client" >&2
  exit 1
fi

if [[ ! -x "$TUNNEL_CLIENT_BIN" ]]; then
  echo "tunnel-client is not installed or not executable at $TUNNEL_CLIENT_BIN" >&2
  exit 1
fi

if [[ -z "$TUNNEL_ID" ]]; then
  echo "HOME_MCP_TUNNEL_ID is missing; cannot derive the tunnel URL" >&2
  exit 1
fi

printf 'https://api.openai.com/v1/tunnel/%s\n' "$TUNNEL_ID" > "$URL_FILE"
: > "$LOG_FILE"

exec "$TUNNEL_CLIENT_BIN" run --profile "$PROFILE" >>"$LOG_FILE" 2>&1
