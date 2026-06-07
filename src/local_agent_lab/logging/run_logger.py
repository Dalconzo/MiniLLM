from __future__ import annotations

import json
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
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.events_file = self.logs_dir / "events.jsonl"

    def start(self, command: str, payload: dict) -> RunContext:
        started_at = datetime.now(timezone.utc).isoformat()
        run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
        run_dir = self.logs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
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
        (run.run_dir / name).write_text(content, encoding="utf-8")

    def _append(self, payload: dict) -> None:
        with self.events_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
