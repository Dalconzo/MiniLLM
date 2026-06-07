from local_agent_lab.tools.git_tools import changed_files_from_diff, repo_root


def test_repo_root_finds_marker(tmp_path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / ".git").mkdir()
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)
    root = repo_root(nested)
    assert root == repo


def test_changed_files_from_diff_extracts_targets() -> None:
    diff_text = """diff --git a/src/app.py b/src/app.py
index 123..456 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,1 +1,2 @@
+print("hi")
diff --git a/tests/test_app.py b/tests/test_app.py
--- a/tests/test_app.py
+++ b/tests/test_app.py
@@ -0,0 +1,1 @@
+assert True
"""
    assert changed_files_from_diff(diff_text) == ["src/app.py", "tests/test_app.py"]
