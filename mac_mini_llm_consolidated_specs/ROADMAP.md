# Project Roadmap

**Status:** Current project plan  
**Date:** 2026-07-27

## 1. Current reality

The project has moved beyond the original speculative phase ordering.

The historical local-agent plan intentionally deferred ChatGPT memory/RAG until after basic repo indexing and coding-assistant infrastructure. The current system already has working memory-oriented MCP capabilities, full-history ingestion, candidate review, subject browsing, retrieval traces, and a curated-memory layer.

Therefore the current roadmap should follow the actual implementation state rather than the original "future upgrade" ordering.

## 2. Current observed implementation baseline

Recent live inspection showed approximately:

```text
ChatGPT conversations: 1,512
messages:              16,573
message chunks:        39,797
candidate memories:    39,795
curated memory records: 8
chunk embeddings:       2
retrieval events:      27
retrieval exposures:   168
subjects:              13
```

This is an operational snapshot, not an architectural requirement.

Important observed gaps:

- semantic retrieval is effectively not populated yet
- curated memory is not reliably participating in subject-scoped context retrieval
- some context queries return weak/incidental lexical matches
- tool schemas do not expose all runtime enum constraints
- candidate review filtering is directionally useful but still permits contextless fragments and duplicates
- current context packets may expose retrieval metadata without enough usable semantic content

## 3. Current phase: Agent-Usable Memory Substrate

### Goal

Make the existing memory layer reliably improve downstream AI answers before adding sophisticated autonomy.

### Exit criteria

The phase is complete when:

- structured/object memory reliably participates in retrieval
- semantic retrieval is populated and measured
- hard constraints and current state reliably dominate weak history
- context packets expose usable canonical information
- provenance and epistemic status are preserved
- feedback can be submitted from real downstream use
- representative regressions are captured as evals
- combined memory outperforms raw-history retrieval on selected tasks

## 4. Priority 0: Consolidate architecture

**Status:** This document set.

Tasks:

```text
[done] define authoritative document precedence
[done] promote object-oriented memory architecture
[done] define AI-facing memory contract
[done] separate security/control plane
[done] define feedback/build loop
[done] update project roadmap
[next] place these docs in the canonical project repo
[next] mark older specs historical/partially superseded
```

## 5. Priority 1: Repair retrieval fundamentals

### 5.1 Populate semantic retrieval

Current chunk embedding coverage is insufficient.

Implement/verify:

- resumable embedding pipeline
- embedding model/version metadata
- health/status reporting
- partial-failure recovery
- semantic index consistency checks
- measurable coverage

Acceptance:

```text
semantic coverage is near complete for intended searchable corpus
memory_context reports non-zero semantic contribution on semantic test queries
semantic retrieval improves fixture recall over lexical-only baseline
```

### 5.2 Integrate curated/object memory

Ensure promoted/canonical memory is retrievable even when source records lack legacy subject fields.

Do not force canonical memory to depend on incidental chunk-domain metadata.

Acceptance:

```text
a query about a curated claim can retrieve that claim directly
subject/domain scoping does not accidentally exclude canonical records
current-state claims can outrank raw chunks
```

### 5.3 Fix agent-facing tool contracts

Publish actual enum schemas for fields such as disclosure depth.

Acceptance:

- invalid enum values are rejected at schema/tool-discovery time
- read tools return predictable structured status/run/trace metadata
- tool-call failures are distinguishable from zero-result success

## 6. Priority 2: Agent context compiler v2

Implement the `AI_MEMORY_CONTRACT.md`.

Required packet semantics:

```text
critical constraints
current state
preferences
outcomes
failures/lessons
contradictions/qualifications
inferred patterns
analogies
uncertainty
provenance
```

Remove unlabeled recommendation/synthesis from the core factual packet.

Acceptance:

- a realistic cooking query receives usable constraints and completed-project state
- canonical state replaces redundant raw chunks where available
- raw evidence can still be drilled into
- inferred patterns are labeled as inference

## 7. Priority 3: Structured feedback MCP

Add append-only `submit_agent_feedback`.

Requirements:

- run/trace linkage
- failure taxonomy
- observed vs expected behavior
- severity/confidence
- source IDs
- server-attached build metadata
- no direct production mutation

Acceptance:

- ChatGPT/downstream agent can report a retrieval failure in one tool call
- Codex can later reproduce the run from stored identifiers
- feedback records are auditable and immutable from ordinary agent tooling

## 8. Priority 4: Counterfactual memory eval harness

Build representative eval cases from real usage.

Initial cases should include:

### Recipes/Baking candidate quality

Verify high-signal filtering removes:

- contextless fragments
- assistant-like noise
- obvious duplicates
- incidental domain mentions

while preserving durable preferences/outcomes.

### Baking recommendation context

Query:

```text
Suggest the next ambitious baking project based on established preferences,
completed projects, constraints, and recent outcomes.
```

