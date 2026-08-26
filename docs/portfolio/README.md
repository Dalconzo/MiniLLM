# Portfolio Decision Evidence

This directory explains the engineering decisions behind MiniLLM and links them
to repository evidence. It is not a release-readiness claim. Historical browser
acceptance passed in sanitized form for `v0.1.0a1`; raw private reports remain
excluded. The object/world-model architecture remains only partially implemented.

## Documents

- [AI-assisted development](ai-assisted-development.md) describes where AI was
  used, what the repository can verify, and where human review remains required.
- [Decision evidence index](decision-evidence-index.md) links constraints,
  rejected alternatives, tests, consequences, and implementation status.
- [Design-changing failures](design-changing-failures.md) records failures that
  changed retrieval, context, recipe, and observability behavior.
- [Memory design rationale](memory-design-rationale.md) explains the memory
  layers, confidence model, feedback lifecycle, contamination controls,
  provenance, and routing, with as-built and planned behavior kept separate.

## Evidence rule

The documents use three evidence labels:

- **As-built**: represented in current source and covered by a named test.
- **Observed**: retained in a committed acceptance report with run/trace IDs.
- **Planned**: specified in an ADR or normative architecture document but not
  demonstrated as a complete implementation.

An architecture document is evidence of intent, not evidence that the feature
exists. A test is evidence of the tested behavior, not a production-readiness
claim. Host-specific production state is not inferred from source files.
