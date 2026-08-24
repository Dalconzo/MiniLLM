# Release-Candidate Verification

Verification date: 2026-08-23

Candidate basis: the committed MiniLLM snapshot through `f8c4637`, plus the
public-surface sanitation changes in this release candidate.

## Automated suite

```text
PYTHONPATH=src pytest -q
471 passed in 17.04s
```

Environment: macOS, Python 3.11.15. The suite was rerun with localhost socket
binding enabled because six HTTP integration tests correctly failed when the
execution sandbox prohibited even loopback binds. With loopback enabled, all
471 tests in the final sanitized public suite passed.

## Security and privacy scan

The candidate was checked with Gitleaks 8.30.1 using full redaction and reported
zero findings. `python scripts/check_public_surface.py` also reported a pass
after corpus-derived browser runbooks and identifiers were removed.
Repository-specific checks additionally reject:

- credential-like and ignored files entering repository indexes;
- symlink and path traversal escapes;
- arbitrary shell or unknown MCP tools;
- unreviewed feedback changing retrieval;
- builder identity authorizing or accessing production state.

The [public fixture provenance record](public-fixture-provenance.md) documents
what was excluded and the limits of the automated privacy regression check.

## Reproduce

```bash
python -m pip install -e '.[dev]'
python scripts/check_public_surface.py
pytest -q
```

Focused boundary tests are listed in the root README. CI repeats the complete
suite on every push and pull request.

## What this does not prove

This record does not attest to a private corpus, a specific host's runtime state,
model quality on arbitrary tasks, external penetration testing, or formal safety
certification.
