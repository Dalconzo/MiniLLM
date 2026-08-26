from __future__ import annotations

import json
import stat
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

import local_agent_lab.evals.memory_v1 as memory_eval
import local_agent_lab.evals.run_memory_v1 as eval_runner
from local_agent_lab.security.paths import PathPolicyError


CANARIES = (
    memory_eval.OPENAI_SECRET_CANARY,
    memory_eval.GITHUB_SECRET_CANARY,
    memory_eval.PASSWORD_SECRET_CANARY,
    memory_eval.JWT_SECRET_CANARY,
)


class _CountingAdapter:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.name = delegate.name
        self.calls = 0

    def execute(self, runtime, case):
        self.calls += 1
        return self.delegate.execute(runtime, case)


class _OneCaseFailureAdapter:
    name = "governed_hybrid"

    def __init__(self, delegate) -> None:
        self.delegate = delegate

    def execute(self, runtime, case):
        if case.id == "RET-01":
            return {"result_quality": "forced_test_failure", "results": [], "refuse_unsupported_advice": False}
        return self.delegate.execute(runtime, case)


class _StaticAdapter:
    name = "static_test_adapter"

    def __init__(self, result) -> None:
        self.result = result

    def execute(self, runtime, case):
        return {"result_quality": "test", "results": [self.result], "refuse_unsupported_advice": False}


class _CanaryLeakAdapter:
    name = "governed_hybrid"

    def __init__(self, delegate) -> None:
        self.delegate = delegate

    def execute(self, runtime, case):
        payload = self.delegate.execute(runtime, case)
        if case.id == "RET-01" and payload.get("results"):
            payload["results"][0]["snippet"] = CANARIES[0]
        return payload


def test_eval_v1_counts_every_adapter_and_governed_search_invocation(tmp_path, monkeypatch) -> None:
    adapters = tuple(_CountingAdapter(adapter) for adapter in memory_eval.memory_eval_v1_adapters())
    governed_calls = 0
    real_search = memory_eval.search_chatgpt_memory

    def counted_search(*args, **kwargs):
        nonlocal governed_calls
        governed_calls += 1
        return real_search(*args, **kwargs)

    monkeypatch.setattr(memory_eval, "memory_eval_v1_adapters", lambda: adapters)
    monkeypatch.setattr(memory_eval, "search_chatgpt_memory", counted_search)
    report = memory_eval.run_memory_eval_v1(tmp_path / "eval-v1", invocation=("pytest", "adapter-spy"))

    assert report["schema_version"] == "offline_synthetic_retrieval_report_v1.2"
    assert report["dataset_identity"]["version"] == memory_eval.DATASET_VERSION
    assert report["dataset_identity"]["case_count"] == len(memory_eval.memory_eval_v1_cases()) == 28
    assert all(adapter.calls == 28 for adapter in adapters)
    assert governed_calls == 28
    assert tuple(variant["variant"] for variant in report["variants"]) == memory_eval.VARIANT_NAMES


def test_no_memory_adapter_is_gold_blind_and_consistently_abstains() -> None:
    class GoldAccessRaises:
        def __getattribute__(self, name):
            raise AssertionError(f"NoMemoryAdapter inspected case field {name}")

    payload = memory_eval.NoMemoryAdapter().execute(None, GoldAccessRaises())

    assert payload == {
        "result_quality": "no_context_available",
        "results": [],
        "refuse_unsupported_advice": True,
        "baseline_behavior": "consistent_abstention",
    }


