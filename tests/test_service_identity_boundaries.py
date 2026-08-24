from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_service_identity_boundaries.sh"


def _layout(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "builder_root": tmp_path / "builder-runtime",
        "production_root": tmp_path / "production-runtime",
        "builder_credentials": tmp_path / "builder-credentials",
        "production_credentials": tmp_path / "production-credentials",
        "production_audit": tmp_path / "production-audit",
        "production_tunnel": tmp_path / "production-tunnel",
        "deployment_control": tmp_path / "deployment-control",
        "production_plist": tmp_path / "com.dalconzo.minillm.production.plist",
        "production_tunnel_plist": tmp_path / "com.dalconzo.minillm.production-tunnel.plist",
        "trusted_repository": tmp_path / "trusted-repository",
        "root_python_runtime": tmp_path / "root-python-runtime",
        "production_release": tmp_path / "production-release",
    }
    for name, path in paths.items():
        if name in {"production_plist", "production_tunnel_plist"}:
            path.write_text("plist fixture\n", encoding="utf-8")
            path.chmod(0o600)
        else:
            path.mkdir(mode=0o700)
    (paths["builder_root"] / "staging.db").write_text("fixture\n", encoding="utf-8")
    (paths["production_root"] / "memory.db").write_text("fixture\n", encoding="utf-8")
    (paths["builder_credentials"] / "builder.token").write_text("fixture\n", encoding="utf-8")
    (paths["production_credentials"] / "production.token").write_text("fixture\n", encoding="utf-8")
    (paths["production_audit"] / "audit.jsonl").write_text("fixture\n", encoding="utf-8")
    (paths["production_tunnel"] / "tunnel.env").write_text("fixture\n", encoding="utf-8")
    (paths["deployment_control"] / "approved-release").write_text("fixture\n", encoding="utf-8")
    for path in tmp_path.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    return paths


def _arguments(paths: dict[str, Path], *, builder: str = "builder", production: str = "production") -> list[str]:
    return [
        "--builder-user",
        builder,
        "--production-user",
        production,
        "--builder-root",
        str(paths["builder_root"]),
        "--production-root",
        str(paths["production_root"]),
        "--builder-credentials",
        str(paths["builder_credentials"]),
        "--production-credentials",
        str(paths["production_credentials"]),
        "--production-audit",
        str(paths["production_audit"]),
        "--production-tunnel",
        str(paths["production_tunnel"]),
        "--deployment-control",
        str(paths["deployment_control"]),
        "--production-plist",
        str(paths["production_plist"]),
        "--production-tunnel-plist",
        str(paths["production_tunnel_plist"]),
        "--trusted-repository",
        str(paths["trusted_repository"]),
        "--root-python-runtime",
        str(paths["root_python_runtime"]),
        "--production-release",
        str(paths["production_release"]),
    ]


