#!/usr/bin/env bash

# Install an immutable MiniLLM baseline without replacing the live user services.
set -euo pipefail

APPLY=false
CONFIRM=""
CANDIDATE_COMMIT=""
GIT_BUNDLE=""
GIT_BUNDLE_SHA256=""
WHEELHOUSE_ARCHIVE=""
WHEELHOUSE_SHA256=""
SOURCE_DATA="${MINILLM_SOURCE_DATA:-$PWD/data}"
SOURCE_TUNNEL_PROFILE="${MINILLM_TUNNEL_PROFILE:-$HOME/.config/tunnel-client/home-mcp.yaml}"
SOURCE_TUNNEL_ENV="${MINILLM_TUNNEL_ENV:-$HOME/.config/tunnel-client/home-mcp.env}"
SOURCE_TUNNEL_CLIENT="${MINILLM_TUNNEL_CLIENT:-$HOME/bin/tunnel-client}"
RESOURCE_URL=""

BUILDER_USER="_minillm_builder"
PRODUCTION_USER="_minillm_prod"
BUILDER_ROOT="/private/var/db/minillm-builder"
PRODUCTION_ROOT="/private/var/db/minillm-production"
RELEASE_ROOT="/Library/Application Support/MiniLLM/releases"
CONTROL_ROOT="/Library/Application Support/MiniLLM/deployment-control"
TRUSTED_REPOSITORY="/Library/Application Support/MiniLLM/trusted-repository"
BOOTSTRAP_ROOT="/Library/Application Support/MiniLLM/bootstrap"
PYTHON_RUNTIME="/Library/Application Support/MiniLLM/python-runtime"
TUNNEL_RUNTIME="/Library/Application Support/MiniLLM/tunnel-runtime"
PRODUCTION_TUNNEL="/Library/Application Support/MiniLLM/production-tunnel"
PRODUCTION_AUDIT="/private/var/log/minillm-production"
HOME_PLIST="/Library/LaunchDaemons/com.dalconzo.minillm.production.plist"
TUNNEL_PLIST="/Library/LaunchDaemons/com.dalconzo.minillm.production-tunnel.plist"
SHADOW_PORT=8766
SHADOW_PID=""

usage() {
  cat <<'EOF'
usage: bootstrap_minillm_production.sh [options]

Dry-run is the default. Applying requires both:
  --apply
  --confirm BOOTSTRAP_MINILLM_PRODUCTION

Required release inputs:
  --candidate-commit SHA       full 40- or 64-character Git commit
  --git-bundle PATH            reviewed Git bundle containing the commit
  --git-bundle-sha256 SHA      exact SHA-256 of the bundle
  --wheelhouse-archive PATH    tar archive containing only reviewed wheels
  --wheelhouse-sha256 SHA      exact SHA-256 of the wheelhouse archive
  --resource-url URL           public tunnel resource URL advertised by MCP

Optional source overrides:
  --source-data PATH
  --source-tunnel-profile PATH
  --source-tunnel-env PATH
  --source-tunnel-client PATH

The bootstrap creates a root-owned release and Python runtime, builder-owned
test runtime, production-owned config/data, and disabled system daemons. It
runs a production-identity shadow smoke test on port 8766. It never calls
launchctl and never stops, edits, or replaces the live user LaunchAgents.
EOF
}

die() {
  printf 'ERROR %s\n' "$*" >&2
  exit 2
}

require_value() {
  [[ -n "$2" ]] || die "$1 is required"
}

