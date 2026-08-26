# MiniLLM Threat Model

Status: pre-release. This document describes the as-built local prototype and its
remaining risks; it is not a claim of formal security certification.

## Assets and trust boundaries

MiniLLM protects private conversation history, curated memory, notes and recipes,
repository source, local credentials, model prompts, diagnostic artifacts,
production service credentials, and deployment authority.

The main boundaries are:

1. repository files entering the index and retrieval system;
2. retrieved memory entering context packets and model prompts;
3. model output becoming a patch, destination path, note, or tool request;
4. external MCP clients invoking typed Home MCP tools;
5. prompts leaving the Python process for a local Ollama endpoint;
6. traces, reports, errors, and model outputs being persisted;
7. builder/staging code approaching production data and deployment controls;
8. administrator requests authorizing immutable releases and rollback.

## Assumptions and non-goals

- The host OS, root account, and local administrator are trusted. MiniLLM does not
  defend against a compromised root account or malicious kernel.
- Ollama is expected to bind to a trusted local interface. Content is redacted at
  the final client boundary, but MiniLLM does not sandbox model execution.
- Home MCP is exposed through the configured tunnel and typed tool surface. Its
  trusted-proxy OAuth route is accepted only when the HTTP server and caller are
  loopback. This preserves the local tunnel path but does not authenticate an
  arbitrary public bind. It is not safe to expose the raw service publicly.
- Secret-pattern detection is defense in depth, not proof that arbitrary private
  prose is non-sensitive. Source visibility and disclosure controls remain primary.
- Generated patches are proposals. Applying a safe path does not make generated
  code correct; code review and tests remain required.

## Threats, controls, and evidence

| Surface | Threat | As-built control | Verification | Residual risk |
| --- | --- | --- | --- | --- |
| Repository input | Ignored credentials, keys, exports, or symlinks enter indexes and prompts | Git-compatible ignore evaluation, fail-closed secret denylist, private-export detection, no-follow reads, resolved-root checks | `tests/test_repo_indexer.py` | Denylist cannot identify every unusually named secret; public release still requires an inventory scan |
| Retrieved memory | Wrong-domain, stale, assistant-authored, tombstoned, or sensitive evidence is treated as current user truth | Domain governance, provenance and epistemic labels, relevance floor, temporal supersession, disclosure tiers, source blocking | `tests/test_memory_search.py`, `tests/test_memory_high_risk_search.py`, `tests/test_memory_privacy.py`, private browser acceptance artifacts | Candidate extraction and context quality remain evaluation targets; no autonomous truth mutation is allowed |
| Retrieved text | Prompt injection inside repository or memory content changes authority | Retrieved material is data inside typed prompts; it receives no shell, deployment, or policy authority; tools remain allowlisted | `tests/test_deployment_gate.py`, `tests/test_home_mcp.py`, `tests/test_trusted_promotion.py` | Local models may still follow malicious prose and produce a bad suggestion; human review and safe output boundaries are required |
| Model paths | Absolute, traversal, encoded, mixed-separator, nested-parent, or symlink paths escape the selected repository/root | Strict relative-path normalization and descriptor-relative no-follow reads/writes; model-created parent trees are refused | `tests/test_patches.py`, `tests/test_home_mcp.py` | Safe path handling does not validate semantic appropriateness of an in-root target |
| Filesystem writes | A path changes after validation or a symlink redirects the final write | Validation is repeated through directory descriptors at final I/O; Home MCP roots are typed and writable/read-only policy is checked | `tests/test_patches.py`, `tests/test_home_mcp.py` | Platforms without required descriptor-relative/no-follow support are not currently claimed |
| MCP/tool calls | Client requests arbitrary shell, unknown methods, hidden files, unauthorized roots, deletes, or unrestricted writes | Typed tools, root allowlist, read/write capability split, no shell tool, structured errors, per-call traces; `none` and unauthenticated `mixed` are read-only; bearer mutations require constant-time token validation; trusted-proxy OAuth mutations require a loopback bind and loopback caller | `tests/test_home_mcp.py` | A compromised local process can use the intentionally trusted loopback OAuth route; tunnel/account compromise can invoke exposed tools within their granted scope |
| Model egress | A missed caller-side check sends a secret to Ollama | `OllamaClient.generate` and `embed` apply final secret redaction to prompt, system, and embedding text | `tests/test_prompt_packing.py` | Pattern redaction cannot detect every private fact; visibility and minimization still matter |
| Diagnostics | Prompts, claims, titles, notes, recipe content, feedback prose, responses, or secrets persist in logs/reports/traces | Metadata-only logging uses a structural allowlist and fail-closed omission; result/request bodies and private-prose fields are omitted; final redaction covers line-complete credentials, Authorization/Bearer values, URL userinfo, tokens, and private keys; opt-in debug remains secret-redacted; private file modes are enforced | `tests/test_prompt_packing.py`, `tests/test_home_mcp.py`, `tests/test_memory_observability.py` | Sanitized debug intentionally retains non-secret detail and must be enabled only for controlled local diagnosis |
| Builder/production | Builder code reads or mutates production memory, credentials, audit logs, tunnel config, or deployment authority | Separate hidden service identities, disjoint private roots, root-owned immutable releases, one-way sanitized snapshots, negative access verification | `tests/test_service_identity_boundaries.py`, `scripts/verify_service_identity_boundaries.sh`, retained release evidence | A local administrator can override OS controls by design |
| Deployment | Candidate self-approves, substitutes artifacts, replays approval, or leaves a failed service live | Check-only gate, root-private reviewed requests, artifact hashes, one-time expiry, human review, smoke tests, audit chain, automatic rollback | `tests/test_deployment_gate.py`, `tests/test_minillm_admin_helper.py`, `tests/test_production_cutover.py` | A separately authorized live rollback drill remains an owner operation |