def _run_with_metadata_fixture(
    tmp_path: Path,
    paths: dict[str, Path],
    *,
    args: list[str] | None = None,
    cross_membership: bool = False,
    bad_shell: bool = False,
    privileged_group: bool = False,
    plist_user: str = "production",
) -> subprocess.CompletedProcess[str]:
    builder_prefixes = (str(paths["builder_root"]), str(paths["builder_credentials"]))
    production_prefixes = (
        str(paths["production_root"]),
        str(paths["production_credentials"]),
        str(paths["production_audit"]),
        str(paths["production_tunnel"]),
    )
    builder_case = "|".join(
        pattern
        for value in builder_prefixes
        for pattern in (shlex.quote(value), f"{shlex.quote(value)}/*")
    )
    production_case = "|".join(
        pattern
        for value in production_prefixes
        for pattern in (shlex.quote(value), f"{shlex.quote(value)}/*")
    )
    builder_groups = "501 502" if cross_membership else "501"
    builder_group_names = "builder admin" if privileged_group else "builder"
    builder_shell = "/bin/bash" if bad_shell else "/usr/bin/false"
    shell_args = " ".join(shlex.quote(value) for value in (args or _arguments(paths)))
    harness = f"""
source {shlex.quote(str(SCRIPT))}
os_current_uid() {{ printf '0\\n'; }}
os_uname() {{ printf 'Darwin\\n'; }}
os_id_uid() {{
  case "$1" in builder) printf '501\\n' ;; production) printf '502\\n' ;; *) return 1 ;; esac
}}
os_id_gid() {{
  case "$1" in builder) printf '501\\n' ;; production) printf '502\\n' ;; *) return 1 ;; esac
}}
os_id_groups() {{
  case "$1" in builder) printf '{builder_groups}\\n' ;; production) printf '502\\n' ;; *) return 1 ;; esac
}}
os_id_group_names() {{
  case "$1" in builder) printf '{builder_group_names}\\n' ;; production) printf 'production\\n' ;; *) return 1 ;; esac
}}
os_user_property() {{
  case "$2" in
    UserShell) if [[ "$1" == builder ]]; then printf '{builder_shell}\\n'; else printf '/usr/bin/false\\n'; fi ;;
    NFSHomeDirectory) printf '/var/empty\\n' ;;
    IsHidden) printf '1\\n' ;;
    *) return 1 ;;
  esac
}}
os_plist_value() {{
  case "$1" in UserName|GroupName) printf '{plist_user}\\n' ;; *) return 1 ;; esac
}}
os_stat_record() {{
  local path=$1 uid mode kind
  case "$path" in
    {builder_case}) uid=501 ;;
    {production_case}) uid=502 ;;
    *) uid=0 ;;
  esac
  if /usr/bin/stat -f '%Lp' "$path" >/dev/null 2>&1; then
    mode=$(/usr/bin/stat -f '%Lp' "$path")
  else
    mode=$(/usr/bin/stat -c '%a' "$path")
  fi
  if [[ -d "$path" ]]; then kind=Directory; else kind=File; fi
  printf '%s|%s|%s|%s\\n' "$uid" "$uid" "$mode" "$kind"
}}
main {shell_args}
"""
    return subprocess.run(
        ["/bin/bash", "-c", harness],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_verifier_help_and_source_are_read_only() -> None:
    result = subprocess.run([str(SCRIPT), "--help"], cwd=ROOT, text=True, capture_output=True, check=False)

    assert result.returncode == 0
    assert "reads identity and filesystem metadata only" in result.stdout
    source = SCRIPT.read_text(encoding="utf-8")
    forbidden_commands = (
        "/usr/bin/sudo",
        "\nsudo ",
        "chown ",
        "chmod ",
        "dscl . -create",
        "dscl . -delete",
        "dscl . -append",
        "launchctl ",
        "sysadminctl ",
        "rm -",
        "mv ",
        "cp ",
    )
    assert all(command not in source for command in forbidden_commands)


def test_complete_private_layout_passes_all_boundary_checks(tmp_path: Path) -> None:
    paths = _layout(tmp_path)

    result = _run_with_metadata_fixture(tmp_path, paths)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "SUMMARY" in result.stdout
    assert "fail=0" in result.stdout
    assert "read_only=true" in result.stdout
    assert result.stdout.count("PASS negative_access") == 10
    assert result.stdout.count("PASS root_readonly_tree") == 2


def test_permissive_production_file_fails(tmp_path: Path) -> None:
    paths = _layout(tmp_path)
    exposed = paths["production_credentials"] / "production.token"
    exposed.chmod(0o644)

    result = _run_with_metadata_fixture(tmp_path, paths)

    assert result.returncode == 1
    assert "FAIL private_permissions" in result.stdout
    assert str(exposed) in result.stdout


def test_cross_plane_group_membership_fails(tmp_path: Path) -> None:
    paths = _layout(tmp_path)

    result = _run_with_metadata_fixture(tmp_path, paths, cross_membership=True)

    assert result.returncode == 1
    assert "FAIL cross_group_membership" in result.stdout


def test_interactive_or_privileged_service_account_fails(tmp_path: Path) -> None:
    paths = _layout(tmp_path)

    result = _run_with_metadata_fixture(tmp_path, paths, bad_shell=True, privileged_group=True)

    assert result.returncode == 1
    assert "FAIL service_account_shell" in result.stdout
    assert "FAIL service_account_groups" in result.stdout


def test_launch_daemon_must_declare_production_identity(tmp_path: Path) -> None:
    paths = _layout(tmp_path)

    result = _run_with_metadata_fixture(tmp_path, paths, plist_user="builder")

    assert result.returncode == 1
    assert result.stdout.count("FAIL production_plist_identity") == 4


def test_symlink_inside_production_tree_fails(tmp_path: Path) -> None:
    paths = _layout(tmp_path)
    (paths["production_root"] / "redirect").symlink_to(paths["builder_root"])

    result = _run_with_metadata_fixture(tmp_path, paths)

    assert result.returncode == 1
    assert "FAIL tree_has_no_symlinks" in result.stdout


def test_overlapping_runtime_and_credential_roots_fail(tmp_path: Path) -> None:
    paths = _layout(tmp_path)
    nested = paths["production_root"] / "credentials"
    nested.mkdir(mode=0o700)
    paths["production_credentials"] = nested

    result = _run_with_metadata_fixture(tmp_path, paths)

    assert result.returncode == 1
    assert "FAIL roots_are_disjoint" in result.stdout
    assert "production_root overlaps production_credentials" in result.stdout


def test_direct_execution_refuses_non_administrator_visibility(tmp_path: Path) -> None:
    if sys.platform != "darwin" or os.geteuid() == 0:
        return
    paths = _layout(tmp_path)

    result = subprocess.run(
        [str(SCRIPT), *_arguments(paths)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "run this read-only verifier as an administrator" in result.stderr
