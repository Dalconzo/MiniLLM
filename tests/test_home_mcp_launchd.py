from __future__ import annotations

import plistlib
import stat
from pathlib import Path

import pytest

from local_agent_lab.config import load_config
from local_agent_lab.services.home_mcp_launchd import (
    HOME_MCP_HOME_LABEL,
    HOME_MCP_TUNNEL_LABEL,
    build_home_mcp_launchd_plist,
    build_home_mcp_tunnel_launchd_plist,
    read_home_mcp_tunnel_url,
    write_home_mcp_auth_token_file,
    write_plist_file,
)
from local_agent_lab.security.paths import PathPolicyError


def _write_config(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "app": {"name": "local-agent-lab", "log_level": "info"},
        "paths": {
            "data_dir": "data",
            "logs_dir": "data/logs",
            "indexes_dir": "data/indexes",
            "memory_dir": "data/memory",
            "patches_dir": "data/patches",
        },
        "ollama": {"host": "http://127.0.0.1:11434", "request_timeout_seconds": 180},
        "runtime": {"default_task": "chat", "redact_before_model": True, "save_full_prompts": True},
        "models": {},
        "routing": {"task_map": {}},
    }
    config_path = config_dir / "agent.yaml"
    config_path.write_text(__import__("json").dumps(payload), encoding="utf-8")
    return config_path


def test_home_mcp_launchd_plists_include_expected_programs(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)

    from local_agent_lab.services import home_mcp_launchd as launchd_mod

    monkeypatch.setattr(launchd_mod.Path, "home", lambda: tmp_path)
    profile_dir = tmp_path / ".config" / "tunnel-client"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "home-mcp.yaml").write_text(
        "\n".join(
            [
                "config_version: 1",
                "control_plane:",
                '  tunnel_id: "tunnel_123"',
                '  api_key: "env:CONTROL_PLANE_API_KEY"',
                "mcp:",
                "  server_urls:",
                "    - channel: main",
                '      url: "http://127.0.0.1:8765/mcp"',
            ]
        ),
        encoding="utf-8",
    )

    home_plist = build_home_mcp_launchd_plist(config, auth_mode="none")
    assert home_plist["Label"] == HOME_MCP_HOME_LABEL
    assert home_plist["ProgramArguments"][:4] == [
        str(config.root_dir / ".venv" / "bin" / "python"),
        "-u",
        "-m",
        "local_agent_lab.cli",
    ]
    assert home_plist["EnvironmentVariables"]["LAGENT_CONFIG"] == str(config.path)
    assert home_plist["KeepAlive"] is True

    tunnel_plist = build_home_mcp_tunnel_launchd_plist(config)
    assert tunnel_plist["Label"] == HOME_MCP_TUNNEL_LABEL
    assert tunnel_plist["ProgramArguments"] == ["/bin/bash", str(config.root_dir / "scripts" / "home_mcp_tunnel.sh")]
    assert tunnel_plist["EnvironmentVariables"]["HOME_MCP_PORT"] == "8765"
    assert tunnel_plist["EnvironmentVariables"]["HOME_MCP_TUNNEL_PROFILE"] == "home-mcp"
    assert tunnel_plist["EnvironmentVariables"]["HOME_MCP_TUNNEL_CLIENT_ENV_FILE"].endswith(
        ".config/tunnel-client/home-mcp.env"
    )
    assert tunnel_plist["EnvironmentVariables"]["HOME_MCP_TUNNEL_CLIENT_BIN"].endswith("/bin/tunnel-client")
    assert tunnel_plist["EnvironmentVariables"]["HOME_MCP_TUNNEL_ID"] == "tunnel_123"
    assert tunnel_plist["KeepAlive"] is True


def test_home_mcp_launchd_plist_derives_https_resource_url(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)

    from local_agent_lab.services import home_mcp_launchd as launchd_mod

    monkeypatch.setattr(launchd_mod.Path, "home", lambda: tmp_path)

    profile_dir = tmp_path / ".config" / "tunnel-client"
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_path = profile_dir / "home-mcp.yaml"
    profile_path.write_text(
        "\n".join(
            [
                "config_version: 1",
                "control_plane:",
                '  tunnel_id: "tunnel_123"',
                '  api_key: "env:CONTROL_PLANE_API_KEY"',
                "mcp:",
                "  server_urls:",
                "    - channel: main",
                '      url: "http://127.0.0.1:8765/mcp"',
            ]
        ),
        encoding="utf-8",
    )

    try:
        plist = build_home_mcp_launchd_plist(config)
        assert plist["EnvironmentVariables"]["HOME_MCP_RESOURCE_URL"] == "https://api.openai.com/v1/tunnel/tunnel_123"
    finally:
        profile_path.unlink(missing_ok=True)


def test_home_mcp_launchd_plist_round_trip_and_tunnel_url(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)

    plist_path = tmp_path / "launchd.plist"
    write_plist_file(plist_path, build_home_mcp_launchd_plist(config))
    parsed = plistlib.loads(plist_path.read_bytes())
    assert parsed["Label"] == HOME_MCP_HOME_LABEL
    assert (plist_path.stat().st_mode & 0o777) == 0o600

    url_file = config.root_dir / "data" / "home_mcp" / "current_tunnel_url.txt"
    url_file.parent.mkdir(parents=True, exist_ok=True)
    url_file.write_text("https://example.trycloudflare.com\n", encoding="utf-8")
    assert read_home_mcp_tunnel_url(config) == "https://example.trycloudflare.com"


