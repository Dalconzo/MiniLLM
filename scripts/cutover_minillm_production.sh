#!/usr/bin/env bash

# Atomically replace the user Home MCP/tunnel LaunchAgents with system daemons.
set -Eeuo pipefail

APPLY=false
CONFIRM=""
USER_UID=501
SOURCE_DATA="${MINILLM_SOURCE_DATA:-$PWD/data}"
USER_HOME="${MINILLM_USER_HOME:-$HOME}"
USER_HOME_PLIST="$USER_HOME/Library/LaunchAgents/com.dalconzo.minillm.home-mcp.plist"
USER_TUNNEL_PLIST="$USER_HOME/Library/LaunchAgents/com.dalconzo.minillm.home-mcp-tunnel.plist"
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
  --user-uid UID
  --source-data PATH

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
    --user-uid) [[ $# -ge 2 ]] || die "--user-uid requires a value"; USER_UID=$2; shift 2 ;;
    --source-data) [[ $# -ge 2 ]] || die "--source-data requires a value"; SOURCE_DATA=$2; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ "$(/usr/bin/uname -s)" == Darwin ]] || die "this cutover supports macOS only"
[[ "$USER_UID" =~ ^[0-9]+$ ]] || die "user UID must be numeric"

printf '%s\n' \
  "MiniLLM production cutover plan" \
  "apply=$APPLY" \
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
