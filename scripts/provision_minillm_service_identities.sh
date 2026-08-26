#!/usr/bin/env bash

# Administrator-reviewed macOS provisioning for the MiniLLM authority split.
set -euo pipefail

APPLY=false
CONFIRM=""
INSTALL_LAUNCH_DAEMON=false
BUILDER_USER="_minillm_builder"
PRODUCTION_USER="_minillm_prod"
BUILDER_UID="450"
PRODUCTION_UID="451"
BUILDER_GID="450"
PRODUCTION_GID="451"
PRODUCTION_RELEASE=""
PRODUCTION_PYTHON=""
PRODUCTION_CONFIG=""
SERVICE_LABEL="com.dalconzo.minillm.production"
PRODUCTION_HOST="127.0.0.1"
PRODUCTION_PORT="8765"
PRODUCTION_AUTH_MODE="oauth"

BUILDER_ROOT="/private/var/db/minillm-builder"
PRODUCTION_ROOT="/private/var/db/minillm-production"
BUILDER_CREDENTIALS="/Library/Application Support/MiniLLM/builder-credentials"
PRODUCTION_CREDENTIALS="/Library/Application Support/MiniLLM/production-credentials"
PRODUCTION_AUDIT="/private/var/log/minillm-production"
PRODUCTION_TUNNEL="/Library/Application Support/MiniLLM/production-tunnel"
DEPLOYMENT_CONTROL="/Library/Application Support/MiniLLM/deployment-control"
RELEASE_ROOT="/Library/Application Support/MiniLLM/releases"
PRODUCTION_PLIST="/Library/LaunchDaemons/com.dalconzo.minillm.production.plist"

usage() {
  cat <<'EOF'
usage: provision_minillm_service_identities.sh [options]

Dry-run is the default. Applying requires both:
  --apply
  --confirm CREATE_MINILLM_SERVICE_IDENTITIES

Identity options:
  --builder-uid ID             default: 450
  --builder-gid ID             default: 450
  --production-uid ID          default: 451
  --production-gid ID          default: 451

Optional disabled LaunchDaemon installation:
  --install-launch-daemon
  --production-release PATH    reviewed root-owned source release
  --production-python PATH     reviewed root-owned Python interpreter
  --production-config PATH     production-only configuration file

The script never loads, starts, or unloads a service; never migrates data;
never copies credentials; and never edits the existing user LaunchAgent.
EOF
}

die() {
  printf 'ERROR %s\n' "$*" >&2
  exit 2
}

require_number() {
  [[ "$2" =~ ^[0-9]+$ ]] || die "$1 must be numeric"
  (( $2 >= 400 )) || die "$1 must be at least 400"
}

require_safe_name() {
  [[ "$2" =~ ^_[a-z][a-z0-9_]{1,30}$ ]] || die "$1 is not an approved hidden service-account name"
}

require_absolute_path() {
  [[ "$2" == /* && "$2" != *$'\n'* && "$2" != *'/../'* && "$2" != */.. ]] || die "$1 must be a normalized absolute path"
}

