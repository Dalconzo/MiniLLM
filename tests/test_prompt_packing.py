from local_agent_lab.agents.base import render_prompt
from local_agent_lab.tools.file_tools import redact_text


def test_render_prompt_includes_task_and_question() -> None:
    prompt = render_prompt("chat", "hello")
    assert "Task: chat" in prompt
    assert "hello" in prompt


def test_redact_text_masks_secrets() -> None:
    text = "password=abc123\napi_key: secret\n-----BEGIN PRIVATE KEY-----\nxyz\n-----END PRIVATE KEY-----"
    redacted = redact_text(text)
    assert "abc123" not in redacted
    assert "secret" not in redacted
    assert "[REDACTED_PRIVATE_KEY]" in redacted
