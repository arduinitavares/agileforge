"""Canonical repository inventory paths, payloads, and fingerprints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from workflow.fingerprints import canonical_hash

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_PATH_BYTES_PREFIX = "agileforge-path-bytes-v1:"

type InventoryContentStatus = Literal[
    "hashable",
    "secret",
    "oversized",
    "symlink",
]
type InventoryFingerprintEntry = tuple[
    str,
    int,
    str | None,
    InventoryContentStatus,
]


def repository_path_bytes(path: str) -> bytes:
    """Return the reversible filesystem bytes represented by one path."""
    try:
        return path.encode("utf-8", errors="surrogateescape")
    except UnicodeEncodeError as exc:
        msg = "Repository paths may contain only valid text or surrogateescape bytes."
        raise ValueError(msg) from exc


def encode_repository_path(path: str) -> str:
    """Encode non-UTF-8 path bytes into an unambiguous canonical string."""
    raw_path = repository_path_bytes(path)
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return f"{_PATH_BYTES_PREFIX}{raw_path.hex()}"
    if path.startswith(_PATH_BYTES_PREFIX):
        return f"{_PATH_BYTES_PREFIX}{raw_path.hex()}"
    return path


def decode_repository_path(encoded_path: str) -> str:
    """Decode one canonical path string without replacement or collision."""
    if not encoded_path.startswith(_PATH_BYTES_PREFIX):
        repository_path_bytes(encoded_path)
        return encoded_path
    raw_hex = encoded_path.removeprefix(_PATH_BYTES_PREFIX)
    try:
        raw_path = bytes.fromhex(raw_hex)
    except ValueError as exc:
        msg = "Canonical repository path contains invalid byte encoding."
        raise ValueError(msg) from exc
    decoded_path = raw_path.decode("utf-8", errors="surrogateescape")
    if encode_repository_path(decoded_path) != encoded_path:
        msg = "Canonical repository path encoding is not reversible."
        raise ValueError(msg)
    return decoded_path


def encode_repository_paths(paths: Sequence[str]) -> list[str]:
    """Return canonical persisted strings for repository paths."""
    return [encode_repository_path(path) for path in paths]


def canonical_inventory_payload(
    *,
    git_available: bool,
    commit: str | None,
    dirty: bool,
    files: Sequence[InventoryFingerprintEntry],
    total_bytes: int,
) -> dict[str, object]:
    """Build the complete root-independent persisted inventory payload."""
    canonical_files = [
        {
            "path": encode_repository_path(path),
            "size_bytes": size_bytes,
            "sha256": sha256,
            "content_status": content_status,
        }
        for path, size_bytes, sha256, content_status in files
    ]
    return {
        "git_available": git_available,
        "commit": commit,
        "dirty": dirty,
        "files": canonical_files,
        "total_bytes": total_bytes,
        "truncated": False,
    }


def inventory_binding_fingerprint(
    inventory_payload: Mapping[str, object],
    selected_for_model: Sequence[str],
) -> str:
    """Bind complete inventory semantics to the exact bounded selection."""
    return canonical_hash(
        {
            "inventory": inventory_payload,
            "selected_for_model": encode_repository_paths(selected_for_model),
        }
    )


__all__ = [
    "InventoryContentStatus",
    "InventoryFingerprintEntry",
    "canonical_inventory_payload",
    "decode_repository_path",
    "encode_repository_path",
    "encode_repository_paths",
    "inventory_binding_fingerprint",
    "repository_path_bytes",
]
