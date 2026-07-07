# Local Agent Lab

`local-agent-lab` is an SSH-first local agent scaffold for a small Apple Silicon Mac mini running Ollama.

The design target is practical local usefulness with small models, explicit retrieval, conservative defaults, and CLI workflows that hold up over SSH.

## Current Scope

This repo now implements Phase 0 through Phase 5 from the spec:

- Bootstrap scripts for the Mac mini toolchain
- YAML-based configuration
- Prompt files under `config/prompts/`
- `lagent health`
- `lagent models`
- `lagent ask`
- `lagent index-repo`
- `lagent search`
- `lagent review --repo ... --file ...`
- `lagent review --repo ... --diff`
- `lagent write-function --spec ... --repo ...`
- `lagent write-tests --repo ... --target ...`
- `lagent explain-log --file ... [--repo ...]`
- `lagent ingest-chatgpt --input ... --dry-run --trace`
- `lagent ingest-chatgpt --input ... --trace`
- `lagent memory-check`
- `lagent memory-trace <run-id>`
- `lagent memory-search "query" --explain --json`
- `lagent memory-embed`
- `lagent memory-subjects`
- `lagent memory-promote <chunk-id> --type decision`
- `lagent memory-show <memory-id>`
- `lagent memory-context "query" --depth medium`
- `lagent memory-audit <run-id>`
- `lagent memory-eval`
- `lagent home-mcp roots`
- `lagent home-mcp serve --host 127.0.0.1 --port 8765`
- `lagent home-mcp create-recipe --title "..." --body "..."`
- Structured run logging under `data/logs/`
- Patch artifacts under `data/patches/`
- Initial routing labels for local-vs-frontier expectations

Eval workflows are still scaffolded for the next pass.

## Quick Start

```bash
uv venv
source .venv/bin/activate
uv pip install -e '.[dev]'
lagent health
lagent models
lagent ask "Summarize the purpose of this repository."
lagent index-repo .
lagent search . "RunLogger"
lagent review --repo . --file src/local_agent_lab/tools/git_tools.py
lagent write-tests --repo . --target src/local_agent_lab/tools/git_tools.py
lagent explain-log --file /tmp/error.log --repo .
lagent ingest-chatgpt --input data/chatgpt_exports/raw --dry-run --trace
lagent memory-check
```

## Layout

- `config/agent.yaml`: model names, routing, paths, and runtime defaults
- `config/prompts/`: task prompt templates
- `src/local_agent_lab/`: CLI, Ollama client, routing, logging, and scaffolding modules
- `docs/`: design contracts for larger memory/search modules
- `scripts/`: bootstrap and helper scripts for the Mac mini
- `data/logs/`: run logs and per-run artifacts
- `data/patches/`: generated patch files for write workflows
- `tests/`: unit coverage for config, routing, indexing, search, review parsing, and patch generation

## Notes

- Ollama is the default local backend.
- `lagent write-function` and `lagent write-tests` generate patch files by default and only write files with `--apply`.
- `lagent explain-log` works without a repo, but `--repo` gives it traceback-aware code lookup.
- ChatGPT export memory work is tracked under `lagent-100`; the storage and observability contract is in `docs/chatgpt_memory_contract.md`, and the user/agent operating model is in `docs/memory_operating_model.md`.
- Memory/RAG commands must expose `run_id` trace artifacts so ingestion, search, ranking, and agent context are inspectable from the terminal.
- Any content sent to the model is passed through a lightweight redaction layer first.
- Every CLI run writes a JSONL event plus per-run artifacts in `data/logs/`.
- `lagent review --diff` requires the target repo to be an actual git checkout with `.git` metadata present.
- `home-mcp` exposes only allowlisted roots and is intended to be tunneled or relayed, not opened directly to the public internet.
