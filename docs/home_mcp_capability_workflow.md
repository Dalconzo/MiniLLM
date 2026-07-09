# Home MCP Capability Workflow

Use this document when adding a new `home-mcp` capability or extending an existing one.

## Goal

Keep the surface narrow, reversible, traceable, and safe enough for ChatGPT and local agents to use without direct shell access.

## Before You Add Anything

Answer these questions first:

- What exact user request does the capability enable?
- What root or directory does it touch?
- Is it read, create, append, edit, delete, or execute?
- Can the safe version still be useful if we remove power?
- Does it need confirmation before it runs?
- Can it be rolled back cleanly?
- What trace data should it write?
- Could it expose secrets or private paths?
- Does it need to be available through ChatGPT, local terminal use, or both?

If the answer is unclear, do not add the capability yet. Put the uncertainty into a bead or spec note first.

## Build Order

1. Define the user-facing need and the exact allowed scope.
2. Add the server tool in `src/local_agent_lab/home_mcp.py`.
3. Add the CLI command in `src/local_agent_lab/cli.py` if humans need terminal access.
4. Add or update trace logging for load, dispatch, action, and error stages.
5. Add unit tests for:
   - happy path
   - blocked path
   - path escape or validation failure
   - trace artifact creation
6. Update `README.md` if the command is user-facing.
7. Update launchd or tunnel wiring only if the capability must stay available after reboot.

## Capability Rules

- Prefer allowlisted roots over arbitrary filesystem access.
- Prefer create-only or append-only operations for human-authored notes.
- Avoid delete unless the spec explicitly requires it.
- Avoid raw shell execution unless a separate bead approves it.
- Keep assistant-generated suggestions separate from confirmed data unless the release explicitly allows promotion.
- Write clear errors, not silent fallback behavior.

## Trace Requirements

Every capability should leave an audit trail that answers:

- when it ran
- what it tried to touch
- what it returned
- where it failed

For high-risk capabilities, include the reason for rejection or refusal in the trace payload.

## Recipe-Specific Pattern

If the capability is recipe-related, prefer this progression:

1. search or list candidate notes
2. draft a structured recipe card from the source text or file
3. review the draft before writing anything durable
4. create or append a structured recipe note
5. promote useful results into the curated memory layer later

Do not jump straight from raw transcript text to a permanent recipe record without review.

## New Capability Checklist

- Add or update the bead that describes the work.
- Keep the implementation inside the existing allowlisted roots.
- Add tests before widening the scope.
- Verify the terminal command and the MCP tool stay in sync.
- Confirm the tunnel or launchd path still works after the change.
- Document the final operator command in `README.md` if needed.
- Keep the structured recipe draft and recipe card schemas aligned.

## What To Avoid

- Broad filesystem reads outside the allowlist.
- Delete-by-default workflows.
- Hidden side effects.
- Untested prompts that write notes automatically.
- New capability surfaces without trace coverage.
