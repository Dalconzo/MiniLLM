# MiniLLM

MiniLLM is a pre-release local AI-systems prototype for governed memory,
retrieval, and bounded agent tools. It is designed to make a small local model
more useful without treating every retrieved sentence or model suggestion as
trusted user truth.

The project focuses on the engineering around a model call: provenance,
epistemic status, temporal supersession, domain boundaries, abstention,
inspectable traces, constrained filesystem writes, and human-approved memory
changes.

> **Project status:** designed and validated as a local prototype. Release and
> compatibility claims apply only to an exact tagged revision with its retained
> evidence manifest. The project-authored public release surface is MIT
> licensed. Do not infer production readiness or formal security certification
> from this repository.

## Why Governed Memory

Raw chat history is easy to store and difficult to use safely. A useful memory
system has to distinguish a user observation from assistant advice, a current
state from a superseded one, a relevant analogy from a wrong-domain leak, and a
real absence of evidence from a low-quality nearest neighbor.

MiniLLM keeps raw evidence, review candidates, curated records, and compiled
context separate. Retrieval carries source IDs and epistemic labels. Ranking
can suppress stale, cross-domain, tombstoned, or sensitive material. Context
packets expose uncertainty rather than silently converting model output into
facts. Memory mutation remains explicit, reviewable, and traced.

## Architecture

The canonical implementation is the `local_agent_lab` Python package, shipped
as the `minillm` distribution. `lagent` is the compatibility CLI and Home MCP is
the typed ChatGPT/tool transport.

```text
CLI (`lagent`) and Home MCP transport
                  |
                  v
domain services: ingestion, search, ranking, review, context, promotion
                  |
                  v
SQLite/FTS, embeddings, files, provenance, traces
                  |
                  v
optional local Ollama inference
```

Core retrieval, governance, evaluation, and public-demo tests run without
Ollama, private exports, paid credentials, or network access. The implementation
authority and legacy boundary are documented in
[`docs/architecture/canonical_implementation.md`](docs/architecture/canonical_implementation.md).

## Reproduce Locally

