#!/usr/bin/env python3
"""Deterministic presentation of MiniLLM's canonical governed-memory evaluation."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any, Sequence

from local_agent_lab.evals.memory_v1 import memory_eval_v1_conversations, run_memory_eval_v1


DEMO_VERSION = "minillm_public_demo_v2.0.0"
REPORT_SCHEMA_VERSION = "minillm_public_demo_report_v2"
GOVERNED_VARIANT = "governed_hybrid"


def _case(report: dict[str, Any], variant: str, case_id: str) -> dict[str, Any]:
    return next(item for item in report["case_results"][variant] if item["case_id"] == case_id)


def _source_catalog() -> dict[str, dict[str, str]]:
    return {
        message.source_id: {
            "source_id": message.source_id,
            "conversation_source_id": conversation.source_id,
            "title": conversation.title,
            "subject": conversation.subject,
            "source_role": message.role,
            "evidence": message.text,
        }
        for conversation in memory_eval_v1_conversations()
        for message in conversation.messages
    }


def _provenance(source_ids: Sequence[str], catalog: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    return [catalog[source_id] for source_id in source_ids]


def build_report() -> dict[str, Any]:
    """Execute the canonical evaluator and retain only deterministic presentation fields."""
    with tempfile.TemporaryDirectory(prefix="minillm-public-demo-") as work_dir:
        evaluation = run_memory_eval_v1(Path(work_dir), invocation=("public_demo",))

    catalog = _source_catalog()
    provenance_case = _case(evaluation, GOVERNED_VARIANT, "PROV-01")
    domain_case = _case(evaluation, GOVERNED_VARIANT, "DOM-01")
    stale_case = _case(evaluation, GOVERNED_VARIANT, "TMP-01")
    abstention_case = _case(evaluation, GOVERNED_VARIANT, "ABS-01")
    refusal_case = _case(evaluation, GOVERNED_VARIANT, "GOV-01")
    failed_baseline = _case(evaluation, "no_memory", "RET-01")

    demonstrations = {
        "provenance_backed_retrieval": {
            "case_id": provenance_case["case_id"],
            "query": provenance_case["query"],
            "result_quality": provenance_case["result_quality"],
            "contract_passed": provenance_case["metrics"]["retrieval_contract_pass"],
            "selected": _provenance(provenance_case["observed_source_ids"], catalog),
        },
        "wrong_domain_suppression": {
            "case_id": domain_case["case_id"],
            "expected_domain": domain_case["gold"]["expected_domain"],
            "domain_filtered_count": domain_case["diagnostics"]["candidate_counts"]["domain_filtered"],
            "forbidden_source_ids": domain_case["gold"]["forbidden_source_ids"],
            "forbidden_source_ids_selected": domain_case["diagnostics"]["forbidden_source_ids_found"],
            "wrong_domain_selection_count": domain_case["metrics"]["wrong_domain_count"],
            "contract_passed": domain_case["metrics"]["retrieval_contract_pass"],
        },
        "stale_or_superseded_suppression": {
            "case_id": stale_case["case_id"],
            "required_current_source_ids": stale_case["gold"]["required_top_source_ids"],
            "stale_source_ids": stale_case["gold"]["stale_source_ids"],
            "stale_source_ids_selected": stale_case["diagnostics"]["stale_source_ids_found"],
            "selected_source_ids": stale_case["observed_source_ids"],
            "contract_passed": stale_case["metrics"]["retrieval_contract_pass"],
        },
        "no_answer_abstention": {
            "case_id": abstention_case["case_id"],
            "result_quality": abstention_case["result_quality"],
            "selected_source_ids": abstention_case["observed_source_ids"],
            "abstained": abstention_case["metrics"]["abstained"],
            "contract_passed": abstention_case["metrics"]["retrieval_contract_pass"],
        },
        "typed_governance_refusal": {
            "case_id": refusal_case["case_id"],
            "decision": "refuse_unsupported_advice",
            "refusal_required": refusal_case["gold"]["expect_refusal"],
            "refusal_applied": refusal_case["diagnostics"]["refuse_unsupported_advice"],
            "contract_passed": refusal_case["metrics"]["retrieval_contract_pass"],
        },
        "failed_baseline_contract": {
            "case_id": failed_baseline["case_id"],
            "variant": failed_baseline["adapter"],
            "failure_type": "retrieval_contract_failure",
            "result_quality": failed_baseline["result_quality"],
            "failure_reasons": failed_baseline["failure_reasons"],
            "contract_passed": failed_baseline["metrics"]["retrieval_contract_pass"],
        },
    }
    checks = {
        "canonical_evaluation_passed": evaluation["status"] == "pass",
        "provenance_exposed": (
            provenance_case["metrics"]["provenance_correct"]
            and bool(demonstrations["provenance_backed_retrieval"]["selected"])
        ),
        "wrong_domain_suppressed": (
            demonstrations["wrong_domain_suppression"]["domain_filtered_count"] > 0
            and demonstrations["wrong_domain_suppression"]["forbidden_source_ids_selected"] == []
            and demonstrations["wrong_domain_suppression"]["wrong_domain_selection_count"] == 0
        ),
        "stale_or_superseded_suppressed": (
            demonstrations["stale_or_superseded_suppression"]["stale_source_ids_selected"] == []
            and demonstrations["stale_or_superseded_suppression"]["selected_source_ids"][0]
            in demonstrations["stale_or_superseded_suppression"]["required_current_source_ids"]
        ),
        "no_answer_abstained": (
            demonstrations["no_answer_abstention"]["abstained"]
            and demonstrations["no_answer_abstention"]["selected_source_ids"] == []
        ),
        "unsupported_advice_refused": demonstrations["typed_governance_refusal"]["refusal_applied"],
        "failed_contract_visible": (
            demonstrations["failed_baseline_contract"]["contract_passed"] is False
            and bool(demonstrations["failed_baseline_contract"]["failure_reasons"])
        ),
    }
    dataset = evaluation["dataset_identity"]
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "demo_version": DEMO_VERSION,
        "canonical_core": {
            "evaluation_schema_version": evaluation["schema_version"],
            "dataset_version": dataset["version"],
            "dataset_sha256": dataset["sha256"],
            "case_count": dataset["case_count"],
            "conversation_count": dataset["conversation_count"],
            "message_count": dataset["message_count"],
            "executed_variants": [item["variant"] for item in evaluation["variants"] if item["executed"]],
            "presented_variant": GOVERNED_VARIANT,
        },
        "runtime_contract": {
            "network_required": False,
            "credentials_required": False,
            "model_required": False,
            "private_data_used": False,
        },
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "demonstrations": demonstrations,
    }


def render_human(report: dict[str, Any]) -> str:
    core = report["canonical_core"]
    demonstrations = report["demonstrations"]
    retrieval = demonstrations["provenance_backed_retrieval"]
    domain = demonstrations["wrong_domain_suppression"]
    stale = demonstrations["stale_or_superseded_suppression"]
    abstention = demonstrations["no_answer_abstention"]
    refusal = demonstrations["typed_governance_refusal"]
    failure = demonstrations["failed_baseline_contract"]
    selected = retrieval["selected"][0]
    lines = [
        "MiniLLM canonical governed-memory demo",
        f"status: {report['status'].upper()}",
        f"dataset: {core['dataset_version']} ({core['case_count']} contracts)",
        f"variants executed: {', '.join(core['executed_variants'])}",
        "runtime: offline, model-free, credential-free, private-data-free",
        "",
        f"[1] provenance: {selected['source_id']} / {selected['source_role']} / {selected['subject']}",
        f"    evidence: {selected['evidence']}",
        f"[2] wrong domain: {domain['domain_filtered_count']} filtered, {domain['wrong_domain_selection_count']} selected",
        f"[3] stale state: selected {stale['selected_source_ids'][0]}, stale selected {len(stale['stale_source_ids_selected'])}",
        f"[4] no answer: {abstention['result_quality']}, {len(abstention['selected_source_ids'])} selected",
        f"[5] safety: {refusal['decision']}={str(refusal['refusal_applied']).lower()}",
        f"[6] failed contract: {failure['variant']} / {', '.join(failure['failure_reasons'])}",
        "",
        "checks:",
        *(f"  {'PASS' if passed else 'FAIL'} {name}" for name, passed in report["checks"].items()),
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit stable machine-readable output")
    args = parser.parse_args(argv)
    try:
        report = build_report()
    except Exception:  # noqa: BLE001 - the public demo returns one stable boundary failure.
        error = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "status": "error",
            "error": {
                "code": "canonical_evaluation_failed",
                "stage": "execute_canonical_evaluation",
            },
        }
        print(json.dumps(error, indent=2, sort_keys=True) if args.json else render_fatal_error(error))
        return 2
    print(
        json.dumps(report, indent=2, sort_keys=True) if args.json else render_human(report),
        end="\n" if args.json else "",
    )
    return 0 if report["status"] == "pass" else 1


def render_fatal_error(error: dict[str, Any]) -> str:
    details = error["error"]
    return f"MiniLLM public demo ERROR: {details['code']} at {details['stage']}\n"


if __name__ == "__main__":
    raise SystemExit(main())
