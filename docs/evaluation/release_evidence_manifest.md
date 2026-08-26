# Release Evidence Manifest

MiniLLM remains pre-release until one clean commit satisfies every gate in the
`minillm_release_evidence_manifest_v1` contract. The manifest builder validates
explicit evidence; it does not run commands, infer success from prose, or fill
missing results.

## Artifact contract

Each supplied JSON file uses `minillm_release_evidence_artifact_v1`:

```json
{
  "artifact_type": "full_tests",
  "command": {
    "argv": ["uv", "run", "--frozen", "pytest", "-q"],
    "exit_code": 0
  },
  "generated_at": "2026-08-24T11:00:00Z",
  "metrics": {
    "tests_errors": 0,
    "tests_failed": 0,
    "tests_passed": 619
  },
  "repository": {
    "git_commit": "<40-hex commit>",
    "git_dirty": false,
    "git_tree": "<40-hex tree>",
    "uv_lock_sha256": "<64-hex digest>"
  },
  "schema_version": "minillm_release_evidence_artifact_v1",
  "status": "pass"
}
```

The versioned policy embedded in the output declares the complete metric
contract. Required artifacts are:

- `license_review`: explicit ownership and dependency-compatibility approval,
  bound to the exact `LICENSE` digest.
- `full_tests`: at least 176 passing tests and no failures or errors.
- `security_subset`: at least 25 passing adversarial tests and no failures or
  errors.
- `coverage`: branch-aware measured coverage of at least 70 percent.
- `four_variant_eval`: all four real variants execute all 28 or more cases and
  the governed variant meets the predeclared retrieval, abstention, provenance,
  wrong-domain, stale-state, leakage, and contract thresholds.
- `deterministic_demo`: the canonical private-data-free demo passes its seven
  behavior checks over the four executed variants.
- `clean_clone`: locked install, full tests, demo, history/privacy scan, and
  parentless public-candidate checks pass from a fresh clone.
- `hosted_ci`: Linux and macOS coverage, Python 3.11 through 3.14, lint, format,
  type, and security jobs pass.
- `browser_acceptance`: a sanitized, non-partial pass retains run/trace IDs and
  does not mutate memory truth.

Every artifact must identify the same commit, tree, clean state, and lockfile as
the repository being released. Evidence older than its declared maximum age is
blocked. The validation time is an explicit `--as-of` argument so identical
inputs produce identical output.

## Pre-release check

Run this while evidence is still incomplete. It exits zero after writing a
private `0600` blocker report, but never emits top-level `pass` or sets
`release_ready`:

```bash
export MINILLM_EVIDENCE_DIR="$TMPDIR/minillm-release-evidence"
mkdir -p "$MINILLM_EVIDENCE_DIR/evidence"
uv run --frozen python scripts/build_release_evidence_manifest.py \
  --repo . \
  --mode pre-release \
  --as-of 2026-08-24T12:00:00Z \
  --output "$MINILLM_EVIDENCE_DIR/manifest.json"
```

Missing hosted CI, browser acceptance, license approval, or any other artifact
is reported as a blocker. A dirty repository is also a blocker.

## Final validation

Final mode takes one explicit path per artifact:

```bash
uv run --frozen python scripts/build_release_evidence_manifest.py \
  --repo . \
  --mode final \
  --as-of 2026-08-24T12:00:00Z \
  --output "$MINILLM_EVIDENCE_DIR/manifest.json" \
  --license-review "$MINILLM_EVIDENCE_DIR/evidence/license-review.json" \
  --full-tests "$MINILLM_EVIDENCE_DIR/evidence/full-tests.json" \
  --security-subset "$MINILLM_EVIDENCE_DIR/evidence/security-subset.json" \
  --coverage "$MINILLM_EVIDENCE_DIR/evidence/coverage.json" \
  --four-variant-eval "$MINILLM_EVIDENCE_DIR/evidence/four-variant-eval.json" \
  --deterministic-demo "$MINILLM_EVIDENCE_DIR/evidence/deterministic-demo.json" \
  --clean-clone "$MINILLM_EVIDENCE_DIR/evidence/clean-clone.json" \
  --hosted-ci "$MINILLM_EVIDENCE_DIR/evidence/hosted-ci.json" \
  --browser-acceptance "$MINILLM_EVIDENCE_DIR/evidence/browser-acceptance.json"
```

Final mode exits nonzero unless every artifact and repository gate passes. Keep
the manifest outside the checkout or in an ignored release-artifact directory;
the release commit itself must remain clean. The output binds each source
artifact by SHA-256 while retaining only allowlisted metrics and checks, not raw
diagnostic content or local paths.

