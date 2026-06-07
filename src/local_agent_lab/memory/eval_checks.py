from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .audit import record_retrieval_event, retrieval_exposures_for_run
from .chatgpt_ingest import import_chatgpt_export
from .curated import promote_chunk_to_memory_record
from .search import search_chatgpt_memory
from .subjects import assign_conversation_subject


def run_memory_eval(work_dir: Path) -> dict[str, Any]:
    work_dir.mkdir(parents=True, exist_ok=True)
    input_path = _write_eval_export(work_dir)
    data_dir = work_dir / "data"
    memory_dir = data_dir / "memory"
    import_report = import_chatgpt_export(input_path=input_path, data_dir=data_dir, memory_dir=memory_dir)
    db_path = memory_dir / "chatgpt_memory.sqlite3"

    checks: list[dict[str, Any]] = []
    exact = search_chatgpt_memory(memory_dir=memory_dir, query="barcode parser")
    checks.append(_check("exact_search", exact["count"] >= 1, {"count": exact["count"]}))

    secret = search_chatgpt_memory(memory_dir=memory_dir, query="credential")
    snippet = secret["results"][0]["snippet"] if secret["results"] else ""
    checks.append(
        _check(
            "redaction",
            "sk-abcdefghijklmnopqrstuvwxyz123456" not in snippet and bool(secret["results"]),
            {"snippet": snippet, "redacted": secret["results"][0].get("redacted_secret_count") if secret["results"] else None},
        )
    )

    with sqlite3.connect(db_path) as connection:
        conversation_id = connection.execute(
            "SELECT id FROM conversations WHERE title = 'Lab automation parser'"
        ).fetchone()[0]
        assign_conversation_subject(connection, conversation_id, "Lab Automation", include_chunks=True)
    subject = search_chatgpt_memory(memory_dir=memory_dir, query="barcode", subject="Lab Automation")
    checks.append(_check("subject_filter", subject["count"] >= 1, {"count": subject["count"]}))

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

    failed = [check for check in checks if check["status"] != "pass"]
    return {
        "status": "fail" if failed else "pass",
        "import_report": {
            "import_id": import_report["import_id"],
            "summary": import_report["summary"],
        },
        "checks": checks,
        "summary": {
            "checks": len(checks),
            "passed": len(checks) - len(failed),
            "failed": len(failed),
        },
    }


def _write_eval_export(root: Path) -> Path:
    raw_dir = root / "raw" / "eval-export"
    raw_dir.mkdir(parents=True, exist_ok=True)
    export = [
        {
            "id": "eval-lab",
            "title": "Lab automation parser",
            "mapping": {
                "u": {
                    "id": "u",
                    "message": {
                        "id": "u",
                        "author": {"role": "user"},
                        "content": {"parts": ["Where is the barcode parser configured?"]},
                    },
                },
                "a": {
                    "id": "a",
                    "message": {
                        "id": "a",
                        "author": {"role": "assistant"},
                        "content": {"parts": ["Use the lab automation barcode parser workflow."]},
                    },
                },
            },
        },
        {
            "id": "eval-secret",
            "title": "Credential note",
            "mapping": {
                "u": {
                    "id": "secret",
                    "message": {
                        "id": "secret",
                        "author": {"role": "user"},
                        "content": {"parts": ["credential sk-abcdefghijklmnopqrstuvwxyz123456 should be redacted"]},
                    },
                }
            },
        },
    ]
    (raw_dir / "conversations.json").write_text(json.dumps(export), encoding="utf-8")
    return root / "raw"


def _check(name: str, passed: bool, details: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "status": "pass" if passed else "fail", "details": details}
