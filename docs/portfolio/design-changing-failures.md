# Design-Changing Failures

These case studies use retained failures, not post-hoc success claims. Each one
changed a contract or boundary rather than only tuning a score.

## 1. A nonsense query returned confident-looking memories

### Failure

The query `qzvorn 9472 xenolith checksum gasket` returned unrelated hydroponics,
memory-system, infrastructure, portfolio, and career results. The trace remained
healthy, which localized the defect to retrieval policy rather than transport or
observability.

Source evidence: private Browser ChatGPT acceptance run dated 2026-08-02. The
raw report is excluded under the
[repository visibility policy](../repository_visibility_policy.md); the failure
was converted into the regression below.

### Design change

Search gained an absolute relevance floor and a first-class
`no_reliable_results` result. Curated trust, recency, subject match, and vector
similarity can no longer create a reliable result without sufficient lexical or
semantic relevance.

### Validation and consequence

- Regression:
  [`test_search_chatgpt_memory_returns_no_reliable_results_for_weak_curated_subject_match`](../../tests/test_memory_search.py#L895)
- Deterministic public control:
  [`test_public_demo_abstains_and_reports_expected_failure`](../../tests/test_public_demo.py)

The product contract changed from "search returns up to N items" to "search may
abstain and explain why." This is safer for agent use and measurable in future
abstention-accuracy evaluations.

## 2. Cross-domain similarity overrode an explicit boundary

### Failure

A Recipes and Baking context request with `allow_cross_domain=false` ranked `AI
Automation Micro-Business` first and admitted another compute-cost chunk. The
trace reported that no candidates had been domain-filtered.

Source evidence: private Browser ChatGPT differential run dated 2026-08-02;
the raw report is excluded and the observed behavior is retained in the
regression below.

### Design change

Domain/subject scope became an eligibility constraint rather than a soft score.
Generic terms such as "project," "time," or "difficulty" cannot override an
explicit in-domain request. Cross-domain material belongs in a separately
labeled analogy path.

### Validation and consequence

- Regression:
  [`test_search_chatgpt_memory_blocks_wrong_domain_subject_leak_when_cross_domain_disabled`](../../tests/test_memory_search.py#L923)
- High-risk defense in depth:
  [`test_cross_domain_candidate_is_blocked_even_when_current_and_canonical`](../../tests/test_memory_high_risk_search.py#L171)

This changed routing from score-only retrieval to governed candidate eligibility.
The remaining planned world-model may add analogy traversal, but it must not
weaken the direct-evidence boundary.

## 3. A structurally valid recipe was operationally unusable

### Failure

The Miso-Butter Roast Bowl reported 32 ingredients and eight steps, but every
method entry was only a heading. Temperatures, durations, quantities, actions,
and doneness cues were absent. Counting sections had produced a false sense of
validity.

Source evidence: private Browser ChatGPT recipe-card run dated 2026-08-02;
the raw report is excluded and the failure is retained in the regressions below.

### Design change

Recipe validation now evaluates whether method entries are executable. A card
that fails remains available for diagnosis and repair but is labeled
`quarantined_non_actionable` and `do_not_execute_without_repair`; callers may
exclude invalid cards.

### Validation and consequence

- Regression:
  [`test_home_mcp_flags_heading_only_recipe_methods_as_non_actionable`](../../tests/test_home_mcp.py#L430)
- Positive control:
  [`test_home_mcp_recipe_validation_accepts_detailed_steps_and_optional_ingredients`](../../tests/test_home_mcp.py#L480)
- Quarantine search behavior:
  [`test_home_mcp_flags_heading_only_recipe_methods_as_non_actionable`](../../tests/test_home_mcp.py)

The acceptance criterion changed from structural presence to agent actionability.
This is an example of evaluation changing the product definition, not merely the
parser.

## 4. Complete embeddings concealed a stale corpus

### Failure

The active imported corpus ended around June 13, while the evaluated sourdough
observation occurred later. Embedding coverage was complete for imported chunks,
so a single "coverage 100%" health signal incorrectly suggested that retrieval
had all relevant evidence.

Source evidence: private Browser ChatGPT freshness diagnosis dated 2026-08-02;
the raw report is excluded and the distinction is retained in the regression below.

### Design change

Health reporting separates embedding completeness from source freshness. It can
report the latest source message, import lag, corpus freshness, missing vectors,
and stale vectors independently.

### Validation and consequence

- Regression:
  [`test_memory_status_reports_corpus_freshness_separately_from_embedding_health`](../../tests/test_memory_observability.py#L349)

The diagnosis contract changed: a retrieval miss may be an ingestion/freshness
failure even when the vector index is internally complete.

## 5. Epistemic labels were correct but context slots were wrong

### Failure

The context packet distinguished `confirmed`, `assistant_suggested`, and
`system_inference`, yet its only current-state item was a memory-system design
decision rather than starter state. Relevant outcomes and constraints were
missing despite a structurally valid packet.

Source evidence: private Browser ChatGPT context-packet run dated 2026-08-02;
the raw report is excluded and the failure is retained in the regressions below.

### Design change

Context compilation gained explicit slots and rules that prevent assistant
advice from entering factual slots. Current state, constraints, preferences,
outcomes, failures, contradictions, and uncertainty are compiled separately;
empty slots remain empty rather than being filled with unrelated evidence.

### Validation and consequence

- Slot classification:
  [`test_context_packet_classifies_slots_and_does_not_duplicate_uncertainty`](../../tests/test_memory_search.py#L159)
- Assistant contamination control:
  [`test_context_packet_keeps_assistant_advice_out_of_factual_slots`](../../tests/test_memory_search.py#L196)

The system now treats packet shape and task-specific semantic content as separate
quality dimensions. The committed reports still do not establish a final,
fully-passing context compiler.

## 6. A secret-shaped query bypassed output redaction

### Failure

A browser acceptance probe used a synthetic credential-shaped query. Retrieval
returned confident-looking memories and echoed the query in the tool response.
Default traces omitted query content, but response and retrieval-audit handling
had treated queries as search control data rather than as sensitive egress and
persistence surfaces.

Source evidence: sanitized private Browser ChatGPT release acceptance run dated
2026-08-24. The raw report remains excluded under the
[repository visibility policy](../repository_visibility_policy.md).

### Design change

Secret detection now runs before FTS, vector embedding, or model-provider work.
A secret-shaped query produces an explicit `no_reliable_results` abstention and
only a type-level redaction marker. Retrieval audit records apply the same final
storage redaction while retaining a one-way query digest for correlation.

### Validation and consequence

- Pre-retrieval abstention:
  [`test_search_abstains_before_retrieval_for_secret_shaped_query`](../../tests/test_memory_search.py)
- Retrieval-audit persistence boundary:
  [`test_record_retrieval_event_never_persists_raw_secret_query`](../../tests/test_memory_audit.py)
- MCP response and log boundary:
  [`test_home_mcp_sensitive_memory_query_abstains_without_echo_or_persistence`](../../tests/test_home_mcp.py)

The privacy contract changed from "redact retrieved content" to "treat every
user-controlled query as sensitive until proven otherwise." This prevents a
query value from reaching both retrieval providers and diagnostic storage.
