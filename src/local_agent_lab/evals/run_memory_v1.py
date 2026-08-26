from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

from local_agent_lab.evals.memory_v1 import run_memory_eval_v1
from local_agent_lab.security.paths import PathPolicyError, safe_write_text
from local_agent_lab.tools.file_tools import redact_text


def render_human_report(report: dict[str, Any]) -> str:
    release = report["release_assessment"]
    repository = report["repository_identity"]
    dataset = report["dataset_identity"]
    config = report["config_identity"]
    code = report["code_identity"]
    generation = report["generation_identity"]
    lines = [
        "# MiniLLM Offline Synthetic Retrieval Comparison",
        "",
        f"- Benchmark status: **{report['status']}**",
        f"- Release assessment: **{release['status']}**",
        f"- Generated: `{generation['generated_at']}`",
        f"- Invocation ID: `{generation['invocation_id']}`",
        f"- Git commit: `{repository['git_commit']}`",
        f"- Git dirty: `{str(repository['git_dirty']).lower()}`",
        f"- Lock SHA-256: `{repository['lock_sha256']}`",
        f"- Dataset: `{dataset['version']}` / `{dataset['sha256']}`",
        f"- Config: `{config['version']}` / `{config['sha256']}`",
        f"- Code SHA-256: `{code['aggregate_sha256']}`",
        "",
        "This is a deterministic offline comparison over synthetic retrieval fixtures. "
        "It is not an online A/B test, production latency benchmark, human answer-quality evaluation, "
        "or full structured/object-memory validation. The deterministic vector adapter is test-only.",
        "",
        "## Variant Metrics",
        "",
        "| Variant | MRR | Precision@5 | Recall@5 | Abstention | Provenance | Wrong domain | Stale | Sensitive leakage | Contract pass | Local p95 ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for variant in report["variants"]:
        metrics = variant["metrics"]
        lines.append(
            "| {variant} | {mrr:.6f} | {precision:.6f} | {recall:.6f} | {abstention:.6f} | "
            "{provenance} | {wrong:.6f} | {stale:.6f} | {sensitive:.6f} | {contract:.6f} | {p95:.6f} |".format(
                variant=variant["variant"],
                mrr=metrics["mrr"],
                precision=metrics["precision_at_5"],
                recall=metrics["recall_at_5"],
                abstention=metrics["abstention_accuracy"],
                provenance="n/a" if metrics["provenance_accuracy"] is None else f"{metrics['provenance_accuracy']:.6f}",
                wrong=metrics["wrong_domain_rate"],
                stale=metrics["stale_selection_rate"],
                sensitive=metrics["sensitive_leakage_rate"],
                contract=metrics["retrieval_contract_pass_rate"],
                p95=metrics["latency_p95_ms"],
            )
        )
    failed_thresholds = [check["metric"] for check in report["thresholds"]["checks"] if check["status"] != "pass"]
    failed_cases = release["failed_hard_gate_case_ids"]
    lines.extend(
        [
            "",
            "## Release Gates",
            "",
            f"- Hard gates: `{release['hard_gate_case_count'] - len(failed_cases)}/{release['hard_gate_case_count']}` passed",
            f"- Failed hard-gate cases: `{failed_cases}`",
            f"- Failed aggregate thresholds: `{failed_thresholds}`",
            f"- Fixture immutable: `{str(release['fixture_immutable']).lower()}`",
            f"- Persisted canaries absent: `{str(release['persisted_canaries_absent']).lower()}`",
            "",
            "## Defensible Claim",
            "",
            _claim_wording(report),
            "",
        ]
    )
    return "\n".join(lines)


def persist_report(report: dict[str, Any], output_dir: Path) -> dict[str, str]:
    if output_dir.is_symlink():
        raise PathPolicyError("evaluation output root must not be a symlink")
    try:
        authorized_root = output_dir.resolve(strict=True)
    except FileNotFoundError as exc:
        raise PathPolicyError("evaluation output root must already exist") from exc
    if not authorized_root.is_dir():
        raise PathPolicyError("evaluation output root must be a directory")

    json_payload = redact_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    markdown_payload = redact_text(render_human_report(report))
    json_path = safe_write_text(authorized_root, "memory-eval-v1.json", json_payload, mode=0o600)
    markdown_path = safe_write_text(authorized_root, "memory-eval-v1.md", markdown_payload, mode=0o600)
    return {"json": str(json_path), "markdown": str(markdown_path)}


def report_exit_code(report: dict[str, Any]) -> int:
    release = report.get("release_assessment", {})
    return 0 if report.get("status") == "pass" and release.get("status") == "pass" else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the deterministic offline synthetic MiniLLM retrieval comparison."
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--invocation-label", default="python_module")
    args = parser.parse_args(argv)
    output_dir = args.output_dir.expanduser().absolute()
    invocation = [args.invocation_label, *sys.argv[1:]]
    if args.work_dir:
        report = run_memory_eval_v1(args.work_dir.resolve(), invocation=invocation)
    else:
        with tempfile.TemporaryDirectory(prefix="minillm-offline-eval-") as work_dir:
            report = run_memory_eval_v1(Path(work_dir), invocation=invocation)
    artifacts = persist_report(report, output_dir)
    print(
        json.dumps(
            {
                "status": report["status"],
                "release_status": report["release_assessment"]["status"],
                "failed_hard_gate_case_ids": report["release_assessment"]["failed_hard_gate_case_ids"],
                "artifacts": artifacts,
            },
            sort_keys=True,
        )
    )
    return report_exit_code(report)


def _claim_wording(report: dict[str, Any]) -> str:
    release = report["release_assessment"]
    if release["status"] != "pass":
        return (
            "Pre-release offline synthetic retrieval benchmark: the governed variant failed one or more "
            "designated retrieval contracts or aggregate gates. No production-readiness claim is supported."
        )
    return (
        f"On dataset `{report['dataset_identity']['version']}`, MiniLLM's governed retrieval variant passed "
        f"{release['hard_gate_case_count']}/{release['hard_gate_case_count']} designated contracts in a "
        "deterministic offline synthetic fixture comparison. This does not establish online A/B performance, "
        "production latency, or full structured/object-memory correctness."
    )


if __name__ == "__main__":
    raise SystemExit(main())
