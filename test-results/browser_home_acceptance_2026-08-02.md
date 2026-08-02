# MiniLLM/Home Acceptance Report

Date: 2026-08-02
Source: Browser ChatGPT Home MCP acceptance and supplemental read-only tests

## Overall Result

Overall result: PARTIAL PASS. Corpus health, filesystem boundaries, recipe title ranking, provenance labeling, and trace instrumentation work. Retrieval relevance and context compilation are not yet reliable enough for an agent to use without substantial skepticism.

## Test Matrix

| # | Test | Result | Run ID(s) | Trace ID(s) | Notes |
| - | ---- | ------ | --------- | ----------- | ----- |
| 1 | Tool/status check | Pass | `20260802T181128Z-8ea97aa1`; `20260802T181131Z-d7e59c8c` | Same as run IDs | Healthy database and full production embedding coverage. Roots correctly separated by write permissions. |
| 2 | Recipe ranking | Pass | `20260802T181138Z-d564e9b8` | Same | Exact title phrase ranked first by a wide margin. No ranking feedback needed. |
| 3 | Recipe card | Fail | `20260802T181146Z-76abdbff`; `20260802T181155Z-0415d645` | Same | Counts are nonzero, but all eight steps are headings rather than executable instructions. |
| 4 | Memory health and subjects | Partial | `20260802T181208Z-ebd7561b` | Same | Subject listing works, but several conversation counts appear internally inconsistent. |
| 5 | Subject review quality | Partial | `20260802T181217Z-92876bdf`; `20260802T181222Z-8cdac8e0` | Same | `high_signal` removes assistant content effectively, but contextless and transient user fragments remain. |
| 6 | Memory search quality | Fail | `20260802T181235Z-bfce695c` | Same | Relevant sourdough results exist, but ranking includes unrelated dough, dessert, and memory-system records. |
| 7 | Context packet quality | Fail | `20260802T181255Z-95e1e203` | Same | Epistemic labeling is good, but the compiled packet does not contain useful starter state, outcomes, or lessons. |
| 8 | Trace audit | Pass | Audited `20260802T181255Z-95e1e203` | `20260802T181313Z-e34d0735` | All expected stages present. Failure localizes to ranking and packet compilation, not instrumentation. |
| 9 | No-result trace | Fail | `20260802T181320Z-00098380` | Search same; trace read `20260802T181327Z-38f0db2e` | Status and trace remained healthy, but a nonsense query returned five unrelated results instead of zero/low-confidence behavior. |
| 10 | Improvement feedback | Pass | Six feedback submissions | Same as submission run IDs | Append-only feedback submitted. No memory curation operations performed. |

## Status And Roots

Memory status:

- Production embedding model: `nomic-embed-text`
- Provider: local Ollama
- Dimension: 768
- Embedded chunks: 39,797
- Coverage: 100%
- Missing chunks: 0
- Stale chunks: 0
- Subjects: 13
- Candidate memories: 39,795
- Curated memory records: 8
- Conversations: 1,512
- Messages: 16,573

The fallback `deterministic-token-hash` embedding also reports complete coverage, but its metadata correctly identifies it as unsuitable for production semantic ranking.

Allowed roots:

- Writable: `recipe_book`, `household`, `projects`, `inbox`
- Read-only: `archive`

No writes were made to any root.

## Recipe Ranking Results

Run/trace: `20260802T181138Z-d564e9b8`

| Rank | Title | Score | Match reason | Matched terms |
| ---: | ----- | ----: | ------------ | ------------- |
| 1 | Miso-Butter Roast Bowl with Jammy Eggs | 19.5 | `title_phrase` | `miso`, `butter` |
| 2 | Honey-Miso Roasted Butternut Squash...Brown-Butter Hazelnut Crumb | 7.75 | `title_terms` | `miso`, `butter` |
| 3 | Thousand-Dollar Peach Crumble Cobbler | 2.75 | `content` | `miso`, `butter` |
| 4 | Baby Red Butter Lettuce Salad with Radish and Tarragon | 1.0 | `content` | `butter` |
| 5 | Bedside Banana-Almond Breakfast Bars | 1.0 | `content` | `butter` |

