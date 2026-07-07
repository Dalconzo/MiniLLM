# Home MCP Scope for Codex

## Codex Starter Prompt

Build the first version of a self-hosted `home-mcp` server for a Mac mini. The goal is to expose a controlled set of MCP tools for recipe notes, household/project memory, and repo-scoped coding work while keeping the system secure, reversible, and easy to extend.

Start with the MVP only:

- Read-only file discovery and search inside allowlisted directories.
- Create-only and append-only Markdown note tools.
- No arbitrary shell access.
- No delete tool.
- No unrestricted filesystem browsing.
- No access outside configured roots.
- Audit logs for every tool call.
- A path-safety layer with tests.

Do not build autonomous agents yet. Design the interfaces so agents and local inference can be added later behind MCP.

## 1. Purpose

This project creates a self-hosted MCP system running on a Mac mini that lets ChatGPT, Codex, and later local models interact with selected local data and project directories.

The system should become a stable tool layer between AI clients and personal infrastructure:

- ChatGPT for high-quality reasoning, planning, writing, and synthesis.
- Codex for implementation work in scoped repos.
- Local inference for private, cheap, routine background tasks.
- Mac mini as the always-on compute and data host.
- MCP as the typed boundary between models and real actions.

Core principle:

> Expose narrow, typed, reversible capabilities, not raw machine access.

This is not "AI can use my Mac." It is "AI can call specific tools inside specific workspaces with logging, validation, and rollback."

## 2. High-Level Architecture

```text
AI clients
  - ChatGPT
  - Codex
  - Open WebUI or local assistant later
  - scheduled local agents later

        |
        v

MCP over HTTPS

        |
        v

Home MCP Server
  - authentication
  - tool registry
  - permission checks
  - input validation
  - path allowlisting
  - logging
  - confirmation gates
  - patch/diff management later

        |
        v

Controlled local resources
  - HomeBrain directory
  - allowed project repos
  - recipe database
  - project notes
  - Nextcloud files later
  - SQLite/Postgres
  - vector index later
  - local model backend later
  - optional camera/sensor systems later
```

The MCP server should be reachable over HTTPS when connected to external AI clients. Use a tunnel or relay instead of raw router port forwarding.

Preferred exposure options:

- Cloudflare Tunnel
- Tailscale Funnel
- VPS reverse proxy over WireGuard or Tailscale
- local development tunnel during testing

Do not expose the Mac mini directly to the public internet.

## 3. Initial Directory Boundary

The first version should only access controlled roots like:

```text
~/HomeBrain/
  recipes/
  baking_logs/
  crafts/
  household/
  projects/
  inbox/
  archive/

~/AIWorkspaces/
  home-mcp/
  recipe-mcp/
  ipad-dashboard/
  bread-monitor/
```

The MCP service must not access:

- `~/.ssh`
- password stores
- browser profiles
- Desktop
- Downloads
- iCloud root
- financial documents
- health documents
- private legal files
- Devyn's personal files unless explicitly scoped later
- arbitrary system paths

Path handling must enforce:

- allowlisted roots only
- no `..` traversal
- no symlink escape from allowed roots
- no hidden-file access unless explicitly enabled
- no unrestricted recursive reads of huge directories
- size limits for file reads
- explicit file type policy

## 4. First-Class Interaction Types

### 4.1 Read-Only Knowledge Interactions

Purpose: let AI understand files, notes, recipes, project docs, logs, and code without modifying anything.

Example user requests:

```text
Search my baking logs for focaccia timing notes.
Find the stuffed pepper recipe we liked and summarize what made it work.
Look through this repo and explain the architecture.
Find TODOs across the bread monitor project.
Compare these recipe attempts and tell me what changed.
```

Initial tools:

```text
list_allowed_roots()
list_files(root_id, glob, limit)
search_files(query, root_id?, file_types?)
read_file(file_id)
read_files(file_ids)
search_notes(query)
search_recipes(query)
get_recipe(recipe_id)
list_recent_files(root_id, limit)
```

