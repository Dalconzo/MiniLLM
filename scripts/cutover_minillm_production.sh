#!/usr/bin/env bash

# Atomically replace the user Home MCP/tunnel LaunchAgents with system daemons.
set -Eeuo pipefail

APPLY=false
CONFIRM=""
OPERATOR_USER="${MINILLM_OPERATOR_USER:-${SUDO_USER:-}}"
OPERATOR_HOME="${MINILLM_OPERATOR_HOME:-}"
SOURCE_REPOSITORY="${MINILLM_SOURCE_REPOSITORY:-}"
SOURCE_DATA="${MINILLM_SOURCE_DATA:-}"
USER_UID="${MINILLM_OPERATOR_UID:-}"
USER_HOME_PLIST="${MINILLM_USER_HOME_PLIST:-}"
USER_TUNNEL_PLIST="${MINILLM_USER_TUNNEL_PLIST:-}"
HOME_PLIST="/Library/LaunchDaemons/com.dalconzo.minillm.production.plist"
TUNNEL_PLIST="/Library/LaunchDaemons/com.dalconzo.minillm.production-tunnel.plist"
PRODUCTION_USER="_minillm_prod"
PRODUCTION_ROOT="/private/var/db/minillm-production"
PYTHON_RUNTIME="/Library/Application Support/MiniLLM/python-runtime"
RELEASE_ROOT="/Library/Application Support/MiniLLM/releases"
RELEASE_CURRENT="$RELEASE_ROOT/current"
BUILDER_ROOT="/private/var/db/minillm-builder"
BUILDER_CREDENTIALS="/Library/Application Support/MiniLLM/builder-credentials"
PRODUCTION_CREDENTIALS="/Library/Application Support/MiniLLM/production-credentials"
PRODUCTION_AUDIT="/private/var/log/minillm-production"
PRODUCTION_TUNNEL="/Library/Application Support/MiniLLM/production-tunnel"
DEPLOYMENT_CONTROL="/Library/Application Support/MiniLLM/deployment-control"
TRUSTED_REPOSITORY="/Library/Application Support/MiniLLM/trusted-repository"
HOME_LABEL="com.dalconzo.minillm.production"
TUNNEL_LABEL="com.dalconzo.minillm.production-tunnel"
USER_HOME_LABEL="com.dalconzo.minillm.home-mcp"
USER_TUNNEL_LABEL="com.dalconzo.minillm.home-mcp-tunnel"
PRODUCTION_HOST="127.0.0.1"
PRODUCTION_PORT="8765"
PRODUCTION_AUTH_MODE="oauth"

ROLLBACK_ARMED=false
CUTOVER_COMPLETE=false
USER_HOME_WAS_LOADED=false
USER_TUNNEL_WAS_LOADED=false

usage() {
  cat <<'EOF'
usage: cutover_minillm_production.sh [options]

Dry-run is the default. Applying requires both:
  --apply
  --confirm CUTOVER_MINILLM_PRODUCTION

Optional overrides:
  --operator-user NAME
  --operator-home PATH
  --source-repository PATH
  --user-uid UID
  --source-data PATH
  --user-home-plist PATH
  --user-tunnel-plist PATH

The cutover stops the user tunnel and Home MCP, copies a final consistent data
snapshot, starts the production Home MCP and tunnel system daemons, verifies
their process owners and local health, and runs the Home MCP smoke test. Any
failure automatically boots out the system daemons and restores the user
LaunchAgents. Source data and user plist files are retained for rollback.
EOF
}

die() {
  printf 'ERROR %s\n' "$*" >&2
  return 2
}

require_value() {
  [[ -n "$2" ]] || die "$1 is required"
}

require_safe_operator_user() {
  [[ "$1" =~ ^[A-Za-z_][A-Za-z0-9._-]{0,63}$ && "$1" != root ]] || die "operator user is invalid"
}

require_normalized_absolute_path() {
  [[ "$2" == /* && "$2" != *$'\n'* && "$2" != *'/../'* && "$2" != */.. && "$2" != *'/./'* && "$2" != */. ]] || \
    die "$1 must be a normalized absolute path"
}

directory_attribute() {
  /usr/bin/dscl . -read "/Users/$1" "$2" 2>/dev/null | /usr/bin/awk '{$1=""; sub(/^ /,""); print}'
}

