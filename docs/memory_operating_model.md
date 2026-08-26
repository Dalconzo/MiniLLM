# ChatGPT Memory Operating Model

This document explains how the ChatGPT export memory system should be used in practice. The storage schema is defined in `docs/chatgpt_memory_contract.md`; this file defines the operating model for users and local agents.

The core rule is simple: the system is multiple memory layers, not one opaque RAG box. Raw transcripts, parsed records, searchable indexes, semantic chunks, curated working memory, subject browsers, and agent context packs each serve different jobs and have different trust levels.

## Memory Layers

### Raw Archive

Purpose: preserve exported ChatGPT data exactly as received.

Storage:

```text
data/chatgpt_exports/raw/<import_id>/
```

Use this when:

- verifying that an import did not lose data
- re-running ingestion after parser improvements
- auditing whether a memory came from a real export
- recovering from bad derived indexes

Trust level: highest for provenance, lowest for direct agent use. Agents should not browse raw exports by default because raw logs are noisy, unredacted, and difficult to scope.

Rules:

- never rewrite raw export files in place
- never redact raw exports directly
- never treat raw exports as the query interface
- record file hashes during ingestion

### Parsed Archive

Purpose: create reproducible JSONL records from raw exports.

Storage:

```text
data/chatgpt_exports/parsed/<import_id>/
  conversations.jsonl
  messages.jsonl
  chunks.jsonl
  attachments.jsonl
  subjects.jsonl
  import_report.json
```

Use this when:

- debugging parser behavior
- comparing source counts to database counts
- inspecting exactly what text was extracted
- rebuilding SQLite, FTS, or vector indexes

Trust level: high for extracted content, medium for inferred metadata. Parser-derived fields such as subjects, token estimates, and summaries should remain inspectable and correctable.

Rules:

- parsed output must be reproducible from raw input
- every parsed record needs a stable public ID
- parser errors should go to import reports and traces, not disappear silently

### SQLite and FTS Searchable Archive

Purpose: provide the canonical local database and precise keyword search.

Storage:

```text
data/memory/chatgpt_memory.sqlite3
```

Use this when:

- finding exact terms, names, commands, filenames, products, or project labels
- filtering by subject, project, role, date, or source
- producing deterministic counts and audit reports
- powering first-pass search before semantic reranking

Trust level: canonical for queryable records. If SQLite and JSONL disagree, `lagent memory-check` should expose the mismatch before agents rely on the data.

Rules:

- SQLite row IDs are internal only
- public IDs such as `conv_*`, `msg_*`, `chk_*`, and `mem_*` must be used in traces and CLI output
- FTS should index non-deleted chunks only
- every schema change must be migration-backed

### Semantic Chunk Memory

Purpose: retrieve conceptually related material when exact keywords are missing.

Storage:

```text
data/memory/vectors/<backend>/
```

Use this when:

- asking broad questions across projects or workflows
- finding similar decisions, designs, or discussions
- connecting related work that used different wording
- reranking keyword results with semantic similarity

Trust level: useful but probabilistic. Semantic similarity is evidence for relevance, not proof that a memory is correct or current.

Rules:

- SQLite remains the source of truth for chunk IDs and vector metadata
- embedding records must include model name, dimension, chunk hash, and creation time
- stale embeddings must be detectable when chunk hashes change
- vector search results need score explanations when exposed to an agent

### Curated Working Memory

Purpose: maintain durable user-approved facts, preferences, decisions, and project state.

Storage:

```text
data/memory/curated/
  memory_records.jsonl
  subjects.jsonl
```

Use this when:

- preserving decisions that should outlive the original conversation
- storing stable preferences or operating rules
- summarizing project state for future agents
- correcting or overriding noisy transcript evidence

Trust level: highest for agent context when records are current and user-approved. Curated memory should still point back to source chunks when possible.

Rules:

- promotion from transcript to curated memory must be explicit
- curated records should include subject, confidence, source IDs, and update history
- agents may prefer curated memory over transcript snippets for operating instructions
- stale curated records should be marked, not silently deleted

## Subjects, Projects, and Workflow Browsing

Subjects are the navigation layer across memory systems. They let the user and agents browse by project, workflow, domain, or recurring task instead of searching the whole archive every time.

Recommended subject types:

- `project`: a specific build, repo, client, experiment, or long-running initiative
- `workflow`: repeated procedures such as coding, lab automation, writing, planning, research, or debugging
- `domain`: broader areas such as biology, local LLMs, finance, hardware, or personal ops
- `preference`: durable user preferences and style expectations
- `decision`: chosen directions, rejected alternatives, and rationale

Recommended commands:

```bash
lagent memory-subjects
lagent memory-search "query" --subject <subject>
lagent memory-context "query" --subject <subject> --depth medium
lagent memory-promote <chunk-id> --type decision --subject <subject>
```

Browsing should show source counts, date ranges, recent activity, curated records, and top related subjects. A subject page should distinguish raw transcript evidence from curated working memory.

## Agent Context Packs

An agent context pack is the controlled memory bundle given to a local agent for a task. It should be generated from search and ranking, not by dumping whole transcripts.

Context pack contents:

