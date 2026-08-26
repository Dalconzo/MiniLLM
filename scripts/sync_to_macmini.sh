#!/usr/bin/env bash
set -euo pipefail

REMOTE="${MINILLM_REMOTE:-}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -z "$REMOTE" ]]; then
  printf 'ERROR set MINILLM_REMOTE to an rsync destination such as <host>:$HOME/MiniLLM/\n' >&2
  exit 64
fi

# Sync only tracked or explicitly unignored files. Git's ignore policy is the
# canonical boundary for private exports, runtime databases, logs, and secrets.
git -C "$ROOT_DIR" ls-files --cached --others --exclude-standard -z \
  | rsync -az --from0 --files-from=- "$ROOT_DIR/" "$REMOTE"
