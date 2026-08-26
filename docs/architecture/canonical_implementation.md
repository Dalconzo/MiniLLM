# Canonical MiniLLM implementation

## Naming contract

- **MiniLLM** is the product and portfolio-project name.
- **`local_agent_lab`** is the canonical Python package. Core behavior, memory
  governance, persistence, model routing, evaluation, and service adapters live
  under `src/local_agent_lab/`.
- **`lagent`** is the compatibility command-line entry point. It delegates to
  `local_agent_lab.cli:app`; it is not a second implementation or product.
- **Home MCP** is a typed transport that exposes selected MiniLLM capabilities
  to MCP clients. It is not the domain layer and does not define a separate
  memory implementation.

This distinction lets documentation use a stable product name while preserving
the command users and deployment scripts already invoke. New Python modules,
tests, and entry points must target `local_agent_lab` unless an explicit,
documented compatibility boundary requires otherwise.

## Removed baseline package

The repository originally contained a second package at `src/minillm/`. That
package was a June 2026 baseline consisting of an argparse CLI, a separate model
configuration format, a raw Ollama HTTP client, and four task prompt helpers.
It was not a compatibility shim:

- no project entry point referred to it;
- no tracked source file or test imported it;
- no documented runtime command invoked `python -m minillm`;
- its implementation did not participate in Home MCP, governed memory,
  observability, deployment controls, or the current evaluation path; and
- its tracked history ended at the initial baseline while the canonical package
  continued to evolve.

Keeping both packages installable made a fresh installation expose two plausible
implementations with different configuration and safety behavior. The legacy
Python files were therefore removed rather than retained as an undocumented
fallback. Historical behavior remains recoverable from Git history at commit
`24b1a919840976d54887f729883a347a91b06082`.

The old `config/models.json` and top-level `prompts/*_system.txt` files are not
part of the canonical runtime contract. They are retained separately from this
code-removal change so deleting a Python implementation cannot silently delete
potentially useful historical configuration content. Canonical runtime
configuration and prompts live under `config/agent*.yaml` and
`config/prompts/`.

## Enforced boundary

`tests/test_canonical_implementation.py` prevents the duplicate package from
returning, verifies that the `lagent` entry point resolves to the canonical
Typer application, and checks that CLI discovery works without Ollama, MCP, or
other external services.
