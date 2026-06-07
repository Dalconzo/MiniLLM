# Local Mac Mini Coding/Search Agent Spec

**Target machine:** Base Costco Mac mini, assumed Apple Silicon M4 or similar, 16GB unified memory  
**Primary access pattern:** SSH-first from another machine  
**Human operator:** David  
**Implementation agent:** Codex  
**Goal:** Build a local, useful, low-cost agent system that can help with coding, narrow search, file/document lookup, small function generation, code review, repo summarization, log analysis, and local knowledge workflows.

---

## 1. Core objective

Build a local agent environment that makes a small Mac mini genuinely useful despite limited RAM.

This is not meant to replace frontier API models. It should act like a reliable local junior engineer / research assistant that can:

- Review small diffs and files
- Write narrow, well-specified functions
- Generate unit tests
- Search local repos and notes
- Summarize codebases incrementally
- Explain logs/errors
- Create task plans for Codex/human execution
- Maintain a lightweight local knowledge index
- Escalate or mark tasks that need a frontier model

The design should emphasize:

- Small models
- Strong retrieval
- Good task decomposition
- Explicit context packing
- Repeatable CLI workflows
- Logs and observability
- No fragile “giant autonomous agent” behavior

---

## 2. Non-goals

Do not build:

- A full Copilot replacement
- A large autonomous multi-agent system yet
- A cloud-hosted service
- A complex frontend unless the backend is already solid
- A giant vector database stack unless simple SQLite-based retrieval fails
- A system that assumes high VRAM/GPU availability
- A system that relies on GUI interactions for normal operation

---

## 3. Operating assumptions

### Hardware

Assume:

- Apple Silicon Mac mini
- 16GB unified memory
- macOS
- Enough disk space for several quantized models, code indexes, logs, and local databases

Expected viable local models:

- 7B class models: default
- 8B class models: default
- 14B class models: optional / slower
- 30B+ models: out of scope for normal use on base machine

### Access

Primary control will be through SSH.

The system must support:

- SSH setup
- CLI-only setup
- Running services through `launchd`, `tmux`, or `brew services`
- Git-based development
- Codex-driven implementation from this spec

GUI use is allowed only when unavoidable, for example:

- Initial macOS permissions
- Enabling remote login if not already enabled
- Manual model app testing if CLI setup fails

---

## 4. Recommended stack

### Runtime / package tools

Use:

- Homebrew
- Python 3.11+
- `uv` for Python dependency management
- Git
- SQLite
- `ripgrep`
- `fd`
- `jq`
- `tmux`

Optional:

- `direnv`
- `just`
- Docker Desktop only if needed, but prefer native services on Mac

### Local LLM backend

Primary:

- `ollama`

Reason:

- Simple installation
- Easy model management
- Local HTTP API
- Good enough for small-agent workflows

Optional later:

- `llama.cpp` directly for more control
- MLX-based runners for Apple Silicon optimization

### Local models

Initial model set:

- **General coding default:** Qwen2.5-Coder 7B or latest equivalent available through Ollama
- **General instruction default:** Llama 3.1/3.2 8B or latest equivalent available locally
- **Fast summarizer:** small 3B–8B model
- **Embedding model:** local sentence-transformer or Ollama embedding model

Codex should implement model names as config, not hard-code them.

Example config:

```yaml
models:
  code_default: "qwen2.5-coder:7b"
  chat_default: "llama3.1:8b"
  summarize_fast: "llama3.2:3b"
  embeddings: "nomic-embed-text"
```

---

## 5. System architecture

Build a local project named:

```text
local-agent-lab
```

Architecture:

```text
local-agent-lab/
  README.md
  SPEC.md
  pyproject.toml
  uv.lock
  justfile
  .env.example
  config/
    agent.yaml
    prompts/
      code_review.md
      small_function.md
      test_generation.md
      repo_summary.md
      log_analysis.md
      narrow_search.md
      task_router.md
  src/
    local_agent_lab/
      __init__.py
      cli.py
      config.py
      llm/
        __init__.py
        ollama_client.py
        model_router.py
      tools/
        __init__.py
        shell.py
        git_tools.py
        file_tools.py
        search.py
        tests.py
      indexing/
        __init__.py
        chunker.py
        sqlite_store.py
        embeddings.py
        repo_indexer.py
        document_indexer.py
      agents/
        __init__.py
        base.py
        task_router.py
        code_reviewer.py
        function_writer.py
        test_writer.py
        repo_summarizer.py
        log_analyzer.py
        narrow_search_agent.py
      memory/
        __init__.py
        project_memory.py
        notes.py
      evals/
        __init__.py
        fixtures.py
        runner.py
      logging/
        __init__.py
        run_logger.py
  data/
    indexes/
    memory/
    logs/
  scripts/
    bootstrap_mac.sh
    install_models.sh
    start_services.sh
    index_repo.sh
    run_evals.sh
  docs/
    chatgpt_memory_contract.md
  tests/
    test_chunker.py
    test_search.py
    test_prompt_packing.py
    test_git_tools.py
    test_router.py
```

