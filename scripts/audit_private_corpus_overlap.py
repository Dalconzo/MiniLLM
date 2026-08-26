#!/usr/bin/env python3
"""Fail closed when a public Git candidate exactly overlaps private ChatGPT text."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import sys
import unicodedata
import urllib.parse
import zipfile
from bisect import bisect_right
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn


SCHEMA_VERSION = 1
TOOL_VERSION = "1.0"
DEFAULT_NGRAM_WORDS = 20
MIN_NGRAM_WORDS = 20
MAX_NGRAM_WORDS = 40
MAX_EXPORT_DOCUMENT_BYTES = 512 * 1024 * 1024
MAX_EXPORT_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
CONVERSATION_FILE_RE = re.compile(r"^conversations(?:[-_.][^.]+)?\.json$", re.IGNORECASE)
WORD_RE = re.compile(r"[^\W_]+(?:['\u2019][^\W_]+)*", re.UNICODE)


class AuditError(RuntimeError):
    """An intentionally non-sensitive, fail-closed audit error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ExportDocument:
    data: bytes


@dataclass(frozen=True)
class ExportInputDigest:
    sha256: str
    conversation_documents: int


@dataclass(frozen=True)
class CandidateObject:
    object_id: str
    object_type: str
    data: bytes
    paths: tuple[str, ...]


@dataclass(frozen=True)
class Word:
    value: str
    line: int


def _safe_error(code: str) -> NoReturn:
    raise AuditError(code)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        _safe_error("export_input_unreadable")
    return digest.hexdigest()


def _fingerprint(key: bytes, value: str | bytes) -> str:
    return _fingerprint_digest(key, value).hex()


def _fingerprint_digest(key: bytes, value: str | bytes) -> bytes:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    return hmac.new(key, encoded, hashlib.sha256).digest()


def _git(repo: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError):
        _safe_error("candidate_git_error")
    return result.stdout


def _validate_git_path(path: str) -> PurePosixPath:
    if not path or path.startswith(("/", "\\")) or "\\" in path:
        _safe_error("candidate_unsafe_git_path")
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        _safe_error("candidate_unsafe_git_path")
    logical = PurePosixPath(path)
    if any(part in {"", ".", ".."} for part in logical.parts):
        _safe_error("candidate_unsafe_git_path")
    decoded = path
    for _iteration in range(4):
        next_value = urllib.parse.unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    decoded_path = PurePosixPath(decoded)
    if decoded.startswith(("/", "\\")) or "\\" in decoded:
        _safe_error("candidate_unsafe_git_path")
    if any(part in {"", ".", ".."} for part in decoded_path.parts):
        _safe_error("candidate_unsafe_git_path")
    return logical


def _resolve_candidate(path: Path) -> tuple[Path, str, str]:
    try:
        if path.is_symlink():
            _safe_error("candidate_symlink_rejected")
        candidate = path.resolve(strict=True)
    except OSError:
        _safe_error("candidate_unreadable")
    git_directory = candidate / ".git"
    if not candidate.is_dir() or git_directory.is_symlink() or not git_directory.is_dir():
        _safe_error("candidate_not_repository")
    top_level_raw = _git(candidate, "rev-parse", "--show-toplevel")
    try:
        top_level = Path(top_level_raw.decode("utf-8").strip()).resolve(strict=True)
    except (OSError, UnicodeDecodeError):
        _safe_error("candidate_not_repository")
    if top_level != candidate:
        _safe_error("candidate_not_git_top_level")
    if _git(candidate, "status", "--porcelain=v1", "--untracked-files=all").strip():
        _safe_error("candidate_dirty")
    try:
        commit = _git(candidate, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
        tree = _git(candidate, "rev-parse", "--verify", "HEAD^{tree}").decode("ascii").strip()
    except UnicodeDecodeError:
        _safe_error("candidate_git_error")
    return candidate, commit, tree


def _head_blob_paths(repo: Path) -> dict[str, tuple[str, ...]]:
    raw = _git(repo, "ls-tree", "-rz", "--full-tree", "HEAD")
    paths_by_object: dict[str, list[str]] = {}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        try:
            metadata, raw_path = entry.split(b"\t", 1)
            mode, object_type, object_id = metadata.decode("ascii").split()
            path = raw_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            _safe_error("candidate_invalid_tree")
        _validate_git_path(path)
        if object_type != "blob" or mode not in {"100644", "100755"}:
            _safe_error("candidate_non_regular_tracked_entry")
        paths_by_object.setdefault(object_id, []).append(path)
    return {object_id: tuple(sorted(paths)) for object_id, paths in paths_by_object.items()}


def _candidate_objects(repo: Path, head_paths: dict[str, tuple[str, ...]]) -> Iterator[CandidateObject]:
    inventory = _git(repo, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)")
    seen: set[str] = set()
    for raw_line in inventory.splitlines():
        try:
            object_id, object_type = raw_line.decode("ascii").split()
        except (ValueError, UnicodeDecodeError):
            _safe_error("candidate_invalid_object_inventory")
        if object_id in seen:
            continue
        seen.add(object_id)
        if object_type not in {"blob", "commit", "tag"}:
            continue
        data = _git(repo, "cat-file", object_type, object_id)
        yield CandidateObject(object_id, object_type, data, head_paths.get(object_id, ()))


def _safe_export_member_path(value: str) -> PurePosixPath:
    if not value or value.startswith(("/", "\\")) or "\\" in value:
        _safe_error("export_archive_unsafe_path")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _safe_error("export_archive_unsafe_path")
    logical = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in logical.parts):
        _safe_error("export_archive_unsafe_path")
    decoded = value
    for _iteration in range(4):
        next_value = urllib.parse.unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    decoded_path = PurePosixPath(decoded)
    if decoded.startswith(("/", "\\")) or "\\" in decoded:
        _safe_error("export_archive_unsafe_path")
    if any(part in {"", ".", ".."} for part in decoded_path.parts):
        _safe_error("export_archive_unsafe_path")
    return logical


