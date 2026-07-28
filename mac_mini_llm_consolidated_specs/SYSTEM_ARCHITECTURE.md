# System Architecture

**Status:** Normative  
**Scope:** Memory substrate, retrieval, context compilation, object model, and downstream-agent integration

## 1. Purpose

The system maintains an evolving, source-backed model of the user's world and exposes compact context to downstream AI agents.

It is not a transcript search engine and not a single opaque "memory store."

The architecture separates:

```text
Raw Evidence
    ↓
Normalized Evidence / Chunks
    ↓
Events
    ↓
Object Resolution
    ↓
Persistent Objects
    ↓
Claims + Evidence
    ↓
Materialized Object State
    ↓
World Model
    ↓
Retrieval
    ↓
Context Compilation
    ↓
Downstream AI Agent
```

## 2. Layer definitions

### 2.1 Raw evidence

Immutable or source-faithful inputs.

Examples:

- ChatGPT messages
- uploaded documents
- tool results
- recipe notes
- execution logs
- photos and captions
- later: sensors, email-derived events, web sources

Raw evidence is not assumed to be true. It is evidence about what was said, observed, suggested, or recorded.

Required properties:

```text
source_id
source_kind
timestamp
author/source role
content hash where practical
provenance pointer
```

Raw evidence should not be silently rewritten.

### 2.2 Normalized evidence / chunks

Searchable slices of raw evidence.

Chunks support lexical and semantic retrieval, but are not durable identities.

A chunk may support one or more:

- event
- claim
- object update
- relationship
- unresolved observation

Chunks are primarily a retrieval and provenance primitive.

### 2.3 Events

Append-only records of meaningful change or occurrence.

Examples:

- a recipe was attempted
- a preference was explicitly stated
- a project milestone completed
- a trade occurred
- an error happened
- a proposed experiment was tested
- a user corrected the system

Events answer:

```text
What happened?
When?
To what object(s)?
What evidence supports the event?
What was the outcome?
```

Historical events remain available even when current state changes.

### 2.4 Persistent objects

Objects represent identities that persist across conversations and events.

Examples:

```text
David
Devyn
Brown-Butter Fruit Financiers
Banana Matcha
Gigabrain Portfolio
Mac Mini Memory System
A software repository
A research question
A skill
A tool
```

Objects MUST support aliases and entity resolution.

Low-confidence identity resolution should remain unresolved rather than force a merge.

### 2.5 Claims

A claim is a typed proposition about an object or relationship.

Example:

```yaml
subject: person_devyn
predicate: dislikes
object: mushrooms
```

Claims SHOULD carry:

```text
epistemic_status
truth_confidence
confidence_basis
temporal_class
valid_from
valid_until
last_confirmed_at
status
supporting_evidence
contradicting_evidence
```

Confidence belongs to claims, not only whole records.

### 2.6 Materialized current state

Each important object may expose a current, query-friendly state derived from claims and events.

Current state is not history.

The system should maintain both:

```text
event log
+
materialized current state
```

Current state may have context-specific views:

```text
default
quick
maximum_quality
travel
current_portfolio
historical_portfolio
```

where the domain benefits from multiple valid canonical views.

### 2.7 World model

The world model links objects and claims through typed relationships.

Initial relationship semantics include:

```text
supports
contradicts
supersedes
qualifies
derived_from
caused_by
worked_for
failed_for
similar_to
contrasts_with
transfers_to
part_of
requires
balanced_by
preferred_by
depends_on
tested_by
```

Relationships may themselves carry confidence, provenance, temporal validity, and retrieval utility.

Cross-domain `similar_to` or `transfers_to` relationships are analogical links, not literal evidence.

## 3. Memory classes

The system should preserve the useful distinctions already established by earlier specs.

Representative memory/event/claim classes:

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
experiment
outcome
lesson
```

These do not all need identical storage tables. They are semantic roles.

## 4. Epistemic status

Every agent-consumable claim SHOULD distinguish how the system knows it.

Recommended values:

```text
explicit_user_statement
user_reported_observation
externally_verified_fact
system_observation
system_inference
assistant_hypothesis
manual_entry
unresolved
```

The following are not equivalent:

```text
"The user said X."
"The user reported X happened."
"The assistant inferred X."
"Several events support X."
"An external source verifies X."
```

The context compiler must preserve these distinctions when they matter.

## 5. Confidence

Do not use a numeric confidence score without a basis.

Recommended structure:

```yaml
truth_confidence: 0.94
confidence_basis:
  - direct_user_statement
  - repeated_confirmation
  - no_conflicting_evidence
```

or:

```yaml
truth_confidence: 0.63
confidence_basis:
  - inferred_from_3_consistent_events
  - no_direct_user_statement