- query and rewritten query
- active subjects or project filters
- curated memory records
- selected transcript chunks
- nearby conversation turns when disclosure rules allow it
- source IDs and links back to local records
- score breakdowns and disclosure tiers
- redaction summary
- retrieval event ID

Recommended command:

```bash
lagent memory-context "task or question" --depth medium --explain
```

Agents should treat context packs as evidence packets. They may use them to answer, plan, or write code, but they should cite source IDs in traces and avoid claiming unsupported facts as known.

## Disclosure Depth

Memory exposure should expand only as relevance and user intent become clearer.

| Depth | Use | Typical exposure |
| --- | --- | --- |
| `far` | orientation and browsing | titles, dates, subjects, summaries, source IDs |
| `medium` | default agent retrieval | matching chunks, snippets, roles, timestamps, score breakdowns |
| `close` | active task context | matching chunks, nearby turns, curated records, extracted decisions |
| `full` | explicit audit or deep recovery | wider conversation windows and attachment metadata, still redacted |

Default depth should be `medium`. `full` should require an explicit flag and must be logged.

## Privacy Defaults

Default behavior is local-first and conservative.

- raw exports stay local
- parsed JSONL stays local
- SQLite, FTS, embeddings, and traces stay local
- external APIs are disabled unless explicitly configured
- personal documents are never sent to external APIs by default
- secret redaction runs before model calls
- every agent-facing memory exposure is logged
- deleted or blocked records use tombstones so re-ingestion does not silently re-expose them

The system should make source boundaries obvious. Results should label whether content came from raw-derived transcript chunks, curated memory, local notes, saved pages, or future live web search.

## Traceability

Every memory command must create a `run_id` and write artifacts under:

```text
data/logs/<run_id>/
```

Required user-facing debugging flow:

```bash
lagent ingest-chatgpt --input data/chatgpt_exports/raw --dry-run --trace
lagent ingest-chatgpt --input data/chatgpt_exports/raw --trace
lagent memory-check
lagent memory-search "query" --explain --json
lagent memory-trace <run-id>
```

Trace output should identify the failed stage, affected source file, record ID, SQLite table, index, vector backend, or privacy rule. If a command fails without a useful `run_id`, stage, and artifact path, the command is not operating-model compliant.

Important stages:

- input discovery
- raw hashing
- export parsing
- normalization
- chunking
- JSONL writes
- SQLite migration
- SQLite writes
- FTS refresh
- embedding
- candidate retrieval
- ranking
- disclosure filtering
- redaction
- audit logging
- validation

## Recommended Workflows

### Initial Import

Use this when the user drops a ChatGPT export into the repo.

```bash
lagent ingest-chatgpt --input data/chatgpt_exports/raw --dry-run --trace
lagent memory-trace <run-id>
lagent ingest-chatgpt --input data/chatgpt_exports/raw --trace
lagent memory-check
```

Expected result: counts match between import report, parsed JSONL, SQLite, and FTS. Any skipped files are listed with reasons.

### Find Project Context

Use this before an agent starts work on an existing project.

```bash
lagent memory-subjects
lagent memory-search "project name or task" --explain --json
lagent memory-context "what should the agent know before working on this?" --depth medium --explain
```

Expected result: the agent receives curated records first, then transcript chunks with source IDs and score explanations.

### Promote Durable Knowledge

Use this when a transcript contains a decision, preference, or useful fact that should become stable memory.

```bash
lagent memory-search "decision or preference" --explain --json
lagent memory-promote <chunk-id> --type decision --subject <subject>
lagent memory-show <memory-id>
```

Expected result: future context packs can use the curated record without repeatedly rediscovering the original transcript.

### Audit an Agent Answer

Use this when an agent appears to rely on bad or surprising memory.

```bash
lagent memory-trace <run-id>
lagent memory-show <source-id>
lagent memory-feedback <retrieval-id> --source <source-id> --rating bad
```

Expected result: the trace shows which candidates were retrieved, how they were ranked, what was exposed, and whether redaction or disclosure rules changed the context.

### Rebuild Derived State

Use this after parser, schema, FTS, or embedding changes.

```bash
lagent memory-check
lagent ingest-chatgpt --input data/chatgpt_exports/raw --trace
lagent memory-check
lagent memory-search "known fixture query" --explain --json
```

Expected result: raw archives remain unchanged, derived outputs are regenerated, and validation reports explain any mismatches.

## What Agents Can Trust

Agents can trust:

- curated memory as the preferred source for durable user preferences and decisions
- SQLite records for canonical IDs, timestamps, roles, and source relationships
- parsed JSONL for debugging how raw exports became records
- raw archives for provenance only
- semantic similarity as a relevance signal only
- traces as the required audit trail for what happened during a command

Agents should not trust:

- a high vector score as proof of factual correctness
- old transcript chunks over newer curated memory
- unscoped whole-archive retrieval for project-specific work
- summaries without source IDs
- any memory exposure that lacks a retrieval event or trace artifact

## Build Order

The practical build order should remain boring and inspectable:

1. observability and validation harness
2. import and parsed archive generation
3. SQLite schema and FTS keyword search
4. subject/project browsing
5. semantic embeddings and hybrid ranking
6. curated working memory
7. agent context packs
8. feedback, privacy hardening, and quality evals

This order keeps each layer testable from the terminal before it becomes part of agent behavior.
