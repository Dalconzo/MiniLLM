# AI-Assisted Development

MiniLLM is developed with AI assistance under a human-owned engineering loop.
The repository does not claim line-by-line human or model authorship percentages.

## Responsibility model

David owns:

- problem framing and user value;
- architecture, data contracts, and system constraints;
- threat models and trust boundaries;
- evaluation design and acceptance criteria;
- review of generated changes;
- release, rollback, and production authorization.

AI tools accelerate:

- implementation variants and focused patches;
- test and fixture generation;
- code review and failure localization;
- adversarial scenario discovery;
- documentation and regression-loop iteration.

## Acceptance rule

Model output is proposed work, not proof. A suggestion becomes part of the system
only after it is reconciled with the architecture, exercised by an observable
test or evaluation, reviewed for security and privacy impact, and accepted by a
human-controlled release path.

Agent feedback follows the same rule at runtime. Submission alone does not alter
memory truth or ranking. Only a separately reviewed and explicitly applied
control can affect retrieval, and that control retains provenance and rollback
metadata.

## Evidence boundary

- Tests demonstrate the behavior they exercise; they do not certify a deployed
  host or establish formal security assurance.
- Synthetic fixtures are public. Private conversations, credentials, logs, and
  host-specific acceptance traces are intentionally absent.
- Architecture documents describe intended constraints. The README labels
  current limitations rather than presenting planned behavior as complete.