resolve_operator_paths() {
  if [[ -z "$OPERATOR_USER" ]]; then
    (( EUID != 0 )) || die "root execution requires --operator-user or a validated SUDO_USER"
    OPERATOR_USER=$(/usr/bin/id -un)
  fi
  require_safe_operator_user "$OPERATOR_USER"
  if [[ -z "$OPERATOR_HOME" ]]; then
    OPERATOR_HOME=$(directory_attribute "$OPERATOR_USER" NFSHomeDirectory) || true
    require_value operator_home "$OPERATOR_HOME"
  fi
  if [[ -z "$USER_UID" ]]; then
    USER_UID=$(directory_attribute "$OPERATOR_USER" UniqueID) || true
    require_value user_uid "$USER_UID"
  fi
  require_normalized_absolute_path operator_home "$OPERATOR_HOME"
  SOURCE_REPOSITORY=${SOURCE_REPOSITORY:-"$OPERATOR_HOME/MiniLLM"}
  SOURCE_DATA=${SOURCE_DATA:-"$SOURCE_REPOSITORY/data"}
  USER_HOME_PLIST=${USER_HOME_PLIST:-"$OPERATOR_HOME/Library/LaunchAgents/$USER_HOME_LABEL.plist"}
  USER_TUNNEL_PLIST=${USER_TUNNEL_PLIST:-"$OPERATOR_HOME/Library/LaunchAgents/$USER_TUNNEL_LABEL.plist"}
  require_normalized_absolute_path source_repository "$SOURCE_REPOSITORY"
  require_normalized_absolute_path source_data "$SOURCE_DATA"
  require_normalized_absolute_path user_home_plist "$USER_HOME_PLIST"
  require_normalized_absolute_path user_tunnel_plist "$USER_TUNNEL_PLIST"
}

verify_production_oauth_contract() {
  /usr/bin/python3 - "$HOME_PLIST" "$TUNNEL_PLIST" "$PRODUCTION_HOST" "$PRODUCTION_PORT" "$PRODUCTION_AUTH_MODE" <<'PY'
import plistlib
import sys

home_path, tunnel_path, expected_host, expected_port, expected_auth_mode = sys.argv[1:]
with open(home_path, "rb") as handle:
    home = plistlib.load(handle)
with open(tunnel_path, "rb") as handle:
    tunnel = plistlib.load(handle)
arguments = home.get("ProgramArguments", [])
environment = home.get("EnvironmentVariables", {})
tunnel_environment = tunnel.get("EnvironmentVariables", {})

def option_value(name):
    try:
        return str(arguments[arguments.index(name) + 1])
    except (ValueError, IndexError):
        return None

if option_value("--host") != expected_host or expected_host != "127.0.0.1":
    raise SystemExit("production Home MCP plist is not loopback-bound")
if option_value("--port") != expected_port:
    raise SystemExit("production Home MCP plist port does not match the tunnel target")
if option_value("--auth-mode") != expected_auth_mode or expected_auth_mode != "oauth":
    raise SystemExit("production Home MCP plist is not trusted-proxy OAuth")
if "--auth-token" in arguments or any("TOKEN" in str(key).upper() for key in environment):
    raise SystemExit("production Home MCP plist contains forbidden bearer-token material")
if tunnel_environment.get("HOME_MCP_PORT") != expected_port:
    raise SystemExit("production tunnel does not target the production Home MCP port")
PY
}

verify_running_oauth_health() {
  local health
  health=$(/usr/bin/curl -fsS --max-time 3 "http://$PRODUCTION_HOST:$PRODUCTION_PORT/health") || \
    die "production Home MCP health check failed"
  /usr/bin/printf '%s' "$health" | /usr/bin/python3 -c '
import json
import sys

expected_auth_mode = sys.argv[1]
authentication = json.load(sys.stdin).get("authentication", {})
if authentication.get("mode") != expected_auth_mode or expected_auth_mode != "oauth":
    raise SystemExit("running production Home MCP did not report trusted-proxy OAuth")
if authentication.get("proxyHandled") is not True:
    raise SystemExit("running production Home MCP did not report proxy-handled OAuth")
if authentication.get("unauthenticatedMutations") is not False:
    raise SystemExit("running production Home MCP enabled unsafe unauthenticated mutations")
' "$PRODUCTION_AUTH_MODE"
}

service_loaded() {
  /bin/launchctl print "$1/$2" >/dev/null 2>&1
}

wait_for_health() {
  local url=$1
  for _ in $(/usr/bin/jot 60); do
    if /usr/bin/curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      return 0
    fi
    /bin/sleep 1
  done
  return 1
}

