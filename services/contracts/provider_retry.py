# services/contracts/provider_retry.py
"""Closed, immutable contracts for bounded OpenRouter transport retries."""

from __future__ import annotations

from datetime import UTC, datetime
from http import HTTPStatus
from typing import Annotated, Literal, Protocol, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

PROVIDER_RETRY_MAX_ELAPSED_SECONDS: float = 120.0
_TRANSIENT_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


class ProviderRetryConfig(BaseModel):
    """Captured non-secret policy; attempts include the first physical send."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, validate_default=True
    )

    max_attempts: Annotated[int, Field(ge=1, le=10)] = 3
    base_delay_seconds: Annotated[float, Field(gt=0, le=60, allow_inf_nan=False)] = 1.0
    max_delay_seconds: Annotated[float, Field(gt=0, le=60, allow_inf_nan=False)] = 8.0
    max_elapsed_seconds: Annotated[
        float, Field(gt=0, le=PROVIDER_RETRY_MAX_ELAPSED_SECONDS, allow_inf_nan=False)
    ] = 60.0
    min_remaining_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 1.0

    @field_validator("max_delay_seconds")
    @classmethod
    def validate_backoff_cap(cls, value: float, info: ValidationInfo) -> float:
        """Require the jitter cap to cover its starting delay."""
        base_delay = info.data.get("base_delay_seconds")
        if base_delay is not None and value < base_delay:
            message = "max_delay_seconds must be at least base_delay_seconds"
            raise ValueError(message)
        return value

    @field_validator("min_remaining_seconds")
    @classmethod
    def validate_remaining_budget(cls, value: float, info: ValidationInfo) -> float:
        """Require the retry minimum to fit within the captured window."""
        max_elapsed = info.data.get("max_elapsed_seconds")
        if max_elapsed is not None and value > max_elapsed:
            message = "min_remaining_seconds must not exceed max_elapsed_seconds"
            raise ValueError(message)
        return value


class ProviderFailureSummary(BaseModel):
    """Sanitized terminal facts shared by audit, durable outcomes, and clients."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["agileforge.provider-failure.v1"] = (
        "agileforge.provider-failure.v1"
    )
    provider: Literal["openrouter"] = "openrouter"
    category: Literal["external_temporary"] = "external_temporary"
    retryable: Literal[True] = True
    reason: Literal["rate_limited", "unavailable"]
    termination_reason: Literal[
        "attempts_exhausted", "retry_after_exceeds_budget", "retry_budget_exhausted"
    ]
    http_status: int
    call_id: Annotated[
        str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    ]
    attempts: Annotated[int, Field(ge=1, le=10)]
    max_attempts: Annotated[int, Field(ge=1, le=10)]
    retry_after_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = (
        None
    )
    manual_retry_requires_new_key: bool

    @model_validator(mode="after")
    def validate_confirmed_failure(self) -> Self:
        """Reject contradictions rather than projecting fabricated provider facts."""
        if self.http_status not in _TRANSIENT_STATUSES:
            message = "http_status must be a confirmed transient OpenRouter status"
            raise ValueError(message)
        expected_reason = (
            "rate_limited"
            if self.http_status == HTTPStatus.TOO_MANY_REQUESTS
            else "unavailable"
        )
        if self.reason != expected_reason:
            message = "reason must match the confirmed http_status"
            raise ValueError(message)
        if self.attempts > self.max_attempts:
            message = "attempts must not exceed max_attempts"
            raise ValueError(message)
        return self


def provider_failure_message(summary: ProviderFailureSummary) -> str:
    """Render only fixed safe copy derived from validated terminal facts."""
    state = "rate-limited" if summary.reason == "rate_limited" else "unavailable"
    retry_instruction = (
        "Retry this action with a new idempotency key."
        if summary.manual_retry_requires_new_key
        else "Retry this action later."
    )
    return (
        f"OpenRouter is temporarily {state}. Automatic retries stopped. "
        f"{retry_instruction}"
    )


class ProviderTransientFailure(RuntimeError):  # noqa: N818 - public contract name
    """Transport exhaustion carrying only its typed sanitized summary."""

    def __init__(self, summary: ProviderFailureSummary) -> None:
        """Capture only validated terminal facts and their fixed safe message."""
        self.summary: ProviderFailureSummary = summary
        super().__init__(provider_failure_message(summary))


