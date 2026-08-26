from __future__ import annotations

import tomllib
from pathlib import Path

from typer.testing import CliRunner

from local_agent_lab.cli import app


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_legacy_minillm_python_package_is_absent() -> None:
    legacy_source = PROJECT_ROOT / "src" / "minillm"

    assert not list(legacy_source.glob("*.py"))


def test_lagent_entry_point_targets_canonical_package() -> None:
    metadata = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["scripts"]["lagent"] == "local_agent_lab.cli:app"


def test_canonical_cli_discovery_needs_no_external_service() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "health" in result.stdout
    assert "home-mcp" in result.stdout
