# Evolving Object Memory Architecture
## Recipe Memory as a Prototype for General-Purpose Persistent Memory

### Purpose

This proposal grew out of the recipe-memory system, but the core problem is much broader.

The original goal looked simple:

> Remember recipes.

In practice, the useful information was not just the final recipe. What mattered was the full evolution of competence:

```text
idea
  ↓
first draft
  ↓
shopping list
  ↓
execution
  ↓
photos
  ↓
critique
  ↓
modifications
  ↓
canonical recipe
  ↓
future experiments
```

The system therefore should not treat a recipe as a static note. It should treat it as a **persistent evolving object** with history, evidence, relationships, and a current canonical state.

This same architecture generalizes beyond cooking to projects, people, companies, research programs, workouts, software systems, businesses, and other long-lived entities.

---

# 1. Core Principle

The memory system should distinguish four layers:

```text
Chunks
  ↓
Events
  ↓
Objects
  ↓
World Model
```

## Chunks

Raw observations or retrieved pieces of information.

Examples:

- one chat message
- one paragraph from a document
- one photo caption
- one tool result
- one note fragment

Chunks are evidence, not durable identities.

## Events

Things that happened.

Examples:

- "We baked financiers on July 12."
- "The pavlova cracked during shaping."
- "The user changed the ingredient ratio."
- "A project milestone was completed."
- "A company announced a new product."

Events are immutable records of change.

## Objects

Persistent identities that survive across events.

Examples:

- Brown-Butter Fruit Financiers
- Devyn
- Gigabrain Portfolio
- Memory System
- Banana Matcha
- A software repository
- A research question

Objects accumulate evidence and state over time.

## World Model

The graph of persistent objects and their relationships.

Examples:

```text
Banana Matcha
  ├── preferred_by → Devyn
  ├── inspired_by → Academic Coffee
  ├── uses_component → Banana Cream
  └── belongs_to → Cooking Project
```

The world model is not merely a semantic index. It is the system's representation of what exists, what changed, and how things relate.

---

# 2. The Missing Abstraction: Identity Persistence

The current memory architecture can store, retrieve, compress, rank, and relate information.

The critical missing concept is **identity persistence**.

The system should ask during ingestion:

> Does this information update an existing object, create a new object, describe an event, or remain only raw evidence?

This should happen before information is flattened into generic memory chunks.

Without persistent identity, repeated conversations create fragmented observations:

```text
financiers
financier batch
brown butter almond cakes
those little cakes from last week
favorite bake
```

A chunk-oriented system may treat these as loosely related memories.

An object-oriented memory system should recognize them as updates to:

```text
Object: Brown-Butter Fruit Financiers
```

This prevents important knowledge from disappearing simply because it was discussed under different wording or in different conversations.

---

# 3. Recipe Memory as the Reference Implementation

Recipes are a useful prototype because their lifecycle makes memory failures obvious.

A recipe should be a first-class object rather than fundamentally a Markdown file.

Markdown should be one serialization or user-facing representation of the object.

A recipe object might contain:

```yaml
Recipe:
  identity:
    name:
    aliases:
    cuisine:
    category:
    status:

  canonical_version:
    ingredients:
    steps:
    timing:
    equipment:
    yield:

  execution_history:
    - date:
      participants:
      ingredients_actually_used:
      substitutions:
      timing:
      photos:
      outcome:
      rating:
      mistakes:
      observations:
      next_time:

  components:
    - lemon_curd
    - mascarpone_cream
    - strawberry_coulis

  flavor_profile:
    sweetness:
    acidity:
    bitterness:
    salt:
    fat:
    crunch:
    creaminess:
    brightness:
    aromatics:
    serving_temperature:

  experiment_queue:
    - hypothesis:
      priority:
      expected_benefit:
      risk:
      required_ingredients:

  evidence:
    confidence:
    supporting_events:
    contradictions:

  relationships:
    preferred_by:
    similar_to:
    derived_from:
    part_of:
    uses_component:
```

