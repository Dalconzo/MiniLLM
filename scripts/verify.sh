#!/usr/bin/env bash
set -euo pipefail

uv lock --check
uv sync --frozen --all-groups
uv run --frozen ruff format --check src tests scripts examples
uv run --frozen ruff check src tests scripts examples
uv run --frozen mypy
./scripts/run_security_tests.sh
uv run --frozen pytest -q
uv run --frozen python examples/public_demo/run_demo.py
