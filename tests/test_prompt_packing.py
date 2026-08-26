import json
import subprocess

from click import unstyle
import pytest
from typer.testing import CliRunner

from local_agent_lab.agents.base import render_prompt
from local_agent_lab.cli import _read_authorized_prompt_text, app
from local_agent_lab.indexing.visibility import VisibilityPolicyError
from local_agent_lab.llm.ollama_client import OllamaClient
from local_agent_lab.logging.run_logger import RunLogger, sanitize_payload_for_persistence
from local_agent_lab.security.paths import PathPolicyError
from local_agent_lab.tools.file_tools import redact_text


def test_render_prompt_includes_task_and_question() -> None:
    prompt = render_prompt("chat", "hello")
    assert "Task: chat" in prompt
    assert "hello" in prompt


def test_redact_text_masks_secrets() -> None:
    private_key = "-----BEGIN " + "PRIVATE KEY-----\nxyz\n-----END " + "PRIVATE KEY-----"
    text = f"password=abc123\napi_key: secret\n{private_key}"
    redacted = redact_text(text)
    assert "abc123" not in redacted
    assert "secret" not in redacted
    assert "[REDACTED_PRIVATE_KEY]" in redacted


def test_redact_text_masks_structured_credentials_and_authorization_headers() -> None:
    bearer_secret = "signed-" + "secret-value"
    source = (
        '{"password": "correct horse battery staple", '
        '"access_token": "access-secret-123", '
        f'"authorization": "Bearer {bearer_secret}"}}'
    )

    redacted = redact_text(source)

    assert "correct horse battery staple" not in redacted
    assert "access-secret-123" not in redacted
    assert "signed-secret-value" not in redacted
    assert redacted.count("[REDACTED]") == 3


def test_redact_text_removes_line_credentials_bearer_headers_and_url_userinfo() -> None:
    private_key = "-----BEGIN " + "PRIVATE KEY-----\nprivate-material\n-----END " + "PRIVATE KEY-----"
    source = "\n".join(
        [
            "Authorization: Bearer signed secret with spaces",
            "Bearer standalone secret phrase",
            "password=correct horse battery staple",
            "token=first second third; mode=safe",
            "postgresql://db-user:db password@example.test/private",
            private_key,
        ]
    )

    redacted = redact_text(source)

    for secret in (
        "signed secret with spaces",
        "standalone secret phrase",
        "correct horse battery staple",
        "first second third",
        "db-user:db password",
        "private-material",
    ):
        assert secret not in redacted
    assert "mode=safe" in redacted
    assert "Authorization: [REDACTED]" in redacted
    assert "postgresql://[REDACTED]@example.test/private" in redacted


def test_ollama_final_egress_redacts_prompt_system_and_embedding(monkeypatch) -> None:
    client = OllamaClient("http://127.0.0.1:11434")
    payloads = []

    def fake_post(path, payload):
        payloads.append((path, payload))
        if path == "/api/generate":
            return {"response": "ok"}
        return {"embeddings": [[0.1, 0.2]]}

    monkeypatch.setattr(client, "_post", fake_post)
    secret = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"

    client.generate(
        model="test",
        prompt=f"Use {secret}",
        system=f"token={secret}",
        temperature=0.0,
    )
    client.embed(model="test", text=f"Remember {secret}")

    serialized = repr(payloads)
    assert secret not in serialized
    assert "REDACTED" in serialized


def test_run_logger_defaults_to_metadata_only_and_redacts_json(tmp_path) -> None:
    logger = RunLogger(tmp_path / "logs")
    run = logger.start("ask", {"question": "private question", "token": "token=secret-value"})
    logger.write_artifact(run, "prompt.md", "private prompt password=hunter2")
    logger.write_artifact(
        run,
        "result.json",
        '{"status":"ok","response":"private answer","token":"sk-' + 'abcdefghijklmnopqrstuvwxyz123456"}',
    )
    logger.finish(run, status="ok", result={"response": "private answer"})

    prompt_artifact = (run.run_dir / "prompt.md").read_text(encoding="utf-8")
    result_artifact = (run.run_dir / "result.json").read_text(encoding="utf-8")
    events = (logger.logs_dir / "events.jsonl").read_text(encoding="utf-8")
    combined = prompt_artifact + result_artifact + events
    assert "private prompt" not in combined
    assert "private question" not in combined
    assert "private answer" not in combined
    assert "hunter2" not in combined
    assert "sk-" + "abcdefghijklmnopqrstuvwxyz123456" not in combined
    assert "detailed_content_omitted" in combined
    assert all(
        event["schema_version"] == "minillm_diagnostics_v1"
        for event in (json.loads(line) for line in events.splitlines())
    )


def test_sanitized_debug_mode_still_redacts_secrets(tmp_path) -> None:
    logger = RunLogger(tmp_path / "logs", allow_detailed_content=True)
    run = logger.start("ask", {"status": "debug"})
    logger.write_artifact(run, "prompt.md", "safe detail token=secret-value")

    content = (run.run_dir / "prompt.md").read_text(encoding="utf-8")
    assert "safe detail" in content
    assert "secret-value" not in content
    assert "token=[REDACTED]" in content


def test_trace_payload_sanitizer_omits_retrieved_private_text() -> None:
    sanitized = sanitize_payload_for_persistence(
        {"stage": "rank_results", "details": {"snippet": "private memory", "score": 0.9}}
    )

    assert sanitized["details"]["snippet"]["detailed_content_omitted"] is True
    assert sanitized["details"]["score"] == 0.9


