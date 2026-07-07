from __future__ import annotations

import plistlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..config import AppConfig


HOME_MCP_HOME_LABEL = "com.dalconzo.minillm.home-mcp"
HOME_MCP_TUNNEL_LABEL = "com.dalconzo.minillm.home-mcp-tunnel"
HOME_MCP_TUNNEL_URL_FILE = "data/home_mcp/current_tunnel_url.txt"
HOME_MCP_TUNNEL_LOG_FILE = "data/logs/home_mcp_tunnel.log"
HOME_MCP_HOME_LOG_FILE = "data/logs/home_mcp_launchd.log"


@dataclass(frozen=True)
class LaunchdInstallResult:
    home_plist: Path
    tunnel_plist: Path | None
    launched: list[str]


def _plist_base(program_arguments: list[str], *, label: str, working_directory: Path, stdout_path: Path, stderr_path: Path, environment: dict[str, str] | None = None, keep_alive: bool = True) -> dict[str, Any]:
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
    if auth_token:
        args.extend(["--auth-token", auth_token])
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


def build_home_mcp_tunnel_launchd_plist(
    config: AppConfig,
    *,
    label: str = HOME_MCP_TUNNEL_LABEL,
    tunnel_script: Path | None = None,
) -> dict[str, Any]:
    script_path = tunnel_script or (config.root_dir / "scripts" / "home_mcp_tunnel.sh")
    logs_dir = config.logs_dir
    return _plist_base(
        ["/bin/bash", str(script_path)],
        label=label,
        working_directory=config.root_dir,
        stdout_path=logs_dir / "home_mcp_tunnel.stdout.log",
        stderr_path=logs_dir / "home_mcp_tunnel.stderr.log",
        environment={
            "HOME_MCP_ROOT": str(config.root_dir),
            "HOME_MCP_TUNNEL_URL_FILE": str(config.root_dir / HOME_MCP_TUNNEL_URL_FILE),
            "HOME_MCP_TUNNEL_LOG_FILE": str(config.root_dir / HOME_MCP_TUNNEL_LOG_FILE),
            "HOME_MCP_PORT": "8765",
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        },
    )


def write_plist_file(plist_path: Path, payload: dict[str, Any]) -> None:
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False))


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