service_pid() {
  /bin/launchctl print "$1/$2" 2>/dev/null |
    /usr/bin/awk '$1 == "pid" && $2 == "=" {print $3; exit}'
}

assert_service_owner() {
  local domain=$1 label=$2 expected_uid=$3 pid actual_uid
  pid=$(service_pid "$domain" "$label")
  [[ "$pid" =~ ^[0-9]+$ ]] || die "could not determine PID for $domain/$label"
  actual_uid=$(/bin/ps -o uid= -p "$pid" | /usr/bin/tr -d ' ')
  [[ "$actual_uid" == "$expected_uid" ]] || die "$domain/$label runs as UID $actual_uid instead of $expected_uid"
  printf 'PASS process_owner label=%s pid=%s uid=%s\n' "$label" "$pid" "$actual_uid"
}

run_as_service_identity() {
  local user=$1 home=$2 working_directory=$3
  shift 3
  /usr/bin/sudo -n -u "$user" -- /usr/bin/env -C "$working_directory" HOME="$home" \
    /bin/bash -c 'umask 077; exec "$@"' minillm-service "$@"
}

secure_production_data() {
  /usr/sbin/chown -R "$PRODUCTION_USER:$PRODUCTION_USER" "$PRODUCTION_ROOT/data"
  /usr/bin/find "$PRODUCTION_ROOT/data" -type d -exec /bin/chmod 0700 {} +
  /usr/bin/find "$PRODUCTION_ROOT/data" -type f -exec /bin/chmod 0600 {} +
}

restore_user_services() {
  local user_domain="gui/$USER_UID" restore_failed=false
  /bin/launchctl bootout "system/$TUNNEL_LABEL" >/dev/null 2>&1 || true
  /bin/launchctl bootout "system/$HOME_LABEL" >/dev/null 2>&1 || true
  /bin/launchctl disable "system/$TUNNEL_LABEL" >/dev/null 2>&1 || true
  /bin/launchctl disable "system/$HOME_LABEL" >/dev/null 2>&1 || true

  if $USER_HOME_WAS_LOADED; then
    /bin/launchctl enable "$user_domain/$USER_HOME_LABEL" >/dev/null 2>&1 || restore_failed=true
    /bin/launchctl bootstrap "$user_domain" "$USER_HOME_PLIST" >/dev/null 2>&1 || restore_failed=true
    /bin/launchctl kickstart -k "$user_domain/$USER_HOME_LABEL" >/dev/null 2>&1 || restore_failed=true
    wait_for_health http://127.0.0.1:8765/health || restore_failed=true
  fi
  if $USER_TUNNEL_WAS_LOADED; then
    /bin/launchctl enable "$user_domain/$USER_TUNNEL_LABEL" >/dev/null 2>&1 || restore_failed=true
    /bin/launchctl bootstrap "$user_domain" "$USER_TUNNEL_PLIST" >/dev/null 2>&1 || restore_failed=true
    /bin/launchctl kickstart -k "$user_domain/$USER_TUNNEL_LABEL" >/dev/null 2>&1 || restore_failed=true
    wait_for_health http://127.0.0.1:8080/readyz || restore_failed=true
  fi
  ! $restore_failed
}

rollback_on_error() {
  local status=$?
  trap - ERR
  if $ROLLBACK_ARMED && ! $CUTOVER_COMPLETE; then
    printf 'ROLLBACK restoring_user_launch_agents=true\n' >&2
    if restore_user_services; then
      printf 'ROLLBACK completed=true user_health=passed tunnel_readiness=passed source_data_preserved=true\n' >&2
    else
      printf 'ROLLBACK completed=false manual_recovery_required=true source_data_preserved=true\n' >&2
    fi
  fi
  exit "$status"
}
trap rollback_on_error ERR

