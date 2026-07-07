# Adaptive Memory System Spec: Full Chat History Seeding + Active Memory

## Project Name

**Mnemonic OS**  
Working prototype domain name: **TasteMemory / LifeMemory**

The system begins as a local-first personal memory engine built on a Mac mini using the user's exported ChatGPT history. It should eventually generalize across many life and work domains: cooking/baking, lab automation, career, AI systems, finance, health, relationships, law/LSAT, style, projects, and creative ideas.

---

## 1. Core Thesis

Modern LLM systems are limited by poor memory and poor context selection, not only by model capability. The goal of this project is to build a **structured, adaptive, source-backed memory substrate** that increases the information density of model reasoning.

Instead of dumping raw chat history into context, the system should convert past conversations into:

- structured memories
- episodes
- facts
- preferences
- skills
- failures
- decisions
- procedures
- constraints
- open questions
- domain-specific lessons
- cross-domain abstractions

The system should retrieve and compile the smallest useful context packet for a given task, with adjustable effort/exertion levels.

---

## 2. Strategic Framing

This project is not a generic chatbot wrapper and not merely vector search over chat history.

It is an attempt to build:

> A local-first adaptive memory/context engine that ingests raw personal history, distills it into structured and relational memory, retrieves it through semantic lenses, and continuously improves through use.

The key performance target is **context-density optimization**:

```text
Best answer = model capability × relevance density × source correctness × retrieval precision × memory quality
```

A smaller model with excellent memory/context may outperform a larger model with poor context selection for many personal and operational tasks.

---

## 3. Main Design Principles

### 3.1 Raw History Is Not Memory

The full ChatGPT export is a raw archive. It must not be treated as trusted memory directly.

The system should maintain three layers:

```text
Raw Archive
  Full conversation and message history.

Search Index
  Chunked and embedded conversation segments.

Structured Memory
  Canonical extracted memories with type, domain, confidence, temporal validity, source links, and relations.
```

The structured memory layer is what makes the system intelligent.

### 3.2 Every Memory Needs a Source

Every canonical memory must trace back to one or more raw conversations/messages/chunks.

Source traceability is required for:

- debugging false memories
- reviewing stale information
- verifying important claims
- reconstructing context
- preventing hallucinated memory

### 3.3 Memory Must Be Domain-Scoped

Because the full chat history spans many topics, the system must not rely on emergent behavior alone to avoid category drift.

The system should enforce explicit domain scoping, then allow deliberate cross-domain retrieval through lenses.

Correct behavior:

```text
Default:
  Query retrieves from relevant domain(s).

Higher effort / analogy mode:
  Query may cross domains if useful.

Governance:
  Cross-domain memories must be labeled as analogies, not direct facts.
```

### 3.4 Temporal Validity Is Mandatory

Old memories can become wrong.

Examples:

- old relationship context may be superseded
- old job plans may be stale
- health/supplement routines may change
- portfolio positions may change
- cooking preferences may evolve

Every durable memory should include:

```text
valid_from
valid_until
last_confirmed_at
status: active | stale | superseded | contradicted | archived
superseded_by
```

### 3.5 Assistant Suggestions Are Not User Facts

The user’s chat history includes both:

- things the user actually said, did, preferred, or reported
- assistant suggestions, plans, guesses, and advice

The system must distinguish them.

A suggestion from the assistant should not become a trusted memory unless the user later confirms it, uses it, rates it, or reports an outcome.

### 3.6 Memory Should Be Adaptive, Not Static

Memory paths should become cheaper when they produce verified value and more expensive when they mislead.

Useful retrieval should reinforce:

- memories
- relations
- lenses
- retrieval paths
- domain bridges
- open-loop associations

Bad retrieval should penalize them.

### 3.7 Emergence Is Allowed Inside Guardrails

The system should support emergent cross-domain behavior, but only after basic controls exist:

- domain labels
- memory types
- source links
- confidence scores
- temporal validity
- retrieval effort levels
- analogy labels
- skeptic/governance review at high effort

---

## 4. Development Stage Target

The project should begin at **Stage 1.5**:

```text
Stage 0:
  Manual memory objects only.

Stage 1:
  Local app with structured memory and effort-based retrieval.

Stage 1.5:
  Full ChatGPT export ingestion pipeline with candidate memory extraction and review.

Stage 2:
  Active memory system with reinforcement, decay, open loops, and context packets.

Stage 3:
  Multi-agent roles: curator, skeptic, analogy hunter, governance, planner.

Stage 4:
  Cross-domain transfer and background exploration.

Stage 5:
  Domain-specific operational memory product, likely lab automation.
```

This spec targets **Stage 1.5 → Stage 2**.

Do not build full agent swarms yet. Build the memory substrate, ingestion pipeline, retrieval system, context compiler, and basic reinforcement loop.

---

## 5. Initial User Domains

The system should support these initial domains:

```text
cooking_baking
lab_automation
career_work
ai_memory_systems
finance_investing
health_supplements
relationships_life
law_lsat
style_wardrobe
creative_writing
home_projects
fitness_training
pets
misc
```

Domains should not be mutually exclusive. A memory may belong to multiple domains.

Example:

```text
"Using baking analogy to understand lab automation tolerance stack-up"
domains:
  - cooking_baking
  - lab_automation
  - ai_memory_systems
```

---

## 6. Memory Types

Initial memory types:

```text
episodic
semantic_fact
procedure
preference
constraint
workaround
failure
decision
skill
project
open_loop
hypothesis
analogy
risk
relationship
health_note
financial_note
source_note
```

Descriptions:

### episodic
Something that happened.

### semantic_fact
A stable fact believed to be true.

### procedure
A repeatable method or sequence.

### preference
A durable preference.

### constraint
A limitation or condition that shaped an action.

### workaround
Something done because the ideal path was blocked.

### failure
Something that went wrong and why.

### decision
A choice made, ideally with rationale and timestamp.

### skill
A capability the user has developed.

### project
An ongoing or completed project.

### open_loop
A persistent unresolved question or background thought.

### hypothesis
A tentative belief to be tested.

### analogy
A cross-domain structural similarity.

### risk
A known failure mode or caution.

### relationship
Context about interpersonal dynamics or people.

### health_note
Health-related memory. Must be treated cautiously.

### financial_note
Finance/investing-related memory. Must be treated cautiously and timestamped.

### source_note
Metadata or source-related information.

---

## 7. Reason Types

Every action-like memory should classify why the action happened.

Allowed reason types:

```text
ideal_procedure
workaround
constraint_response
preference_choice
experiment
accident
error_recovery
assistant_suggestion
user_reported_outcome
unknown
```

This is critical.

The system must not confuse:

- "we did this because it is best practice"
- "we did this because we lacked the right equipment"
- "assistant suggested this but user never did it"
- "user tried this and it failed"
- "user likes this"
- "this was a one-off experiment"

---

## 8. Memory Lenses

A lens is a way of viewing and retrieving memory.

Initial lenses:

```text
procedural
workaround
failure_mode
constraint
preference
skill_progression
creativity
analogy
risk
temporal
source_authority
contradiction
planning
identity_pattern
operational
health_caution
financial_caution
relationship_context
```

Lenses can be applied programmatically or semantically.

Example:

```text
Query:
  "What went wrong with ambitious bakes?"

Active lenses:
  failure_mode
  constraint
  skill_progression
  planning

Possible retrieved memories:
  - Black Forest cake ganache was bitter due to 78% chocolate constraint.
  - Croquembouche attempt had complexity/timing issues.
  - Khachapuri crust was overdone.
  - Crème brûlée succeeded and became repeatable.
```

---

## 9. Effort / Exertion Levels

Retrieval should use adjustable effort levels from 1 to 5.

### Effort 1: Reflex

- fastest
- high-confidence memories only
- recent and directly relevant
- no graph traversal
- no cross-domain analogies

### Effort 2: Basic Recall

- direct semantic search
- key preferences
- known constraints
- recent episodes

### Effort 3: Structured Recall

- one-hop graph traversal
- related failures/workarounds
- active domain memories
- skill progression
- moderate context packet

### Effort 4: Deep Recall

- two-hop graph traversal
- contradiction check
- temporal check
- cross-lens retrieval
- open-loop check
- more synthesis

### Effort 5: Exploratory / Creative / Governance

