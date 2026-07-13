from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from local_agent_lab.bake_cam import (
    BakeCamError,
    capture_now,
    create_session,
    health_check,
    latest_capture,
    list_sessions,
    schedule_session,
    session_dir,
    sync_spooled_captures,
)
from local_agent_lab.cli import app


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
    path = config_dir / "agent.yaml"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_create_and_list_baking_session(tmp_path: Path) -> None:
    session = create_session(
        tmp_path,
        session_type="starter_feeding",
        name="Rye starter morning feed",
        recipe_id="recipe_book:starter.md",
    )

    assert session["activity_type"] == "starter_feeding"
    assert session["capture_count"] == 0
    assert (session_dir(tmp_path, session["session_id"]) / "session.json").exists()
    assert (session_dir(tmp_path, session["session_id"]) / "captures").is_dir()

    sessions = list_sessions(tmp_path)
    assert [item["session_id"] for item in sessions] == [session["session_id"]]


def test_rejects_unknown_session_type(tmp_path: Path) -> None:
    try:
        create_session(tmp_path, session_type="pizza", name="Bad type")
    except BakeCamError as exc:
        assert exc.stage == "validate_session"
        assert exc.error_code == "invalid_session_type"
    else:
        raise AssertionError("expected invalid session type")


def test_health_check_parses_remote_probe(tmp_path: Path) -> None:
    def fake_runner(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "hostname=DavesDev\n"
                "time=2026-07-12T20:00:00Z\n"
                "disk=123 456 789 10% /\n"
                "camera_tool=/usr/bin/rpicam-still\n"
                "camera_probe=Available cameras|0 : imx708 [4608x2592]| \n"
                "video_devices=/dev/video0\n"
            ),
            stderr="",
        )

    payload = health_check(tmp_path, device_id="DavesDev", runner=fake_runner)

    assert payload["status"] == "ok"
    assert payload["ssh_ok"] is True
    assert payload["camera_available"] is True
    assert payload["camera_mode"] == "uvc"
    assert payload["probe"]["camera_tool"] == "/usr/bin/rpicam-still"
    assert payload["trace"][0]["stage"] == "ssh_health_probe_attempt"


def test_health_check_marks_no_camera_probe_degraded(tmp_path: Path) -> None:
    def fake_runner(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "hostname=DavesDev\n"
                "time=2026-07-12T20:00:00Z\n"
                "disk=123 456 789 10% /\n"
                "camera_tool=/usr/bin/rpicam-still\n"
                "camera_probe=No cameras available!|\n"
                "video_devices=/dev/video19 /dev/video20\n"
            ),
            stderr="",
        )

    payload = health_check(tmp_path, device_id="DavesDev", runner=fake_runner)

    assert payload["status"] == "degraded"
    assert payload["ssh_ok"] is True
    assert payload["camera_available"] is False


def test_schedule_session_generates_t_plus_plan(tmp_path: Path) -> None:
    session = create_session(tmp_path, session_type="starter_feeding", name="Rye starter")

    payload = schedule_session(tmp_path, session_id=session["session_id"], at="0h,2h,4h,8h")

    assert [item["offset_label"] for item in payload["capture_plan"]] == ["t+0h", "t+2h", "t+4h", "t+8h"]
    assert {item["status"] for item in payload["capture_plan"]} == {"pending"}


def test_capture_now_writes_image_metadata_and_latest(tmp_path: Path) -> None:
    session = create_session(tmp_path, session_type="bulk_fermentation", name="Country loaf bulk")

    def fake_runner(command, **kwargs):
        if command[0] == "ssh":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[0] == "scp":
            destination = Path(command[-1])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"fake-jpeg")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {command}")

    payload = capture_now(tmp_path, session_id=session["session_id"], device_id="DavesDev", runner=fake_runner)

    assert payload["status"] == "ok"
    capture = payload["capture"]
    assert Path(capture["local_path"]).read_bytes() == b"fake-jpeg"
    assert capture["upload_status"] == "ok"
    assert (session_dir(tmp_path, session["session_id"]) / "latest" / "main.jpg").exists()
    assert latest_capture(tmp_path, session_id=session["session_id"])["capture_id"] == capture["capture_id"]

    updated_session = json.loads((session_dir(tmp_path, session["session_id"]) / "session.json").read_text())
    assert updated_session["capture_count"] == 1
    assert updated_session["last_error"] is None


def test_capture_failure_marks_session_error(tmp_path: Path) -> None:
    session = create_session(tmp_path, session_type="final_proof", name="Pan loaf proof")

    def fake_runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="camera missing")

    payload = capture_now(tmp_path, session_id=session["session_id"], device_id="DavesDev", runner=fake_runner)

    assert payload["status"] == "error"
    assert payload["error"]["stage"] == "remote_capture"
    updated_session = json.loads((session_dir(tmp_path, session["session_id"]) / "session.json").read_text())
    assert updated_session["last_error"]["error_code"] == "capture_failed"