require_absolute_file() {
  [[ "$2" == /* && -f "$2" && ! -L "$2" ]] || die "$1 must be an absolute regular file: $2"
}

require_absolute_directory() {
  [[ "$2" == /* && -d "$2" && ! -L "$2" ]] || die "$1 must be an absolute real directory: $2"
}

sha256_file() {
  /usr/bin/python3 - "$1" <<'PY'
import hashlib
import sys

digest = hashlib.sha256()
with open(sys.argv[1], "rb") as handle:
    for block in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(block)
print(digest.hexdigest())
PY
}

secure_tree() {
  local root=$1 owner=$2 group=$3
  /usr/sbin/chown -R "$owner:$group" "$root"
  /usr/bin/find "$root" -type d -exec /bin/chmod 0700 {} +
  /usr/bin/find "$root" -type f ! -perm -0100 -exec /bin/chmod 0600 {} +
  /usr/bin/find "$root" -type f -perm -0100 -exec /bin/chmod 0700 {} +
}

run_as_service_identity() {
  local user=$1 home=$2 working_directory=$3
  shift 3
  [[ "$working_directory" == /* && -d "$working_directory" ]] || die "service working directory is invalid: $working_directory"
  /usr/bin/sudo -n -u "$user" -- /usr/bin/env -C "$working_directory" HOME="$home" \
    /bin/bash -c 'umask 077; exec "$@"' minillm-service "$@"
}

root_readonly_tree() {
  local root=$1
  /usr/sbin/chown -R root:wheel "$root"
  /usr/bin/find "$root" -type d -exec /bin/chmod 0755 {} +
  /usr/bin/find "$root" -type f -exec /bin/chmod 0644 {} +
  while IFS= read -r executable; do
    [[ -n "$executable" ]] && /bin/chmod 0755 "$root/$executable"
  done < <(
    /usr/bin/git -C "$TRUSTED_REPOSITORY" ls-tree -r "$CANDIDATE_COMMIT" |
      /usr/bin/awk '$1 == "100755" {print substr($0, index($0, "\t") + 1)}'
  )
}

extract_regular_tar() {
  local archive=$1 destination=$2
  /usr/bin/python3 - "$archive" "$destination" <<'PY'
import os
from pathlib import Path, PurePosixPath
import tarfile
import sys

archive = Path(sys.argv[1])
destination = Path(sys.argv[2])
destination.mkdir(parents=True, exist_ok=False)
with tarfile.open(archive, "r:*") as payload:
    members = payload.getmembers()
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise SystemExit(f"unsafe archive path: {member.name!r}")
        if not (member.isdir() or member.isfile()):
            raise SystemExit(f"archive contains a non-regular entry: {member.name!r}")
    payload.extractall(destination)
for path in (destination, *destination.rglob("*")):
    if path.is_symlink():
        raise SystemExit(f"archive extracted a symlink: {path}")
    os.chown(path, 0, 0)
PY
}

shadow_port_unused() {
  [[ -z "$(/usr/sbin/lsof -nP -t -iTCP:"$SHADOW_PORT" -sTCP:LISTEN 2>/dev/null)" ]]
}

terminate_process_tree() {
  local pid=$1 child
  while IFS= read -r child; do
    [[ -n "$child" ]] && terminate_process_tree "$child"
  done < <(/usr/bin/pgrep -P "$pid" 2>/dev/null || true)
  /bin/kill "$pid" >/dev/null 2>&1 || true
}

stop_shadow() {
  local pid=$SHADOW_PID
  if [[ -n "$pid" ]]; then
    # Killing only the background shell or sudo supervisor orphans the worker.
    terminate_process_tree "$pid"
    wait "$pid" >/dev/null 2>&1 || true
    SHADOW_PID=""
  fi
}

cleanup() {
  stop_shadow || true
  if [[ -n "${RELEASE_STAGING:-}" && -d "$RELEASE_STAGING" ]]; then
    /bin/rm -rf "$RELEASE_STAGING"
  fi
  if [[ -n "${RELEASE_ARCHIVE:-}" && -f "$RELEASE_ARCHIVE" ]]; then
    /bin/rm -f "$RELEASE_ARCHIVE"
  fi
}
trap cleanup EXIT

while (( $# )); do
  case "$1" in
    --apply) APPLY=true; shift ;;
    --confirm) [[ $# -ge 2 ]] || die "--confirm requires a value"; CONFIRM=$2; shift 2 ;;
    --candidate-commit) [[ $# -ge 2 ]] || die "--candidate-commit requires a value"; CANDIDATE_COMMIT=$2; shift 2 ;;
    --git-bundle) [[ $# -ge 2 ]] || die "--git-bundle requires a value"; GIT_BUNDLE=$2; shift 2 ;;
    --git-bundle-sha256) [[ $# -ge 2 ]] || die "--git-bundle-sha256 requires a value"; GIT_BUNDLE_SHA256=$2; shift 2 ;;
    --wheelhouse-archive) [[ $# -ge 2 ]] || die "--wheelhouse-archive requires a value"; WHEELHOUSE_ARCHIVE=$2; shift 2 ;;
    --wheelhouse-sha256) [[ $# -ge 2 ]] || die "--wheelhouse-sha256 requires a value"; WHEELHOUSE_SHA256=$2; shift 2 ;;
    --resource-url) [[ $# -ge 2 ]] || die "--resource-url requires a value"; RESOURCE_URL=$2; shift 2 ;;
    --source-data) [[ $# -ge 2 ]] || die "--source-data requires a value"; SOURCE_DATA=$2; shift 2 ;;
    --source-tunnel-profile) [[ $# -ge 2 ]] || die "--source-tunnel-profile requires a value"; SOURCE_TUNNEL_PROFILE=$2; shift 2 ;;
    --source-tunnel-env) [[ $# -ge 2 ]] || die "--source-tunnel-env requires a value"; SOURCE_TUNNEL_ENV=$2; shift 2 ;;
    --source-tunnel-client) [[ $# -ge 2 ]] || die "--source-tunnel-client requires a value"; SOURCE_TUNNEL_CLIENT=$2; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ "$(/usr/bin/uname -s)" == Darwin ]] || die "this bootstrap supports macOS only"
require_value candidate_commit "$CANDIDATE_COMMIT"
require_value git_bundle "$GIT_BUNDLE"
require_value git_bundle_sha256 "$GIT_BUNDLE_SHA256"
require_value wheelhouse_archive "$WHEELHOUSE_ARCHIVE"
require_value wheelhouse_sha256 "$WHEELHOUSE_SHA256"
require_value resource_url "$RESOURCE_URL"
[[ "$CANDIDATE_COMMIT" =~ ^[0-9a-f]{40}$|^[0-9a-f]{64}$ ]] || die "candidate commit must be a full lowercase Git object ID"
[[ "$GIT_BUNDLE_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "git bundle SHA-256 is invalid"
[[ "$WHEELHOUSE_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "wheelhouse SHA-256 is invalid"
[[ "$RESOURCE_URL" =~ ^https://api\.openai\.com/v1/tunnel/tunnel_[A-Za-z0-9]+$ ]] || die "resource URL is not an approved OpenAI tunnel URL"
require_absolute_file git_bundle "$GIT_BUNDLE"
require_absolute_file wheelhouse_archive "$WHEELHOUSE_ARCHIVE"
require_absolute_directory source_data "$SOURCE_DATA"
require_absolute_file source_tunnel_profile "$SOURCE_TUNNEL_PROFILE"
require_absolute_file source_tunnel_env "$SOURCE_TUNNEL_ENV"
require_absolute_file source_tunnel_client "$SOURCE_TUNNEL_CLIENT"

printf '%s\n' \
  "MiniLLM production bootstrap plan" \
  "apply=$APPLY" \
  "candidate_commit=$CANDIDATE_COMMIT" \
  "trusted_repository=$TRUSTED_REPOSITORY" \
  "release=$RELEASE_ROOT/$CANDIDATE_COMMIT" \
  "root_python_runtime=$PYTHON_RUNTIME" \
  "builder_runtime=$BUILDER_ROOT/venv" \
  "production_data=$PRODUCTION_ROOT/data" \
  "production_tunnel=$PRODUCTION_TUNNEL" \
  "shadow_port=$SHADOW_PORT" \
  "live_user_launch_agents_untouched=true" \
  "system_daemons_started=false"

if ! $APPLY; then
  printf 'DRY_RUN no host changes performed\n'
  exit 0
fi

[[ "$CONFIRM" == BOOTSTRAP_MINILLM_PRODUCTION ]] || die "--apply requires --confirm BOOTSTRAP_MINILLM_PRODUCTION"
(( EUID == 0 )) || die "--apply must run as root after review"
/usr/bin/install -d -o root -g wheel -m 0700 "$CONTROL_ROOT"
INPUT_ROOT="$CONTROL_ROOT/bootstrap-inputs/$CANDIDATE_COMMIT"
/usr/bin/install -d -o root -g wheel -m 0700 "$CONTROL_ROOT/bootstrap-inputs" "$INPUT_ROOT"
/usr/bin/install -o root -g wheel -m 0400 "$GIT_BUNDLE" "$INPUT_ROOT/candidate.bundle.pending"
/usr/bin/install -o root -g wheel -m 0400 "$WHEELHOUSE_ARCHIVE" "$INPUT_ROOT/wheelhouse.tar.gz.pending"
/bin/mv -f "$INPUT_ROOT/candidate.bundle.pending" "$INPUT_ROOT/candidate.bundle"
/bin/mv -f "$INPUT_ROOT/wheelhouse.tar.gz.pending" "$INPUT_ROOT/wheelhouse.tar.gz"
GIT_BUNDLE="$INPUT_ROOT/candidate.bundle"
WHEELHOUSE_ARCHIVE="$INPUT_ROOT/wheelhouse.tar.gz"
[[ "$(sha256_file "$GIT_BUNDLE")" == "$GIT_BUNDLE_SHA256" ]] || die "Git bundle digest mismatch"
[[ "$(sha256_file "$WHEELHOUSE_ARCHIVE")" == "$WHEELHOUSE_SHA256" ]] || die "wheelhouse digest mismatch"

/usr/bin/install -d -o root -g wheel -m 0755 "$RELEASE_ROOT" "$BOOTSTRAP_ROOT"

if [[ ! -e "$TRUSTED_REPOSITORY" ]]; then
  /usr/bin/git clone --no-checkout "$GIT_BUNDLE" "$TRUSTED_REPOSITORY" >/dev/null
else
  [[ ! -L "$TRUSTED_REPOSITORY" && "$(/usr/bin/stat -f '%u' "$TRUSTED_REPOSITORY")" == 0 ]] || die "trusted repository ownership is invalid"
  [[ -z "$(/usr/bin/git -C "$TRUSTED_REPOSITORY" status --porcelain=v1 --untracked-files=all)" ]] || die "trusted repository is not clean"
fi
/usr/bin/git -C "$TRUSTED_REPOSITORY" bundle verify "$GIT_BUNDLE" >/dev/null
/usr/bin/git -C "$TRUSTED_REPOSITORY" fetch --no-tags "$GIT_BUNDLE" "$CANDIDATE_COMMIT" >/dev/null
/usr/bin/git -C "$TRUSTED_REPOSITORY" checkout --detach "$CANDIDATE_COMMIT" >/dev/null
[[ "$(/usr/bin/git -C "$TRUSTED_REPOSITORY" rev-parse HEAD)" == "$CANDIDATE_COMMIT" ]] || die "trusted repository checkout does not match candidate"
[[ -z "$(/usr/bin/git -C "$TRUSTED_REPOSITORY" status --porcelain=v1 --untracked-files=all)" ]] || die "trusted repository is not clean"
/usr/bin/git -C "$TRUSTED_REPOSITORY" cat-file -e "$CANDIDATE_COMMIT^{commit}"
/usr/bin/git -C "$TRUSTED_REPOSITORY" config core.hooksPath /dev/null
/usr/bin/git -C "$TRUSTED_REPOSITORY" config core.fsmonitor false
/usr/bin/git -C "$TRUSTED_REPOSITORY" config core.filemode false
/usr/sbin/chown -R root:wheel "$TRUSTED_REPOSITORY"
/usr/bin/find "$TRUSTED_REPOSITORY" -type d -exec /bin/chmod 0700 {} +
/usr/bin/find "$TRUSTED_REPOSITORY" -type f -exec /bin/chmod 0600 {} +

RELEASE="$RELEASE_ROOT/$CANDIDATE_COMMIT"
if [[ ! -e "$RELEASE" ]]; then
  RELEASE_ARCHIVE="$CONTROL_ROOT/bootstrap-release-$CANDIDATE_COMMIT.tar"
  RELEASE_STAGING="$RELEASE_ROOT/.$CANDIDATE_COMMIT.bootstrap-$$"
  /usr/bin/git -C "$TRUSTED_REPOSITORY" archive --format=tar --output "$RELEASE_ARCHIVE" "$CANDIDATE_COMMIT" \
    README.md pyproject.toml uv.lock config docs examples prompts scripts src tests
  extract_regular_tar "$RELEASE_ARCHIVE" "$RELEASE_STAGING"
  root_readonly_tree "$RELEASE_STAGING"
  /bin/mv "$RELEASE_STAGING" "$RELEASE"
  RELEASE_STAGING=""
fi
[[ ! -L "$RELEASE" && "$(/usr/bin/stat -f '%u' "$RELEASE")" == 0 ]] || die "release ownership is invalid"
if /usr/bin/find "$RELEASE" -perm -0022 -print -quit | /usr/bin/grep -q .; then
  die "release contains group/other-writable entries"
fi

WHEELHOUSE_ROOT="$BOOTSTRAP_ROOT/$CANDIDATE_COMMIT/wheelhouse"
if [[ ! -e "$WHEELHOUSE_ROOT" ]]; then
  /usr/bin/install -d -o root -g wheel -m 0755 "$BOOTSTRAP_ROOT/$CANDIDATE_COMMIT"
  extract_regular_tar "$WHEELHOUSE_ARCHIVE" "$WHEELHOUSE_ROOT"
  if /usr/bin/find "$WHEELHOUSE_ROOT" -type f ! -name '*.whl' -print -quit | /usr/bin/grep -q .; then
    die "wheelhouse archive contains non-wheel files"
  fi
  /usr/sbin/chown -R root:wheel "$WHEELHOUSE_ROOT"
  /usr/bin/find "$WHEELHOUSE_ROOT" -type d -exec /bin/chmod 0755 {} +
  /usr/bin/find "$WHEELHOUSE_ROOT" -type f -exec /bin/chmod 0644 {} +
fi

if [[ ! -x "$PYTHON_RUNTIME/bin/python" ]]; then
  RUNTIME_STAGING="$BOOTSTRAP_ROOT/$CANDIDATE_COMMIT/python-runtime"
  [[ ! -e "$RUNTIME_STAGING" ]] || die "partial root Python runtime already exists"
  /opt/homebrew/bin/python3 -m venv --copies "$RUNTIME_STAGING"
  "$RUNTIME_STAGING/bin/python" -m pip install --no-index --no-cache-dir --only-binary=:all: \
    --find-links "$WHEELHOUSE_ROOT" local-agent-lab pytest >/dev/null
  "$RUNTIME_STAGING/bin/python" -c 'import local_agent_lab, pytest, typer, yaml'
  /usr/sbin/chown -R root:wheel "$RUNTIME_STAGING"
  /bin/chmod -R go-w "$RUNTIME_STAGING"
  /bin/mv "$RUNTIME_STAGING" "$PYTHON_RUNTIME"
fi
[[ ! -L "$PYTHON_RUNTIME/bin/python" && "$(/usr/bin/stat -f '%u' "$PYTHON_RUNTIME/bin/python")" == 0 ]] || die "production Python is not a root-owned real executable"

if [[ ! -x "$BUILDER_ROOT/venv/bin/python" ]]; then
  run_as_service_identity "$BUILDER_USER" "$BUILDER_ROOT" "$BUILDER_ROOT" \
    /opt/homebrew/bin/python3 -m venv --copies "$BUILDER_ROOT/venv"
fi
run_as_service_identity "$BUILDER_USER" "$BUILDER_ROOT" "$BUILDER_ROOT" \
  "$BUILDER_ROOT/venv/bin/python" -m pip install --no-index --no-cache-dir --only-binary=:all: \
  --find-links "$WHEELHOUSE_ROOT" local-agent-lab pytest >/dev/null
run_as_service_identity "$BUILDER_USER" "$BUILDER_ROOT" "$BUILDER_ROOT" \
  "$BUILDER_ROOT/venv/bin/python" -c 'import local_agent_lab, pytest, typer, yaml'
secure_tree "$BUILDER_ROOT" "$BUILDER_USER" "$BUILDER_USER"

/usr/bin/install -d -o "$PRODUCTION_USER" -g "$PRODUCTION_USER" -m 0700 \
  "$PRODUCTION_ROOT/config" "$PRODUCTION_ROOT/data" "$PRODUCTION_AUDIT" \
  "$PRODUCTION_TUNNEL" "$PRODUCTION_TUNNEL/profiles"
/usr/bin/install -o "$PRODUCTION_USER" -g "$PRODUCTION_USER" -m 0600 \
  "$RELEASE/config/agent.yaml" "$PRODUCTION_ROOT/config/agent.yaml"
/bin/cp -R "$RELEASE/config/prompts" "$PRODUCTION_ROOT/config/prompts"
/usr/bin/python3 - "$PRODUCTION_ROOT/config/agent.yaml" "$RESOURCE_URL" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
resource_url = sys.argv[2]
with path.open("a", encoding="utf-8") as handle:
    handle.write("\nhome_mcp:\n")
    handle.write(f"  resource_url: {resource_url!r}\n")
PY

if /usr/bin/find "$SOURCE_DATA" -type l -print -quit | /usr/bin/grep -q .; then
  die "source data contains symlinks"
fi
/usr/bin/rsync -rlpt --exclude local_staging --exclude logs --exclude patches --no-links \
  "$SOURCE_DATA/" "$PRODUCTION_ROOT/data/"
secure_tree "$PRODUCTION_ROOT" "$PRODUCTION_USER" "$PRODUCTION_USER"

/usr/bin/install -o "$PRODUCTION_USER" -g "$PRODUCTION_USER" -m 0600 \
  "$SOURCE_TUNNEL_PROFILE" "$PRODUCTION_TUNNEL/profiles/home-mcp.yaml"
/usr/bin/install -o "$PRODUCTION_USER" -g "$PRODUCTION_USER" -m 0600 \
  "$SOURCE_TUNNEL_ENV" "$PRODUCTION_TUNNEL/home-mcp.env"
/usr/bin/install -d -o root -g wheel -m 0755 "$TUNNEL_RUNTIME"
/usr/bin/install -o root -g wheel -m 0755 "$SOURCE_TUNNEL_CLIENT" "$TUNNEL_RUNTIME/tunnel-client"

CURRENT_TMP="$RELEASE_ROOT/.current-bootstrap-$$"
/bin/ln -s "$CANDIDATE_COMMIT" "$CURRENT_TMP"
/usr/bin/python3 - "$CURRENT_TMP" "$RELEASE_ROOT/current" <<'PY'
import os
import sys

os.replace(sys.argv[1], sys.argv[2])
PY
/usr/bin/install -o root -g wheel -m 0600 /dev/null "$CONTROL_ROOT/current_build"
/bin/echo "$CANDIDATE_COMMIT" > "$CONTROL_ROOT/current_build"

"$RELEASE/scripts/provision_minillm_service_identities.sh" \
  --apply --confirm CREATE_MINILLM_SERVICE_IDENTITIES \
  --builder-uid 450 --builder-gid 450 --production-uid 451 --production-gid 451 \
  --install-launch-daemon \
  --production-release "$RELEASE_ROOT/current" \
  --production-python "$PYTHON_RUNTIME/bin/python" \
  --production-config "$PRODUCTION_ROOT/config/agent.yaml" >/dev/null

/usr/bin/python3 - "$TUNNEL_PLIST" "$PRODUCTION_USER" "$RELEASE_ROOT/current" \
  "$PRODUCTION_ROOT" "$PRODUCTION_TUNNEL" "$TUNNEL_RUNTIME/tunnel-client" \
  "$PRODUCTION_AUDIT" "$RESOURCE_URL" <<'PY'
import plistlib
import sys

path, user, release, production_root, tunnel_root, client, audit, resource_url = sys.argv[1:]
tunnel_id = resource_url.rsplit("/", 1)[-1]
payload = {
    "Label": "com.dalconzo.minillm.production-tunnel",
    "UserName": user,
    "GroupName": user,
    "ProgramArguments": ["/bin/bash", release + "/scripts/home_mcp_tunnel.sh"],
    "WorkingDirectory": release,
    "EnvironmentVariables": {
        "HOME_MCP_ROOT": production_root,
        "HOME_MCP_TUNNEL_URL_FILE": production_root + "/data/home_mcp/current_tunnel_url.txt",
        "HOME_MCP_TUNNEL_LOG_FILE": audit + "/home_mcp_tunnel.log",
        "HOME_MCP_TUNNEL_CLIENT_ENV_FILE": tunnel_root + "/home-mcp.env",
        "HOME_MCP_TUNNEL_CLIENT_BIN": client,
        "HOME_MCP_TUNNEL_PROFILE": "home-mcp",
        "HOME_MCP_TUNNEL_PROFILE_DIR": tunnel_root + "/profiles",
        "HOME_MCP_TUNNEL_ID": tunnel_id,
        "HOME_MCP_PORT": "8765",
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
    },
    "StandardOutPath": audit + "/home_mcp_tunnel.stdout.log",
    "StandardErrorPath": audit + "/home_mcp_tunnel.stderr.log",
    "Umask": 0o077,
    "RunAtLoad": False,
    "KeepAlive": True,
    "Disabled": True,
}
with open(path, "wb") as handle:
    plistlib.dump(payload, handle, fmt=plistlib.FMT_XML, sort_keys=False)
PY
/usr/sbin/chown root:wheel "$TUNNEL_PLIST"
/bin/chmod 0600 "$TUNNEL_PLIST"
/usr/bin/plutil -lint "$HOME_PLIST" >/dev/null
/usr/bin/plutil -lint "$TUNNEL_PLIST" >/dev/null

SHADOW_LOG="$PRODUCTION_AUDIT/bootstrap-shadow.log"
/usr/bin/install -o "$PRODUCTION_USER" -g "$PRODUCTION_USER" -m 0600 /dev/null "$SHADOW_LOG"
shadow_port_unused || die "production shadow port $SHADOW_PORT is already in use"
run_as_service_identity "$PRODUCTION_USER" "$PRODUCTION_ROOT" "$RELEASE_ROOT/current" /usr/bin/env \
  "PYTHONPATH=$RELEASE_ROOT/current/src" \
  "LAGENT_CONFIG=$PRODUCTION_ROOT/config/agent.yaml" \
  "$PYTHON_RUNTIME/bin/python" -u -m local_agent_lab.cli home-mcp serve \
  --host 127.0.0.1 --port "$SHADOW_PORT" --auth-mode none >>"$SHADOW_LOG" 2>&1 &
SHADOW_PID=$!
for _ in $(/usr/bin/jot 60); do
  if /usr/bin/curl -fsS --max-time 2 "http://127.0.0.1:$SHADOW_PORT/health" >/dev/null 2>&1; then
    break
  fi
  /bin/sleep 1
done
/usr/bin/curl -fsS --max-time 3 "http://127.0.0.1:$SHADOW_PORT/health" >/dev/null || die "production shadow health check failed"
run_as_service_identity "$PRODUCTION_USER" "$PRODUCTION_ROOT" "$RELEASE_ROOT/current" /usr/bin/env \
  "PYTHONPATH=$RELEASE_ROOT/current/src" \
  "LAGENT_CONFIG=$PRODUCTION_ROOT/config/agent.yaml" \
  "$PYTHON_RUNTIME/bin/python" -m local_agent_lab.cli home-mcp smoke-test \
  --url "http://127.0.0.1:$SHADOW_PORT/mcp" >/dev/null
stop_shadow
for _ in $(/usr/bin/jot 20); do
  shadow_port_unused && break
  /bin/sleep 1
done
shadow_port_unused || die "production shadow process did not stop"
secure_tree "$PRODUCTION_ROOT" "$PRODUCTION_USER" "$PRODUCTION_USER"
secure_tree "$PRODUCTION_AUDIT" "$PRODUCTION_USER" "$PRODUCTION_USER"
secure_tree "$PRODUCTION_TUNNEL" "$PRODUCTION_USER" "$PRODUCTION_USER"

printf '%s\n' \
  "APPLIED production_bootstrap=true" \
  "candidate_commit=$CANDIDATE_COMMIT" \
  "shadow_smoke=passed" \
  "home_launch_daemon_installed_disabled=true" \
  "tunnel_launch_daemon_installed_disabled=true" \
  "live_user_launch_agents_untouched=true" \
  "production_cutover_performed=false" \
  "NEXT run the read-only identity verifier as root" \
  "NEXT perform a separately confirmed cutover with automatic rollback"
