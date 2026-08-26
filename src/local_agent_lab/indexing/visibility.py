from __future__ import annotations

import fnmatch
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..security.paths import PathPolicyError, normalize_relative_path


DEFAULT_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
        ".mypy_cache",
        ".pytest_cache",
    }
)
DEFAULT_EXCLUDED_PATH_PREFIXES = frozenset({"data/indexes", "data/logs"})

# These defaults intentionally favor omission over guessing whether a credential-like
# file is safe. Explicit ignored-file allowances never override this denylist.
DEFAULT_SECRET_BASENAMES = frozenset(
    {
        ".dockercfg",
        ".env",
        ".envrc",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "access-token.txt",
        "credentials",
        "credentials.ini",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "private_key.txt",
        "secrets",
        "secrets.txt",
    }
)
DEFAULT_SECRET_GLOBS = (
    ".env.*",
    "*.jks",
    "*.kdbx",
    "*.key",
    "*.keystore",
    "*.ovpn",
    "*.p12",
    "*.p8",
    "*.pem",
    "*.pfx",
    "*.ppk",
    "access-token.*",
    "access_token.*",
    "auth-token.*",
    "auth_token.*",
    "credentials.cfg",
    "credentials.conf",
    "credentials.ini",
    "credentials.properties",
    "id_dsa.*",
    "id_ecdsa.*",
    "id_ed25519.*",
    "id_rsa.*",
    "private-key.*",
    "private_key.*",
    "secrets.cfg",
    "secrets.conf",
    "secrets.ini",
    "secrets.properties",
    "*credential*.json",
    "*credential*.cfg",
    "*credential*.conf",
    "*credential*.ini",
    "*credential*.properties",
    "*credential*.toml",
    "*credential*.yaml",
    "*credential*.yml",
    "*secret*.json",
    "*secret*.cfg",
    "*secret*.conf",
    "*secret*.ini",
    "*secret*.properties",
    "*secret*.toml",
    "*secret*.yaml",
    "*secret*.yml",
    "*service-account*.json",
    "*token*.json",
    "*token*.cfg",
    "*token*.conf",
    "*token*.ini",
    "*token*.properties",
    "*token*.toml",
    "*token*.yaml",
    "*token*.yml",
)
DEFAULT_SECRET_PATH_GLOBS = (
    ".aws/*",
    ".config/gcloud/*",
    ".kube/*",
    ".ssh/*",
    "config/*credential*",
    "config/*secret*",
    "config/*token*",
)
DEFAULT_PRIVATE_EXPORT_MARKERS = frozenset(
    {
        "library_files.json",
        "message_feedback.json",
        "shared_conversations.json",
        "user.json",
    }
)


class VisibilityPolicyError(RuntimeError):
    pass


@dataclass(frozen=True)
class VisibilityDecision:
    path: Path
    relative_path: str | None
    visible: bool
    reason: str


@dataclass(frozen=True)
class VisibilityScan:
    visible_files: tuple[Path, ...]
    decisions: tuple[VisibilityDecision, ...]

    @property
    def skipped_files(self) -> int:
        return sum(1 for decision in self.decisions if not decision.visible)