def test_sync_recovers_spooled_capture_after_copy_failure(tmp_path: Path) -> None:
    session = create_session(tmp_path, session_type="bulk_fermentation", name="Spool test")

    def failing_copy_runner(command, **kwargs):
        if command[0] == "ssh":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[0] == "scp":
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="Read from remote host davesdev: Operation timed out")
        raise AssertionError(f"unexpected command: {command}")

    failed = capture_now(tmp_path, session_id=session["session_id"], device_id="DavesDev", runner=failing_copy_runner, attempts=1)
    assert failed["status"] == "error"
    session_after_failure = json.loads((session_dir(tmp_path, session["session_id"]) / "session.json").read_text())
    assert len(session_after_failure["spooled_captures"]) == 1

    def successful_sync_runner(command, **kwargs):
        assert command[0] == "scp"
        destination = Path(command[-1])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"recovered-jpeg")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    synced = sync_spooled_captures(tmp_path, session_id=session["session_id"], runner=successful_sync_runner)

    assert synced["status"] == "ok"
    assert len(synced["synced"]) == 1
    local_path = Path(synced["synced"][0]["local_path"])
    assert local_path.read_bytes() == b"recovered-jpeg"
    session_after_sync = json.loads((session_dir(tmp_path, session["session_id"]) / "session.json").read_text())
    assert session_after_sync["capture_count"] == 1
    assert session_after_sync["spooled_captures"] == []


def test_health_retries_transient_ssh_failure(tmp_path: Path) -> None:
    calls = {"count": 0}

    def fake_runner(command, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return subprocess.CompletedProcess(command, 255, stdout="", stderr="Connection timed out during banner exchange")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "hostname=DavesDev\n"
                "time=2026-07-12T20:00:00Z\n"
                "disk=123 456 789 10% /\n"
                "camera_tool=/usr/bin/rpicam-still\n"
                "camera_probe=Available cameras|0 : imx708 [4608x2592]|\n"
                "video_devices=/dev/video0\n"
            ),
            stderr="",
        )

    payload = health_check(tmp_path, device_id="DavesDev", runner=fake_runner, attempts=2)

    assert payload["status"] == "ok"
    assert calls["count"] == 2
    assert [event["stage"] for event in payload["trace"][:2]] == ["ssh_health_probe_attempt", "ssh_health_probe_attempt"]


def test_bake_cam_cli_session_status_flow(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    monkeypatch.setenv("LAGENT_CONFIG", str(config_path))
    runner = CliRunner()

    created = runner.invoke(
        app,
        [
            "bake-cam",
            "start-session",
            "--type",
            "starter_feeding",
            "--name",
            "CLI starter feed",
            "--json",
        ],
    )
    assert created.exit_code == 0, created.output
    created_payload = json.loads(created.output)
    assert created_payload["session"]["activity_type"] == "starter_feeding"

    listed = runner.invoke(app, ["bake-cam", "list-sessions", "--json"])
    assert listed.exit_code == 0, listed.output
    listed_payload = json.loads(listed.output)
    assert listed_payload["sessions"][0]["session_id"] == created_payload["session"]["session_id"]

    status = runner.invoke(app, ["bake-cam", "status"])
    assert status.exit_code == 0, status.output
    assert "active_sessions: 1" in status.output


def test_bake_cam_cli_schedule_flow(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    monkeypatch.setenv("LAGENT_CONFIG", str(config_path))
    runner = CliRunner()
    created = runner.invoke(
        app,
        ["bake-cam", "start-session", "--type", "starter_feeding", "--name", "CLI schedule feed", "--json"],
    )
    assert created.exit_code == 0, created.output
    session_id = json.loads(created.output)["session"]["session_id"]

    scheduled = runner.invoke(app, ["bake-cam", "schedule", "--session", session_id, "--every", "2h", "--until", "8h", "--json"])

    assert scheduled.exit_code == 0, scheduled.output
    payload = json.loads(scheduled.output)
    assert [item["offset_label"] for item in payload["capture_plan"]] == ["t+0h", "t+2h", "t+4h", "t+6h", "t+8h"]


def test_bake_cam_cli_start_session_accepts_started_at(tmp_path: Path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    monkeypatch.setenv("LAGENT_CONFIG", str(config_path))
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "bake-cam",
            "start-session",
            "--type",
            "starter_feeding",
            "--name",
            "Started at feed",
            "--started-at",
            "2026-07-12T17:30:00-07:00",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["session"]["started_at"] == "2026-07-12T17:30:00-07:00"
