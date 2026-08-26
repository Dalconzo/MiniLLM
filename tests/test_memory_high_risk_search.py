from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from local_agent_lab.memory.chatgpt_ingest import import_chatgpt_export
from local_agent_lab.memory.curated import create_memory_record
from local_agent_lab.memory.search import search_chatgpt_memory
from local_agent_lab.sqlite_utils import sqlite_connection


def _write_health_export(root):
    export_dir = root / "raw" / "health-export"
    export_dir.mkdir(parents=True)
    now = datetime.now(timezone.utc)

    def conversation(conversation_id: str, role: str, text: str, created_at: datetime) -> dict:
        message_id = f"msg-{conversation_id}"
        return {
            "id": conversation_id,
            "title": "Vitamin D supplement history",
            "mapping": {
                message_id: {
                    "id": message_id,
                    "parent": None,
                    "children": [],
                    "message": {
                        "id": message_id,
                        "author": {"role": role},
                        "create_time": created_at.timestamp(),
                        "content": {"parts": [text]},
                    },
                }
            },
        }

    payload = [
        conversation(
            "recent-user-health",
            "user",
            "I reported taking a 1000 IU vitamin D supplement dose daily.",
            now - timedelta(days=1),
        ),
        conversation(
            "stale-user-health",
            "user",
            "I reported taking a 500 IU vitamin D supplement dose daily.",
            now - timedelta(days=60),
        ),
        conversation(
            "assistant-health",
            "assistant",
            "Take a 2000 IU vitamin D supplement dose daily.",
            now - timedelta(hours=12),
        ),
    ]
    (export_dir / "conversations.json").write_text(json.dumps(payload), encoding="utf-8")
    return root / "raw"


def _memory_dir(tmp_path):
    data_dir = tmp_path / "data"
    memory_dir = data_dir / "memory"
    import_chatgpt_export(input_path=_write_health_export(tmp_path), data_dir=data_dir, memory_dir=memory_dir)
    return memory_dir


def test_search_applies_high_risk_age_and_authorship_policy_to_real_candidates(tmp_path) -> None:
    result = search_chatgpt_memory(
        memory_dir=_memory_dir(tmp_path),
        query="vitamin D supplement dose",
        limit=10,
        depth="full",
    )

    assert result["governance"]["high_risk"] is True
    assert result["governance"]["policy"] == "conservative_high_risk_v2"
    assert len(result["results"]) == 1
    assert "1000 IU" in result["results"][0]["snippet"]
    assert result["results"][0]["source_role"] == "user"
    assert result["results"][0]["high_risk_decision"]["disposition"] == "allow_fact"
    rejection_reasons = {
        reason for decision in result["governance"]["rejected_evidence"] for reason in decision["reasons"]
    }
    assert "stale_high_risk_evidence" in rejection_reasons
    assert "assistant_only_suggestion" in rejection_reasons


def test_manual_high_trust_memory_cannot_authorize_health_advice(tmp_path) -> None:
    memory_dir = _memory_dir(tmp_path)
    with sqlite_connection(memory_dir / "chatgpt_memory.sqlite3") as connection:
        create_memory_record(
            connection,
            record_type="research_note",
            title="Vitamin D supplement dose note",
            body="A manually curated note mentions a 1000 IU vitamin D supplement dose.",
            trust_level="high",
            source_kind="manual",
            source_ref="manual-health-note",
        )

    result = search_chatgpt_memory(
        memory_dir=memory_dir,
        query="what dose of vitamin D supplement should I take",
        limit=10,
        depth="full",
    )

    assert result["governance"]["advice_requested"] is True
    assert result["governance"]["refuse_unsupported_advice"] is True
    manual = next(item for item in result["results"] if item.get("memory_record_id"))
    assert manual["high_risk_decision"]["supports_advice"] is False


def test_current_official_high_trust_memory_can_support_health_advice(tmp_path) -> None:
    memory_dir = _memory_dir(tmp_path)
    with sqlite_connection(memory_dir / "chatgpt_memory.sqlite3") as connection:
        create_memory_record(
            connection,
            record_type="research_note",
            title="Official vitamin D supplement dose guidance",
            body="Official source guidance for vitamin D supplement dosing and safety.",
            trust_level="high",
            source_kind="official_source",
            source_ref="official-health-guidance",
        )

    result = search_chatgpt_memory(
        memory_dir=memory_dir,
        query="what dose of vitamin D supplement should I take",
        limit=10,
        depth="full",
    )

    official = next(item for item in result["results"] if item.get("memory_record_id"))
    assert official["high_risk_decision"]["supports_advice"] is True
    assert result["governance"]["refuse_unsupported_advice"] is False
