# Browser ChatGPT Acceptance Questions

Use this document in normal browser ChatGPT with the MiniLLM/Home MCP app enabled.

Purpose:

- Verify the real ChatGPT app integration, not just local CLI behavior.
- Keep each test narrow enough that a failure points to a specific layer.
- Require returned run IDs, trace IDs, file IDs, source IDs, or error stages wherever available.
- Avoid private raw transcript disclosure in the test record.

Important tool-use rule:

- When a test names a tool in backticks, call that exact MiniLLM tool if it is available.
- Do not substitute generic file tools for memory tools unless the exact tool is unavailable.
- If an exact tool appears unavailable, first list the available MiniLLM tools and report that mismatch.

Before starting, run the Mac mini log watcher from another terminal:

```bash
ssh macmini-minillm 'cd /Users/daviddalconzo/MiniLLM && tail -f data/logs/watchers/browser_chatgpt_mcp_watch.log'
```

Refresh the ChatGPT app metadata before starting:

1. Open ChatGPT settings.
2. Go to Plugins/Apps or `chatgpt.com/plugins`.
3. Open the developer-mode MiniLLM/Home MCP app details.
4. Choose Refresh.
5. Start a new browser ChatGPT conversation after the refresh.

Expected current tool count:

- The local Mac mini endpoint currently advertises 30 tools.
- If browser ChatGPT only shows the old 10-tool subset, stop and refresh/recreate the developer-mode app before continuing.
- The old subset usually looks like `append_markdown_log`, `append_recipe_attempt`, `create_markdown_note`, `create_recipe`, `create_recipe_card`, `list_allowed_roots`, `list_files`, `read_file`, `search_files`, and `search_recipes`.

## Test 1: Tool Discovery And Status

Paste this into browser ChatGPT:

```text
Use the MiniLLM app.

Call `list_allowed_roots`, `tools/list` if available to you, and `memory_status`.

List the allowed roots, the tools you can see, and the current memory status.

Rules:
- Do not write files.
- Include any run_id, trace id, source id, or tool-call id returned by the system.
- Clearly label whether each root is writable or read-only.
- If `memory_status`, `memory_search`, `memory_subjects`, `memory_review_subjects`, `memory_context`, `memory_trace`, or `recipe_standard` are missing, report that as stale ChatGPT app metadata and stop the suite.
- If anything fails, report the failing stage and exact error.
```

## Test 2: Recipe Standard And Search

Paste this into browser ChatGPT:

```text
Use the MiniLLM app.

Call `recipe_standard`. Then call `search_recipes` for "miso butter".

Return:
- The recipe standard sections required by the system.
- Matching recipe file IDs.
- Ingredient count and step count for each likely match.
- Whether each match appears to follow the current recipe standard.
- Any run_id, trace id, or tool-call id returned by the system.

Rules:
- Do not create or edit anything.
- If the recipe has many ingredients and zero steps, call that out as a failure.
- If anything fails, report the failing stage and exact error.
```

## Test 3: Memory Status And Search

Paste this into browser ChatGPT:

```text
Use the MiniLLM app.

Call `memory_status`. Then call `memory_search` for "Home MCP memory tools".

Return:
- Memory system status.
- The top relevant results.
- Source IDs or memory IDs when available.
- Whether each result is raw transcript, curated memory, or synthesis.
- Any run_id, trace id, retrieval_event_id, or tool-call id returned by the system.

Rules:
- Do not expose long private transcript text.
- Prefer concise snippets and metadata.
- If anything fails, report the failing stage and exact error.
```

## Test 4: Subject Browsing

Paste this into browser ChatGPT:

```text
Use the MiniLLM app.

Call `memory_subjects`. Then call `memory_review_subjects` for the subject "Recipes and Baking".

Return:
- The available subject names.
- Candidate memory IDs or source IDs for "Recipes and Baking".
- Why each candidate belongs under that subject.
- Whether each item appears raw, curated, suggested, promoted, blocked, or tombstoned if that metadata is available.
- Any run_id, trace id, or tool-call id returned by the system.

Rules:
- Do not promote, edit, delete, or tombstone anything.
- If anything fails, report the failing stage and exact error.
```

## Test 5: Context Packet

Paste this into browser ChatGPT:

```text
Use the MiniLLM app.

Call `memory_context` to build a medium-depth memory context packet for:
"what should an agent know about my recipe and baking work?"

Return:
- The context items selected.
- Why each item was included.
- Whether each item came from raw transcript, curated memory, recipe notes, or synthesis.
- Any source IDs, memory IDs, run_id, trace id, retrieval_event_id, or tool-call id returned by the system.

Rules:
- Do not expose long private transcript text.
- Do not write files.
- If anything fails, report the failing stage and exact error.
```

## Test 6: Disposable Write Probe

Paste this into browser ChatGPT:

```text
Use the MiniLLM app.

Call `search_recipes` for "__browser_smoke_test_recipe_july_20_2026__".

If no matching recipe exists, create a minimal recipe card with:
- title: "__browser_smoke_test_recipe_july_20_2026__"
- ingredients: "water"
- steps: "verify write path"
- tags: "smoke-test", "browser-chatgpt"

Then search for it again and return:
- Created file ID.
- Root where it was written.
- Whether it follows the recipe standard.
- Any run_id, trace id, or tool-call id returned by the system.

Rules:
- Write only to the recipe book root.
- Do not overwrite an existing recipe.
- Do not create any other files.
- If anything fails, report the failing stage and exact error.
```

## Test 7: Safety Boundary

Paste this into browser ChatGPT:

```text
Use the MiniLLM app.

Call `read_file` with:
- root_id: "recipe_book"
- relative_path: "../archive/should-not-read.md"

Expected behavior:
- The request should be rejected.
- Do not try alternate bypass paths.
- Do not retry with path traversal.

Return:
- Whether the system rejected the request.
- The exact error code, error message, failure stage, or source reference if available.
- Any run_id, trace id, or tool-call id returned by the system.
```

## Test 8: Audit Trace Lookup

Paste this into browser ChatGPT after one or more previous tests have run:

```text
Use the MiniLLM app.

Call `memory_trace` for a run_id returned by one of the previous memory tests if one is available.

Then find or summarize the latest Home MCP or memory trace information from the last few tool calls.

Return:
- Recent run IDs or trace IDs.
- Tool names called.
- Whether each call succeeded or failed.
- Where logs or artifacts would be inspected from the Mac mini side if deeper debugging is needed.

Rules:
- Do not expose private transcript content.
- Do not write files.
- If direct trace lookup is not available as a tool, say that clearly and report what metadata you can see from the prior tool calls.
```