Expected:

- hard constraints present
- completed projects present
- strongest outcomes present
- unrelated AI/career history suppressed
- canonical state preferred over raw transcript

### Domain drift

Query:

```text
What should I make for dinner?
```

Expected:

- cooking/relationship preferences may appear
- unrelated finance/AI theory does not

### Attribution

Ensure Devyn/David/Tank/Francie claims do not cross entities.

### Temporal supersession

Ensure current partner/job/location/state wins over superseded state.

### OpenAI/local combined memory

Compare:

```text
no memory
OpenAI/platform memory
local memory
raw-history RAG
combined
```

Score downstream answer quality.

## 9. Priority 5: Minimal object/event/claim layer

Implement the minimum viable object architecture:

```text
objects
events
claims
claim_evidence
relationships
materialized object state
```

Focus first on cooking/baking because outcomes and identity resolution are observable.

Acceptance:

- financiers resolve to one object across multiple conversations
- recipe attempts append as events
- current canonical state remains separate from attempt history
- preferences attach to people, not duplicated recipe text
- claim provenance is queryable

## 10. Priority 6: Active memory formation

New usage should form structured active memory from:

```text
explicit user statements
reported outcomes
corrections
decisions
completed tasks
confirmed preference changes
```

Active memory should not require broad human review when evidence is explicit and low-risk, but should retain provenance and reversible state updates.

Assistant inference remains separately labeled.

## 11. Priority 7: Feedback → regression → Codex loop

Automate:

```text
feedback queue
trace replay
fixture generation
regression eval
candidate patch
staging tests
A/B comparison
change report
```

Keep production promotion human-triggered at this stage.

## 12. Priority 8: Retrieval credit assignment

Extend retrieval telemetry beyond simple exposure.

Track where practical:

```text
retrieved
exposed
used
ignored
corrected
caused violation
supported final answer
successful downstream result
```

Use these signals to improve retrieval utility.

Do not conflate utility with factual confidence.

## 13. Priority 9: Temporal/state intelligence

Add:

```text
temporal classes
revalidation policies
automatic stale candidates
supersession/qualification workflow
current-state materialization
```

Prioritize volatile domains:

- jobs
- location
- active projects
- portfolio
- health routines
- equipment/current inventory

## 14. Priority 10: Richer consolidation

Only after the substrate is reliable:

- duplicate proposition consolidation
- automatic object alias resolution
- repeated-pattern abstraction
- experiment promotion
- conditional contradiction reconciliation
- world-model compression

## 15. Priority 11: Exploratory memory

Later:

- background "dreaming"/consolidation
- analogy hunter
- open-loop activation
- creative cross-domain traversal
- specialized curator/skeptic roles
- long-running local inference

These features should be evaluated against downstream benefit, not implemented because they resemble human cognition.

## 16. Security/build isolation track

In parallel with memory work:

### Near term

- dedicated production service identity
- builder/staging isolation
- sanitized test snapshots
- separate credentials
- append-only feedback queue
- human deployment gate

### Later

- signed/verified promotion artifacts
- stronger automated rollback
- reproducible staging environments
- policy-as-code checks
- automated security regression suite

Do not grant builder authority over the security/control plane.

## 17. Deferred work

Defer until agent-facing memory quality is strong:

- large multi-agent swarms
- complex dashboard/UI work
- graph database migration without demonstrated need
- unrestricted local autonomous maintenance
- automatic production code deployment
- broad ingestion of sensitive third-party data
- full personal-traffic surveillance/CRM ingestion

## 18. Project success milestones

### Milestone A: Useful retrieval

The downstream AI can ask normal personal questions and local memory measurably helps.

### Milestone B: Trustworthy state

The system distinguishes current truth, history, inference, contradiction, and provenance.

### Milestone C: Adaptive improvement

Real usage automatically creates structured feedback and regression cases.

### Milestone D: Safe semi-autonomous development

Codex can diagnose, patch, test, and benchmark memory-system changes in staging without production authority.

### Milestone E: Rich persistent world model

Objects, events, relationships, abstractions, and open loops support high-quality cross-domain reasoning without flooding context.

## 19. Immediate next engineering tickets

Recommended next tickets for Codex:

```text
1. Import these authoritative docs into the project repo and mark old specs historical.
2. Expose actual enum constraints in memory MCP tool schemas.
3. Diagnose why curated memory is absent from subject-scoped memory_context.
4. Implement/resume full embedding coverage and add coverage health metrics.
5. Add the first agent-context regression fixture for the ambitious-baking query.
6. Implement append-only submit_agent_feedback with run/trace/build metadata.
7. Update memory_context output toward the AI Memory Contract packet shape.
8. Split retrieval utility from factual confidence in schema/logic.
9. Draft minimal object/event/claim migration plan without rewriting all storage at once.
10. Add combined-memory counterfactual eval harness.
```
