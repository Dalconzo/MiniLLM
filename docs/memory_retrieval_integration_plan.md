# Memory Retrieval Integration Plan

Date: 2026-08-02

This note captures the diagnostic-first work from browser acceptance follow-up beads `lagent-244`, `lagent-246`, `lagent-247`, and `lagent-248`.

## Implemented hooks

- Feedback materialization now has explicit lifecycle tables for reviewed ranking controls. `submit_agent_feedback` still creates append-only submitted feedback only; live ranking is not changed until a separate reviewed/applied control is created.
- Context requests now separate `retrieval_depth`, `packet_detail`, and `disclosure_tier`. The old `depth` argument remains as a backward-compatible alias.
- Context packets now record the separated control values in `task`, classify basic evidence slots, avoid duplicate assistant-authored uncertainty entries, and emit explicit empty-slot notes.
- Subject summaries now expose explicit conversation counts, chunk-derived conversation counts, message counts, and provenance warnings. Memory status validation also checks subject provenance anomalies.

## Remaining implementation work

- Wire applied feedback controls into ranking behind a reviewed/apply step. Keep raw feedback submission non-mutating.
- Replace the lightweight packet slot classifier with true slot-specific retrieval: current state, constraints, preferences, outcomes, failures, contradictions, and uncertainty should each get targeted evidence queries.
- Add review UI/CLI for feedback controls so a human can move controls through submitted, reviewed, applied, superseded, and rejected states.
- Run a corpus-wide subject provenance audit before rewriting assignments; warnings are now visible, but reassignment should remain a separate migration.

## Test Gates

- Unit and integration tests must show that submitted feedback alone does not alter ranking.
- Applied controls must expose nonzero feedback score components and rollback metadata.
- Context trace output must keep requested controls consistent with packet task fields.
- Subject counts must never show chunks with zero conversations unless explicitly marked as unresolved/synthetic provenance.
