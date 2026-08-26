from __future__ import annotations

from pathlib import Path


def index_document(path: str | Path) -> dict[str, str]:
    return {"document": str(Path(path).resolve()), "status": "not_implemented"}
