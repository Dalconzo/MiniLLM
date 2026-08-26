from __future__ import annotations

import os
import plistlib
import shlex
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..config import AppConfig
from ..security.paths import safe_write_text


HOME_MCP_HOME_LABEL = "com.dalconzo.minillm.home-mcp"
HOME_MCP_TUNNEL_LABEL = "com.dalconzo.minillm.home-mcp-tunnel"
HOME_MCP_TUNNEL_URL_FILE = "data/home_mcp/current_tunnel_url.txt"
HOME_MCP_TUNNEL_LOG_FILE = "data/logs/home_mcp_tunnel.log"
HOME_MCP_HOME_LOG_FILE = "data/logs/home_mcp_launchd.log"
HOME_MCP_AUTH_TOKEN_FILE = ".config/minillm/home-mcp-auth-token"


@dataclass(frozen=True)
class LaunchdInstallResult:
    home_plist: Path
    tunnel_plist: Path | None
    launched: list[str]


def _plist_base(
    program_arguments: list[str],
    *,
    label: str,
    working_directory: Path,
    stdout_path: Path,
    stderr_path: Path,
    environment: dict[str, str] | None = None,
    keep_alive: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": program_arguments,
        "RunAtLoad": True,
        "KeepAlive": keep_alive,
        "WorkingDirectory": str(working_directory),
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }
    if environment:
        payload["EnvironmentVariables"] = environment
    return payload


def build_home_mcp_launchd_plist(
    config: AppConfig,
    *,
    auth_mode: str = "none",
    auth_token: str | None = None,
    resource_url: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    label: str = HOME_MCP_HOME_LABEL,
) -> dict[str, Any]:
    python_path = Path(config.root_dir / ".venv" / "bin" / "python")
    args = [
        str(python_path),
        "-u",
        "-m",
        "local_agent_lab.cli",
        "home-mcp",
        "serve",
        "--host",
        host,
        "--port",
        str(port),
        "--auth-mode",
        auth_mode,
    ]
    tunnel_id = _detect_tunnel_id(config)
    resource_value = resource_url or (f"https://api.openai.com/v1/tunnel/{tunnel_id}" if tunnel_id else None)
    logs_dir = config.logs_dir
    env = {
        "LAGENT_CONFIG": str(config.path),
        "PYTHONUNBUFFERED": "1",
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
    }
    if resource_value:
        env["HOME_MCP_RESOURCE_URL"] = resource_value
    if auth_mode in {"bearer", "mixed"} or auth_token:
        env["LAGENT_HOME_MCP_TOKEN_FILE"] = str(_home_mcp_auth_token_file())
    return _plist_base(
        args,
        label=label,
        working_directory=config.root_dir,
        stdout_path=logs_dir / "home_mcp.stdout.log",
        stderr_path=logs_dir / "home_mcp.stderr.log",
        environment=env,
    )


