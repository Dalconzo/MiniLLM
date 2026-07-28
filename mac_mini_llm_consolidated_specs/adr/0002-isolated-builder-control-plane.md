# ADR 0002: Separate Builder Authority from Production and Security Authority

**Status:** Accepted  
**Date:** 2026-07-27

## Context

The project intends to automate a loop in which downstream AI usage generates feedback, Codex implements fixes, and the system evaluates candidates.

The Mac mini is dedicated to the project, so loss of the physical machine is not the primary concern. However, a builder with production authority could corrupt memory state, weaken permissions, alter audit logs, or deploy insecure changes.

## Decision

The builder may run on the same physical Mac but must be isolated from production authority.

Recommended boundary:

```text
production service / data
        |
append-only feedback
        v
builder VM / staging
        |
tested candidate artifact
        v
separate deployment gate
```

The builder must not possess production credentials or authority to modify the security/control plane.

## Consequences

Codex can be highly autonomous inside staging while failures remain contained.

Security and deployment changes remain human-gated until explicitly revisited by a later ADR.
