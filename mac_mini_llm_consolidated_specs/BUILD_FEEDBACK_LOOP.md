# Build and Feedback Loop

**Status:** Normative workflow  
**Purpose:** Turn real agent usage into reproducible improvements without allowing the runtime to directly rewrite itself.

## 1. Objective

Use the memory system normally.

When the downstream AI notices that memory helped or hurt, capture structured feedback tied to the exact run.

Codex then diagnoses the implementation, converts reproducible failures into regression tests where possible, proposes a fix, runs evals in staging, and produces a candidate change for promotion.

The loop is:

```text
USE
 ↓
OBSERVE
 ↓
STRUCTURED FEEDBACK
 ↓
REPRODUCE
 ↓
REGRESSION EVAL
 ↓
CODEX PATCH
 ↓
STAGING TEST
 ↓
A/B EVAL
 ↓
PROMOTION GATE
 ↓
PRODUCTION
```

## 2. Principle: feedback describes behavior, not implementation

The downstream agent's unique role is to report:

```text
what it received
what it needed
what was misleading
what was missing
what it used
what it ignored
how the memory changed the answer
```

It should not normally prescribe low-level implementation changes such as retrieval weight values.

Codex owns code-level diagnosis unless the architecture contract itself needs revision.

## 3. Feedback tool

Add an MCP primitive approximately equivalent to:

```python
submit_agent_feedback(
    run_id: str,
    trace_id: str | None,
    component: str,
    category: str,
    severity: str,
    observed_behavior: str,
    expected_behavior: str,
    relevant_source_ids: list[str],
    confidence: float,
    downstream_effect: str | None = None,
    suggested_direction: str | None = None,
) -> FeedbackResult
```

The exact implementation may vary.

### Required categories

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
other
```

### Server-attached metadata

The server should attach:

```text
feedback_id
timestamp
build_sha
schema_version
retrieval/ranking profile
tool version
relevant model identifiers
environment/staging/production
```

The agent should not be trusted to supply authoritative build metadata.

## 4. Feedback is append-only evidence

Submitting feedback must not directly change:

- memory truth confidence
- ranking weights
- production code
- permissions
- schemas
- deployment configuration

Feedback enters a review/build queue.

## 5. Reproduction

Before changing code, Codex should try to reproduce the behavior from:

```text
run_id
trace_id
query
context packet
retrieved source IDs
ranking metadata
build SHA
```

If exact replay is impossible, create the smallest faithful fixture.

## 6. Regression-test-first rule

For actionable failures:

> Prefer turning the feedback into a failing regression eval before implementing the fix.

Example:

```text
Observed:
AI Automation Micro-Business ranked above relevant cooking state.

Expected:
Cooking constraints, completed baking projects, and relevant outcomes dominate.

Regression:
Given this fixture corpus and query, no unrelated AI-memory object appears
above direct cooking constraints or relevant baking objects.
```

Not every feedback item can become deterministic. The build report should say when it cannot.

## 7. Candidate implementation

Codex should receive:

```text
authoritative architecture docs
relevant ADRs
current implementation
feedback record
trace/replay artifacts
existing evals
```

Codex may choose implementation strategy.

It should not violate normative architecture/security invariants merely to make one eval pass.

## 8. Staging

Candidate changes run in an isolated builder/staging environment against:

- synthetic fixtures
- sanitized production snapshots where appropriate
- existing regression suite
- new regression case
- representative downstream-agent evaluations

Production data is not the builder's writable test environment.

## 9. Counterfactual A/B evaluation

Important memory changes should compare at least:

```text
baseline build
candidate build
```

For representative downstream prompts, where practical also compare memory conditions:

```text
no memory
platform/OpenAI memory only
local structured memory
raw-history RAG
combined memory
```

Metrics include:

```text
constraint violations
correct personalization
stale usage
misattribution
irrelevant retrieval
unnecessary questions
repetition of failed suggestions
useful novelty
context token cost
provenance correctness
answer quality
latency
```

## 10. Promotion gate

A candidate is promotable when:

- the targeted regression is fixed or materially improved
- existing critical evals do not regress beyond tolerance
- security checks pass
- schema/migration checks pass
- rollback exists
- any security/control-plane changes have separate approval

Early project phases should keep final promotion human-triggered.

## 11. Rollback

Every production deployment should identify:

```text
previous build
candidate build
migration version
rollback command/path
data compatibility assumptions
```

Do not deploy irreversible migrations without explicit review.

## 12. Learning from helpful retrieval

Positive feedback matters too.

The agent should be able to report:

```text
this constraint prevented a bad suggestion
this object summary was sufficient
this inferred pattern improved novelty
this episode was unnecessary because canonical state was enough
```

These signals improve retrieval utility and context compilation.

They do not prove the underlying fact is true.

## 13. Credit assignment

Track exposure-level outcomes when possible:

```text
retrieved
exposed
used
ignored
challenged
corrected
supported decision
caused error
led to successful action
```

The goal is to learn which memories, relationships, lenses, and packet sections actually help downstream reasoning.

## 14. Spec evolution

Not every failure is an implementation bug.

Codex should classify feedback as:

```text
implementation defect
missing eval
schema limitation
architecture ambiguity
new requirement
security-policy question
```

Architecture-level changes should become ADRs.

Security-policy changes require human approval.

## 15. Build-system context

The authoritative build plan must live outside the memory engine itself.

Recommended source of truth:

```text
Git repository
  README.md
  SYSTEM_ARCHITECTURE.md
  AI_MEMORY_CONTRACT.md
  SECURITY_MODEL.md
  BUILD_FEEDBACK_LOOP.md
  ROADMAP.md
  adr/
```

The memory system may index these files.

It must not be their sole authority.

This avoids a bootstrap failure where broken memory changes Codex's understanding of what the memory system is supposed to be.

## 16. Example feedback record

```yaml
component: memory_context
category: retrieval_noise
severity: medium

observed_behavior: >
  The packet returned multiple baking-adjacent historical chunks but no usable
  canonical cooking constraints. An unrelated AI Automation Micro-Business
  conversation ranked first.

expected_behavior: >
  Hard cooking constraints, completed baking projects, and relevant current
  preferences should dominate before incidental cross-domain history.

downstream_effect: >
  The packet did not materially improve the answer and would have required the
  downstream model to reconstruct the user from raw history.

confidence: 0.98
```

## 17. Initial automation boundary

Automate early:

```text
feedback capture
trace attachment
reproduction fixture generation
eval execution
candidate patch generation
staging comparison
report generation
```

Keep human-gated initially:

```text
production deployment
security changes
permission changes
destructive migrations
new sensitive-data access
```

## 18. Definition of success

The loop is working when ordinary use naturally produces durable improvements:

```text
real failure
→ structured feedback
→ regression fixture
→ candidate fix
→ measured improvement
→ no critical regressions
→ controlled promotion
```

without requiring the human to manually translate every AI complaint into a coding task.
