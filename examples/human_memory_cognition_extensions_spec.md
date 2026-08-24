# Spec Addendum: Human-Memory Cognition Extensions for Local 7B Models

## Purpose

This addendum extends the Adaptive Memory System with human-memory-inspired mechanisms that are still practical on a Mac mini running local 7B-class models.

The goal is not to simulate the whole brain. The goal is to add the highest-value cognitive primitives that make memory more useful, reliable, and context-efficient:

- source monitoring
- inhibition
- prediction
- surprise
- prospective memory
- schemas
- counterfactuals
- affective salience
- skill models
- person models
- nightly consolidation
- retrieval evaluation

---

## 1. Feasibility

### Is this too much for a Mac mini + 7B models?

No, if implemented as:

- structured metadata
- deterministic code where possible
- cheap classifiers
- cached results
- scheduled background jobs
- optional high-effort retrieval
- review queues for uncertain outputs

The 7B model should not be asked to reason over the whole memory system at once. It should perform small jobs:

- classify domain
- extract candidate memory
- assign lenses
- summarize episode
- propose prediction
- propose schema
- compile context packet
- flag obvious stale/conflicting memory

The larger intelligence should come from memory structure, scoring, feedback, and consolidation.

### Where diminishing returns start

Diminishing returns begin if:

- every query runs every subsystem
- every result triggers cross-domain analogy
- every memory gets rewritten constantly
- skeptic/governance runs on trivial questions
- speculative model output is promoted without review
- the UI exposes too much before basics work

Core rule:

```text
cheap by default
deep only when effort or risk justifies it
background consolidation when idle
```

---

## 2. Priority Tiers

### Tier 1: Add Early

These improve reliability immediately.

```text
epistemic_status
confidence_basis
source_reliability
inhibition_tags
prospective_memory
basic person_models
basic skill_models
retrieval_evals
```

### Tier 2: Add After Retrieval Works

These improve intelligence and planning.

```text
prediction
surprise_score
schema_memory
counterfactual_memory
nightly_consolidation
```

### Tier 3: Experimental Later

These are powerful but noisy if added too early.

```text
external_inspiration_stream
automatic_cross_domain_analogy
background_memory_daemons
multi_agent_skeptic_curator_analogy_roles
self_modifying_schemas
automated_memory_pruning
```

---

## 3. New Fields for `memories`

Add these fields or equivalent extension tables.

### epistemic_status

How the system knows something.

Allowed values:

```text
observed
user_reported
assistant_suggested
assistant_inferred
system_inferred
externally_sourced
confirmed
contradicted
speculative
counterfactual
unknown
```

Example:

```text
Memory:
  Crème brûlée was a successful repeatable dessert.

epistemic_status:
  user_reported
```

Example:

```text
Memory:
  Pavlova roulade could be a good summer bake.

epistemic_status:
  assistant_suggested
```

### confidence_basis

Why confidence is high or low.

Allowed values:

```text
repeated_user_confirmation
single_user_statement
direct_user_outcome_report
assistant_suggestion_only
assistant_inference
old_import
recent_confirmation
contradicted_once
multiple_sources_agree
uncertain_context
```

### source_reliability

Numeric score from `0.0` to `1.0`.

Suggested defaults:

```text
direct user outcome report: 0.95
repeated user confirmation: 0.95
single user statement: 0.85
recent active memory: 0.85
old imported memory: 0.65
assistant inference: 0.45
assistant suggestion only: 0.30
speculative memory: 0.25
contradicted memory: 0.05
```

### inhibition_tags

Tags used to suppress irrelevant or unsafe retrieval.

Examples:

```text
stale_relationship_context
old_finance_data
assistant_suggestion_only
health_caution
financial_caution
current_partner_only
avoid_for_fast_retrieval
analogy_only
superseded_context
```

### prediction

A short expectation derived from memory.

Example:

```text
Future desserts using high-cacao chocolate may need additional sweetness, cream, salt, or fruit contrast.
```

### surprise_score

How much an outcome violated expectation.

```text
surprise_score: float 0.0 to 1.0
```

High-surprise events should get extra salience and be queued for schema updates.

### affective_salience

Proxy for emotional/motivational importance.

Store as JSON:

```json
{
  "stress": 0.2,
  "pride": 0.8,
  "delight": 0.7,
  "frustration": 0.1,
  "social_success": 0.9,
  "risk": 0.2,
  "urgency": 0.1
}
```

### prospective_trigger

Future-oriented trigger.

Example:

```text
When planning a date-night dessert with less than 4 hours available, avoid multi-component bakes requiring long chilling.
```

Fields:

```text
prospective_trigger
trigger_status: active | paused | completed | archived
```

### counterfactual

A useful “what should have happened” lesson.

Example:

```text
If the Black Forest cake layers had been chilled and trimmed longer, stacking would likely have been cleaner.
```

Counterfactuals must not be treated as factual history.

### schema_id

Optional link to a reusable schema.

Examples:

```text
ambitious_bake_schema
date_night_dinner_schema
lab_failure_debugging_schema
portfolio_thesis_schema
supplement_routine_schema
```

---

## 4. New Tables

### schemas

Schemas are reusable frames learned from repeated memories.

```text
id
name
domains_json
description
slots_json
evidence_memory_ids_json
confidence
created_at
updated_at
```

Example:

```json
{
  "name": "ambitious_bake_schema",
  "domains": ["cooking_baking"],
  "description": "A frame for evaluating whether a bake is likely to be impressive, stressful, repeatable, or worth attempting.",
  "slots": {
    "technique_novelty": "low|medium|high",
    "parallel_components": "integer",
    "chill_time_required": "boolean",
    "finishing_precision": "low|medium|high",
    "transport_risk": "low|medium|high",
    "social_payoff": "low|medium|high",
    "failure_recovery_options": "text"
  }
}
```

### predictions

```text
id
memory_id nullable
schema_id nullable
prediction_text
domains_json
confidence
status: active | confirmed | failed | expired
evidence_memory_ids_json
created_at
resolved_at nullable
```

### prospective_memories

```text
id
trigger_text
action_text
domains_json
lenses_json
priority
status: active | paused | completed | archived
source_memory_ids_json
created_at
last_triggered_at nullable
```

### person_models

Separate social memory by person.

```text
id
person_name
canonical_name
relationship_type
status: active | historical | uncertain
preferences_json
dislikes_json
constraints_json
communication_notes_json
source_memory_ids_json
valid_from
valid_until nullable
created_at
updated_at
```

Important: do not merge different people’s preferences.

### skill_models

Tracks user competence.

```text
id
skill_name
domains_json
level: novice | beginner | intermediate | advanced | expert | unknown
confidence
evidence_memory_ids_json
related_failures_json
next_progressions_json
created_at
updated_at
```

### eval_cases

Retrieval tests.

```text
id
query
expected_domains_json
forbidden_domains_json
must_include_memory_ids_json
must_exclude_memory_ids_json
expected_lenses_json
notes
created_at
updated_at
```

### eval_runs

```text
id
eval_case_id
retrieval_event_id
passed
score
failure_notes
created_at
```

---

## 5. New Processes

### 5.1 Source-Monitoring Pass

Classifies the epistemic status of candidate memories.

Input:

- candidate memory
- source chunk
- message author roles

Output:

```text
epistemic_status
confidence_basis
needs_review
source_reliability
```

Rules:

```text
If user directly reported an outcome:
  epistemic_status = user_reported
  confidence_basis = direct_user_outcome_report

If assistant suggested something and user did not confirm:
  epistemic_status = assistant_suggested
  confidence_basis = assistant_suggestion_only
  needs_review = true

If system inferred a general principle:
  epistemic_status = system_inferred
  confidence_basis = assistant_inference or multiple_sources_agree
```

