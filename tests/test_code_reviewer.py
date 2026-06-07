from local_agent_lab.agents.code_reviewer import (
    build_diff_review_prompt,
    build_file_review_prompt,
    parse_review_response,
    render_review_output,
)


def test_build_file_review_prompt_includes_line_numbers_and_context(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    prompt = build_file_review_prompt(
        repo=repo,
        relative_path="src/app.py",
        file_content="def run():\n    return 1\n",
        retrieved_context=[{"relative_path": "tests/test_app.py", "chunk_index": 0, "snippet": "assert run() == 1"}],
    )
    assert "Target file: src/app.py" in prompt
    assert "   1: def run():" in prompt
    assert "tests/test_app.py" in prompt


def test_build_diff_review_prompt_includes_diff_and_context(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    prompt = build_diff_review_prompt(
        repo=repo,
        diff_text="diff --git a/a.py b/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-print(1)\n+print(2)\n",
        retrieved_context=[{"relative_path": "a.py", "chunk_index": 0, "snippet": "print(1)"}],
    )
    assert "Unified diff:" in prompt
    assert "+++ b/a.py" in prompt
    assert "Retrieved context:" in prompt


def test_parse_review_response_from_markdown() -> None:
    response = """
## Summary
Looks mostly safe, but one branch now returns the wrong type.

## Likely bugs
- src/app.py:14 - The function now returns None on the empty-input path. | fix: restore the previous empty list default | test: add an empty-input regression test

## Missing tests
- tests/test_app.py:1 - No test covers the empty-input branch.
"""
    result = parse_review_response(response)
    assert result.summary.startswith("Looks mostly safe")
    assert len(result.findings) == 2
    assert result.findings[0].file == "src/app.py"
    assert result.findings[0].line == 14
    assert result.findings[0].suggested_fix == "restore the previous empty list default"


def test_render_review_output_formats_findings() -> None:
    response = """
## Summary
One issue found.

## Likely bugs
- src/app.py:7 - The new condition drops negative values unexpectedly.
"""
    rendered = render_review_output(parse_review_response(response))
    assert "[high] src/app.py:7" in rendered
    assert "Summary:" in rendered
