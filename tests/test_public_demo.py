from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from local_agent_lab.evals.memory_v1 import DATASET_VERSION, EVAL_SCHEMA_VERSION, memory_eval_v1_cases


REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO = REPO_ROOT / "examples" / "public_demo" / "run_demo.py"


def _run_demo(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DEMO), *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _json_report() -> dict[str, Any]:
    completed = _run_demo("--json")
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout)


def test_public_demo_is_deterministic_offline_and_uses_versioned_eval_contract() -> None:
    first = _run_demo("--json")
    second = _run_demo("--json")

    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout
    report = json.loads(first.stdout)
    assert report["status"] == "pass"
    assert report["canonical_core"]["evaluation_schema_version"] == EVAL_SCHEMA_VERSION
    assert report["canonical_core"]["dataset_version"] == DATASET_VERSION
    assert report["canonical_core"]["case_count"] == len(memory_eval_v1_cases()) == 28
    assert report["canonical_core"]["executed_variants"] == [
        "no_memory",
        "raw_fts",
        "vector_only",
        "governed_hybrid",
    ]
    assert report["runtime_contract"] == {
        "credentials_required": False,
        "model_required": False,
        "network_required": False,
        "private_data_used": False,
    }


def test_public_demo_exposes_canonical_provenance_and_governance_outcomes() -> None:
    demonstrations = _json_report()["demonstrations"]
    retrieval = demonstrations["provenance_backed_retrieval"]
    domain = demonstrations["wrong_domain_suppression"]
    stale = demonstrations["stale_or_superseded_suppression"]

    assert retrieval["case_id"] == "PROV-01"
    assert retrieval["selected"][0] == {
        "conversation_source_id": "conv_starter_current",
        "evidence": (
            "Synthetic correction: after feeding the invented Northstar starter, it rose 82 percent "
            "over 18 hours and then receded 14 percent."
        ),
        "source_id": "src_starter_current",
        "source_role": "user",
        "subject": "Recipes and Baking",
        "title": "Synthetic Northstar starter tracking",
    }
    assert retrieval["contract_passed"] is True
    assert domain["domain_filtered_count"] > 0
    assert domain["wrong_domain_selection_count"] == 0
    assert domain["forbidden_source_ids_selected"] == []
    assert stale["selected_source_ids"][0] == "src_finance_current"
    assert stale["stale_source_ids"] == ["src_finance_stale"]
    assert stale["stale_source_ids_selected"] == []


def test_public_demo_abstains_refuses_and_exposes_a_real_failed_contract() -> None:
    demonstrations = _json_report()["demonstrations"]
    abstention = demonstrations["no_answer_abstention"]
    refusal = demonstrations["typed_governance_refusal"]
    failed = demonstrations["failed_baseline_contract"]

    assert abstention == {
        "abstained": True,
        "case_id": "ABS-01",
        "contract_passed": True,
        "result_quality": "no_reliable_results",
        "selected_source_ids": [],
    }
    assert refusal == {
        "case_id": "GOV-01",
        "contract_passed": True,
        "decision": "refuse_unsupported_advice",
        "refusal_applied": True,
        "refusal_required": True,
    }
    assert failed["variant"] == "no_memory"
    assert failed["failure_type"] == "retrieval_contract_failure"
    assert failed["contract_passed"] is False
    assert failed["failure_reasons"] == ["relevant_source_missing", "wrong_top_source"]


def test_public_demo_stable_json_omits_volatile_machine_details() -> None:
    report = _json_report()
    serialized = json.dumps(report, sort_keys=True)
    forbidden_keys = {
        "elapsed_ms",
        "generated_at",
        "invocation_id",
        "latency_mean_ms",
        "latency_p50_ms",
        "latency_p95_ms",
        "repository_identity",
        "runtime_identity",
        "working_directory",
    }

    assert forbidden_keys.isdisjoint(_all_keys(report))
    assert str(REPO_ROOT) not in serialized
    assert "/Users/" not in serialized


def test_public_demo_is_a_presentation_layer_not_a_parallel_ranker() -> None:
    source = DEMO.read_text(encoding="utf-8")

    assert "run_memory_eval_v1" in source
    assert "memory_eval_v1_conversations" in source
    assert "governed_retrieval" not in source
    assert "_tokens" not in source
    assert "MIN_RELEVANCE" not in source
    assert "cosine_similarity" not in source


def test_public_demo_human_output_is_short_and_readable() -> None:
    completed = _run_demo()

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert "status: PASS" in completed.stdout
    assert "memory_eval_dataset_v1.2.0 (28 contracts)" in completed.stdout
    assert "src_starter_current / user / Recipes and Baking" in completed.stdout
    assert "wrong domain:" in completed.stdout
    assert "no answer: no_reliable_results, 0 selected" in completed.stdout
    assert "failed contract: no_memory / relevant_source_missing, wrong_top_source" in completed.stdout
    assert len(completed.stdout.splitlines()) < 30


def _all_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for child in value.values() for key in _all_keys(child)}
    if isinstance(value, list):
        return {key for child in value for key in _all_keys(child)}
    return set()