def test_home_mcp_tunnel_client_env_file_is_written(tmp_path, monkeypatch) -> None:
    from local_agent_lab.services import home_mcp_launchd as launchd_mod

    monkeypatch.setattr(launchd_mod.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "sk-test-key")

    env_file = launchd_mod.write_home_mcp_tunnel_client_env_file()
    assert env_file is not None
    assert env_file.exists()
    assert "export CONTROL_PLANE_API_KEY=sk-test-key" in env_file.read_text(encoding="utf-8")
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(env_file.parent.stat().st_mode) == 0o700


def test_home_mcp_tunnel_env_writer_rejects_symlinked_parent(tmp_path, monkeypatch) -> None:
    from local_agent_lab.services import home_mcp_launchd as launchd_mod

    outside = tmp_path / "outside"
    outside.mkdir()
    config_root = tmp_path / ".config"
    config_root.mkdir()
    (config_root / "tunnel-client").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(launchd_mod.Path, "home", lambda: tmp_path)

    with pytest.raises(RuntimeError, match="must not be a symlink"):
        launchd_mod.write_home_mcp_tunnel_client_env_file(tunnel_api_key="must-not-escape")
    assert not (outside / "home-mcp.env").exists()


def test_home_mcp_tunnel_env_writer_rejects_unsafe_parent_mode(tmp_path, monkeypatch) -> None:
    from local_agent_lab.services import home_mcp_launchd as launchd_mod

    config_root = tmp_path / ".config"
    config_root.mkdir()
    tunnel_root = config_root / "tunnel-client"
    tunnel_root.mkdir(mode=0o777)
    tunnel_root.chmod(0o777)
    monkeypatch.setattr(launchd_mod.Path, "home", lambda: tmp_path)

    with pytest.raises(RuntimeError, match="must not be group/other writable"):
        launchd_mod.write_home_mcp_tunnel_client_env_file(tunnel_api_key="must-not-persist")
    assert not (tunnel_root / "home-mcp.env").exists()


def test_home_mcp_tunnel_env_writer_rejects_final_symlink_and_hard_link(tmp_path, monkeypatch) -> None:
    from local_agent_lab.services import home_mcp_launchd as launchd_mod

    tunnel_root = tmp_path / ".config" / "tunnel-client"
    tunnel_root.mkdir(parents=True, mode=0o700)
    outside = tmp_path / "outside.env"
    outside.write_text("outside remains unchanged\n", encoding="utf-8")
    env_file = tunnel_root / "home-mcp.env"
    env_file.symlink_to(outside)
    monkeypatch.setattr(launchd_mod.Path, "home", lambda: tmp_path)

    with pytest.raises((PathPolicyError, OSError)):
        launchd_mod.write_home_mcp_tunnel_client_env_file(tunnel_api_key="must-not-escape")
    assert outside.read_text(encoding="utf-8") == "outside remains unchanged\n"

    env_file.unlink()
    env_file.hardlink_to(outside)
    with pytest.raises(PathPolicyError, match="multiple hard links"):
        launchd_mod.write_home_mcp_tunnel_client_env_file(tunnel_api_key="must-not-overwrite")
    assert outside.read_text(encoding="utf-8") == "outside remains unchanged\n"


def test_home_mcp_bearer_secret_uses_private_file_not_plist_or_argv(tmp_path, monkeypatch) -> None:
    from local_agent_lab.services import home_mcp_launchd as launchd_mod

    config = load_config(_write_config(tmp_path))
    monkeypatch.setattr(launchd_mod.Path, "home", lambda: tmp_path)
    secret = "bearer secret with spaces"

    token_file = write_home_mcp_auth_token_file(auth_token=secret)
    plist = build_home_mcp_launchd_plist(config, auth_mode="bearer", auth_token=secret)
    serialized = plistlib.dumps(plist).decode("utf-8")

    assert token_file is not None
    assert token_file.read_text(encoding="utf-8").strip() == secret
    assert (token_file.stat().st_mode & 0o777) == 0o600
    assert secret not in serialized
    assert "--auth-token" not in plist["ProgramArguments"]
    assert plist["EnvironmentVariables"]["LAGENT_HOME_MCP_TOKEN_FILE"] == str(token_file)


def test_home_mcp_auth_token_writer_rejects_symlinked_private_directory(tmp_path, monkeypatch) -> None:
    from local_agent_lab.services import home_mcp_launchd as launchd_mod

    outside = tmp_path / "outside"
    outside.mkdir()
    config_root = tmp_path / ".config"
    config_root.mkdir()
    (config_root / "minillm").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(launchd_mod.Path, "home", lambda: tmp_path)

    with pytest.raises(RuntimeError, match="must not be a symlink"):
        write_home_mcp_auth_token_file(auth_token="must-not-escape")
    assert not (outside / "home-mcp-auth-token").exists()
