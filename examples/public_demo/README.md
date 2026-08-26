# MiniLLM Canonical Governed-Memory Demo

This deterministic demo is a short presentation layer over MiniLLM's canonical,
versioned offline memory evaluation. It does not contain its own tokenizer,
ranker, relevance threshold, or governance engine.

Each run calls `run_memory_eval_v1`, which builds the shared synthetic corpus,
ingests it through MiniLLM, and executes the `no_memory`, `raw_fts`,
`vector_only`, and `governed_hybrid` adapters across the same 28 contracts. The
demo then presents selected stable outcomes from that run:

- source-backed retrieval with conversation, role, subject, and evidence
  provenance;
- wrong-domain filtering;
- stale or superseded state suppression;
- explicit no-answer abstention;
- a typed refusal of unsupported advice;
- an actually failed retrieval contract from the `no_memory` baseline.

The fixture is defined only by `memory_eval_v1_conversations()` and
`memory_eval_v1_cases()` in `src/local_agent_lab/evals/memory_v1.py`. There is no
second demo fixture to drift from the evaluation contract.

## Run

Install exactly the committed lockfile environment:

```bash
uv sync --frozen --all-groups
```

Run the human-readable demo:

```bash
uv run --frozen python examples/public_demo/run_demo.py
```

Run the stable machine-readable form:

```bash
uv run --frozen python examples/public_demo/run_demo.py --json
```

Run its focused acceptance tests:

```bash
uv run --frozen pytest -q tests/test_public_demo.py
```

The demo is offline, deterministic, model-free, credential-free, and uses no
private data. Stable JSON intentionally omits generation timestamps, local
paths, latency, repository dirtiness, and machine/runtime identity. It is a
demonstration of governed retrieval contracts, not a production-performance or
human answer-quality claim.
