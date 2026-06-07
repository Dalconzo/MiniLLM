#!/usr/bin/env bash
set -euo pipefail

models=(
  "qwen2.5-coder:7b"
  "llama3.2:3b"
  "nomic-embed-text"
)

for model in "${models[@]}"; do
  echo "pulling $model"
  ollama pull "$model"
done