def test_payload_sanitizer_omits_structured_credential_fields() -> None:
    bearer_secret = "signed-" + "secret-value"
    sanitized = sanitize_payload_for_persistence(
        {
            "headers": {"authorization": f"Bearer {bearer_secret}"},
            "access_token": "access-secret-123",
            "safe_count": 2,
        }
    )

    serialized = json.dumps(sanitized)
    assert "signed-secret-value" not in serialized
    assert "access-secret-123" not in serialized
    assert sanitized["safe_count"] == 2


def test_metadata_only_sanitizer_omits_private_domain_prose_and_result_bodies() -> None:
    private_values = {
        "claim": "private memory claim",
        "title": "private conversation title",
        "notes": "private review notes",
        "candidate_text": "private candidate text",
        "observed_behavior": "private feedback observation",
        "ingredients": ["private ingredient"],
        "steps": ["private recipe step"],
        "result": {"body": "private result body"},
    }

    sanitized = sanitize_payload_for_persistence(
        {
            "stage": "rank_results",
            "run_id": "run-safe-id",
            "details": {**private_values, "returned": 7, "ranking_profile": "hybrid_memory_v1"},
        }
    )
    serialized = json.dumps(sanitized)

    for private_value in (
        "private memory claim",
        "private conversation title",
        "private review notes",
        "private candidate text",
        "private feedback observation",
        "private ingredient",
        "private recipe step",
        "private result body",
    ):
        assert private_value not in serialized
    assert sanitized["stage"] == "rank_results"
    assert sanitized["run_id"] == "run-safe-id"
    assert sanitized["details"]["returned"] == 7
    assert sanitized["details"]["ranking_profile"] == "hybrid_memory_v1"


def test_run_logger_rejects_final_event_log_symlink_swap(tmp_path) -> None:
    logger = RunLogger(tmp_path / "logs")
    outside = tmp_path / "outside.jsonl"
    outside.write_text("unchanged\n", encoding="utf-8")
    logger.events_file.unlink()
    logger.events_file.symlink_to(outside)

    with pytest.raises(PathPolicyError, match="symlink"):
        logger.start("ask", {"question": "must not escape"})

    assert outside.read_text(encoding="utf-8") == "unchanged\n"


def test_run_logger_rejects_preexisting_logs_directory_symlink(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    logs_link = tmp_path / "logs"
    logs_link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="must not be a symlink"):
        RunLogger(logs_link)


def test_write_function_spec_input_must_be_visible_inside_selected_repo(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("private-spec.md\n", encoding="utf-8")
    ignored = repo / "private-spec.md"
    ignored.write_text("Private product strategy with no credential patterns.\n", encoding="utf-8")

    with pytest.raises(VisibilityPolicyError, match="gitignored"):
        _read_authorized_prompt_text(ignored, repo=repo)


def test_prompt_input_outside_selected_repo_fails_closed(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("private prose\n", encoding="utf-8")

    with pytest.raises(VisibilityPolicyError, match="outside the authorized root"):
        _read_authorized_prompt_text(outside, repo=repo)


def test_explain_log_accepts_only_explicit_safe_log_root_for_external_file(tmp_path) -> None:
    repo = tmp_path / "repo"
    log_root = tmp_path / "safe-logs"
    repo.mkdir()
    log_root.mkdir()
    log_file = log_root / "error.log"
    log_file.write_text("RuntimeError: expected test failure\n", encoding="utf-8")

    with pytest.raises(VisibilityPolicyError, match="outside the authorized root"):
        _read_authorized_prompt_text(log_file, repo=repo)

    content, label = _read_authorized_prompt_text(log_file, repo=repo, safe_root=log_root)
    assert content == "RuntimeError: expected test failure\n"
    assert label == "error.log"


def test_repo_ignored_prompt_input_cannot_bypass_policy_via_safe_log_root(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
    ignored = repo / "ignored.log"
    ignored.write_text("private non-secret incident details\n", encoding="utf-8")

    with pytest.raises(VisibilityPolicyError, match="gitignored"):
        _read_authorized_prompt_text(ignored, repo=repo, safe_root=repo)


def test_prompt_input_rejects_symlink_even_within_safe_log_root(tmp_path) -> None:
    log_root = tmp_path / "safe-logs"
    log_root.mkdir()
    outside = tmp_path / "outside.log"
    outside.write_text("private\n", encoding="utf-8")
    linked = log_root / "linked.log"
    linked.symlink_to(outside)

    with pytest.raises(VisibilityPolicyError):
        _read_authorized_prompt_text(linked, safe_root=log_root)


def test_generation_and_log_cli_help_require_explicit_boundaries() -> None:
    runner = CliRunner()

    write_help = runner.invoke(app, ["write-function", "--help"])
    test_help = runner.invoke(app, ["write-tests", "--help"])
    log_help = runner.invoke(app, ["explain-log", "--help"])

    assert write_help.exit_code == 0
    assert "--target" in unstyle(write_help.stdout)
    assert "--test-target" in unstyle(write_help.stdout)
    assert test_help.exit_code == 0
    assert "--test-target" in unstyle(test_help.stdout)
    assert log_help.exit_code == 0
    assert "--log-root" in unstyle(log_help.stdout)
