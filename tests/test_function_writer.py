from pathlib import Path

from local_agent_lab.agents.function_writer import (
    build_function_writer_prompt,
    parse_function_writer_response,
    render_function_plan,
)


def test_build_function_writer_prompt_includes_spec_and_context(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    prompt = build_function_writer_prompt(
        repo=repo,
        spec_text="Add a normalize_name helper.",
        retrieved_context=[{"relative_path": "src/names.py", "chunk_index": 0, "snippet": "def slugify(name):"}],
    )
    assert "Add a normalize_name helper." in prompt
    assert "src/names.py" in prompt
    assert '"target_file": "relative/path.py"' in prompt


def test_parse_function_writer_response_accepts_json_fence() -> None:
    response = """```json
{
  "summary": "Adds a helper and tests.",
  "target_file": "src/names.py",
  "implementation": "def normalize_name(value):\\n    return value.strip().title()\\n",
  "test_target_file": "tests/test_names.py",
  "tests": "from src.names import normalize_name\\n",
  "assumptions": ["Input is a string."]
}
```"""
    plan = parse_function_writer_response(response)
    assert plan.target_file == "src/names.py"
    assert "normalize_name" in plan.implementation
    assert plan.assumptions == ["Input is a string."]


def test_render_function_plan_shows_patch_path(tmp_path) -> None:
    plan = parse_function_writer_response(
        """
        {
          "summary": "Adds a helper.",
          "target_file": "src/names.py",
          "implementation": "x",
          "test_target_file": "tests/test_names.py",
          "tests": "y",
          "assumptions": []
        }
        """
    )
    rendered = render_function_plan(plan, Path("/tmp/out.patch"), applied=False)
    assert "Patch file: /tmp/out.patch" in rendered
    assert "Applied: no" in rendered


def test_parse_function_writer_response_accepts_markdown_fallback() -> None:
    response = """
Summary: Adds normalize_name.
Target file: src/names.py
```python
def normalize_name(value):
    return value.strip().title()
```
Test file: tests/test_names.py
```python
def test_normalize_name():
    assert normalize_name(" a ") == "A"
```
Assumptions:
- Input is a string.
"""
    plan = parse_function_writer_response(response)
    assert plan.target_file == "src/names.py"
    assert plan.test_target_file == "tests/test_names.py"
    assert "normalize_name" in plan.tests
