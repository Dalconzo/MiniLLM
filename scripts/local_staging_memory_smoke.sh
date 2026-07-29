#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${ROOT_DIR}/.venv/bin/python"
CONFIG="${ROOT_DIR}/config/agent.local-staging.yaml"
PORT="${LAGENT_STAGING_HOME_MCP_PORT:-8876}"
URL="http://127.0.0.1:${PORT}/mcp"
PID=""

export LAGENT_CONFIG="${CONFIG}"

cleanup() {
  if [[ -n "${PID}" ]] && kill -0 "${PID}" >/dev/null 2>&1; then
    kill "${PID}" >/dev/null 2>&1 || true
    wait "${PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

echo "staging config: ${LAGENT_CONFIG}"
echo "memory-check"
"${PYTHON}" -m local_agent_lab.cli memory-check --json

echo "memory-status"
"${PYTHON}" -m local_agent_lab.cli memory-status --json

echo "home-mcp roots"
"${PYTHON}" -m local_agent_lab.cli home-mcp roots

echo "starting staging home-mcp on ${URL}"
"${PYTHON}" -m local_agent_lab.cli home-mcp serve --host 127.0.0.1 --port "${PORT}" --auth-mode none &
PID="$!"

echo "home-mcp smoke-test"
"${PYTHON}" -m local_agent_lab.cli home-mcp smoke-test --url "${URL}"

echo "local staging smoke passed"
