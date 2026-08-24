from local_agent_lab.tools.patches import PatchFile, apply_files, build_unified_patch


def test_build_unified_patch_handles_new_file(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    patch = build_unified_patch(repo, [PatchFile(relative_path="src/app.py", content="print('hi')\n")])
    assert "--- a/src/app.py" in patch
    assert "+++ b/src/app.py" in patch
    assert "+print('hi')" in patch


def test_apply_files_writes_content(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    written = apply_files(repo, [PatchFile(relative_path="tests/test_app.py", content="def test_x():\n    pass\n")])
    assert written == ["tests/test_app.py"]
    assert (repo / "tests/test_app.py").read_text(encoding="utf-8").startswith("def test_x")