This behaves as expected. The exact phrase/title result dominates weaker co-occurrence results, including peach cobbler.

## Recipe Card Quality

Recipe: `recipe_book:miso-butter-roast-bowl-with-jammy-eggs.md`

- Ingredients count: 32
- Steps count: 8
- Structure confidence: 0.9
- Parsed schema version: 1

Despite the nonzero counts, the card is not actionable. The method contains only labels:

- "Start the rice"
- "Heat the oven and prep the first roast"
- "Cook the jammy eggs"
- "Make the miso-butter sauce"

There are no temperatures, cooking durations, doneness cues, or actual actions. Metadata claims that ingredient quantities are repeated inside each step, but they are not.

Assessment:

- Actionable: No
- Missing steps: Yes, functionally
- Poorly standardized: Yes
- Over-expanded: ingredient-side only, due to numerous optional toppings and components
- Under-expanded: severely on the method side

## Subject Distribution

| Subject | Chunks | Conversations | Assessment |
| ------- | -----: | ------------: | ---------- |
| Lab Automation | 9,536 | 158 | Plausible |
| Career and Job Search | 9,134 | 44 | Dense but plausible |
| Relationships and Life | 3,570 | 1 | Highly suspicious |
| Recipes and Baking | 3,533 | 57 | Plausible |
| AI Memory and Local LLMs | 2,710 | 10 | Dense |
| Finance and Investing | 1,534 | 9 | Dense |
| Health and Supplements | 1,468 | 7 | Dense |
| Pets and Animal Care | 884 | 2 | Very concentrated |
| Fitness and Training | 800 | 0 | Invalid-looking provenance |
| Law and LSAT | 609 | 0 | Invalid-looking provenance |
| Creative Writing | 484 | 0 | Invalid-looking provenance |
| Home Projects and Devices | 382 | 0 | Invalid-looking provenance |
| Style and Wardrobe | 380 | 4 | Plausible |

The four subjects with zero conversations but hundreds of chunks are the clearest accounting defect. "Relationships and Life" having 3,570 chunks from one conversation also warrants inspection, even if a single imported conversation was unusually large.

## Subject Review Comparison

High signal run/trace: `20260802T181217Z-92876bdf`

- Pending candidates in subject: 283
- Assistant-authored candidates: 0
- Returned: 10

All candidates run/trace: `20260802T181222Z-8cdac8e0`

- Pending candidates in subject: 4,016
- Assistant-authored candidates: 3,131
- Returned: 10

The filter reduces the candidate pool by about 93% and completely removes assistant-authored candidates. That part works well.

However, the top `high_signal` candidates still include:

- One-off scheduling requests
- Questions rather than durable facts
- Image-dependent fragments such as asking what kind of olive oil something "looks like"
- Context-dependent follow-ups about cake appearance
- Transient procedural choices

Therefore, `high_signal` is a strong source-role filter, but not yet a dependable durability or standalone-context filter.

## Memory Search Evaluation

Query: `starter rise fermentation timing after feeding`
Run/trace: `20260802T181235Z-bfce695c`

Candidate flow:

- FTS: 10
- Vector: 10
- Curated: 2
- Merged: 19
- Before domain governance: 21
- Domain-filtered: 0
- Governance-filtered: 0
- Ranked: 21
- Returned: 10

Quality:

- Ranks 2 and 3 are directly useful sourdough results: bulk fermentation timing of 3.5-5 hours; weak starter and underproofing troubleshooting.
- Rank 1 concerns generic dough fermentation rather than starter feeding or starter rise.
- Rank 4 is a curated decision about using cooking as a memory-system testbed. It has zero keyword and semantic relevance but is elevated through curated trust and subject match.
- Ranks 5-7 concern khachapuri dough timing rather than sourdough starter state.
- Ranks 8-10 concern choux, browning, and creme brulee.
- Domain detection classified the query as `misc`, despite explicit starter and fermentation terminology.
- Almost all returned evidence is assistant-authored rather than user-reported starter observations.

