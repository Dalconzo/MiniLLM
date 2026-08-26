from __future__ import annotations

import errno
import os
import re
import stat
from collections.abc import Collection
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import unquote


class PathPolicyError(ValueError):
    """Raised when an untrusted path violates an authorized-root policy."""


_PERCENT_ESCAPE = re.compile(r"%[0-9a-fA-F]{2}")


def normalize_relative_path(value: str, *, allow_nested: bool = True) -> str:
    """Return a strict POSIX relative path or reject ambiguous path syntax."""
    raw = str(value).strip()
    if not raw or "\x00" in raw or "\n" in raw or "\r" in raw:
        raise PathPolicyError("path must be a non-empty single-line relative path")
    if "\\" in raw:
        raise PathPolicyError("backslash and mixed path separators are not allowed")
    if _PERCENT_ESCAPE.search(raw) or unquote(raw) != raw:
        raise PathPolicyError("percent-encoded path syntax is not allowed")

    posix = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if posix.is_absolute() or windows.is_absolute() or windows.drive or windows.root:
        raise PathPolicyError("absolute paths are not allowed")

    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PathPolicyError("empty, dot, and parent path segments are not allowed")
    if not allow_nested and len(parts) != 1:
        raise PathPolicyError("nested destinations are not allowed")
    return "/".join(parts)


def normalize_generated_destination(
    value: str,
    *,
    authorized_relative_paths: Collection[str] = (),
) -> str:
    """Validate a model-selected destination against exact caller authorization."""
    normalized = normalize_relative_path(value)
    if "/" not in normalized:
        return normalized

    authorized = {normalize_relative_path(path) for path in authorized_relative_paths}
    if normalized not in authorized:
        raise PathPolicyError("nested model-generated destination was not explicitly authorized")
    return normalized


def resolve_authorized_path(
    root: Path,
    relative_path: str,
    *,
    allow_nested: bool = True,
    require_exists: bool = False,
) -> Path:
    """Resolve a path and prove all existing components remain inside root."""
    normalized = normalize_relative_path(relative_path, allow_nested=allow_nested)
    if root.is_symlink():
        raise PathPolicyError("authorized root must not be a symlink")
    root_path = root.resolve(strict=True)
    if not root_path.is_dir():
        raise PathPolicyError("authorized root must be a real directory")

    candidate = root_path.joinpath(*normalized.split("/"))
    current = root_path
    for part in normalized.split("/"):
        current = current / part
        if current.exists() or current.is_symlink():
            if current.is_symlink():
                raise PathPolicyError(f"symlink path component is not allowed: {part}")
            try:
                current.resolve(strict=True).relative_to(root_path)
            except ValueError as exc:
                raise PathPolicyError("path escapes authorized root") from exc
    if require_exists and not candidate.exists():
        raise FileNotFoundError(candidate)
    try:
        candidate.resolve(strict=False).relative_to(root_path)
    except ValueError as exc:
        raise PathPolicyError("path escapes authorized root") from exc
    return candidate


def safe_read_text(root: Path, relative_path: str, *, encoding: str = "utf-8") -> str:
    """Read a regular file through no-follow directory descriptors."""
    parent_fd, name = _open_parent(root, relative_path, create_parents=False)
    try:
        flags = os.O_RDONLY | _flag("O_NOFOLLOW")
        file_fd = os.open(name, flags, dir_fd=parent_fd)
        try:
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise PathPolicyError("authorized read target must be a regular file")
            with os.fdopen(file_fd, "r", encoding=encoding, closefd=False) as handle:
                return handle.read()
        finally:
            os.close(file_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise PathPolicyError("symlink path component is not allowed") from exc
        raise
    finally:
        os.close(parent_fd)


def safe_write_text(
    root: Path,
    relative_path: str,
    content: str,
    *,
    encoding: str = "utf-8",
    create_parents: bool = False,
    mode: int = 0o644,
) -> Path:
    """Write a regular file at the final authorized boundary without following links."""
    normalized = normalize_relative_path(relative_path)
    parent_fd, name = _open_parent(root, normalized, create_parents=create_parents)
    try:
        flags = os.O_WRONLY | os.O_CREAT | _flag("O_NOFOLLOW")
        file_fd = os.open(name, flags, mode, dir_fd=parent_fd)
        try:
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise PathPolicyError("authorized write target must be a regular file")
            if metadata.st_nlink > 1:
                raise PathPolicyError("authorized write target must not have multiple hard links")
            os.ftruncate(file_fd, 0)
            os.fchmod(file_fd, mode)
            with os.fdopen(file_fd, "w", encoding=encoding, closefd=False) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(file_fd)
        finally:
            os.close(file_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise PathPolicyError("symlink path component is not allowed") from exc
        raise
    finally:
        os.close(parent_fd)
    return root.resolve(strict=True).joinpath(*normalized.split("/"))


def safe_append_text(
    root: Path,
    relative_path: str,
    content: str,
    *,
    encoding: str = "utf-8",
    mode: int = 0o600,
) -> Path:
    """Append to a regular file without following path components or the final target."""
    normalized = normalize_relative_path(relative_path)
    parent_fd, name = _open_parent(root, normalized, create_parents=False)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | _flag("O_NOFOLLOW")
        file_fd = os.open(name, flags, mode, dir_fd=parent_fd)
        try:
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise PathPolicyError("authorized append target must be a regular file")
            if metadata.st_nlink > 1:
                raise PathPolicyError("authorized append target must not have multiple hard links")
            os.fchmod(file_fd, mode)
            with os.fdopen(file_fd, "a", encoding=encoding, closefd=False) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(file_fd)
        finally:
            os.close(file_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise PathPolicyError("symlink path component is not allowed") from exc
        raise
    finally:
        os.close(parent_fd)
    return root.resolve(strict=True).joinpath(*normalized.split("/"))


def _open_parent(root: Path, relative_path: str, *, create_parents: bool) -> tuple[int, str]:
    normalized = normalize_relative_path(relative_path)
    parts = normalized.split("/")
    if root.is_symlink():
        raise PathPolicyError("authorized root must not be a symlink")
    root_path = root.resolve(strict=True)
    if not root_path.is_dir():
        raise PathPolicyError("authorized root must be a real directory")

    directory_flags = os.O_RDONLY | _flag("O_DIRECTORY") | _flag("O_NOFOLLOW")
    current_fd = os.open(root_path, directory_flags)
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create_parents:
                    raise PathPolicyError("model-generated parent directories must already exist")
                os.mkdir(part, mode=0o755, dir_fd=current_fd)
                next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise PathPolicyError("symlink path component is not allowed") from exc
                raise
            os.close(current_fd)
            current_fd = next_fd
        return current_fd, parts[-1]
    except Exception:
        os.close(current_fd)
        raise


def _flag(name: str) -> int:
    return int(getattr(os, name, 0))
