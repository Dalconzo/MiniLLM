from pathlib import Path

from local_agent_lab.agents.test_writer import (
    build_test_writer_prompt,
    parse_test_writer_response,
    render_test_plan,
)


def test_build_test_writer_prompt_includes_target_and_context(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    prompt = build_test_writer_prompt(
        repo=repo,
        target_file="src/names.py",
        file_content="def normalize_name(value):\n    return value.strip()\n",
        retrieved_context=[{"relative_path": "tests/test_existing.py", "chunk_index": 0, "snippet": "def test_slugify():"}],
    )
    assert "Target file: src/names.py" in prompt
    assert "test_existing.py" in prompt
    assert '"edge_cases": ["item"]' in prompt


def test_parse_test_writer_response_accepts_plain_json() -> None:
    response = """
    {
      "summary": "Adds focused tests.",
      "target_file": "src/names.py",
      "test_target_file": "tests/test_names.py",
      "tests": "def test_normalize_name():\\n    pass\\n",
      "edge_cases": ["empty input"],
      "assumptions": ["pytest is available"]
    }
    """
    plan = parse_test_writer_response(response)
    assert plan.test_target_file == "tests/test_names.py"
    assert plan.edge_cases == ["empty input"]


def test_render_test_plan_lists_edge_cases(tmp_path) -> None:
    plan = parse_test_writer_response(
        """
        {
          "summary": "Adds tests.",
          "target_file": "src/names.py",
          "test_target_file": "tests/test_names.py",
          "tests": "x",
          "edge_cases": ["blank input"],
          "assumptions": []
        }
        """
    )
    rendered = render_test_plan(plan, Path("/tmp/tests.patch"), applied=True)
    assert "Patch file: /tmp/tests.patch" in rendered
    assert "- blank input" in rendered
    assert "Applied: yes" in rendered


def test_parse_test_writer_response_accepts_markdown_fallback() -> None:
    response = """
Summary: Adds tests for clamp.
Target file: src/math_utils.py
Test file: tests/test_math_utils.py
```python
def test_clamp_in_range():
    assert clamp(5, 0, 10) == 5
```
Edge cases:
- below lower bound
- above upper bound
Assumptions:
- pytest is available.
"""
    plan = parse_test_writer_response(response)
    assert plan.test_target_file == "tests/test_math_utils.py"
    assert "test_clamp_in_range" in plan.tests
    assert plan.edge_cases == ["below lower bound", "above upper bound"]


def test_parse_test_writer_response_accepts_triple_quoted_json_strings() -> None:
    response = """```json
{
  "summary": "Adds tests.",
  "target_file": "src/math_utils.py",
  "test_target_file": "tests/test_math_utils.py",
  "tests": \"\"\"
def test_clamp():
    assert clamp(1, 0, 2) == 1
\"\"\",
  "edge_cases": ["in range"],
  "assumptions": []
}
```"""
    plan = parse_test_writer_response(response)
    assert "def test_clamp()" in plan.tests
