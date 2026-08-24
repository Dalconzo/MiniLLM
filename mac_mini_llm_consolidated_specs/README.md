# Mac Mini LLM: Authoritative Architecture Set

**Status:** Current design authority  
**Date:** 2026-07-27  
**Project:** Mac Mini LLM / Mnemonic OS / Home MCP

This directory consolidates the project's current architecture from the earlier design documents:

- `local_mac_mini_agent_codex_spec.md`
- `adaptive_memory_full_history_spec.md`
- `evolving_object_memory_architecture.md`
- `Adaptive Stigmergic Memory Prototype.pdf`
- `home_mcp_scope_for_codex.md`

The older documents remain useful design history. When they conflict with this set, this set takes precedence unless an Architecture Decision Record (ADR) explicitly says otherwise.

## Document precedence

1. `SECURITY_MODEL.md`
2. `AI_MEMORY_CONTRACT.md`
3. `SYSTEM_ARCHITECTURE.md`
4. `BUILD_FEEDBACK_LOOP.md`
5. `ROADMAP.md`
6. ADRs in `adr/`
7. Historical source specs

Security constraints override convenience, implementation plans, and agent autonomy.

## Core objective

Build a local-first, source-backed memory and tool substrate that makes downstream AI agents materially better at personalized reasoning.

The system should not optimize for remembering the most text. It should optimize for exposing the **smallest amount of decision-relevant, trustworthy context** that improves the downstream agent's answer or action.

The target is:

```text
raw evidence
    ↓
events
    ↓
persistent objects
    ↓
claims + evidence
    ↓
materialized current state
    ↓
world model
    ↓
retrieval
    ↓
agent context packet
```

Raw history remains available as evidence, but should rarely be the final representation shown to an agent.

## Architectural invariants

- Raw chat history is evidence, not canonical memory.
- Assistant inference must never silently become user fact.
- Every important canonical claim must remain traceable to evidence.
- Current state must not destroy historical observations.
- Contradictions must not be silently merged away.
- Cross-domain analogy must be labeled separately from direct evidence.
- Hard constraints must be able to outrank weak semantic similarity.
- Retrieval usefulness and factual confidence are separate signals.
- The system must be able to represent uncertainty and unresolved attribution.
- Memory quality is ultimately measured by downstream agent performance.
- The production system must not have authority to rewrite its own security boundary.

## Working model

The project has three distinct layers:

### 1. Memory/data plane

Stores and retrieves evidence, events, objects, claims, relationships, context packets, feedback, and traces.

### 2. Agent/build plane

Codex and local/frontier models inspect code, propose patches, run evals, and improve the implementation.

### 3. Security/control plane

Defines permissions, roots, deployment authority, credentials, confirmation policy, audit integrity, and builder isolation.

The first two may evolve quickly. The third should evolve deliberately and remain human-gated.

## Current phase

The project is now in **Agent-Usable Memory Substrate** development.

The immediate goal is not advanced multi-agent behavior or autonomous "dreaming." It is to make the existing memory system reliably useful to a downstream AI agent, then automate the feedback → regression test → implementation → evaluation loop.

See `ROADMAP.md`.
