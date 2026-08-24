# MiniLLM Threat Model

Status: pre-release. This document describes repository controls and remaining
risks; it is not a claim of formal security certification.

## Assets and boundaries

MiniLLM may handle private conversation history, curated memory, local files,
credentials, prompts, traces, and deployment authority. The important boundaries
are repository ingestion, memory retrieval, context construction, model egress,
typed MCP tools, filesystem writes, diagnostics, and builder-to-production
promotion.

## Controls and evidence

| Surface | Principal control | Evidence |
| --- | --- | --- |
| Repository input | Git-compatible ignore rules, credential denylist, no-follow reads, root containment | `tests/test_repo_indexer.py` |
| Memory retrieval | provenance and epistemic labels, relevance floor, domain policy, temporal handling, source blocking | `tests/test_memory_search.py`, `tests/test_memory_high_risk.py` |
| Model egress | final-boundary redaction and minimized prompt artifacts | `tests/test_prompt_packing.py`, `tests/test_memory_privacy.py` |
| Filesystem writes | strict relative paths and descriptor-relative no-follow operations | `tests/test_patches.py`, `tests/test_home_mcp.py` |
| Tool calls | typed allowlist, scoped roots, no arbitrary shell, structured errors, per-call traces | `tests/test_home_mcp.py` |
| Feedback | submitted evidence is inert until a reviewed control is applied | `tests/test_memory_feedback.py`, `tests/test_memory_search.py` |
| Deployment | separate identities, check-only policy, immutable hashes, human review, audit, rollback | `tests/test_deployment_gate.py`, `tests/test_promotion_controller.py`, `tests/test_trusted_promotion.py` |

## Residual risk

Pattern-based redaction cannot recognize every private fact. A local model may
produce unsafe or incorrect suggestions. Passing repository tests does not prove
host configuration, model quality, or resistance to a compromised administrator.
Human review, least privilege, private runtime storage, and conservative network
exposure remain required.

## Release conditions

Before publication or release, the complete tests, focused boundary tests,
credential scan, generated-artifact scan, and a clean-checkout quick start must
pass. Private corpora, credentials, host paths, and operational traces must remain
outside the repository.