- analogy search
- cross-domain retrieval
- skeptic review
- memory conflict review
- open-loop activation
- creative synthesis
- optional multi-agent evaluation later

Effort should control:

- number of memories retrieved
- graph traversal depth
- model size
- number of LLM calls
- number of lenses
- contradiction checking
- temporal checking
- source verification strictness
- willingness to cross domains
- amount of synthesis

---

## 10. Domain Drift Control

### Problem

Full chat history covers unrelated domains. If retrieval is purely semantic, the system may drift:

- health advice leaking into cooking
- old relationship context affecting current relationship planning
- finance uncertainty treated like stable fact
- assistant-suggested ideas treated as user decisions
- analogies treated as literal facts

### Required Solution

Use layered scoping:

```text
1. Detect query domain.
2. Retrieve primarily from matching domain(s).
3. Expand to adjacent domains only if effort >= 3.
4. Allow cross-domain analogy only if effort >= 4 or user explicitly requests creativity/analogy.
5. Label cross-domain items as analogy/transfer, not direct evidence.
6. Run skeptic/governance check when high-risk domains are involved.
```

### Domain Expansion Rules

Examples:

```text
cooking_baking may expand to:
  fitness_training, health_supplements, relationships_life, lab_automation, ai_memory_systems

lab_automation may expand to:
  career_work, ai_memory_systems, cooking_baking, home_projects

finance_investing may expand to:
  career_work, ai_memory_systems
  but must keep current positions/time-sensitive claims low-confidence unless recently confirmed

health_supplements may expand to:
  fitness_training, cooking_baking
  but must use health_caution lens and avoid turning old notes into recommendations

relationships_life may expand to:
  cooking_baking, career_work, style_wardrobe
  but must respect temporal validity and superseded context
```

### Hard Rule

Emergent filtering is allowed only after explicit domain scoping. Do not assume emergence alone will prevent bad retrieval.

---

## 11. Active Memory vs Seeded Memory

### Seeded Memory

Memory extracted from imported ChatGPT history.

Characteristics:

- source-backed
- may be stale
- may include assistant suggestions
- needs candidate review
- useful for bootstrapping

### Active Memory

Memory formed during new usage of the system.

Characteristics:

- created after system launch
- should include current timestamp
- should have stronger recency weighting
- can be reinforced or penalized by user rating
- may update or supersede seeded memory

### Required Distinction

Every memory must include:

```text
origin:
  seeded_import | active_session | manual_entry | external_source | system_generated
```

This allows the system to prefer active memories when user context has changed.

---

## 12. Data Model

Use SQLite for v0. Design tables so migration to Postgres/pgvector/Neo4j later is possible.

### raw_conversations

```text
id
source_export_id
title
create_time
update_time
mapping_json
raw_json_path
created_at
```

### raw_messages

```text
id
conversation_id
message_id
parent_message_id
author_role: user | assistant | system | tool | unknown
content_text
content_json
create_time
status
created_at
```

### raw_chunks

```text
id
conversation_id
chunk_text
chunk_type: turn_pair | mini_thread | full_conversation_summary
message_ids_json
domain_candidates_json
timestamp_start
timestamp_end
embedding_json nullable
created_at
```

### episodes

```text
id
title
domain_primary
domains_json
date
raw_notes
summary
outcome_score nullable
source_chunk_ids_json
origin
created_at
updated_at
```

### candidate_memories

```text
id
content
memory_type
reason_type
domains_json
lenses_json
entities_json
relations_json
abstract_pattern
outcome
confidence
salience
temporal_validity_json
source_chunk_ids_json
source_message_ids_json
origin
review_status: pending | approved | rejected | merged
review_notes
created_at
updated_at
```

### memories

```text
id
canonical_content
memory_type
reason_type
domains_json
lenses_json
abstract_pattern
outcome
confidence
salience
energy_cost
decay_rate
status: active | stale | superseded | contradicted | archived
valid_from
valid_until
last_confirmed_at
origin
created_at
updated_at
last_accessed_at
access_count
success_count
failure_count
```

### memory_sources

```text
id
memory_id
source_type: raw_message | raw_chunk | episode | manual
source_id
quote
confidence
created_at
```

### entities

```text
id
name
canonical_name
entity_type
domains_json
metadata_json
created_at
updated_at
```