Provenance and snippets are technically usable at medium depth. The problem is selection, not disclosure.

## Context Packet Evaluation

Query: `what should an agent know before helping me with sourdough starter tracking and baking?`
Run/trace: `20260802T181255Z-95e1e203`

What works:

- Retrieved evidence is separated from system inference.
- `assistant_suggested`, `user_reported`, and `confirmed` are explicitly distinguished.
- Source IDs and score breakdowns are included.
- Requested depth and packet depth both report `medium`.
- Domain detection correctly resolves `cooking_baking` in this call.

What fails:

- The packet current state contains an unrelated curated decision about cross-domain memory testing.
- The packet current state contains a generic user question about learning fundamentals before attempting croquembouche.
- It does not surface useful starter information such as starter origin/storage, feed ratios, observed rise/fall, time since feeding, temperature, peak behavior, successful or failed bakes, tracking preferences, or current uncertainties.
- `critical_constraints`, `relevant_preferences`, `relevant_outcomes`, `failures_and_lessons`, and `contradictions_and_qualifications` are empty.
- The uncertainty section duplicates each assistant-authored item in two slightly different representations.

Structurally, this is a medium-depth packet. Semantically, it fails its task.

## Trace Audit

Audited run: `20260802T181255Z-95e1e203`
Trace read ID: `20260802T181313Z-e34d0735`

All expected stages are present:

- `receive_request`
- `call_tool`
- `subject_resolution`
- `domain_detection`
- `retrieval_sources`
- `apply_filters`
- `rank_results`
- `apply_disclosure`
- `record_retrieval_event`
- `compile_context_packet`
- `render_response`

Counts:

- Curated: 2
- FTS: 8
- Vector: 8
- Merged: 14
- Before governance: 16
- Domain-filtered: 0
- Governance-filtered: 0
- Scoped: 16
- Ranked: 16
- Returned: 8

Top score components:

| Rank | Score | Keyword contribution | Semantic contribution | Recency contribution |
| ---: | ----: | -------------------: | --------------------: | -------------------: |
| 1 | 0.429756 | 0.233465 | 0.153609 | 0.042682 |
| 2 | 0.373530 | 0.350000 | 0 | 0.023530 |
| 3 | 0.367230 | 0.187150 | 0.156550 | 0.023530 |
| 4 | 0.364776 | 0.321271 | 0 | 0.043505 |
| 5 | 0.342326 | 0.299430 | 0 | 0.042896 |

Disclosure tiers:

- Medium: 8
- Other tiers: 0

Failure localization:

- Primary: `rank_results`
- Secondary: `compile_context_packet`
- Not implicated: subject resolution, disclosure enforcement, audit recording, response rendering

## No-Result Behavior

Query: `qzvorn 9472 xenolith checksum gasket`
Search run: `20260802T181320Z-00098380`
Trace read: `20260802T181327Z-38f0db2e`

The tool returned `status: ok`, and the trace remained inspectable. That mechanical behavior passes.

However, it returned five unrelated results:

- Hydro plant options
- Cross-domain memory testbed
- Mac Mini infrastructure
- Portfolio synchronization
- Resume optimization

Candidate flow:

- FTS: 2
- Vector: 5
- Curated: 2
- Merged: 7
- Before governance: 9
- Domain-filtered: 0
- Ranked: 9
- Returned: 5
- Disclosure: 5 at `far`

The top FTS result received keyword relevance `1.0`, which strongly suggests broken token matching or normalization for the nonsense query. Cross-domain lab, finance, and career material also surfaced despite `allow_cross_domain: false`.

This needs an absolute relevance threshold and a proper zero-result path.

## Feedback Submitted

The plugin's feedback enum did not expose requested labels such as `ranking_issue`, `wrong_subject`, `trace_gap`, or `unclear_output`. The closest accepted categories were used.

