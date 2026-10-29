from __future__ import annotations

import shutil
import stat
import tomllib
from pathlib import Path

import pytest

import local_agent_lab.config as config_module
from local_agent_lab import __version__
from local_agent_lab.config import load_config


ROOT = Path(__file__).resolve().parents[1]


def test_python_and_tooling_target_the_advertised_311_floor() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["requires-python"] == ">=3.11,<3.15"
    assert metadata["tool"]["ruff"]["target-version"] == "py311"
    assert metadata["tool"]["mypy"]["python_version"] == "3.11"
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert lock["version"] == 1
    assert lock["requires-python"].replace(" ", "") == metadata["project"]["requires-python"]


def test_wheel_declares_default_config_and_prompt_data() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    data_files = metadata["tool"]["setuptools"]["data-files"]

    assert metadata["project"]["name"] == "minillm"
    assert metadata["project"]["version"].endswith("a1")
    assert __version__ == metadata["project"]["version"]
    assert data_files["share/minillm/config"] == ["config/agent.yaml"]
    assert data_files["share/minillm/config/prompts"] == ["config/prompts/*.md"]


def test_mac_bootstrap_uses_the_frozen_environment_and_public_demo() -> None:
    script = (ROOT / "scripts" / "bootstrap_mac.sh").read_text(encoding="utf-8")

    assert "uv sync --frozen --all-groups" in script
    assert "uv run --frozen python examples/public_demo/run_demo.py --json" in script


def test_primary_verification_is_uv_locked_and_ci_reuses_the_security_subset() -> None:
    verify_path = ROOT / "scripts" / "verify.sh"
    security_path = ROOT / "scripts" / "run_security_tests.sh"
    verify = verify_path.read_text(encoding="utf-8")
    security = security_path.read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert stat.S_IMODE(verify_path.stat().st_mode) & stat.S_IXUSR
    assert stat.S_IMODE(security_path.stat().st_mode) & stat.S_IXUSR
    assert "uv lock --check" in verify
    assert "uv sync --frozen --all-groups" in verify
    assert "./scripts/run_security_tests.sh" in verify
    assert "uv run --frozen pytest -q" in verify
    assert "uv run --frozen pytest -q" in security
    assert "./scripts/run_security_tests.sh" in ci


def test_installed_config_fallback_uses_packaged_prompts_and_runtime_home(tmp_path, monkeypatch) -> None:
    installed_config_dir = tmp_path / "prefix" / "share" / "minillm" / "config"
    installed_config_dir.mkdir(parents=True)
    shutil.copy2(ROOT / "config" / "agent.yaml", installed_config_dir / "agent.yaml")
    shutil.copytree(ROOT / "config" / "prompts", installed_config_dir / "prompts")
    runtime_home = tmp_path / "runtime"

    monkeypatch.setattr(config_module, "SOURCE_CONFIG_PATH", tmp_path / "missing" / "agent.yaml")
    monkeypatch.setattr(config_module, "INSTALLED_CONFIG_DIR", installed_config_dir)
    monkeypatch.setattr(config_module, "INSTALLED_CONFIG_PATH", installed_config_dir / "agent.yaml")
    monkeypatch.setenv("LAGENT_HOME", str(runtime_home))
    monkeypatch.delenv("LAGENT_CONFIG", raising=False)

    config = load_config()

    assert config.path == (installed_config_dir / "agent.yaml").resolve()
    assert config.root_dir == runtime_home.resolve()
    assert config.prompt_path("chat").read_text(encoding="utf-8").startswith("You are a practical local assistant")
    assert config.logs_dir == runtime_home / "data" / "logs"


def test_explicit_config_keeps_adjacent_prompts_and_supports_runtime_override(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "custom" / "config"
    config_dir.mkdir(parents=True)
    shutil.copy2(ROOT / "config" / "agent.yaml", config_dir / "agent.yaml")
    shutil.copytree(ROOT / "config" / "prompts", config_dir / "prompts")
    runtime_home = tmp_path / "runtime"
    monkeypatch.setenv("LAGENT_HOME", str(runtime_home))

    config = load_config(config_dir / "agent.yaml")

    assert config.root_dir == runtime_home.resolve()
    assert config.prompts_dir == config_dir / "prompts"
    assert config.prompt_path("review").name == "code_review.md"


@pytest.mark.parametrize("unsafe_path", ["../outside", "/tmp/minillm-outside"])
def test_configured_runtime_paths_cannot_escape_runtime_root(tmp_path, monkeypatch, unsafe_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_text = (ROOT / "config" / "agent.yaml").read_text(encoding="utf-8")
    config_text = config_text.replace("data_dir: data", f"data_dir: {unsafe_path}", 1)
    config_path = config_dir / "agent.yaml"
    config_path.write_text(config_text, encoding="utf-8")
    monkeypatch.setenv("LAGENT_HOME", str(tmp_path / "runtime"))

    with pytest.raises(ValueError):
        load_config(config_path)
