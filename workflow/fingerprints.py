"""Canonical fingerprints for framework-neutral workflow contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from workflow.contracts import GRAPH_VERSION

if TYPE_CHECKING:
    from workflow.facts import WorkflowFactSnapshot


def _datetime_to_utc_z(value: datetime) -> str:
    """Return a datetime serialized as a UTC ISO string with Z suffix."""
    normalized = value if value.tzinfo else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat().replace("+00:00", "Z")


def normalize_for_hash(value: object) -> object:
    """Normalize objects into deterministic JSON-compatible values."""
    if isinstance(value, datetime):
        return _datetime_to_utc_z(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in sorted(value.items(), key=lambda entry: str(entry[0])):
            canonical_key = str(key)
            if canonical_key in normalized:
                msg = f"Duplicate canonical mapping key {canonical_key!r}."
                raise ValueError(msg)
            normalized[canonical_key] = normalize_for_hash(item)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [normalize_for_hash(item) for item in value]
    return value


def canonical_json(value: object) -> str:
    """Serialize a normalized value for hashing."""
    return json.dumps(
        normalize_for_hash(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def canonical_hash(value: object) -> str:
    """Return the canonical SHA-256 fingerprint for a value."""
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def fact_fingerprint(snapshot: WorkflowFactSnapshot) -> str:
    """Return the graph-versioned fingerprint for an immutable fact snapshot."""
    payload = {
        "graph_version": GRAPH_VERSION,
        "facts": snapshot.model_dump(mode="json"),
    }
    return canonical_hash(payload)


def decision_fingerprint(payload: Mapping[str, object]) -> str:
    """Return a canonical fingerprint for an evaluated node decision payload."""
    return canonical_hash(payload)