def test_eval_v1_adapters_diverge_and_scope_is_not_ab_testing(tmp_path) -> None:
    report = memory_eval.run_memory_eval_v1(tmp_path / "eval-v1")
    metrics = {variant["variant"]: variant["metrics"] for variant in report["variants"]}

    assert report["benchmark_scope"]["name"] == "deterministic_offline_synthetic_retrieval_comparison"
    assert "online_ab_testing" in report["benchmark_scope"]["not_validated"]
    assert report["benchmark_scope"]["vector_baseline"] == "test_only_deterministic_token_hash"
    assert metrics["no_memory"]["mrr"] == 0.0
    assert metrics["raw_fts"]["stale_selection_rate"] > metrics["governed_hybrid"]["stale_selection_rate"]
    assert metrics["vector_only"]["abstention_accuracy"] < metrics["governed_hybrid"]["abstention_accuracy"]
    assert metrics["raw_fts"]["wrong_domain_rate"] > metrics["governed_hybrid"]["wrong_domain_rate"]
    assert (
        metrics["governed_hybrid"]["retrieval_contract_pass_rate"] > metrics["raw_fts"]["retrieval_contract_pass_rate"]
    )
    assert metrics["governed_hybrid"]["task_success_rate"] == metrics["governed_hybrid"]["retrieval_contract_pass_rate"]


def test_release_status_fails_when_one_designated_hard_gate_fails(tmp_path, monkeypatch) -> None:
    real_adapters = memory_eval.memory_eval_v1_adapters()
    failing = _OneCaseFailureAdapter(real_adapters[-1])
    monkeypatch.setattr(memory_eval, "memory_eval_v1_adapters", lambda: (*real_adapters[:-1], failing))

    report = memory_eval.run_memory_eval_v1(tmp_path / "failed-eval")

    assert report["status"] == "fail"
    assert report["release_assessment"]["status"] == "pre_release"
    assert report["release_assessment"]["all_designated_hard_gates_passed"] is False
    assert "RET-01" in report["release_assessment"]["failed_hard_gate_case_ids"]
    assert eval_runner.report_exit_code(report) == 1


def test_detected_retrieval_canary_fails_gate_but_is_scrubbed_from_report(tmp_path, monkeypatch) -> None:
    real_adapters = memory_eval.memory_eval_v1_adapters()
    leaking = _CanaryLeakAdapter(real_adapters[-1])
    monkeypatch.setattr(memory_eval, "memory_eval_v1_adapters", lambda: (*real_adapters[:-1], leaking))

    report = memory_eval.run_memory_eval_v1(tmp_path / "leaking-eval")
    serialized = json.dumps(report, sort_keys=True)

    assert report["status"] == "fail"
    assert report["release_assessment"]["status"] == "pre_release"
    assert report["release_assessment"]["retrieval_canaries_absent"] is False
    assert report["release_assessment"]["report_canaries_absent"] is True
    assert report["fixture_integrity"]["retrieval_output_canary_scan"]["status"] == "fail"
    assert CANARIES[0] not in serialized


def test_detected_post_ingestion_canary_fails_persistence_gate(tmp_path, monkeypatch) -> None:
    real_import = memory_eval.import_chatgpt_export

    def leaking_import(*, input_path, data_dir, memory_dir):
        result = real_import(input_path=input_path, data_dir=data_dir, memory_dir=memory_dir)
        (Path(data_dir) / "simulated-ingestion-leak.txt").write_text(CANARIES[1], encoding="utf-8")
        return result

    monkeypatch.setattr(memory_eval, "import_chatgpt_export", leaking_import)

    report = memory_eval.run_memory_eval_v1(tmp_path / "leaking-persistence")

    assert report["status"] == "fail"
    assert report["release_assessment"]["persisted_canaries_absent"] is False
    scan = report["fixture_integrity"]["downstream_persistence_canary_scan"]
    assert scan["status"] == "fail"
    assert scan["canaries"][1]["found_surfaces"] == ["eval_workdir/data/simulated-ingestion-leak.txt"]
    assert CANARIES[1] not in json.dumps(report, sort_keys=True)