Entity types:

```text
person
ingredient
dish
tool
technique
instrument
company
project
asset
supplement
medication
exercise
location
concept
risk
preference
skill
unknown
```

### memory_entities

```text
memory_id
entity_id
role
```

### memory_relations

```text
id
source_memory_id
target_memory_id
relation_type
strength
energy_cost
confidence
domains_json
lenses_json
success_count
failure_count
last_used_at
created_at
updated_at
```

Relation types:

```text
supports
contradicts
supersedes
caused_by
worked_for
failed_for
similar_to
contrasts_with
transfers_to
part_of
requires
balanced_by
derived_from
example_of
generalizes_to
specializes_from
```

### open_loops

```text
id
question
current_hypothesis
domains_json
trigger_patterns_json
priority
energy_budget
status: active | paused | resolved | archived
source_memory_ids_json
created_at
updated_at
last_triggered_at
```

### retrieval_events

```text
id
query
detected_domains_json
effort_level
lenses_used_json
memory_ids_returned_json
context_packet_id
user_rating nullable
notes
created_at
```

### context_packets

```text
id
query
effort_level
domains_json
lenses_json
compiled_context
memory_ids_used_json
source_ids_json
warnings
created_at
```

---

## 13. Import Pipeline

### 13.1 Input

ChatGPT data export zip.

Expected important file:

```text
conversations.json
```

The system should support either:

```text
/path/to/export.zip
```

or:

```text
/path/to/unzipped_export/
```

### 13.2 Pipeline Steps

```text
1. Load export.
2. Parse conversations.
3. Extract messages.
4. Clean message text.
5. Build chunks.
6. Classify chunk domains.
7. Embed chunks.
8. Extract candidate memories.
9. Deduplicate candidates.
10. Human review / approval.
11. Promote approved candidates to canonical memories.
12. Create entities.
13. Create memory relations.
14. Build initial open loops.
15. Generate seed summary report.
```

### 13.3 CLI Commands

Implement CLI commands:

```bash
python -m app.cli import-export ./export.zip
python -m app.cli build-chunks
python -m app.cli classify-domains --limit 500
python -m app.cli extract-candidates --domain cooking_baking --limit 100
python -m app.cli extract-candidates --all --limit 1000
python -m app.cli dedupe-candidates
python -m app.cli promote-approved
python -m app.cli seed-report
```

### 13.4 Do Not Process Everything at Once Initially

The first run should process in batches.

Recommended starting order:

```text
1. cooking_baking
2. ai_memory_systems
3. lab_automation
4. career_work
5. law_lsat
6. relationships_life
7. fitness_training
8. finance_investing
9. health_supplements
10. all remaining domains
```

Health and finance should be imported with caution and lower default confidence.

---

## 14. Domain Classification

Each chunk should be assigned:

```text
primary_domain
secondary_domains
confidence
reason
```

Use hybrid classification:

```text
keyword rules
+ embedding similarity
+ LLM classifier
```

### Domain Classifier Output Schema

```json
{
  "primary_domain": "cooking_baking",
  "secondary_domains": ["relationships_life"],
  "confidence": 0.91,
  "reason": "The chunk discusses making a dessert for the user's partner and includes baking steps."
}
```

If confidence is low, assign:

```text
primary_domain: misc
```

---

## 15. Candidate Memory Extraction

The extractor must be strict.

### General Extraction Rules

Only extract durable information likely useful later.

Do extract:

- things the user did
- outcomes the user reported
- preferences stated by the user
- constraints encountered
- failures and fixes
- decisions made
- ongoing projects
- skill progression
- stable identity/context facts
- hypotheses the user is developing
- open loops and unresolved questions

Do not directly trust:

- assistant suggestions
- speculative plans
- future possibilities
- old relationship context
- time-sensitive financial positions
- old supplement routines
- uncertain inferred facts

### Candidate Memory JSON Schema

