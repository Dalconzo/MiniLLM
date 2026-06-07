# ChatGPT Export Memory Contract

This document defines the storage contract for the local ChatGPT export memory system. It is the schema gate for `lagent-101`; ingestion, search, embeddings, privacy controls, and agent context commands should treat this as the source of truth until migrations supersede it.

## Goals

- Preserve raw ChatGPT exports exactly as received.
- Normalize conversations into stable, searchable records.
- Support separate keyword, semantic, curated, browsing, and audit layers.
- Keep personal data local by default and make every agent exposure traceable.
- Let future implementations swap vector backends without changing public IDs.

## Directory Contract

All paths are relative to the repo or configured `paths.data_dir`.

```text
data/
  chatgpt_exports/
    raw/
      <import_id>/
        conversations.json
        user.json
        assets/
        manifest.json
    parsed/
      <import_id>/
        conversations.jsonl
        messages.jsonl
        chunks.jsonl
        attachments.jsonl
        subjects.jsonl
        import_report.json
    quarantine/
      <import_id>/
        <file>
  memory/
    chatgpt_memory.sqlite3
    vectors/
      <backend>/
        <collection-or-shard>
    curated/
      memory_records.jsonl
      subjects.jsonl
    exports/
      <timestamp>/
        memory_records.md
        audit_summary.json
  logs/
    <run_id>/
      command.json
      trace.jsonl
      validation_report.json
      import_report.json
      search_explain.json
```

Rules:

- `raw/` is immutable after import. Do not rewrite, redact, reformat, or delete files in place.
- `parsed/` is reproducible derived output and may be regenerated from `raw/`.
- `quarantine/` stores unsupported, malformed, or suspicious files with an import report entry.
- `memory/chatgpt_memory.sqlite3` is the canonical queryable store.
- `memory/vectors/` is backend-owned. SQLite remains the source of truth for chunk IDs, vector IDs, model metadata, and validity.
- `memory/curated/` is an optional human-editable export/import mirror for curated records. SQLite remains canonical.
- `logs/<run_id>/` stores command-level observability artifacts for terminal inspection and debugging.

## Stable IDs

IDs should be deterministic unless the record is created by user action.

| Entity | ID format | Source |
| --- | --- | --- |
| import | `imp_<sha256-12>` | Hash of raw export root path, file hashes, and import timestamp |
| conversation | `conv_<sha256-16>` | Export conversation ID when present, else title plus create time plus message hashes |
| message | `msg_<sha256-16>` | Conversation ID plus export message ID or turn index plus content hash |
| chunk | `chk_<sha256-16>` | Message ID plus chunk index plus normalized text hash |
| attachment | `att_<sha256-16>` | Message ID plus filename or asset path plus content hash when available |
| subject | `sub_<slug>` | User-provided slug, normalized to lowercase kebab case |
| memory record | `mem_<uuid7-or-ulid>` | Created on promotion or manual entry |
| retrieval event | `ret_<uuid7-or-ulid>` | Created for every memory retrieval/context request |
| exposure item | `exp_<uuid7-or-ulid>` | Created for each item exposed in a retrieval event |
| command run | `run_<timestamp>-<random>` | Created by the shared run logger for every CLI command |
| trace event | `trc_<uuid7-or-ulid>` | Created for each observable stage in a memory command |

Never expose internal SQLite row IDs as public identifiers.

## Normalized JSONL Contracts

`parsed/<import_id>/conversations.jsonl`

Required fields:

- `id`
- `import_id`
- `source_conversation_id`
- `title`
- `created_at`
- `updated_at`
- `message_count`
- `mapping_shape`
- `source_path`
- `content_sha256`

`parsed/<import_id>/messages.jsonl`

Required fields:

- `id`
- `conversation_id`
- `import_id`
- `source_message_id`
- `parent_message_id`
- `role`
- `author_name`
- `created_at`
- `turn_index`
- `content_text`
- `content_sha256`
- `token_estimate`
- `attachment_count`
- `metadata_json`

