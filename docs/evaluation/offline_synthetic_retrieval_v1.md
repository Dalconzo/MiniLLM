# Offline Synthetic Retrieval Evaluation v1.2

## Scope

This evaluation is a deterministic offline comparison of four retrieval contracts over the same synthetic fixture corpus:

- `no_memory`: consistently returns no context and refuses memory-grounded advice.
- `raw_fts`: SQLite FTS5 retrieval without governance.
- `vector_only`: deterministic token-hash vectors for test comparison only.
- `governed_hybrid`: the production governed search function over the synthetic corpus.

It is not an online A/B test, a production latency benchmark, an evaluation of generated answer quality, or full validation of structured/object-memory behavior. Reported latency is local fixture runtime. The deterministic vector baseline is unsuitable for production semantic ranking.

## Release Contract

All 28 cases are designated hard gates for the governed variant. A report is `pass` only when:

- every designated hard-gate case passes its retrieval contract;
- every predeclared aggregate threshold passes;
- the fixture database remains immutable during adapter execution; and
- downstream persistence, retrieval-output, and report-output scans find none of the secret canary literals.

The sensitive-data cases use invented canaries in an ephemeral, unredacted ChatGPT-export fixture. The evaluator
proves each canary is present immediately before the real ingestion function receives the export, removes the raw
fixture after ingestion, and then checks that the literals are absent from parsed ingestion artifacts, SQLite/FTS,
embeddings, retrieval outputs, and the machine report. The human report is derived only from that checked machine
payload. Raw input is intentionally not counted as an expected-clean surface.

Any failure produces top-level `status: fail`, `release_assessment.status: pre_release`, and a nonzero exit from `scripts/run_evals.sh`.

## Reproduction

```bash
./scripts/run_evals.sh
```

An explicit artifact directory may be supplied:

```bash
./scripts/run_evals.sh /tmp/minillm-eval-evidence
```

The runner writes:

- `memory-eval-v1.json`: complete machine-readable cases, metrics, thresholds, identities, and diagnostics.
- `memory-eval-v1.md`: concise human-readable result and defensible claim wording.

Each report binds the generation timestamp, invocation identity, Git commit and dirty state, `uv.lock` digest,
dataset/config digests, and a code digest covering the adapters plus governed search, ranking, domain, embedding,
and ingestion logic. Filesystem locations are represented by repository-relative or stable labels; public reports do
not preserve local home-directory or temporary-workspace paths.

## Metrics

The report includes MRR, precision@5, recall@5, abstention accuracy, provenance accuracy, wrong-domain rate, stale-selection rate, sensitive-leakage rate, retrieval-contract pass rate, and local fixture latency.

`task_success_rate` remains only as a compatibility alias for `retrieval_contract_pass_rate`. It must not be interpreted as end-task answer quality.

Provenance is correct only when returned source IDs and roles resolve against fixture source records. A `misc` result counts as wrong-domain whenever the case declares an expected domain. Superseded evidence retained after a higher-ranked active correction is reported separately and is not treated as stale truth selection.

## Defensible Claim

Use only the wording emitted in the generated Markdown artifact. A passing report supports this limited claim:

> MiniLLM's governed retrieval variant passed all designated contracts in a deterministic offline synthetic fixture comparison.

It does not support claims of production readiness, online A/B superiority, production latency, or complete structured/object-memory correctness.

## Public Fixture Policy

The corpus is invented for this benchmark. `Northstar` is a fictional starter name, and its correction sequence and
measurements are synthetic. Fixtures must not copy names, measurements, quotations, timestamps, or other distinctive
observations from private exports. Secret strings are synthetic canaries, not credentials.

Retained evidence is regenerated after evaluator changes. Superseded reports are removed rather than presented as
evidence for the current tree. A report generated from a dirty tree remains useful as a development artifact, but it
must retain `git_dirty: true` and must not be described as release evidence for a clean commit.

The private development checkout retains a dirty-tree artifact for diagnosis.
That artifact is excluded from the public candidate and is not final
clean-commit release evidence.
