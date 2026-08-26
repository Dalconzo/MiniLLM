#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ $# -gt 0 ]]; then
  ARTIFACT_DIR="$1"
else
  RUN_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
  ARTIFACT_DIR="${ROOT_DIR}/test-results/evaluation-v1/${RUN_STAMP}"
fi

if [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
  PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
else
  PYTHON_BIN="python3"
fi

mkdir -p "${ARTIFACT_DIR}"
echo "Running deterministic offline synthetic retrieval comparison"
echo "Artifacts: ${ARTIFACT_DIR}"
cd "${ROOT_DIR}"
exec "${PYTHON_BIN}" -m local_agent_lab.evals.run_memory_v1 \
  --output-dir "${ARTIFACT_DIR}" \
  --invocation-label "scripts/run_evals.sh"
