# ADR 0001: Evolve Toward Object-Centric Memory

Status: accepted incrementally

## Constraint

Transcript chunks are evidence, not durable identity. Treating each chunk or
one canonical note as the final memory abstraction fragments entities, loses
execution history, and lets old state compete with corrected state.

## Decision

Preserve immutable source evidence and evolve the memory model through explicit
events, persistent objects, typed claims, and materialized current state. Add
these layers incrementally so existing retrieval, candidate review, curated
records, and context packets remain usable during the transition.

Candidate merges must retain every supporting source, keep assistant suggestions
separate from user evidence, and refuse unresolved contradictions. Current state
is derived from evidence and temporal resolution rather than obtained by
overwriting history.

## Rejected Alternatives

- Use a transcript chunk as a durable object.
- Treat one mutable note as the complete memory model.
- Force low-confidence candidates into a shared entity.
- Replace the working retrieval stack in one migration.

## Validation

- Candidate-merge tests prove that several sources can create one reviewed
  record without discarding evidence.
- Canonicalization tests keep opposing user claims as conflicts.
- Context tests keep assistant advice out of factual state and demote corrected
  observations without deleting their audit history.

## Consequence

The current implementation provides compatible groundwork, not a claim that a
general object/event/claim world model is complete. The incremental boundary
keeps failures observable and rollback practical while richer structures are
added.
