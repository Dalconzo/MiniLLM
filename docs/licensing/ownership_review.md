# MiniLLM Ownership and License Review

Status: approved for the public release boundary described below.

This is an engineering distribution review, not legal advice.

## Evidence collected

- The reviewed Git history uses one author identity. Commit counts and personal
  email addresses are intentionally omitted because they are not ownership
  evidence and become stale.
- The lock-wide Python inventory covers 51/51 third-party package names. Most use
  MIT, BSD, ISC, Apache-2.0, or PSF terms; the development-only `pathspec`
  dependency uses MPL-2.0. The machine-readable snapshot is
  `docs/licensing/dependency_inventory.json`.
- Configured model weights are downloaded, not committed. Qwen and Nomic publish
  Apache-2.0 terms; Llama 3.2 uses Meta's separate community license and acceptable
  use policy.
- Private ChatGPT exports, Apple Health exports, Home MCP runtime data, logs,
  indexes, and local credentials must remain outside any public release.

## Owner attestation

On 2026-08-23, David Dalconzo confirmed that he owns the project and selected the
MIT License. This attestation covers the public-eligible source, tests, scripts,
configuration, prompts, synthetic fixtures, and newly authored public
documentation. Private exports, historical design inputs, runtime artifacts,
and excluded media are not part of the licensed release.

| Material | Current status | Required decision |
| --- | --- | --- |
| `src/`, `tests/`, `scripts/`, configuration, and current docs | Owner attestation recorded | Include only files accepted by the public-candidate policy |
| `Adaptive Stigmergic Memory Prototype.pdf` | Tracked only in private development history; excluded by the public-candidate allowlist | Keep excluded; no redistribution right is claimed or required for the project release |
| root historical specs (`*_spec.md`, `SPEC.md`, `home_mcp_scope_for_codex.md`, and related files) | Private design history; excluded by the public-candidate allowlist | Keep excluded; publish only the newly authored architecture/portfolio summaries under `docs/` |
| prompts and synthetic fixtures | Covered by owner attestation; independently scanned for exact private-corpus overlap | Include only synthetic/public-safe fixtures that pass the release gates |
| test-result reports | Browser/private run reports are excluded by the public-candidate allowlist | Publish only a newly generated sanitized release artifact after final acceptance |
| logos, screenshots, photos, and generated visual assets | None are required for the current package; private baking images exist outside tracked source | Keep private imagery excluded; no visual asset rights are needed for the initial release |

## Compatibility decision

The selected MIT project license is compatible with the observed Python
dependency model because dependencies are installed as separate, unmodified
packages rather than copied into this repository. The MPL-2.0 development
dependency remains under its own terms. Downloaded model weights are not
redistributed and retain their upstream terms.

The project license should explicitly cover project-authored source and docs only.
Third-party dependencies and downloaded model weights remain under their own terms.
Any excluded historical or third-party artifact should carry its own notice or be
removed before the public release.

## Release check

Before preparing a public release:

1. regenerate or verify the dependency inventory against the final lockfile;
2. run the public-candidate and exact private-corpus overlap scans;
3. verify `LICENSE` and package metadata agree;
4. bind the inventory and decision to the release commit.