| Feedback ID | Component/category | Source run / trace | Submission run / trace |
| ----------- | ------------------ | ------------------ | ---------------------- |
| `afbk_da21c07c678335af` | `memory_review_subjects` / `retrieval_noise` | `20260802T181217Z-92876bdf` | `20260802T181233Z-de01f4c5` |
| `afbk_42bf9e27e6405d5b` | `memory_search` / `retrieval_noise` | `20260802T181235Z-bfce695c` | `20260802T181250Z-70e31859` |
| `afbk_d53cd9fe05777210` | `memory_context` / `bad_context_compilation` | `20260802T181255Z-95e1e203` | `20260802T181308Z-a8723f6f` |
| `afbk_2280b02cdab26b26` | `memory_search` / `retrieval_noise` | Run `20260802T181320Z-00098380`; trace `20260802T181327Z-38f0db2e` | `20260802T181346Z-593e70fb` |
| `afbk_3041a7511a126e08` | `search_recipes` / `bad_canonicalization` | `20260802T181146Z-76abdbff` | `20260802T181354Z-244e9ea3` |
| `afbk_72b078a38705e755` | `memory_search` / `bad_canonicalization` | `20260802T181208Z-ebd7561b` | `20260802T181402Z-8f4607a9` |

Mutation statement: Browser ChatGPT did not mutate memory truth. It did not approve, reject, promote, delete, tombstone, merge, rewrite, or otherwise curate any candidate or curated memory. It did not create or modify notes or recipes. The only writes were append-only `submit_agent_feedback` records, and each tool response explicitly reported `mutates_memory_truth: false`.

## Supplemental Tests

Several additional read-only differential tests were run. The strongest new finding is that the sourdough evaluation was partly testing missing ingestion, not just retrieval quality: the latest full ChatGPT export in the database ends around June 13, 2026, so late-July and August starter observations were not available to retrieve.

| Test | Result | What it establishes |
| ---- | ------ | ------------------- |
| Corpus freshness | Fail | Recent conversations are not in the corpus |
| Exact phrase from imported data | Pass | Core lexical/vector retrieval works when the source exists |
| Exact title constraint | Pass | Title filtering is a genuine hard filter |
| Explicit source exclusion | Pass | `exclude_source_ids` works correctly |
| Feedback-to-ranking loop | Inactive | Submitted feedback currently does not alter ranking |
| Far versus full context depth | Partial/fail | Disclosure expands, but retrieval does not become more task-aware |
| Cross-domain disabled | Fail | AI/lab content still leaks into baking packets |
| Depth trace consistency | Fail | Full request is internally reported as effective depth `close` |

### Corpus Freshness

Status run: `20260802T183708Z-308f7829`

The latest substantial import is:

- Source export dated approximately June 13
- Imported June 15
- 1,511 conversations
- 16,571 messages

There is a second tiny import with one conversation and two messages, but it does not bring the corpus up to date.

Search for the recent observation that the starter rose 75-100% after 24 hours and then fell about 25% did not find the actual statement. Run: `20260802T183558Z-8f1ea1c3`.

Recommended change: add incremental ingestion and report separate freshness dimensions:

```text
embedding_health: complete
corpus_freshness: stale
latest_source_message: 2026-06-13
import_lag_days: 50
```

Feedback: `afbk_c3d01f2fae02f7aa`

### Exact-Match Control

Exact user statement searched:

```text
what's a fancy bake I can do before I see my girlfriend tonight...
```

Run: `20260802T183615Z-9c161bd1`

The correct user chunk ranked first with keyword relevance 1.0, semantic similarity 1.0, correct role `user`, epistemic status `user_reported`, and title `Fancy Bake for Tonight`.

This shows the underlying FTS, embedding lookup, provenance, and exact-match ranking pipeline are not fundamentally broken.

### Title Filter

Starter query constrained to title `Top Tier Sourdough Recipe`.

Run: `20260802T183621Z-a4ac152f`

All ten results came from that title, and the irrelevant curated records disappeared. Top results became genuinely useful: bulk fermentation timing, weak starter troubleshooting, levain maturity signs, and levain rise/timing.