---

## 6. User-facing CLI

Implement a CLI named:

```bash
lagent
```

The CLI should expose these commands:

```bash
lagent health
lagent models
lagent ask "question"
lagent review --repo /path/to/repo --diff
lagent review --repo /path/to/repo --file path/to/file.py
lagent write-function --spec specs/function.md --repo /path/to/repo
lagent write-tests --repo /path/to/repo --target path/to/file.py
lagent search --repo /path/to/repo "query"
lagent index-repo /path/to/repo
lagent summarize-repo /path/to/repo
lagent explain-log --file logs/error.log
lagent route --task "plain English task"
lagent eval
```

All commands must:

- Print concise terminal output
- Save full run logs to `data/logs/`
- Include prompt, model, retrieved context IDs, elapsed time, and output
- Avoid modifying source files unless an explicit `--apply` flag is passed

Default behavior should be read-only.

---

## 7. First milestone behavior

The first useful version should do these five things well:

### 7.1 Diff-based code review

Command:

```bash
lagent review --repo /path/to/repo --diff
```

Behavior:

- Run `git diff`
- Split diff into manageable chunks if needed
- Retrieve relevant nearby files/functions
- Ask local coding model for review
- Output:
  - bugs
  - edge cases
  - simplifications
  - missing tests
  - risky assumptions
- Avoid generic style comments unless meaningful

Acceptance criteria:

- Works on a repo with unstaged changes
- Does not hallucinate nonexistent files without saying uncertainty
- Identifies at least obvious syntax/logic problems in test fixture diffs
- Provides actionable comments with file/function references

---

### 7.2 Narrow repo search

Command:

```bash
lagent search --repo /path/to/repo "where do we parse barcodes?"
```

Behavior:

- Use hybrid search:
  - lexical search via `ripgrep`
  - semantic search via local embeddings
  - file/path heuristics
- Return:
  - top matching files
  - relevant code snippets
  - short explanation of why each result matters

Acceptance criteria:

- Finds exact symbol matches
- Finds conceptually related chunks even if exact phrase differs
- Ranks tests and docs lower than source code unless query clearly asks for tests/docs
- Handles repos under at least 50k LOC

---

### 7.3 Small function writer

Command:

```bash
lagent write-function --spec specs/function.md --repo /path/to/repo
```

Behavior:

- Read the spec
- Search repo for style and similar functions
- Generate:
  - proposed implementation
  - target file suggestion
  - imports needed
  - unit tests
- Do not apply changes by default
- With `--apply`, write a patch file first, not direct edits

Acceptance criteria:

- Produces code that passes formatting and basic tests for fixture repo
- Uses existing repo style when examples are available
- Fails safely if the spec is ambiguous

---

### 7.4 Test writer

Command:

```bash
lagent write-tests --repo /path/to/repo --target path/to/file.py
```

Behavior:

- Inspect target file
- Search existing tests
- Generate test cases
- Identify edge cases
- Produce a patch or test file

Acceptance criteria:

- Matches existing test framework where detectable
- Does not invent unavailable dependencies unless clearly marked
- Includes positive, negative, and edge case tests where relevant

---

### 7.5 Log/error explainer

Command:

```bash
lagent explain-log --file logs/error.log
```

Behavior:

- Read error log
- Extract stack traces and key messages
- Search repo for referenced files/functions
- Explain likely cause
- Suggest next debugging commands

Acceptance criteria:

- Handles Python tracebacks
- Handles shell command errors
- Identifies missing env vars, import errors, syntax errors, failed assertions
- Distinguishes certainty from speculation

---

## 8. Retrieval design

Small local models need strong retrieval. Do not rely on the model remembering the whole repo.

### 8.1 Chunking

Implement chunking for:

- Python files
- Markdown files
- YAML/JSON/TOML config
- Generic text
- Logs

For Python:

