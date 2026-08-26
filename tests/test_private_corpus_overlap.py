from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "audit_private_corpus_overlap.py"
PRIVATE_WORDS = (
    "amber kestrel measures every ceramic vessel before sunrise because careful records preserve the unusual "
    "fermentation lesson that emerged during the quiet winter kitchen experiment near the window"
)
PUBLIC_WORDS = (
    "silver orchard documentation explains a deterministic demonstration with synthetic evidence explicit "
    "provenance careful abstention conservative routing and repeatable evaluation for every independent reviewer"
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_AUTHOR_NAME": "Fixture Author",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture Author",
        },
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str = "candidate fixture") -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _candidate(tmp_path: Path, content: str = PUBLIC_WORDS) -> Path:
    repo = tmp_path / "public-candidate"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    (repo / "docs").mkdir()
    (repo / "docs" / "case-study.md").write_text(f"# Case study\n\n{content}\n", encoding="utf-8")
    _commit(repo)
    return repo


def _message(role: str, text: str, *, message_id: str) -> dict[str, Any]:
    return {
        "id": message_id,
        "author": {"role": role, "name": "Private Author Name"},
        "content": {"content_type": "text", "parts": [text]},
    }


def _conversation(*messages: tuple[str, str], title: str = "Private Conversation Title") -> dict[str, Any]:
    mapping = {
        f"private-node-{index}": {
            "id": f"private-node-{index}",
            "message": _message(role, text, message_id=f"private-message-{index}"),
        }
        for index, (role, text) in enumerate(messages)
    }
    return {"title": title, "id": "private-conversation-id", "mapping": mapping}


def _export(path: Path, *conversations: dict[str, Any]) -> Path:
    path.write_text(json.dumps(list(conversations)), encoding="utf-8")
    return path


def _run(
    candidate: Path,
    exports: list[Path],
    *,
    report: Path | None = None,
    ngram_words: int | None = None,
    expected_code: int = 0,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(SCRIPT), "--candidate-repo", str(candidate)]
    for export in exports:
        command.extend(("--export", str(export)))
    if report is not None:
        command.extend(("--report", str(report)))
    if ngram_words is not None:
        command.extend(("--ngram-words", str(ngram_words)))
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == expected_code, result.stderr
    return result


def _assert_private_values_absent(value: str, export_path: Path) -> None:
    for private_value in (
        PRIVATE_WORDS,
        str(export_path),
        export_path.name,
        "Private Conversation Title",
        "private-conversation-id",
        "private-message-0",
        "Private Author Name",
    ):
        assert private_value not in value


def test_detects_user_overlap_without_disclosing_private_text(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path, PRIVATE_WORDS)
    export = _export(tmp_path / "private-export.json", _conversation(("user", PRIVATE_WORDS)))
    report = tmp_path / "private-report.json"

    result = _run(candidate, [export], report=report, expected_code=1)
    response = json.loads(result.stdout)
    persisted = report.read_text(encoding="utf-8")

    assert response["status"] == "overlap_detected"
    assert response["candidate"]["commit"] == _git(candidate, "rev-parse", "HEAD")
    assert response["candidate"]["tree"] == _git(candidate, "rev-parse", "HEAD^{tree}")
    assert response["counts"]["matched_spans"] == 1
    assert response["matches"][0]["candidate_path"] == "docs/case-study.md"
    assert response["matches"][0]["start_line"] == 3
    assert len(response["matches"][0]["span_fingerprint"]) == 64
    assert stat.S_IMODE(report.stat().st_mode) == 0o600
    assert report.stat().st_nlink == 1
    _assert_private_values_absent(result.stdout + result.stderr + persisted, export)


