# Builder-to-Production Gate

The v1 deployment gate is a pure policy check between the local builder/staging plane and Mac mini production. It does not sync files, execute shell commands, write production state, restart services, approve itself, or deploy anything.

The implementation is `local_agent_lab.deployment_gate.check_deployment_candidate`.

## Authority Boundary

The gate expects:

- a builder identity distinct from the production service identity;
- staging runtime state rooted at `data/local_staging`;
- production runtime state rooted at `data`;
- the known `MiniLLM` repository and an allowlisted branch;
- a nonempty candidate commit;
- passed tests, evals, security checks, and migration-compatibility checks;
- complete rollback metadata;
- a bounded artifact inventory from a trusted caller.

An `allow` result means only that the candidate is eligible for separate human-triggered promotion. It is not deployment authorization and does not perform promotion.

## Artifact Boundary

V1 uses an explicit allowlist:

| Category | Allowed paths |
| --- | --- |
| Source | Python files under `src/local_agent_lab/` |
| Tests | Python files under `tests/` |
| Configuration | JSON, TOML, YAML, or Markdown under `config/` |
| Documentation | Markdown under `docs/` |

Allowlisting is applied only after the deny rules. The following never pass this gate:

- anything under `data/`, including staging data, production memory, Home MCP data, exports, patches, and logs;
- credentials, tokens, `.env` files, SSH/private keys, and certificates;
- production tunnel or auth configuration;
- production `config/agent.yaml`;
- Home MCP auth, launchd/tunnel, audit implementation, and deployment-gate source paths;
- files outside the explicit allowlist.

Those control-plane changes require a separate human security review and a later, independently authorized mechanism. The v1 gate has no override flag.

## Traversal And Symlinks

Artifact paths must be POSIX-style, repo-relative paths with no empty, dot, or parent components. Absolute paths and backslash paths are rejected.

Because the gate is pure and performs no filesystem reads, a trusted inventory step must supply both:

```text
path
resolved_path
```

The paths must match exactly after lexical validation. A mismatch is treated as possible symlink redirection and denied. Missing resolved-path metadata is also denied. The caller, not the builder model, is responsible for producing the resolved path.

## Rollback Contract

Every candidate must include:

```text
previous_build
candidate_build
migration_version
rollback_reference
data_compatibility_assumptions
```

`candidate_build` must equal the checked candidate commit, and `previous_build` must differ. V1 accepts only opaque `git-ref:`, `artifact:`, or `procedure:` rollback references. It rejects shell separators and never executes the reference.

## Structured Result

The decision exposes:

```text
status: allow | deny
promotable: boolean
check_only: true
deployment_performed: false
reasons: structured code/message/field/path objects
approved_artifacts: path and category
rollback_requirements
missing_rollback_fields
```

Common denial codes include `path_traversal_denied`, `artifact_resolution_mismatch`, `runtime_data_path_denied`, `credential_path_denied`, `control_plane_path_denied`, `identity_boundary_violation`, `required_check_failed`, and `missing_rollback_metadata`.

## Required Follow-Up

Promotion remains human-triggered. A separate control-plane process must verify the commit and inventory, perform the actual sync, run production smoke tests, record deployment audit metadata, and execute rollback if required. None of those actions belong in this check-only module.
