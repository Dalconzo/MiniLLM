# Browser ChatGPT Acceptance Results

Use this file as a template for manual browser ChatGPT acceptance runs. Keep results sanitized. Do not paste raw private transcript content.

Run metadata:

- Date:
- Browser ChatGPT model:
- MiniLLM app enabled: yes/no
- Mac mini service status before run:
- Local smoke test before run:
- Memory eval before run:
- Log watcher running: yes/no
- Tester:

Preflight commands:

```bash
ssh macmini-minillm 'cd /Users/daviddalconzo/MiniLLM && .venv/bin/python -m local_agent_lab.cli home-mcp service-status'
ssh macmini-minillm 'cd /Users/daviddalconzo/MiniLLM && .venv/bin/python -m local_agent_lab.cli home-mcp smoke-test --url http://127.0.0.1:8765/mcp'
ssh macmini-minillm 'cd /Users/daviddalconzo/MiniLLM && .venv/bin/python -m local_agent_lab.cli memory-eval'
ssh macmini-minillm 'cd /Users/daviddalconzo/MiniLLM && tail -f data/logs/watchers/browser_chatgpt_mcp_watch.log'
```

## Test Results

### Test 1: Tool Discovery And Status

Status: not run

Observed:

- Roots:
- Tools:
- Memory status:
- Run IDs / trace IDs:
- Failure stage, if any:

Notes:

### Test 2: Recipe Standard And Search

Status: not run

Observed:

- Recipe standard returned:
- Matching recipe file IDs:
- Ingredient counts:
- Step counts:
- Run IDs / trace IDs:
- Failure stage, if any:

Notes:

### Test 3: Memory Status And Search

Status: not run

Observed:

- Memory status:
- Top result IDs:
- Source kinds:
- Retrieval event IDs / trace IDs:
- Failure stage, if any:

Notes:

### Test 4: Subject Browsing

Status: not run

Observed:

- Subject names:
- `Recipes and Baking` present:
- Candidate IDs:
- Lifecycle/status labels:
- Run IDs / trace IDs:
- Failure stage, if any:

Notes:

### Test 5: Context Packet

Status: not run

Observed:

- Context items:
- Source kinds:
- Inclusion reasons:
- Source IDs / retrieval event IDs / trace IDs:
- Failure stage, if any:

Notes:

### Test 6: Disposable Write Probe

Status: not run

Observed:

- Pre-write search result:
- Created file ID:
- Created root:
- Follow-up search result:
- Recipe standard compliance:
- Run IDs / trace IDs:
- Failure stage, if any:

Cleanup needed: yes/no

Notes:

### Test 7: Safety Boundary

Status: not run

Observed:

- Rejected: yes/no
- Error code/message/stage:
- Any unsafe content returned: yes/no
- Run IDs / trace IDs:

Notes:

### Test 8: Audit Trace Lookup

Status: not run

Observed:

- Recent run IDs:
- Recent tool names:
- Success/failure summary:
- Trace lookup available directly: yes/no
- Failure stage, if any:

Notes:

## Final Assessment

Overall status: not run

Passed tests:

Failed tests:

Blocked tests:

Issues to bead:

- 

Mac mini log cross-check:

- Tool calls observed in logs: yes/no
- Disposable write logged: yes/no
- Safety rejection logged: yes/no
- Suspicious private data in logs/results: yes/no

Follow-up commands:

```bash
ssh macmini-minillm 'cd /Users/daviddalconzo/MiniLLM && tail -n 240 data/logs/watchers/browser_chatgpt_mcp_watch.log'
```

