# Security Policy

MiniLLM is pre-release software for local agent-memory and tool workflows. Do not
expose the raw service directly to the public internet or load sensitive data
without reviewing the documented trust boundaries.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository. Do not open a
public issue containing credentials, private conversation text, internal host
details, exploit payloads, or sensitive traces.

Include the affected commit, the smallest safe reproduction, expected versus
observed behavior, and whether secrets or private data may have been exposed.

## Supported version

Only the current `main` branch is evaluated during pre-release development.

## Security model

The detailed [threat model](docs/security/threat-model.md) covers assets, trust
boundaries, controls, verification, residual risk, and release conditions.

Public test data must follow the [fixture provenance policy](docs/evidence/public-fixture-provenance.md).
Do not commit raw exports, real corpus candidate identifiers, browser acceptance
results, production traces, or verbatim corpus-derived fixtures.
