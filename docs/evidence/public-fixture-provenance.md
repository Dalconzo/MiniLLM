# Public Fixture Provenance

The public repository does not contain raw ChatGPT exports, private memory
databases, browser acceptance results, production traces, real corpus candidate
identifiers, or host-specific test output.

Text examples committed under `tests/`, `examples/`, and source-level evaluation
fixtures are synthetic test inputs. They exercise provenance, context loss,
temporal state, conflicting evidence, and domain filtering without representing
claims about a real person. Public fixtures must not be copied from a private
corpus verbatim.

The CI public-surface check rejects known trace classes, including personal
absolute paths, private IPv4 addresses, internal tracker identifiers,
corpus-shaped candidate identifiers, private browser-result paths, and the
specific location/name fixtures removed during the public-readiness review.
Gitleaks separately scans for credentials and secret-like content.

This check is a regression guard, not proof that arbitrary text is anonymous.
Human review remains required before publishing any new corpus-derived fixture.
