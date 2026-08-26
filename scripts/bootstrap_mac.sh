#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required but not installed."
  exit 1
fi

packages=(git python uv ripgrep fd jq tmux ollama just)

for pkg in "${packages[@]}"; do
  if brew list "$pkg" >/dev/null 2>&1; then
    echo "already installed: $pkg"
  else
    echo "installing: $pkg"
    brew install "$pkg"
  fi
done

cd "$ROOT_DIR"
uv sync --frozen --all-groups
uv run --frozen python examples/public_demo/run_demo.py --json >/dev/null

echo "bootstrap complete: locked environment installed and public demo passed"
