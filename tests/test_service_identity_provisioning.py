from __future__ import annotations

import platform
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "provision_minillm_service_identities.sh"


def test_provisioner_is_valid_shell_and_documents_non_mutating_default() -> None:
    syntax = subprocess.run(["/bin/bash", "-n", str(SCRIPT)], text=True, capture_output=True, check=False)
    help_result = subprocess.run([str(SCRIPT), "--help"], text=True, capture_output=True, check=False)

    assert syntax.returncode == 0, syntax.stderr
    assert help_result.returncode == 0
    assert "Dry-run is the default" in help_result.stdout
    assert "never loads, starts, or unloads a service" in help_result.stdout


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS provisioning contract")
def test_default_run_is_a_dry_run_with_no_migration_or_credentials() -> None:
    result = subprocess.run([str(SCRIPT)], cwd=ROOT, text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr
    assert "apply=false" in result.stdout
    assert "DRY_RUN no host changes performed" in result.stdout
    assert "existing_user_launch_agent_untouched=true" in result.stdout
    assert "credentials_copied=false" in result.stdout
    assert "production_data_migrated=false" in result.stdout
    assert "service_started=false" in result.stdout


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS provisioning contract")
def test_apply_requires_exact_human_confirmation_before_root_check() -> None:
    result = subprocess.run([str(SCRIPT), "--apply"], cwd=ROOT, text=True, capture_output=True, check=False)

    assert result.returncode == 2
    assert "--confirm CREATE_MINILLM_SERVICE_IDENTITIES" in result.stderr


def test_provisioner_does_not_embed_credentials_or_manage_the_live_service() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "launchctl" not in source
    assert "CONTROL_PLANE_API_KEY" not in source
    assert "LAGENT_HOME_MCP_TOKEN" not in source
    assert source.count('"--auth-token"') == 1
    assert "CREATE_MINILLM_SERVICE_IDENTITIES" in source
    assert '"Disabled": True' in source
    assert "only the controlled releases/current pointer may be a release symlink" in source
    assert '"PYTHONPATH": release.rstrip("/") + "/src"' in source
    assert '"Umask": 0o077' in source


def test_production_launch_daemon_is_loopback_oauth_and_secret_free() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'PRODUCTION_HOST="127.0.0.1"' in source
    assert 'PRODUCTION_PORT="8765"' in source
    assert 'PRODUCTION_AUTH_MODE="oauth"' in source
    assert '"--host", host, "--port", port, "--auth-mode", auth_mode' in source
    assert 'expected_auth_mode != "oauth"' in source
    assert 'if "--auth-token" in arguments:' in source
    assert '"--auth-mode", "none"' not in source
    assert '"--auth-mode", "mixed"' not in source


def test_numeric_id_lookup_uses_one_record_per_line_directory_output() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'dscl . -list "/$kind" "$attribute"' in source
    assert "'$2 == expected_id {print $1}'" in source
    assert 'dscl . -search "/$kind"' not in source
