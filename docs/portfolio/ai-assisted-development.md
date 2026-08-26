# AI-Assisted Development Statement

## Scope and evidence boundary

MiniLLM was developed with AI assistance. Private Browser ChatGPT acceptance
reports informed the implementation loop, but raw reports are excluded from the
public repository because they can contain personal source text and local runtime
identifiers. Sanitized findings, ADRs, and executable regression tests preserve
the decision evidence. The package metadata identifies David Dalconzo as the
project author, but the repository does not provide line-by-line human/AI
authorship records. This statement therefore describes the documented process
and decisions; it does not assign unverifiable percentages of code to a human or
model.

AI assistance documented by the repository includes:

- Browser ChatGPT executing read-only acceptance workflows, reporting run and
  trace IDs, diagnosing failures, and proposing changes.
- Codex or local/frontier models inspecting behavior, proposing patches, and
  running tests inside the build loop described by the architecture documents.
- Model output used as proposed evidence or implementation input, not as
  self-validating proof that a design is correct.

The project's control-plane policy assigns release authorization, security
boundary changes, and production authority to a human-gated process. AI feedback
is append-only evidence by default and does not silently change memory truth,
ranking, code, or permissions.

## Responsibility model

The engineering workflow evidenced in this repository is:

1. Define a behavioral requirement and an observable acceptance test.
2. Let an AI-assisted client exercise the public tool surface and preserve exact
   run/trace identifiers.
3. Convert a reproducible failure into a focused regression test.
4. Accept, modify, defer, or reject the proposed direction according to the
   architecture and security constraints.
5. Re-run tests and acceptance checks before making a stronger claim.

The retained evidence supports evaluation-driven iteration. It does not support
a claim that an AI autonomously designed, validated, or deployed MiniLLM.

## Verified suggestion outcomes

### Accepted: explicit abstention instead of weak filler

**Suggestion source:** The Browser ChatGPT report recommended an absolute
relevance threshold and no-result path after a nonsense query returned five
unrelated memories.

**Decision:** Accept the direction. Curated trust, subject match, recency, and
semantic similarity may not admit a result without a minimum relevance basis.

**Validation:**

- Sanitized failure narrative:
  [design-changing-failures.md](design-changing-failures.md)
- Regression test:
  [`test_search_chatgpt_memory_returns_no_reliable_results_for_weak_curated_subject_match`](../../tests/test_memory_search.py#L895)

### Accepted: separate retrieval breadth, packet detail, and disclosure

**Suggestion source:** Browser ChatGPT found that a `full` request mixed three
different concerns and reported contradictory effective-depth fields.

**Decision:** Accept the separation into retrieval depth, packet detail, and
disclosure tier rather than making one overloaded depth flag control all three.

**Validation:**

- Sanitized finding:
  [design-changing-failures.md](design-changing-failures.md)
- As-built controls:
  [`normalize_context_controls`](../../src/local_agent_lab/memory/context_packet.py)
- Regression test:
  [`test_context_controls_separate_retrieval_packet_and_disclosure_depth`](../../tests/test_memory_search.py#L144)

### Materially changed: feedback may influence ranking only after review

**Suggestion source:** Browser ChatGPT proposed materializing feedback through a
`submitted -> reviewed/validated -> applied -> superseded` lifecycle and using it
for source/query penalties.

**Decision:** Accept the lifecycle goal but reject direct submission-to-ranking
coupling. Agent feedback is first stored as `submitted`. A separate ranking
control must be created and explicitly reach `applied` before search consumes it.
This preserves auditability and prevents an AI client from silently training the
live system through ordinary feedback calls.

**Validation:**

- Sanitized finding:
  [design-changing-failures.md](design-changing-failures.md)
- Review/control states:
  [feedback.py](../../src/local_agent_lab/memory/feedback.py#L12-L38)
- Submission starts without a ranking control:
  [`test_agent_feedback_submission_creates_submitted_review_without_ranking_control`](../../tests/test_memory_feedback.py#L100)
- Only `applied` controls affect search:
  [`test_search_chatgpt_memory_consumes_only_applied_feedback_controls`](../../tests/test_memory_search.py#L1256)

### Rejected: a superficially passing simulated comparison

**Suggestion source:** An AI coding worker produced an initial comparison harness
and report that appeared to satisfy the evaluation milestone.

**Decision:** Reject that implementation as release evidence. Independent review
showed that aggregate pass status masked failed designated cases, the no-memory
adapter read a gold refusal label, provenance checked only for nonempty IDs, raw
canaries were sanitized before ingestion, the report was not bound to the code
revision, and the advertised shell runner did not execute the comparison. Those
defects violated the human-authored criterion that every compared variant must
actually run and that a simulated comparison must not be called A/B testing.

**Validation:**

- The audit and replacement rationale are retained in the design-changing
  failure record and its associated tests.
- [`test_eval_v1_counts_every_adapter_and_governed_search_invocation`](../../tests/test_memory_eval_v1.py)
  spies on every adapter invocation.
- [`test_report_binds_generation_repository_lock_and_code_identity`](../../tests/test_memory_eval_v1.py)
  verifies report identity rather than trusting a copied result.
- [`test_release_status_fails_when_one_designated_hard_gate_fails`](../../tests/test_memory_eval_v1.py)
  proves a failed designated case cannot hide behind aggregate metrics.
- [`offline_synthetic_retrieval_v1.md`](../evaluation/offline_synthetic_retrieval_v1.md)
  limits the resulting claim to a deterministic offline retrieval comparison.

This rejection is important to the authorship claim: a model produced plausible
code and a green-looking report, but the acceptance criteria, adversarial review,
and decision not to publish the stronger claim determined what was allowed to
survive.

## What is not claimed

- No percentage of the codebase is attributed to AI or human authorship.
- Passing unit tests are not described as independent third-party validation.
- Browser ChatGPT recommendations are not treated as decisions until the
  repository contains the corresponding design and validation evidence.
- Planned object memory and world-model behavior is not presented as complete.
- Private partial acceptance reports are not described as a final pass or
  distributed as public evidence.