`parsed/<import_id>/chunks.jsonl`

Required fields:

- `id`
- `message_id`
- `conversation_id`
- `import_id`
- `chunk_index`
- `text`
- `text_sha256`
- `token_estimate`
- `start_char`
- `end_char`
- `subject_ids`
- `source_kind`

`parsed/<import_id>/attachments.jsonl`

Required fields:

- `id`
- `message_id`
- `conversation_id`
- `import_id`
- `source_path`
- `filename`
- `mime_type`
- `size_bytes`
- `content_sha256`
- `extracted_text_path`
- `metadata_json`

## SQLite Schema Contract

SQLite database path: `data/memory/chatgpt_memory.sqlite3`.

Every table should include `created_at` and `updated_at` unless explicitly event-only.

### `schema_migrations`

- `version INTEGER PRIMARY KEY`
- `name TEXT NOT NULL`
- `applied_at TEXT NOT NULL`
- `checksum TEXT NOT NULL`

### `imports`

- `id TEXT PRIMARY KEY`
- `source_root TEXT NOT NULL`
- `raw_manifest_path TEXT NOT NULL`
- `imported_at TEXT NOT NULL`
- `status TEXT NOT NULL`
- `parser_version TEXT NOT NULL`
- `file_count INTEGER NOT NULL`
- `conversation_count INTEGER NOT NULL`
- `message_count INTEGER NOT NULL`
- `chunk_count INTEGER NOT NULL`
- `content_sha256 TEXT NOT NULL`
- `notes TEXT`

Indexes:

- `idx_imports_imported_at(imported_at)`
- `idx_imports_status(status)`

### `conversations`

- `id TEXT PRIMARY KEY`
- `import_id TEXT NOT NULL REFERENCES imports(id)`
- `source_conversation_id TEXT`
- `title TEXT NOT NULL`
- `created_at TEXT`
- `updated_at TEXT`
- `message_count INTEGER NOT NULL`
- `first_message_at TEXT`
- `last_message_at TEXT`
- `summary TEXT`
- `content_sha256 TEXT NOT NULL`
- `is_deleted INTEGER NOT NULL DEFAULT 0`
- `metadata_json TEXT NOT NULL DEFAULT '{}'`

Indexes:

- `idx_conversations_import_id(import_id)`
- `idx_conversations_title(title)`
- `idx_conversations_created_at(created_at)`
- `idx_conversations_last_message_at(last_message_at)`
- `idx_conversations_deleted(is_deleted)`

### `messages`

- `id TEXT PRIMARY KEY`
- `conversation_id TEXT NOT NULL REFERENCES conversations(id)`
- `import_id TEXT NOT NULL REFERENCES imports(id)`
- `source_message_id TEXT`
- `parent_message_id TEXT`
- `role TEXT NOT NULL`
- `author_name TEXT`
- `turn_index INTEGER NOT NULL`
- `created_at TEXT`
- `content_text TEXT NOT NULL`
- `content_sha256 TEXT NOT NULL`
- `token_estimate INTEGER NOT NULL`
- `attachment_count INTEGER NOT NULL DEFAULT 0`
- `is_deleted INTEGER NOT NULL DEFAULT 0`
- `metadata_json TEXT NOT NULL DEFAULT '{}'`

Indexes:

- `idx_messages_conversation_turn(conversation_id, turn_index)`
- `idx_messages_role(role)`
- `idx_messages_created_at(created_at)`
- `idx_messages_content_hash(content_sha256)`

### `message_chunks`

- `id TEXT PRIMARY KEY`
- `message_id TEXT NOT NULL REFERENCES messages(id)`
- `conversation_id TEXT NOT NULL REFERENCES conversations(id)`
- `import_id TEXT NOT NULL REFERENCES imports(id)`
- `chunk_index INTEGER NOT NULL`
- `text TEXT NOT NULL`
- `text_sha256 TEXT NOT NULL`
- `token_estimate INTEGER NOT NULL`
- `start_char INTEGER NOT NULL`
- `end_char INTEGER NOT NULL`
- `source_kind TEXT NOT NULL`
- `summary TEXT`
- `is_deleted INTEGER NOT NULL DEFAULT 0`
- `metadata_json TEXT NOT NULL DEFAULT '{}'`

