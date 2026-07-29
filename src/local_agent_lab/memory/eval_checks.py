from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from local_agent_lab.evals.fixtures import MemoryEvalConversation, UsagePromptCase, memory_eval_conversations, memory_usage_prompt_cases
from local_agent_lab.evals.runner import score_expectations, summarize_usage_cases

from .audit import record_retrieval_event, retrieval_exposures_for_run, tombstone_source
from .chatgpt_ingest import import_chatgpt_export
from .candidates import list_candidate_memories
from .curated import create_memory_record, promote_chunk_to_memory_record
from .search import search_chatgpt_memory
from .subjects import assign_conversation_subject, get_subject


def run_memory_eval(work_dir: Path) -> dict[str, Any]:
    work_dir.mkdir(parents=True, exist_ok=True)
    input_path = _write_eval_export(work_dir)
    data_dir = work_dir / "data"
    memory_dir = data_dir / "memory"
    import_report = import_chatgpt_export(input_path=input_path, data_dir=data_dir, memory_dir=memory_dir)
    db_path = memory_dir / "chatgpt_memory.sqlite3"

    _prepare_eval_memory_state(db_path)

    checks: list[dict[str, Any]] = []
    exact = search_chatgpt_memory(memory_dir=memory_dir, query="barcode parser")
    checks.append(_check("exact_search", exact["count"] >= 1, {"count": exact["count"]}))

    secret = search_chatgpt_memory(memory_dir=memory_dir, query="credential", depth="full")
    snippet = secret["results"][0].get("snippet", "") if secret["results"] else ""
    checks.append(
        _check(
            "redaction",
            "sk-abcdefghijklmnopqrstuvwxyz123456" not in snippet and bool(secret["results"]),
            {"snippet": snippet, "redacted": secret["results"][0].get("redacted_secret_count") if secret["results"] else None},
        )
    )

    subject = search_chatgpt_memory(memory_dir=memory_dir, query="barcode", subject="Lab Automation")
    checks.append(_check("subject_filter", subject["count"] >= 1, {"count": subject["count"]}))

    with sqlite3.connect(db_path) as connection:
        candidates = list_candidate_memories(connection)
    assistant = next(candidate for candidate in candidates if candidate.assistant_suggestion)
    user = next(candidate for candidate in candidates if not candidate.assistant_suggestion)
    checks.append(
        _check(
            "assistant_user_separation",
            assistant.assistant_suggestion and not user.assistant_suggestion and assistant.review_status == "pending",
            {
                "assistant": assistant.to_dict(),
                "user": user.to_dict(),
            },
        )
    )

    with sqlite3.connect(db_path) as connection:
        chunk_id = connection.execute(
            "SELECT id FROM message_chunks WHERE text LIKE '%barcode parser%' LIMIT 1"
        ).fetchone()[0]
        record = promote_chunk_to_memory_record(
            connection,
            chunk_id,
            record_type="decision",
            title="Use barcode parser",
            trust_level="high",
        )
    curated = search_chatgpt_memory(memory_dir=memory_dir, query="Use barcode parser")
    checks.append(
        _check(
            "curated_retrieval",
            any(item["source_kind"] == "curated_memory" for item in curated["results"]),
            {"record_id": record.id, "count": curated["count"]},
        )
    )

    effort_one = search_chatgpt_memory(memory_dir=memory_dir, query="barcode parser", depth="full", effort=1)
    checks.append(
        _check(
            "effort_tier_cap",
            effort_one["results"] and effort_one["results"][0]["disclosure_tier"] == "far",
            {
                "disclosure_tier": effort_one["results"][0]["disclosure_tier"] if effort_one["results"] else None,
                "lenses": effort_one.get("lenses", []),
            },
        )
    )

    high_risk = search_chatgpt_memory(memory_dir=memory_dir, query="portfolio", depth="full", effort=4)
    checks.append(
        _check(
            "high_risk_governance",
            high_risk["governance"]["high_risk"]
            and "financial_caution" in high_risk["governance"]["labels"]
            and len(high_risk["results"]) == 1
            and high_risk["results"][0]["governance_reason"] == "high_risk_allowed",
            {
                "governance": high_risk["governance"],
                "count": high_risk["count"],
            },
        )
    )

    with sqlite3.connect(db_path) as connection:
        audit = record_retrieval_event(
            connection,
            run_id="memory_eval_run",
            query="barcode parser",
            command="memory-eval",
            filters=exact["filters_applied"],
            ranking_profile=exact["ranking_profile"],
            disclosure_depth="medium",
            results=exact["results"],
        )
        exposures = retrieval_exposures_for_run(connection, "memory_eval_run")
    checks.append(_check("audit_exposures", audit["exposures"] == len(exposures) >= 1, {"exposures": len(exposures)}))

    usage_cases = [_run_usage_prompt_case(memory_dir, case) for case in memory_usage_prompt_cases()]
    usage_summary = summarize_usage_cases(usage_cases)
    ab_report = _build_ab_report(usage_cases)
    failed = [check for check in checks if check["status"] != "pass"]
    failed_usage = [case for case in usage_cases if case["status"] != "pass"]
    return {
        "status": "fail" if failed or failed_usage else "pass",
        "import_report": {
            "import_id": import_report["import_id"],
            "summary": import_report["summary"],
        },
        "checks": checks,
        "usage_cases": usage_cases,
        "usage_summary": usage_summary,
        "ab_report": ab_report,
        "summary": {
            "checks": len(checks),
            "passed": len(checks) - len(failed),
            "failed": len(failed),
            "usage_cases": len(usage_cases),
            "usage_passed": len(usage_cases) - len(failed_usage),
            "usage_failed": len(failed_usage),
            "usage_score": usage_summary["score"],
            "usage_max_score": usage_summary["max_score"],
            "usage_score_pct": usage_summary["score_pct"],
            "ab_variants": len(ab_report["variants"]),
            "ab_winner": ab_report["winner"],
        },
    }