def _read_regular_export_file(path: Path) -> bytes:
    try:
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            _safe_error("export_input_not_regular")
        if metadata.st_size > MAX_EXPORT_DOCUMENT_BYTES:
            _safe_error("export_document_too_large")
        return path.read_bytes()
    except OSError:
        _safe_error("export_input_unreadable")


def _directory_documents(root: Path) -> tuple[list[ExportDocument], ExportInputDigest]:
    selected: list[Path] = []
    try:
        for current, directories, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            for name in [*directories, *files]:
                if (current_path / name).is_symlink():
                    _safe_error("export_symlink_rejected")
            for name in files:
                if CONVERSATION_FILE_RE.fullmatch(name):
                    selected.append(current_path / name)
    except OSError:
        _safe_error("export_input_unreadable")
    if not selected:
        _safe_error("export_conversations_missing")
    documents: list[ExportDocument] = []
    document_digests: list[str] = []
    total_bytes = 0
    for path in sorted(selected):
        data = _read_regular_export_file(path)
        total_bytes += len(data)
        if total_bytes > MAX_EXPORT_TOTAL_BYTES:
            _safe_error("export_input_too_large")
        documents.append(ExportDocument(data))
        document_digests.append(_sha256(data))
    aggregate = hashlib.sha256()
    for digest in sorted(document_digests):
        aggregate.update(bytes.fromhex(digest))
    return documents, ExportInputDigest(aggregate.hexdigest(), len(documents))


def _zip_documents(path: Path) -> tuple[list[ExportDocument], ExportInputDigest]:
    documents: list[ExportDocument] = []
    seen_names: set[str] = set()
    total_bytes = 0
    try:
        with zipfile.ZipFile(path) as archive:
            selected: list[zipfile.ZipInfo] = []
            for info in archive.infolist():
                logical = _safe_export_member_path(info.filename)
                if info.filename in seen_names:
                    _safe_error("export_archive_duplicate_path")
                seen_names.add(info.filename)
                mode = (info.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    _safe_error("export_symlink_rejected")
                if info.flag_bits & 0x1:
                    _safe_error("export_archive_encrypted")
                if not info.is_dir() and CONVERSATION_FILE_RE.fullmatch(logical.name):
                    if info.file_size > MAX_EXPORT_DOCUMENT_BYTES:
                        _safe_error("export_document_too_large")
                    selected.append(info)
            if not selected:
                _safe_error("export_conversations_missing")
            for info in sorted(selected, key=lambda item: item.filename):
                data = archive.read(info)
                if len(data) != info.file_size:
                    _safe_error("export_archive_size_mismatch")
                total_bytes += len(data)
                if total_bytes > MAX_EXPORT_TOTAL_BYTES:
                    _safe_error("export_input_too_large")
                documents.append(ExportDocument(data))
    except AuditError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError):
        _safe_error("export_archive_invalid")
    return documents, ExportInputDigest(_sha256_file(path), len(documents))


def _load_export_input(path: Path) -> tuple[list[ExportDocument], ExportInputDigest]:
    try:
        if path.is_symlink():
            _safe_error("export_symlink_rejected")
        resolved = path.resolve(strict=True)
    except OSError:
        _safe_error("export_input_unreadable")
    if resolved.is_dir():
        return _directory_documents(resolved)
    if not resolved.is_file():
        _safe_error("export_input_not_regular")
    if resolved.suffix.casefold() == ".zip":
        return _zip_documents(resolved)
    data = _read_regular_export_file(resolved)
    return [ExportDocument(data)], ExportInputDigest(_sha256(data), 1)


