set shell := ["zsh", "-cu"]

bootstrap:
  ./scripts/bootstrap_mac.sh

install:
  uv venv
  source .venv/bin/activate && uv pip install -e '.[dev]'

health:
  source .venv/bin/activate && lagent health

models:
  source .venv/bin/activate && lagent models

ask QUESTION:
  source .venv/bin/activate && lagent ask "{{QUESTION}}"

test:
  source .venv/bin/activate && pytest
