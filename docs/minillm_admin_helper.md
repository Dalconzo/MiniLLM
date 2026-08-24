# MiniLLM Administration Helper

`minillm-admin` replaces repeated interactive root commands with a small,
fixed command surface. It does not grant a shell, accept command strings, or
execute files from the user checkout. Installation and request review remain
explicit administrator actions.

## Trust boundary

The installed helper lives at `/usr/local/sbin/minillm-admin`. Its root-owned
configuration, request state, copied review material, results, lock, and audit
journal live under:

```text
/Library/Application Support/MiniLLM/deployment-control/admin
```

The installer copies and hashes these executable artifacts:

- the standalone administration helper;
- the read-only service-identity boundary verifier;
- the transactional first-cutover script;
- the existing root-controlled `minillm-promote` entry point.

Every invocation rechecks those hashes. Normal MiniLLM release promotion cannot
replace the helper or its policy. Commands run with a fixed environment, fixed
absolute executable paths, `shell=False`, and no caller-provided arguments.

The passwordless allowlist contains exactly:

```text
minillm-admin status
minillm-admin verify
minillm-admin deploy-reviewed
minillm-admin cutover-reviewed
minillm-admin rollback
minillm-admin audit-verify
```

`review` and `revoke` are deliberately absent. They require normal interactive
administrator authentication, even after installation.

## Installation

Run the installer without `--apply` first. It prints the operator, source and
installed artifact hashes, exact capability list, and confirms that it will
not manage either service. Applying additionally requires the exact
`INSTALL_MINILLM_ADMIN_HELPER` confirmation.

The long installer command should be staged as a reviewed one-line wrapper in
`/private/tmp`, as was done for the production bootstrap. The wrapper must pin
the SHA-256 of all three repository-supplied files. The only command the human
then enters is:

```bash
ssh -t minillm-host.example 'sudo /private/tmp/install-minillm-admin'
```

The installer validates the generated policy with `visudo` before and after it
is moved to `/etc/sudoers.d/minillm-admin`.

## Reviewed request workflow

The untrusted preparation inbox is fixed at:

```text
<repo-root>/build/admin-requests
```

An agent may prepare files there, but cannot approve them. The human inspects
`request.json` and performs the short passworded review:

```bash
ssh -t minillm-host.example 'sudo /usr/local/sbin/minillm-admin review'
```

The helper opens the 0700 inbox and each source with no-follow descriptors,
checks ownership and modes, verifies all declared digests, copies the exact
bytes into a new root-private request directory, and materializes the existing
one-time `minillm-promote` authorization when needed. A reviewed request expires
within four hours and is bound to one operation and one immutable commit.

After review, Codex may execute only the corresponding argument-free command:

```bash
ssh minillm-host.example 'sudo -n /usr/local/sbin/minillm-admin deploy-reviewed'
```

The reviewed request is moved from `pending` to `consumed` before a host
mutation. A retry or replay therefore fails; a new human review is required.

## Request schemas

Common fields are strict. Unknown fields are rejected.

```json
{
  "schema_version": 1,
  "request_id": "cutover-20260823-001",
  "operation": "cutover-reviewed",
  "created_at": "2026-08-23T12:00:00Z",
  "expires_at": "2026-08-23T13:00:00Z",
  "candidate_commit": "FULL_COMMIT_ID",
  "reason": "Reviewed boundary pass and transactional rollback behavior.",
  "approval_reference": "change:cutover-20260823-001"
}
```

A deployment additionally provides the safe inbox filenames and exact hashes
of the candidate manifest and Git bundle:

```json
{
  "schema_version": 1,
  "request_id": "deploy-20260823-001",
  "operation": "deploy-reviewed",
  "created_at": "2026-08-23T12:00:00Z",
  "expires_at": "2026-08-23T13:00:00Z",
  "candidate_commit": "FULL_COMMIT_ID",
  "reason": "Reviewed committed candidate, tests, evals, and manifest.",
  "approval_reference": "change:deploy-20260823-001",
  "manifest_file": "candidate.json",
  "manifest_sha256": "SHA256",
  "bundle_file": "candidate.bundle",
  "bundle_sha256": "SHA256"
}
```

A rollback uses a separately reviewed authorization and the persisted failed
promotion operation ID:

```json
{
  "schema_version": 1,
  "request_id": "rollback-20260823-001",
  "operation": "rollback",
  "created_at": "2026-08-23T12:00:00Z",
  "expires_at": "2026-08-23T13:00:00Z",
  "candidate_commit": "FAILED_CANDIDATE_COMMIT",
  "reason": "Reviewed failed smoke evidence and approved restoration.",
  "approval_reference": "incident:rollback-20260823-001",
  "manifest_file": "candidate.json",
  "manifest_sha256": "SHA256",
  "operation_id": "PERSISTED_PROMOTION_OPERATION_ID"
}
```

## Audit and failure behavior

`admin/audit/admin-audit.jsonl` is a root-private SHA-256 hash chain. Records
include operator, operation, request and candidate IDs, artifact digests,
timestamps, stages, result, and rollback status. Request reasons, file content,
credentials, environment values, and child-process output are not recorded.

`audit-verify` validates every link. A malformed, stale, replayed, substituted,
or cross-commit request fails before execution. Boundary verification runs
immediately before each reviewed mutation. A failed operation remains consumed
and requires a new human-reviewed request; cutover itself retains its automatic
user-service rollback.

## Revocation and recovery

Immediate revocation is passworded and removes the sudoers entry first:

```bash
ssh -t minillm-host.example 'sudo /usr/local/sbin/minillm-admin revoke'
```

The installer also supports the explicit
`--uninstall --apply --confirm REVOKE_MINILLM_ADMIN_HELPER` path. Both preserve
audit records, reviewed material, consumed requests, and results for incident
review. If the helper is damaged, an administrator should first remove
`/etc/sudoers.d/minillm-admin`, validate the complete sudoers policy with
`visudo -c`, and then use the existing production service/cutover recovery
instructions in `service_identity_provisioning.md`.