def test_artifact_runner_returns_nonzero_and_persists_pre_release_evidence(tmp_path, monkeypatch) -> None:
    report = memory_eval.run_memory_eval_v1(tmp_path / "source")
    report["status"] = "fail"
    report["release_assessment"]["status"] = "pre_release"
    report["release_assessment"]["all_designated_hard_gates_passed"] = False
    report["release_assessment"]["failed_hard_gate_case_ids"] = ["RET-01"]
    monkeypatch.setattr(eval_runner, "run_memory_eval_v1", lambda *args, **kwargs: report)
    output_dir = tmp_path / "failed-artifacts"
    output_dir.mkdir()

    exit_code = eval_runner.main(["--output-dir", str(output_dir), "--invocation-label", "pytest"])

    assert exit_code == 1
    machine = json.loads((output_dir / "memory-eval-v1.json").read_text(encoding="utf-8"))
    human = (output_dir / "memory-eval-v1.md").read_text(encoding="utf-8")
    assert machine["release_assessment"]["status"] == "pre_release"
    assert "No production-readiness claim is supported" in human


def test_eval_report_persistence_enforces_private_no_follow_boundary(tmp_path) -> None:
    report = memory_eval.run_memory_eval_v1(tmp_path / "source")
    diagnostic_token = "sk-" + "evalabcdefghijklmnopqrstuvwxyz123456"
    report["diagnostic_token"] = diagnostic_token
    missing_root = tmp_path / "missing-output"

    with pytest.raises(PathPolicyError, match="must already exist"):
        eval_runner.persist_report(report, missing_root)

    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    linked_root = tmp_path / "linked-output"
    linked_root.symlink_to(outside_dir, target_is_directory=True)
    with pytest.raises(PathPolicyError, match="must not be a symlink"):
        eval_runner.persist_report(report, linked_root)

    output_dir = tmp_path / "authorized-output"
    output_dir.mkdir()
    outside_file = tmp_path / "outside.json"
    outside_file.write_text("outside remains unchanged\n", encoding="utf-8")
    json_path = output_dir / "memory-eval-v1.json"
    json_path.symlink_to(outside_file)
    with pytest.raises((PathPolicyError, OSError)):
        eval_runner.persist_report(report, output_dir)
    assert outside_file.read_text(encoding="utf-8") == "outside remains unchanged\n"

    json_path.unlink()
    json_path.hardlink_to(outside_file)
    with pytest.raises(PathPolicyError, match="multiple hard links"):
        eval_runner.persist_report(report, output_dir)
    assert outside_file.read_text(encoding="utf-8") == "outside remains unchanged\n"

    json_path.unlink()
    artifacts = eval_runner.persist_report(report, output_dir)
    machine_payload = Path(artifacts["json"]).read_text(encoding="utf-8")
    assert diagnostic_token not in machine_payload
    assert "[REDACTED]" in machine_payload
    assert stat.S_IMODE(Path(artifacts["json"]).stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(artifacts["markdown"]).stat().st_mode) == 0o600


def test_raw_canaries_reach_ingestion_then_never_persist_or_surface(tmp_path, monkeypatch) -> None:
    real_import = memory_eval.import_chatgpt_export
    observed_raw_canaries: list[str] = []

    def inspect_raw_input(*, input_path, data_dir, memory_dir):
        raw_payload = b"".join(path.read_bytes() for path in Path(input_path).rglob("*") if path.is_file())
        observed_raw_canaries.extend(literal for literal in CANARIES if literal.encode() in raw_payload)
        return real_import(input_path=input_path, data_dir=data_dir, memory_dir=memory_dir)

    monkeypatch.setattr(memory_eval, "import_chatgpt_export", inspect_raw_input)
    work_dir = tmp_path / "eval-v1"
    report = memory_eval.run_memory_eval_v1(work_dir)
    persisted_payload = b"".join(path.read_bytes() for path in work_dir.rglob("*") if path.is_file())
    sqlite_paths = list(work_dir.rglob("*.sqlite3*"))
    serialized_report = json.dumps(report, sort_keys=True).encode()
    human_report = eval_runner.render_human_report(report).encode()

    assert observed_raw_canaries == list(CANARIES)
    assert sqlite_paths
    assert b"[REDACTED:" in persisted_payload
    assert not list(work_dir.glob("ephemeral-raw-input-*"))
    for literal in CANARIES:
        encoded = literal.encode()
        assert encoded not in persisted_payload
        assert encoded not in serialized_report
        assert encoded not in human_report
    integrity = report["fixture_integrity"]
    assert integrity["raw_input_canary_validation"]["status"] == "pass"
    assert integrity["raw_input_canary_validation"]["input_sanitized_before_ingestion"] is False
    assert integrity["raw_input_canary_validation"]["removed_after_ingestion"] is True
    assert integrity["downstream_persistence_canary_scan"]["status"] == "pass"
    assert integrity["retrieval_output_canary_scan"]["status"] == "pass"
    assert integrity["report_output_canary_scan"]["status"] == "pass"