## Home MCP authentication modes

| Mode | Read requests | Mutating tools |
| --- | --- | --- |
| `none` | Allowed | Denied by default |
| `mixed` | Allowed without a token | Require a valid bearer token by default |
| `bearer` | Require a valid bearer token | Require a valid bearer token |
| `oauth` | Require the loopback trusted-proxy route | Require the loopback trusted-proxy route |

`home_mcp.unsafe_allow_unauthenticated_mutations: true` (or the equivalent
environment variable) is an explicit unsafe local-development override for
`none`/unauthenticated `mixed`; committed configuration keeps it false. Launchd
stores bearer material in a mode-0600 single-link regular file and places only
that file's path in the plist environment. Tokens are not placed in launchd
`ProgramArguments` or plist values.

The existing secure-tunnel deployment must launch Home MCP with
`--auth-mode oauth`; launching it with `none` intentionally leaves browser reads
available but blocks browser writes.

The production provisioner and bootstrap enforce this as a deployment contract:
the Home MCP daemon binds to `127.0.0.1`, its root-private plist selects
`--auth-mode oauth`, and neither command arguments nor plist environment values
contain bearer tokens. Bootstrap validates the generated plist, verifies that the
tunnel targets the same local port, and requires the live shadow health response
to report proxy-handled OAuth with unsafe unauthenticated mutations disabled.
`none` and unauthenticated `mixed` remain supported only as separate read-only
application modes; they are not accepted by the tunnel-facing production path.

## Security release conditions

A resume or release may describe the prototype as security-boundary tested only
after the following evidence is retained for the release commit:

- adversarial path, symlink, ignored-file, secret-egress, diagnostic-leakage, MCP,
  and deployment subsets pass;
- the public fixture corpus yields zero sensitive-data leakage;
- detailed logging is disabled in the released default configuration;
- dependency and ownership review is complete;
- the final clean-clone and browser acceptance reports pass;
- residual risks and unsupported platforms remain visible in the README.
