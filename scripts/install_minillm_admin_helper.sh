#!/usr/bin/env bash

# Install or revoke the fixed MiniLLM passwordless administration surface.
set -euo pipefail

APPLY=false
UNINSTALL=false
CONFIRM=""
OPERATOR="${SUDO_USER:-}"
OPERATOR_UID=""
REQUEST_SOURCE_ROOT=""
HELPER_SOURCE=""
HELPER_SHA256=""
VERIFIER_SOURCE=""
VERIFIER_SHA256=""
CUTOVER_SOURCE=""
CUTOVER_SHA256=""

HELPER_PATH="/usr/local/sbin/minillm-admin"
SUDOERS_PATH="/etc/sudoers.d/minillm-admin"
CONTROL_ROOT="/Library/Application Support/MiniLLM/deployment-control"
ADMIN_ROOT="$CONTROL_ROOT/admin"
LIBEXEC_ROOT="$ADMIN_ROOT/libexec"
CONFIG_PATH="$ADMIN_ROOT/config.json"
TRUSTED_REPOSITORY="/Library/Application Support/MiniLLM/trusted-repository"
RELEASE_ROOT="/Library/Application Support/MiniLLM/releases"
PYTHON_RUNTIME="/Library/Application Support/MiniLLM/python-runtime"
PROMOTE_PATH="$PYTHON_RUNTIME/bin/minillm-promote"
PRODUCTION_ROOT="/private/var/db/minillm-production"
PRODUCTION_AUDIT="/private/var/log/minillm-production"
PRODUCTION_TUNNEL="/Library/Application Support/MiniLLM/production-tunnel"
BUILDER_ROOT="/private/var/db/minillm-builder"
BUILDER_CREDENTIALS="/Library/Application Support/MiniLLM/builder-credentials"
PRODUCTION_CREDENTIALS="/Library/Application Support/MiniLLM/production-credentials"
PRODUCTION_HOME_PLIST="/Library/LaunchDaemons/com.dalconzo.minillm.production.plist"
PRODUCTION_TUNNEL_PLIST="/Library/LaunchDaemons/com.dalconzo.minillm.production-tunnel.plist"
INSTALLED_VERIFIER="$LIBEXEC_ROOT/verify-service-identity-boundaries"
INSTALLED_CUTOVER="$LIBEXEC_ROOT/cutover-minillm-production"

usage() {
  cat <<'EOF'
usage: install_minillm_admin_helper.sh [options]

Dry-run is the default. Installation requires:
  --apply --confirm INSTALL_MINILLM_ADMIN_HELPER
  --helper-source PATH --helper-sha256 SHA256
  --verifier-source PATH --verifier-sha256 SHA256
  --cutover-source PATH --cutover-sha256 SHA256

Options:
  --operator NAME
  --operator-uid UID
  --request-source-root PATH

Revocation requires:
  --uninstall --confirm REVOKE_MINILLM_ADMIN_HELPER

The installer copies reviewed artifacts into root-owned locations and installs
an exact-command sudoers policy. It never starts, stops, or reloads a service.
The review and revoke operations remain passworded administrator actions and
are deliberately absent from the NOPASSWD allowlist.
EOF
}

die() {
  printf 'ERROR %s\n' "$*" >&2
  exit 2
}

sha256_file() {
  /usr/bin/shasum -a 256 "$1" | /usr/bin/awk '{print $1}'
}

