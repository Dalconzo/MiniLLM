# Production Promotion Controller

`local_agent_lab.promotion_controller` is the command-neutral control-plane core for human-triggered production promotion and rollback. It builds and evaluates immutable plans; it does not read Git state, copy files, connect to a host, restart a service, write an audit log, or execute rollback commands.

The module is intentionally separate from the builder and from `deployment_gate.py`. A trusted adapter must gather facts from Git and the target host, invoke this library, persist its audit output, and perform only the actions represented by a ready plan.

That adapter is implemented in `local_agent_lab.trusted_promotion`, with the
separate root-controlled entry point `minillm-promote`. Both files are explicit
control-plane denials in the check-only gate and in the controller's independent
artifact checks. They cannot promote themselves through the ordinary release
path.

## Promotion Flow

1. A trusted inventory adapter resolves an immutable 40- or 64-character Git commit, verifies that its commit object exists, and confirms the checked-out worktree is clean.
2. The adapter builds a content manifest for that committed tree. Every entry contains its repo-relative path, trusted resolved path, SHA-256 content digest, and byte size.
3. The adapter invokes the check-only deployment gate and passes its result, the original candidate, and its independent verification to `build_promotion_plan`.
4. A human supplies authorization bound to the exact action, candidate commit, previous build, manifest digest, validity window, reason, and approval reference.
5. Preflight smoke evidence and a successful rollback drill are required before the plan can become `ready`.
6. A separate trusted executor may apply the ready plan. It reports restart and post-promotion smoke evidence to `assess_promotion_execution`.
7. Any failed restart or smoke result produces `status="stopped"`, `further_promotion_allowed=false`, and `rollback_required=true`. The controller never continues through a failed smoke boundary.

An allow result never means that this Python module deployed anything:

```text
requires_trusted_executor: true
promotion_performed: false
controller_host_changes_performed: false
```

## Independent Verification

The controller does not trust a builder-supplied allow result by itself. It:

- reruns `check_deployment_candidate` and requires an exact match with the supplied decision;
- requires a full immutable commit ID rather than a branch, tag, or short hash;
- binds repository, branch, candidate commit, checked-out commit, and manifest commit;
- rejects dirty/uncommitted worktrees and unverified commit objects;
- requires a verifier identity distinct from the builder and production service identities;
- recomputes a canonical manifest SHA-256;
- requires the committed manifest, gated inventory, and approved inventory to match exactly;
- binds smoke evidence to the exact candidate build and manifest and rejects future-dated evidence;
- independently rejects runtime data, credentials, secrets, private keys, tunnel/auth paths, and control-plane source.

The controller source itself, `src/local_agent_lab/promotion_controller.py`, cannot be promoted through this path. Controller, security-policy, auth, tunnel, launchd, and audit changes require a separate human security-review mechanism.

## Human Authorization

Promotion and rollback need separate `HumanAuthorization` records. Presence of an object is not enough. The controller requires:

```text
actor_type: human
decision: approve
action: promote | rollback
authorization_id
actor
candidate_commit
previous_build
manifest_sha256
authorized_at
expires_at
reason
approval_reference
```

The authorization must be current when the plan is prepared and must match the exact candidate and manifest. Builder and production service identities cannot authorize their own work. Approval references are treated as opaque data and cannot contain shell syntax.

## Audit Contract

Every plan or assessment contains frozen `AuditMetadata`. It records:

- operation and parent-operation IDs;
- action, status, human actor, authorization ID, verifier identity, and external executor identity;
- repository, branch, candidate build, and previous build;
- complete content-hashed manifest and its canonical digest;
- digest of the consumed check-only gate decision;
- identity, manifest, authorization, smoke, and rollback-readiness checks;
- verification, authorization, and record timestamps;
- migration version and data-compatibility assumptions;
- rollback reference;
- externally reported restart and smoke results;
- whether an external host change was reported;
- the invariant that the controller itself performed no host changes;
- a canonical `record_sha256` for tamper detection after persistence.

Dataclasses are frozen and collection fields are tuples. The trusted adapter must append the serialized audit object to durable storage rather than replacing prior records. It must also verify the stored canonical digest when reading audit history.

## Rollback Flow

`build_rollback_plan` accepts a promotion plan, its execution outcome, and a new human authorization whose action is exactly `rollback`. It returns only opaque declarative steps; it contains no shell command or host callback.

The initial rollback plan is `pending_test`. `test_rollback_plan` makes it `ready` only when evidence confirms all of the following:

- the rollback reference resolves;
- the previous build is available;
- data compatibility has been verified;
- the restore procedure has been tested;
- the overall rollback drill passed;
- timestamped evidence is available.

Even a ready assessment retains:

```text
requires_trusted_executor: true
rollback_performed: false
```

The trusted executor must verify that production is still on the expected candidate before acting, restore only committed code/config artifacts, restart through the production service manager, run post-rollback smoke tests, and append the resulting evidence. Runtime databases, exports, logs, credentials, and secrets must never be copied by promotion or rollback.

## Trusted Adapter

The adapter now implements the previously missing integration:

- Git inventory disables repository fsmonitor/untracked-cache execution,
  requires a clean checkout at the exact full commit, rejects symlink/submodule
  artifacts, and hashes bytes read directly from committed blobs;
- root-private authorization files are bound to action, commit, previous build,
  manifest digest, human actor, reason, reference, and expiration, then atomically
  moved from `pending` to `consumed` before a host mutation;
- a nonblocking production lock prevents concurrent operations;
- audit records are append-only JSONL envelopes with sequence numbers, previous
  record digests, canonical record digests, `fsync`, and full-chain verification;
- release creation copies only a previously trusted release, rejects symlinks,
  special files, runtime data, environments, and credential-like paths, then
  replaces only gate-approved files with independently rehashed committed blobs;
- activation uses an atomic release pointer plus a root-private current-build
  marker and requires compare-and-swap against the approved previous build;
- candidate tests are demoted to `_minillm_builder`; application smoke tests are
  demoted to `_minillm_prod`; candidate code is never executed as root;
- restart and smoke stop at the first failure and persist an outcome requiring
  rollback;
- complete promotion state is stored under the root-private control directory
  with its own digest, permitting a later process to load it safely;
- rollback requires a distinct one-time human authorization, reconstructs and
  verifies the original controller state, reruns readiness checks, compare-and-
  swaps from the failed candidate to the approved previous release, restarts,
  smokes, and appends the outcome.

The dedicated command surface is:

```bash
minillm-promote verify --manifest candidate.json --repository-dir /path/to/trusted-checkout
sudo minillm-promote authorize --manifest candidate.json --repository-dir /path/to/trusted-checkout \
  --action promote --reason 'Reviewed tests and manifest' --approval-reference 'ticket:local-change-001'
sudo minillm-promote apply --manifest candidate.json --repository-dir /path/to/trusted-checkout \
  --authorization-id <authorization-id>
sudo minillm-promote rollback --operation-id <operation-id> \
  --authorization-id <separate-rollback-authorization-id>
sudo minillm-promote audit-verify
```

The adapter remains inert until the root-owned control and release roots exist
and the administrator has installed the dedicated runtimes.
It refuses the current dirty checkout and cannot operate against the existing
per-user LaunchAgent. No production deployment has been performed by adding
this adapter.
