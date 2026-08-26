#!/usr/bin/env bash
set -euo pipefail

uv run --frozen pytest -q \
  tests/test_memory_privacy.py \
  tests/test_memory_observability.py \
  tests/test_memory_eval_checks.py \
  tests/test_memory_eval_v1.py \
  tests/test_deployment_gate.py \
  tests/test_promotion_controller.py \
  tests/test_trusted_promotion.py \
  tests/test_minillm_admin_helper.py \
  tests/test_service_identity_boundaries.py \
  tests/test_service_identity_provisioning.py \
  tests/test_staging_snapshot.py \
  tests/test_production_bootstrap.py \
  tests/test_production_cutover.py \
  tests/test_patches.py \
  tests/test_prompt_packing.py \
  tests/test_repo_indexer.py \
  tests/test_search.py \
  tests/test_sqlite_utils.py \
  tests/test_chatgpt_ingest.py \
  tests/test_home_mcp_launchd.py \
  tests/test_public_release_candidate.py \
  tests/test_public_artifact_audit.py \
  tests/test_ci_security.py \
  tests/test_home_mcp.py::test_home_mcp_security_acceptance_rejects_boundary_violations_and_audits