_AuditIdentifier = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
]
_AuditDigest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
_AuditAttemptFingerprint = Annotated[str, Field(pattern=r"^(?:sha256:)?[a-f0-9]{64}$")]
_AuditSeconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class ProviderTryAuditRecord(BaseModel):
    """One allowlisted start or finish for a host-owned physical provider try."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["agileforge.provider-try.v1"] = "agileforge.provider-try.v1"
    provider: Literal["openrouter"] = "openrouter"
    project_id: Annotated[int, Field(gt=0)]
    action_id: _AuditIdentifier
    call_id: Annotated[
        str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    ]
    workflow_node_attempt_id: Annotated[int, Field(gt=0)] | None = None
    attempt_fingerprint: _AuditAttemptFingerprint | None = None
    node_id: _AuditIdentifier | None = None
    instance_key: (
        Annotated[
            str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:/-]+$")
        ]
        | None
    ) = None
    idempotency_key_digest: _AuditDigest | None = None
    model_id: Annotated[
        str,
        Field(min_length=1, max_length=256, pattern=r"^openrouter/[A-Za-z0-9_./:-]+$"),
    ]
    request_fingerprint: _AuditDigest
    try_ordinal: Annotated[int, Field(ge=1, le=10)]
    max_attempts: Annotated[int, Field(ge=1, le=10)]
    retry_config: ProviderRetryConfig
    started_at: datetime
    finished_at: datetime | None = None
    duration_seconds: _AuditSeconds | None = None
    http_status: Annotated[int, Field(ge=100, le=599)] | None = None
    retry_classification: (
        Literal["rate_limited", "unavailable", "non_retryable", "cancelled"] | None
    ) = None
    disposition: (
        Literal[
            "success",
            "retry_scheduled",
            "exhausted",
            "non_retryable",
            "cancelled",
            "not_sent",
        ]
        | None
    ) = None
    termination_reason: (
        Literal[
            "attempts_exhausted", "retry_after_exceeds_budget", "retry_budget_exhausted"
        ]
        | None
    ) = None
    retry_after_seconds: _AuditSeconds | None = None
    selected_delay_seconds: _AuditSeconds | None = None
    next_eligible_retry_at: datetime | None = None

    @field_validator("started_at", "finished_at", "next_eligible_retry_at")
    @classmethod
    def validate_utc(cls, value: datetime | None) -> datetime | None:
        """Persist explicit UTC wall times without local-time ambiguity."""
        if value is not None and value.utcoffset() != UTC.utcoffset(value):
            message = "audit timestamps must be aware UTC values"
            raise ValueError(message)
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        """Keep optional node identity complete and physical sends bounded."""
        if (
            self.try_ordinal > self.max_attempts
            or self.max_attempts != self.retry_config.max_attempts
        ):
            message = "audit ordinal and max_attempts must match retry_config"
            raise ValueError(message)
        if self.workflow_node_attempt_id is None:
            if any(
                item is not None
                for item in (self.attempt_fingerprint, self.node_id, self.instance_key)
            ):
                message = "node association requires a node attempt identity"
                raise ValueError(message)
        elif any(
            item is None
            for item in (
                self.attempt_fingerprint,
                self.node_id,
                self.idempotency_key_digest,
            )
        ):
            message = "node attempt association requires its complete host identity"
            raise ValueError(message)
        return self

    @model_validator(mode="after")
    def validate_phase(self) -> Self:
        """Reject finish facts on starts and incomplete scheduled/terminal finishes."""
        finish_values = (
            self.duration_seconds,
            self.http_status,
            self.retry_classification,
            self.disposition,
            self.termination_reason,
            self.retry_after_seconds,
            self.selected_delay_seconds,
            self.next_eligible_retry_at,
        )
        if self.finished_at is None:
            if any(value is not None for value in finish_values):
                message = "started records cannot contain finish facts"
                raise ValueError(message)
            return self
        if self.duration_seconds is None or self.disposition is None:
            message = "finished records require duration and disposition"
            raise ValueError(message)
        if self.disposition == "retry_scheduled":
            self._validate_schedule()
        elif (
            self.selected_delay_seconds is not None
            or self.next_eligible_retry_at is not None
        ):
            message = "only scheduled retries carry a delay and eligible time"
            raise ValueError(message)
        if (self.disposition in {"exhausted", "not_sent"}) != (
            self.termination_reason is not None
        ):
            message = "only temporary terminal records require a termination reason"
            raise ValueError(message)
        self._validate_classification()
        return self

    def _validate_schedule(self) -> None:
        if (
            self.try_ordinal >= self.max_attempts
            or self.http_status not in _TRANSIENT_STATUSES
            or self.selected_delay_seconds is None
            or self.next_eligible_retry_at is None
        ):
            message = "scheduled retries require a transient response and timing"
            raise ValueError(message)

    def _validate_classification(self) -> None:
        if self.disposition == "not_sent":
            self._validate_unsent_reservation()
            return
        if self.disposition in {"retry_scheduled", "exhausted"}:
            self._validate_transient_classification()
            return
        expected = {
            "success": None,
            "cancelled": "cancelled",
            "non_retryable": "non_retryable",
        }.get(self.disposition or "")
        if self.retry_classification != expected:
            message = "retry classification must match the finish disposition"
            raise ValueError(message)
        invalid_status = (
            (
                self.disposition == "success"
                and self.http_status is not None
                and not HTTPStatus.OK <= self.http_status < HTTPStatus.MULTIPLE_CHOICES
            )
            or (self.disposition == "cancelled" and self.http_status is not None)
            or (
                self.disposition == "non_retryable"
                and self.http_status in _TRANSIENT_STATUSES
            )
        )
        if invalid_status:
            message = "confirmed status must match the finish disposition"
            raise ValueError(message)

    def _validate_transient_classification(self) -> None:
        if self.http_status is None:
            if (
                self.retry_classification is not None
                or self.termination_reason == "attempts_exhausted"
                or self.retry_after_seconds is not None
            ):
                message = "null-status exhaustion requires only local budget facts"
                raise ValueError(message)
            return
        if self.http_status not in _TRANSIENT_STATUSES:
            message = "temporary retry finishes require a confirmed eligible status"
            raise ValueError(message)
        expected = (
            "rate_limited"
            if self.http_status == HTTPStatus.TOO_MANY_REQUESTS
            else "unavailable"
        )
        if self.retry_classification != expected:
            message = "retry classification must match the confirmed status"
            raise ValueError(message)

    def _validate_unsent_reservation(self) -> None:
        if (
            self.try_ordinal <= 1
            or self.termination_reason != "retry_budget_exhausted"
            or any(
                value is not None
                for value in (
                    self.http_status,
                    self.retry_classification,
                    self.retry_after_seconds,
                    self.selected_delay_seconds,
                    self.next_eligible_retry_at,
                )
            )
        ):
            message = "unsent reservations require only final retry budget facts"
            raise ValueError(message)


class ProviderAuditError(RuntimeError):
    """Local audit persistence/integrity failure, never a provider classification."""

    def __init__(self) -> None:
        """Expose fixed copy without SQL, exception, or provider body details."""
        super().__init__("Provider attempt audit could not be recorded or verified.")


class ProviderAttemptAudit(Protocol):
    """Independent durable writes and typed terminal verification for one call."""

    def append_started(self, record: ProviderTryAuditRecord) -> None:
        """Commit safe host identity before a physical send."""
        ...

    def append_finished(self, record: ProviderTryAuditRecord) -> None:
        """Commit the send outcome before further work or publication."""
        ...

    def terminal_failure(
        self,
        *,
        project_id: int,
        action_id: str,
        call_id: str,
        expected_summary: ProviderFailureSummary | None = None,
    ) -> ProviderFailureSummary | None:
        """Return only terminal facts proven by every independently committed try."""
        ...


class ProviderRetryClock(Protocol):
    """One deterministic wall/monotonic clock and asynchronous wait seam."""

    def monotonic(self) -> float:
        """Return monotonic time for retry and host budgets."""
        ...

    def utc_now(self) -> datetime:
        """Return aware wall time for HTTP dates and audit timestamps."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait without coupling retry policy to real time in tests."""
        ...


__all__ = [
    "PROVIDER_RETRY_MAX_ELAPSED_SECONDS",
    "ProviderAttemptAudit",
    "ProviderAuditError",
    "ProviderFailureSummary",
    "ProviderRetryClock",
    "ProviderRetryConfig",
    "ProviderTransientFailure",
    "ProviderTryAuditRecord",
    "provider_failure_message",
]
