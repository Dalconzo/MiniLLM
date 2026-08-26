# Filesystem Boundary Inventory

This inventory identifies paths influenced by users, models, external MCP clients,
configuration, and internal run IDs. A path is not considered safe merely because
it was checked earlier; final reads and writes must enforce the same root policy.

| Boundary | Input authority | Authorized root | Final mechanism | Tests/status |
| --- | --- | --- | --- | --- |
| `review --file` | CLI user or agent | selected repository | `safe_read_text` with strict relative path and no-follow descriptors | adversarial parser tests; broader CLI integration remains roadmap work |
| `write-function --spec` | CLI caller | selected repository only | lexical containment, `RepositoryVisibilityPolicy`, `.gitignore`/denylist decision, and descriptor-relative no-follow read before indexing or prompt construction | `tests/test_prompt_packing.py` |
| `write-tests --target` | CLI caller | selected repository | `RepositoryVisibilityPolicy` plus descriptor-relative no-follow read | `tests/test_prompt_packing.py`, `tests/test_patches.py` |
| `explain-log --file` | CLI caller | selected repository, or separately supplied `--log-root` when outside the repository | repository root takes precedence; lexical containment, visibility policy, and descriptor-relative no-follow read occur before parsing or prompt construction | `tests/test_prompt_packing.py` |
| generated implementation/test paths | local model output; exact destinations selected by CLI caller | selected repository; each nested relative destination must exactly match `--target` or `--test-target`, and its parent must already exist | `_validate_generated_files`, `build_unified_patch`, and `apply_files` independently enforce the same exact authorization; final `safe_write_text` reopens no-follow | `tests/test_patches.py` |
| existing final write/append target | filesystem state at final operation | canonical authorized root | open without truncation, inspect the opened inode, reject non-regular files and `st_nlink > 1`, then truncate or append through that descriptor | hard-link and symlink adversarial cases in `tests/test_patches.py` |
| generated patch artifact | internal command and run ID; content originates from model output | configured patches directory | fixed single-segment basename, immediate secret redaction, descriptor-relative no-follow write, hard-link rejection, and `0600` mode | `tests/test_patches.py` symlink, hard-link, nested-name, redaction, and mode cases |
| repository index files | repository input | selected repository | `RepositoryVisibilityPolicy.read_bytes` with Git ignore, denylist, containment, and no-follow descriptors | `tests/test_repo_indexer.py` |
| Home MCP file references | external MCP arguments | selected allowlisted root | `_resolve_safe_path` plus `safe_read_text`/`safe_write_text` | `tests/test_home_mcp.py` |
| Home MCP note folder and filename | external MCP arguments | writable typed root | strict folder resolution, slugged filename, final no-follow write; directories created descriptor-relative | `tests/test_home_mcp.py` |
| Home MCP recursive search/normalization | files already under typed root | selected allowlisted root | rejects symlink entries, proves resolved containment, reopens no-follow before read/write | `tests/test_home_mcp.py` |
| run artifacts | internal fixed artifact names | per-run directory | metadata-only sanitizer plus no-follow private-mode write | `tests/test_prompt_packing.py` |
| trace streams | internal trace writer | per-run directory | fixed filename and final payload sanitizer | `tests/test_memory_observability.py`, `tests/test_home_mcp.py` |
| memory/import databases and JSONL | operator configuration and deterministic IDs | configured data/memory roots | subsystem-specific fixed names; production root is identity-isolated | production and memory test suites |
| baking session/capture JSON metadata | device/session metadata | configured `baking_observations` root | descriptor-relative private directory creation; final secret redaction and no-follow/hard-link-safe `0600` writes | `tests/test_bake_cam.py` symlinked root/parent/final, hard-link, redaction, and mode cases |
| baking command trace JSONL | subprocess and device diagnostics | exact per-run directory | metadata-only payload sanitizer, final secret redaction, and descriptor-relative no-follow/hard-link-safe `0600` write | `tests/test_bake_cam.py` diagnostic omission, redaction, and mode cases |
| offline evaluation JSON/Markdown reports | deterministic evaluator output; exact directory selected by operator | existing explicit output directory | rejects missing/symlink roots; fixed filenames receive immediate secret redaction and descriptor-relative no-follow/hard-link-safe `0600` writes | `tests/test_memory_eval_v1.py` root/final symlink, hard-link, redaction, and mode cases |
| offline evaluation hostile raw export | deterministic synthetic fixture generator; fixed dataset-relative filename | random private directory below the explicit evaluation work root | descriptor-relative parent creation and no-follow/hard-link-safe `0600` write; canaries intentionally reach ingestion unredacted; entire raw tree is removed in `finally` even when ingestion fails | `tests/test_memory_eval_v1.py` parent/final symlink, hard-link, mode, canary, and failure-cleanup cases |
| legacy memory-eval raw export fixture | deterministic synthetic fixture generator; work-root basename selected by caller | existing parent of the requested work root | fixed `raw/eval-export/conversations.json` layout created descriptor-relatively; no-follow/hard-link-safe `0600` write | `tests/test_memory_eval_checks.py` work-root symlink, final hard-link, and mode cases; synthetic secret canary is intentionally retained in this hostile ingestion input and must be removed/redacted by downstream ingestion |
| Home MCP tunnel credential environment file | operator environment or explicit install argument; fixed destination | current user's home and private `.config/tunnel-client` directory | owned real parents only; group/other-writable parents rejected; credential directory tightened to `0700`; final descriptor-relative no-follow/hard-link-safe `0600` write and post-write inode validation | `tests/test_home_mcp_launchd.py` parent/final symlink, unsafe mode, hard-link, file mode, and directory mode cases |
| staging snapshots/releases/admin requests | administrator-reviewed manifests | disjoint staging, release, and control roots | trusted promotion/path policies, root ownership, immutable hashes | deployment/security suites and retained release evidence |

## Required review rule

New code that combines an untrusted string with a filesystem root must either use
the canonical path/visibility policy or document why its input is internally fixed.
Any new final persistence sink must be added to this inventory and to the security
test subset before release.