Rules:

- Reads must be scoped by root.
- Search results should include file ID, relative path, modified time, snippet, and match reason.
- Large files should be chunked.
- Binary files should return metadata unless a parser exists.
- Secrets should be redacted before returning content.
- Tool output should be structured JSON, not loose prose.

### 4.2 Safe Write Interactions

Purpose: let AI create new notes, append logs, and save generated documents without risking destructive edits.

Example user requests:

```text
Save this as a recipe note.
Append tonight's creme brulee result to the baking log.
Create a project note for the craft iPad setup.
Make a household checklist from this plan.
Save this menu as Devyn Bar Celebration v1.
```

Initial tools:

```text
create_markdown_note(root_id, folder, title, body, tags?)
append_markdown_log(root_id, file_id, entry, tags?)
create_recipe(title, body, metadata?)
append_recipe_attempt(recipe_id, notes, outcome?, next_time?)
create_project_note(project_id, title, body)
```

Rules:

- Prefer create and append before edit.
- No overwriting existing files without an explicit overwrite mechanism.
- File names must be sanitized.
- Writes should return created or changed paths, a summary, and a change ID.
- Every write should be logged.
- Once Git-backed writes are added, every write should create a commit or snapshot.

### 4.3 Patch-Based Edit Interactions

Purpose: let AI edit existing files safely through diffs instead of blind overwrites.

Example user requests:

```text
Refactor the recipe parser and update the tests.
Edit the stuffed pepper recipe to include plating notes.
Clean up this README and add setup instructions.
Change this project plan to split MVP and later phases.
```

Tools for later phases:

```text
propose_patch(file_ids, instructions)
show_patch(patch_id)
apply_patch(patch_id)
reject_patch(patch_id)
get_git_diff(root_id)
revert_last_change(root_id)
```

Rules:

- All edits should be patch-first.
- The AI should inspect the patch before applying it.
- Destructive patches require confirmation.
- Patch application should fail safely on conflicts.
- Patches must be limited to allowed roots.
- Before applying a patch, create a Git snapshot or branch.
- After applying, return changed files and diff summary.

Preferred flow:

```text
read files
propose patch
inspect patch
apply patch
run checks if available
show final diff
```

### 4.4 Coding Workspace Interactions

Purpose: let ChatGPT or Codex perform real multi-file coding work inside a repo-scoped sandbox.

Example user requests:

```text
Add tests for this module.
Refactor this CLI command.
Build the first version of the MCP server.
Fix the failing pytest suite.
Add a new tool for recipe search.
```

Tools for coding phase:

```text
list_repos()
select_repo(repo_id)
repo_tree(repo_id, glob?)
search_code(repo_id, query)
read_code_files(repo_id, paths)
propose_code_patch(repo_id, instructions, files?)
apply_code_patch(repo_id, patch_id)
run_repo_command(repo_id, command_name)
get_repo_status(repo_id)
get_repo_diff(repo_id)
create_branch(repo_id, branch_name)
commit_changes(repo_id, message)
```

Allowed command mapping should be configured per repo:

```json
{
  "test": "pytest",
  "lint": "ruff check .",
  "format_check": "ruff format --check .",
  "typecheck": "mypy .",
  "build": "npm run build"
}
```

Do not expose:

```text
run_shell(command)
run_python(code)
install_any_dependency(package)
delete_any_file(path)
edit_any_file(path, body)
```

Rules:

- Commands must be allowlisted by repo.
- Commands must have timeouts.
- Commands should run as a low-permission service user.
- Network access should be disabled by default for tests.
- Dependency installation requires explicit confirmation.
- Before multi-file edits, create a branch or snapshot.
- After edits, run tests/checks where available.
- Always return final diff and test results.

### 4.5 Agent Interactions

Purpose: allow the Mac mini to run local background workers that maintain data, summarize changes, index files, or perform bounded analysis.

