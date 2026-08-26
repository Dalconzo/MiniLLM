# Memory Design Rationale and Implementation Status

## Core distinction

MiniLLM is not designed as one opaque vector store. It separates source-faithful
evidence, searchable chunks, candidate memories, curated records, retrieval
events, and agent context packets because each layer has a different trust and
mutation contract.

The normative architecture extends that sequence to events, persistent objects,
claims, materialized current state, and a world model. Those later layers are a
design direction, not a claim about the complete current implementation.

## As-built versus planned

| Layer | Purpose | Status | Repository evidence |
| --- | --- | --- | --- |
| Raw/parsed evidence | Preserve source content and provenance; rebuild derived state. | **As-built** for ChatGPT exports. | [chatgpt_ingest.py](../../src/local_agent_lab/memory/chatgpt_ingest.py), [memory operating model](../memory_operating_model.md#raw-archive) |
| SQLite records and FTS chunks | Stable IDs, filters, exact retrieval, and audit-friendly counts. | **As-built.** | [search.py](../../src/local_agent_lab/memory/search.py), [`test_search_chatgpt_memory_returns_citations_and_score_breakdown`](../../tests/test_memory_search.py#L67) |
| Semantic chunk retrieval | Local embedding lookup and hybrid ranking. | **As-built**, with backend health tracked separately from source freshness. | [embeddings.py](../../src/local_agent_lab/memory/embeddings.py), [observability.py](../../src/local_agent_lab/memory/observability.py) |
| Candidate memories | Pending extracted propositions for review; not trusted truth. | **As-built.** | [candidate_extraction.py](../../src/local_agent_lab/memory/candidate_extraction.py), [candidate_canonicalization.py](../../src/local_agent_lab/memory/candidate_canonicalization.py) |
| Curated memory records | Explicit durable records with trust, status, source links, and review operations. | **As-built.** | [curated.py](../../src/local_agent_lab/memory/curated.py), [candidate_merge.py](../../src/local_agent_lab/memory/candidate_merge.py) |
| Retrieval events and context packets | Record exposure and compile bounded agent-facing evidence. | **As-built.** | [audit.py](../../src/local_agent_lab/memory/audit.py), [context_packet.py](../../src/local_agent_lab/memory/context_packet.py) |
| Reviewed feedback controls | Keep AI feedback inert until a separate control is explicitly applied. | **As-built, limited.** | [feedback.py](../../src/local_agent_lab/memory/feedback.py), [`test_search_chatgpt_memory_consumes_only_applied_feedback_controls`](../../tests/test_memory_search.py#L1256) |
| Events | Immutable meaningful occurrences linked to evidence and objects. | **Planned.** | [ADR 0001](../architecture/adr/0001-object-centric-memory.md) |
| Persistent objects and claims | Stable identity, aliases, typed propositions, contradictions, and evidence. | **Planned/incremental.** Candidate merging is compatible groundwork, not the complete object layer. | [ADR 0001](../architecture/adr/0001-object-centric-memory.md) |
| Materialized current state and world model | Derive current state without destroying history; connect typed relationships. | **Planned.** Context-packet state resolution is not a general world model. | [ADR 0001](../architecture/adr/0001-object-centric-memory.md) |

## Memory types

The as-built ontology defines semantic categories including episodic evidence,
facts, procedures, preferences, constraints, workarounds, failures, decisions,
skills, projects, open loops, hypotheses, risks, and relationships. Curated
storage currently uses a smaller record vocabulary such as decision, lesson,
preference, project, research note, and workflow.

These types are not all separate databases or complete object classes. They are
semantic roles used for extraction, review, filtering, and context compilation.
The broader event/object/claim interpretation remains planned.

Evidence:
[ontology.py](../../src/local_agent_lab/memory/ontology.py#L23-L53).

## Confidence and epistemic status

Confidence is deliberately not treated as one undifferentiated score.

**As-built:**

- Source role is mapped to epistemic status such as `user_reported`,
  `assistant_suggested`, or `system_inferred`.
- Confidence bases describe why a claim is treated a certain way, including
  `single_user_statement`, `repeated_user_confirmation`,
  `assistant_suggestion_only`, and `uncertain_context`.
- Curated records have explicit trust levels and lifecycle statuses.
- Context packets currently assign bounded heuristic numeric confidence from
  trust, source kind, and source role.

Evidence:
[ontology.py](../../src/local_agent_lab/memory/ontology.py#L89-L171) and
[context_packet.py](../../src/local_agent_lab/memory/context_packet.py#L563-L585).

**Planned:** Claim-level truth confidence with supporting/contradicting evidence,
temporal validity, and confidence basis is normative architecture but is not yet
a complete generalized claim store. Current packet confidence should not be
presented as calibrated probability.

## Reinforcement, feedback, and decay

Three concepts are intentionally separate in the design:

1. whether a claim is true;
2. whether retrieving it was useful;
3. whether it was relevant to a particular task.

**As-built:**

- User feedback can record `up`, `down`, `saved`, `ignored`, or `resolved`.
- The current curated-record lifecycle uses a limited threshold heuristic: two
  negative ratings can mark an active record stale, while repeated positive or
  saved feedback can reactivate stale/superseded records.
- Agent feedback submission creates append-only evidence in `submitted` state.
- Search consumes only separate feedback controls whose status is `applied`.

Evidence:
[feedback.py](../../src/local_agent_lab/memory/feedback.py#L12-L38),
[feedback.py](../../src/local_agent_lab/memory/feedback.py#L574-L602), and
[`test_feedback_ranking_control_is_explicit_reviewed_hook`](../../tests/test_memory_feedback.py#L127).

**Planned/limited:** There is no demonstrated general time-decay engine, learned
credit assignment, or automatic claim-confidence update. Temporal classes and
decay/revalidation policy remain architecture goals. The current feedback status
heuristic should not be described as a mature reinforcement system.

## Contamination controls

Contamination means that weak, wrong-domain, assistant-authored, stale,
duplicated, or unreviewed material is presented as trusted current user state.

**As-built controls include:**

- role and epistemic labels for user, assistant, system, and curated sources;
- assistant-only evidence cannot form a canonical user fact;
- hard subject/domain eligibility when cross-domain retrieval is disabled;
- relevance floors and explicit no-result behavior;
- candidate rejection for non-standalone, meta, duplicate, conflicting, or
  wrong-domain material;
- tombstones/source exclusions and stale/superseded status handling;
- context slots that keep assistant advice out of factual state;
- append-only agent feedback that does not directly mutate truth.

Evidence:
[candidate_canonicalization tests](../../tests/test_candidate_canonicalization.py),
[domain-boundary test](../../tests/test_memory_search.py#L923), and
[context contamination test](../../tests/test_memory_search.py#L196).

**Known limitation:** Retained acceptance reports still show candidate-review
noise and incomplete context quality. The controls reduce contamination; they do
not justify a claim that contamination has been eliminated.

## Provenance

Provenance is the mechanism that lets an agent distinguish a user statement,
assistant suggestion, curated record, inference, and system observation.

**As-built:**

- Imported conversations, messages, and chunks receive stable public IDs.
- Retrieved items expose source kind, role, timestamp, subject/domain metadata,
  retrieval source, and score components according to disclosure policy.
- Retrieval events record which source IDs were exposed for a run.
- Candidate merges preserve links to all supporting evidence and keep user and
  assistant evidence separate.
- Context packets retain source IDs and epistemic labels.

Evidence:
[`test_search_chatgpt_memory_returns_citations_and_score_breakdown`](../../tests/test_memory_search.py#L67),
[`test_apply_candidate_merge_creates_one_record_and_preserves_all_evidence`](../../tests/test_candidate_merge_apply.py#L86), and
the [sanitized failure evidence](design-changing-failures.md).

Provenance supports audit and correction. It does not prove that the underlying
claim is true.

## Routing

MiniLLM currently has two distinct routing layers.

**Model/task routing, as-built:** A task maps through configuration to a model
profile and an explicit routing label. This is deterministic configuration, not
an autonomous learned router.

Evidence:
[model_router.py](../../src/local_agent_lab/llm/model_router.py) and
[agent.yaml](../../config/agent.yaml#L51-L67).

**Memory routing, as-built:** Query-domain detection, subject filters, lenses,
effort, and cross-domain policy govern candidate eligibility and ranking.
Cross-domain analogy is separate from direct evidence by design.

Evidence:
[domain_scoping.py](../../src/local_agent_lab/memory/domain_scoping.py),
[search.py](../../src/local_agent_lab/memory/search.py), and
[`test_search_chatgpt_memory_blocks_wrong_domain_subject_leak_when_cross_domain_disabled`](../../tests/test_memory_search.py#L923).

**Planned:** Object-aware routing, graph traversal, richer analogy exploration,
and dynamic multi-model escalation are not demonstrated as a complete system.

## Current claim boundary

The defensible current claim is that MiniLLM implements and tests a governed
local memory prototype with source-backed retrieval, explicit abstention,
epistemic labels, reviewable feedback controls, context packets, and audit
traces. The repository does not yet support claims of a complete human-like
world model, calibrated belief engine, autonomous reinforcement/decay, or final
production readiness.
