# Diagnostic logging policy

MiniLLM diagnostic events use schema `minillm_diagnostics_v1`. This policy applies
to CLI run events, memory traces, Home MCP traces, reports, and run artifacts.

## Default mode

`runtime.save_full_prompts` is `false` in committed configurations. The default
mode records only allowlisted operational metadata needed to locate a failure:
run and trace IDs, stages, timestamps, model and command identity, source IDs,
counts, scores, safe paths, status, and error codes. Unknown structured fields and
private-prose fields are fail-closed to omission metadata. This includes claims,
titles, notes, candidate text, feedback observations, recipe ingredients and
steps, and request/result bodies. Detailed text artifacts are likewise replaced
with omission metadata.

All strings that remain eligible for persistence pass the secret redactor at the
final boundary. It removes recognized tokens and private keys, complete
Authorization/Bearer values, complete line-scoped credential assignments, and URL
userinfo. This final check is independent of ingestion-time redaction.
Append and artifact writes use descriptor-relative no-follow operations and private
file modes so a symlink swap cannot redirect diagnostic output outside its run
root.

## Sanitized detailed mode

Setting `runtime.save_full_prompts: true` is an explicit local debugging choice.
The name is retained for configuration compatibility; it does not bypass the
final secret redactor. This mode may retain non-secret prompt, response, diff, and
retrieved text, so it must not be enabled for shared reports, public fixtures, or
routine production operation. Disable it after the targeted debugging session and
review any retained artifacts before sharing them.

## Persisted artifacts

| Artifact | Default contents | Detailed mode |
| --- | --- | --- |
| `events.jsonl` | Versioned run lifecycle metadata; request and result bodies omitted | Lifecycle events remain metadata-only |
| `trace.jsonl` | Versioned stage, source ID, count, score, and sanitized error metadata | Sanitized trace details may be retained |
| JSON reports | Structured values with sensitive text fields omitted | Sanitized detailed fields may be retained |
| Text, Markdown, HTML, diff, patch, and log artifacts | Omission/redaction metadata | Sanitized content may be retained |

## Verification

- `tests/test_prompt_packing.py` covers default omission, opt-in sanitized detail,
  private-domain field omission, line-complete credential and URL-userinfo
  redaction, final model egress redaction, trace sanitization, and final event-log
  symlink rejection.
- `tests/test_memory_audit.py` and `tests/test_home_mcp.py` verify trace structure
  and Home MCP audit behavior.
- `docs/security/path_boundary_inventory.md` inventories diagnostic write roots.

This is a leakage-reduction control, not a claim that arbitrary secrets can always
be recognized. Ignored/denied source exclusion and least-privilege filesystem
boundaries remain independent required controls.
