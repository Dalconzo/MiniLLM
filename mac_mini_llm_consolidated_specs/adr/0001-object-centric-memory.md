# ADR 0001: Promote Object-Centric Memory to the Primary Architecture

**Status:** Accepted  
**Date:** 2026-07-27

## Context

Earlier memory designs centered a canonical `memory` record extracted from conversation chunks.

Subsequent recipe-memory work exposed repeated identity fragmentation, duplicate static notes, loss of execution history, contamination of tested knowledge by speculative ideas, and difficulty preserving current state separately from past attempts.

## Decision

Adopt the following conceptual hierarchy:

```text
raw evidence
→ events
→ persistent objects
→ claims + evidence
→ materialized current state
→ world model
→ retrieval
```

Canonical "memory" remains a useful compatibility concept but is no longer the final architectural abstraction.

## Consequences

Positive:

- stable entity identity
- cleaner current-state retrieval
- preserved history
- better provenance
- better contradiction handling
- better compression
- direct support for recipe/project/person state

Costs:

- entity resolution complexity
- migrations
- claim/state reconciliation logic
- more explicit schemas

## Implementation strategy

Do not rewrite the whole system immediately.

Add the minimal object/event/claim layer incrementally and bridge existing curated records into it.
