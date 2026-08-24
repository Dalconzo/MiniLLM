# AI Memory Contract

**Status:** Normative  
**Audience:** Memory system implementers, MCP tool authors, downstream agents, Codex  
**Purpose:** Define what an AI agent needs from the memory system, independent of implementation details.

## 1. Core contract

The memory system exists to improve downstream AI decisions.

For a given task, it should expose the smallest trustworthy context that helps the agent answer:

```text
What do I already know that changes this answer?
How sure am I?
Who or what does it apply to?
Is it still true?
Why do I believe it?
What relevant contradiction or uncertainty exists?
What useful information might be missing?
```

The system is successful when downstream answers improve, not merely when retrieval recall improves.

## 2. Required distinction: evidence vs belief

The agent must be able to distinguish:

```text
raw evidence
current canonical claim
historical state
inference
assistant hypothesis
analogy
unresolved contradiction
```

A context packet must not flatten these into undifferentiated prose.

## 3. Agent-facing context packet

The preferred packet shape is:

```yaml
task:
  query: ...

critical_constraints:
  - claim: ...
    strength: hard
    confidence: ...
    epistemic_status: ...
    source_ids: [...]

current_state:
  - object: ...
    claim: ...
    temporal_class: ...
    confidence: ...
    source_ids: [...]

relevant_preferences:
  - claim: ...
    strength: ...
    confidence: ...
    source_ids: [...]

relevant_outcomes:
  - object/event: ...
    outcome: ...
    relevance: ...
    source_ids: [...]

failures_and_lessons:
  - ...

contradictions_and_qualifications:
  - ...

inferred_patterns:
  - pattern: ...
    confidence: ...
    supporting_ids: [...]
    epistemic_status: system_inference

analogies:
  - pattern: ...
    source_domain: ...
    target_domain: ...
    confidence: ...

uncertainty:
  - ...

omitted_but_available:
  - category: raw_history
    reason: low marginal value

provenance:
  retrieval_event_id: ...
  context_packet_id: ...
```

The exact serialization may vary. The semantic distinctions may not.

## 4. Critical ordering

The context compiler should prioritize approximately:

```text
1. Hard constraints / safety-critical facts
2. Directly relevant current state
3. Strong stable preferences
4. Relevant prior outcomes / failures
5. Current plans / open loops
6. Inferred patterns
7. Cross-domain analogies
8. Raw historical evidence
```

This is not a fixed ranking formula. It is a behavioral priority.

## 5. Current truth

The agent should receive current truth in preference to historical truth when the task asks about the present.

Historical states remain available when:

- the user asks for history
- the current claim is disputed
- the old state explains a change
- the agent needs provenance

Superseded state must not be presented as active without an explicit stale/historical label.

## 6. Hard constraints

Hard constraints should behave like gates.

Examples:

```text
dietary incompatibility
known allergen or intolerance
explicit legal/security boundary
required equipment limitation
explicit user prohibition
```

A hard constraint should not lose to a semantically attractive recommendation.

The agent must be able to distinguish `hard` from `strong`, `soft`, or `contextual`.

## 7. Preferences

Preferences influence ranking but do not automatically invalidate alternatives.

The system should distinguish:

```text
explicit preference
repeated observed pattern
one-off choice
assistant inference
```

An inferred preference should not be stated to the user as if explicitly declared.

## 8. Outcomes and negative memory

The agent needs prior outcomes to avoid repeating bad suggestions.

Useful outcome context includes:

```text
attempted
completed
successful
mixed
failed
confounded
rejected
repeatable
not worth repeating
```

Where possible, preserve causal context:

```text
mixed result because rushed finishing stage
```

is more useful than:

```text
mixed result
```

## 9. Inference and abstraction

The system may infer higher-level patterns across events and objects.

Example:

```text
User tends to prefer technically refined desserts with richness balanced by acidity or bitterness.
```

Such abstractions are valuable but must carry:

```text
epistemic_status: system_inference
supporting evidence
confidence basis
```

Inference is allowed. Silent promotion of inference to user fact is not.

## 10. Contradictions

The agent should receive unresolved contradictions when they materially affect the task.

Preferred representation:

```yaml
current_model:
  ...

qualification_history:
  - older claim
  - later evidence

resolution_status:
  reconciled | unresolved | superseded | context_dependent
```

The system should prefer conditional reconciliation over arbitrary winner selection.

