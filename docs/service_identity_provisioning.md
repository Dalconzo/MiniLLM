# MiniLLM Service Identity Provisioning

This document specifies the administrator-owned host boundary. Repository code
can describe and verify this boundary, but it
must not create accounts, change permissions, install services, copy secrets,
or grant deployment authority.

The reviewed helper `scripts/provision_minillm_service_identities.sh` is now
the sole repository-provided provisioning utility. It is dry-run by default,
requires root plus the literal confirmation
`CREATE_MINILLM_SERVICE_IDENTITIES` before applying, is idempotent for matching
accounts, and stops on UID/GID collisions. It never starts or unloads a
service, migrates data, copies credentials, or edits the existing LaunchAgent.

The existing builder-to-production gate remains check-only. Passing that gate
means a candidate is eligible for separate human promotion; it does not grant
the builder access to the production identity or runtime.

## Required Host Layout

Use dedicated hidden macOS service accounts and groups. The names below are the
v1 defaults; an administrator may choose different names if every verifier
argument and service definition is updated consistently.

| Plane | Account | Primary group | Runtime root |
| --- | --- | --- | --- |
| Builder/staging | `_minillm_builder` | `_minillm_builder` | `/private/var/db/minillm-builder` |
| Production/Home MCP | `_minillm_prod` | `_minillm_prod` | `/private/var/db/minillm-production` |

Additional private roots:

| Purpose | Path | Owner | Maximum access |
| --- | --- | --- | --- |
| Builder credentials | `/Library/Application Support/MiniLLM/builder-credentials` | `_minillm_builder` | directories `0700`; files `0600` or `0400` |
| Production credentials | `/Library/Application Support/MiniLLM/production-credentials` | `_minillm_prod` | directories `0700`; files `0600` or `0400` |
| Production audit logs | `/private/var/log/minillm-production` | `_minillm_prod` | private tree; no group/other bits |
| Production tunnel state | `/Library/Application Support/MiniLLM/production-tunnel` | `_minillm_prod` | private tree; no group/other bits |
| Deployment control | `/Library/Application Support/MiniLLM/deployment-control` | `root` | private tree; no group/other bits |
| Trusted Git repository | `/Library/Application Support/MiniLLM/trusted-repository` | `root` | private tree; no service-identity access |
| Python/control runtime | `/Library/Application Support/MiniLLM/python-runtime` | `root` | service-readable; no group/other writes |
| Immutable releases | `/Library/Application Support/MiniLLM/releases/<commit>` | `root` | service-readable; no group/other writes |
| Production launch daemon | `/Library/LaunchDaemons/com.dalconzo.minillm.production.plist` | `root` | `0600` or `0400` |
| Tunnel launch daemon | `/Library/LaunchDaemons/com.dalconzo.minillm.production-tunnel.plist` | `root` | `0600` or `0400` |

All roots must be real absolute paths, not symlinks or nested aliases. Private
trees must not contain symlinks or ACL grants. The builder and production UIDs
and primary GIDs must be distinct, and neither identity may belong to the
other's primary group.

The production executable/release tree should be separately root-owned and
read-only to both service identities. It is intentionally not a writable
runtime root. Source promotion into that tree belongs to the later
administrator-controlled deployment mechanism, not this bead.

## Account Contract

An administrator must allocate unused local UIDs and GIDs after inspecting the
host directory. Do not copy example numeric IDs without checking for a
collision.

Each account must have:

- a unique UID and a unique private primary group;
- no interactive password;
- `/usr/bin/false` as its login shell;
- `/var/empty` as its directory-service home;
- no `admin`, `wheel`, `sudo`, SSH, or cross-plane group membership;
- no access token shared with the other plane.

On macOS, the administrator may use Directory Utility or reviewed `dscl`
commands. The provisioning operation must remain a human administrator action.
Before creating anything, inventory identities and numeric IDs:

```bash
dscl . -list /Users UniqueID | sort -nk2
dscl . -list /Groups PrimaryGroupID | sort -nk2
```

Review the repository-generated plan before making those changes:

```bash
scripts/provision_minillm_service_identities.sh
```

After confirming that UIDs/GIDs 450 and 451 remain unused, the explicit first
stage is:

```bash
sudo scripts/provision_minillm_service_identities.sh \
  --apply \
  --confirm CREATE_MINILLM_SERVICE_IDENTITIES \
  --builder-uid 450 \
  --builder-gid 450 \
  --production-uid 451 \
  --production-gid 451
```

This creates only the identities and empty private roots. It does not cut over
Home MCP. Re-run the dry run and inventory if the numeric IDs are no longer
free; never force a collision.