def test_ephemeral_raw_export_write_is_private_and_rejects_links(tmp_path) -> None:
    conversations = memory_eval.memory_eval_v1_conversations()
    raw_root = tmp_path / "raw"
    raw_root.mkdir(mode=0o700)

    export_path = memory_eval._write_ephemeral_raw_export(raw_root, conversations)
    assert CANARIES[0] in export_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(export_path.stat().st_mode) == 0o600

    outside = tmp_path / "outside.json"
    outside.write_text("outside remains unchanged\n", encoding="utf-8")
    export_path.unlink()
    export_path.symlink_to(outside)
    with pytest.raises((PathPolicyError, OSError)):
        memory_eval._write_ephemeral_raw_export(raw_root, conversations)
    assert outside.read_text(encoding="utf-8") == "outside remains unchanged\n"

    export_path.unlink()
    export_path.hardlink_to(outside)
    with pytest.raises(PathPolicyError, match="multiple hard links"):
        memory_eval._write_ephemeral_raw_export(raw_root, conversations)
    assert outside.read_text(encoding="utf-8") == "outside remains unchanged\n"


def test_ephemeral_raw_export_rejects_symlinked_parent(tmp_path) -> None:
    raw_root = tmp_path / "raw"
    raw_root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    (raw_root / memory_eval.DATASET_VERSION).symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathPolicyError, match="symlink"):
        memory_eval._write_ephemeral_raw_export(raw_root, memory_eval.memory_eval_v1_conversations())
    assert not (outside / "conversations.json").exists()


def test_ephemeral_raw_export_is_removed_when_ingestion_fails(tmp_path, monkeypatch) -> None:
    raw_roots: list[Path] = []
    real_mkdtemp = memory_eval.tempfile.mkdtemp

    def tracked_mkdtemp(*args, **kwargs):
        path = Path(real_mkdtemp(*args, **kwargs))
        raw_roots.append(path)
        return str(path)

    def failed_import(**kwargs):
        raise RuntimeError("simulated ingestion failure")

    monkeypatch.setattr(memory_eval.tempfile, "mkdtemp", tracked_mkdtemp)
    monkeypatch.setattr(memory_eval, "import_chatgpt_export", failed_import)

    with pytest.raises(RuntimeError, match="simulated ingestion failure"):
        memory_eval._prepare_runtime(tmp_path / "eval-v1")

    assert len(raw_roots) == 1
    assert not raw_roots[0].exists()


def test_report_uses_only_stable_path_labels(tmp_path) -> None:
    report = memory_eval.run_memory_eval_v1(
        tmp_path / "eval-v1",
        invocation=(
            "scripts/run_evals.sh",
            str(tmp_path / "private-artifacts"),
            f"--work-dir={tmp_path / 'private-work'}",
        ),
    )
    serialized = json.dumps(report, sort_keys=True)

    assert str(Path.home()) not in serialized
    assert str(tmp_path) not in serialized
    assert report["generation_identity"]["working_directory"].startswith("repository")
    assert "external_working_directory" in report["generation_identity"]["invocation"]
    assert "--work-dir=external_working_directory" in report["generation_identity"]["invocation"]
    assert all(
        path.startswith("eval_workdir/")
        for path in report["fixture_integrity"]["downstream_persistence_canary_scan"]["scanned_files"]
    )