while (( $# )); do
  case "$1" in
    --apply) APPLY=true; shift ;;
    --confirm) [[ $# -ge 2 ]] || die "--confirm requires a value"; CONFIRM=$2; shift 2 ;;
    --operator-user) [[ $# -ge 2 ]] || die "--operator-user requires a value"; OPERATOR_USER=$2; shift 2 ;;
    --operator-home) [[ $# -ge 2 ]] || die "--operator-home requires a value"; OPERATOR_HOME=$2; shift 2 ;;
    --source-repository) [[ $# -ge 2 ]] || die "--source-repository requires a value"; SOURCE_REPOSITORY=$2; shift 2 ;;
    --user-uid) [[ $# -ge 2 ]] || die "--user-uid requires a value"; USER_UID=$2; shift 2 ;;
    --source-data) [[ $# -ge 2 ]] || die "--source-data requires a value"; SOURCE_DATA=$2; shift 2 ;;
    --user-home-plist) [[ $# -ge 2 ]] || die "--user-home-plist requires a value"; USER_HOME_PLIST=$2; shift 2 ;;
    --user-tunnel-plist) [[ $# -ge 2 ]] || die "--user-tunnel-plist requires a value"; USER_TUNNEL_PLIST=$2; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ "$(/usr/bin/uname -s)" == Darwin ]] || die "this cutover supports macOS only"
resolve_operator_paths
[[ "$USER_UID" =~ ^[0-9]+$ ]] || die "user UID must be numeric"

printf '%s\n' \
  "MiniLLM production cutover plan" \
  "apply=$APPLY" \
  "operator=$OPERATOR_USER uid=$USER_UID home=$OPERATOR_HOME" \
  "source_repository=$SOURCE_REPOSITORY" \
  "source_data=$SOURCE_DATA" \
  "production_data=$PRODUCTION_ROOT/data" \
  "user_home_label=gui/$USER_UID/$USER_HOME_LABEL" \
  "user_tunnel_label=gui/$USER_UID/$USER_TUNNEL_LABEL" \
  "production_home_label=system/$HOME_LABEL" \
  "production_tunnel_label=system/$TUNNEL_LABEL" \
  "automatic_user_service_rollback=true" \
  "source_data_preserved=true"

if ! $APPLY; then
  printf 'DRY_RUN no host changes performed\n'
  exit 0
fi

[[ "$CONFIRM" == CUTOVER_MINILLM_PRODUCTION ]] || die "--apply requires --confirm CUTOVER_MINILLM_PRODUCTION"
(( EUID == 0 )) || die "--apply must run as root after review"
DIRECTORY_HOME=$(directory_attribute "$OPERATOR_USER" NFSHomeDirectory) || die "operator user does not resolve through dscl"
DIRECTORY_UID=$(directory_attribute "$OPERATOR_USER" UniqueID) || die "operator user UID does not resolve through dscl"
[[ "$DIRECTORY_HOME" == "$OPERATOR_HOME" ]] || die "operator home does not match the dscl NFSHomeDirectory"
[[ "$DIRECTORY_UID" == "$USER_UID" ]] || die "operator UID does not match the dscl UniqueID"
[[ -d "$SOURCE_DATA" && ! -L "$SOURCE_DATA" ]] || die "source data must be a real directory"
[[ -d "$PRODUCTION_ROOT/data" && ! -L "$PRODUCTION_ROOT/data" ]] || die "production data is not bootstrapped"
[[ -x "$PYTHON_RUNTIME/bin/python" && ! -L "$PYTHON_RUNTIME/bin/python" ]] || die "root Python runtime is invalid"
[[ -L "$RELEASE_CURRENT" && -d "$RELEASE_CURRENT" ]] || die "current release pointer is invalid"
RELEASE_TARGET=$(/usr/bin/readlink "$RELEASE_CURRENT")
[[ "$RELEASE_TARGET" =~ ^[0-9a-f]{40}$|^[0-9a-f]{64}$ ]] || die "current release target is not an immutable commit"
PRODUCTION_RELEASE="$RELEASE_ROOT/$RELEASE_TARGET"
[[ -d "$PRODUCTION_RELEASE" && ! -L "$PRODUCTION_RELEASE" ]] || die "current production release is invalid"
[[ -f "$HOME_PLIST" && ! -L "$HOME_PLIST" ]] || die "production Home MCP plist is missing"
[[ -f "$TUNNEL_PLIST" && ! -L "$TUNNEL_PLIST" ]] || die "production tunnel plist is missing"
[[ "$(/usr/libexec/PlistBuddy -c 'Print :Umask' "$HOME_PLIST")" == 63 ]] || die "production Home MCP umask is not private"
[[ "$(/usr/libexec/PlistBuddy -c 'Print :Umask' "$TUNNEL_PLIST")" == 63 ]] || die "production tunnel umask is not private"
verify_production_oauth_contract
[[ -f "$USER_HOME_PLIST" && -f "$USER_TUNNEL_PLIST" ]] || die "rollback user plists are missing"
if /usr/bin/find "$SOURCE_DATA" -type l -print -quit | /usr/bin/grep -q .; then
  die "source data contains symlinks"
fi
service_loaded system "$HOME_LABEL" && die "production Home MCP is already loaded"
service_loaded system "$TUNNEL_LABEL" && die "production tunnel is already loaded"
wait_for_health http://127.0.0.1:8765/health || die "current user Home MCP is not healthy before cutover"

"$PRODUCTION_RELEASE/scripts/verify_service_identity_boundaries.sh" \
  --builder-user _minillm_builder \
  --production-user "$PRODUCTION_USER" \
  --builder-root "$BUILDER_ROOT" \
  --production-root "$PRODUCTION_ROOT" \
  --builder-credentials "$BUILDER_CREDENTIALS" \
  --production-credentials "$PRODUCTION_CREDENTIALS" \
  --production-audit "$PRODUCTION_AUDIT" \
  --production-tunnel "$PRODUCTION_TUNNEL" \
  --deployment-control "$DEPLOYMENT_CONTROL" \
  --production-plist "$HOME_PLIST" \
  --production-tunnel-plist "$TUNNEL_PLIST" \
  --trusted-repository "$TRUSTED_REPOSITORY" \
  --root-python-runtime "$PYTHON_RUNTIME" \
  --production-release "$PRODUCTION_RELEASE" || die "production identity boundary verification failed"

USER_HOME_WAS_LOADED=$(service_loaded "gui/$USER_UID" "$USER_HOME_LABEL" && printf true || printf false)
USER_TUNNEL_WAS_LOADED=$(service_loaded "gui/$USER_UID" "$USER_TUNNEL_LABEL" && printf true || printf false)
$USER_HOME_WAS_LOADED || die "user Home MCP is not loaded; refusing an unprovable rollback"
$USER_TUNNEL_WAS_LOADED || die "user tunnel is not loaded; refusing an unprovable rollback"
ROLLBACK_ARMED=true

/bin/launchctl bootout "gui/$USER_UID" "$USER_TUNNEL_PLIST"
/bin/launchctl disable "gui/$USER_UID/$USER_TUNNEL_LABEL"
/bin/launchctl bootout "gui/$USER_UID" "$USER_HOME_PLIST"
/bin/launchctl disable "gui/$USER_UID/$USER_HOME_LABEL"

/usr/bin/rsync -rlpt --exclude local_staging --exclude logs --exclude patches --no-links \
  "$SOURCE_DATA/" "$PRODUCTION_ROOT/data/"
secure_production_data

/bin/launchctl enable "system/$HOME_LABEL"
/bin/launchctl bootstrap system "$HOME_PLIST"
/bin/launchctl kickstart -k "system/$HOME_LABEL"
wait_for_health http://127.0.0.1:8765/health || die "production Home MCP health check failed"
verify_running_oauth_health

PRODUCTION_UID=$(/usr/bin/id -u "$PRODUCTION_USER")
assert_service_owner system "$HOME_LABEL" "$PRODUCTION_UID"
run_as_service_identity "$PRODUCTION_USER" "$PRODUCTION_ROOT" "$RELEASE_CURRENT" /usr/bin/env \
  "PYTHONPATH=$RELEASE_CURRENT/src" \
  "LAGENT_CONFIG=$PRODUCTION_ROOT/config/agent.yaml" \
  "$PYTHON_RUNTIME/bin/python" -m local_agent_lab.cli home-mcp smoke-test \
  --url http://127.0.0.1:8765/mcp >/dev/null
secure_production_data

/bin/launchctl enable "system/$TUNNEL_LABEL"
/bin/launchctl bootstrap system "$TUNNEL_PLIST"
/bin/launchctl kickstart -k "system/$TUNNEL_LABEL"
for _ in $(/usr/bin/jot 60); do
  service_loaded system "$TUNNEL_LABEL" && [[ -n "$(service_pid system "$TUNNEL_LABEL")" ]] && break
  /bin/sleep 1
done
assert_service_owner system "$TUNNEL_LABEL" "$PRODUCTION_UID"
wait_for_health http://127.0.0.1:8080/readyz || die "production tunnel readiness check failed"

CUTOVER_COMPLETE=true
ROLLBACK_ARMED=false
printf '%s\n' \
  "APPLIED production_cutover=true" \
  "home_mcp_health=passed" \
  "home_mcp_smoke=passed" \
  "home_mcp_owner_uid=$PRODUCTION_UID" \
  "tunnel_owner_uid=$PRODUCTION_UID" \
  "tunnel_readiness=passed" \
  "user_launch_agents_loaded=false" \
  "source_data_preserved=true" \
  "NEXT run browser connector acceptance against the unchanged tunnel URL"
