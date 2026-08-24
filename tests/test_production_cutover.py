from __future__ import annotations

import platform
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "cutover_minillm_production.sh"


def test_cutover_is_valid_shell_and_documents_automatic_rollback() -> None:
    syntax = subprocess.run(["/bin/bash", "-n", str(SCRIPT)], text=True, capture_output=True, check=False)
    help_result = subprocess.run([str(SCRIPT), "--help"], text=True, capture_output=True, check=False)

    assert syntax.returncode == 0, syntax.stderr
    assert help_result.returncode == 0
    assert "Dry-run is the default" in help_result.stdout
    assert "automatically" in help_result.stdout
    assert "Source data and user plist files are retained for rollback" in help_result.stdout


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS cutover contract")
def test_cutover_default_is_non_mutating() -> None:
    result = subprocess.run([str(SCRIPT)], cwd=ROOT, text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr
    assert "apply=false" in result.stdout
    assert "DRY_RUN no host changes performed" in result.stdout
    assert "automatic_user_service_rollback=true" in result.stdout
    assert "source_data_preserved=true" in result.stdout


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS cutover contract")
def test_cutover_apply_requires_exact_confirmation_before_root() -> None:
    result = subprocess.run([str(SCRIPT), "--apply"], cwd=ROOT, text=True, capture_output=True, check=False)

    assert result.returncode == 2
    assert "--confirm CUTOVER_MINILLM_PRODUCTION" in result.stderr


def test_cutover_requires_confirmation_and_contains_fail_closed_ordering() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "--confirm CUTOVER_MINILLM_PRODUCTION" in source
    assert source.index('bootout "gui/$USER_UID" "$USER_TUNNEL_PLIST"') < source.index('bootstrap system "$HOME_PLIST"')
    assert source.index('bootstrap system "$HOME_PLIST"') < source.index('bootstrap system "$TUNNEL_PLIST"')
    assert "restore_user_services" in source
    assert "trap rollback_on_error ERR" in source
    assert "return 2" in source
    assert "exit 2" not in source
    assert '"$SOURCE_DATA/" "$PRODUCTION_ROOT/data/"' in source
    assert '"$PRODUCTION_ROOT/data/" "$SOURCE_DATA/"' not in source
    assert "http://127.0.0.1:8080/readyz" in source


def test_cutover_rechecks_boundaries_and_keeps_runtime_outputs_private() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    boundary_check = 'verify_service_identity_boundaries.sh"'
    stop_user_tunnel = 'bootout "gui/$USER_UID" "$USER_TUNNEL_PLIST"'
    assert source.index(boundary_check) < source.index(stop_user_tunnel)
    assert source.count("Print :Umask") == 2
    assert "/bin/bash -c 'umask 077; exec \"$@\"'" in source
    assert source.count("secure_production_data") == 3
    assert source.index("home-mcp smoke-test") < source.rindex("secure_production_data")


def test_rollback_reports_success_only_after_both_user_endpoints_recover() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "wait_for_health http://127.0.0.1:8765/health || restore_failed=true" in source
    assert "wait_for_health http://127.0.0.1:8080/readyz || restore_failed=true" in source
    assert "ROLLBACK completed=false manual_recovery_required=true" in source
    assert "ROLLBACK completed=true user_health=passed tunnel_readiness=passed" in source
