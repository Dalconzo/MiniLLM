from __future__ import annotations

import json
import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True)
class RunContext:
    run_id: str
    run_dir: Path
    started_at: str


class RunLogger:
    def __init__(self, logs_dir: Path) -> None:
        self.logs_dir = logs_dir
        self.logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.logs_dir, 0o700)
        self.events_file = self.logs_dir / "events.jsonl"
        self.events_file.touch(exist_ok=True, mode=0o600)
        os.chmod(self.events_file, 0o600)

    def start(self, command: str, payload: dict) -> RunContext:
        started_at = datetime.now(timezone.utc).isoformat()
        run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
        run_dir = self.logs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(run_dir, 0o700)
        self._append(
            {
                "event": "start",
                "run_id": run_id,
                "command": command,
                "payload": payload,
                "started_at": started_at,
            }
        )
        return RunContext(run_id=run_id, run_dir=run_dir, started_at=started_at)

    def finish(self, run: RunContext, *, status: str, result: dict) -> None:
        self._append(
            {
                "event": "finish",
                "run_id": run.run_id,
                "status": status,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "result": result,
            }
        )

    def write_artifact(self, run: RunContext, name: str, content: str) -> None:
        artifact_path = run.run_dir / name
        artifact_path.write_text(content, encoding="utf-8")
        os.chmod(artifact_path, 0o600)

    def _append(self, payload: dict) -> None:
        with self.events_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_redact_shared_event_payload(payload)) + "\n")


def _redact_shared_event_payload(value):
    """Keep private text in per-run 0600 artifacts, not the shared event index."""
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if key in {"body", "content", "full_text", "snippet"} and isinstance(item, str):
                redacted[key] = _text_fingerprint(item)
            elif key == "text" and isinstance(item, str):
                try:
                    decoded = json.loads(item)
                except (json.JSONDecodeError, TypeError):
                    redacted[key] = item
                else:
                    redacted[key] = json.dumps(_redact_shared_event_payload(decoded), sort_keys=True)
            else:
                redacted[key] = _redact_shared_event_payload(item)
        return redacted
    if isinstance(value, list):
        return [_redact_shared_event_payload(item) for item in value]
    return value


def _text_fingerprint(text: str) -> dict[str, object]:
    return {
        "redacted": True,
        "character_count": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