require_absolute_regular_file() {
  local label=$1 path=$2
  [[ "$path" == /* && -f "$path" && ! -L "$path" ]] || die "$label must be an absolute real file: $path"
}

require_sha256() {
  [[ "$2" =~ ^[0-9a-f]{64}$ ]] || die "$1 is not a lowercase SHA-256"
}

while (( $# )); do
  case "$1" in
    --apply) APPLY=true; shift ;;
    --uninstall) UNINSTALL=true; shift ;;
    --confirm) [[ $# -ge 2 ]] || die "--confirm requires a value"; CONFIRM=$2; shift 2 ;;
    --operator) [[ $# -ge 2 ]] || die "--operator requires a value"; OPERATOR=$2; shift 2 ;;
    --operator-uid) [[ $# -ge 2 ]] || die "--operator-uid requires a value"; OPERATOR_UID=$2; shift 2 ;;
    --request-source-root) [[ $# -ge 2 ]] || die "--request-source-root requires a value"; REQUEST_SOURCE_ROOT=$2; shift 2 ;;
    --helper-source) [[ $# -ge 2 ]] || die "--helper-source requires a value"; HELPER_SOURCE=$2; shift 2 ;;
    --helper-sha256) [[ $# -ge 2 ]] || die "--helper-sha256 requires a value"; HELPER_SHA256=$2; shift 2 ;;
    --verifier-source) [[ $# -ge 2 ]] || die "--verifier-source requires a value"; VERIFIER_SOURCE=$2; shift 2 ;;
    --verifier-sha256) [[ $# -ge 2 ]] || die "--verifier-sha256 requires a value"; VERIFIER_SHA256=$2; shift 2 ;;
    --cutover-source) [[ $# -ge 2 ]] || die "--cutover-source requires a value"; CUTOVER_SOURCE=$2; shift 2 ;;
    --cutover-sha256) [[ $# -ge 2 ]] || die "--cutover-sha256 requires a value"; CUTOVER_SHA256=$2; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ "$(/usr/bin/uname -s)" == Darwin ]] || die "the MiniLLM admin helper targets macOS only"
[[ -n "$OPERATOR" ]] || die "operator is required; use sudo or pass --operator"
[[ "$OPERATOR" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] || die "operator name is invalid"
if [[ -z "$OPERATOR_UID" ]]; then
  OPERATOR_UID=$(/usr/bin/id -u "$OPERATOR" 2>/dev/null) || die "operator does not exist: $OPERATOR"
fi
[[ "$OPERATOR_UID" =~ ^[1-9][0-9]*$ ]] || die "operator UID is invalid"
[[ "$(/usr/bin/id -u "$OPERATOR")" == "$OPERATOR_UID" ]] || die "operator name and UID do not match"
if [[ -z "$REQUEST_SOURCE_ROOT" ]]; then
  OPERATOR_HOME=$(/usr/bin/dscl . -read "/Users/$OPERATOR" NFSHomeDirectory | /usr/bin/awk '{$1=""; sub(/^ /,""); print}')
  REQUEST_SOURCE_ROOT="$OPERATOR_HOME/MiniLLM/build/admin-requests"
fi
[[ "$REQUEST_SOURCE_ROOT" == /* && "$REQUEST_SOURCE_ROOT" != *'/../'* && "$REQUEST_SOURCE_ROOT" != */.. ]] || die "request source root is invalid"

if $UNINSTALL; then
  printf '%s\n' \
    "MiniLLM admin helper revocation plan" \
    "apply=$APPLY" \
    "sudoers_path=$SUDOERS_PATH" \
    "helper_path=$HELPER_PATH" \
    "audit_preserved=$ADMIN_ROOT/audit" \
    "pending_requests_preserved=$ADMIN_ROOT/requests"
  if ! $APPLY; then
    printf 'DRY_RUN no host changes performed\n'
    exit 0
  fi
  [[ "$CONFIRM" == REVOKE_MINILLM_ADMIN_HELPER ]] || die "--uninstall --apply requires --confirm REVOKE_MINILLM_ADMIN_HELPER"
  (( EUID == 0 )) || die "revocation must run as root"
  /bin/rm -f "$SUDOERS_PATH"
  /bin/rm -f "$HELPER_PATH" "$CONFIG_PATH" "$INSTALLED_VERIFIER" "$INSTALLED_CUTOVER"
  printf 'APPLIED admin_helper_revoked=true audit_preserved=true requests_preserved=true\n'
  exit 0
fi

require_absolute_regular_file helper_source "$HELPER_SOURCE"
require_absolute_regular_file verifier_source "$VERIFIER_SOURCE"
require_absolute_regular_file cutover_source "$CUTOVER_SOURCE"
require_sha256 helper_sha256 "$HELPER_SHA256"
require_sha256 verifier_sha256 "$VERIFIER_SHA256"
require_sha256 cutover_sha256 "$CUTOVER_SHA256"
[[ "$(sha256_file "$HELPER_SOURCE")" == "$HELPER_SHA256" ]] || die "helper source digest mismatch"
[[ "$(sha256_file "$VERIFIER_SOURCE")" == "$VERIFIER_SHA256" ]] || die "verifier source digest mismatch"
[[ "$(sha256_file "$CUTOVER_SOURCE")" == "$CUTOVER_SHA256" ]] || die "cutover source digest mismatch"
require_absolute_regular_file promote_path "$PROMOTE_PATH"
PROMOTE_SHA256=$(sha256_file "$PROMOTE_PATH")

printf '%s\n' \
  "MiniLLM admin helper installation plan" \
  "apply=$APPLY" \
  "operator=$OPERATOR uid=$OPERATOR_UID" \
  "helper_sha256=$HELPER_SHA256" \
  "verifier_sha256=$VERIFIER_SHA256" \
  "cutover_sha256=$CUTOVER_SHA256" \
  "promote_sha256=$PROMOTE_SHA256" \
  "request_source_root=$REQUEST_SOURCE_ROOT" \
  "passwordless_operations=status,verify,deploy-reviewed,cutover-reviewed,rollback,audit-verify" \
  "passworded_operations=review,revoke" \
  "service_changes=false"

if ! $APPLY; then
  printf 'DRY_RUN no host changes performed\n'
  exit 0
fi

[[ "$CONFIRM" == INSTALL_MINILLM_ADMIN_HELPER ]] || die "--apply requires --confirm INSTALL_MINILLM_ADMIN_HELPER"
(( EUID == 0 )) || die "installation must run as root"
[[ -x /usr/sbin/visudo ]] || die "visudo is unavailable"

/usr/bin/install -d -o root -g wheel -m 0755 /usr/local/sbin
/usr/bin/install -d -o "$OPERATOR" -g staff -m 0700 "$REQUEST_SOURCE_ROOT"
/usr/bin/install -d -o root -g wheel -m 0700 \
  "$ADMIN_ROOT" "$LIBEXEC_ROOT" "$ADMIN_ROOT/requests" \
  "$ADMIN_ROOT/requests/pending" "$ADMIN_ROOT/requests/consumed" \
  "$ADMIN_ROOT/requests/material" "$ADMIN_ROOT/results" "$ADMIN_ROOT/audit"
/usr/bin/install -o root -g wheel -m 0555 "$HELPER_SOURCE" "$HELPER_PATH.pending"
/usr/bin/install -o root -g wheel -m 0500 "$VERIFIER_SOURCE" "$INSTALLED_VERIFIER.pending"
/usr/bin/install -o root -g wheel -m 0500 "$CUTOVER_SOURCE" "$INSTALLED_CUTOVER.pending"
[[ "$(sha256_file "$HELPER_PATH.pending")" == "$HELPER_SHA256" ]] || die "installed helper digest mismatch"
[[ "$(sha256_file "$INSTALLED_VERIFIER.pending")" == "$VERIFIER_SHA256" ]] || die "installed verifier digest mismatch"
[[ "$(sha256_file "$INSTALLED_CUTOVER.pending")" == "$CUTOVER_SHA256" ]] || die "installed cutover digest mismatch"
/bin/mv -f "$HELPER_PATH.pending" "$HELPER_PATH"
/bin/mv -f "$INSTALLED_VERIFIER.pending" "$INSTALLED_VERIFIER"
/bin/mv -f "$INSTALLED_CUTOVER.pending" "$INSTALLED_CUTOVER"

SUDOERS_PENDING="$ADMIN_ROOT/minillm-admin.sudoers.pending"
/usr/bin/printf '%s\n' \
  "# Managed by install_minillm_admin_helper.sh; review/revoke intentionally omitted." \
  "$OPERATOR ALL=(root) NOPASSWD:NOSETENV: $HELPER_PATH status" \
  "$OPERATOR ALL=(root) NOPASSWD:NOSETENV: $HELPER_PATH verify" \
  "$OPERATOR ALL=(root) NOPASSWD:NOSETENV: $HELPER_PATH deploy-reviewed" \
  "$OPERATOR ALL=(root) NOPASSWD:NOSETENV: $HELPER_PATH cutover-reviewed" \
  "$OPERATOR ALL=(root) NOPASSWD:NOSETENV: $HELPER_PATH rollback" \
  "$OPERATOR ALL=(root) NOPASSWD:NOSETENV: $HELPER_PATH audit-verify" \
  > "$SUDOERS_PENDING"
/usr/sbin/chown root:wheel "$SUDOERS_PENDING"
/bin/chmod 0440 "$SUDOERS_PENDING"
/usr/sbin/visudo -cf "$SUDOERS_PENDING" >/dev/null
SUDOERS_SHA256=$(sha256_file "$SUDOERS_PENDING")
/usr/bin/install -o root -g wheel -m 0440 "$SUDOERS_PENDING" "$SUDOERS_PATH.pending"
/usr/sbin/visudo -cf "$SUDOERS_PATH.pending" >/dev/null
/bin/mv -f "$SUDOERS_PATH.pending" "$SUDOERS_PATH"
/bin/rm -f "$SUDOERS_PENDING"

CONFIG_PENDING="$ADMIN_ROOT/config.json.pending"
/usr/bin/python3 - \
  "$CONFIG_PENDING" "$OPERATOR" "$OPERATOR_UID" "$REQUEST_SOURCE_ROOT" \
  "$CONTROL_ROOT" "$TRUSTED_REPOSITORY" "$RELEASE_ROOT" "$PYTHON_RUNTIME" \
  "$PRODUCTION_ROOT" "$PRODUCTION_AUDIT" "$PRODUCTION_TUNNEL" "$BUILDER_ROOT" \
  "$BUILDER_CREDENTIALS" "$PRODUCTION_CREDENTIALS" "$PRODUCTION_HOME_PLIST" \
  "$PRODUCTION_TUNNEL_PLIST" "$HELPER_PATH" "$SUDOERS_PATH" "$INSTALLED_VERIFIER" \
  "$INSTALLED_CUTOVER" "$PROMOTE_PATH" "$HELPER_SHA256" "$SUDOERS_SHA256" \
  "$VERIFIER_SHA256" "$CUTOVER_SHA256" "$PROMOTE_SHA256" <<'PY'
import json
from pathlib import Path
import sys

(
    output, operator, operator_uid, request_source, control, repository, releases,
    runtime, production, production_audit, production_tunnel, builder,
    builder_credentials, production_credentials, home_plist, tunnel_plist,
    helper, sudoers, verifier, cutover, promote, helper_sha, sudoers_sha,
    verifier_sha, cutover_sha, promote_sha,
) = sys.argv[1:]
payload = {
    "schema_version": 1,
    "operator_name": operator,
    "operator_uid": int(operator_uid),
    "request_source_root": request_source,
    "control_root": control,
    "trusted_repository": repository,
    "release_root": releases,
    "python_runtime": runtime,
    "production_root": production,
    "production_audit": production_audit,
    "production_tunnel": production_tunnel,
    "builder_root": builder,
    "builder_credentials": builder_credentials,
    "production_credentials": production_credentials,
    "production_home_plist": home_plist,
    "production_tunnel_plist": tunnel_plist,
    "helper_path": helper,
    "sudoers_path": sudoers,
    "verifier_path": verifier,
    "cutover_path": cutover,
    "promote_path": promote,
    "artifact_sha256": {
        helper: helper_sha,
        sudoers: sudoers_sha,
        verifier: verifier_sha,
        cutover: cutover_sha,
        promote: promote_sha,
    },
}
Path(output).write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
PY
/usr/sbin/chown root:wheel "$CONFIG_PENDING"
/bin/chmod 0400 "$CONFIG_PENDING"
/bin/mv -f "$CONFIG_PENDING" "$CONFIG_PATH"

[[ "$(sha256_file "$HELPER_PATH")" == "$HELPER_SHA256" ]] || die "final helper digest mismatch"
[[ "$(sha256_file "$SUDOERS_PATH")" == "$SUDOERS_SHA256" ]] || die "final sudoers digest mismatch"
/usr/sbin/visudo -cf "$SUDOERS_PATH" >/dev/null

printf '%s\n' \
  "APPLIED minillm_admin_helper_installed=true" \
  "sudoers_valid=true" \
  "service_changes=false" \
  "NEXT create $REQUEST_SOURCE_ROOT/request.json and run a separately passworded review" \
  "NEXT use sudo -n $HELPER_PATH status"
