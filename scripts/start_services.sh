#!/usr/bin/env bash
set -euo pipefail

if pgrep -x ollama >/dev/null 2>&1; then
  echo "ollama already running"
else
  echo "starting ollama serve in background"
  nohup ollama serve >/tmp/ollama.log 2>&1 &
fi