- Prefer AST-aware chunks:
  - module docstring
  - imports
  - classes
  - functions
  - methods
- Include parent class/function context
- Include line numbers

For Markdown:

- Chunk by heading sections
- Preserve heading hierarchy

For generic files:

- Chunk by size with overlap

Each chunk record should include:

```json
{
  "id": "stable_chunk_id",
  "repo_path": "/absolute/repo/path",
  "relative_path": "src/foo.py",
  "language": "python",
  "kind": "function",
  "symbol": "parse_barcode",
  "start_line": 40,
  "end_line": 89,
  "content": "...",
  "content_hash": "...",
  "last_indexed_at": "..."
}
```

---

### 8.2 Storage

Use SQLite first.

Tables:

- `repos`
- `files`
- `chunks`
- `embeddings`
- `runs`
- `retrieval_events`
- `project_notes`

Use sqlite-vss or store vectors as blobs if simple enough. If vector search becomes annoying, use a lightweight local vector DB later, but do not start there unless necessary.

---

### 8.3 Search strategy

For every query, combine:

- Exact string search
- Symbol search
- Path search
- Embedding similarity
- Recent file boost
- Git diff boost
- Test/source weighting

Return ranked chunks with reasons.

Example reason strings:

```text
Exact match for "barcode"
Function name matches parse_barcode
Semantically similar to "tube ID parsing"
Recently modified in current diff
```

---

## 9. Model routing

Implement a model router.

Inputs:

- Task type
- Estimated context length
- Required precision
- Whether code generation is involved
- Whether output modifies files

Routes:

```text
code review      -> code_default
function writing -> code_default
test writing     -> code_default
summarization    -> summarize_fast
chat/ask         -> chat_default
routing          -> summarize_fast or chat_default
```

Also implement escalation labels:

```text
LOCAL_OK
LOCAL_WEAK_BUT_TRY
ESCALATE_TO_FRONTIER
```

The local system should explicitly say when a task is probably above its weight class.

Escalation examples:

- Repo-wide architecture refactor
- Security-sensitive code review
- Ambiguous production migration
- Large multi-file feature
- Long research task requiring current web information
- Complex algorithm design

---

## 10. Prompting requirements

Prompts must be stored as markdown files under:

```text
config/prompts/
```

### 10.1 Code review prompt

Must instruct model to:

- Be critical
- Avoid generic comments
- Cite file paths and line ranges when possible
- Separate likely bugs from style suggestions
- Mention uncertainty
- Suggest tests

Output format:

```markdown
## Summary

## Likely bugs

## Edge cases

## Missing tests

## Simplifications

## Questions / uncertainty
```

---

### 10.2 Small function prompt

Must instruct model to:

- Use existing style from retrieved context
- Keep code minimal
- Prefer pure functions when possible
- Avoid unnecessary abstractions
- Include tests
- Do not invent project conventions

Output format:

```markdown
## Proposed target

## Implementation

```python
...
```

## Tests

```python
...
```

## Notes
```

---

### 10.3 Narrow search prompt

Must instruct model to:

- Explain results, not just list them
- Prefer concrete files/functions
- Avoid pretending certainty
- Provide next search commands where useful

Output format:

```markdown
## Best matches

## Why these matter

## Suggested next checks
```

---

## 11. Safety and file modification policy

Default mode is read-only.

The agent must not:

- Delete files
- Rewrite files directly
- Run destructive shell commands
- Install random packages without explicit command invocation
- Exfiltrate secrets
- Include `.env` contents in model prompts
- Send local files to remote APIs unless explicitly configured later

For any modifying action:

- Require `--apply`
- Generate a patch file first
- Show summary
- Ask human to apply manually or provide a separate apply command

Patch location:

```text
data/patches/YYYYMMDD_HHMMSS_task_name.patch
```

---

## 12. Shell tool policy

Implement a shell wrapper.

Allowed by default:

```text
git status
git diff
git grep
rg
fd
ls
cat
sed
head
tail
python -m pytest
ruff check
mypy
npm test
```

Blocked by default:

```text
rm
sudo
curl | sh
chmod -R
chown -R
git reset --hard
git clean
brew install
pip install
npm install
docker system prune
```

Codex may add an override mechanism later, but first version should keep it simple.

---

## 13. Observability

Every agent run should log:

- Timestamp
- Command
- Repo path
- Model used
- Prompt file
- Retrieved chunks
- Token estimate
- Runtime
- Exit status
- Short output
- Full output

Store logs as JSONL:

```text
data/logs/runs.jsonl
```