```

`truth_confidence` MUST NOT be reduced merely because a claim was retrieved in an unhelpful context.

## 6. Temporal classes

Timestamps alone are insufficient. Claims and state should classify expected persistence.

Recommended temporal classes:

```text
enduring
slowly_changing
active_state
ephemeral
```

Examples:

```text
Devyn dislikes mushrooms          -> enduring
David owns an immersion blender   -> slowly_changing
Active sourdough starter          -> active_state
Six strawberries in refrigerator  -> ephemeral
```

Temporal class informs decay, revalidation, and retrieval priority.

## 7. Constraint semantics

Constraints require explicit strength because some should gate an answer rather than merely influence ranking.

Recommended values:

```text
hard
strong
soft
contextual
```

A hard dietary restriction must be able to override high semantic similarity to an incompatible recipe.

Constraint retrieval is a first-class operation, not just another weighted memory score.

## 8. Object-aware ingestion

Ingestion should ask, in this order:

```text
1. What evidence arrived?
2. Does it describe an event?
3. Which existing objects are involved?
4. Does it create a new persistent object?
5. What claims or relationships are supported?
6. Does current state need updating?
7. Are there contradictions or supersession candidates?
8. Should any inference be proposed separately?
```

Ideas and assistant suggestions should not silently modify canonical state.

Proposed changes belong in hypotheses/experiments until supported.

## 9. Candidate promotion

Candidate quality should be evaluated by future agent usefulness.

A high-signal candidate is one that could materially change a reasonable future agent decision.

Candidate scoring SHOULD consider:

```text
future decision impact
persistence
standalone interpretability
attribution certainty
epistemic quality
novelty
specificity
```

A candidate such as:

```text
"like this?"
```

is low signal even if it appeared during an important conversation.

A long passage is not automatically high signal. Useful propositions should be extracted from it.

## 10. Deduplication and canonicalization

Deduplication must operate at the proposition/object level, not only text similarity.

Use signals such as:

```text
resolved subject/object identity
predicate similarity
temporal overlap
source agreement
domain
memory/event type
semantic similarity
```

Near-duplicate evidence may strengthen one canonical claim.

Contradictions must not be merged silently.

Where possible, reconcile conditionally:

```text
18 min works for shallow financiers
22–24 min works for deeper fruit-filled financiers
```

rather than choosing one as universally true.

## 11. Retrieval architecture

Retrieval should be hierarchical.

### Ring 0: automatic context

Tiny, high-impact context:

- hard constraints
- directly relevant current state
- strong stable preferences
- critical contradictions/warnings

### Ring 1: canonical object state

Relevant objects, claims, relationships, and compact summaries.

### Ring 2: episodes/events

Relevant execution history, failures, decisions, outcomes, and recent state changes.

### Ring 3: raw evidence

Source messages/chunks/documents used for verification or ambiguity resolution.

The downstream model should not pay Ring 3 context cost unless needed.

## 12. Retrieval flow

A typical retrieval should:

```text
1. Detect relevant domain(s), entities, and task type.
2. Retrieve hard constraints and critical current state.
3. Resolve relevant objects.
4. Rank current claims/object states.
5. Add relevant events and outcomes.
6. Check temporal validity and contradictions.
7. At higher effort, explore analogies/open loops.
8. Retrieve raw evidence only as needed.
9. Compile a bounded agent context packet.
10. Log exposures and downstream usage signals.
```

## 13. Effort levels

Effort is a compute/retrieval work budget, not a fixed result-count schedule.

Higher effort MAY increase:

- graph traversal
- contradiction checks
- source verification
- temporal checks
- object resolution work
- analogy search
- open-loop inspection
- number or capability of models used
- synthesis depth

Higher effort does NOT imply a larger final context packet.

A deep retrieval may still conclude that six items are the best context.

## 14. Cross-domain retrieval

Default retrieval is domain-scoped.

Cross-domain retrieval should happen when:

```text
the user explicitly requests analogy/creativity
or
higher effort finds a structurally useful transfer
```

Cross-domain results MUST be labeled as analogies or transferable patterns, not direct evidence.

Primary-domain retrieval and analogy retrieval should be separable stages.

## 15. Open loops and experiments

Open loops represent unresolved persistent questions.

Experiments represent proposed changes or hypotheses tied to an object.

Neither should silently alter current truth.

Experiment states may include:

```text
proposed
scheduled
attempted
supported
rejected
inconclusive
promoted
```

Supported experiments may update current canonical state with provenance.

## 16. Negative memory

Failures, mediocre outcomes, rejected ideas, and prior unsuccessful recommendations are useful memory.

They should remain retrievable without dominating ordinary recall.

The system should be able to represent:

```text
We tried this.
It was not worth repeating.
```

and distinguish that from:

```text
We tried this, but the attempt was confounded.
```

## 17. Reinforcement

Keep three concepts separate:

```text
truth_confidence
retrieval_utility
task_relevance_history
```

User satisfaction with an answer must not automatically alter factual truth confidence.

Retrieval utility may adapt based on:

```text
useful exposure
ignored exposure
user correction
constraint violation
successful downstream action
repeated irrelevance
```

Truth confidence changes only when evidence about truth changes.

## 18. Storage philosophy

Do not force all memory into one representation.

Use appropriate layers:

```text
raw archive
normalized relational store
object/current-state store
event log
claim/evidence store
FTS index
semantic vector index
human-readable Markdown projections
audit and retrieval logs
```

Markdown is excellent for inspection and portability. It is not necessarily the authoritative internal state.

## 19. Non-goals for the current phase

Do not prioritize yet:

- fully autonomous memory "dreaming"
- large multi-agent swarms
- unrestricted background modification
- graph-database migration solely for novelty
- complex GUI work before agent-facing retrieval is useful
- sophisticated reinforcement before reliable credit assignment