---

# 4. Canonical State vs. History

Every persistent object may have a current canonical state.

The system must distinguish:

```text
Current State
≠
History
```

The canonical representation should be clean and useful.

History should remain immutable.

For a recipe:

```text
Canonical Recipe
```

may say:

> Bake at 375°F for 20–24 minutes.

But the event history may contain:

```text
Attempt 1: baked 18 min, center too wet
Attempt 2: baked 22 min, excellent
Attempt 3: fruit-heavy batch needed 24 min
```

The system should never destroy those observations when updating the canonical recipe.

Instead, canonical state is a synthesized view derived from history.

---

# 5. Immutable Execution / Experiment Records

Every substantial attempt should generate an immutable event.

For cooking:

```yaml
Execution:
  object_id:
  timestamp:
  source_conversation:
  participants:
  actual_inputs:
  substitutions:
  environment:
  process_changes:
  timing:
  outcome:
  rating:
  qualitative_feedback:
  media:
  failures:
  next_experiment:
```

The same structure generalizes.

For software:

```text
Execution → deploy/test/run
```

For research:

```text
Execution → experiment/read/analysis
```

For investing:

```text
Execution → trade/rebalance/thesis update
```

For exercise:

```text
Execution → workout
```

Events should be append-only unless explicitly corrected with provenance.

---

# 6. Evidence-Aware Canonicalization

A canonical state should know why it is canonical.

Example:

```text
Brown-Butter Fruit Financiers

Canonical confidence: High

Evidence:
- 5 successful executions
- user rated first batch 10/10
- repeated positive feedback
- taller shape preferred
- tangier fruit consistently preferred

Rejected alternatives:
- sweeter fruit filling
- thinner bake
```

The system should be able to answer:

> Why is this the canonical version?

That requires storing provenance and evaluation, not just the latest text.

---

# 7. Object Versioning

Objects should support versioned states.

Example:

```text
Financiers v1
  ↓
Financiers v2
  ↓
Financiers v3 [canonical]
```

But versioning should not necessarily duplicate the full object.

Prefer:

```text
base object
+
state deltas
+
event evidence
```

Possible internal model:

```yaml
ObjectState:
  object_id:
  version:
  valid_from:
  supersedes:
  derived_from_events:
  state_patch:
```

This enables historical queries such as:

> What did we believe the best financier recipe was in June?

or:

> When did banana extract enter the banana-matcha recipe?

---

# 8. Component Graphs

Some objects contain reusable sub-objects.

Cooking makes this especially clear.

```text
Strawberry Pavlova
  ├── Lemon Curd
  ├── Mascarpone Cream
  └── Strawberry Coulis
```

These should not be duplicated text fragments.

They should be linked objects.

Advantages:

- improvements propagate
- reuse is explicit
- ingredient dependencies are queryable
- failures can be localized
- comparisons become possible

This generalizes naturally.

Software:

```text
Application
  ├── Authentication Service
  ├── Database Layer
  └── Retrieval System
```

Research:

```text
Research Program
  ├── Dataset
  ├── Hypothesis
  └── Experiment
```

---

# 9. Experiment Queue

Ideas should not silently modify the canonical object.

They should enter a separate experiment queue.

Example:

```yaml
Experiment:
  object_id: brown_butter_financiers
  hypothesis: Increase batter depth to improve interior texture.
  expected_benefit: Taller moist center.
  risk: Uneven bake.
  priority: high
  status: proposed
```

Possible statuses:

```text
proposed
scheduled
attempted
supported
rejected
inconclusive
promoted
```

Only supported experiments should influence canonical state.

This prevents speculative ideas from contaminating established knowledge.

---

# 10. Negative Memory Matters

The memory system should explicitly preserve failed or mediocre outcomes.

Example:

```text
Green soup:
Outcome: okay
Priority for preservation: low

False brownies:
Outcome: okay
Do not treat as canonical favorite
```

