#!/usr/bin/env bash

# Read-only host verifier for the MiniLLM builder/production identity boundary.
set -u
set -o pipefail

PASS_COUNT=0
FAIL_COUNT=0

usage() {
  cat <<'EOF'
usage: verify_service_identity_boundaries.sh [options]

Required options:
  --builder-user NAME
  --production-user NAME
  --builder-root PATH
  --production-root PATH
  --builder-credentials PATH
  --production-credentials PATH
  --production-audit PATH
  --production-tunnel PATH
  --deployment-control PATH
  --production-plist PATH
  --production-tunnel-plist PATH
  --trusted-repository PATH
  --root-python-runtime PATH
  --production-release PATH

The verifier must be run by an administrator so private trees can be fully
enumerated. It reads identity and filesystem metadata only; it never elevates,
changes permissions, reads file contents, or manages services.
EOF
}

pass_check() {
  PASS_COUNT=$((PASS_COUNT + 1))
  printf 'PASS %-34s %s\n' "$1" "$2"
}

fail_check() {
  FAIL_COUNT=$((FAIL_COUNT + 1))
  printf 'FAIL %-34s %s\n' "$1" "$2"
}

os_current_uid() {
  /usr/bin/id -u
}

os_uname() {
  /usr/bin/uname -s
}

os_id_uid() {
  /usr/bin/id -u "$1"
}

os_id_gid() {
  /usr/bin/id -g "$1"
}

os_id_groups() {
  /usr/bin/id -G "$1"
}

os_id_group_names() {
  /usr/bin/id -Gn "$1"
}

os_user_property() {
  /usr/bin/dscl . -read "/Users/$1" "$2" 2>/dev/null | /usr/bin/awk '{$1=""; sub(/^ /,""); print}'
}

os_plist_value() {
  /usr/libexec/PlistBuddy -c "Print :$1" "$2" 2>/dev/null
}

os_realpath() {
  local path=$1
  local parent base resolved_parent
  if [[ -d "$path" ]]; then
    (cd -P -- "$path" 2>/dev/null && /bin/pwd -P)
    return
  fi
  parent=${path%/*}
  base=${path##*/}
  [[ -n "$parent" ]] || parent=/
  resolved_parent=$(cd -P -- "$parent" 2>/dev/null && /bin/pwd -P) || return 1
  printf '%s/%s\n' "${resolved_parent%/}" "$base"
}

os_stat_record() {
  local path=$1
  if /usr/bin/stat -f '%u|%g|%Lp|%HT' "$path" >/dev/null 2>&1; then
    /usr/bin/stat -f '%u|%g|%Lp|%HT' "$path"
  else
    /usr/bin/stat -c '%u|%g|%a|%F' "$path"
  fi
}

os_has_acl() {
  /bin/ls -lde "$1" 2>/dev/null | /usr/bin/awk '
    NR > 1 && /^[[:space:]]*[0-9]+:/ { found = 1 }
    END { exit(found ? 0 : 1) }
  '
}

os_find_print0() {
  /usr/bin/find "$1" -print0
}

groups_contain() {
  local groups=$1
  local wanted=$2
  local group
  for group in $groups; do
    [[ "$group" == "$wanted" ]] && return 0
  done
  return 1
}

normalize_declared_path() {
  local path=$1
  while [[ "$path" != / && "$path" == */ ]]; do
    path=${path%/}
  done
  printf '%s\n' "$path"
}

validate_path() {
  local label=$1
  local path=$2
  local declared resolved

  if [[ "$path" != /* || "$path" == *'/../'* || "$path" == */.. || "$path" == *'/./'* || "$path" == */. ]]; then
    fail_check path_is_absolute "$label must be an absolute normalized path: $path"
    return 1
  fi
  if [[ ! -e "$path" && ! -L "$path" ]]; then
    fail_check path_exists "$label does not exist: $path"
    return 1
  fi
  if [[ -L "$path" ]]; then
    fail_check path_not_symlink "$label is a symlink: $path"
    return 1
  fi
  declared=$(normalize_declared_path "$path")
  if ! resolved=$(os_realpath "$path"); then
    fail_check path_resolves "$label could not be resolved: $path"
    return 1
  fi
  if [[ "$resolved" != "$declared" ]]; then
    fail_check path_resolution_exact "$label resolves to $resolved instead of $declared"
    return 1
  fi
  pass_check path_valid "$label: $declared"
  return 0
}