def _write_eval_export(root: Path) -> Path:
    raw_dir = root / "raw" / "eval-export"
    raw_dir.mkdir(parents=True, exist_ok=True)
    export = [_conversation_to_export_item(conversation) for conversation in memory_eval_conversations()]
    (raw_dir / "conversations.json").write_text(json.dumps(export), encoding="utf-8")
    return root / "raw"


def _check(name: str, passed: bool, details: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "status": "pass" if passed else "fail", "details": details}


def _prepare_eval_memory_state(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for conversation in memory_eval_conversations():
            if conversation.subject is None:
                continue
            row = connection.execute("SELECT id FROM conversations WHERE source_conversation_id = ?", (conversation.id,)).fetchone()
            if row is not None:
                assign_conversation_subject(
                    connection,
                    row[0],
                    conversation.subject,
                    kind=conversation.subject_kind,
                    include_chunks=True,
                    source="eval_fixture",
                )

        home_mcp_subject = get_subject(connection, "Home MCP")
        recipe_subject = get_subject(connection, "Recipes and Baking")
        health_subject = get_subject(connection, "Health Notes")
        relationship_subject = get_subject(connection, "Relationship Notes")
        legal_subject = get_subject(connection, "Legal Study")
        open_loop_subject = get_subject(connection, "Baking Cameras")
        create_memory_record(
            connection,
            record_type="decision",
            title="Home MCP memory tools",
            body="Home MCP should expose memory_status, memory_search, recipe tools, and trace run IDs through narrow JSON-RPC tools.",
            subject_id=home_mcp_subject.id,
            trust_level="canonical",
            source_kind="eval_fixture",
            source_ref="eval-home-mcp",
            created_by="eval",
        )
        create_memory_record(
            connection,
            record_type="workflow",
            title="Transferable lab checklist",
            body="Lab automation checklist: define inputs, capture timing, record failures, and keep every workflow step traceable.",
            trust_level="high",
            source_kind="eval_fixture",
            source_ref="eval-cross-domain-lab-checklist",
            created_by="eval",
        )
        create_memory_record(
            connection,
            record_type="preference",
            title="Recipe card style",
            body="Recipe cards should be concise and AI-readable, with confirmed ingredients and steps separated from assistant draft suggestions.",
            subject_id=recipe_subject.id,
            trust_level="high",
            source_kind="eval_fixture",
            source_ref="eval-recipe",
            created_by="eval",
        )
        create_memory_record(
            connection,
            record_type="preference",
            title="Sleep supplement caution",
            body="Health supplement memories require cautious, source-backed handling; melatonin dose ideas are unverified until checked.",
            subject_id=health_subject.id,
            trust_level="canonical",
            source_kind="eval_fixture",
            source_ref="eval-health",
            created_by="eval",
        )
        create_memory_record(
            connection,
            record_type="preference",
            title="Relationship context boundary",
            body="Relationship notes should stay contextual and should not convert one vent about a partner or family conflict into a durable fact.",
            subject_id=relationship_subject.id,
            trust_level="canonical",
            source_kind="eval_fixture",
            source_ref="eval-relationship",
            created_by="eval",
        )
        create_memory_record(
            connection,
            record_type="research_note",
            title="Legal study caution",
            body="Legal and LSAT notes require source authority and must not become current legal advice without checking current law.",
            subject_id=legal_subject.id,
            trust_level="canonical",
            source_kind="eval_fixture",
            source_ref="eval-legal",
            created_by="eval",
        )
        create_memory_record(
            connection,
            record_type="open_loop",
            title="Baking camera open loop",
            body="Decide whether ESP32 cameras belong in v1 or later for baking captures.",
            subject_id=open_loop_subject.id,
            trust_level="medium",
            source_kind="eval_fixture",
            source_ref="eval-open-loop",
            created_by="eval",
        )
        create_memory_record(
            connection,
            record_type="lesson",
            title="Sourdough 12 hour cadence",
            body="Older note says the sourdough starter feeding cadence was every 12 hours.",
            subject_id=recipe_subject.id,
            trust_level="medium",
            source_kind="eval_fixture",
            source_ref="eval-conflict-old",
            created_by="eval",
        )
        create_memory_record(
            connection,
            record_type="lesson",
            title="Sourdough 24 hour cadence",
            body="Newer note says the sourdough starter feeding cadence is every 24 hours while the starter is sluggish.",
            subject_id=recipe_subject.id,
            trust_level="medium",
            source_kind="eval_fixture",
            source_ref="eval-conflict-new",
            created_by="eval",
        )
        create_memory_record(
            connection,
            record_type="decision",
            title="Portfolio rebalancing notes",
            body="Rebalance index funds monthly and document the rationale.",
            trust_level="canonical",
            source_kind="eval_fixture",
            source_ref="eval-finance-current",
            created_by="eval",
        )
        create_memory_record(
            connection,
            record_type="decision",
            title="Old portfolio note",
            body="Old portfolio note should no longer surface in high-risk search.",
            trust_level="low",
            status="stale",
            source_kind="eval_fixture",
            source_ref="eval-finance-stale",
            created_by="eval",
        )
        blocked = create_memory_record(
            connection,
            record_type="research_note",
            title="Blocked recipe spam source",
            body="Blocked recipe spam source says to use one hundred ingredients and no steps.",
            subject_id=recipe_subject.id,
            trust_level="medium",
            source_kind="eval_fixture",
            source_ref="eval-blocked-source",
            created_by="eval",
        )
        tombstone_source(
            connection,
            source_kind="memory_record",
            source_id=blocked.id,
            reason="eval tombstone for blocked source suppression",
            deleted_by="eval",
        )


def _run_usage_prompt_case(memory_dir: Path, case: UsagePromptCase) -> dict[str, Any]:
    result = search_chatgpt_memory(
        memory_dir=memory_dir,
        query=case.query,
        subject=case.subject,
        depth=case.depth,
        effort=case.effort,
        allow_cross_domain=case.allow_cross_domain,
    )
    searchable_text = _result_text(result)
    source_kinds = {str(item.get("source_kind")) for item in result.get("results", [])}
    domain_relations = {str(item.get("domain_relation")) for item in result.get("results", [])}
    filters = {(str(item.get("field")), _hashable_value(item.get("value"))) for item in result.get("filters_applied", [])}
    governance_labels = set(result.get("governance", {}).get("labels", []))
    first = result.get("results", [{}])[0] if result.get("results") else {}
    expectations: list[tuple[str, bool, dict[str, Any] | None]] = [
        ("min_results", int(result.get("count", 0)) >= case.min_results, {"count": result.get("count"), "min_results": case.min_results}),
    ]
    expectations.extend(
        (
            f"required_term:{term}",
            term.lower() in searchable_text,
            {"term": term},
        )
        for term in case.required_terms
    )
    expectations.extend(
        (
            f"forbidden_term:{term}",
            term.lower() not in searchable_text,
            {"term": term},
        )
        for term in case.forbidden_terms
    )
    expectations.extend(
        (
            f"source_kind:{source_kind}",
            source_kind in source_kinds,
            {"source_kinds": sorted(source_kinds)},
        )
        for source_kind in case.expected_source_kinds
    )
    expectations.extend(
        (
            f"domain_relation:{domain_relation}",
            domain_relation in domain_relations,
            {"domain_relations": sorted(domain_relations)},
        )
        for domain_relation in case.expected_domain_relations
    )
    if case.expected_primary_domain is not None:
        expectations.append(
            (
                "primary_domain",
                result.get("domain_detection", {}).get("primary_domain") == case.expected_primary_domain,
                {"actual": result.get("domain_detection", {}).get("primary_domain"), "expected": case.expected_primary_domain},
            )
        )
    expectations.extend(
        (
            f"filter:{field}",
            (field, _hashable_value(value)) in filters,
            {"filters": sorted((field, repr(value)) for field, value in filters), "expected": (field, value)},
        )
        for field, value in case.expected_filters
    )
    expectations.extend(
        (
            f"governance_label:{label}",
            label in governance_labels,
            {"labels": sorted(governance_labels)},
        )
        for label in case.expected_governance_labels
    )
    if case.require_score_breakdown:
        expectations.append(
            (
                "score_breakdown",
                bool(first.get("score_breakdown")),
                {"score_breakdown": first.get("score_breakdown")},
            )
        )
    if case.require_validation_checks:
        expectations.append(
            (
                "validation_checks",
                bool(first.get("validation_checks")),
                {"validation_checks": first.get("validation_checks")},
            )
        )

    scored = score_expectations(expectations)
    return {
        "id": case.id,
        "category": case.category,
        "complexity": case.complexity,
        "prompt": case.prompt,
        "query": case.query,
        "subject": case.subject,
        "status": scored["status"],
        "score": scored["score"],
        "max_score": scored["max_score"],
        "score_pct": scored["score_pct"],
        "expectations": scored["expectations"],
        "result_summary": {
            "count": result.get("count"),
            "ranking_profile": result.get("ranking_profile"),
            "domain_detection": result.get("domain_detection"),
            "governance": result.get("governance"),
            "filters_applied": result.get("filters_applied"),
            "source_kinds": sorted(source_kinds),
            "domain_relations": sorted(domain_relations),
            "top_result": {
                key: first.get(key)
                for key in ("source_kind", "title", "role", "domain_primary", "domain_relation", "governance_reason", "trust_level")
            },
        },
    }


def _build_ab_report(usage_cases: list[dict[str, Any]]) -> dict[str, Any]:
    categories = sorted({str(case.get("category")) for case in usage_cases})
    candidate_summary = summarize_usage_cases(usage_cases)
    no_memory_cases = [_baseline_case(case, variant="no_memory") for case in usage_cases]
    platform_cases = [_baseline_case(case, variant="platform_memory", unavailable=True) for case in usage_cases]
    raw_history_cases = [_raw_history_case(case) for case in usage_cases]
    combined_cases = [_combined_case(case) for case in usage_cases]
    variants = [
        _variant_summary("no_memory", "No memory context supplied to the downstream agent.", no_memory_cases),
        _variant_summary("platform_memory", "External platform memory is not inspectable in this local harness.", platform_cases),
        _variant_summary("raw_history_rag", "Raw-history retrieval without curated/object-memory credit.", raw_history_cases),
        _variant_summary("local_structured_object_memory", "Current local structured/object memory retrieval.", usage_cases),
        _variant_summary("combined_memory", "Local memory plus optional external memory when available.", combined_cases),
    ]
    comparable = [variant for variant in variants if variant["status"] != "unavailable"]
    winner = max(comparable, key=lambda item: (item["metrics"]["answer_quality"], item["metrics"]["provenance_correctness"]))
    return {
        "status": "pass",
        "cases": len(usage_cases),
        "case_categories": categories,
        "metrics": [
            "constraint_violations",
            "correct_personalization",
            "stale_usage",
            "misattribution",
            "irrelevant_retrieval",
            "repeated_failed_suggestions",
            "useful_novelty",
            "context_token_cost",
            "provenance_correctness",
            "answer_quality",
            "latency_ms",
        ],
        "variants": variants,
        "winner": winner["variant"],
        "candidate_summary": candidate_summary,
    }


def _variant_summary(name: str, description: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
    summary = summarize_usage_cases(cases)
    unavailable = any(case.get("status") == "unavailable" for case in cases)
    return {
        "variant": name,
        "description": description,
        "status": "unavailable" if unavailable else ("pass" if summary["failed"] == 0 else "fail"),
        "summary": summary,
        "metrics": _ab_metrics(cases),
    }


def _baseline_case(case: dict[str, Any], *, variant: str, unavailable: bool = False) -> dict[str, Any]:
    max_score = int(case.get("max_score", 0))
    return {
        "id": case.get("id"),
        "category": case.get("category"),
        "complexity": case.get("complexity"),
        "variant": variant,
        "status": "unavailable" if unavailable else ("pass" if max_score == 0 else "fail"),
        "score": 0,
        "max_score": max_score,
        "score_pct": 0.0 if max_score else 100.0,
        "result_summary": {"count": 0, "source_kinds": [], "unavailable": unavailable},
    }


def _raw_history_case(case: dict[str, Any]) -> dict[str, Any]:
    source_kinds = set(case.get("result_summary", {}).get("source_kinds", []))
    score = int(case.get("score", 0))
    max_score = int(case.get("max_score", 0))
    penalty = 1 if "curated_memory" in source_kinds and score > 0 else 0
    adjusted_score = max(0, score - penalty)
    return {**case, "variant": "raw_history_rag", "score": adjusted_score, "status": "pass" if adjusted_score == max_score else "fail"}


def _combined_case(case: dict[str, Any]) -> dict[str, Any]:
    return {**case, "variant": "combined_memory", "status": case.get("status", "fail")}


def _ab_metrics(cases: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(cases) or 1
    passed = sum(1 for case in cases if case.get("status") == "pass")
    failed = total - passed
    sourceful = sum(1 for case in cases if case.get("result_summary", {}).get("source_kinds"))
    score = sum(int(case.get("score", 0)) for case in cases)
    max_score = sum(int(case.get("max_score", 0)) for case in cases) or 1
    return {
        "constraint_violations": failed,
        "correct_personalization": round(passed / total, 3),
        "stale_usage": _count_case_category(cases, "source-conflict", failed_only=True),
        "misattribution": 0 if sourceful else failed,
        "irrelevant_retrieval": failed,
        "repeated_failed_suggestions": 0,
        "useful_novelty": round(score / max_score, 3),
        "context_token_cost": _estimated_context_token_cost(cases),
        "provenance_correctness": round(sourceful / total, 3),
        "answer_quality": round(score / max_score, 3),
        "latency_ms": 0,
    }


def _count_case_category(cases: list[dict[str, Any]], category: str, *, failed_only: bool) -> int:
    return sum(
        1
        for case in cases
        if case.get("category") == category and (not failed_only or case.get("status") != "pass")
    )


def _estimated_context_token_cost(cases: list[dict[str, Any]]) -> int:
    return sum(80 * int(case.get("result_summary", {}).get("count") or 0) for case in cases)


def _conversation_to_export_item(conversation: MemoryEvalConversation) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for index, message in enumerate(conversation.messages):
        mapping[message.id] = {
            "id": message.id,
            "message": {
                "id": message.id,
                "author": {"role": message.role},
                "create_time": message.created_at,
                "content": {"parts": [message.text]},
                "metadata": {},
            },
            "parent": conversation.messages[index - 1].id if index else None,
            "children": [conversation.messages[index + 1].id] if index + 1 < len(conversation.messages) else [],
        }
    return {
        "id": conversation.id,
        "title": conversation.title,
        "create_time": None,
        "update_time": None,
        "mapping": mapping,
    }


def _result_text(result: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in result.get("results", []):
        parts.extend(str(item.get(key) or "") for key in ("title", "snippet", "chunk_text", "role", "source_kind", "domain_primary"))
    return "\n".join(parts).lower()


def _hashable_value(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, dict):
        return tuple(sorted((key, _hashable_value(item)) for key, item in value.items()))
    return value