This matters because absence of negative evidence creates repeated mistakes.

The system should remember:

```text
We tried this.
It was not worth repeating.
```

That is useful knowledge.

Negative memory should remain retrievable without dominating normal recall.

---

# 11. Ratings and Multi-Dimensional Evaluation

A single scalar rating is often insufficient.

For a recipe:

```yaml
Evaluation:
  taste:
  texture:
  appearance:
  effort:
  repeatability:
  novelty:
  partner_reaction:
  leftover_quality:
```

For other domains:

Software:

```text
correctness
maintainability
latency
reliability
```

Research:

```text
novelty
confidence
replicability
importance
```

Investing:

```text
thesis_strength
upside
downside
liquidity
time_horizon
```

Objects should support domain-specific evaluation schemas built on a shared evaluation primitive.

---

# 12. Memory Promotion

Not every memory deserves object status.

Suggested promotion pipeline:

```text
Raw Chunk
  ↓
Repeated / Important Observation
  ↓
Event
  ↓
Candidate Object
  ↓
Persistent Object
  ↓
Canonical State
```

Promotion signals may include:

- repeated mentions
- explicit user importance
- emotional salience
- repeated actions
- linked evidence
- future usefulness
- named identity
- high retrieval frequency

Example:

A single idea for a pastry may remain a chunk.

After the user makes it:

```text
event
```

After repeated use and feedback:

```text
persistent recipe object
```

---

# 13. Object Resolution

The system needs an entity-resolution process.

Incoming observation:

> "Those financiers were maybe my favorite bake."

Candidate matches:

```text
Brown-Butter Fruit Financiers
Chocolate Financiers
Financier Experiment Notes
```

Resolver should consider:

- semantic similarity
- aliases
- temporal context
- linked conversation
- ingredient overlap
- known references
- recent active objects

Possible output:

```yaml
resolution:
  object_id: brown_butter_fruit_financiers
  confidence: 0.97
```

Low-confidence cases should remain unresolved rather than being merged aggressively.

---

# 14. Contradiction Handling

Objects will contain conflicting observations.

Example:

```text
Attempt 1:
18 minutes was perfect.

Attempt 4:
18 minutes was underbaked.
```

The correct response is not to delete one.

Instead store context:

```text
18 minutes:
works for shallow unfilled financiers

20–24 minutes:
preferred for fruit-filled deeper financiers
```

The memory system should seek conditional reconciliation before deciding that one observation is wrong.

This is important across all domains.

---

# 15. Context-Dependent Canonical States

There may be more than one valid canonical state.

Example:

```text
Banana Matcha
  ├── Daily Version
  ├── Batch-Prep Version
  └── Café-Style Maximum-Effort Version
```

Therefore canonicalization should allow:

```yaml
canonical_views:
  default:
  quick:
  maximum_quality:
  travel:
```

This avoids forcing one representation to satisfy every context.

---

# 16. User Preferences as Separate Objects

Preferences should not be buried inside recipes.

Examples:

```text
User dislikes:
- mushrooms
- cilantro
- raw onion
- goat cheese

Devyn prefers:
- vegetarian dishes
- complex but not overly sweet desserts
```

These should exist as persistent preference objects linked to recipes.

Then planning can query:

```text
recipes
WHERE compatible_with(Devyn)
AND duration < 2 hours
AND novelty > medium
```

This prevents duplicate preference storage across every recipe.

---

# 17. Query Model

The system should support queries that combine object state, event history, relationships, and evaluation.

Examples:

```text
What desserts have we rated highest?
```

```text
What dishes used preserved lemon successfully?
```

```text
Which recipes did Devyn like that take under 90 minutes?
```

```text
What baking techniques have we attempted but not mastered?
```

```text
Show recipes where the last attempt suggested an unresolved experiment.
```

```text
What did we make with saffron that was actually successful?
```

This requires structured object memory, not retrieval over raw prose alone.

---

# 18. Memory Retrieval Should Be Object-Aware