paths_overlap() {
  local left=${1%/}
  local right=${2%/}
  [[ "$left" == "$right" || "$left" == "$right"/* || "$right" == "$left"/* ]]
}

check_disjoint_roots() {
  local names=(
    builder_root production_root builder_credentials production_credentials
    production_audit production_tunnel deployment_control trusted_repository
    root_python_runtime production_release
  )
  local paths=(
    "$BUILDER_ROOT" "$PRODUCTION_ROOT" "$BUILDER_CREDENTIALS" "$PRODUCTION_CREDENTIALS"
    "$PRODUCTION_AUDIT" "$PRODUCTION_TUNNEL" "$DEPLOYMENT_CONTROL" "$TRUSTED_REPOSITORY"
    "$ROOT_PYTHON_RUNTIME" "$PRODUCTION_RELEASE"
  )
  local i j
  for ((i = 0; i < ${#paths[@]}; i++)); do
    for ((j = i + 1; j < ${#paths[@]}; j++)); do
      if paths_overlap "${paths[$i]}" "${paths[$j]}"; then
        fail_check roots_are_disjoint "${names[$i]} overlaps ${names[$j]}"
      fi
    done
  done
  if [[ "$FAIL_COUNT" -eq 0 ]]; then
    pass_check roots_are_disjoint 'all runtime, release, repository, credential, audit, tunnel, and control roots are separate'
  fi
}

mode_value() {
  local mode=$1
  printf '%d\n' "$((8#$mode))"
}

check_private_entry() {
  local entry=$1
  local expected_uid=$2
  local label=$3
  local profile=$4
  local record owner_uid owner_gid mode kind permissions owner_bits

  if [[ -L "$entry" ]]; then
    fail_check tree_has_no_symlinks "$label contains a symlink: $entry"
    return
  fi
  if ! record=$(os_stat_record "$entry"); then
    fail_check metadata_readable "$label metadata unavailable: $entry"
    return
  fi
  IFS='|' read -r owner_uid owner_gid mode kind <<<"$record"
  if [[ "$owner_uid" != "$expected_uid" ]]; then
    fail_check tree_owner "$label has owner UID $owner_uid, expected $expected_uid: $entry"
  fi
  permissions=$(mode_value "$mode")
  if (( (permissions & 0077) != 0 )); then
    fail_check private_permissions "$label grants group/other permissions ($mode): $entry"
  fi
  if os_has_acl "$entry"; then
    fail_check no_extended_acl "$label has an extended ACL: $entry"
  fi
  owner_bits=$(( (permissions >> 6) & 7 ))
  if [[ -d "$entry" ]]; then
    if (( owner_bits != 7 )); then
      fail_check directory_owner_access "$label directory owner mode must be 7xx: $entry ($mode)"
    fi
  elif [[ "$profile" == credentials ]]; then
    if (( (permissions & 0111) != 0 || (owner_bits & 4) == 0 )); then
      fail_check credential_file_mode "$label credential file must be owner-readable and non-executable: $entry ($mode)"
    fi
  fi
}

check_private_tree() {
  local root=$1
  local expected_uid=$2
  local label=$3
  local profile=$4
  local entry before_failures=$FAIL_COUNT

  while IFS= read -r -d '' entry; do
    check_private_entry "$entry" "$expected_uid" "$label" "$profile"
  done < <(os_find_print0 "$root" 2>/dev/null)

  if [[ "$FAIL_COUNT" -eq "$before_failures" ]]; then
    pass_check private_tree "$label ownership, modes, symlinks, and ACLs are valid"
  fi
}

check_root_readonly_tree() {
  local root=$1 label=$2 entry record owner_uid owner_gid mode kind permissions before_failures=$FAIL_COUNT
  while IFS= read -r -d '' entry; do
    if [[ -L "$entry" ]]; then
      fail_check tree_has_no_symlinks "$label contains a symlink: $entry"
      continue
    fi
    record=$(os_stat_record "$entry") || {
      fail_check metadata_readable "$label metadata unavailable: $entry"
      continue
    }
    IFS='|' read -r owner_uid owner_gid mode kind <<<"$record"
    permissions=$(mode_value "$mode")
    [[ "$owner_uid" == 0 ]] || fail_check tree_owner "$label has owner UID $owner_uid, expected root: $entry"
    (( (permissions & 0022) == 0 )) || fail_check readonly_permissions "$label grants group/other write permissions ($mode): $entry"
    if os_has_acl "$entry"; then
      fail_check no_extended_acl "$label has an extended ACL: $entry"
    fi
  done < <(os_find_print0 "$root" 2>/dev/null)
  if [[ "$FAIL_COUNT" -eq "$before_failures" ]]; then
    pass_check root_readonly_tree "$label is root-owned and not writable by service identities"
  fi
}

class_permission_bits() {
  local actor_uid=$1
  local actor_groups=$2
  local owner_uid=$3
  local owner_gid=$4
  local mode=$5
  local permissions
  permissions=$(mode_value "$mode")
  if [[ "$actor_uid" == "$owner_uid" ]]; then
    printf '%d\n' "$(( (permissions >> 6) & 7 ))"
  elif groups_contain "$actor_groups" "$owner_gid"; then
    printf '%d\n' "$(( (permissions >> 3) & 7 ))"
  else
    printf '%d\n' "$(( permissions & 7 ))"
  fi
}

check_denied() {
  local actor_name=$1
  local actor_uid=$2
  local actor_groups=$3
  local path=$4
  local label=$5
  local record owner_uid owner_gid mode kind bits

  if ! record=$(os_stat_record "$path"); then
    fail_check negative_access "$label metadata unavailable: $path"
    return
  fi
  IFS='|' read -r owner_uid owner_gid mode kind <<<"$record"
  bits=$(class_permission_bits "$actor_uid" "$actor_groups" "$owner_uid" "$owner_gid" "$mode")
  if (( bits == 0 )) && ! os_has_acl "$path"; then
    pass_check negative_access "$actor_name has no POSIX access bits on $label"
  else
    fail_check negative_access "$actor_name has effective POSIX bits $bits on $label"
  fi
}

check_launch_plist() {
  local plist_path=$1 plist_label=$2
  local record owner_uid owner_gid mode kind permissions owner_bits before_failures=$FAIL_COUNT
  if [[ ! -f "$plist_path" ]]; then
    fail_check production_plist_type "$plist_label is not a regular file: $plist_path"
    return
  fi
  record=$(os_stat_record "$plist_path") || {
    fail_check production_plist_metadata "$plist_label metadata unavailable"
    return
  }
  IFS='|' read -r owner_uid owner_gid mode kind <<<"$record"
  permissions=$(mode_value "$mode")
  owner_bits=$(( (permissions >> 6) & 7 ))
  [[ "$owner_uid" == 0 ]] || fail_check production_plist_owner "production plist owner UID is $owner_uid, expected root"
  (( (permissions & 0077) == 0 )) || fail_check production_plist_mode "production plist grants group/other permissions: $mode"
  (( (permissions & 0111) == 0 && (owner_bits & 4) != 0 )) || fail_check production_plist_mode "production plist must be root-readable and non-executable: $mode"
  if os_has_acl "$PRODUCTION_PLIST"; then
    fail_check no_extended_acl "$plist_label has an extended ACL"
  fi
  local declared_user declared_group
  declared_user=$(os_plist_value UserName "$plist_path" || true)
  declared_group=$(os_plist_value GroupName "$plist_path" || true)
  [[ "$declared_user" == "$PRODUCTION_USER" ]] || \
    fail_check production_plist_identity "UserName is ${declared_user:-missing}, expected $PRODUCTION_USER"
  [[ "$declared_group" == "$PRODUCTION_USER" ]] || \
    fail_check production_plist_identity "GroupName is ${declared_group:-missing}, expected $PRODUCTION_USER"
  if [[ "$FAIL_COUNT" -eq "$before_failures" ]]; then
    pass_check production_plist_private "$plist_label is root-owned/private and declares the production identity"
  fi
}

check_service_account_contract() {
  local user=$1 label=$2 shell home hidden group_names before_failures=$FAIL_COUNT
  shell=$(os_user_property "$user" UserShell || true)
  home=$(os_user_property "$user" NFSHomeDirectory || true)
  hidden=$(os_user_property "$user" IsHidden || true)
  group_names=$(os_id_group_names "$user" || true)
  [[ "$shell" == /usr/bin/false ]] || fail_check service_account_shell "$label shell is ${shell:-missing}"
  [[ "$home" == /var/empty ]] || fail_check service_account_home "$label home is ${home:-missing}"
  [[ "$hidden" == 1 ]] || fail_check service_account_hidden "$label IsHidden is ${hidden:-missing}"
  if [[ " $group_names " == *' admin '* || " $group_names " == *' wheel '* || " $group_names " == *' sudo '* ]]; then
    fail_check service_account_groups "$label belongs to a privileged group: $group_names"
  fi
  if [[ "$FAIL_COUNT" -eq "$before_failures" ]]; then
    pass_check service_account_contract "$label is hidden, noninteractive, and unprivileged"
  fi
}

main() {
  BUILDER_USER=''
  PRODUCTION_USER=''
  BUILDER_ROOT=''
  PRODUCTION_ROOT=''
  BUILDER_CREDENTIALS=''
  PRODUCTION_CREDENTIALS=''
  PRODUCTION_AUDIT=''
  PRODUCTION_TUNNEL=''
  DEPLOYMENT_CONTROL=''
  PRODUCTION_PLIST=''
  PRODUCTION_TUNNEL_PLIST=''
  TRUSTED_REPOSITORY=''
  ROOT_PYTHON_RUNTIME=''
  PRODUCTION_RELEASE=''

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --builder-user) BUILDER_USER=${2-}; shift 2 ;;
      --production-user) PRODUCTION_USER=${2-}; shift 2 ;;
      --builder-root) BUILDER_ROOT=${2-}; shift 2 ;;
      --production-root) PRODUCTION_ROOT=${2-}; shift 2 ;;
      --builder-credentials) BUILDER_CREDENTIALS=${2-}; shift 2 ;;
      --production-credentials) PRODUCTION_CREDENTIALS=${2-}; shift 2 ;;
      --production-audit) PRODUCTION_AUDIT=${2-}; shift 2 ;;
      --production-tunnel) PRODUCTION_TUNNEL=${2-}; shift 2 ;;
      --deployment-control) DEPLOYMENT_CONTROL=${2-}; shift 2 ;;
      --production-plist) PRODUCTION_PLIST=${2-}; shift 2 ;;
      --production-tunnel-plist) PRODUCTION_TUNNEL_PLIST=${2-}; shift 2 ;;
      --trusted-repository) TRUSTED_REPOSITORY=${2-}; shift 2 ;;
      --root-python-runtime) ROOT_PYTHON_RUNTIME=${2-}; shift 2 ;;
      --production-release) PRODUCTION_RELEASE=${2-}; shift 2 ;;
      -h|--help) usage; return 0 ;;
      *) printf 'ERROR unknown argument: %s\n' "$1" >&2; usage >&2; return 2 ;;
    esac
  done

  local required_values=(
    "$BUILDER_USER" "$PRODUCTION_USER" "$BUILDER_ROOT" "$PRODUCTION_ROOT"
    "$BUILDER_CREDENTIALS" "$PRODUCTION_CREDENTIALS" "$PRODUCTION_AUDIT"
    "$PRODUCTION_TUNNEL" "$DEPLOYMENT_CONTROL" "$PRODUCTION_PLIST"
    "$PRODUCTION_TUNNEL_PLIST" "$TRUSTED_REPOSITORY" "$ROOT_PYTHON_RUNTIME"
    "$PRODUCTION_RELEASE"
  )
  local value
  for value in "${required_values[@]}"; do
    if [[ -z "$value" ]]; then
      printf 'ERROR all identity and path options are required\n' >&2
      usage >&2
      return 2
    fi
  done

  if [[ "$(os_uname)" != Darwin ]]; then
    printf 'ERROR this verifier targets the macOS production host\n' >&2
    return 2
  fi
  if [[ "$(os_current_uid)" != 0 ]]; then
    printf 'ERROR run this read-only verifier as an administrator for complete metadata visibility\n' >&2
    return 2
  fi

  local builder_uid production_uid builder_gid production_gid builder_groups production_groups
  if ! builder_uid=$(os_id_uid "$BUILDER_USER" 2>/dev/null); then
    fail_check builder_identity_exists "identity not found: $BUILDER_USER"
    builder_uid=''
  else
    pass_check builder_identity_exists "$BUILDER_USER has UID $builder_uid"
  fi
  if ! production_uid=$(os_id_uid "$PRODUCTION_USER" 2>/dev/null); then
    fail_check production_identity_exists "identity not found: $PRODUCTION_USER"
    production_uid=''
  else
    pass_check production_identity_exists "$PRODUCTION_USER has UID $production_uid"
  fi

  if [[ -n "$builder_uid" && -n "$production_uid" ]]; then
    builder_gid=$(os_id_gid "$BUILDER_USER")
    production_gid=$(os_id_gid "$PRODUCTION_USER")
    builder_groups=$(os_id_groups "$BUILDER_USER")
    production_groups=$(os_id_groups "$PRODUCTION_USER")
    if [[ "$builder_uid" == 0 || "$production_uid" == 0 || "$builder_uid" == "$production_uid" ]]; then
      fail_check identity_boundary "builder/production UIDs must be distinct and non-root"
    else
      pass_check identity_boundary "builder UID $builder_uid and production UID $production_uid are distinct"
    fi
    if [[ "$builder_gid" == "$production_gid" ]]; then
      fail_check primary_group_boundary "builder and production primary GIDs are both $builder_gid"
    else
      pass_check primary_group_boundary "builder GID $builder_gid and production GID $production_gid are distinct"
    fi
    if groups_contain "$builder_groups" "$production_gid" || groups_contain "$production_groups" "$builder_gid"; then
      fail_check cross_group_membership "a service identity belongs to the other plane's primary group"
    else
      pass_check cross_group_membership "neither service identity belongs to the other plane's primary group"
    fi
    check_service_account_contract "$BUILDER_USER" builder
    check_service_account_contract "$PRODUCTION_USER" production
  fi

  local valid_paths=1
  validate_path builder_root "$BUILDER_ROOT" || valid_paths=0
  validate_path production_root "$PRODUCTION_ROOT" || valid_paths=0
  validate_path builder_credentials "$BUILDER_CREDENTIALS" || valid_paths=0
  validate_path production_credentials "$PRODUCTION_CREDENTIALS" || valid_paths=0
  validate_path production_audit "$PRODUCTION_AUDIT" || valid_paths=0
  validate_path production_tunnel "$PRODUCTION_TUNNEL" || valid_paths=0
  validate_path deployment_control "$DEPLOYMENT_CONTROL" || valid_paths=0
  validate_path production_plist "$PRODUCTION_PLIST" || valid_paths=0
  validate_path production_tunnel_plist "$PRODUCTION_TUNNEL_PLIST" || valid_paths=0
  validate_path trusted_repository "$TRUSTED_REPOSITORY" || valid_paths=0
  validate_path root_python_runtime "$ROOT_PYTHON_RUNTIME" || valid_paths=0
  validate_path production_release "$PRODUCTION_RELEASE" || valid_paths=0
  check_disjoint_roots

  if [[ "$valid_paths" == 1 && -n "$builder_uid" && -n "$production_uid" ]]; then
    check_private_tree "$BUILDER_ROOT" "$builder_uid" builder_runtime runtime
    check_private_tree "$PRODUCTION_ROOT" "$production_uid" production_runtime runtime
    check_private_tree "$BUILDER_CREDENTIALS" "$builder_uid" builder_credentials credentials
    check_private_tree "$PRODUCTION_CREDENTIALS" "$production_uid" production_credentials credentials
    check_private_tree "$PRODUCTION_AUDIT" "$production_uid" production_audit runtime
    check_private_tree "$PRODUCTION_TUNNEL" "$production_uid" production_tunnel runtime
    check_private_tree "$DEPLOYMENT_CONTROL" 0 deployment_control runtime
    check_private_tree "$TRUSTED_REPOSITORY" 0 trusted_repository runtime
    check_root_readonly_tree "$ROOT_PYTHON_RUNTIME" root_python_runtime
    check_root_readonly_tree "$PRODUCTION_RELEASE" production_release
    check_launch_plist "$PRODUCTION_PLIST" production_home_plist
    check_launch_plist "$PRODUCTION_TUNNEL_PLIST" production_tunnel_plist

    check_denied "$BUILDER_USER" "$builder_uid" "$builder_groups" "$PRODUCTION_ROOT" production_runtime
    check_denied "$BUILDER_USER" "$builder_uid" "$builder_groups" "$PRODUCTION_CREDENTIALS" production_credentials
    check_denied "$BUILDER_USER" "$builder_uid" "$builder_groups" "$PRODUCTION_AUDIT" production_audit
    check_denied "$BUILDER_USER" "$builder_uid" "$builder_groups" "$PRODUCTION_TUNNEL" production_tunnel
    check_denied "$BUILDER_USER" "$builder_uid" "$builder_groups" "$DEPLOYMENT_CONTROL" deployment_control
    check_denied "$BUILDER_USER" "$builder_uid" "$builder_groups" "$PRODUCTION_PLIST" production_plist
    check_denied "$BUILDER_USER" "$builder_uid" "$builder_groups" "$PRODUCTION_TUNNEL_PLIST" production_tunnel_plist
    check_denied "$BUILDER_USER" "$builder_uid" "$builder_groups" "$TRUSTED_REPOSITORY" trusted_repository
    check_denied "$PRODUCTION_USER" "$production_uid" "$production_groups" "$BUILDER_CREDENTIALS" builder_credentials
    check_denied "$PRODUCTION_USER" "$production_uid" "$production_groups" "$TRUSTED_REPOSITORY" trusted_repository
  fi

  printf 'SUMMARY pass=%d fail=%d read_only=true\n' "$PASS_COUNT" "$FAIL_COUNT"
  [[ "$FAIL_COUNT" -eq 0 ]]
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