```json
{
  "content": "string",
  "memory_type": "preference",
  "reason_type": "user_reported_outcome",
  "domains": ["cooking_baking"],
  "lenses": ["preference", "planning"],
  "abstract_pattern": "string or null",
  "outcome": "string or null",
  "confidence": 0.0,
  "salience": 0.0,
  "entities": [
    {
      "name": "string",
      "entity_type": "person",
      "role": "subject"
    }
  ],
  "temporal_validity": {
    "valid_from": "date or null",
    "valid_until": "date or null",
    "last_confirmed_at": "date or null",
    "status": "active"
  },
  "source_message_ids": ["string"],
  "source_chunk_ids": ["string"],
  "needs_review": true,
  "review_reason": "string or null"
}
```

### Important Extraction Instruction

The model must explicitly identify whether a memory came from:

```text
user statement
user reported outcome
assistant suggestion
assistant interpretation
manual/system generated synthesis
```

Assistant suggestions should default to low confidence and pending review.

---

## 16. Deduplication and Canonicalization

Many memories will repeat across chats.

Deduplication should use:

- semantic similarity
- entity overlap
- domain overlap
- memory type
- temporal validity
- source agreement

Example duplicates:

```text
"Crème brûlée was a hit."
"Crème brûlée is repeatable."
"Crème brûlée went well and can be repeated."
```

Canonical memory:

```text
Crème brûlée was a successful and repeatable dessert project for the user.
```

Supporting sources are attached.

### Merge Rules

When merging:

```text
confidence increases with corroboration
salience increases with repetition and importance
temporal validity should become wider only if supported
contradictions should not merge silently
old superseded facts should remain but be marked superseded
```

---

## 17. Temporal Supersession

The system should detect possible supersession.

Examples:

```text
old:
  User is with Emma and planning wedding.

new:
  User is now with Devyn and no current wedding plan.

result:
  old memory status = superseded
  old memory valid_until = date of update if known
  new memory status = active
  relation = supersedes
```

Supersession detection should run especially for:

- relationships
- jobs
- locations
- supplement routines
- medications
- portfolio positions
- current projects
- goals
- dietary constraints
- equipment owned
- preferences if contradicted

---

## 18. Retrieval System

### Inputs

```text
query
optional domain override
optional lenses
effort_level 1-5
max_memories
include_sources boolean
```

### Step-by-Step Retrieval

```text
1. Detect query domain(s).
2. Select default lenses.
3. Build initial candidate pool from matching domains.
4. Score direct memories.
5. Expand by graph traversal according to effort level.
6. Include open loops if effort >= 4.
7. Include cross-domain analogies if effort >= 4 or requested.
8. Run contradiction/temporal checks if effort >= 4.
9. Compile context packet.
10. Log retrieval event.
```

### Scoring Formula

```text
score =
  semantic_similarity
  + domain_match_bonus
  + lens_match_bonus
  + salience
  + confidence
  + recency_bonus
  + relation_strength_bonus
  + success_history_bonus
  - energy_cost
  - staleness_penalty
  - contradiction_penalty
  - domain_drift_penalty
```

### Domain Drift Penalty

Apply if memory domain does not match query domain.

Reduce penalty when:

- effort >= 4
- user asks for analogy/creativity
- relation type is transfers_to or similar_to
- lens includes analogy or creativity

---

## 19. Context Compiler

The context compiler turns retrieved memories into a compact packet.

### Context Packet Format

```text
Task:
  <query>

Detected domain/lenses:
  <domains and lenses>

Relevant current truth:
  - ...

Relevant prior episodes:
  - ...

Preferences and constraints:
  - ...

Failures/workarounds:
  - ...

Procedures/skills:
  - ...

Transferable patterns:
  - ...

Open loops:
  - ...

Risks/contradictions:
  - ...

Recommended next direction:
  - ...

Sources:
  - memory/source IDs
```

The compiler should prefer concise, dense context over exhaustiveness.

The context packet should be the artifact passed to a downstream assistant or agent.

---

## 20. Active Memory Formation

When the user uses the system after initial seeding, new interactions should produce active memories.

Active memories should be formed when:

- user confirms something
- user reports an outcome
- user corrects a memory
- user makes a decision
- user completes a task
- user rates an answer
- a repeated pattern is observed
- an open loop receives useful evidence

Active memories should have:

```text
origin = active_session
higher recency
source = active conversation/session
review_status = approved if directly user-stated
```

---

## 21. Reinforcement and Penalization

After a context packet or answer, user can rate usefulness 1-5.

### If rating >= 4