Standard RAG might retrieve:

```text
chunk 44
chunk 891
chunk 1201
```

Object-aware retrieval should instead produce:

```text
Object: Brown-Butter Fruit Financiers

Current state
Relevant events
Supporting chunks
Pending experiments
```

Raw chunks remain available for provenance.

But they should rarely be the final abstraction presented to the reasoning model.

---

# 19. Compression

Compression should operate differently at each level.

## Chunk Compression

Summarize local content.

## Event Compression

Extract:

```text
what happened
what changed
outcome
evidence
```

## Object Compression

Maintain current state and important historical deltas.

## World-Model Compression

Maintain relationships and high-value abstractions.

This allows old raw conversations to become cold storage without losing learned knowledge.

---

# 20. Memory "Dreaming" / Consolidation

The existing memory-system idea of background consolidation becomes especially valuable with persistent objects.

A consolidation process could periodically ask:

```text
Which recent events update existing objects?

Which object states should change?

Which experiments were resolved?

Which aliases refer to the same object?

Which repeated patterns imply a new abstraction?

Which obsolete assumptions should be downgraded?
```

This resembles human memory consolidation more closely than simple summarization.

---

# 21. Abstraction Formation

Repeated objects may produce higher-level knowledge.

Example cooking observations:

```text
User repeatedly prefers:
brown butter
acidic fruit
restrained sweetness
creamy + crisp contrast
```

These can generate an abstraction:

```text
Preferred Dessert Flavor Architecture
```

The abstraction is not explicitly stated in any one event.

It is inferred across many objects.

This becomes useful for generating future recipes.

The same process applies to:

- investing strategies
- software-design preferences
- learning styles
- exercise response
- research interests

---

# 22. Object Types Should Be Extensible

The architecture should provide a general object primitive with domain schemas layered on top.

Base object:

```yaml
Object:
  id:
  type:
  aliases:
  created_at:
  updated_at:
  status:
  current_state:
  events:
  relationships:
  evidence:
  confidence:
  metadata:
```

Specializations:

```text
Recipe
Person
Project
Company
Portfolio
ResearchQuestion
SoftwareSystem
WorkoutProgram
Place
Tool
Skill
```

Domain-specific schemas should extend rather than replace the shared object model.

---

# 23. Relationship Semantics

Relationships should be typed.

Examples:

```text
uses_component
preferred_by
created_for
derived_from
supersedes
part_of
similar_to
conflicts_with
supports
tested_by
depends_on
```

Relationships may themselves have evidence and confidence.

Example:

```yaml
Relationship:
  subject: banana_matcha
  predicate: preferred_by
  object: devyn
  confidence: high
  evidence:
    - event_2026_07_21
```

---

# 24. Source Provenance

Every meaningful state claim should be traceable.

Possible evidence sources:

```text
conversation message
tool result
photo
user rating
document
web source
execution log
sensor
```

Canonical state should preserve pointers to supporting evidence.

This protects against memory hallucination and enables later correction.

---

# 25. Confidence

Confidence should belong to claims, not merely whole memories.

Example:

```yaml
claim:
  text: Devyn prefers the banana-matcha version with stronger matcha.
  confidence: 0.85
  evidence:
    - conversation_123_message_81
```

Different fields in an object may have different confidence levels.

---

# 26. Object State Should Be Materialized, Not Reconstructed Every Time

The system should preserve both:

```text
event log
+
materialized current state
```

Reconstructing state from thousands of events during every query is inefficient.

This resembles event-sourced software architectures:

```text
Events = source of history
Materialized Object = fast current view
```

Periodic reconciliation can ensure the materialized state remains consistent with evidence.

---

# 27. The Recipe-Book Lessons

The cooking-memory audit exposed several failure modes:

## Important successes can disappear

The financiers were one of the highest-rated bakes but were absent from the persistent cookbook.

## Conversation topics are not stable identities

The same recipe may appear under multiple names across chats.