Also save full artifacts:

```text
data/logs/runs/YYYYMMDD_HHMMSS_<command>/
  prompt.md
  context.md
  output.md
  metadata.json
```

---

## 14. Evaluation harness

Create fixture repos under:

```text
tests/fixtures/
```

Include tiny repos with known issues:

### Fixture 1: Python utility repo

Contains:

- One function with off-by-one bug
- One missing edge case
- One bad exception handling pattern
- Existing pytest tests

Expected agent behavior:

- Find at least 2 of 3 issues
- Suggest meaningful tests

### Fixture 2: Log traceback repo

Contains:

- Python traceback
- Referenced source file
- Missing env var or bad path

Expected behavior:

- Identify likely root cause
- Suggest exact file/function to inspect

### Fixture 3: Search repo

Contains:

- Similar concepts with different naming
- Example: barcode parsing, tube ID validation, sample accession extraction

Expected behavior:

- Find conceptually related files even without exact phrase match

Command:

```bash
lagent eval
```

Output:

```text
Eval summary:
- code_review_basic: pass/fail
- log_analysis_basic: pass/fail
- narrow_search_basic: pass/fail
```

---

## 15. Bootstrap plan for Codex

Codex should implement in phases.

### Phase 0: Machine bootstrap

Create:

```bash
scripts/bootstrap_mac.sh
```

It should:

- Check for Homebrew
- Install missing CLI tools:
  - git
  - python
  - uv
  - ripgrep
  - fd
  - jq
  - tmux
  - ollama
- Create project dirs
- Verify SSH-friendly operation
- Print next steps

Do not make irreversible system changes.

---

### Phase 1: Minimal CLI

Implement:

```bash
lagent health
lagent models
lagent ask
```

Health should check:

- Python version
- Ollama reachable
- Config file exists
- Data dirs exist
- SQLite DB opens
- Required CLI tools installed

---

### Phase 2: Indexing and search

Implement:

```bash
lagent index-repo
lagent search
```

Start with lexical search + SQLite chunks. Add embeddings after basic indexing works.

---

### Phase 3: Code review

Implement:

```bash
lagent review --diff
lagent review --file
```

This is the first major useful feature.

---

### Phase 4: Function and test generation

Implement:

```bash
lagent write-function
lagent write-tests
```

Generate patches but do not apply by default.

---

### Phase 5: Log analysis

Implement:

```bash
lagent explain-log
```

Integrate traceback parsing and repo search.

---

### Phase 6: Evals

Implement:

```bash
lagent eval
```

Use fixture repos and deterministic checks where possible.

---

## 16. SSH-first setup commands

Expected use from another computer:

```bash
ssh user@mac-mini.local
git clone <repo-url> ~/local-agent-lab
cd ~/local-agent-lab
bash scripts/bootstrap_mac.sh
uv sync
cp .env.example .env
just health
just install-models
lagent health
```

`justfile` should include:

```make
health:
    uv run lagent health

models:
    uv run lagent models

ask q:
    uv run lagent ask "{{q}}"

index repo:
    uv run lagent index-repo "{{repo}}"

search repo q:
    uv run lagent search --repo "{{repo}}" "{{q}}"

eval:
    uv run lagent eval
```

---

## 17. Config file

Create:

```yaml
# config/agent.yaml

paths:
  data_dir: "./data"
  index_dir: "./data/indexes"
  log_dir: "./data/logs"
  patch_dir: "./data/patches"

ollama:
  base_url: "http://localhost:11434"
  timeout_s: 120

models:
  code_default: "qwen2.5-coder:7b"
  chat_default: "llama3.1:8b"
  summarize_fast: "llama3.2:3b"
  embeddings: "nomic-embed-text"

retrieval:
  max_chunks: 12
  max_context_chars: 24000
  chunk_size_chars: 3000
  chunk_overlap_chars: 400

safety:
  read_only_default: true
  allow_apply: false
  redact_env_files: true

logging:
  save_prompts: true
  save_context: true
  save_outputs: true
```

---

## 18. Redaction requirements

Before sending context to the local model, redact:

- `.env`
- private keys
- API keys
- OAuth tokens
- passwords
- SSH keys
- certificates
- obvious secrets

Implement simple regex redaction first.

Patterns:

```text
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
AWS_SECRET_ACCESS_KEY=...
-----BEGIN PRIVATE KEY-----
password = ...
token = ...
```

Replace values with:

```text
[REDACTED_SECRET]
```

---

