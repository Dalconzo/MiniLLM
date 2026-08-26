# ADR 0002: Isolate Builder and Production Authority

Status: accepted and implemented in deployment tooling

## Constraint

An AI-assisted builder that can edit code must not also possess production
credentials, modify production memory, rewrite audit history, weaken policy, or
approve and deploy its own security changes.

## Decision

Use separate noninteractive builder and production identities, private and
disjoint credential/data roots, immutable root-owned releases, and a narrow
root-owned administration helper. Mutating production operations require a
separately reviewed, expiring, digest-bound request. Deployment performs
fail-closed preflight checks, records an audit event, and preserves automatic
rollback.

The builder can prepare and test a candidate but cannot access production
credentials, deployment authority, or the trusted repository. The production
service can run a reviewed release but cannot modify the release or control
plane.

## Rejected Alternatives

- Run build and production under one service identity.
- Let the builder invoke arbitrary passwordless root commands.
- Trust checks or manifests from a user-writable candidate checkout.
- Promote control-plane changes through the ordinary application path.

## Validation

- Identity-boundary tests deny shared identities and cross-plane access.
- Promotion tests reject candidate-supplied control-plane authority.
- Administration-helper tests reject path, argument, environment, replay, and
  unreviewed-request injection.
- Host verification checks ownership, permissions, process identity, audit
  integrity, cutover, and rollback independently of source tests.

## Consequence

Deployment requires more ceremony, but the compromise of an editing agent does
not automatically grant production control. Source tests establish the
contract; claims about a specific host still require retained host evidence.