The ownership artifact is intentionally manual. Do not create it until the
repository owner has approved distributability and selected a compatible
license. Hosted CI and browser evidence likewise must come from their actual
runs; synthetic evidence is valid only in the manifest builder's tests.

## Local and clean-clone evidence runners

`scripts/run_release_evidence.py` executes the five gates that can be proven
inside a local checkout. It does not create ownership/license, hosted CI,
clean-clone, or browser-acceptance evidence.

Final mode requires a clean repository and an artifact directory outside the
checkout. It verifies that the runner, security subset, evaluation script,
canonical demo, coverage configuration, and lockfile are committed before it
runs:

```bash
uv run --frozen python scripts/run_release_evidence.py \
  --repo . \
  --mode final \
  --output-dir "$MINILLM_EVIDENCE_DIR/local"
```

The runner invokes these real workflows:

- the complete pytest suite, with JUnit XML used only as machine input;
- the committed `scripts/run_security_tests.sh` subset, with JUnit XML
  requested through `PYTEST_ADDOPTS`;
- a second complete pytest run under branch coverage using the committed
  `[tool.coverage]` configuration, parsed from Coverage.py JSON;
- all four adapters through `scripts/run_evals.sh`, parsed from the canonical
  `memory-eval-v1.json` report;
- the canonical deterministic public demo in JSON mode.

Each output artifact records a stable repository-relative command label, exit
code, portable environment labels, and SHA-256/byte count of stdout and stderr.
Execution-only absolute paths are removed while that command evidence is
constructed, not rewritten after JSON serialization. Raw command output is not
copied into release evidence. Artifacts are atomically written as
single-link `0600` files and bind the Git commit, Git tree, worktree state, and
`uv.lock` digest. The repository identity is checked again after all commands;
any source or lockfile change invalidates the run.

For iterative work, development mode may run a dirty checkout:

```bash
uv run --frozen python scripts/run_release_evidence.py \
  --repo . \
  --mode development \
  --output-dir "$MINILLM_EVIDENCE_DIR/development"
```

Development artifacts report `status: development_pass`, preserve
`repository.git_dirty: true`, and set `final_manifest_eligible: false`. The
manifest validator therefore rejects them as final evidence even when every
underlying command succeeds. The runner summary counts those successful local
commands under `artifacts_succeeded`; `artifacts_passed` counts only clean-commit
artifacts accepted for final release evidence.

On a clean release commit, pass the five generated files to the corresponding
manifest options:

```text
full-tests.json             -> --full-tests
security-subset.json        -> --security-subset
coverage.json               -> --coverage
four-variant-eval.json      -> --four-variant-eval
deterministic-demo.json     -> --deterministic-demo
```

The four externally established artifacts remain mandatory before final mode
can produce `release_ready: true`.

### Clean-clone evidence

`scripts/run_clean_clone_evidence.py` proves the reproducible-install gate from
an explicit clean source repository and its checked-out commit. It invokes the
committed `create_public_release_candidate.py`, producing a new repository with
one parentless commit and no remotes. It proves that `.venv` did not exist in
that candidate, runs the candidate's canonical `./scripts/verify.sh`, and then
runs the candidate creator again in independent `--check-only` mode.

Forbidden-corpus literal lists are private inputs and must be regular files
outside the source repository. Repeat `--forbidden-literals-file` for each
external list. Candidate, release-manifest, and evidence paths must not already
exist and must be outside the source checkout:

```bash
uv run --frozen python scripts/run_clean_clone_evidence.py \
  --source-repo . \
  --source-commit HEAD \
  --candidate-dir "$MINILLM_EVIDENCE_DIR/public-candidate" \
  --candidate-manifest "$MINILLM_EVIDENCE_DIR/public-candidate-manifest.json" \
  --artifact "$MINILLM_EVIDENCE_DIR/evidence/clean-clone.json" \
  --forbidden-literals-file "$MINILLM_EVIDENCE_DIR/private-overlap-canaries.txt"
```

The runner independently validates the candidate manifest, source and output
commit/tree identities, `uv.lock` digest, clean worktree, single parentless
history, absence of remotes, and successful privacy scan. The resulting
`clean_clone` artifact binds to the private source commit/tree/lock identity,
uses `minillm_release_evidence_artifact_v1`, contains exactly the six metrics
required by the release manifest, and is created as a private `0600` file.

Subprocess stdout and stderr are never copied into the artifact or an error.
Every recorded command and command failure retains only its stage, portable
argument labels, exit code, byte counts, and SHA-256 digests. Run the complete
candidate and evidence bundle through `scripts/audit_public_artifacts.py` with
the same external private-overlap canaries before publication. A failed run leaves no
passing artifact; existing outputs are never overwritten. Supply a successful
artifact to the final manifest builder with `--clean-clone`.
