set shell := ["bash", "-cu"]

bootstrap:
  ./scripts/bootstrap_mac.sh

install:
  uv sync --frozen --all-groups

lock-check:
  uv lock --check

health:
  uv run --frozen lagent health

models:
  uv run --frozen lagent models

ask QUESTION:
  uv run --frozen lagent ask "{{QUESTION}}"

test:
  uv run --frozen pytest

lint:
  uv run --frozen ruff check src tests scripts examples

format-check:
  uv run --frozen ruff format --check src tests scripts examples

type-check:
  uv run --frozen mypy

security:
  ./scripts/run_security_tests.sh

coverage:
  mkdir -p build
  ulimit -n 4096; COVERAGE_FILE=build/.coverage uv run --frozen pytest --cov=local_agent_lab --cov-branch --cov-report=term-missing --cov-report=json:build/coverage.json --cov-report=xml:build/coverage.xml

wheel:
  uv build --wheel --no-build-isolation --out-dir dist

check:
  ./scripts/verify.sh
