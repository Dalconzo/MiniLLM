from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def test_third_party_actions_are_pinned_to_immutable_revisions() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    uses = re.findall(r"^\s*- uses:\s*([^\s#]+)", source, flags=re.MULTILINE)

    assert uses
    for reference in uses:
        assert re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", reference), reference


def test_ci_audits_public_surface_and_locked_runtime_dependencies() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")

    assert "scripts/create_public_release_candidate.py" in source
    assert "scripts/audit_public_artifacts.py" in source
    assert "--forbidden-literals-file" in source
    assert "uv export --frozen --no-dev --no-emit-project" in source
    assert "--no-hashes" not in source
    assert "uv run --frozen pip-audit --disable-pip --requirement" in source
