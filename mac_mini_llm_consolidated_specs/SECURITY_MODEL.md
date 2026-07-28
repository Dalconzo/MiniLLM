# Security Model

**Status:** Highest-precedence normative document  
**Scope:** Home MCP, production memory service, Codex/build environment, credentials, deployment and permissions

## 1. Core principle

Expose narrow, typed, reversible capabilities, not general machine control.

The Mac mini may be dedicated to the project, but the security goal is not primarily protecting the hardware. It is protecting:

```text
production system integrity
memory integrity
credentials
audit integrity
permission boundaries
deployment authority
the trusted feedback/evaluation loop
```

## 2. Same machine is acceptable; same authority domain is not

The production system and builder may run on the same physical Mac.

They must not share unrestricted authority.

Recommended trust layout:

```text
HOST / CONTROL PLANE
│
├── production Home MCP service
│     ├── production memory DB
│     ├── allowlisted HomeBrain roots
│     ├── audit logs
│     └── append-only feedback interface
│
├── deployment gate
│     └── accepts tested artifacts/commits only
│
└── builder VM or strongly isolated builder environment
      ├── Codex/local LLMs
      ├── repo clone
      ├── test/staging database
      ├── eval harness
      └── no production credentials
```

## 3. Trust zones

### Zone A: production data plane

May access only the production data and tools explicitly needed by MCP contracts.

### Zone B: builder/staging

Codex and local LLMs may have broad authority inside this sandbox.

They may:

- edit repo files
- run tests
- start staging services
- recreate test databases
- generate embeddings
- run local models
- benchmark changes
- fuzz tool interfaces

They must not possess production deployment authority.

### Zone C: security/control plane

Contains:

- allowlisted roots
- MCP permission policy
- production credentials
- tunnel/auth configuration
- deployment gate
- audit policy
- builder isolation settings

This zone remains human-gated.

## 4. Constitution rule

The production system must not be able to rewrite its own constitution.

An LLM may propose changes to security configuration.

It may not autonomously apply changes that grant:

- broader filesystem access
- new credentials
- arbitrary shell
- deployment authority
- weaker confirmation rules
- wider network access
- audit-log modification
- builder access to production secrets

## 5. Production service identity

Run production MCP as a dedicated low-permission service user where practical.

The service user should have:

- read/write only to required production roots
- no access to user SSH keys
- no browser profiles
- no password stores
- no arbitrary home-directory access
- no package-manager/system-administration authority

## 6. Builder identity

Builder credentials should be distinct from production credentials.

The builder should have:

```text
no production API token
no production database write credentials
no production SSH key
no writable HomeBrain mount
no write access to deployment policy
```

Use sanitized snapshots or synthetic fixtures for tests.

## 7. MCP capability rules

Prefer:

```text
search
read
create
append
propose patch
run named test/lint command
submit feedback
```

Avoid:

```text
run_shell(command)
run_python(arbitrary_code)
write_any_file(path, body)
delete_any_file(path)
install_any_dependency(package)
change_permissions(...)
```

A tool should express a user-meaningful capability, not raw operating-system power.

## 8. Path safety

All filesystem tools must enforce:

- configured allowlisted roots
- normalized paths
- no `..` traversal
- no symlink escape
- hidden-file policy
- file-type policy
- file-size/read limits
- bounded recursion
- structured file IDs where practical

Path-safety tests are mandatory.

## 9. Network policy

Production MCP must not be exposed through raw router port forwarding.

Use an authenticated tunnel or private overlay such as:

- Tailscale
- Cloudflare Tunnel
- equivalent authenticated reverse proxy

Builder network access should be disabled or minimized during tests by default.

Network-enabled build/test steps should be explicit.

## 10. Write safety

For ordinary knowledge roots:

```text
create/append preferred
edit via patch
delete disabled or strongly gated
```

For coding repos:

```text
branch/snapshot
propose patch
run checks
inspect diff
apply/promote
```

Writes should be reversible.

## 11. Deployment authority

The builder may produce:

```text
candidate commit
candidate artifact
migration
eval report
```

A separate deployment gate promotes it.

The deployment mechanism should verify at minimum:

```text
known repo
expected branch/commit
tests/evals passed
security policy unchanged or separately approved
migration compatibility
rollback path
```

Early versions may keep final promotion human-triggered.

## 12. Feedback security

`submit_agent_feedback` should be append-only from the agent's perspective.

The downstream agent may report observations.

It must not use the feedback interface to:

- mutate production ranking directly
- modify security policy
- self-approve code
- deploy code
- alter audit records

Feedback is evidence for the build system, not an instruction channel with authority.

## 13. Audit integrity

Log every MCP/tool action with:

```text
timestamp
client identity
tool
argument summary
root/repo
files read
files written
command name
duration
status
error
run_id / trace_id
change/patch id
```

Audit logs should be append-oriented and harder for the builder to modify than ordinary project data.

Avoid logging full sensitive contents unless necessary.

## 14. Secrets

Never expose to model context unless explicitly required:

- private keys
- API tokens
- OAuth tokens
- passwords
- SSH keys
- certificates
- `.env` secrets

Use redaction and file exclusion.

The builder should receive test credentials, not production credentials.

## 15. Sensitive personal data

Early production scope should continue to exclude private third-party data unless intentionally added.

Especially avoid automatic ingestion of:

- Devyn's private messages/files
- private legal documents
- bank/tax records
- medical records

Adding a sensitive domain requires an explicit threat-model review.

## 16. Permission tiers

Retain explicit capability levels:

```text
0 no access
1 metadata only
2 read-only content
3 create-only
4 append-only
5 patch-propose
6 patch-apply with confirmation
7 allowlisted command execution
8 bounded autonomous maintenance
```

Do not create an unrestricted "Level 9."

## 17. Human gates

Require explicit human approval for changes to:

```text
security policy
permissions
new allowlisted roots
production credentials
dependency installation in production
deployment authority
network exposure
destructive tools
sensitive-data scope
audit retention
builder isolation
```

This can be relaxed only after a separate security review demonstrates why.

## 18. Threat model: self-modifying builder

Assume a coding model can:

- misunderstand instructions
- produce insecure code
- optimize the wrong metric
- follow malicious content in a repo
- make overly broad changes
- accidentally remove safeguards

Therefore builder autonomy is safe only when:

```text
its failures are contained
production secrets are absent
promotion is gated
security invariants are externally enforced
rollback exists
```

The goal is not to make the builder harmless. The goal is to make it unable to cross the production/control boundary.

## 19. Security acceptance tests

At minimum, test that:

- path traversal fails
- symlink escape fails
- hidden secret files are blocked
- unauthorized roots are blocked
- production feedback cannot mutate memory/code
- builder credentials cannot access production data
- arbitrary shell is unavailable through MCP
- named command allowlists reject unknown commands
- audit entries are emitted on success and failure
- deployment rejects unapproved security-policy changes