class RepositoryVisibilityPolicy:
    """Fail-closed visibility rules for files that may enter a repository index."""

    def __init__(
        self,
        root: str | Path,
        *,
        allow_ignored_patterns: Iterable[str] = (),
        excluded_dirs: Iterable[str] = DEFAULT_EXCLUDED_DIRS,
        excluded_path_prefixes: Iterable[str] = DEFAULT_EXCLUDED_PATH_PREFIXES,
        secret_basenames: Iterable[str] = DEFAULT_SECRET_BASENAMES,
        secret_globs: Iterable[str] = DEFAULT_SECRET_GLOBS,
        secret_path_globs: Iterable[str] = DEFAULT_SECRET_PATH_GLOBS,
        private_export_markers: Iterable[str] = DEFAULT_PRIVATE_EXPORT_MARKERS,
    ) -> None:
        candidate_root = Path(root)
        if candidate_root.is_symlink():
            raise VisibilityPolicyError("repository root cannot be a symlink")
        try:
            self.root = candidate_root.resolve(strict=True)
        except OSError as exc:
            raise VisibilityPolicyError(f"repository root is unavailable: {candidate_root}") from exc
        if not self.root.is_dir():
            raise VisibilityPolicyError(f"repository root is not a directory: {self.root}")

        self.allow_ignored_patterns = tuple(_normalize_pattern(item) for item in allow_ignored_patterns)
        self.excluded_dirs = frozenset(str(item) for item in excluded_dirs)
        self.excluded_path_prefixes = frozenset(_normalize_pattern(item) for item in excluded_path_prefixes)
        self.secret_basenames = frozenset(str(item).casefold() for item in secret_basenames)
        self.secret_globs = tuple(_normalize_pattern(item).casefold() for item in secret_globs)
        self.secret_path_globs = tuple(_normalize_pattern(item).casefold() for item in secret_path_globs)
        self.private_export_markers = frozenset(str(item).casefold() for item in private_export_markers)
        if self._looks_like_private_export(self.root):
            raise VisibilityPolicyError("repository root appears to be a private ChatGPT export")

    def scan(self) -> VisibilityScan:
        decisions: list[VisibilityDecision] = []
        candidates: list[Path] = []

        for current_root, dirnames, filenames in os.walk(self.root, followlinks=False):
            current = Path(current_root)
            kept_dirs: list[str] = []
            for dirname in dirnames:
                directory = current / dirname
                if (
                    dirname in self.excluded_dirs
                    or directory.is_symlink()
                    or self._looks_like_private_export(directory)
                ):
                    continue
                kept_dirs.append(dirname)
            dirnames[:] = kept_dirs

            for filename in filenames:
                path = current / filename
                decision = self.evaluate(path)
                if decision.visible:
                    candidates.append(path)
                else:
                    decisions.append(decision)

        ignored_paths = self._git_ignored_paths(candidates)
        visible_files: list[Path] = []
        for path in candidates:
            relative = path.relative_to(self.root).as_posix()
            if relative in ignored_paths and not self._explicitly_allows_ignored(relative):
                decisions.append(VisibilityDecision(path, relative, False, "gitignored"))
                continue
            decisions.append(VisibilityDecision(path, relative, True, "visible"))
            visible_files.append(path)

        decisions.sort(key=lambda item: item.relative_path or str(item.path))
        visible_files.sort(key=lambda item: item.relative_to(self.root).as_posix())
        return VisibilityScan(tuple(visible_files), tuple(decisions))

    def evaluate(self, path: str | Path) -> VisibilityDecision:
        candidate = Path(path)
        try:
            relative = candidate.relative_to(self.root)
        except ValueError:
            return VisibilityDecision(candidate, None, False, "outside_root")
        relative_path = relative.as_posix()

        if any(part in self.excluded_dirs for part in relative.parts):
            return VisibilityDecision(candidate, relative_path, False, "excluded_directory")
        if any(
            relative_path == prefix or relative_path.startswith(f"{prefix}/") for prefix in self.excluded_path_prefixes
        ):
            return VisibilityDecision(candidate, relative_path, False, "excluded_path")
        try:
            metadata = candidate.lstat()
        except OSError:
            return VisibilityDecision(candidate, relative_path, False, "unavailable")
        if stat.S_ISLNK(metadata.st_mode):
            return VisibilityDecision(candidate, relative_path, False, "symlink")
        if not stat.S_ISREG(metadata.st_mode):
            return VisibilityDecision(candidate, relative_path, False, "not_regular_file")
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self.root)
        except (OSError, ValueError):
            return VisibilityDecision(candidate, relative_path, False, "resolved_outside_root")
        if self._is_secret_path(relative):
            return VisibilityDecision(candidate, relative_path, False, "secret_denylist")
        return VisibilityDecision(candidate, relative_path, True, "visible")

    def evaluate_logical_path(self, relative_path: str) -> VisibilityDecision:
        """Evaluate an existing or deleted repository path before prompt use."""
        try:
            normalized = normalize_relative_path(relative_path)
        except PathPolicyError:
            return VisibilityDecision(self.root / relative_path, None, False, "invalid_path")
        relative = Path(normalized)
        candidate = self.root / relative
        if any(part in self.excluded_dirs for part in relative.parts):
            return VisibilityDecision(candidate, normalized, False, "excluded_directory")
        if any(normalized == prefix or normalized.startswith(f"{prefix}/") for prefix in self.excluded_path_prefixes):
            return VisibilityDecision(candidate, normalized, False, "excluded_path")
        if self._is_secret_path(relative):
            return VisibilityDecision(candidate, normalized, False, "secret_denylist")
        if normalized in self._git_ignored_paths([candidate]) and not self._explicitly_allows_ignored(normalized):
            return VisibilityDecision(candidate, normalized, False, "gitignored")
        return VisibilityDecision(candidate, normalized, True, "visible_logical_path")

    def read_bytes(self, path: str | Path, *, max_bytes: int) -> bytes | None:
        decision = self.evaluate(path)
        if not decision.visible or decision.relative_path is None:
            raise VisibilityPolicyError(f"refusing repository read: {decision.reason}")
        if os.open not in os.supports_dir_fd:
            raise VisibilityPolicyError("descriptor-relative safe reads are unavailable")

        parts = Path(decision.relative_path).parts
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptors: list[int] = []
        try:
            current_fd = os.open(self.root, directory_flags)
            descriptors.append(current_fd)
            for part in parts[:-1]:
                current_fd = os.open(part, directory_flags, dir_fd=current_fd)
                descriptors.append(current_fd)
            file_fd = os.open(parts[-1], file_flags, dir_fd=current_fd)
            descriptors.append(file_fd)
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise VisibilityPolicyError("refusing repository read: not_regular_file")
            if metadata.st_size > max_bytes:
                return None
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(file_fd, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            return None if len(payload) > max_bytes else payload
        except OSError as exc:
            raise VisibilityPolicyError(f"safe repository read failed: {decision.relative_path}") from exc
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _is_secret_path(self, relative: Path) -> bool:
        relative_path = relative.as_posix().casefold()
        basename = relative.name.casefold()
        return (
            basename in self.secret_basenames
            or any(fnmatch.fnmatchcase(basename, pattern) for pattern in self.secret_globs)
            or any(fnmatch.fnmatchcase(relative_path, pattern) for pattern in self.secret_path_globs)
        )

    def _explicitly_allows_ignored(self, relative_path: str) -> bool:
        return any(fnmatch.fnmatchcase(relative_path, pattern) for pattern in self.allow_ignored_patterns)

    def _looks_like_private_export(self, directory: Path) -> bool:
        try:
            names = {entry.name.casefold() for entry in directory.iterdir() if not entry.is_symlink()}
        except OSError:
            return True
        has_conversations = any(
            name == "conversations.json" or (name.startswith("conversations-") and name.endswith(".json"))
            for name in names
        )
        return has_conversations and bool(names & self.private_export_markers)

    def _git_ignored_paths(self, paths: Iterable[Path]) -> set[str]:
        path_list = list(paths)
        relative_paths = [path.relative_to(self.root).as_posix() for path in path_list]
        if not relative_paths:
            return set()

        has_ignore_rules = any(path.name == ".gitignore" for path in path_list)
        try:
            worktree = subprocess.run(
                ["git", "-C", str(self.root), "rev-parse", "--is-inside-work-tree"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError as exc:
            if has_ignore_rules:
                raise VisibilityPolicyError("cannot safely evaluate .gitignore without Git") from exc
            return set()
        if worktree.returncode != 0:
            if has_ignore_rules:
                raise VisibilityPolicyError("cannot safely evaluate .gitignore outside a Git worktree")
            return set()

        return self._git_ignored_relative_paths(relative_paths)

    def _git_ignored_relative_paths(self, relative_paths: Iterable[str]) -> set[str]:
        relative_path_list = list(relative_paths)
        if not relative_path_list:
            return set()
        payload = b"".join(os.fsencode(path) + b"\0" for path in relative_path_list)
        try:
            result = subprocess.run(
                ["git", "-C", str(self.root), "check-ignore", "--no-index", "-z", "--stdin"],
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as exc:
            raise VisibilityPolicyError("git ignore evaluation failed") from exc
        if result.returncode not in {0, 1}:
            raise VisibilityPolicyError("git ignore evaluation failed")
        return {os.fsdecode(item) for item in result.stdout.split(b"\0") if item}


def _normalize_pattern(value: str) -> str:
    return str(value).strip().replace("\\", "/").strip("/")
