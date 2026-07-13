# Baking Observation Workstations

This document defines the first-pass architecture for sourdough, proofing, and baking observation workstations.

The goal is to capture useful visual evidence for baking workflows without turning the recipe system into a black box. The first release should be boring: scheduled still images, inspectable local storage, clear grouping metadata, and simple commands to verify every step.

## Goals

- Capture still images of dough, starters, bakes, and other kitchen processes at predetermined times.
- Group captures by activity, date, recipe, feeding, dough batch, or elapsed time from a start event.
- Upload or sync captures into the Mac mini project storage for later browsing, recipe notes, and memory integration.
- Allow a live still snapshot from each camera workstation.
- Preserve enough metadata for local agents to reason about proofing progress later.
- Keep live video and computer-vision judgment as later versions.

## Hardware Roles

### Raspberry Pi Dev Kit 5

Primary v0 workstation.

- SSH target: `dave@DavesDev`
- Camera hardware: Arducam modules
- Responsibilities:
  - capture scheduled still images
  - write capture metadata
  - expose live snapshot command
  - sync captures to Mac mini storage
  - run local health checks for camera availability, disk, time sync, and upload status

### ESP32-S3 Boards

Secondary path after the Pi flow is proven.

- Use for low-cost fixed camera stations when image quality and network reliability are acceptable.
- Prefer still-image capture first.
- Do not block v0 on ESP32 firmware unless the Pi path fails to cover the core workflow.

## Storage Contract

Mac mini canonical storage should live under a dedicated data root:

```text
data/baking_observations/
  sessions/
    <session_id>/
      session.json
      captures/
        <elapsed_or_timestamp>_<camera_id>.jpg
        <elapsed_or_timestamp>_<camera_id>.json
      latest/
        <camera_id>.jpg
  devices/
    <device_id>.json
  indexes/
    captures.sqlite3
```

Each capture metadata file should include:

- `capture_id`
- `session_id`
- `device_id`
- `camera_id`
- `captured_at`
- `elapsed_seconds`
- `activity_type`
- `recipe_id`
- `batch_id`
- `feeding_id`
- `local_path`
- `source_device_path`
- `upload_status`
- `error`

## Session Types

Initial session types:

- `starter_feeding`
- `bulk_fermentation`
- `final_proof`
- `bake`
- `misc`

Sourdough starter sessions should support `t+hours` capture plans from a feeding start time. Baking and proofing sessions should also support absolute clock schedules.

## CLI Shape

Target commands:

```bash
lagent bake-cam devices
lagent bake-cam health --device DavesDev
lagent bake-cam start-session --type starter_feeding --name "rye starter 2026-07-12" --recipe-id optional
lagent bake-cam schedule --session <session-id> --every 30m --until 12h
lagent bake-cam capture-now --session <session-id> --device DavesDev --camera main
lagent bake-cam latest --device DavesDev --camera main
lagent bake-cam sync --device DavesDev
lagent bake-cam list-sessions
lagent bake-cam show-session <session-id>
```

Commands must print a `run_id` and write trace artifacts for any multi-step operation.

## Trace Requirements

Every capture/sync command should expose:

- SSH connection result
- camera detection result
- capture command executed
- source file path
- destination file path
- metadata write result
- upload/sync result
- elapsed timing
- error stage and error message on failure

The system should be debuggable from the terminal without needing to inspect the device manually.

## Unstable Network Handling

The Raspberry Pi may sit in a location where Tailscale/Wi-Fi is intermittent. The primary transport remains SSH over Tailscale, but the system should not assume continuous connectivity.

Near-term requirements:

- Retry transient SSH failures with bounded backoff.
- Keep remote captures in a device-local spool when copy-back fails.
- Make failed transfer recovery explicit through `sync`.
- Preserve the remote path in local trace/session errors.
- Treat Bluetooth or a nearby-device relay as a fallback for status metadata and delayed transfer, not as a live video path.

Bluetooth fallback should be evaluated after still capture is reliable. The useful target is small control/status messages and possibly low-rate thumbnail transfer; full-resolution image sync can wait for Wi-Fi/Tailscale recovery unless testing proves otherwise.

## V0 Acceptance

- The Mac mini repo has a documented storage contract for baking observations.
- The Pi can be reached over SSH.
- A command can capture one still image from the Pi.
- The image and metadata are copied to `data/baking_observations/sessions/<session_id>/`.
- `latest` returns or updates the latest still image for a camera.
- `health` reports SSH, camera availability, disk space, and time.
- Failures identify the stage that failed.

## Later Versions

- ESP32-S3 firmware path for still capture and upload.
- Mobile/Home MCP tools for starting sessions and retrieving latest images.
- Recipe attempt integration so capture sessions can attach to recipe notes.
- Memory integration for visual observations and session summaries.
- Local AI observation of image sequences.
- Live video feed for trusted local devices.