def _detect_tunnel_id(config: AppConfig) -> str | None:
    raw = config.raw.get("home_mcp", {})
    if isinstance(raw, dict):
        value = raw.get("tunnel_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    profile_path = Path.home() / ".config" / "tunnel-client" / "home-mcp.yaml"
    if profile_path.exists():
        try:
            payload = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if isinstance(payload, dict):
            control_plane = payload.get("control_plane")
            if isinstance(control_plane, dict):
                value = control_plane.get("tunnel_id")
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def _tunnel_client_env_file() -> Path:
    return Path.home() / ".config" / "tunnel-client" / "home-mcp.env"


def _home_mcp_auth_token_file() -> Path:
    return Path.home() / HOME_MCP_AUTH_TOKEN_FILE


def _format_shell_assignment(name: str, value: str) -> str:
    return f"export {name}={shlex.quote(value)}"


def write_home_mcp_tunnel_client_env_file(*, tunnel_api_key: str | None = None) -> Path | None:
    key = tunnel_api_key or os.environ.get("CONTROL_PLANE_API_KEY")
    if not key:
        return None
    env_file = _tunnel_client_env_file()
    home = Path.home()
    _ensure_private_credential_directory(home, env_file.parent)
    written = safe_write_text(
        home,
        ".config/tunnel-client/home-mcp.env",
        "\n".join(
            [
                "# Managed by MiniLLM home-mcp install-service.",
                _format_shell_assignment("CONTROL_PLANE_API_KEY", key),
                "",
            ]
        ),
        mode=0o600,
    )
    _validate_private_credential_file(written)
    return written


def write_home_mcp_auth_token_file(*, auth_token: str | None = None) -> Path | None:
    token = auth_token or os.environ.get("LAGENT_HOME_MCP_TOKEN")
    if not token:
        return None
    token_file = _home_mcp_auth_token_file()
    home = Path.home()
    _ensure_private_credential_directory(home, token_file.parent)
    written = safe_write_text(home, HOME_MCP_AUTH_TOKEN_FILE, token.strip() + "\n", mode=0o600)
    _validate_private_credential_file(written)
    return written


def _ensure_private_credential_directory(home: Path, credential_dir: Path) -> None:
    if home.is_symlink() or not home.is_dir():
        raise RuntimeError("credential home must be a real directory and must not be a symlink")
    try:
        relative_parts = credential_dir.relative_to(home).parts
    except ValueError as exc:
        raise RuntimeError("credential directory must remain inside the user home") from exc

    current = home
    for index, part in enumerate(relative_parts):
        current = current / part
        if current.is_symlink():
            raise RuntimeError(f"credential directory must not be a symlink: {current}")
        try:
            current.mkdir(mode=0o700, exist_ok=True)
        except FileExistsError as exc:
            raise RuntimeError(f"credential parent must be a real directory: {current}") from exc
        metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(f"credential parent must be a real directory: {current}")
        if metadata.st_uid != os.geteuid():
            raise RuntimeError(f"credential parent must be owned by the current user: {current}")
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise RuntimeError(f"credential parent must not be group/other writable: {current}")
        if index == len(relative_parts) - 1:
            current.chmod(0o700)
            if stat.S_IMODE(current.lstat().st_mode) != 0o700:
                raise RuntimeError(f"credential directory must have mode 0700: {current}")


def _validate_private_credential_file(path: Path) -> None:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RuntimeError("credential file must be a single-link regular file")
    if metadata.st_uid != os.geteuid():
        raise RuntimeError("credential file must be owned by the current user")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise RuntimeError("credential file must have mode 0600")


def build_home_mcp_tunnel_launchd_plist(
    config: AppConfig,
    *,
    label: str = HOME_MCP_TUNNEL_LABEL,
    tunnel_script: Path | None = None,
) -> dict[str, Any]:
    script_path = tunnel_script or (config.root_dir / "scripts" / "home_mcp_tunnel.sh")
    logs_dir = config.logs_dir
    tunnel_id = _detect_tunnel_id(config)
    env = {
        "HOME_MCP_ROOT": str(config.root_dir),
        "HOME_MCP_TUNNEL_URL_FILE": str(config.root_dir / HOME_MCP_TUNNEL_URL_FILE),
        "HOME_MCP_TUNNEL_LOG_FILE": str(config.root_dir / HOME_MCP_TUNNEL_LOG_FILE),
        "HOME_MCP_TUNNEL_CLIENT_ENV_FILE": str(_tunnel_client_env_file()),
        "HOME_MCP_TUNNEL_CLIENT_BIN": str(Path.home() / "bin" / "tunnel-client"),
        "HOME_MCP_TUNNEL_PROFILE": "home-mcp",
        "HOME_MCP_PORT": "8765",
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
    }
    if tunnel_id:
        env["HOME_MCP_TUNNEL_ID"] = tunnel_id
    return _plist_base(
        ["/bin/bash", str(script_path)],
        label=label,
        working_directory=config.root_dir,
        stdout_path=logs_dir / "home_mcp_tunnel.stdout.log",
        stderr_path=logs_dir / "home_mcp_tunnel.stderr.log",
        environment=env,
    )


def write_plist_file(plist_path: Path, payload: dict[str, Any]) -> None:
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    content = plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False).decode("utf-8")
    safe_write_text(plist_path.parent, plist_path.name, content, mode=0o600)


