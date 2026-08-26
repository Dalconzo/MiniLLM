# Decision Evidence Index

This index connects important MiniLLM decisions to the constraint that forced
them, alternatives that were rejected, validation evidence, and current
consequences. "Rejected" means rejected by the documented design; it does not
imply that every alternative was implemented as an experimental variant.

## ADR-backed decisions

| Decision | Constraint | Rejected alternatives | Validation | Consequence | Status |
| --- | --- | --- | --- | --- | --- |
| [ADR 0001: object-centric memory](../architecture/adr/0001-object-centric-memory.md) | Static canonical records fragmented identity, lost execution history, and mixed current state with past attempts. | A transcript/chunk as durable identity; one static note as the final abstraction; forced low-confidence entity merges; immediate whole-system rewrite. | [`test_apply_candidate_merge_creates_one_record_and_preserves_all_evidence`](../../tests/test_candidate_merge_apply.py#L86), [`test_opposing_user_claims_are_reported_as_conflict_not_merged`](../../tests/test_candidate_canonicalization.py#L269), [`test_context_packet_keeps_assistant_advice_out_of_factual_slots`](../../tests/test_memory_search.py#L196) | Existing chunk/curated retrieval remains compatible while object/event/claim layers are added incrementally. | **Partially as-built.** Candidate merging, provenance, and context-state controls exist. General persistent objects, events, claims, and world-model materialization remain planned. |
| [ADR 0002: isolated builder control plane](../architecture/adr/0002-isolated-builder-control-plane.md) | A builder with production credentials could modify memory truth, weaken policy, alter audit records, or deploy its own security changes. | One shared service identity; builder self-deployment; trusting candidate-supplied gate results; promoting control-plane code through the ordinary path. | [`test_gate_denies_same_builder_and_production_identity`](../../tests/test_deployment_gate.py#L199), [`test_controller_code_requires_separate_review_even_when_check_only_gate_allows_it`](../../tests/test_promotion_controller.py#L289), [`test_complete_private_layout_passes_all_boundary_checks`](../../tests/test_service_identity_boundaries.py#L186), [`test_environment_operator_and_extra_argument_injection_are_rejected`](../../tests/test_minillm_admin_helper.py#L282) | Build automation can be broad inside staging while production identity, credentials, audit, and release authority remain separately controlled. | **As-built in source and tests.** Any claim about a particular host's current deployment still requires host evidence. |

## Failure-driven supporting decisions

| Decision | Constraint | Rejected alternatives | Validation | Consequence | Status |
| --- | --- | --- | --- | --- | --- |
| Abstain on weak retrieval | A nonsense query returned plausible-looking but unrelated memories. | Always fill the requested result count; let curated trust or recency overcome zero relevance. | [Sanitized failure narrative](design-changing-failures.md), [`test_search_chatgpt_memory_returns_no_reliable_results_for_weak_curated_subject_match`](../../tests/test_memory_search.py#L895), [`test_public_demo_abstains_and_reports_expected_failure`](../../tests/test_public_demo.py) | Search can return `no_reliable_results` with trace counts instead of fabricating confidence through filler. | **As-built and observed.** |
| Enforce domain boundaries before semantic expansion | Baking context admitted lab/AI records even with cross-domain retrieval disabled. | Treat domain as a soft ranking bonus; let generic semantic similarity override an explicit subject boundary. | [Sanitized failure narrative](design-changing-failures.md), [`test_search_chatgpt_memory_blocks_wrong_domain_subject_leak_when_cross_domain_disabled`](../../tests/test_memory_search.py#L923) | Direct evidence stays in-domain unless cross-domain behavior is explicitly allowed and labeled. | **As-built in regression tests.** |
| Quarantine non-actionable recipes | A recipe with eight heading-only "steps" passed structural counting but could not be executed safely by an agent. | Treat nonzero ingredient/step counts as sufficient; let an agent invent missing procedure. | [Sanitized failure narrative](design-changing-failures.md), [`test_home_mcp_flags_heading_only_recipe_methods_as_non_actionable`](../../tests/test_home_mcp.py#L430) | Invalid cards remain discoverable for repair but are marked non-actionable and can be excluded from executable results. | **As-built and observed.** |
| Separate corpus freshness from embedding health | Complete embeddings for an old import looked healthy while recent user state was absent. | Use embedding coverage as a proxy for corpus currency; diagnose every miss as ranking failure. | [Sanitized failure narrative](design-changing-failures.md), [`test_memory_status_reports_corpus_freshness_separately_from_embedding_health`](../../tests/test_memory_observability.py#L349) | Status reports can distinguish "everything imported is embedded" from "the imported corpus is current." | **As-built in status/tests.** A current private corpus is environment-specific. |
| Keep agent feedback inert until reviewed/applied | A downstream AI can identify failures but should not silently change truth or ranking. | Apply penalties directly on submission; conflate answer satisfaction with factual confidence. | [Sanitized failure narrative](design-changing-failures.md), [`test_agent_feedback_submission_creates_submitted_review_without_ranking_control`](../../tests/test_memory_feedback.py#L100), [`test_search_chatgpt_memory_consumes_only_applied_feedback_controls`](../../tests/test_memory_search.py#L1256) | Feedback becomes auditable evidence; a separate reviewed control changes retrieval and can carry rollback rationale. | **As-built.** General automated credit assignment remains planned. |
| Separate retrieval depth, packet detail, and disclosure | A single depth setting produced contradictory diagnostics and changed disclosure without improving retrieval. | Treat "full" as one opaque knob; assume more exposed text means better retrieval. | [Sanitized failure narrative](design-changing-failures.md), [`test_context_controls_separate_retrieval_packet_and_disclosure_depth`](../../tests/test_memory_search.py#L144) | Callers can request broader search, richer compilation, or more disclosure independently. | **As-built.** |

## Evidence that remains incomplete

- The tagged alpha retains a sanitized browser/Home MCP pass, while raw reports
  remain local and every later release requires fresh commit-bound acceptance.
- The repository contains tests for object-compatible candidate merging, but not
  a complete persistent object/event/claim/world-model implementation.
- The offline synthetic comparison executes all four declared retrieval variants,
  but it is not an online A/B test, generated-answer evaluation, production
  latency benchmark, or complete structured/object-memory validation.
- Service-identity and promotion tests validate source behavior; they do not by
  themselves prove the state of a particular Mac mini.