## Static files create duplication

Two banana-matcha notes were accidentally created because each looked like a separate document operation rather than an update to one recipe identity.

## Ideas contaminate tested knowledge

Recipe concepts should not be indistinguishable from completed recipes.

## Negative results matter

Mediocre dishes should remain remembered as mediocre so they are not repeatedly resurfaced as recommendations.

## Components are reusable knowledge

Lemon curd, mascarpone cream, coulis, sauces, and doughs should be linked components.

## The canonical recipe is a conclusion

It should be derived from executions and evidence, not treated as the only memory.

---

# 28. Recommended Architecture Change

Add a persistent **Object Layer** above episodic memory.

Current conceptual architecture:

```text
Raw Data
  ↓
Chunks
  ↓
Semantic / Episodic Memory
  ↓
Retrieval
```

Proposed architecture:

```text
Raw Data
  ↓
Chunks
  ↓
Events
  ↓
Object Resolution
  ↓
Persistent Objects
  ↓
Object State + History
  ↓
World Model
  ↓
Abstraction / Consolidation
  ↓
Retrieval + Reasoning
```

---

# 29. Minimum Viable Object Layer

A first implementation does not need the full architecture.

Start with:

## Object table

```yaml
id
type
name
aliases
status
canonical_state
created_at
updated_at
```

## Event table

```yaml
id
object_id
timestamp
event_type
payload
source
```

## Relationship table

```yaml
subject_id
predicate
object_id
confidence
source
```

## Claim / Evidence table

```yaml
claim_id
object_id
field
value
confidence
evidence_ids
```

Then add:

- entity resolution
- versioning
- experiment queues
- consolidation
- automatic abstraction

---

# 30. Ingestion Algorithm

A possible ingestion flow:

```text
1. Receive message / document / tool output.

2. Chunk if necessary.

3. Extract candidate:
   - events
   - entities
   - claims
   - relationships
   - preferences
   - evaluations

4. Resolve entities against existing objects.

5. Create new objects for unresolved persistent identities.

6. Append immutable events.

7. Update claim confidence.

8. Determine whether canonical state should change.

9. Update materialized object state.

10. Link source evidence.

11. Schedule low-confidence conflicts for consolidation.
```

---

# 31. Retrieval Algorithm

Instead of only semantic chunk search:

```text
1. Identify candidate objects relevant to the query.

2. Rank objects.

3. Retrieve:
   - canonical state
   - most relevant recent events
   - important historical events
   - unresolved contradictions
   - linked objects

4. Retrieve raw chunks only when additional evidence is needed.

5. Construct reasoning context.
```

This dramatically reduces context noise.

---

# 32. Storage Philosophy

Do not force everything into one representation.

Use:

```text
Structured object store
+
event log
+
vector index
+
raw source archive
+
human-readable Markdown views
```

Each serves a different purpose.

Markdown is excellent for:

- manual inspection
- editing
- export
- portability
- Codex interaction

It should not necessarily be the authoritative internal schema.

---

# 33. Expected General Benefits

Adding persistent evolving objects should improve:

## Continuity

The system stops rediscovering the same entities.

## Reliability

Canonical claims remain tied to evidence.

## Personalization

Preferences accumulate across real outcomes.

## Reasoning

Models operate over meaningful identities rather than unrelated chunks.

## Compression

Old conversations can be summarized into object changes.

## Planning

Pending experiments and unresolved tasks remain attached to the relevant object.

## Learning

The system can observe what repeatedly succeeds and form abstractions.

---

# 34. Core Design Principle

The central insight is:

> Memory should preserve not only what was said, but what exists, what happened to it, what we currently believe about it, and why.

A useful persistent memory system should therefore model:

```text
Object
+
History
+
Relationships
+
Evidence
+
Current State
+
Future Experiments
```

Recipes are simply one unusually visible example of this general problem.

The objective is not merely to remember past conversations.

It is to maintain an evolving model of the user's world.