After selecting unused IDs, create one private group and one hidden service
account per plane. Use placeholders such as `<builder_uid>` and
`<production_gid>` in the reviewed change record; do not store real passwords
or credentials in the repository. Set `IsHidden` to `1`, `UserShell` to
`/usr/bin/false`, and `NFSHomeDirectory` to `/var/empty`.

## Filesystem Contract

The administrator creates each root with its required owner and mode. A safe
pattern is `install -d -o <owner> -g <group> -m 0700 <path>`, issued manually
after every path and identity has been reviewed.

Rules enforced by the verifier:

1. Builder and production identities exist, are non-root, and are distinct.
2. Their primary groups are distinct and have no cross-membership.
3. Builder runtime and credentials are owned by the builder identity.
4. Production runtime, credentials, audit, and tunnel trees are owned by the
   production identity.
5. Deployment-control state, the trusted Git repository, immutable release,
   root Python runtime, and both production launch daemons are root-owned.
6. Private directories provide owner `rwx`; private files have no group/other
   permissions. Credential files are non-executable and owner-readable only.
7. Protected trees contain no symlinks or extended ACL entries.
8. All configured roots resolve to separate, non-nested locations.
9. POSIX ownership, groups, modes, and the absence of ACLs deny the builder all
   access bits on production runtime/auth/audit/tunnel/control roots and deny
   production access to builder credentials. Neither identity can access the
   trusted Git repository.

The verifier inspects names and metadata only. It does not open credential or
audit file contents.

## Credential Separation

- Create different credentials for staging and production. Never copy a token
  between the two credential roots.
- Service definitions should receive paths to credential files, not secret
  values in command-line arguments, plist contents, repository config, or a
  shared environment file.
- Tunnel credentials belong only in the production tunnel/credential roots.
- The builder must not own, read, enumerate, or replace production credentials.
- The production service must not consume the builder's staging credentials.
- Rotation is an administrator operation: stage a new production credential,
  restart only the production service, verify health, then revoke the old one.

The current per-user LaunchAgent implementation still derives configuration
from a user's home directory and can place an auth token in process arguments.
Do not claim the boundary complete until the production LaunchDaemon and tunnel
client consume explicit root-owned configuration and production-only secret
file paths.

## Service Contract

Production must run as a system LaunchDaemon with both `UserName` and
`GroupName` set to `_minillm_prod` (or the approved replacements). The plist is
root-owned, private, and not writable by either service identity. It must point
at:

- a root-owned, reviewed release;
- the production runtime root;
- the production configuration;
- production-only credential file paths;
- production audit and tunnel roots.

Builder/staging jobs run only as `_minillm_builder`, use
`/private/var/db/minillm-builder`, and have no deployment command, production
plist, production socket, SSH key, tunnel profile, or production API token.

The production service should have no write access to the source checkout. The
builder may prepare candidate artifacts, but only the administrator-controlled
promotion process may replace the production release or service definition.

The optional `--install-launch-daemon` provisioning stage requires explicit
paths to an already reviewed root-owned release, root-owned Python runtime, and
production-owned config. It installs a root-private plist with `Disabled=true`
and does not bootstrap or start it. Loading the daemon, migrating the current
data, and switching the tunnel remain separate reviewed administrator actions.
This prevents a partially provisioned service from replacing the known-good
personal connector.

The reviewed second stage is `scripts/bootstrap_minillm_production.sh`. It
accepts a content-hashed Git bundle and offline wheelhouse, installs only an
allowlisted source tree as an immutable release, keeps the full Git repository
root-private, creates a root-owned Python runtime, copies mutable data into the
production identity, and installs both system daemons disabled. Candidate
application code is executed only after demotion to `_minillm_prod` during a
shadow smoke test on port `8766`. The script does not call `launchctl` and does
not alter the live user LaunchAgents.

The shadow port must be unused before bootstrap. The bootstrap tracks the
entire sudo/service subprocess tree, terminates descendants before their
supervisor, and requires port `8766` to be unused again before reporting a
successful shadow smoke. An existing listener is a hard stop rather than
evidence that the new candidate started successfully.

The root-owned Python runtime is intentionally outside
`/private/var/db/minillm-production`. Making the interpreter production-owned
would allow a compromised service to replace code later invoked by the trusted
control plane. Application imports instead come from the atomic,
root-controlled `releases/current/src` path.

## Read-Only Verification

After provisioning and before enabling production, run the repository verifier
as an administrator so it can enumerate every private tree. The script itself
does not elevate privileges or make changes:

```bash
sudo scripts/verify_service_identity_boundaries.sh \
  --builder-user _minillm_builder \
  --production-user _minillm_prod \
  --builder-root /private/var/db/minillm-builder \
  --production-root /private/var/db/minillm-production \
  --builder-credentials '/Library/Application Support/MiniLLM/builder-credentials' \
  --production-credentials '/Library/Application Support/MiniLLM/production-credentials' \
  --production-audit /private/var/log/minillm-production \
  --production-tunnel '/Library/Application Support/MiniLLM/production-tunnel' \
  --deployment-control '/Library/Application Support/MiniLLM/deployment-control' \
  --production-plist /Library/LaunchDaemons/com.dalconzo.minillm.production.plist \
  --production-tunnel-plist /Library/LaunchDaemons/com.dalconzo.minillm.production-tunnel.plist \
  --trusted-repository '/Library/Application Support/MiniLLM/trusted-repository' \
  --root-python-runtime '/Library/Application Support/MiniLLM/python-runtime' \
  --production-release '/Library/Application Support/MiniLLM/releases/<full_commit>'
```

Expected result: every line is `PASS`, the summary reports zero failures, and
the exit status is `0`. `FAIL` is a hard deployment stop. Exit status `2`
indicates the verifier could not establish trustworthy facts, such as being run
without administrator visibility.

Re-run the verifier after account, group, path, credential, plist, or service
changes and attach its non-secret output to the human deployment record.

## Controlled Cutover

`scripts/cutover_minillm_production.sh` is a separate dry-run-first operation.
It is not invoked by the bootstrap. Applying requires the exact confirmation
`CUTOVER_MINILLM_PRODUCTION` and administrator execution.

The cutover performs these operations in fail-closed order:

1. prove the existing user Home MCP and tunnel are both loaded and local Home
   MCP health is good;
2. stop and disable the user tunnel first, then the user Home MCP;
3. take a final data copy while the old service is stopped;
4. start the production Home MCP and verify local health, smoke behavior, and
   the `_minillm_prod` process UID;
5. start the production tunnel and verify its process UID plus `/readyz`;
6. retain the old data and plist files, but leave the user jobs unloaded.

An `ERR` trap is armed before the first service change. Any failure boots out
and disables both system jobs, re-enables and bootstraps the user Home MCP, waits
for local health, then restores the user tunnel. No production data is copied
back into the retained user data tree during rollback.

Review the no-op plan first:

```bash
scripts/cutover_minillm_production.sh
```

Only after the disabled system daemons pass the administrator verifier and the
shadow smoke should the administrator apply the exact one-line cutover command
recorded for that release.

## Provisioning Sequence And Rollback

1. Stop before changes and record current service labels, release reference,
   runtime paths, owners, and modes without copying secrets.
2. Create the two identities and private groups.
3. Create empty private roots; run the verifier before copying any state.
4. Install a reviewed root-owned release and LaunchDaemon definition.
5. Provision distinct credentials directly into their private roots.
6. Copy production data once under administrator control, preserve a backup,
   and set final production ownership.
7. Run the verifier, repository tests, memory health checks, and Home MCP smoke
   tests before enabling the tunnel.
8. Enable production and confirm its process UID, runtime root, logs, and
   credential paths match this contract.
9. Prove the builder cannot read, write, traverse, or replace each protected
   production path using the verifier and an administrator-reviewed manual
   impersonation check if required by local policy.

Rollback means stopping the new LaunchDaemon, restoring the prior reviewed
service definition/release and data backup, and revoking newly issued
credentials. Do not relax permissions or merge identities to recover service.

## Passwordless Administration Boundary

After the identities, immutable runtime, and root-private control paths exist,
the separately reviewed helper in `minillm_admin_helper.md` can replace repeated
interactive sudo commands. Its NOPASSWD surface contains only argument-free,
typed operations. Deployment, cutover, and rollback still require a fresh
root-private request created by a separate passworded human review; the helper
does not convert builder output into deployment authority.

## Residual Administrator Work

Repository code cannot perform the following without an explicit administrator
invocation:

- allocate UIDs/GIDs or create accounts and groups;
- enable, bootstrap, boot out, or replace a launchd service;
- rotate or revoke production and staging credentials;
- execute the administrator verifier and manual process-identity checks;
- authorize promotion or rollback.

As of 2026-08-23, the identities, private roots, immutable baseline, and
administrator verifier are applied. The first cutover automatically rolled
back after the production tunnel failed readiness, so the live Home MCP and
tunnel remain the healthy per-user LaunchAgents. The corrected baseline must
be bootstrapped and cut over again before this isolation work is complete. Do
not mark the bead complete until the verifier passes and both
`system/com.dalconzo.minillm.production` services run as `_minillm_prod`.

Those actions require explicit administrator approval and should be tracked in
the deployment audit separately from builder-generated artifacts.