Agents sit behind MCP. The MCP tool should trigger bounded jobs; agents should not become the external interface themselves.

Example pattern:

```text
ChatGPT calls: run_recipe_indexer()

Mac mini agent:
  - scans recipe folder
  - extracts ingredients
  - updates recipe database
  - creates embeddings
  - reports what changed
```

Tools for later phases:

```text
run_agent(agent_name, input)
list_agents()
get_agent_status(run_id)
get_agent_result(run_id)
cancel_agent_run(run_id)
```

Initial agent candidates:

```text
recipe_indexer
note_tagger
inbox_summarizer
embedding_updater
baking_log_analyzer
codebase_summarizer
```

Rules:

- Agents should be boring and bounded.
- Agents should not modify source files unless their contract explicitly says so.
- Prefer agents that produce reports or proposed changes.
- Background jobs should log inputs, outputs, duration, and touched files.
- Long-running jobs should be resumable or cancellable.
- No autonomous deletion.
- No autonomous spending, messaging, emailing, or external posting.

### 4.6 Local Inference Interactions

Purpose: make it easy to transition from ChatGPT-only use to hybrid or local-first inference.

Initial local model use cases:

- tagging
- summarization
- metadata extraction
- ingredient extraction
- duplicate detection
- embedding generation
- simple Q&A over private docs
- routine classification

Local inference should not initially handle:

- major architectural decisions
- security-sensitive edits
- financial decisions
- legal interpretation
- health recommendations
- autonomous coding merges

Possible local backends:

```text
Ollama
llama.cpp
vLLM
Open WebUI
LocalAI
```

Design rule:

> Local inference should call the same MCP tools as ChatGPT wherever possible.

This keeps the system model-agnostic.

## 5. Data Domains

### 5.1 Recipes and Baking Logs

Primary value: preserve cooking knowledge and make it reusable.

Entities:

```text
recipes
recipe_attempts
ingredients
equipment
timelines
plating_notes
failure_notes
next_time_notes
shopping_lists
```

Useful tools:

```text
search_recipes(query)
get_recipe(recipe_id)
create_recipe(title, body, metadata)
append_recipe_attempt(recipe_id, notes, outcome, next_time)
compare_recipe_attempts(recipe_id)
generate_grocery_list(recipe_id, servings)
```

Important metadata:

```text
vegetarian: true/false
contains_oats: true/false
contains_mushrooms: true/false
difficulty
make_ahead_level
date_last_made
rating
partner_reaction
```

### 5.2 Household and Project Memory

Primary value: shared operational memory for household projects, moving, maintenance, and creative plans.

Entities:

```text
household_projects
maintenance_logs
shopping_lists
room_plans
inventory
manuals
warranties
cleaning_routines
```

Useful tools:

```text
create_household_project(title, body)
append_household_log(project_id, entry)
search_household_docs(query)
create_checklist(title, items)
update_project_status(project_id, status)
```

### 5.3 Crafts and Studio Station

Primary value: support a craft/project station, patterns, materials, measurements, and work-in-progress notes.

Entities:

```text
craft_projects
sewing_patterns
crochet_patterns
materials
measurements
project_attempts
design_boards
```

Useful tools:

```text
create_craft_project(title, body)
append_craft_notes(project_id, entry)
search_patterns(query)
save_material_note(project_id, material, details)
```

### 5.4 Coding Projects

Primary value: let Codex and ChatGPT work on local repos safely.

Entities:

```text
repos
branches
patches
test_results
architecture_notes
task_plans
```

Useful tools:

```text
select_repo(repo_id)
search_code(query)
read_code_files(paths)
propose_code_patch(instructions)
apply_code_patch(patch_id)
run_tests()
get_diff()
commit_changes(message)
```

### 5.5 Personal Knowledge and Documents

Initial scope:

```text
non-sensitive notes
project docs
recipe docs
public/reference PDFs
manuals
learning notes
portfolio materials
```

Explicitly out of early scope:

```text
passwords
tax documents
banking documents
medical records
private legal records
Devyn's private documents
raw personal messages
```

## 6. Permission Levels

Use explicit permission tiers.

| Level | Name | Capability |
| --- | --- | --- |
| 0 | No access | Tool exists but is disabled. |
| 1 | Metadata only | Can list filenames, sizes, modified dates, and tags. |
| 2 | Read-only content | Can read and search content in allowlisted roots. |
| 3 | Create-only | Can create new files or notes, but cannot edit existing files. |
| 4 | Append-only | Can append logs or notes to approved files. |
| 5 | Patch-propose | Can generate patches but cannot apply them. |
| 6 | Patch-apply with confirmation | Can apply approved patches. |
| 7 | Command execution with allowlist | Can run approved tests/builds/linters only. |
| 8 | Autonomous bounded maintenance | Can run approved agents that perform bounded recurring tasks. |

Avoid any Level 9 equivalent that allows arbitrary shell or unrestricted filesystem access.

## 7. Confirmation Policy

Require confirmation for:

- overwriting files
- deleting files
- moving files
- renaming many files
- applying code patches
- committing changes
- installing dependencies
- running commands with network access
- changing MCP permissions
- exposing new directories
- sending data to external services
- any action involving private or sensitive data

Do not require confirmation for:

- read-only search
- reading explicitly selected files
- creating a new note in an approved inbox
- appending to an approved log
- running safe metadata extraction
- running local-only read-only indexing

For destructive actions, require:

```text
dry-run preview
explicit approval
rollback path
```

## 8. Logging and Audit

Every tool call should log:

```text
timestamp
client identity
tool name
arguments summary
allowed root/repo
files read
files written
command run
duration
success/failure
error message
diff/patch id if applicable
```

Logs should avoid storing full sensitive contents unless necessary.

Suggested audit directory:

```text
~/HomeBrain/.audit/
  tool_calls.log
  patches/
  snapshots/
  agent_runs/
```

## 9. Versioning and Rollback

Every writable root should be versioned.

Preferred:

```bash
cd ~/HomeBrain
git init
```

Before write operations:

```bash
git status
git add .
git commit -m "snapshot before MCP write"
```

After write operations:

```bash
git add .
git commit -m "MCP: <short action summary>"
```

At minimum, create timestamped backups for edited files.

Useful rollback tools:

```text
get_recent_changes(root_id)
revert_change(change_id)
restore_file_version(file_id, version_id)
```

## 10. Tool Design Guidelines

### 10.1 Tools Should Be Narrow

Bad:

```text
write_file(path, content)
run_shell(command)
```

Good:

```text
create_recipe_note(title, body, metadata)
append_baking_log(recipe_id, entry)
propose_code_patch(repo_id, instructions, files)
run_repo_command(repo_id, command_name)
```

### 10.2 Tools Should Be Typed

Every tool should have a strict schema.

Good:

```json
{
  "recipe_id": "string",
  "outcome": "excellent | good | mixed | failed",
  "notes": "string",
  "next_time": "string"
}
```

Bad:

```json
{
  "stuff": "anything"
}
```

### 10.3 Tools Should Return Structured Results

Good return shape:

```json
{
  "ok": true,
  "summary": "Appended baking log entry.",
  "files_changed": ["baking_logs/creme-brulee.md"],
  "change_id": "2026-07-06-abc123"
}
```

### 10.4 Tools Should Fail Safely

A failed tool call should not partially corrupt state.

Preferred behavior:

```text
validate
prepare
snapshot
execute
verify
commit
return result
```

### 10.5 Tools Should Be Composable

A good interaction should be possible as a sequence of simple calls:

```text
search
read
propose
apply
verify
```

Avoid giant tools that try to do everything.

## 11. Capability Expansion Checklist

Before adding a new tool, answer:

```text
What exact user request does this enable?
What directory/data can it touch?
Is it read, create, append, edit, delete, or execute?
Can it be made narrower?
Does it need confirmation?
Can it be rolled back?
What should it log?
What are the worst plausible failures?
Does it expose secrets?
Does it send data to an external service?
Can local inference run it safely?
Should ChatGPT, Codex, and local agents all have access?
```

Only add the tool if the safe version is still useful.

## 12. Suggested MVP Build Plan

### MVP 1: Local Repo and Note Server

Build a local MCP server with:

```text
list_allowed_roots
list_files
read_file
search_files
create_markdown_note
append_markdown_log
```

Storage:

```text
~/HomeBrain/
```

Requirements:

- TypeScript or Python.
- HTTPS-ready deployment.
- Token-based auth initially.
- Path allowlisting.
- Audit logs.
- Basic tests.
- No arbitrary command execution.
- No delete capability.

### MVP 2: Git-Backed Writes

Add:

```text
get_git_status
snapshot_root
get_git_diff
revert_last_change
```

Every write should snapshot or commit.

### MVP 3: Coding Workspace Tools

Add:

```text
list_repos
repo_tree
search_code
read_code_files
propose_code_patch
apply_code_patch
run_repo_command
get_repo_diff
```

Start with one repo:

```text
~/AIWorkspaces/home-mcp/
```

Allowlisted commands only:

```text
test
lint
typecheck
build
```

### MVP 4: Recipe Domain Tools

Add structured recipe functions:

```text
search_recipes
get_recipe
create_recipe
append_recipe_attempt
compare_recipe_attempts
```

Store recipe metadata in SQLite and recipe bodies as Markdown.

### MVP 5: Local Inference Layer

Add local model integration for:

```text
summarize_file
extract_recipe_metadata
tag_note
generate_embedding
detect_duplicate_notes
```

Do not use local inference for autonomous edits yet.

## 13. Recommended Tech Stack

### Server

Preferred options:

```text
TypeScript + MCP SDK
Python + FastAPI/MCP SDK
```

TypeScript is probably the cleaner first choice if the project will follow OpenAI Apps SDK patterns and be worked on heavily in Codex.

### Storage

```text
Markdown files for human-readable notes
SQLite for structured metadata
Git for versioning
Qdrant or Chroma for vector search later
Nextcloud for broader file sync later
```

### Hosting

```text
Mac mini
Cloudflare Tunnel
launchd service on macOS
local firewall
separate low-permission service user
```

### Local Inference Later

```text
Ollama
llama.cpp
Open WebUI
Qdrant/Chroma embeddings
```

## 14. Security Rules

Hard rules:

```text
No arbitrary shell.
No whole-home-directory access.
No secret-file access.
No destructive action without confirmation.
No delete tool in MVP.
No dependency install without confirmation.
No public unauthenticated endpoint.
No write access outside allowlisted roots.
No hidden autonomous background modifications.
No private Devyn data unless explicitly scoped later.
```

Soft rules:

```text
Prefer append over edit.
Prefer patch over overwrite.
Prefer local processing over external upload.
Prefer structured metadata over loose text.
Prefer small tools over giant tools.
Prefer read-only first.
```

## 15. Ideal End State

The mature system should allow requests like:

```text
Look through the bread monitor repo, fix the failing tests, and show me the diff.

Save this dinner as a recipe, append tonight's outcome, and make a next-time checklist.

Search all craft projects for anything involving linen and summarize unfinished work.

Run the recipe indexer and tell me what changed.

Compare my last three focaccia attempts and suggest the next experiment.

Create a new project folder for the kitchen camera system, add a README, and scaffold the first Python package.

Use the local model to tag all new notes from the inbox, but do not modify originals.
```

The system should make these possible without giving AI general access to the Mac mini.

## 16. Core Philosophy

This is not a remote-control interface for a computer.

It is a personal operating layer:

```text
controlled data
controlled tools
controlled actions
reversible changes
replaceable models
```

Build the boring, safe interface first. Once the interface is reliable, local agents and self-hosted inference become straightforward additions instead of fragile hacks.