def _launchctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=True, text=True, capture_output=True)


def install_home_mcp_launchd(
    config: AppConfig,
    *,
    auth_mode: str = "none",
    auth_token: str | None = None,
    with_tunnel: bool = True,
) -> LaunchdInstallResult:
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    home_plist = launch_agents / f"{HOME_MCP_HOME_LABEL}.plist"
    if auth_mode in {"bearer", "mixed"}:
        token_file = write_home_mcp_auth_token_file(auth_token=auth_token)
        if auth_mode == "bearer" and token_file is None and not _home_mcp_auth_token_file().exists():
            raise RuntimeError(
                "bearer auth requires LAGENT_HOME_MCP_TOKEN or a pre-created private Home MCP token file"
            )
    home_payload = build_home_mcp_launchd_plist(config, auth_mode=auth_mode, auth_token=auth_token)
    write_plist_file(home_plist, home_payload)

    launched: list[str] = []
    uid = subprocess.check_output(["id", "-u"], text=True).strip()
    domain = f"gui/{uid}"
    try:
        _launchctl(["launchctl", "bootout", domain, str(home_plist)])
    except subprocess.CalledProcessError:
        pass
    _launchctl(["launchctl", "bootstrap", domain, str(home_plist)])
    _launchctl(["launchctl", "kickstart", "-k", f"{domain}/{HOME_MCP_HOME_LABEL}"])
    launched.append(HOME_MCP_HOME_LABEL)

    tunnel_plist: Path | None = None
    if with_tunnel:
        env_file = write_home_mcp_tunnel_client_env_file()
        if env_file is None:
            existing = _tunnel_client_env_file()
            if not existing.exists():
                raise RuntimeError(
                    f"CONTROL_PLANE_API_KEY is required to install the home-mcp tunnel client or pre-create {existing}"
                )
        tunnel_plist = launch_agents / f"{HOME_MCP_TUNNEL_LABEL}.plist"
        tunnel_payload = build_home_mcp_tunnel_launchd_plist(config)
        write_plist_file(tunnel_plist, tunnel_payload)
        try:
            _launchctl(["launchctl", "bootout", domain, str(tunnel_plist)])
        except subprocess.CalledProcessError:
            pass
        _launchctl(["launchctl", "bootstrap", domain, str(tunnel_plist)])
        _launchctl(["launchctl", "kickstart", "-k", f"{domain}/{HOME_MCP_TUNNEL_LABEL}"])
        launched.append(HOME_MCP_TUNNEL_LABEL)

    return LaunchdInstallResult(home_plist=home_plist, tunnel_plist=tunnel_plist, launched=launched)


def uninstall_home_mcp_launchd(*, with_tunnel: bool = True) -> list[str]:
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    uid = subprocess.check_output(["id", "-u"], text=True).strip()
    domain = f"gui/{uid}"
    labels = [HOME_MCP_HOME_LABEL]
    if with_tunnel:
        labels.append(HOME_MCP_TUNNEL_LABEL)
    removed: list[str] = []
    for label in labels:
        plist_path = launch_agents / f"{label}.plist"
        try:
            _launchctl(["launchctl", "bootout", domain, str(plist_path)])
            removed.append(label)
        except subprocess.CalledProcessError:
            pass
    return removed


def read_home_mcp_tunnel_url(config: AppConfig) -> str | None:
    path = config.root_dir / HOME_MCP_TUNNEL_URL_FILE
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None
