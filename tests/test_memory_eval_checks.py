from local_agent_lab.memory.eval_checks import run_memory_eval


def test_run_memory_eval_reports_passes(tmp_path) -> None:
    report = run_memory_eval(tmp_path / "eval")

    assert report["status"] == "pass"
    assert report["summary"]["failed"] == 0
    assert report["summary"]["usage_failed"] == 0
    assert report["summary"]["usage_cases"] == 14
    assert report["summary"]["usage_score"] == report["summary"]["usage_max_score"] == 70
    assert report["usage_summary"]["score_pct"] == 100.0
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
