from pathlib import Path

from local_agent_lab.agents.log_analyzer import (
    build_log_analysis_prompt,
    parse_log_analysis_response,
    parse_log_text,
    render_log_analysis,
)


SAMPLE_TRACEBACK = """Traceback (most recent call last):
  File "/tmp/demo/src/app.py", line 10, in <module>
    main()
  File "/tmp/demo/src/app.py", line 6, in main
    return load_user(config["name"])
  File "/tmp/demo/src/users.py", line 14, in load_user
    return users[name]
KeyError: 'alice'
"""


def test_parse_log_text_extracts_frames_and_exception() -> None:
    parsed = parse_log_text(SAMPLE_TRACEBACK)
    assert len(parsed.frames) == 3
    assert parsed.frames[-1].file == "/tmp/demo/src/users.py"
    assert parsed.frames[-1].line == 14
    assert parsed.error_type == "KeyError"
    assert parsed.error_message == "'alice'"


def test_build_log_analysis_prompt_includes_parsed_traceback_and_context(tmp_path) -> None:
    log_file = tmp_path / "error.log"
    log_file.write_text(SAMPLE_TRACEBACK, encoding="utf-8")
    parsed = parse_log_text(SAMPLE_TRACEBACK)
    prompt = build_log_analysis_prompt(
        log_file=log_file,
        log_text=SAMPLE_TRACEBACK,
        parsed_log=parsed,
        retrieved_context=[{"relative_path": "src/users.py", "chunk_index": 0, "snippet": "users = {}"}],
        repo=tmp_path,
    )
    assert "KeyError" in prompt
    assert "src/users.py" in prompt
    assert '"likely_failure_point"' in prompt


def test_parse_log_analysis_response_accepts_json() -> None:
    response = """```json
{
  "summary": "The lookup fails for a missing user.",
  "likely_failure_point": "src/users.py:14",
  "probable_cause": "The code indexes users[name] without handling unknown names.",
  "next_steps": ["Inspect the config name value.", "Guard missing users."]
}
```"""
    analysis = parse_log_analysis_response(response)
    assert analysis.likely_failure_point == "src/users.py:14"
    assert analysis.next_steps[0] == "Inspect the config name value."


def test_parse_log_analysis_response_accepts_markdown() -> None:
    response = """
## Summary
The crash happens during a direct dictionary lookup.

## Likely failure point
src/users.py:14 in load_user

## Probable cause
The user map does not contain the requested key.

## Next steps
- Check where the requested username comes from.
- Add a fallback or explicit error for missing users.
"""
    analysis = parse_log_analysis_response(response)
    assert "src/users.py:14" in analysis.likely_failure_point
    assert len(analysis.next_steps) == 2


def test_render_log_analysis_formats_sections() -> None:
    analysis = parse_log_analysis_response(
        """
        {
          "summary": "Short summary.",
          "likely_failure_point": "src/app.py:6",
          "probable_cause": "Missing config field.",
          "next_steps": ["Inspect config parsing."]
        }
        """
    )
    rendered = render_log_analysis(analysis)
    assert "Failure point: src/app.py:6" in rendered
    assert "Next steps:" in rendered