Recommended change:

```python
max_chunks_per_message = 2
max_chunks_per_conversation = 4
```

Permit higher limits only when the caller explicitly scopes by title and requests document-style retrieval.

### Source Exclusion

Run: `20260802T183634Z-add022e7`

Known bad sources were excluded correctly. This provides an immediate mechanism for operationalizing retrieval feedback: feedback can populate a query-pattern/source penalty or exclusion table. The filtering capability already exists.

### Feedback Efficacy

Original run: `20260802T181235Z-bfce695c`
Repeated run: `20260802T183626Z-9c4aa888`

Ordering and scores were effectively identical. The feedback component remained 0.0 for every result.

Recommended change: create a feedback materialization stage:

```text
submitted -> reviewed/validated -> applied -> superseded
```

Potential effects:

- Penalize a source globally
- Penalize a source for a query pattern
- Mark a result as wrong-subject
- Add a relevance threshold exception
- Add training/evaluation examples without changing live ranking

Feedback: `afbk_5490cd70851e7fc7`

### Cross-Domain Enforcement

Context packet query: `baking project preferences, available time, and desired difficulty`

Parameters:

```text
subject = Recipes and Baking
allow_cross_domain = false
```

Far run: `20260802T183642Z-48fd7414`
Full run: `20260802T183648Z-d5d2d076`

The rank-one result was `AI Automation Micro-Business`, primary domain `lab_automation`. An `AI Bubble and Compute Costs` chunk also entered the packet. Trace reported `domain_filtered = 0`.

Recommended eligibility rule when `allow_cross_domain=false`:

```python
candidate.primary_domain == query.primary_domain
candidate.subject_id == requested_subject
strong_explicit_domain_match(candidate, query)
```

Generic semantic similarity should not override the boundary.

Feedback: `afbk_bc6073ec2948e966`

### Depth Behavior

Far and full context calls retrieved the same eight sources in the same order. Full depth exposed larger snippets and more packet categories, but did not perform broader or more targeted retrieval.

The full trace reported:

```text
requested_depth = full
effective_depth = close
disclosure tiers = {"full": 8}
packet_depth = full
```

Recommended API split:

```python
retrieval_depth = "close" | "broad"
packet_detail = "summary" | "standard" | "complete"
disclosure_tier = "far" | "medium" | "close" | "full"
```

Feedback: `afbk_b4f4bebf759f0ce2`

## Diagnosis

For the sourdough task specifically:

- 55% data freshness/setup
- 45% retrieval and context implementation

For the system overall:

- 35-45% early setup, sparse curation, and stale ingestion
- 55-65% implementation issues

The solid core:

- Exact phrase retrieval works
- Provenance works
- Epistemic role labeling works
- Title filtering works
- Source exclusion works
- Embeddings are complete
- Tracing is strong

The broken layer is mostly retrieval governance and packet synthesis, not storage or basic search.

## Recommended Change Order

1. Implement incremental ingestion and corpus-freshness reporting.
2. Add an absolute relevance/no-result threshold.
3. Make subject and `allow_cross_domain=false` hard constraints.
4. Require lexical or semantic relevance before adding curated-trust bonuses.
5. Materialize feedback into ranking penalties or query-specific exclusions.
6. Add result diversity by message and conversation.
7. Separate retrieval depth, packet detail, and disclosure tier.
8. Make context compilation retrieve slots independently: current state, preferences, outcomes, failures, constraints, and uncertainty.
9. Re-run the acceptance suite before substantial manual curation.

## Next Quantitative Testing

- A 20-30 query golden set with expected source IDs and forbidden sources.
- Recall@5, precision@5, reciprocal rank, and wrong-domain rate.
- The same suite before and after curating 50 high-value memories, to measure cold-start penalty.
- Incremental-import idempotence: import new data, repeat import, verify no duplication.
- Supersession tests where a newer user fact contradicts an older one.
- Disclosure red-team tests for sensitive content at every tier.
- A disposable end-to-end curation test only after explicit authorization for specific test candidates.

