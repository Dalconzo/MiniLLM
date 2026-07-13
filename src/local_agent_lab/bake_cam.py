from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence
from uuid import uuid4


SESSION_TYPES = {"starter_feeding", "bulk_fermentation", "final_proof", "bake", "misc"}
DEFAULT_DEVICES = {
    "DavesDev": {
        "device_id": "DavesDev",
        "ssh_target": "dave@DavesDev",
        "role": "raspberry_pi_camera",
        "default_camera_id": "main",
    }
}

Runner = Callable[..., subprocess.CompletedProcess[str]]


class BakeCamError(Exception):
    def __init__(self, message: str, *, stage: str, error_code: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.error_code = error_code
        self.details = details or {}

    def to_dict(self) -> dict:
        return {
            "message": str(self),
            "stage": self.stage,
            "error_code": self.error_code,
            "details": self.details,
        }


@dataclass(frozen=True)
class TraceEvent:
    stage: str
    status: str
    message: str
    details: dict
    timestamp: str

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "stage": self.stage,
            "status": self.status,
            "message": self.message,
            "details": self.details,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def baking_root(data_dir: Path) -> Path:
    root = data_dir / "baking_observations"
    root.mkdir(parents=True, exist_ok=True)
    (root / "sessions").mkdir(parents=True, exist_ok=True)
    (root / "devices").mkdir(parents=True, exist_ok=True)
    (root / "indexes").mkdir(parents=True, exist_ok=True)
    return root


def slugify(value: str, *, fallback: str = "item") -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-._")
    return slug[:80] or fallback


def session_dir(data_dir: Path, session_id: str) -> Path:
    return baking_root(data_dir) / "sessions" / slugify(session_id, fallback="session")


def session_path(data_dir: Path, session_id: str) -> Path:
    return session_dir(data_dir, session_id) / "session.json"


def create_session(
    data_dir: Path,
    *,
    session_type: str,
    name: str,
    recipe_id: str | None = None,
    batch_id: str | None = None,
    feeding_id: str | None = None,
    started_at: str | None = None,
) -> dict:
    if session_type not in SESSION_TYPES:
        raise BakeCamError(
            f"unsupported session type: {session_type}",
            stage="validate_session",
            error_code="invalid_session_type",
            details={"allowed": sorted(SESSION_TYPES)},
        )
    started = started_at or utc_now()
    date_prefix = started[:10].replace("-", "")
    session_id = f"{date_prefix}-{slugify(name, fallback=session_type)}-{uuid4().hex[:6]}"
    path = session_dir(data_dir, session_id)
    (path / "captures").mkdir(parents=True, exist_ok=True)
    (path / "latest").mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "session_id": session_id,
        "name": name,
        "activity_type": session_type,
        "recipe_id": recipe_id,
        "batch_id": batch_id,
        "feeding_id": feeding_id,
        "started_at": started,
        "created_at": utc_now(),
        "status": "active",
        "capture_count": 0,
        "last_capture_at": None,
        "last_error": None,
    }
    write_json(path / "session.json", payload)
    return payload


def load_session(data_dir: Path, session_id: str) -> dict:
    path = session_path(data_dir, session_id)
    if not path.exists():
        raise BakeCamError(
            f"session not found: {session_id}",
            stage="load_session",
            error_code="session_not_found",
            details={"session_id": session_id, "path": str(path)},
        )
    return json.loads(path.read_text(encoding="utf-8"))