def test_provenance_resolves_fixture_records_and_misc_is_wrong_domain(tmp_path) -> None:
    runtime = memory_eval._prepare_runtime(tmp_path / "runtime")
    case = next(case for case in memory_eval.memory_eval_v1_cases() if case.id == "RET-01")
    base_result = {
        "source_id": "src_lab_parser",
        "internal_source_id": "internal",
        "title": "Lab automation parser",
        "role": "user",
        "source_kind": "chatgpt_export",
        "domain_primary": "lab_automation",
        "snippet": "fixture",
        "score": 1.0,
        "retrieval_source": "test",
        "provenance_complete": True,
    }

    wrong_role = memory_eval._evaluate_case(runtime, _StaticAdapter({**base_result, "role": "assistant"}), case)
    misc_domain = memory_eval._evaluate_case(runtime, _StaticAdapter({**base_result, "domain_primary": "misc"}), case)

    assert wrong_role["metrics"]["provenance_correct"] is False
    assert "unresolved_provenance" in wrong_role["failure_reasons"]
    assert misc_domain["metrics"]["wrong_domain_count"] == 1
    assert "wrong_domain_selected" in misc_domain["failure_reasons"]
    assert misc_domain["category"] == "retrieval"


def test_report_binds_generation_repository_lock_and_code_identity(tmp_path) -> None:
    first = memory_eval.run_memory_eval_v1(tmp_path / "first", invocation=("pytest", "identity"))
    second = memory_eval.run_memory_eval_v1(tmp_path / "second", invocation=("pytest", "identity"))

    assert first["dataset_identity"] == second["dataset_identity"]
    assert first["config_identity"] == second["config_identity"]
    assert first["generation_identity"]["invocation"] == ["pytest", "identity"]
    assert first["generation_identity"]["invocation_id"] != second["generation_identity"]["invocation_id"]
    datetime.fromisoformat(first["generation_identity"]["generated_at"])
    assert len(first["repository_identity"]["git_commit"]) == 40
    assert isinstance(first["repository_identity"]["git_dirty"], bool)
    assert len(first["repository_identity"]["lock_sha256"]) == 64
    assert len(first["code_identity"]["aggregate_sha256"]) == 64
    assert "src/local_agent_lab/evals/memory_v1.py" in first["code_identity"]["files"]
    assert "src/local_agent_lab/memory/search.py" in first["code_identity"]["files"]


def test_synthetic_starter_fixture_does_not_reuse_private_history() -> None:
    serialized = json.dumps([memory_eval.asdict(item) for item in memory_eval.memory_eval_v1_conversations()])

    assert "Northstar" in serialized
    assert "Synthetic" in serialized
    assert "rose 82 percent over 18 hours" in serialized
    assert "receded 14 percent" in serialized


def test_multi_turn_corrections_select_active_truth_and_retain_superseded_audit_evidence(tmp_path) -> None:
    report = memory_eval.run_memory_eval_v1(tmp_path / "eval-v1")
    governed = {item["case_id"]: item for item in report["case_results"]["governed_hybrid"]}

    cadence = governed["TMP-04"]
    starter = governed["PROV-01"]
    assert cadence["observed_source_ids"][0] == "src_cadence_new"
    assert cadence["diagnostics"]["stale_source_ids_found"] == []
    assert "src_cadence_old" in cadence["diagnostics"]["superseded_evidence_retained_after_active"]
    assert starter["observed_source_ids"][0] == "src_starter_current"
    assert starter["metrics"]["retrieval_contract_pass"] is True


def test_run_evals_script_persists_machine_and_human_artifacts(tmp_path) -> None:
    repository = memory_eval._repository_root()
    output_dir = tmp_path / "artifacts"
    completed = subprocess.run(
        ("bash", "scripts/run_evals.sh", str(output_dir)),
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    machine = json.loads((output_dir / "memory-eval-v1.json").read_text(encoding="utf-8"))
    human = (output_dir / "memory-eval-v1.md").read_text(encoding="utf-8")
    assert machine["status"] == "pass"
    assert machine["release_assessment"]["failed_hard_gate_case_ids"] == []
    assert "Offline Synthetic Retrieval Comparison" in human
    assert "not an online A/B test" in human
    assert "28/28 designated contracts" in human
