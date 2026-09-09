"""Stable original and retry-bound instance keys for execution actions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, cast

type ExecutionKind = Literal["task", "story", "sprint"]

_EXECUTION_KINDS = frozenset({"task", "story", "sprint"})
_INSTANCE_KEY = re.compile(
    r"(?:(?:retry:([1-9][0-9]*):))?(task|story|sprint):([1-9][0-9]*)\Z"
)


def _positive_id(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        message = f"{label} must be a positive integer."
        raise ValueError(message)
    return value


@dataclass(frozen=True)
class ExecutionIdentity:
    """One original or retry-local execution subject."""

    kind: ExecutionKind
    entity_id: int
    retry_attempt_id: int | None = None

    def __post_init__(self) -> None:
        """Reject invalid kinds and identities before a key can be serialized."""
        if self.kind not in _EXECUTION_KINDS:
            message = "Execution kind is invalid."
            raise ValueError(message)
        _positive_id(self.entity_id, "Execution entity ID")
        if self.retry_attempt_id is not None:
            _positive_id(self.retry_attempt_id, "Retry attempt ID")


def execution_instance_key(
    kind: ExecutionKind,
    entity_id: int,
    retry_attempt_id: int | None = None,
) -> str:
    """Return the canonical positioned binding for one execution subject."""
    identity = ExecutionIdentity(kind, entity_id, retry_attempt_id)
    suffix = f"{identity.kind}:{identity.entity_id}"
    return (
        suffix
        if identity.retry_attempt_id is None
        else f"retry:{identity.retry_attempt_id}:{suffix}"
    )


def parse_execution_instance_key(value: str) -> ExecutionIdentity:
    """Parse only canonical positive-ID original and retry instance bindings."""
    if not isinstance(value, str):
        message = "Execution instance key must be a string."
        raise TypeError(message)
    match = _INSTANCE_KEY.fullmatch(value)
    if match is None:
        message = "Execution instance key is not canonical."
        raise ValueError(message)
    retry_text, kind, entity_text = match.groups()
    return ExecutionIdentity(
        kind=cast("ExecutionKind", kind),
        entity_id=int(entity_text),
        retry_attempt_id=None if retry_text is None else int(retry_text),
    )


__all__ = [
    "ExecutionIdentity",
    "ExecutionKind",
    "execution_instance_key",
    "parse_execution_instance_key",
]
