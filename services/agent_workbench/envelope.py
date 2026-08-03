"""Stable response envelopes for agent workbench commands."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from services.agent_workbench.version import (
    COMMAND_VERSION,
    agileforge_version,
)

SCHEMA_VERSION = "agileforge.cli.v1"


@dataclass(frozen=True)
class WorkbenchWarning:
    """Recoverable command warning included in an envelope."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    remediation: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dictionary payload."""
        return {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
            "remediation": list(self.remediation),
        }


@dataclass(frozen=True)
class WorkbenchError:
    """Command error included in a failed envelope."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    remediation: list[str] = field(default_factory=list)
    exit_code: int = 1
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dictionary payload."""
        return {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
            "remediation": list(self.remediation),
            "exit_code": self.exit_code,
            "retryable": self.retryable,
        }


def utc_now_iso() -> str:
    """Return the current UTC timestamp in CLI envelope format."""
    return (
        datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def _meta(
    *,
    command: str,
    command_version: str | None,
    generated_at: str | None,
    correlation_id: str | None,
    source_fingerprint: str | None,
) -> dict[str, str]:
    """Build common CLI envelope metadata."""
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "command_version": command_version or COMMAND_VERSION,
        "agileforge_version": agileforge_version(),
        "generated_at": generated_at or utc_now_iso(),
        "correlation_id": correlation_id or str(uuid4()),
    }
    if source_fingerprint is not None:
        metadata["source_fingerprint"] = source_fingerprint
    return metadata


def success_envelope(  # noqa: PLR0913
    *,
    command: str,
    data: dict[str, Any] | list[Any],
    warnings: list[WorkbenchWarning] | None = None,
    generated_at: str | None = None,
    command_version: str | None = None,
    correlation_id: str | None = None,
    source_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Build a successful command response envelope."""
    return {
        "ok": True,
        "data": data,
        "warnings": [warning.to_dict() for warning in warnings or []],
        "errors": [],
        "meta": _meta(
            command=command,
            command_version=command_version,
            generated_at=generated_at,
            correlation_id=correlation_id,
            source_fingerprint=source_fingerprint,
        ),
    }


def error_envelope(  # noqa: PLR0913
    *,
    command: str,
    error: WorkbenchError,
    warnings: list[WorkbenchWarning] | None = None,
    generated_at: str | None = None,
    command_version: str | None = None,
    correlation_id: str | None = None,
    source_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Build a failed command response envelope."""
    return {
        "ok": False,
        "data": None,
        "warnings": [warning.to_dict() for warning in warnings or []],
        "errors": [error.to_dict()],
        "meta": _meta(
            command=command,
            command_version=command_version,
            generated_at=generated_at,
            correlation_id=correlation_id,
            source_fingerprint=source_fingerprint,
        ),
    }