### 5.2 Inhibition Pass

Suppresses irrelevant, stale, or unsafe retrieval before ranking.

Examples:

```text
If query domain = cooking_baking:
  inhibit finance_investing unless effort >= 4 and analogy requested

If query involves current partner:
  inhibit stale_relationship_context

If query is health-related:
  inhibit old_import memories unless recently confirmed or explicitly requested

If memory epistemic_status = assistant_suggested:
  inhibit from direct factual context unless framed as suggestion/inspiration
```

### 5.3 Prospective Trigger Check

Activates future-oriented memories when conditions match.

Run when:

- user asks a planning question
- new active memory is formed
- effort >= 3
- nightly consolidation runs

Example:

```text
Query:
  What dessert should I make tonight? I have three hours.

Trigger:
  Warn against multi-component bakes requiring long chilling when time is short.
```

### 5.4 Prediction Generation

Creates lightweight expectations from memories.

Only run for memories with:

- high salience
- repeated evidence
- user-reported outcomes
- failures/workarounds
- procedures
- schemas

Example:

```text
Memory:
  Bitter ganache came from using 78% chocolate under constraint.

Prediction:
  Future desserts using high-cacao chocolate may need additional sweetness, cream, salt, or fruit contrast.
```

### 5.5 Surprise Update

Detects when new outcomes violate expectations.

Process:

```text
1. New active memory is created.
2. Retrieve relevant predictions.
3. Compare outcome to prediction.
4. If mismatch, assign surprise_score.
5. Increase salience for high-surprise events.
6. Queue for consolidation/schema update.
```

### 5.6 Schema Update

Consolidates repeated memories into reusable frames.

Run:

- nightly
- manually
- after high-surprise memories
- after multiple related failures

Process:

```text
1. Cluster related memories by domain/lens/abstract pattern.
2. Check if a schema exists.
3. If no schema exists and cluster is strong, propose one.
4. If schema exists, update slots/evidence/confidence.
5. Do not auto-promote major schema changes without review in v0.
```

### 5.7 Counterfactual Lesson Generation

Extracts useful “what should have happened” lessons.

Run for:

- failures
- mixed outcomes
- stressful episodes
- time/constraint issues

Counterfactuals must be marked as counterfactual.

### 5.8 Skill Model Update

Tracks user competence.

Run after:

- completed episodes
- repeated successful tasks
- user-reported skill improvement
- failures that reveal skill gaps

Output:

```text
skill_name
evidence_memory_ids
level
confidence
next_progressions
```

### 5.9 Person Model Update

Maintains separated social memory.

Run when:

- user states a preference/dislike for another person
- relationship context changes
- old person context is contradicted
- planning query involves a person

Critical rule:

```text
Do not merge different people’s preferences.
Do not apply historical partner context to current partner context.
```

### 5.10 Nightly Consolidation

Sleep-like background memory maintenance.

Command:

```bash
python -m app.cli consolidate-nightly
```

Tasks:

```text
deduplicate memories
find contradictions
update stale flags
update schemas
update skills
update person models
process open loops
generate counterfactuals
update predictions
decay unused memories
create consolidation report
```

Do not rewrite canonical memories aggressively. Prefer proposing changes for review.

---

## 6. Retrieval Updates

Updated scoring formula:

```text
score =
  semantic_similarity
  + domain_match_bonus
  + lens_match_bonus
  + salience
  + confidence
  + source_reliability
  + recency_bonus
  + relation_strength_bonus
  + success_history_bonus
  + prediction_relevance_bonus
  + prospective_trigger_bonus
  + schema_match_bonus
  - energy_cost
  - staleness_penalty
  - contradiction_penalty
  - domain_drift_penalty
  - inhibition_penalty
```

### 6.1 Hard Filters

Before scoring, apply hard filters for:

```text
status = archived
status = superseded, unless historical query
epistemic_status = contradicted
forbidden domain
private/person mismatch
```

### 6.2 Soft Penalties

Apply penalties for:

```text
assistant_suggested
old_import
low_confidence
stale
analogy_only
health_caution
financial_caution
```

### 6.3 Effort-Level Activation

```text
Effort 1:
  no schema generation
  no counterfactuals
  no cross-domain analogy
  strong inhibition
  active/current memories preferred

Effort 2:
  include prospective triggers
  include basic predictions

Effort 3:
  include schemas
  include person/skill models
  include relevant failure/counterfactual memories

Effort 4:
  include contradiction checks
  include open loops
  include surprise/high-salience memories
  allow adjacent-domain bridges

Effort 5:
  include analogy search
  include skeptic/governance review
  include cross-domain schema comparison
  include exploratory synthesis
```

---

## 7. Context Compiler Updates

Context packets may include these sections when relevant:

```text
Predictions:
  What is likely to happen?

Prospective reminders:
  What should be remembered before acting?

Relevant schemas:
  Which reusable frame applies?

Skill model:
  What can the user currently do?

Person model:
  What preferences/constraints matter for involved people?

Counterfactual lessons:
  What should be done differently based on prior outcomes?

Inhibited context:
  Important memories intentionally excluded and why.
```

Do not include all sections every time. Include only what helps.

---

## 8. UI Additions

### Memory Detail Page

Show/edit:

- epistemic_status
- confidence_basis
- source_reliability
- prediction
- surprise_score
- affective_salience
- prospective_trigger
- counterfactual
- inhibition_tags
- schema link

### Schemas Page

Show:

- schema name
- description
- slots
- evidence memories
- confidence
- proposed updates

### Person Models Page

Show:

- people
- active/historical status
- preferences
- dislikes
- constraints
- source memories
- temporal validity

### Skill Models Page

Show:

- skill
- level
- evidence memories
- related failures
- next progression

### Prospective Memory Page

Show:

- trigger
- action
- priority
- status
- last triggered

### Eval Harness Page

Allow user to create and run retrieval evals.

Fields:

- query
- expected domains
- forbidden domains
- must include memories
- must exclude memories
- effort level
- pass/fail result

---

## 9. Evaluation Requirements

The system must prove the new features help rather than add noise.

### Required Eval Cases

#### Current Partner Query

```text
What should I cook for my girlfriend?
```

Must:

- retrieve current partner/person model
- include relevant food dislikes/preferences
- suppress stale relationship context

Must not:

- treat old partner context as current

#### Fast Dinner Query

```text
What should I make for dinner tonight?
```

Must:

- stay in cooking_baking
- include current preferences/constraints
- not retrieve finance/health/AI theory

#### Cross-Domain Analogy Query

```text
What cooking lessons transfer to lab automation?
```

Must:

- include cooking and lab domains
- label transfer as analogy
- avoid literal transfer errors

#### Health Query

```text
Should I change my supplement routine?
```

Must:

- flag health caution
- mark old supplement memories as potentially stale
- avoid overconfident recommendations

#### Finance Query

```text
How should I adjust my portfolio?
```

Must:

- flag financial caution
- mark old positions/prices as stale
- distinguish thesis memory from current data

#### Ambitious Bake Query

```text
Should I attempt a complicated bake tonight?
```

Must:

- retrieve ambitious_bake_schema if available
- include time/stress/skill constraints
- include prospective warning if time is short
- include relevant failures/counterfactuals

### Metrics

Track:

```text
domain_precision
forbidden_memory_count
source_traceability_rate
stale_memory_warning_rate
context_packet_user_rating
retrieval_latency
token_count_estimate
```

---

## 10. Performance Strategy for 7B Models

### Use Small Models for Narrow Jobs

Local 7B models should handle:

- domain classification
- lens assignment
- simple extraction
- summarization
- source-status classification
- context packet drafting
- schema proposal drafts