Indexes:

- `idx_chunks_message_index(message_id, chunk_index)`
- `idx_chunks_conversation(conversation_id)`
- `idx_chunks_import(import_id)`
- `idx_chunks_hash(text_sha256)`
- `idx_chunks_deleted(is_deleted)`

FTS:

```sql
CREATE VIRTUAL TABLE chatgpt_chunks_fts USING fts5(
    title,
    role,
    text,
    conversation_id UNINDEXED,
    message_id UNINDEXED,
    chunk_id UNINDEXED,
    tokenize = 'unicode61'
);
```

### `attachments`

- `id TEXT PRIMARY KEY`
- `message_id TEXT NOT NULL REFERENCES messages(id)`
- `conversation_id TEXT NOT NULL REFERENCES conversations(id)`
- `import_id TEXT NOT NULL REFERENCES imports(id)`
- `source_path TEXT NOT NULL`
- `filename TEXT`
- `mime_type TEXT`
- `size_bytes INTEGER`
- `content_sha256 TEXT`
- `extracted_text TEXT`
- `extracted_text_sha256 TEXT`
- `metadata_json TEXT NOT NULL DEFAULT '{}'`

Indexes:

- `idx_attachments_message(message_id)`
- `idx_attachments_conversation(conversation_id)`
- `idx_attachments_hash(content_sha256)`

### `subjects`

- `id TEXT PRIMARY KEY`
- `slug TEXT NOT NULL UNIQUE`
- `name TEXT NOT NULL`
- `description TEXT`
- `parent_subject_id TEXT REFERENCES subjects(id)`
- `source TEXT NOT NULL`
- `confidence REAL`
- `is_archived INTEGER NOT NULL DEFAULT 0`

Indexes:

- `idx_subjects_parent(parent_subject_id)`
- `idx_subjects_source(source)`

### `conversation_subjects`

- `conversation_id TEXT NOT NULL REFERENCES conversations(id)`
- `subject_id TEXT NOT NULL REFERENCES subjects(id)`
- `source TEXT NOT NULL`
- `confidence REAL`
- `notes TEXT`
- `PRIMARY KEY(conversation_id, subject_id)`

Indexes:

- `idx_conversation_subjects_subject(subject_id)`

### `chunk_subjects`

- `chunk_id TEXT NOT NULL REFERENCES message_chunks(id)`
- `subject_id TEXT NOT NULL REFERENCES subjects(id)`
- `source TEXT NOT NULL`
- `confidence REAL`
- `PRIMARY KEY(chunk_id, subject_id)`

Indexes:

- `idx_chunk_subjects_subject(subject_id)`

### `embedding_models`

- `id TEXT PRIMARY KEY`
- `provider TEXT NOT NULL`
- `model TEXT NOT NULL`
- `dimension INTEGER NOT NULL`
- `normalize INTEGER NOT NULL DEFAULT 1`
- `created_at TEXT NOT NULL`
- `metadata_json TEXT NOT NULL DEFAULT '{}'`

Unique constraints:

- `UNIQUE(provider, model, dimension, normalize)`

### `chunk_embeddings`

- `chunk_id TEXT NOT NULL REFERENCES message_chunks(id)`
- `embedding_model_id TEXT NOT NULL REFERENCES embedding_models(id)`
- `vector_backend TEXT NOT NULL`
- `vector_ref TEXT NOT NULL`
- `text_sha256 TEXT NOT NULL`
- `embedded_at TEXT NOT NULL`
- `is_stale INTEGER NOT NULL DEFAULT 0`
- `metadata_json TEXT NOT NULL DEFAULT '{}'`
- `PRIMARY KEY(chunk_id, embedding_model_id)`

Indexes:

- `idx_chunk_embeddings_backend(vector_backend, vector_ref)`
- `idx_chunk_embeddings_stale(is_stale)`
- `idx_chunk_embeddings_hash(text_sha256)`

### `memory_records`

Curated working memory lives here. It is separate from raw chat transcripts and should be trusted more only when provenance is clear.

- `id TEXT PRIMARY KEY`
- `record_type TEXT NOT NULL`
- `title TEXT NOT NULL`
- `body TEXT NOT NULL`
- `subject_id TEXT REFERENCES subjects(id)`
- `trust_level TEXT NOT NULL`
- `source_kind TEXT NOT NULL`
- `source_ref TEXT`
- `provenance_json TEXT NOT NULL DEFAULT '{}'`
- `status TEXT NOT NULL DEFAULT 'active'`
- `valid_from TEXT`
- `valid_to TEXT`
- `last_verified_at TEXT`
- `created_by TEXT NOT NULL`
- `metadata_json TEXT NOT NULL DEFAULT '{}'`

Indexes:

- `idx_memory_records_type(record_type)`
- `idx_memory_records_subject(subject_id)`
- `idx_memory_records_status(status)`
- `idx_memory_records_trust(trust_level)`

Recommended `record_type` values:

- `project_fact`
- `decision`
- `preference`
- `workflow`
- `open_loop`
- `lesson`
- `contact_note`
- `research_note`

### `memory_links`

- `from_kind TEXT NOT NULL`
- `from_id TEXT NOT NULL`
- `to_kind TEXT NOT NULL`
- `to_id TEXT NOT NULL`
- `link_type TEXT NOT NULL`
- `confidence REAL`
- `notes TEXT`
- `PRIMARY KEY(from_kind, from_id, to_kind, to_id, link_type)`

Indexes:

- `idx_memory_links_to(to_kind, to_id)`
- `idx_memory_links_type(link_type)`

### `retrieval_events`

- `id TEXT PRIMARY KEY`
- `created_at TEXT NOT NULL`
- `query TEXT NOT NULL`
- `query_sha256 TEXT NOT NULL`
- `caller TEXT NOT NULL`
- `command TEXT NOT NULL`
- `filters_json TEXT NOT NULL DEFAULT '{}'`
- `ranking_profile TEXT NOT NULL`
- `disclosure_depth TEXT NOT NULL`
- `result_count INTEGER NOT NULL`
- `redaction_applied INTEGER NOT NULL DEFAULT 1`
- `metadata_json TEXT NOT NULL DEFAULT '{}'`

Indexes:

- `idx_retrieval_events_created_at(created_at)`
- `idx_retrieval_events_caller(caller)`
- `idx_retrieval_events_query_hash(query_sha256)`

### `memory_command_runs`

This table mirrors memory-specific command metadata from `data/logs/<run_id>/command.json` so failures can be queried without reading every artifact file.

- `id TEXT PRIMARY KEY`
- `created_at TEXT NOT NULL`
- `command TEXT NOT NULL`
- `status TEXT NOT NULL`
- `input_path TEXT`
- `config_path TEXT`
- `artifact_dir TEXT NOT NULL`
- `started_at TEXT NOT NULL`
- `finished_at TEXT`
- `error_stage TEXT`
- `error_code TEXT`
- `error_message TEXT`
- `metadata_json TEXT NOT NULL DEFAULT '{}'`

Indexes:

- `idx_memory_command_runs_created_at(created_at)`
- `idx_memory_command_runs_command(command)`
- `idx_memory_command_runs_status(status)`
- `idx_memory_command_runs_error(error_stage, error_code)`

### `memory_trace_events`

Trace events are append-only breadcrumbs for local debugging. They should be coarse enough to read but detailed enough to identify the failed stage, source file, record ID, and write target.

- `id TEXT PRIMARY KEY`
- `run_id TEXT NOT NULL REFERENCES memory_command_runs(id)`
- `timestamp TEXT NOT NULL`
- `stage TEXT NOT NULL`
- `level TEXT NOT NULL`
- `source_kind TEXT`
- `source_ref TEXT`
- `record_id TEXT`
- `message TEXT NOT NULL`
- `details_json TEXT NOT NULL DEFAULT '{}'`