def list_sessions(data_dir: Path, *, limit: int = 20) -> list[dict]:
    sessions_root = baking_root(data_dir) / "sessions"
    rows = []
    for path in sorted(sessions_root.glob("*/session.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        rows.append(json.loads(path.read_text(encoding="utf-8")))
        if len(rows) >= limit:
            break
    return rows


def latest_capture(data_dir: Path, *, session_id: str | None = None, camera_id: str | None = None) -> dict:
    sessions = [load_session(data_dir, session_id)] if session_id else list_sessions(data_dir, limit=100)
    candidates: list[tuple[str, Path, dict]] = []
    for session in sessions:
        captures_dir = session_dir(data_dir, session["session_id"]) / "captures"
        for meta_path in captures_dir.glob("*.json"):
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            if camera_id and metadata.get("camera_id") != camera_id:
                continue
            candidates.append((str(metadata.get("captured_at") or ""), meta_path, metadata))
    if not candidates:
        raise BakeCamError(
            "no captures found",
            stage="latest_capture",
            error_code="capture_not_found",
            details={"session_id": session_id, "camera_id": camera_id},
        )
    _timestamp, _meta_path, metadata = sorted(candidates, key=lambda item: item[0], reverse=True)[0]
    return metadata


def schedule_session(data_dir: Path, *, session_id: str, every: str | None = None, until: str | None = None, at: str | None = None) -> dict:
    session = load_session(data_dir, session_id)
    if at:
        offsets = [_parse_duration(item.strip()) for item in at.split(",") if item.strip()]
    elif every and until:
        step = _parse_duration(every)
        end = _parse_duration(until)
        if step <= 0:
            raise BakeCamError("schedule interval must be positive", stage="validate_schedule", error_code="invalid_interval")
        offsets = list(range(0, end + 1, step))
    else:
        raise BakeCamError(
            "provide either --at or both --every and --until",
            stage="validate_schedule",
            error_code="missing_schedule",
        )
    if not offsets:
        raise BakeCamError("schedule has no capture points", stage="validate_schedule", error_code="empty_schedule")

    existing_captures = _captures_by_elapsed(data_dir, session_id)
    plan = []
    for offset in sorted(set(offsets)):
        matching_capture = _capture_for_offset(existing_captures, offset)
        plan.append(
            {
                "offset_seconds": offset,
                "offset_label": format_duration(offset),
                "status": "done" if matching_capture else "pending",
                "capture_id": matching_capture.get("capture_id") if matching_capture else None,
            }
        )
    session["capture_plan"] = plan
    session["schedule"] = {"every": every, "until": until, "at": at, "updated_at": utc_now()}
    write_json(session_path(data_dir, session_id), session)
    return {"session_id": session_id, "capture_plan": plan, "schedule": session["schedule"]}


def sync_spooled_captures(
    data_dir: Path,
    *,
    session_id: str | None = None,
    runner: Runner = subprocess.run,
    timeout: int = 45,
    attempts: int = 3,
) -> dict:
    sessions = [load_session(data_dir, session_id)] if session_id else list_sessions(data_dir, limit=100)
    synced = []
    failed = []
    trace: list[TraceEvent] = []
    for session in sessions:
        remaining = []
        for item in session.get("spooled_captures", []):
            local_image = Path(item["local_path"])
            scp = _scp_command(resolve_device(data_dir, item["device_id"])["ssh_target"], item["source_device_path"], local_image)
            copy_result, copy_attempt_events = _run_with_retries(scp, runner=runner, timeout=timeout, attempts=attempts, stage="sync_copy")
            trace.extend(copy_attempt_events)
            if copy_result.returncode == 0:
                metadata = {**item, "upload_status": "ok", "error": None}
                local_meta = local_image.with_suffix(".json")
                write_json(local_meta, metadata)
                latest_dir = session_dir(data_dir, item["session_id"]) / "latest"
                latest_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(local_image, latest_dir / f"{slugify(item['camera_id'])}.jpg")
                write_json(latest_dir / f"{slugify(item['camera_id'])}.json", metadata)
                _mark_session_capture(data_dir, item["session_id"], item["captured_at"])
                synced.append(metadata)
                trace.append(_trace("sync_copy", "ok", "Synced spooled remote capture.", {"capture_id": item["capture_id"]}))
            else:
                remaining.append(item)
                failed.append(
                    {
                        "capture_id": item["capture_id"],
                        "session_id": item["session_id"],
                        "remote_path": item["source_device_path"],
                        "error": (copy_result.stderr or "").strip(),
                    }
                )
                trace.append(
                    _trace(
                        "sync_copy",
                        "error",
                        "Failed to sync spooled remote capture.",
                        {"capture_id": item["capture_id"], "stderr": (copy_result.stderr or "").strip()},
                    )
                )
        if session.get("spooled_captures") is not None:
            refreshed = load_session(data_dir, session["session_id"])
            refreshed["spooled_captures"] = remaining
            write_json(session_path(data_dir, session["session_id"]), refreshed)
    return {
        "status": "ok" if not failed else "degraded",
        "synced": synced,
        "failed": failed,
        "trace": [event.to_dict() for event in trace],
    }


def refresh_session_plan(data_dir: Path, session_id: str) -> dict:
    session = load_session(data_dir, session_id)
    plan = session.get("capture_plan") or []
    if not plan:
        return session
    existing_captures = _captures_by_elapsed(data_dir, session_id)
    refreshed = []
    for item in plan:
        offset = int(item["offset_seconds"])
        matching_capture = _capture_for_offset(existing_captures, offset)
        refreshed.append(
            {
                **item,
                "status": "done" if matching_capture else "pending",
                "capture_id": matching_capture.get("capture_id") if matching_capture else None,
            }
        )
    session["capture_plan"] = refreshed
    write_json(session_path(data_dir, session_id), session)
    return session


def format_duration(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"t+{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"t+{seconds // 60}m"
    return f"t+{seconds}s"


def list_devices(data_dir: Path) -> list[dict]:
    root = baking_root(data_dir) / "devices"
    existing = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in root.glob("*.json")}
    for device_id, payload in DEFAULT_DEVICES.items():
        existing.setdefault(device_id, payload)
    return sorted(existing.values(), key=lambda item: item["device_id"])


def resolve_device(data_dir: Path, device_id: str) -> dict:
    for device in list_devices(data_dir):
        if device["device_id"] == device_id:
            return device
    raise BakeCamError(
        f"unknown device: {device_id}",
        stage="resolve_device",
        error_code="unknown_device",
        details={"device_id": device_id, "known_devices": [item["device_id"] for item in list_devices(data_dir)]},
    )


def health_check(data_dir: Path, *, device_id: str, runner: Runner = subprocess.run, timeout: int = 12, attempts: int = 3) -> dict:
    device = resolve_device(data_dir, device_id)
    ssh_target = device["ssh_target"]
    trace: list[TraceEvent] = []

    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={timeout}",
        ssh_target,
        (
            "printf 'hostname='; hostname; "
            "printf 'time='; date -u +%Y-%m-%dT%H:%M:%SZ; "
            "printf 'disk='; df -Pk . | tail -1; "
            "printf 'camera_tool='; "
            "(command -v rpicam-still || command -v libcamera-still || command -v fswebcam || true); "
            "printf 'camera_probe='; "
            "((command -v rpicam-hello >/dev/null && rpicam-hello --list-cameras 2>&1) || "
            "(command -v libcamera-hello >/dev/null && libcamera-hello --list-cameras 2>&1) || true) | tr '\\n' '|'; "
            "printf '\\n'; "
            "printf 'video_devices='; ls /dev/video* 2>/dev/null | tr '\\n' ' '; printf '\\n'"
        ),
    ]
    result, attempt_events = _run_with_retries(command, runner=runner, timeout=timeout + 3, attempts=attempts, stage="ssh_health_probe")
    trace.extend(attempt_events)
    ok = result.returncode == 0
    trace.append(
        _trace(
            "ssh_health_probe",
            "ok" if ok else "error",
            "Ran remote camera health probe." if ok else "Remote camera health probe failed.",
            {"returncode": result.returncode, "stderr": result.stderr.strip()},
        )
    )
    parsed = _parse_probe_output(result.stdout)
    camera_probe = parsed.get("camera_probe", "")
    video_devices = parsed.get("video_devices", "")
    has_uvc_camera = "/dev/video0" in video_devices
    has_csi_camera = bool(parsed.get("camera_tool")) and "no cameras available" not in camera_probe.lower()
    camera_available = has_uvc_camera or has_csi_camera
    status = "ok" if ok and camera_available else "degraded" if ok else "error"
    payload = {
        "status": status,
        "device": device,
        "ssh_ok": ok,
        "camera_available": camera_available,
        "camera_mode": "uvc" if has_uvc_camera else "libcamera" if has_csi_camera else None,
        "probe": parsed,
        "trace": [event.to_dict() for event in trace],
    }
    return payload


def capture_now(
    data_dir: Path,
    *,
    session_id: str,
    device_id: str,
    camera_id: str = "main",
    runner: Runner = subprocess.run,
    timeout: int = 45,
    attempts: int = 3,
) -> dict:
    session = load_session(data_dir, session_id)
    device = resolve_device(data_dir, device_id)
    ssh_target = device["ssh_target"]
    trace: list[TraceEvent] = []
    captured_at = utc_now()
    elapsed_seconds = _elapsed_seconds(session.get("started_at"), captured_at)
    capture_id = f"cap-{captured_at.replace(':', '').replace('+00:00', 'Z')}-{slugify(camera_id)}-{uuid4().hex[:6]}"
    remote_dir = f"/tmp/lagent-bake-cam/{slugify(session_id)}"
    remote_path = f"{remote_dir}/{capture_id}.jpg"
    local_base = session_dir(data_dir, session_id)
    local_image = local_base / "captures" / f"{capture_id}.jpg"
    local_meta = local_base / "captures" / f"{capture_id}.json"

    remote_capture = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=12",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=2",
        ssh_target,
        (
            f"mkdir -p {remote_dir!r} && "
            "("
            f"(test -e /dev/video0 && command -v ffmpeg >/dev/null && timeout 12s ffmpeg -hide_banner -loglevel error -y -f v4l2 -input_format mjpeg -video_size 1280x720 -i /dev/video0 -frames:v 1 {remote_path!r}) || "
            f"(test -e /dev/video0 && command -v ffmpeg >/dev/null && timeout 12s ffmpeg -hide_banner -loglevel error -y -f v4l2 -video_size 640x480 -i /dev/video0 -frames:v 1 {remote_path!r}) || "
            f"(command -v rpicam-still >/dev/null && timeout 12s rpicam-still -n -t 1000 -o {remote_path!r}) || "
            f"(command -v libcamera-still >/dev/null && timeout 12s libcamera-still -n -t 1000 -o {remote_path!r}) || "
            f"(command -v fswebcam >/dev/null && timeout 12s fswebcam -r 1920x1080 --no-banner {remote_path!r})"
            ") && "
            f"test -s {remote_path!r}"
        ),
    ]
    result, attempt_events = _run_with_retries(remote_capture, runner=runner, timeout=timeout, attempts=attempts, stage="remote_capture")
    trace.extend(attempt_events)
    trace.append(
        _trace(
            "remote_capture",
            "ok" if result.returncode == 0 else "error",
            "Captured remote still image." if result.returncode == 0 else "Remote still capture failed.",
            {"returncode": result.returncode, "stderr": result.stderr.strip(), "remote_path": remote_path},
        )
    )
    if result.returncode != 0:
        error = {
            "message": "remote still capture failed",
            "stage": "remote_capture",
            "error_code": "capture_failed",
            "details": {"stderr": result.stderr.strip(), "remote_path": remote_path},
        }
        _mark_session_error(data_dir, session_id, error)
        return _capture_error_payload(session_id, device, camera_id, trace, error)

    metadata = {
        "schema_version": 1,
        "capture_id": capture_id,
        "session_id": session_id,
        "device_id": device_id,
        "camera_id": camera_id,
        "captured_at": captured_at,
        "elapsed_seconds": elapsed_seconds,
        "activity_type": session.get("activity_type"),
        "recipe_id": session.get("recipe_id"),
        "batch_id": session.get("batch_id"),
        "feeding_id": session.get("feeding_id"),
        "local_path": str(local_image),
        "source_device_path": remote_path,
        "upload_status": "pending",
        "error": None,
    }
    _append_spooled_capture(data_dir, session_id, metadata)

    scp = _scp_command(ssh_target, remote_path, local_image)
    copy_result, copy_attempt_events = _run_with_retries(scp, runner=runner, timeout=timeout, attempts=attempts, stage="copy_capture")
    trace.extend(copy_attempt_events)
    trace.append(
        _trace(
            "copy_capture",
            "ok" if copy_result.returncode == 0 else "error",
            "Copied capture to Mac mini storage." if copy_result.returncode == 0 else "Failed to copy capture to Mac mini storage.",
            {"returncode": copy_result.returncode, "stderr": copy_result.stderr.strip(), "local_path": str(local_image)},
        )
    )
    if copy_result.returncode != 0:
        error = {
            "message": "capture copy failed",
            "stage": "copy_capture",
            "error_code": "copy_failed",
            "details": {"stderr": copy_result.stderr.strip(), "local_path": str(local_image), "remote_path": remote_path},
        }
        _mark_session_error(data_dir, session_id, error)
        return _capture_error_payload(session_id, device, camera_id, trace, error)

    metadata["upload_status"] = "ok"
    write_json(local_meta, metadata)
    latest_dir = local_base / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(local_image, latest_dir / f"{slugify(camera_id)}.jpg")
    write_json(latest_dir / f"{slugify(camera_id)}.json", metadata)
    _mark_session_capture(data_dir, session_id, captured_at)
    _remove_spooled_capture(data_dir, session_id, capture_id)
    trace.append(_trace("write_metadata", "ok", "Wrote capture metadata and latest pointers.", {"metadata_path": str(local_meta)}))
    return {"status": "ok", "session_id": session_id, "capture": metadata, "trace": [event.to_dict() for event in trace]}


def status_summary(data_dir: Path) -> dict:
    sessions = [refresh_session_plan(data_dir, item["session_id"]) for item in list_sessions(data_dir, limit=10)]
    latest = None
    try:
        latest = latest_capture(data_dir)
    except BakeCamError:
        latest = None
    return {
        "status": "ok",
        "devices": list_devices(data_dir),
        "active_sessions": [item for item in sessions if item.get("status") == "active"],
        "recent_sessions": sessions,
        "latest_capture": latest,
    }


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_trace(path: Path, events: Sequence[dict]) -> None:
    path.write_text("".join(json.dumps(event, sort_keys=True) + "\n" for event in events), encoding="utf-8")


def _run(command: list[str], *, runner: Runner, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return runner(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(command, returncode=124, stdout=exc.stdout or "", stderr=str(exc))


def _scp_command(ssh_target: str, remote_path: str, local_path: Path) -> list[str]:
    return [
        "scp",
        "-O",
        "-q",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=12",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=2",
        f"{ssh_target}:{remote_path}",
        str(local_path),
    ]


def _run_with_retries(
    command: list[str],
    *,
    runner: Runner,
    timeout: int,
    attempts: int,
    stage: str,
) -> tuple[subprocess.CompletedProcess[str], list[TraceEvent]]:
    events: list[TraceEvent] = []
    attempts = max(1, attempts)
    result = _run(command, runner=runner, timeout=timeout)
    events.append(_attempt_event(stage, 1, attempts, result))
    for attempt in range(2, attempts + 1):
        if not _is_transient_transport_failure(result):
            break
        if runner is subprocess.run:
            time.sleep(min(2, attempt - 1))
        result = _run(command, runner=runner, timeout=timeout)
        events.append(_attempt_event(stage, attempt, attempts, result))
    return result, events


def _attempt_event(stage: str, attempt: int, attempts: int, result: subprocess.CompletedProcess[str]) -> TraceEvent:
    return _trace(
        f"{stage}_attempt",
        "ok" if result.returncode == 0 else "error",
        f"{stage} attempt {attempt}/{attempts} returned {result.returncode}.",
        {
            "attempt": attempt,
            "attempts": attempts,
            "returncode": result.returncode,
            "stderr": (result.stderr or "").strip(),
        },
    )


def _is_transient_transport_failure(result: subprocess.CompletedProcess[str]) -> bool:
    stderr = (result.stderr or "").lower()
    return any(
        marker in stderr
        for marker in [
            "timed out",
            "operation timed out",
            "connection timed out",
            "connection reset",
            "connection closed",
            "banner exchange",
            "no route to host",
            "network is unreachable",
            "could not resolve hostname",
            "broken pipe",
        ]
    )


def _trace(stage: str, status: str, message: str, details: dict | None = None) -> TraceEvent:
    return TraceEvent(stage=stage, status=status, message=message, details=details or {}, timestamp=utc_now())


def _parse_probe_output(output: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            parsed[key.strip()] = value.strip()
    return parsed


def _parse_duration(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", value.lower())
    if not match:
        raise BakeCamError(
            f"invalid duration: {value}",
            stage="validate_schedule",
            error_code="invalid_duration",
            details={"value": value},
        )
    amount = int(match.group(1))
    unit = match.group(2) or "s"
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return amount * multipliers[unit]


def _captures_by_elapsed(data_dir: Path, session_id: str) -> list[dict]:
    captures_dir = session_dir(data_dir, session_id) / "captures"
    captures = []
    for meta_path in captures_dir.glob("*.json"):
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        if metadata.get("elapsed_seconds") is not None:
            captures.append(metadata)
    return captures


def _capture_for_offset(captures: list[dict], offset_seconds: int) -> dict | None:
    for capture in captures:
        if abs(int(capture["elapsed_seconds"]) - offset_seconds) <= 60:
            return capture
    return None


def _elapsed_seconds(started_at: str | None, captured_at: str) -> int | None:
    if not started_at:
        return None
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((captured - start).total_seconds()))


def _mark_session_capture(data_dir: Path, session_id: str, captured_at: str) -> None:
    session = load_session(data_dir, session_id)
    session["capture_count"] = int(session.get("capture_count") or 0) + 1
    session["last_capture_at"] = captured_at
    session["last_error"] = None
    write_json(session_path(data_dir, session_id), session)


def _mark_session_error(data_dir: Path, session_id: str, error: dict) -> None:
    session = load_session(data_dir, session_id)
    session["last_error"] = error
    write_json(session_path(data_dir, session_id), session)


def _append_spooled_capture(data_dir: Path, session_id: str, metadata: dict) -> None:
    session = load_session(data_dir, session_id)
    spooled = [item for item in session.get("spooled_captures", []) if item.get("capture_id") != metadata["capture_id"]]
    spooled.append(metadata)
    session["spooled_captures"] = spooled
    write_json(session_path(data_dir, session_id), session)


def _remove_spooled_capture(data_dir: Path, session_id: str, capture_id: str) -> None:
    session = load_session(data_dir, session_id)
    session["spooled_captures"] = [item for item in session.get("spooled_captures", []) if item.get("capture_id") != capture_id]
    write_json(session_path(data_dir, session_id), session)


def _capture_error_payload(
    session_id: str,
    device: dict,
    camera_id: str,
    trace: list[TraceEvent],
    error: dict,
) -> dict:
    return {
        "status": "error",
        "session_id": session_id,
        "device": device,
        "camera_id": camera_id,
        "error": error,
        "trace": [event.to_dict() for event in trace],
    }
