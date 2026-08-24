from __future__ import annotations

from collections import defaultdict
from typing import Any


def score_expectations(expectations: list[tuple[str, bool, dict[str, Any] | None]]) -> dict[str, Any]:
    scored = [
        {
            "name": name,
            "status": "pass" if passed else "fail",
            "details": details or {},
        }
        for name, passed, details in expectations
    ]
    passed = sum(1 for item in scored if item["status"] == "pass")
    total = len(scored)
    return {
        "status": "pass" if passed == total else "fail",
        "score": passed,
        "max_score": total,
        "score_pct": round((passed / total) * 100, 1) if total else 100.0,
        "expectations": scored,
    }


def summarize_usage_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    by_category: dict[str, dict[str, Any]] = defaultdict(lambda: {"cases": 0, "score": 0, "max_score": 0, "failed": 0})
    by_complexity: dict[str, dict[str, Any]] = defaultdict(lambda: {"cases": 0, "score": 0, "max_score": 0, "failed": 0})
    total_score = 0
    total_max = 0
    failed = 0
    for case in cases:
        score = int(case.get("score", 0))
        max_score = int(case.get("max_score", 0))
        category = str(case.get("category", "uncategorized"))
        complexity = str(case.get("complexity", "unknown"))
        total_score += score
        total_max += max_score
        if case.get("status") != "pass":
            failed += 1
        for bucket in (by_category[category], by_complexity[complexity]):
            bucket["cases"] += 1
            bucket["score"] += score
            bucket["max_score"] += max_score
            if case.get("status") != "pass":
                bucket["failed"] += 1

    def finalize(bucket: dict[str, Any]) -> dict[str, Any]:
        max_score = int(bucket["max_score"])
        return {
            **bucket,
            "score_pct": round((int(bucket["score"]) / max_score) * 100, 1) if max_score else 100.0,
        }

    return {
        "cases": len(cases),
        "failed": failed,
        "score": total_score,
        "max_score": total_max,
        "score_pct": round((total_score / total_max) * 100, 1) if total_max else 100.0,
        "by_category": {key: finalize(value) for key, value in sorted(by_category.items())},
        "by_complexity": {key: finalize(value) for key, value in sorted(by_complexity.items())},
    }


def runner_status() -> str:
    return "ready"