Prerequisites are Git, [uv](https://docs.astral.sh/uv/), and a supported CPython.
The package currently declares Python 3.11 through 3.14. That matrix passed on
Linux, with a Python 3.11 macOS smoke path, in the retained
[`v0.1.0a1` CI run](https://github.com/Dalconzo/MiniLLM/actions/runs/32795673961).

```bash
git clone <repository-url> minillm
cd minillm
uv sync --frozen --all-groups
uv run --frozen lagent health
```

The committed `uv.lock` is the only supported dependency-resolution path. The
macOS host bootstrap delegates to the same frozen install:

```bash
./scripts/bootstrap_mac.sh
```

Private ChatGPT exports, memory databases, credentials, runtime logs, and Home
MCP data are not required for installation or the public verification path.

## Deterministic Demo

The synthetic demo shows provenance-backed retrieval, wrong-domain and stale
evidence suppression, abstention, and a typed governance refusal:

```bash
uv run --frozen python examples/public_demo/run_demo.py
uv run --frozen python examples/public_demo/run_demo.py --json
```

It has no credentials, private data, model dependency, network dependency, or
non-deterministic timestamps. See
[`examples/public_demo/README.md`](examples/public_demo/README.md).

## Verification

The canonical clean-clone verification path depends only on the locked `uv`
environment:

```bash
./scripts/verify.sh
```

An optional `justfile` exposes the same checks plus coverage and wheel helpers:

```bash
just check
just coverage
just wheel
```

The [`v0.2.0a1` release
candidate](https://github.com/Dalconzo/MiniLLM/releases/tag/v0.2.0a1) is
verified by hosted CI across Linux and macOS with Python 3.11 through 3.14. Each
full Linux matrix job reports **735 passed and 6 skipped**; the focused security
job reports **302 passed and 6 skipped**; branch-aware coverage is **75.02%**.
The canonical 28-case evaluation passes **28/28 governed retrieval contracts**.
Release notes link the exact hosted run. Raw local, browser, and host-specific
evidence artifacts are intentionally not published.

Security-oriented tests cover generated-path confinement, traversal and symlink
attacks, ignored and secret-bearing repository inputs, final model egress
redaction, diagnostic persistence, Home MCP boundaries, and isolated deployment
controls. The controls, assumptions, and residual risks are in
[`docs/security/threat_model.md`](docs/security/threat_model.md).

## Evaluation

Run the versioned offline comparison with:

```bash
./scripts/run_evals.sh
```

The current synthetic fixture has 28 cases spanning ordinary retrieval,
no-answer behavior, wrong-domain evidence, stale and conflicting memory,
missing provenance, and sensitive data. It actually executes `no_memory`,
`raw_fts`, `vector_only`, and `governed_hybrid` adapters against the same fixture.

In the latest local working-tree run, the governed variant passed **28/28**
designated retrieval contracts, with MRR `0.973684`, recall@5 `1.0`, abstention
accuracy `1.0`, provenance accuracy `1.0`, and zero measured wrong-domain,
stale-selection, or sensitive-canary leakage. This is a deterministic synthetic
retrieval comparison. It is **not** an online A/B test, production latency
benchmark, generated-answer evaluation, or proof of complete object-memory
correctness. The method, metrics, thresholds, and claim limits are documented in
[`docs/evaluation/offline_synthetic_retrieval_v1.md`](docs/evaluation/offline_synthetic_retrieval_v1.md).

## Security And Privacy Defaults

- Repository indexing respects `.gitignore`, a documented secret denylist,
  private-export rules, and no-follow reads.
- Generated paths must remain relative to an authorized root and are checked
  again at final descriptor-relative I/O.
- Text is redacted again immediately before Ollama egress and diagnostic
  persistence.
- Detailed prompt/model logging is off by default; opt-in diagnostics remain
  redacted and local.
- Home MCP exposes typed allowlisted tools rather than arbitrary filesystem or
  shell access.
- Assistant-authored memory remains a suggestion until explicit confirmation.

Pattern redaction is defense in depth, not a guarantee that all private prose is
non-sensitive. The threat model and
[`docs/repository_visibility_policy.md`](docs/repository_visibility_policy.md)
define the supported boundaries. Vulnerability reporting and support scope are
documented in [`SECURITY.md`](SECURITY.md).

## Engineering Evidence

- [`docs/portfolio/decision-evidence-index.md`](docs/portfolio/decision-evidence-index.md)
  connects decisions to constraints, rejected alternatives, tests, and effects.
- [`docs/portfolio/design-changing-failures.md`](docs/portfolio/design-changing-failures.md)
  records failures that changed the architecture.
- [`docs/portfolio/memory-design-rationale.md`](docs/portfolio/memory-design-rationale.md)
  explains memory types, confidence, decay/reinforcement, contamination controls,
  provenance, and routing.
- [`docs/portfolio/ai-assisted-development.md`](docs/portfolio/ai-assisted-development.md)
  states how AI was used and where human judgment and validation were required.
- [`docs/architecture/adr/`](docs/architecture/adr/) contains the sanitized
  primary architecture decisions included in the public release.

## Current Limitations

- The offline vector baseline is deterministic test machinery, not a production
  embedding-quality benchmark.
- The evaluation measures governed retrieval contracts, not generated-answer
  quality, online superiority, or complete object-memory correctness.
- The full private-corpus deployment depends on local Ollama, host-specific
  service isolation, credentials, and data that are not distributed here.
- Candidate extraction and structured/object-memory behavior need broader
  end-to-end evaluation beyond retrieval contracts.

The defensible description remains: **designed and validated a local prototype
for governed memory and bounded AI tooling**. It is not a production-readiness
or formal-security claim. The next substantive release should be
`v0.2.0a1`; its scope is defined in
[`docs/roadmap/v0.2.0a1.md`](docs/roadmap/v0.2.0a1.md).

## License

Project-authored source and public documentation are available under the
[MIT License](LICENSE). Third-party dependencies and separately downloaded model
weights remain governed by their own licenses.
