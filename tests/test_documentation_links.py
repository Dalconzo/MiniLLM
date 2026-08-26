from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from urllib.parse import unquote

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_POLICY_SCRIPT = REPO_ROOT / "scripts" / "create_public_release_candidate.py"


def _load_release_policy() -> ModuleType:
    spec = importlib.util.spec_from_file_location("minillm_documentation_release_policy", RELEASE_POLICY_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RELEASE_POLICY = _load_release_policy()


def _public_documents() -> tuple[Path, ...]:
    documents = [REPO_ROOT / "README.md"]
    for document in sorted((REPO_ROOT / "docs").rglob("*.md")):
        relative = document.relative_to(REPO_ROOT).as_posix()
        if RELEASE_POLICY._path_decision(relative) == "include":
            documents.append(document)
    return tuple(documents)


PUBLIC_DOCUMENTS = _public_documents()
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


def _local_targets(document: Path) -> list[Path]:
    targets: list[Path] = []
    text = document.read_text(encoding="utf-8")
    for raw_target in MARKDOWN_LINK_RE.findall(text):
        target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
        if not target or target.startswith(("#", "http://", "https://", "mailto:")):
            continue
        path_text = unquote(target.split("#", 1)[0])
        targets.append((document.parent / path_text).resolve())
    return targets


@pytest.mark.parametrize("document", PUBLIC_DOCUMENTS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_public_document_local_links_resolve(document: Path) -> None:
    missing = [target for target in _local_targets(document) if not target.exists()]
    assert not missing, [str(target.relative_to(REPO_ROOT)) for target in missing]

    excluded: list[str] = []
    for target in _local_targets(document):
        try:
            relative = target.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            excluded.append(str(target))
            continue
        if RELEASE_POLICY._path_decision(relative) != "include":
            excluded.append(relative)
    assert not excluded, excluded