Indexes:

- `idx_memory_trace_events_run(run_id)`
- `idx_memory_trace_events_stage(stage)`
- `idx_memory_trace_events_level(level)`
- `idx_memory_trace_events_source(source_kind, source_ref)`

### `retrieval_exposures`

- `id TEXT PRIMARY KEY`
- `retrieval_event_id TEXT NOT NULL REFERENCES retrieval_events(id)`
- `source_kind TEXT NOT NULL`
- `source_id TEXT NOT NULL`
- `rank INTEGER NOT NULL`
- `score REAL NOT NULL`
- `score_breakdown_json TEXT NOT NULL DEFAULT '{}'`
- `disclosure_tier TEXT NOT NULL`
- `exposed_fields_json TEXT NOT NULL`
- `redacted INTEGER NOT NULL DEFAULT 1`
- `created_at TEXT NOT NULL`

Indexes:

- `idx_retrieval_exposures_event(retrieval_event_id)`
- `idx_retrieval_exposures_source(source_kind, source_id)`
- `idx_retrieval_exposures_rank(retrieval_event_id, rank)`

### `feedback_events`

- `id TEXT PRIMARY KEY`
- `retrieval_event_id TEXT REFERENCES retrieval_events(id)`
- `source_kind TEXT NOT NULL`
- `source_id TEXT NOT NULL`
- `rating TEXT NOT NULL`
- `notes TEXT`
- `created_at TEXT NOT NULL`
- `metadata_json TEXT NOT NULL DEFAULT '{}'`

Indexes:

- `idx_feedback_source(source_kind, source_id)`
- `idx_feedback_rating(rating)`
- `idx_feedback_created_at(created_at)`

### `deletions`

- `id TEXT PRIMARY KEY`
- `source_kind TEXT NOT NULL`
- `source_id TEXT NOT NULL`
- `reason TEXT NOT NULL`
- `deleted_at TEXT NOT NULL`
- `deleted_by TEXT NOT NULL`
- `tombstone_json TEXT NOT NULL DEFAULT '{}'`

Indexes:

- `idx_deletions_source(source_kind, source_id)`
- `idx_deletions_deleted_at(deleted_at)`

## Disclosure Tiers

Retrieval should expose progressively more context based on semantic closeness, explicit `--depth`, and privacy rules.

| Tier | Intended exposure |
| --- | --- |
| `far` | Conversation title, date, subject labels, one-line summary, source IDs |
| `medium` | Matching chunks, snippets, role, timestamps, score breakdown |
| `close` | Matching chunks plus nearby turns, extracted decisions, linked curated records |
| `full` | Wider conversation window and attachments metadata, still redacted by default |

Default agent-facing retrieval should use `medium`. `full` should be opt-in and logged.

## Ranking Inputs

The first hybrid scorer should combine:

- FTS rank.
- Vector similarity when embeddings exist.
- Subject or project match.
- Recency.
- Curated memory trust level.
- User feedback.
- Penalties for deleted, archived, low-confidence, or stale records.

Every result returned to an agent should include `score_breakdown_json` so ranking remains inspectable.

## Observability Contract

The memory system should be trace-first. A command is not complete unless a user can inspect what happened from this terminal without attaching a debugger.

Required behavior:

- Every memory command creates a `run_id`.
- Every memory command writes `data/logs/<run_id>/command.json`.
- Every memory command with multiple stages writes `data/logs/<run_id>/trace.jsonl`.
- Commands that validate state write `data/logs/<run_id>/validation_report.json`.
- Commands that import data write `data/logs/<run_id>/import_report.json`.
- Commands that rank results write `data/logs/<run_id>/search_explain.json` when `--explain` is set.
- Failure output must include `run_id`, `stage`, `error_code`, and the artifact path.

`command.json` required fields:

- `run_id`
- `command`
- `argv`
- `started_at`
- `finished_at`
- `status`
- `config_path`
- `artifact_dir`
- `input_paths`
- `output_paths`
- `sqlite_path`
- `schema_version`
- `error`

`trace.jsonl` required fields per line:

- `timestamp`
- `stage`
- `level`
- `message`
- `source_kind`
- `source_ref`
- `record_id`
- `details`

Recommended trace stages:

- `load_config`
- `discover_input`
- `hash_raw_files`
- `parse_export`
- `normalize_conversations`
- `normalize_messages`
- `chunk_messages`
- `write_jsonl`
- `migrate_sqlite`
- `write_sqlite`
- `refresh_fts`
- `embed_chunks`
- `retrieve_candidates`
- `rank_results`
- `apply_disclosure`
- `redact_context`
- `write_audit`
- `validate_state`

Failure categories:

- `input_not_found`
- `unsupported_export_shape`
- `invalid_json`
- `parse_error`
- `schema_mismatch`
- `sqlite_migration_failed`
- `sqlite_write_failed`
- `fts_refresh_failed`
- `embedding_backend_unavailable`
- `vector_write_failed`
- `privacy_policy_blocked`
- `redaction_failed`
- `invariant_failed`

## Verification Commands

These commands are mandatory before the system should be considered usable.

```bash
lagent ingest-chatgpt --input data/chatgpt_exports/raw --dry-run --trace
lagent ingest-chatgpt --input data/chatgpt_exports/raw --trace
lagent memory-check
lagent memory-search "query" --explain --json
lagent memory-trace <run-id>
```

`lagent ingest-chatgpt --dry-run --trace` should verify:

- input paths exist
- export shape is recognized
- raw files can be hashed
- conversations/messages can be counted
- expected IDs can be generated
- planned JSONL and SQLite writes are listed
- no raw files are modified

`lagent memory-check` should verify:

- raw export paths referenced by imports still exist
- parsed JSONL files are valid JSONL
- required JSONL fields are present
- SQLite schema version is current
- row counts match import reports
- every message references an existing conversation
- every chunk references an existing message
- FTS row count matches non-deleted chunk count
- embedding metadata matches current chunk hashes when embeddings exist
- deleted/tombstoned content is excluded from retrieval
- retrieval audit rows have matching exposure rows

`lagent memory-search "query" --explain --json` should return:

- `run_id`
- `query`
- `ranking_profile`
- `results`
- `candidate_counts`
- `filters_applied`
- `score_breakdown` per result
- `source_kind` per result
- `disclosure_tier` per result
- `exposed_fields` per result

`lagent memory-trace <run-id>` should show:

- command arguments
- artifact paths
- stage timeline
- records read and written
- candidates found, filtered, and ranked
- validation failures
- model calls and redaction summaries when applicable
- exact file, source record, or SQLite table associated with an error

## Migration Expectations

- Use explicit numbered migrations stored in code, not ad hoc `CREATE TABLE` drift.
- Insert a row into `schema_migrations` inside the same transaction as each migration.
- Migrations must be forward-only for normal operation.
- Destructive schema changes require a backup under `data/memory/backups/`.
- Adding a vector backend must not change chunk IDs or public memory IDs.
- Re-ingesting the same raw export must be idempotent and update derived rows only when content hashes change.

## Privacy Defaults

- Raw exports, parsed JSONL, SQLite data, embeddings, retrieval logs, and curated memory remain local by default.
- External API use must be explicitly configured and visible in retrieval or model-call logs.
- Retrieval events must record what content was exposed to an agent.
- URL tracking parameters should be stripped when saved as links in memory records.
- Deletion uses tombstones so future re-ingestion does not silently re-expose blocked content.
- Secret redaction should run before any content is passed to an LLM, including local models when configured.

## Initial Commands This Contract Supports

```bash
lagent ingest-chatgpt --input data/chatgpt_exports/raw
lagent ingest-chatgpt --input data/chatgpt_exports/raw --dry-run --trace
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