## 11. Attribution

Misattribution is a high-severity memory error.

Agent-facing memory should resolve the subject explicitly:

```text
person_david
person_devyn
dog_tank
dog_francie
project_memory_system
```

If attribution is uncertain:

```text
attribution_status: ambiguous
```

Ambiguous claims should be suppressed or clearly labeled.

The system should prefer no memory over confidently assigning a claim to the wrong entity.

## 12. Provenance

Every material claim should be traceable.

The agent does not always need full evidence in context, but it should receive identifiers sufficient to drill down.

Preferred hierarchy:

```text
canonical claim
  ↓
supporting event(s)
  ↓
source chunk(s)
  ↓
raw message/document/tool result
```

## 13. Controlled disclosure

The agent should be able to request more detail progressively.

Recommended disclosure levels:

```text
summary
canonical
episode
evidence
full
```

The tool schema must publish allowed enum values explicitly.

Do not advertise an unconstrained string where the runtime accepts only hidden enum values.

## 14. Context density

The compiler optimizes:

```text
marginal downstream answer quality per context token
```

It should aggressively suppress:

- duplicates
- weak incidental matches
- raw text when canonical state exists
- stale state
- assistant speculation
- irrelevant cross-domain material

It should deliberately surface:

- constraints
- contradictions
- state changes
- high-impact preferences
- relevant past outcomes
- uncertainty

## 15. Memory context vs memory synthesis

Separate factual/context compilation from reasoning.

### `memory_context`

Provides:

```text
facts
claims
state
history
outcomes
uncertainty
contradictions
analogies
provenance
```

It should not tell the downstream agent what conclusion to reach.

### `memory_synthesis` (optional)

May provide:

```text
generated hypotheses
possible patterns
candidate interpretation
suggested exploration
```

and must be clearly labeled generated reasoning.

The core context packet should not contain an unlabeled `Recommended next direction` that can become self-reinforcing historical advice.

## 16. Effort semantics

Effort controls work performed, not minimum packet size.

### Low effort

- current high-confidence claims
- hard constraints
- direct relevance
- minimal verification

### Medium effort

- object state
- relevant events
- failures/preferences
- temporal checks

### High effort

- contradiction review
- deeper source verification
- graph traversal
- open loops
- analogy search
- alternative interpretations

The final packet remains bounded.

## 17. Cross-domain analogy

The agent benefits from lateral recall, but direct evidence and analogy must not be mixed.

Preferred representation:

```yaml
analogy:
  source_domain: lab_automation
  target_domain: cooking_baking
  structural_similarity: stateful workflow with error recovery
  evidence_status: analogy_only
```

## 18. Feedback and utility

The system should record more than "retrieved" and "user liked answer."

Useful exposure states:

```text
retrieved
exposed_to_model
explicitly_used
ignored
contradicted_by_model
corrected_by_user
caused_constraint_violation
supported_final_decision
led_to_successful_action
```

These signals update retrieval utility, not automatically truth confidence.

## 19. Failure taxonomy

The downstream agent should be able to report at least:

```text
retrieval_miss
retrieval_noise
constraint_miss
stale_memory
misattribution
duplicate_memory
bad_canonicalization
bad_context_compilation
cross_domain_leak
tool_contract_error
unsupported_inference
provenance_gap
latency
security
```

## 20. Agent-facing tool requirements

Memory tools SHOULD:

- use strict typed schemas
- publish allowed enums
- return `status`
- return `run_id`
- return `trace_id`
- return applied filters
- expose enough ranking metadata for debugging
- preserve source IDs
- avoid side effects for read/retrieval operations
- fail atomically
- distinguish no-result from tool failure

## 21. Success criteria

A memory system improvement is real when it improves one or more downstream metrics without unacceptable regressions.

Primary metrics:

```text
constraint violations
correct personalization
stale-fact usage
misattribution
unnecessary clarification questions
repeated failed suggestions
useful novelty
context token cost
answer quality
provenance correctness
```

Retrieval recall, cosine similarity, and top-k accuracy are implementation metrics, not the final objective.

## 22. Counterfactual evaluation

For representative prompts, compare:

```text
A. no persistent memory
B. platform/OpenAI memory only
C. local structured/object memory only
D. raw-history RAG only
E. platform memory + local structured/object memory
```

Use the same downstream model and prompt where possible.

The project target is for condition E to provide the best reliable personalization at acceptable context cost.