For used memories:

```text
success_count += 1
energy_cost *= 0.95
salience += 0.03
last_accessed_at = now
```

For used relations:

```text
success_count += 1
energy_cost *= 0.95
strength += 0.03
```

For used lenses:

```text
increase lens weight for this domain/query type
```

### If rating <= 2

For used memories:

```text
failure_count += 1
energy_cost *= 1.05
confidence -= 0.03 if failure was factual/relevance-related
```

For used relations:

```text
failure_count += 1
energy_cost *= 1.05
strength -= 0.03
```

Do not reinforce merely because a memory was retrieved.

Only reinforce based on usefulness feedback or downstream verified success.

---

## 22. Open Loops

Open loops represent background thoughts.

Examples:

```text
What makes ambitious bakes stressful vs successful?
How can cooking memory architecture transfer to lab automation?
What is the best business wedge for this memory system?
Which repeated failures show up across domains?
What makes context packets better than raw chat retrieval?
```

### Open Loop Triggering

An open loop triggers when a new memory or query matches:

- trigger patterns
- entity overlap
- semantic similarity
- lens overlap
- domain bridge

Open loops should not interrupt low-effort retrieval.

Trigger rules:

```text
effort 1-2:
  ignore open loops

effort 3:
  include only direct open loops in same domain

effort 4-5:
  include cross-domain open loops if relevant
```

---

## 23. Skeptic / Governance Layer

Not full v0, but implement basic checks.

### Skeptic Should Flag

- stale memory
- contradicted memory
- assistant suggestion treated as fact
- high-risk health/finance claim
- cross-domain analogy treated literally
- low-confidence memory overused
- relationship context possibly superseded
- query drifting across domains without reason

### Skeptic Should Not

- criticize everything
- block creativity
- add irrelevant edge cases
- derail low-stakes queries

### Basic Governance Output

```json
{
  "warnings": [
    {
      "type": "stale_memory",
      "message": "This memory may be superseded by a later relationship update.",
      "memory_id": "..."
    }
  ],
  "safe_to_use": true
}
```

---

## 24. UI Requirements

Build minimal Streamlit UI.

### Page 1: Import

- choose export zip/folder path
- run import
- show parsed conversation count
- show parsed message count
- show chunk count

### Page 2: Domain Classification

- classify batch
- show domain distribution
- filter chunks by domain
- inspect low-confidence classifications

### Page 3: Candidate Memories

- list pending candidates
- filter by domain/type/confidence
- approve/reject/merge
- show source chunk/message

### Page 4: Canonical Memories

- browse/search memories
- filter by domain/type/lens/status
- inspect sources
- edit memory
- mark stale/superseded

### Page 5: Retrieval Lab

Inputs:

- query
- effort level
- domain override
- lens selection
- include cross-domain analogies toggle

Outputs:

- retrieved memories
- scores
- context packet
- warnings
- sources
- rating control

### Page 6: Open Loops

- create open loop
- view active loops
- inspect triggered memories
- update hypothesis
- mark resolved/archived

### Page 7: Seed Report

Show:

- domain distribution
- number of memories by type
- highest salience memories
- possible stale memories
- unresolved contradictions
- active open loops
- top cross-domain bridges

---

## 25. Project Structure

```text
mnemonic_os/
  README.md
  pyproject.toml
  .env.example

  app/
    main.py
    config.py
    cli.py

    db/
      base.py
      session.py
      models.py
      migrations/

    schemas/
      raw.py
      episode.py
      memory.py
      retrieval.py
      context_packet.py
      open_loop.py

    services/
      chatgpt_import/
        loader.py
        parser.py
        cleaner.py
        chunker.py

      llm.py
      embeddings.py
      domain_classifier.py
      candidate_extractor.py
      deduper.py
      canonicalizer.py
      relation_builder.py
      retrieval.py
      lenses.py
      context_compiler.py
      reinforcement.py
      open_loops.py
      governance.py
      seed_report.py

    prompts/
      classify_domain.md
      extract_candidates.md
      dedupe_candidates.md
      build_relations.md
      compile_context.md
      governance_review.md
      open_loop_update.md

  ui/
    streamlit_app.py

  data/
    mnemonic.sqlite
    raw_exports/
    processed/
    exports/
    seed_reports/

  tests/
    test_import.py
    test_domain_classifier.py
    test_candidate_extractor.py
    test_dedupe.py
    test_retrieval.py
    test_context_compiler.py
    test_temporal_supersession.py
```

