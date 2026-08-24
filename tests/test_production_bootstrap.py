from __future__ import annotations

import hashlib
import platform
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bootstrap_minillm_production.sh"
COMMIT = "a" * 40
RESOURCE_URL = "https://api.openai.com/v1/tunnel/tunnel_fixture123"


def _inputs(tmp_path: Path) -> list[str]:
    bundle = tmp_path / "candidate.bundle"
    wheelhouse = tmp_path / "wheelhouse.tar.gz"
    profile = tmp_path / "home-mcp.yaml"
    environment = tmp_path / "home-mcp.env"
    client = tmp_path / "tunnel-client"
    data = tmp_path / "data"
    bundle.write_bytes(b"bundle fixture")
    wheelhouse.write_bytes(b"wheelhouse fixture")
    profile.write_text("profile: fixture\n", encoding="utf-8")
    environment.write_text("CONTROL_PLANE_API_KEY=fixture\n", encoding="utf-8")
    client.write_text("fixture\n", encoding="utf-8")
    data.mkdir()
    return [
        "--candidate-commit",
        COMMIT,
        "--git-bundle",
        str(bundle),
        "--git-bundle-sha256",
        hashlib.sha256(bundle.read_bytes()).hexdigest(),
        "--wheelhouse-archive",
        str(wheelhouse),
        "--wheelhouse-sha256",
        hashlib.sha256(wheelhouse.read_bytes()).hexdigest(),
        "--resource-url",
        RESOURCE_URL,
        "--source-data",
        str(data),
        "--source-tunnel-profile",
        str(profile),
        "--source-tunnel-env",
        str(environment),
        "--source-tunnel-client",
        str(client),
    ]


def test_bootstrap_is_valid_shell_and_documents_non_disruptive_default() -> None:
    syntax = subprocess.run(["/bin/bash", "-n", str(SCRIPT)], text=True, capture_output=True, check=False)
    help_result = subprocess.run([str(SCRIPT), "--help"], text=True, capture_output=True, check=False)

    assert syntax.returncode == 0, syntax.stderr
    assert help_result.returncode == 0
    assert "Dry-run is the default" in help_result.stdout
    assert "never calls" in help_result.stdout
    assert "never stops, edits, or replaces the live user LaunchAgents" in help_result.stdout


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS bootstrap contract")
def test_bootstrap_dry_run_reports_no_cutover(tmp_path: Path) -> None:
    result = subprocess.run([str(SCRIPT), *_inputs(tmp_path)], text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr
    assert "apply=false" in result.stdout
    assert "DRY_RUN no host changes performed" in result.stdout
    assert "live_user_launch_agents_untouched=true" in result.stdout
    assert "system_daemons_started=false" in result.stdout


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS bootstrap contract")
def test_bootstrap_apply_requires_exact_confirmation_before_root(tmp_path: Path) -> None:
    result = subprocess.run(
        [str(SCRIPT), *_inputs(tmp_path), "--apply"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "--confirm BOOTSTRAP_MINILLM_PRODUCTION" in result.stderr


def test_bootstrap_keeps_executable_code_root_owned_and_services_disabled() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'PYTHON_RUNTIME="/Library/Application Support/MiniLLM/python-runtime"' in source
    assert '"PYTHONPATH=$RELEASE_ROOT/current/src"' in source
    assert '"Disabled": True' in source
    assert "/usr/bin/launchctl" not in source
    assert "/bin/launchctl" not in source
    assert "CONTROL_PLANE_API_KEY=" not in source
    assert "BOOTSTRAP_MINILLM_PRODUCTION" in source
    assert "source_tunnel_env" in source
    assert '"HOME_MCP_TUNNEL_ID": tunnel_id' in source
    assert 'resource_url.rsplit("/", 1)[-1]' in source
    assert "config core.filemode false" in source
    assert 'git -C "$TRUSTED_REPOSITORY" bundle verify' in source
    assert 'INPUT_ROOT="$CONTROL_ROOT/bootstrap-inputs/$CANDIDATE_COMMIT"' in source
    assert source.index('GIT_BUNDLE="$INPUT_ROOT/candidate.bundle"') < source.index('sha256_file "$GIT_BUNDLE"')


def test_bootstrap_shadow_smoke_is_unique_and_reaps_the_process_tree() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    launch = 'run_as_service_identity "$PRODUCTION_USER" "$PRODUCTION_ROOT" "$RELEASE_ROOT/current"'
    assert "shadow_port_unused" in source
    assert source.index('shadow_port_unused || die "production shadow port') < source.index(launch)
    assert "terminate_process_tree" in source
    assert 'terminate_process_tree "$child"' in source
    assert source.index('stop_shadow\nfor _ in $(/usr/bin/jot 20)') > source.index(launch)
    assert 'shadow_port_unused || die "production shadow process did not stop"' in source
    assert "stop_shadow || true" in source


def test_service_identity_commands_use_an_accessible_working_directory_and_retry_install() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "run_as_service_identity()" in source
    assert '/usr/bin/env -C "$working_directory" HOME="$home"' in source
    assert "/bin/bash -c 'umask 077; exec \"$@\"'" in source
    assert "/bin/bash -c 'cd" not in source
    assert source.count('run_as_service_identity "$BUILDER_USER"') == 3
    assert source.count('run_as_service_identity "$PRODUCTION_USER"') == 2
    assert source.index('if [[ ! -x "$BUILDER_ROOT/venv/bin/python" ]]') < source.index(
        '"$BUILDER_ROOT/venv/bin/python" -m pip install'
    )


def test_shadow_outputs_are_private_during_and_after_execution() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert '"Umask": 0o077' in source
    final_stop = source.rindex("stop_shadow")
    assert final_stop < source.rindex('secure_tree "$PRODUCTION_ROOT"')
    assert final_stop < source.rindex('secure_tree "$PRODUCTION_AUDIT"')
    assert final_stop < source.rindex('secure_tree "$PRODUCTION_TUNNEL"')


def test_retry_can_advance_a_clean_trusted_repository_from_the_verified_bundle() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    verify = 'git -C "$TRUSTED_REPOSITORY" bundle verify "$GIT_BUNDLE"'
    fetch = 'git -C "$TRUSTED_REPOSITORY" fetch --no-tags "$GIT_BUNDLE" "$CANDIDATE_COMMIT"'
    checkout = 'git -C "$TRUSTED_REPOSITORY" checkout --detach "$CANDIDATE_COMMIT"'
    assert source.index(verify) < source.index(fetch) < source.index(checkout)
    assert "trusted repository already contains a different baseline" not in source
