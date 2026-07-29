# Local Staging Memory Environment

This repo can run a local staging copy of the memory system without the Mac mini, production Home MCP service, tunnel client, or production credentials.

Use staging when:

- The Mac mini is unavailable.
- A memory feature needs local smoke tests before promotion.
- A change needs artifacts that can later be synced to the Mac mini deliberately.

Do not use staging as a production memory source. Staging is a builder/test plane.

## Profile

Staging is selected with `LAGENT_CONFIG`:

```bash
export LAGENT_CONFIG=/Users/daviddalconzo/MiniLLM/config/agent.local-staging.yaml
```

Runtime state goes under:

```text
data/local_staging/
```

Home MCP staging files go under:

```text
data/local_staging/home_mcp/
```

This keeps staging separate from the Mac mini-style production data path:

```text
data/home_mcp/
```

## Required Smoke Commands

Run the local staging smoke script:

```bash
scripts/local_staging_memory_smoke.sh
```

The script runs:

```bash
.venv/bin/python -m local_agent_lab.cli memory-check --json
.venv/bin/python -m local_agent_lab.cli memory-status --json
.venv/bin/python -m local_agent_lab.cli home-mcp roots
.venv/bin/python -m local_agent_lab.cli home-mcp serve --host 127.0.0.1 --port 8876 --auth-mode none
.venv/bin/python -m local_agent_lab.cli home-mcp smoke-test --url http://127.0.0.1:8876/mcp
```

If staging has a populated memory database, also run:

```bash
.venv/bin/python -m local_agent_lab.cli memory-embed --limit 25 --json
.venv/bin/python -m local_agent_lab.cli memory-search "recipe baking preferences" --explain
.venv/bin/python -m local_agent_lab.cli memory-context "what should an agent know about my baking work?" --subject "Recipes and Baking" --explain
```

## Ingesting Export Data

If a local ChatGPT export exists at `data/chatgpt_exports/raw/`, ingest it into staging with the staging config exported:

```bash
export LAGENT_CONFIG=/Users/daviddalconzo/MiniLLM/config/agent.local-staging.yaml
.venv/bin/python -m local_agent_lab.cli memory-ingest --source data/chatgpt_exports/raw --dry-run
.venv/bin/python -m local_agent_lab.cli memory-ingest --source data/chatgpt_exports/raw
.venv/bin/python -m local_agent_lab.cli memory-check
.venv/bin/python -m local_agent_lab.cli memory-status
```

Only runtime outputs under `data/local_staging/` should change. Private raw exports remain under `data/chatgpt_exports/` and must stay untracked.

## Promotion Boundary

Staging artifacts are evidence, not automatic deployment input. Promotion to the Mac mini should require:

- Passing automated tests locally.
- Passing the staging smoke script.
- Reviewing changed code and docs.
- Syncing code/config only, not private staging runtime data.
- Running the Mac mini smoke checklist after sync.

Never copy staging credentials, tunnels, production auth config, or private raw export data into git.