## 19. Suggested implementation details

### CLI framework

Use `typer`.

### Config

Use `pydantic-settings` or plain YAML loading.

### SQLite

Use `sqlite3` initially.

### Embeddings

Start with Ollama embeddings if available.

Fallback:

- lexical search only
- warn user that semantic search is unavailable

### Token estimates

Use rough char estimate:

```text
estimated_tokens = chars / 4
```

Good enough for routing.

### Context packing

Build context in this order:

1. Task instructions
2. User query/spec
3. Current diff or target file
4. Retrieved chunks
5. Relevant tests
6. Output format

Keep under configured context char budget.

---

## 20. Good first test commands

After implementation, these should work:

```bash
lagent health
lagent ask "Write a Python function that validates an A1-H12 well coordinate."
lagent index-repo ~/some-python-repo
lagent search --repo ~/some-python-repo "where are config values loaded?"
lagent review --repo ~/some-python-repo --diff
lagent write-tests --repo ~/some-python-repo --target src/foo.py
lagent explain-log --file ~/some-python-repo/error.log
lagent eval
```

---

## 21. Example narrow coding task

Spec file:

```markdown
# Function spec

Add a function that converts a 96-well plate coordinate like A1, H12, or C7 into a 1-based column-major index.

Rules:

- Rows A-H map to 1-8
- Columns 1-12
- A1 = 1
- B1 = 2
- H1 = 8
- A2 = 9
- H12 = 96
- Invalid coordinates should raise ValueError
```

Expected agent output:

- Function
- Tests for valid examples
- Tests for invalid row/column
- Minimal implementation
- No unnecessary class

---

## 22. Definition of done

The system is usable when:

- It can be installed from SSH on a fresh Mac mini
- `lagent health` passes
- At least one local coding model runs through Ollama
- It can index a small repo
- It can search the repo with useful ranked results
- It can review a git diff without modifying files
- It can generate a small function and tests as a patch
- It can explain a traceback using repo context
- It logs all runs
- It has at least three basic eval fixtures
- README explains setup and commands clearly

---

## 23. Future upgrades

Do not implement these in v1 unless core features are stable.

### 23.1 Hybrid local/frontier routing

Add optional API escalation:

- Local model drafts
- Frontier model reviews hard cases
- Local model applies simple patches

### 23.2 Background indexing daemon

Watch repos for changes and update index automatically.

### 23.3 Personal knowledge base

Index:

- Markdown notes
- project specs
- lab automation docs
- personal CRM notes
- research summaries

### 23.4 Agent council

Run multiple local models/prompts:

- reviewer
- implementer
- test writer
- skeptic

Aggregate their outputs.

### 23.5 Private personalized search layer

Add a privacy-preserving search system that uses the Mac mini as the backend and the iPhone as a query/capture interface.

Goal:

- Provide a private alternative to daily Google-style search workflows
- Keep query logs, preferences, saved pages, embeddings, and ranking signals local by default
- Use local LLMs for query rewriting, result classification, reranking, summarization, and answer synthesis
- Support iPhone access through Tailscale, a web UI, Shortcuts, or SSH-triggered commands
- Prefer transparent, editable ranking rules over opaque recommender behavior

This should not attempt to build a full public web index.

The system should combine:

- metasearch for public web discovery
- local indexing for personal documents and saved pages
- keyword search for precision
- vector search for semantic recall
- local LLMs for judgment and compression
- personal preference memory for ranking improvements over time

Recommended components:

- SearXNG for private metasearch
- SQLite or Postgres for query history, ranking feedback, and source preferences
- SQLite FTS, Meilisearch, or Tantivy for keyword search
- Qdrant or sqlite-vss for vector search if needed
- FastAPI for a local search API
- Ollama, llama.cpp, or MLX for local model inference
- Tailscale for secure iPhone access
- Optional Progressive Web App for mobile UI

Suggested CLI surface:

```bash
lagent web-search "query"
lagent search-all "query"
lagent search-personal "query"
lagent search-web "query"
lagent save-page <url>
lagent rank-source <domain> --trust 0.8 --topic "lab automation"
lagent block-source <domain>
lagent search-feedback <run-id> --result <n> --rating up
```

Ranking should combine:

- keyword relevance
- semantic similarity
- source trust
- freshness
- personal interest match
- prior positive/negative feedback
- topic-specific source preferences
- SEO-spam penalties
- paywall or affiliate penalties

Example scoring model:

```text
score = (
    0.35 * keyword_relevance
    + 0.25 * semantic_similarity
    + 0.15 * source_trust_score
    + 0.10 * freshness_score
    + 0.10 * personal_interest_match
    + 0.05 * reading_level_match
    - 0.20 * seo_spam_score
    - 0.15 * paywall_penalty
)
```

Store local search memory:

`search_events`

- id
- timestamp
- query
- rewritten_query
- clicked_url
- domain
- dwell_seconds
- rating
- topic
- notes

`source_preferences`

- domain
- topic
- trust_score
- spam_score
- last_updated

`query_memory`

- query_pattern
- preferred_source_types
- preferred_answer_style
- excluded_domains

Initial iPhone workflows:

- iPhone Shortcut sends query to Mac mini FastAPI endpoint
- iPhone Share Sheet sends URL or page text to `save-page`
- iPhone voice query triggers `search-all`
- Results return as Markdown, HTML, or JSON
- User can mark results as useful, bad, spam, or saved

Safety and privacy requirements:

- Store all query logs locally by default
- Strip tracking parameters from saved URLs
- Do not send personal documents to external APIs unless explicitly configured
- Clearly label whether a result came from local index, saved pages, or live web
- Keep ranking rules inspectable and editable
- Allow domain blocking and source trust overrides

V1 acceptance criteria for this module:

- Mac mini can run a private search endpoint locally
- iPhone can submit a query over Tailscale
- System returns ranked results with citations or links
- System can save useful pages into a local index
- System records thumbs-up/thumbs-down feedback
- System uses feedback to adjust future ranking
- System can search local notes/docs separately from web results
- System clearly distinguishes retrieved facts from LLM synthesis

Placement and priority:

- This should not be part of the initial v1 milestone
- Keep the original v1 focused on the local coding/search assistant
- Treat this as a major later module that should be promoted only after `lagent index-repo`, `lagent search`, and logging are stable

### 23.6 ChatGPT export memory and RAG

Build a private local memory layer from exported ChatGPT logs after the core coding/search assistant is stable.

The storage and schema contract lives in:

```text
docs/chatgpt_memory_contract.md
```

This module should preserve immutable raw exports, normalize conversations into queryable SQLite records, support FTS and semantic chunk retrieval, maintain curated working memory separately from raw transcripts, expose subject/project/workflow browsing, and log every agent-facing memory exposure.

This module must be trace-first, not a black box. Every memory command should emit a `run_id`, write artifacts under `data/logs/<run_id>/`, and provide enough stage-level detail to identify whether a failure happened during input discovery, parsing, normalization, chunking, SQLite migration, FTS refresh, embedding, retrieval, ranking, redaction, or audit logging.

Initial CLI surface:

```bash
lagent ingest-chatgpt --input data/chatgpt_exports/raw --dry-run --trace
lagent ingest-chatgpt --input data/chatgpt_exports/raw
lagent memory-check
lagent memory-search "query"
lagent memory-search "query" --explain --json
lagent memory-embed
lagent memory-subjects
lagent memory-context "query" --depth medium
lagent memory-promote <chunk-id> --type decision
lagent memory-show <memory-id>
lagent memory-feedback <retrieval-id> --source <source-id> --rating useful
lagent memory-trace <run-id>
```

This should use multiple memory systems rather than one opaque store:

- immutable raw export archive
- normalized JSONL import artifacts
- SQLite canonical memory database
- SQLite FTS keyword index
- swappable semantic vector backend
- curated working memory records
- subject/project/workflow labels
- retrieval exposure and feedback logs
- command trace artifacts and validation reports

### 23.7 Web search adapter

Add current web search only if explicitly configured.

### 23.8 GUI

Optional local dashboard:

- Recent runs
- Indexed repos
- Search interface
- Review outputs
- Patch previews

Use FastAPI + simple HTML or Streamlit only after CLI is good.

---

## 24. Codex instruction summary

Codex should treat this as an implementation project.

Priorities:

1. Build boring reliable CLI first
2. Keep all default actions read-only
3. Prefer simple local files and SQLite over complex infra
4. Make every feature testable
5. Log everything
6. Use retrieval, not giant prompts
7. Add eval fixtures early
8. Make install work over SSH
9. Avoid GUI assumptions
10. Do not overbuild autonomous behavior

The v1 success condition is not “magic AI agent.”

The v1 success condition is:

> A local Mac mini can act as a useful, safe, repo-aware coding/search assistant for narrow tasks, with repeatable CLI commands and measurable behavior.