def _conversation_list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("conversations"), list):
        return payload["conversations"]
    if isinstance(payload, dict) and isinstance(payload.get("mapping"), dict):
        return [payload]
    _safe_error("export_schema_invalid")


def _user_messages(document: ExportDocument) -> tuple[list[str], int]:
    try:
        payload = json.loads(document.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _safe_error("export_json_invalid")
    conversations = _conversation_list(payload)
    messages: list[str] = []
    for conversation in conversations:
        if not isinstance(conversation, dict) or not isinstance(conversation.get("mapping"), dict):
            _safe_error("export_schema_invalid")
        for node in conversation["mapping"].values():
            if not isinstance(node, dict):
                _safe_error("export_schema_invalid")
            message = node.get("message")
            if message is None:
                continue
            if not isinstance(message, dict):
                _safe_error("export_schema_invalid")
            author = message.get("author")
            content = message.get("content")
            if not isinstance(author, dict) or not isinstance(content, dict):
                _safe_error("export_schema_invalid")
            role = author.get("role")
            if not isinstance(role, str):
                _safe_error("export_schema_invalid")
            if role != "user":
                continue
            extracted: list[str] = []
            parts = content.get("parts")
            if parts is not None:
                if not isinstance(parts, list):
                    _safe_error("export_schema_invalid")
                for part in parts:
                    if isinstance(part, str):
                        extracted.append(part)
                    elif isinstance(part, dict) and isinstance(part.get("text"), str):
                        extracted.append(part["text"])
                    elif part is not None and not isinstance(part, dict):
                        _safe_error("export_schema_invalid")
            text = content.get("text")
            if text is not None:
                if not isinstance(text, str):
                    _safe_error("export_schema_invalid")
                extracted.append(text)
            if extracted:
                messages.append("\n".join(extracted))
    return messages, len(conversations)


def _normalized_words(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return [match.group(0).replace("\u2019", "'") for match in WORD_RE.finditer(normalized)]


def _word_locations(text: str) -> list[Word]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    newline_offsets = [index for index, character in enumerate(normalized) if character == "\n"]
    return [
        Word(match.group(0).replace("\u2019", "'"), bisect_right(newline_offsets, match.start()) + 1)
        for match in WORD_RE.finditer(normalized)
    ]


def _windows(values: Sequence[str], size: int) -> Iterator[tuple[int, str]]:
    for index in range(len(values) - size + 1):
        yield index, " ".join(values[index : index + size])


def _private_fingerprints(
    export_paths: Sequence[Path],
    ngram_words: int,
    key: bytes,
) -> tuple[set[bytes], list[ExportInputDigest], dict[str, int]]:
    fingerprints: set[bytes] = set()
    input_digests: list[ExportInputDigest] = []
    counts = {
        "export_inputs": len(export_paths),
        "conversation_documents": 0,
        "conversations": 0,
        "user_messages": 0,
        "eligible_user_messages": 0,
        "private_ngram_windows": 0,
        "unique_private_ngram_fingerprints": 0,
    }
    for path in export_paths:
        documents, input_digest = _load_export_input(path)
        input_digests.append(input_digest)
        counts["conversation_documents"] += len(documents)
        for document in documents:
            messages, conversation_count = _user_messages(document)
            counts["conversations"] += conversation_count
            counts["user_messages"] += len(messages)
            for message in messages:
                words = _normalized_words(message)
                if len(words) < ngram_words:
                    continue
                counts["eligible_user_messages"] += 1
                for _index, window in _windows(words, ngram_words):
                    counts["private_ngram_windows"] += 1
                    fingerprints.add(_fingerprint_digest(key, window))
    counts["unique_private_ngram_fingerprints"] = len(fingerprints)
    return fingerprints, input_digests, counts


def _object_matches(
    candidate_object: CandidateObject,
    private_fingerprints: set[bytes],
    ngram_words: int,
    key: bytes,
) -> list[dict[str, Any]]:
    text = candidate_object.data.decode("utf-8", errors="replace")
    words = _word_locations(text)
    matching_indexes = [
        index
        for index, window in _windows([word.value for word in words], ngram_words)
        if _fingerprint_digest(key, window) in private_fingerprints
    ]
    if not matching_indexes:
        return []
    runs: list[tuple[int, int]] = []
    start = previous = matching_indexes[0]
    for index in matching_indexes[1:]:
        if index == previous + 1:
            previous = index
            continue
        runs.append((start, previous))
        start = previous = index
    runs.append((start, previous))

    locations = candidate_object.paths or (None,)
    matches: list[dict[str, Any]] = []
    for path in locations:
        for start, final_window_start in runs:
            final_word = final_window_start + ngram_words - 1
            span = " ".join(word.value for word in words[start : final_word + 1])
            matches.append(
                {
                    "candidate_path": path,
                    "object_type": candidate_object.object_type,
                    "object_fingerprint": _sha256(candidate_object.data),
                    "start_line": words[start].line,
                    "end_line": words[final_word].line,
                    "matched_ngram_windows": final_window_start - start + 1,
                    "span_fingerprint": _fingerprint(key, span),
                }
            )
    return matches


def _scan_candidate(
    repo: Path,
    private_fingerprints: set[bytes],
    ngram_words: int,
    key: bytes,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    head_paths = _head_blob_paths(repo)
    matches: list[dict[str, Any]] = []
    counts = {
        "head_tracked_blobs": len(head_paths),
        "git_objects_scanned": 0,
        "git_blob_objects_scanned": 0,
        "candidate_bytes_scanned": 0,
        "matched_spans": 0,
        "matched_ngram_windows": 0,
    }
    for candidate_object in _candidate_objects(repo, head_paths):
        counts["git_objects_scanned"] += 1
        counts["candidate_bytes_scanned"] += len(candidate_object.data)
        if candidate_object.object_type == "blob":
            counts["git_blob_objects_scanned"] += 1
        object_matches = _object_matches(candidate_object, private_fingerprints, ngram_words, key)
        matches.extend(object_matches)
    matches.sort(
        key=lambda item: (
            item["candidate_path"] is None,
            item["candidate_path"] or "",
            item["start_line"],
            item["span_fingerprint"],
        )
    )
    counts["matched_spans"] = len(matches)
    counts["matched_ngram_windows"] = sum(item["matched_ngram_windows"] for item in matches)
    return matches, counts


def _write_private_report(path: Path, report: dict[str, Any], candidate: Path) -> None:
    try:
        if path.is_symlink() or path.exists():
            _safe_error("report_destination_exists_or_symlink")
        parent = path.parent.resolve(strict=True)
        destination = parent / path.name
        if destination.is_relative_to(candidate):
            _safe_error("report_destination_inside_candidate")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(destination, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            payload = (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            metadata = os.fstat(descriptor)
            if stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1:
                _safe_error("report_permissions_invalid")
        finally:
            os.close(descriptor)
    except AuditError:
        raise
    except OSError:
        _safe_error("report_write_failed")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit a committed public Git candidate for exact private ChatGPT corpus overlap."
    )
    parser.add_argument("--export", action="append", type=Path, required=True, dest="exports")
    parser.add_argument("--candidate-repo", type=Path, required=True)
    parser.add_argument("--ngram-words", type=int, default=DEFAULT_NGRAM_WORDS)
    parser.add_argument("--report", type=Path)
    return parser


def _emit(value: dict[str, Any], *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")), file=stream)


def _run(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    if not MIN_NGRAM_WORDS <= args.ngram_words <= MAX_NGRAM_WORDS:
        _safe_error("ngram_words_out_of_bounds")
    candidate, commit, tree = _resolve_candidate(args.candidate_repo)
    fingerprint_key = os.urandom(32)
    private_fingerprints, export_digests, private_counts = _private_fingerprints(
        args.exports,
        args.ngram_words,
        fingerprint_key,
    )
    matches, candidate_counts = _scan_candidate(
        candidate,
        private_fingerprints,
        args.ngram_words,
        fingerprint_key,
    )
    overlap_found = bool(matches)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "status": "overlap_detected" if overlap_found else "passed",
        "candidate": {
            "path": str(candidate),
            "commit": commit,
            "tree": tree,
        },
        "configuration": {
            "ngram_words": args.ngram_words,
            "minimum_ngram_words": MIN_NGRAM_WORDS,
            "maximum_ngram_words": MAX_NGRAM_WORDS,
            "fingerprint_scheme": "HMAC-SHA256 with an ephemeral undisclosed per-run key",
            "fingerprint_key_id": _sha256(fingerprint_key),
        },
        "export_inputs": [
            {
                "sha256": item.sha256,
                "conversation_documents": item.conversation_documents,
            }
            for item in export_digests
        ],
        "counts": {**private_counts, **candidate_counts},
        "matches": matches,
    }
    if args.report is not None:
        _write_private_report(args.report, report, candidate)
    return report, overlap_found


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        report, overlap_found = _run(args)
    except AuditError as exc:
        _emit({"status": "error", "error": exc.code}, stream=sys.stderr)
        return 1
    except Exception:
        _emit({"status": "error", "error": "internal_error"}, stream=sys.stderr)
        return 1
    _emit(report)
    return 1 if overlap_found else 0


if __name__ == "__main__":
    raise SystemExit(main())
