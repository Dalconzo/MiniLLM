from local_agent_lab.memory.eval_checks import run_memory_eval


def test_run_memory_eval_reports_passes(tmp_path) -> None:
    report = run_memory_eval(tmp_path / "eval")

    assert report["status"] == "pass"
    assert report["summary"]["failed"] == 0
    assert {check["name"] for check in report["checks"]} == {
        "exact_search",
        "redaction",
        "subject_filter",
        "curated_retrieval",
        "audit_exposures",
    }