Avoid asking 7B models to do:

- huge full-history reasoning in one prompt
- massive deduplication in one pass
- complex multi-domain synthesis without retrieved context
- high-stakes medical/financial conclusions

### Batch and Cache Everything

Cache:

- embeddings
- domain classifications
- extracted candidates
- compiled summaries
- schema proposals
- source-monitoring results

Do not reprocess unchanged chunks.

### Prefer Deterministic Code Where Possible

Use code for:

- timestamp handling
- domain hard filters
- effort-level gating
- energy-cost updates
- source linking
- supersession status updates once identified
- eval scoring
- relation-weight updates

Use LLMs for:

- semantic extraction
- abstraction
- analogy proposal
- counterfactual proposal
- context writing

### Review Queues

Because 7B outputs will be imperfect:

- candidate memories require review
- schema proposals require review
- person model changes require review if confidence is low
- health/finance memories default to caution
- supersession proposals should be inspected

### Latency Budget

```text
Effort 1:
  under 1 second if cached

Effort 2:
  1-3 seconds

Effort 3:
  3-8 seconds

Effort 4:
  8-20 seconds

Effort 5:
  longer is acceptable for deep mode
```

Background consolidation can run slowly.

---

## 11. Implementation Order

### Milestone A: Source Monitoring

Add:

- epistemic_status
- confidence_basis
- source_reliability

Implement source-monitoring pass.

### Milestone B: Inhibition

Add:

- inhibition_tags
- inhibition pass during retrieval
- stale/current partner suppression rules

### Milestone C: Prospective Memory

Add:

- prospective_memories table
- trigger matching
- context packet section

### Milestone D: Eval Harness

Add:

- eval_cases
- eval_runs
- UI
- basic metrics

### Milestone E: Person and Skill Models

Add:

- person_models table
- skill_models table
- update passes
- UI pages

### Milestone F: Prediction + Surprise

Add:

- prediction field/table
- surprise_score
- prediction comparison on new active memories

### Milestone G: Schema Memory

Add:

- schemas table
- schema proposal pass
- schema retrieval bonus

### Milestone H: Counterfactuals

Add:

- counterfactual field
- generation pass for failures/mixed outcomes
- context packet inclusion

### Milestone I: Nightly Consolidation

Add:

```bash
python -m app.cli consolidate-nightly
```

Tasks:

- dedupe
- update schemas
- update skills
- update person models
- process open loops
- generate report

---

## 12. Acceptance Criteria

This addendum is implemented enough when:

1. Memories distinguish source/epistemic status.
2. Assistant suggestions are not treated as confirmed facts.
3. Retrieval can suppress stale or irrelevant memories.
4. Current partner/person context does not mix with old partner context.
5. Prospective reminders can trigger during planning queries.
6. High-effort retrieval can include schemas/counterfactuals.
7. The system can track at least five user skills from cooking/baking history.
8. The system can maintain separate person models.
9. Nightly consolidation can propose schema updates without corrupting canonical memory.
10. Retrieval evals can catch domain drift and stale-memory mistakes.

---

## 13. Recommended First Build Scope

For the first pass, implement only:

```text
epistemic_status
confidence_basis
source_reliability
inhibition_tags
prospective_memory
basic person_models
basic skill_models
retrieval evals
```

Delay:

```text
affective_salience
surprise_score
schemas
counterfactuals
nightly consolidation
external inspiration
multi-agent skeptic
```

Reason:

The first group improves reliability immediately. The second group improves intelligence and creativity after the substrate works.

---

## 14. Summary

These human-memory extensions are not too heavy for a Mac mini if implemented as structured memory mechanics rather than constant LLM reasoning.

The system should be cheap by default and cognitively rich only when effort level, risk, or user intent justifies it.

The design goal remains:

```text
Build a local adaptive memory system that gives even small models better, denser, safer, and more relevant context than raw chat history or ordinary vector search.
```
