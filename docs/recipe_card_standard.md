# Recipe Card Standard

Use this format for every new recipe note and for normalizing older cards.

## Goals

- Keep recipe notes short, explicit, and easy for an AI to parse.
- Make the recipe act as the source of truth between the user and the food.
- Avoid nested, decorative, or overly chatty layouts.
- Preserve enough provenance that a card can be traced back later.

## Required Sections

1. `# Title`
1. Short summary paragraph
1. `## At a glance`
1. `## Ingredients`
1. `## Method`
1. `## Notes`
1. `## Source`

## Writing Rules

- One ingredient per bullet.
- One method step per numbered item.
- Do not use nested ingredient or step subheadings.
- Keep the summary to one short paragraph.
- Put substitutions, warnings, and edge cases in `## Notes`.
- Put file IDs, query prompts, or other provenance in `## Source`.
- If a field is unknown, leave it out rather than inventing it.

## At A Glance

Include these when known:

- Yield
- Prep time
- Cook time
- Total time
- Tags

## Standard Checklist

Before creating a new recipe card, the agent should confirm:

- The card follows the required section order.
- Ingredients are flat and complete.
- Method steps are flat and imperative.
- Notes are brief.
- Source is explicit.
- The card does not duplicate chatter that belongs in the chat transcript.

## Normalization Rule

Existing recipe notes should be rewritten into this layout when the source content is already present in the card. The rewrite should preserve the recipe's substance while removing nested layout noise.