while (( $# )); do
  case "$1" in
    --apply) APPLY=true; shift ;;
    --confirm) [[ $# -ge 2 ]] || die "--confirm requires a value"; CONFIRM=$2; shift 2 ;;
    --install-launch-daemon) INSTALL_LAUNCH_DAEMON=true; shift ;;
    --builder-uid) [[ $# -ge 2 ]] || die "--builder-uid requires a value"; BUILDER_UID=$2; shift 2 ;;
    --builder-gid) [[ $# -ge 2 ]] || die "--builder-gid requires a value"; BUILDER_GID=$2; shift 2 ;;
    --production-uid) [[ $# -ge 2 ]] || die "--production-uid requires a value"; PRODUCTION_UID=$2; shift 2 ;;
    --production-gid) [[ $# -ge 2 ]] || die "--production-gid requires a value"; PRODUCTION_GID=$2; shift 2 ;;
    --production-release) [[ $# -ge 2 ]] || die "--production-release requires a value"; PRODUCTION_RELEASE=$2; shift 2 ;;
    --production-python) [[ $# -ge 2 ]] || die "--production-python requires a value"; PRODUCTION_PYTHON=$2; shift 2 ;;
    --production-config) [[ $# -ge 2 ]] || die "--production-config requires a value"; PRODUCTION_CONFIG=$2; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ "$(/usr/bin/uname -s)" == "Darwin" ]] || die "this provisioning script supports macOS only"
require_safe_name builder_user "$BUILDER_USER"
require_safe_name production_user "$PRODUCTION_USER"
require_number builder_uid "$BUILDER_UID"
require_number builder_gid "$BUILDER_GID"
require_number production_uid "$PRODUCTION_UID"
require_number production_gid "$PRODUCTION_GID"
[[ "$BUILDER_UID" != "$PRODUCTION_UID" ]] || die "builder and production UIDs must differ"
[[ "$BUILDER_GID" != "$PRODUCTION_GID" ]] || die "builder and production GIDs must differ"

for declared_path in \
  "$BUILDER_ROOT" "$PRODUCTION_ROOT" "$BUILDER_CREDENTIALS" \
  "$PRODUCTION_CREDENTIALS" "$PRODUCTION_AUDIT" "$PRODUCTION_TUNNEL" \
  "$DEPLOYMENT_CONTROL" "$RELEASE_ROOT" "$PRODUCTION_PLIST"; do
  require_absolute_path path "$declared_path"
done

if $INSTALL_LAUNCH_DAEMON; then
  [[ -n "$PRODUCTION_RELEASE" && -n "$PRODUCTION_PYTHON" && -n "$PRODUCTION_CONFIG" ]] || \
    die "LaunchDaemon installation requires --production-release, --production-python, and --production-config"
  require_absolute_path production_release "$PRODUCTION_RELEASE"
  require_absolute_path production_python "$PRODUCTION_PYTHON"
  require_absolute_path production_config "$PRODUCTION_CONFIG"
fi

printf '%s\n' \
  "MiniLLM service identity provisioning plan" \
  "apply=$APPLY" \
  "builder=$BUILDER_USER uid=$BUILDER_UID gid=$BUILDER_GID root=$BUILDER_ROOT" \
  "production=$PRODUCTION_USER uid=$PRODUCTION_UID gid=$PRODUCTION_GID root=$PRODUCTION_ROOT" \
  "release_root=$RELEASE_ROOT" \
  "deployment_control=$DEPLOYMENT_CONTROL" \
  "production_home_mcp=$PRODUCTION_HOST:$PRODUCTION_PORT auth_mode=$PRODUCTION_AUTH_MODE" \
  "install_launch_daemon=$INSTALL_LAUNCH_DAEMON" \
  "existing_user_launch_agent_untouched=true" \
  "credentials_copied=false" \
  "production_data_migrated=false" \
  "service_started=false"

if ! $APPLY; then
  printf 'DRY_RUN no host changes performed\n'
  exit 0
fi

[[ "$CONFIRM" == "CREATE_MINILLM_SERVICE_IDENTITIES" ]] || \
  die "--apply requires --confirm CREATE_MINILLM_SERVICE_IDENTITIES"
(( EUID == 0 )) || die "--apply must be run as root (use sudo after reviewing the dry run)"

directory_value() {
  /usr/bin/dscl . -read "$1" "$2" 2>/dev/null | /usr/bin/awk '{$1=""; sub(/^ /,""); print}'
}

assert_numeric_id_available() {
  local kind=$1 id=$2 expected_record=$3 attribute found
  attribute=$([[ "$kind" == Users ]] && printf UniqueID || printf PrimaryGroupID)
  found=$(
    /usr/bin/dscl . -list "/$kind" "$attribute" 2>/dev/null |
      /usr/bin/awk -v expected_id="$id" '$2 == expected_id {print $1}' || true
  )
  if [[ -n "$found" && "$found" != "$expected_record" ]]; then
    die "$kind numeric ID $id is already assigned to $found"
  fi
}

ensure_group() {
  local name=$1 gid=$2 record="/Groups/$1"
  assert_numeric_id_available Groups "$gid" "$name"
  if /usr/bin/dscl . -read "$record" >/dev/null 2>&1; then
    [[ "$(directory_value "$record" PrimaryGroupID)" == "$gid" ]] || die "existing group $name has a different GID"
    return
  fi
  /usr/bin/dscl . -create "$record"
  /usr/bin/dscl . -create "$record" PrimaryGroupID "$gid"
  /usr/bin/dscl . -create "$record" RealName "MiniLLM private service group"
  /usr/bin/dscl . -create "$record" Password "*"
}

ensure_user() {
  local name=$1 uid=$2 gid=$3 record="/Users/$1"
  assert_numeric_id_available Users "$uid" "$name"
  if /usr/bin/dscl . -read "$record" >/dev/null 2>&1; then
    [[ "$(directory_value "$record" UniqueID)" == "$uid" ]] || die "existing user $name has a different UID"
    [[ "$(directory_value "$record" PrimaryGroupID)" == "$gid" ]] || die "existing user $name has a different primary GID"
    return
  fi
  /usr/bin/dscl . -create "$record"
  /usr/bin/dscl . -create "$record" UniqueID "$uid"
  /usr/bin/dscl . -create "$record" PrimaryGroupID "$gid"
  /usr/bin/dscl . -create "$record" RealName "MiniLLM private service identity"
  /usr/bin/dscl . -create "$record" NFSHomeDirectory "/var/empty"
  /usr/bin/dscl . -create "$record" UserShell "/usr/bin/false"
  /usr/bin/dscl . -create "$record" IsHidden "1"
  /usr/bin/dscl . -create "$record" Password "*"
}

ensure_group "$BUILDER_USER" "$BUILDER_GID"
ensure_group "$PRODUCTION_USER" "$PRODUCTION_GID"
ensure_user "$BUILDER_USER" "$BUILDER_UID" "$BUILDER_GID"
ensure_user "$PRODUCTION_USER" "$PRODUCTION_UID" "$PRODUCTION_GID"

/usr/bin/install -d -o "$BUILDER_USER" -g "$BUILDER_USER" -m 0700 "$BUILDER_ROOT" "$BUILDER_CREDENTIALS"
/usr/bin/install -d -o "$PRODUCTION_USER" -g "$PRODUCTION_USER" -m 0700 \
  "$PRODUCTION_ROOT" "$PRODUCTION_CREDENTIALS" "$PRODUCTION_AUDIT" "$PRODUCTION_TUNNEL"
/usr/bin/install -d -o root -g wheel -m 0700 \
  "$DEPLOYMENT_CONTROL" "$DEPLOYMENT_CONTROL/audit" \
  "$DEPLOYMENT_CONTROL/approvals" "$DEPLOYMENT_CONTROL/approvals/pending" \
  "$DEPLOYMENT_CONTROL/approvals/consumed" "$DEPLOYMENT_CONTROL/operations"
/usr/bin/install -d -o root -g wheel -m 0755 "$RELEASE_ROOT"

if $INSTALL_LAUNCH_DAEMON; then
  [[ -d "$PRODUCTION_RELEASE" ]] || die "production release must resolve to a directory"
  if [[ -L "$PRODUCTION_RELEASE" ]]; then
    [[ "$PRODUCTION_RELEASE" == "$RELEASE_ROOT/current" ]] || die "only the controlled releases/current pointer may be a release symlink"
    release_target=$(/usr/bin/readlink "$PRODUCTION_RELEASE")
    [[ "$release_target" =~ ^[0-9a-f]{40}$|^[0-9a-f]{64}$ ]] || die "releases/current must target a full immutable commit"
    [[ -d "$RELEASE_ROOT/$release_target" && ! -L "$RELEASE_ROOT/$release_target" ]] || die "releases/current target must be a real release directory"
  fi
  [[ -x "$PRODUCTION_PYTHON" && ! -L "$PRODUCTION_PYTHON" ]] || die "production Python must be a real executable"
  [[ -f "$PRODUCTION_CONFIG" && ! -L "$PRODUCTION_CONFIG" ]] || die "production config must be a real file"
  [[ "$(/usr/bin/stat -f '%u' "$PRODUCTION_RELEASE")" == 0 ]] || die "production release must be root-owned"
  [[ "$(/usr/bin/stat -f '%u' "$PRODUCTION_PYTHON")" == 0 ]] || die "production Python must be root-owned"
  [[ "$(/usr/bin/stat -f '%u' "$PRODUCTION_CONFIG")" == "$PRODUCTION_UID" ]] || die "production config must be production-owned"

  pending_plist="$DEPLOYMENT_CONTROL/$SERVICE_LABEL.plist.pending"
  /usr/bin/python3 - "$pending_plist" "$PRODUCTION_USER" "$PRODUCTION_RELEASE" "$PRODUCTION_PYTHON" "$PRODUCTION_CONFIG" "$PRODUCTION_AUDIT" "$SERVICE_LABEL" "$PRODUCTION_HOST" "$PRODUCTION_PORT" "$PRODUCTION_AUTH_MODE" <<'PY'
import plistlib
import sys

path, user, release, python, config, audit, label, host, port, auth_mode = sys.argv[1:]
payload = {
    "Label": label,
    "UserName": user,
    "GroupName": user,
    "ProgramArguments": [
        python, "-u", "-m", "local_agent_lab.cli", "home-mcp", "serve",
        "--host", host, "--port", port, "--auth-mode", auth_mode,
    ],
    "WorkingDirectory": release,
    "EnvironmentVariables": {
        "LAGENT_CONFIG": config,
        "PYTHONPATH": release.rstrip("/") + "/src",
        "PYTHONUNBUFFERED": "1",
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
    },
    "StandardOutPath": audit + "/home_mcp.stdout.log",
    "StandardErrorPath": audit + "/home_mcp.stderr.log",
    "Umask": 0o077,
    "RunAtLoad": False,
    "KeepAlive": True,
    "Disabled": True,
}
with open(path, "wb") as handle:
    plistlib.dump(payload, handle, fmt=plistlib.FMT_XML, sort_keys=False)
PY
  /usr/sbin/chown root:wheel "$pending_plist"
  /bin/chmod 0600 "$pending_plist"
  /usr/bin/plutil -lint "$pending_plist" >/dev/null
  /usr/bin/python3 - "$pending_plist" "$PRODUCTION_HOST" "$PRODUCTION_PORT" "$PRODUCTION_AUTH_MODE" <<'PY'
import plistlib
import sys

path, expected_host, expected_port, expected_auth_mode = sys.argv[1:]
with open(path, "rb") as handle:
    payload = plistlib.load(handle)
arguments = payload.get("ProgramArguments", [])
environment = payload.get("EnvironmentVariables", {})

def option_value(name):
    try:
        return str(arguments[arguments.index(name) + 1])
    except (ValueError, IndexError):
        return None

if option_value("--host") != expected_host or expected_host != "127.0.0.1":
    raise SystemExit("production Home MCP must bind to the approved loopback host")
if option_value("--port") != expected_port:
    raise SystemExit("production Home MCP port does not match the approved tunnel target")
if option_value("--auth-mode") != expected_auth_mode or expected_auth_mode != "oauth":
    raise SystemExit("production Home MCP must use trusted-proxy OAuth")
if "--auth-token" in arguments:
    raise SystemExit("production Home MCP plist must not contain a bearer token argument")
if any("TOKEN" in str(key).upper() for key in environment):
    raise SystemExit("OAuth production plist must not contain bearer-token environment entries")
PY
  /usr/bin/install -o root -g wheel -m 0600 "$pending_plist" "$PRODUCTION_PLIST"
fi

printf '%s\n' \
  "APPLIED identities_and_private_roots=true" \
  "launch_daemon_installed=$INSTALL_LAUNCH_DAEMON" \
  "service_started=false" \
  "NEXT run scripts/verify_service_identity_boundaries.sh as root" \
  "NEXT provision separate credentials directly into each private credential root" \
  "NEXT create a committed root-owned release before any production cutover"