def test_zero_match_passes_and_reports_only_counts_and_hashes(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    export = _export(tmp_path / "private-export.json", _conversation(("user", PRIVATE_WORDS)))

    result = _run(candidate, [export])
    response = json.loads(result.stdout)

    assert response["status"] == "passed"
    assert response["counts"]["matched_spans"] == 0
    assert response["matches"] == []
    assert len(response["export_inputs"][0]["sha256"]) == 64
    _assert_private_values_absent(result.stdout + result.stderr, export)


def test_split_conversation_files_are_combined_without_persisting_parsed_data(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path, PRIVATE_WORDS)
    export_dir = tmp_path / "private-split-export"
    export_dir.mkdir()
    _export(export_dir / "conversations-001.json", _conversation(("user", "short private note")))
    _export(export_dir / "conversations-002.json", _conversation(("user", PRIVATE_WORDS)))
    (export_dir / "unrelated.json").write_text("not conversation JSON", encoding="utf-8")

    result = _run(candidate, [export_dir], expected_code=1)
    response = json.loads(result.stdout)

    assert response["counts"]["conversation_documents"] == 2
    assert response["counts"]["conversations"] == 2
    assert not list(export_dir.rglob("*.db"))
    assert not list(export_dir.rglob("*.jsonl"))
    _assert_private_values_absent(result.stdout + result.stderr, export_dir)


def test_multiple_export_inputs_are_audited_together(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path, PRIVATE_WORDS)
    first = _export(
        tmp_path / "private-export-one.json",
        _conversation(("assistant", PRIVATE_WORDS), ("user", "short first request")),
    )
    second = _export(
        tmp_path / "private-export-two.json",
        _conversation(("user", PRIVATE_WORDS)),
    )

    result = _run(candidate, [first, second], expected_code=1)
    response = json.loads(result.stdout)

    assert response["counts"]["export_inputs"] == 2
    assert len(response["export_inputs"]) == 2
    assert response["counts"]["matched_spans"] == 1
    _assert_private_values_absent(result.stdout + result.stderr, first)
    assert str(second) not in result.stdout + result.stderr
    assert second.name not in result.stdout + result.stderr


def test_assistant_only_overlap_does_not_match(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path, PRIVATE_WORDS)
    export = _export(
        tmp_path / "private-export.json",
        _conversation(("assistant", PRIVATE_WORDS), ("user", "a short user request")),
    )

    result = _run(candidate, [export])
    response = json.loads(result.stdout)

    assert response["status"] == "passed"
    assert response["counts"]["user_messages"] == 1
    assert response["counts"]["eligible_user_messages"] == 0
    assert response["counts"]["matched_spans"] == 0


def test_zip_export_is_read_in_memory(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path, PRIVATE_WORDS)
    export = tmp_path / "private-export.zip"
    payload = json.dumps([_conversation(("user", PRIVATE_WORDS))])
    with zipfile.ZipFile(export, "w") as archive:
        archive.writestr("account/conversations-001.json", payload)

    result = _run(candidate, [export], expected_code=1)
    response = json.loads(result.stdout)

    assert response["counts"]["conversation_documents"] == 1
    assert response["counts"]["matched_spans"] == 1
    _assert_private_values_absent(result.stdout + result.stderr, export)


def test_malformed_private_export_fails_without_echoing_input(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    export = tmp_path / "private-export-secret-name.json"
    export.write_bytes((PRIVATE_WORDS + "\n").encode() + b"\xff")

    result = _run(candidate, [export], expected_code=1)

    assert json.loads(result.stderr) == {"error": "export_json_invalid", "status": "error"}
    _assert_private_values_absent(result.stdout + result.stderr, export)


def test_export_symlink_is_rejected_without_path_disclosure(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    target = _export(tmp_path / "private-target.json", _conversation(("user", PRIVATE_WORDS)))
    export = tmp_path / "private-export-link.json"
    export.symlink_to(target)

    result = _run(candidate, [export], expected_code=1)

    assert json.loads(result.stderr) == {"error": "export_symlink_rejected", "status": "error"}
    _assert_private_values_absent(result.stdout + result.stderr, export)


def test_tracked_candidate_symlink_is_rejected(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    (candidate / "docs" / "link.md").symlink_to("case-study.md")
    _commit(candidate, "add tracked symlink")
    export = _export(tmp_path / "private-export.json", _conversation(("user", PRIVATE_WORDS)))

    result = _run(candidate, [export], expected_code=1)

    assert json.loads(result.stderr) == {
        "error": "candidate_non_regular_tracked_entry",
        "status": "error",
    }
    _assert_private_values_absent(result.stdout + result.stderr, export)


def test_candidate_repository_symlink_is_rejected(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    candidate_link = tmp_path / "public-candidate-link"
    candidate_link.symlink_to(candidate, target_is_directory=True)
    export = _export(tmp_path / "private-export.json", _conversation(("user", PRIVATE_WORDS)))

    result = _run(candidate_link, [export], expected_code=1)

    assert json.loads(result.stderr) == {"error": "candidate_symlink_rejected", "status": "error"}
    _assert_private_values_absent(result.stdout + result.stderr, export)


def test_dirty_candidate_is_rejected(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    (candidate / "docs" / "case-study.md").write_text("dirty candidate\n", encoding="utf-8")
    export = _export(tmp_path / "private-export.json", _conversation(("user", PRIVATE_WORDS)))

    result = _run(candidate, [export], expected_code=1)

    assert json.loads(result.stderr) == {"error": "candidate_dirty", "status": "error"}
    _assert_private_values_absent(result.stdout + result.stderr, export)


def test_private_report_cannot_be_written_inside_candidate(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    export = _export(tmp_path / "private-export.json", _conversation(("user", PRIVATE_WORDS)))

    result = _run(candidate, [export], report=candidate / "audit.json", expected_code=1)

    assert json.loads(result.stderr) == {
        "error": "report_destination_inside_candidate",
        "status": "error",
    }
    assert not (candidate / "audit.json").exists()
    _assert_private_values_absent(result.stdout + result.stderr, export)


def test_unreachable_or_historical_git_blob_content_is_scanned(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path, PRIVATE_WORDS)
    (candidate / "docs" / "case-study.md").write_text(PUBLIC_WORDS, encoding="utf-8")
    _commit(candidate, "remove private overlap from current tree")
    export = _export(tmp_path / "private-export.json", _conversation(("user", PRIVATE_WORDS)))

    result = _run(candidate, [export], expected_code=1)
    response = json.loads(result.stdout)

    assert response["status"] == "overlap_detected"
    assert any(match["candidate_path"] is None for match in response["matches"])
    assert response["counts"]["git_blob_objects_scanned"] >= 2


def test_encoded_traversal_shaped_git_path_is_rejected(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    unsafe = candidate / "docs" / "%2e%2e" / "private.md"
    unsafe.parent.mkdir()
    unsafe.write_text(PUBLIC_WORDS, encoding="utf-8")
    _commit(candidate, "add traversal-shaped path")
    export = _export(tmp_path / "private-export.json", _conversation(("user", PRIVATE_WORDS)))

    result = _run(candidate, [export], expected_code=1)

    assert json.loads(result.stderr) == {"error": "candidate_unsafe_git_path", "status": "error"}


@pytest.mark.parametrize("ngram_words", [19, 41])
def test_ngram_configuration_is_bounded_conservatively(tmp_path: Path, ngram_words: int) -> None:
    candidate = _candidate(tmp_path)
    export = _export(tmp_path / "private-export.json", _conversation(("user", PRIVATE_WORDS)))

    result = _run(candidate, [export], ngram_words=ngram_words, expected_code=1)

    assert json.loads(result.stderr) == {"error": "ngram_words_out_of_bounds", "status": "error"}
