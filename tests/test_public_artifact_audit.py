from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_public_artifacts.py"


def _load_auditor() -> ModuleType:
    spec = importlib.util.spec_from_file_location("minillm_public_artifact_auditor", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


AUDITOR = _load_auditor()


def _bundle(tmp_path: Path) -> tuple[Path, Path]:
    candidate = tmp_path / "public-candidate"
    candidate.mkdir()
    (candidate / "README.md").write_text("# Portable public candidate\n", encoding="utf-8")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "full-tests.json").write_text(
        json.dumps({"command": ["uv", "run", "pytest"], "tests_passed": 718}),
        encoding="utf-8",
    )
    return candidate, evidence


def test_complete_candidate_and_evidence_bundle_pass(tmp_path: Path) -> None:
    candidate, evidence = _bundle(tmp_path)
    canaries = tmp_path / "private-overlap-canaries.txt"
    canaries.write_text("private corpus overlap marker\n", encoding="utf-8")

    report = AUDITOR.audit([candidate, evidence], [canaries])

    assert report["status"] == "pass"
    assert report["files_and_objects_scanned"] == 2
    assert report["private_overlap_canary_count"] == 1


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("/" + "Users/reviewer/project", "local_user_path"),
        ("/private/" + "tmp", "machine_temporary_path"),
        ("/private/" + "tmp/evidence.json", "machine_temporary_path"),
        ("lagent-" + "204", "internal_tracker_id"),
        ("20260815" + "T043029Z-" + "f2324ef3", "private_run_id"),
        ("ctx_" + "0123456789abcdef", "private_acceptance_id"),
        ("projects:" + "_acceptance" + "_tests/" + "results/report.md", "private_acceptance_path"),
        ("sk-" + "proj-abcdefghijklmnop", "api_token"),
        ("AKIA" + "ABCDEFGHIJKLMNOP", "aws_access_key"),
        ("xoxb-" + "abcdefghijklmnop", "slack_token"),
    ],
)
def test_bundle_scan_rejects_nonportable_or_sensitive_content(tmp_path: Path, value: str, code: str) -> None:
    candidate, evidence = _bundle(tmp_path)
    (evidence / "unsafe.json").write_text(value, encoding="utf-8")

    with pytest.raises(AUDITOR.PublicArtifactAuditError, match=code):
        AUDITOR.audit([candidate, evidence], [])


def test_bundle_scan_rejects_private_corpus_overlap(tmp_path: Path) -> None:
    candidate, evidence = _bundle(tmp_path)
    canaries = tmp_path / "private-overlap-canaries.txt"
    canaries.write_text("private corpus overlap marker\n", encoding="utf-8")
    (candidate / "leak.txt").write_text("PRIVATE CORPUS OVERLAP MARKER", encoding="utf-8")

    with pytest.raises(AUDITOR.PublicArtifactAuditError, match="private_corpus_overlap"):
        AUDITOR.audit([candidate, evidence], [canaries])
