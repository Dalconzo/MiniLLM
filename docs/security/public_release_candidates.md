# Public Release Candidate Boundary

`scripts/create_public_release_candidate.py` creates an offline public-release
candidate without rewriting or copying the private development repository's Git
history. It is intentionally a release gate, not a publishing command.

## Creation

Start from a clean source worktree and identify the exact reviewed commit. Keep a
local UTF-8 file outside the repository with one private corpus canary per line.
Canaries must be at least eight bytes and are hashed, but never copied, into the
release manifest.

```bash
uv run --frozen python scripts/create_public_release_candidate.py \
  --source-repo . \
  --source-commit <reviewed-commit> \
  --output-dir ../minillm-public-candidate \
  --forbidden-literals-file ../minillm-private-release-canaries.txt
```

The command exports approved paths from the committed Git tree into a temporary
sibling directory, scans them, creates one parentless commit with a neutral
identity, scans every object in the resulting Git object database, and atomically
moves the candidate into place. It does not use a remote or publish anything.

The sibling `<output>.release-manifest.json` binds:

- the source commit and tree;
- the parentless output commit and tree;
- every output path, mode, size, and SHA-256 digest;
- the lockfile digest;
- scan checks and hashed corpus-canary inputs; and
- aggregate exclusion counts without disclosing excluded private paths.

## Fail-closed policy

Only project metadata and files beneath `.github`, `config`, `docs`, `examples`,
`scripts`, `src`, and `tests` are eligible. Data, exports, test results, runtime
state, Beads metadata, caches, internal browser/private-memory procedures,
hardware side missions, stale local evidence, historical extension specs, and
unapproved root files are omitted. A tracked
secret-like path, symlink, submodule, encoded traversal path, mixed-separator
path, credential pattern, personal home path, browser/run identifier, or supplied
private corpus literal rejects the complete candidate.

Creation refuses a dirty source worktree. This prevents a reviewer from confusing
uncommitted local state with the tree that was actually exported.

## Private-corpus exact-overlap audit

The release-candidate scanner's explicit canaries cannot prove that an unlisted
span from a private ChatGPT export was not copied into public documentation,
fixtures, prompts, or source. Before clean-clone validation or publication, run
the independent exact-overlap gate against every relevant raw export:

```bash
uv run --frozen python scripts/audit_private_corpus_overlap.py \
  --export /path/to/older-export.zip \
  --export /path/to/newer-export-directory \
  --candidate-repo ../minillm-public-candidate \
  --report /private/path/minillm-overlap-audit.json
```

The auditor reads only raw `conversations*.json` documents, including documents
inside ZIP exports, and considers only user-authored message text. It creates no
parsed corpus, candidate-memory data, index, or temporary export artifact. The
default comparison uses normalized exact 20-word spans. The threshold may be
raised only within the conservative 20--40 word range; it cannot be lowered to a
short phrase that would create excessive false positives or raised enough to
make the gate ineffective.

The candidate must be a clean Git top-level worktree. The audit scans the
committed `HEAD` tree and all blob, commit, and tag content present in its Git
object database, rather than walking ignored caches, virtual environments, or
other untracked files. Absolute, mixed-separator, encoded-traversal, symlink, and
non-regular tracked paths fail closed. A match makes the command exit nonzero,
including when the overlapping blob is historical or unreachable from `HEAD`.

Console output and the optional report contain only the public candidate path,
candidate-side line locations where available, aggregate counts, commit/tree
bindings, input-file SHA-256 digests, and non-reversible per-run HMAC-SHA256 span
fingerprints. They never contain export paths, conversation titles or IDs,
author names, message IDs, or private message text. The optional report is
created as a new single-link mode-`0600` file and refuses existing or symlinked
destinations. Keep it outside the public repository.

Malformed schemas, invalid UTF-8 JSON, encrypted or malformed archives, unsafe
archive members, export symlinks, candidate dirtiness, and Git inspection errors
all fail closed with non-sensitive error codes. Passing this gate does not prove
semantic independence: paraphrases and spans shorter than the configured window
remain review concerns. It complements, rather than replaces, ownership review,
secret scanning, explicit private canaries, and manual inspection.

## Independent verification

The only mode allowed while the private source worktree is dirty is read-only
verification of an already-created candidate:

```bash
uv run --frozen python scripts/create_public_release_candidate.py \
  --source-repo . \
  --source-commit <reviewed-commit> \
  --output-dir ../minillm-public-candidate \
  --forbidden-literals-file ../minillm-private-release-canaries.txt \
  --check-only
```

Verification fails if the manifest changed, the candidate worktree is dirty,
history contains more than one commit, the commit has a parent, another ref
exists, unreachable objects are present, or any output path/content violates the
same release policy.

Ownership approval, dependency-license compatibility, clean-clone testing,
hosted CI, and publication remain separate human-reviewed gates.