---

## 26. LLM Provider Abstraction

Implement provider interface:

```python
class LLMProvider:
    def complete(self, prompt: str, system: str | None = None) -> str:
        ...

    def structured_complete(self, prompt: str, schema, system: str | None = None):
        ...
```

Initial providers:

```text
OpenAIProvider
DummyProvider
```

Optional later:

```text
OllamaProvider
AnthropicProvider
LocalModelProvider
```

---

## 27. Embedding Provider Abstraction

```python
class EmbeddingProvider:
    def embed(self, text: str) -> list[float]:
        ...
```

v0 can use:

- OpenAI embeddings
- local sentence-transformers
- fallback keyword scoring if embeddings are not configured

---

## 28. Prompt: Candidate Extraction Requirements

The candidate extraction prompt must include:

```text
You are extracting durable memory from a ChatGPT history chunk.

Only extract information likely to be useful later.

Separate:
- what the user said
- what the user did
- what outcome the user reported
- what the assistant suggested
- what is speculative
- what is stale or may be superseded

Do not treat assistant advice as user fact unless the user accepted it or reported using it.

For every extracted memory include:
- content
- memory_type
- reason_type
- domains
- lenses
- entities
- abstract_pattern
- confidence
- salience
- source message ids
- source chunk ids
- temporal validity
- review requirement
```

---

## 29. Prompt: Context Compiler Requirements

The context compiler prompt must include:

```text
You are compiling a dense context packet for a downstream AI agent.

Do not answer the user directly unless asked.
Do not include irrelevant history.
Prefer compact, high-signal context.
Mark stale or uncertain memories.
Mark cross-domain analogies as analogies.
Preserve source memory IDs.
Separate current truth from old/superseded context.
```

---

## 30. High-Risk Domain Handling

### Health

Health memories should:

- default to lower confidence unless user directly stated/reporting
- include dates
- avoid turning old routines into current advice
- be retrieved with health_caution lens
- trigger governance warnings for recommendations

### Finance

Finance memories should:

- include timestamps
- mark positions/theses as possibly stale
- distinguish current holdings from past discussion
- trigger financial_caution lens
- avoid treating old market data as current

### Relationships

Relationship memories should:

- use temporal validity
- allow supersession
- avoid pulling old partner context into current partner context
- flag stale relationship memories

### Legal

Legal/law memories should:

- distinguish LSAT/law learning from legal advice
- mark jurisdiction/time-sensitive claims as caution

---

## 31. Acceptance Criteria

### Import

- Can import ChatGPT export zip.
- Can parse conversations and messages.
- Can create chunks.
- Can classify chunks by domain.
- Can embed or keyword-index chunks.

### Seeding

- Can extract candidate memories from chunks.
- Can review/approve/reject candidates.
- Can deduplicate and canonicalize memories.
- Can preserve source links.
- Can mark assistant suggestions separately.
- Can detect likely stale/superseded context.

### Retrieval

- Can query memory by text.
- Can use effort levels 1-5.
- Can use domain scoping.
- Can use memory lenses.
- Can compile context packets.
- Can include source IDs.
- Can warn about domain drift/stale memory.

### Active Memory

- Can create new memory manually or from session.
- Can rate retrievals.
- Can reinforce/penalize used memories.
- Can update confidence/salience/energy cost.

### Open Loops

- Can create open loops.
- Can trigger open loops during high-effort retrieval.
- Can update hypotheses.

### UI

- Can run in Streamlit locally.
- Can inspect raw chunks and sources.
- Can review candidate memories.
- Can browse canonical memories.
- Can run retrieval lab.

---

## 32. Example Queries for Testing

### Cooking/Baking

```text
What dessert should I make tonight based on prior wins and skill progression?
```

Expected:

- retrieves crème brûlée/focaccia/Black Forest/etc.
- distinguishes wins from failures
- includes preferences and constraints
- effort 5 adds creative transfer

### Domain Drift Test

```text
What should I make for dinner?
```

Expected:

- primarily cooking_baking
- may include relationship preferences
- should not retrieve finance/AI theory unless explicitly requested

### Cross-Domain Analogy Test

```text
What cooking lessons transfer to lab automation?
```

Expected:

- retrieves cooking and lab automation memories
- labels transfer as analogy
- does not treat analogy as literal evidence

### Stale Context Test

```text
What should I cook for my girlfriend?
```

Expected:

- uses current partner context
- does not use superseded Emma wedding context as active current truth

### Health Caution Test

```text
What supplements should I take tonight?
```

Expected:

- retrieves current supplement memories if available
- marks old routines as potentially stale
- includes caution warning
- avoids overconfident medical advice

### Finance Caution Test

```text
How should I adjust my portfolio?
```

Expected:

- retrieves strategy memories
- marks positions/time-sensitive market data as stale unless recently confirmed
- includes finance caution
- avoids treating old prices as current

---

## 33. Seed Report Requirements

After a seed import, generate:

```text
Total conversations imported
Total messages imported
Total chunks created
Domain distribution
Candidate memories by type
Approved memories by type
Top entities
Possible stale memories
Possible contradictions
Top recurring patterns
Top open loops
Suggested next domain to clean/review
```

Output as:

```text
data/seed_reports/seed_report_<timestamp>.md
```

---

## 34. Implementation Order

### Milestone 1: Skeleton

- project structure
- SQLite models
- config/env
- CLI shell
- Streamlit shell

### Milestone 2: Import

- load export
- parse conversations/messages
- clean text
- create chunks
- display import counts

### Milestone 3: Domain Classification

- keyword classifier first
- LLM classifier second
- review low-confidence classifications

### Milestone 4: Candidate Extraction

- extraction prompt
- structured output validation
- candidate memory table
- source linking

### Milestone 5: Review + Promotion

- candidate UI
- approve/reject/merge
- promote canonical memories
- create memory_sources

### Milestone 6: Retrieval

- direct semantic/keyword retrieval
- effort levels
- domain scoping
- lens filtering
- domain drift penalty

### Milestone 7: Context Compiler

- context packet generation
- source IDs
- warnings
- retrieval logging

### Milestone 8: Reinforcement

- rating UI
- update salience/confidence/energy
- retrieval_events

### Milestone 9: Open Loops

- create/view loops
- trigger high-effort retrieval
- update hypotheses

### Milestone 10: Governance Checks

- stale memory warning
- assistant suggestion warning
- high-risk domain warning
- cross-domain analogy warning

### Milestone 11: Seed Report

- generate markdown report
- show in UI

---

## 35. Definition of Done for First Working Prototype

The prototype is good enough when:

1. It imports the full ChatGPT export without crashing.
2. It lets the user process one domain at a time.
3. It extracts useful candidate memories from cooking/baking chats.
4. It lets the user approve/reject/merge candidates.
5. It retrieves memories with domain scoping.
6. Effort 1 and effort 5 produce visibly different context packets.
7. It warns when memory may be stale, assistant-suggested, or cross-domain.
8. It can answer cooking/baking queries better than raw vector search alone.
9. It preserves source links back to the raw chat chunks.
10. The code is modular enough to add lab automation and AI-memory domains next.

---

## 36. Important Build Notes for Codex

- Prefer working simple implementation over theoretical completeness.
- Do not overbuild multi-agent behavior yet.
- Keep every memory inspectable and editable.
- Do not trust raw chat text as canonical memory.
- Do not trust assistant suggestions as user facts.
- Use domain boundaries first, then controlled cross-domain retrieval.
- Make temporal validity a first-class field.
- Make source traceability non-negotiable.
- Optimize for local experimentation on a Mac mini.
- The first success metric is not beauty. It is whether the system retrieves better context than raw chat search.

---

## 37. Future Direction

After v0 works, add:

- lab automation memory schema
- MCP server interface
- local LLM support through Ollama
- background memory daemons
- analogy hunter agent
- skeptic agent
- memory curator agent
- external inspiration stream
- graph database migration
- active session ingestion
- source document ingestion beyond ChatGPT
- real-world project/task integration
- lab run memory product prototype

Long-term, this becomes a generalizable memory substrate for AI agents and business workflows.
