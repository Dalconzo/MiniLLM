# Security Policy

## Supported Status

MiniLLM is a pre-release local prototype. The latest published alpha is
[`v0.1.0a1`](https://github.com/Dalconzo/MiniLLM/releases/tag/v0.1.0a1). Security
fixes are handled on a best-effort basis for the latest tagged alpha; there is
no support SLA, production-readiness claim, formal certification, or guarantee
that older revisions will receive patches.

## Reporting A Vulnerability

Use the repository's **Security** tab to submit a private vulnerability report
when private reporting is available. If it is unavailable, contact the
maintainer through the [GitHub profile](https://github.com/Dalconzo) and request
a private reporting channel without including exploit details in a public issue.

Include the affected commit or tag, operating system, reproduction steps,
expected boundary, observed behavior, and whether real private data or
credentials may have been exposed. Do not test with real credentials, private
exports, production databases, or another person's data.

## Data And Credential Boundaries

The public verification path uses synthetic fixtures and requires no ChatGPT
export, Home MCP data, production database, model credential, tunnel token, or
paid API. Private deployments must keep those inputs outside the repository and
outside generated public evidence.

Never commit:

- ChatGPT or health exports, personal documents, private-corpus canary lists, or
  derived private indexes;
- SQLite databases, embeddings, prompts containing retrieved private text,
  runtime traces, Home MCP data, or browser acceptance artifacts;
- API keys, OAuth material, tunnel credentials, private keys, cookies, `.env`
  files, service-account files, or credential-bearing configuration;
- production service state, audit logs, host-specific paths, or administrator
  review requests.

Use synthetic data for bug reports. Revoke exposed credentials through their
provider before discussing the incident publicly.

## Security Scope And Limits

MiniLLM enforces authorized-root path checks, no-follow file access, repository
visibility rules, final prompt/diagnostic redaction, typed tool boundaries, and
human approval for memory truth changes. See
[`docs/security/threat_model.md`](docs/security/threat_model.md).

Redaction is defense in depth, not proof that arbitrary prose is non-sensitive.
Pattern scanners cannot recognize every secret or infer all private meaning.
The security suite uses adversarial and synthetic cases; it does not constitute
a penetration test, third-party audit, formal verification, or assurance about
an operator's host configuration. Local administrators and compromised
dependencies remain outside the controls that application tests can prove.
