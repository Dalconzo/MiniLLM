# Repository Visibility Policy

Repository indexing is deny-by-default for files that are ignored, credential-like,
symlinked, non-regular, or resolved outside the requested repository root.

The canonical implementation is `local_agent_lab.indexing.visibility`.

## Defaults

The indexer:

- uses Git's own `check-ignore --no-index` behavior so root and nested `.gitignore`
  rules, including negations, have Git-compatible semantics;
- fails instead of indexing when `.gitignore` files exist but Git cannot evaluate
  them;
- rejects file symlinks and does not follow directory symlinks;
- verifies that every accepted path resolves beneath the repository root;
- opens accepted files component-by-component with descriptor-relative
  `O_NOFOLLOW` reads;
- excludes runtime/index/log directories and common generated dependency trees;
- excludes credential-bearing names and formats even when Git would include them;
- excludes directories shaped like private ChatGPT exports when conversation files
  and an export manifest occur together;
- rejects the entire scan when the requested repository root itself has the shape
  of a private ChatGPT export;
- rechecks current visibility before every indexed search or exact chunk fetch and
  transactionally removes newly ignored, newly denied, missing, or replaced paths
  from the file, chunk, and FTS tables before returning context.

The default secret denylist includes:

- `.env`, `.env.*`, and `.envrc`;
- `.netrc`, `.npmrc`, `.pypirc`, and `.dockercfg`;
- SSH private-key names such as `id_rsa` and `id_ed25519`;
- backup variants of standard private-key names, including `id_rsa.backup`;
- common text/config credential names such as `secrets.txt`, `credentials.ini`,
  `private_key.txt`, and `access-token.txt`;
- private-key, certificate-container, VPN, password-database, and Java-keystore
  formats such as `.pem`, `.key`, `.p8`, `.p12`, `.pfx`, `.ppk`, `.ovpn`,
  `.kdbx`, `.jks`, and `.keystore`;
- credential, secret, service-account, and token JSON/YAML/TOML names;
- files under `.ssh`, `.aws`, `.kube`, and `.config/gcloud`;
- credential, secret, and token files under `config`;
- ChatGPT export directories containing both `conversations*.json` and
  a known export manifest such as `library_files.json` or `user.json`, including
  their attachment files.

The generic config denylist is deliberately extension-limited. It rejects common
credential-bearing `.ini`, `.conf`, `.cfg`, and `.properties` files without
blanket-blocking normal source files such as `credential_parser.py`.

## Existing indexes

Visibility is enforced at retrieval as well as ingestion. Search and exact chunk
fetches rescan repository metadata using the current `.gitignore` and denylist,
then purge any no-longer-visible file and its FTS rows in the same SQLite
transaction. A stale index therefore cannot keep supplying content from a file
that was ignored, renamed to a denied credential name, deleted, or replaced by a
symlink after the original indexing run. Re-run `lagent index-repo` when newly
visible files or changed visible content need to be added; retrieval-time refresh
is intentionally removal-only.

## SQLite index outputs

The shared SQLite connection boundary rejects URI database paths, lexical parent
traversal, symlinked database files, symlinked existing parent components, and
non-regular existing targets before `sqlite3.connect` runs. Repository indexing
and ChatGPT ingestion use this boundary, including an initial validation before
creating a missing database parent.

## Explicit ignored-file allowances

`RepositoryVisibilityPolicy(..., allow_ignored_patterns=(...))` can preserve a
specific non-secret ignored fixture or generated source file. The default is no
allowance. An allowance cannot override the secret denylist, symlink policy,
excluded runtime paths, or root-containment checks.
