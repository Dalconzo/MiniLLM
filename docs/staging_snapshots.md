# Sanitized Staging Snapshots

`local_agent_lab.staging_snapshot` creates bounded structural fixtures for the isolated builder/staging plane. It is a one-way, administrator-triggered exporter, not a database clone, backup, import tool, or production synchronization mechanism.

## Safety Boundary

The exporter:

- opens the source SQLite database with `mode=ro` and `PRAGMA query_only=ON`;
- requires an explicit `administrator_authorized=True` configuration value;
- accepts only the `synthetic` content mode;
- writes a JSON artifact only beneath a declared, disjoint staging root;
- remaps every exported identifier with a stable, namespaced SHA-256 identifier;
- caps rows per dataset and total serialized bytes;
- writes the artifact atomically with mode `0600`;
- provides no import, apply, synchronization, or write-back operation.

The authorization flag is a fail-closed API guard, not a substitute for OS permissions. The production service identity must still be the only identity able to read the production database and invoke this exporter. The builder should receive only the resulting snapshot.

## Data Contract

The snapshot contains structural subsets of:

- subjects;
- conversation and chunk subject assignments;
- candidate memories;
- curated memory records;
- memory links.

The exact output fields are published in `FIELD_ALLOWLIST` and embedded in every snapshot. Free text is replaced with deterministic synthetic fixture text. Unknown or sensitive-looking categorical values collapse to `other`.

The exporter never selects or emits:

- raw messages, chunks, candidate content, record titles, record bodies, descriptions, summaries, or notes;
- metadata, source-link, or provenance JSON;
- original identifiers or exact source event timestamps;
- source filesystem paths or attachments;
- embedding vectors or model metadata;
- retrieval events, feedback, audit logs, traces, or run records;
- credentials, passwords, tokens, keys, certificates, auth data, or tunnel configuration.

## Determinism And Provenance

The administrator supplies a timezone-aware `snapshot_at`. Repeating an export against unchanged source rows with the same timestamp, namespace, limits, and exporter version produces byte-identical JSON and stable remapped IDs.

Each artifact records:

- `staging_snapshot_v1` schema version;
- exporter version;
- canonical UTC snapshot time;
- source schema migration versions, without migration names or paths;
- SHA-256 digest of the sanitized datasets;
- deterministic snapshot ID;
- row and byte limits;
- per-dataset available/exported/truncated counts;
- an explicit statement that write-back and production-import authority are absent.

Changing `snapshot_at`, the namespace, limits, exporter implementation, or selected source rows intentionally creates a different artifact.

## Example

This API is deliberately not wired into the builder CLI. A trusted administrator-side process may call it after establishing OS-level read access to production and write access only to the staging snapshot destination:

```python
from pathlib import Path

from local_agent_lab.staging_snapshot import StagingSnapshotConfig, export_staging_snapshot

export_staging_snapshot(
    StagingSnapshotConfig(
        source_db=Path("/srv/minillm-production/memory/chatgpt_memory.sqlite3"),
        source_root=Path("/srv/minillm-production/memory"),
        destination=Path("/srv/minillm-staging/snapshots/memory-2026-08-20.json"),
        staging_root=Path("/srv/minillm-staging"),
        snapshot_at="2026-08-20T18:00:00Z",
        administrator_authorized=True,
        max_rows_per_dataset=500,
        max_output_bytes=2_000_000,
    )
)
```

The production identity should transfer ownership of the completed artifact to the staging identity through an administrator-controlled handoff. The staging identity must not receive production database, credential, audit, tunnel, or deployment-control access.

## Rejection Rules

Export fails before writing when:

- authorization is absent or raw/passthrough content is requested;
- source and staging roots overlap;
- the source escapes its declared root, is a symlink, is not a SQLite file, or lacks required memory tables;
- the destination escapes staging, is a symlink, or is not JSON;
- source or destination paths indicate auth, secret, tunnel, audit, trace, token, credential, key, certificate, password, or log data;
- row/byte limits exceed hard policy bounds;
- serialized output exceeds the configured byte limit.

Snapshots are private runtime artifacts and remain outside source control. Generated synthetic fixtures should be authored separately when a committed regression fixture is needed.
