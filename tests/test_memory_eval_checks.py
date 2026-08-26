import stat

import pytest

from local_agent_lab.memory.eval_checks import _write_eval_export, run_memory_eval
from local_agent_lab.security.paths import PathPolicyError


def test_run_memory_eval_reports_passes(tmp_path) -> None:
    report = run_memory_eval(tmp_path / "eval")

    assert report["status"] == "pass"
    assert report["summary"]["failed"] == 0
    assert report["summary"]["usage_failed"] == 0
    assert report["summary"]["usage_cases"] == 14
    assert report["summary"]["usage_score"] == report["summary"]["usage_max_score"] == 70
    assert report["usage_summary"]["score_pct"] == 100.0
    assert report["evaluation_v1"]["status"] == "pass"
    assert report["evaluation_v1"]["case_count"] == 28
    assert report["evaluation_v1"]["thresholds"]["status"] == "pass"
    assert report["evaluation_v1"]["fixture_integrity"]["immutable"] is True
    assert {variant["variant"] for variant in report["ab_report"]["variants"]} == {
        "no_memory",
        "raw_fts",
        "vector_only",
        "governed_hybrid",
    }
    assert all(variant["executed_case_count"] == 28 for variant in report["evaluation_v1"]["variants"])
    assert {case["category"] for case in report["usage_cases"]} == {
        "cross-domain",
        "curated-memory",
        "governance",
        "high-risk",
        "holistic",
        "lifecycle",
        "project-catalog",
        "retrieval",
        "source-conflict",
    }
    assert {case["complexity"] for case in report["usage_cases"]} == {1, 2, 3, 4}
    assert {check["name"] for check in report["checks"]} == {
        "assistant_user_separation",
        "exact_search",
        "effort_tier_cap",
        "high_risk_governance",
        "redaction",
        "subject_filter",
        "curated_retrieval",
        "audit_exposures",
    }


def test_eval_export_fixture_is_private_and_rejects_symlink_or_hard_link_escape(tmp_path) -> None:
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    linked_root = tmp_path / "linked-eval"
    linked_root.symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises((PathPolicyError, OSError)):
        _write_eval_export(linked_root)
    assert not (outside_dir / "raw").exists()

    eval_root = tmp_path / "eval"
    raw_root = _write_eval_export(eval_root)
    fixture = raw_root / "eval-export" / "conversations.json"
    assert fixture.is_file()
    assert stat.S_IMODE(fixture.stat().st_mode) == 0o600

    outside_file = tmp_path / "outside.json"
    outside_file.write_text("outside remains unchanged\n", encoding="utf-8")
    fixture.unlink()
    fixture.hardlink_to(outside_file)
    with pytest.raises(PathPolicyError, match="multiple hard links"):
        _write_eval_export(eval_root)

    assert outside_file.read_text(encoding="utf-8") == "outside remains unchanged\n"
