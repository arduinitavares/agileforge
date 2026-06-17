"""Authority curation ADK workflow schemas."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    """Base schema that rejects unknown fields."""

    model_config = ConfigDict(extra="forbid", strict=True)


class AuthorityCurationWorkflowInput(_StrictModel):
    """Input passed from host service into the ADK workflow."""

    project_id: int
    spec_version_id: int
    source_authority_id: int
    source_authority_fingerprint: str = Field(min_length=1)
    source_authority_json: dict[str, object]
    feedback_json: dict[str, object]
    repair_menu: list[AuthorityCurationRepairMenuEntry] = Field(
        default_factory=list
    )
    contract_version: Literal[
        "authority_curation.v1",
        "authority_curation.v2",
    ] = "authority_curation.v1"
    max_iterations: int = Field(default=2, ge=1, le=2)


class AuthorityCurationCriticFinding(_StrictModel):
    """One critic finding produced inside the curation loop."""

    feedback_id: str = Field(min_length=1)
    target_kind: str = Field(min_length=1)
    target_id: str | None = Field(default=None, min_length=1)
    source_item_id: str | None = Field(default=None, min_length=1)
    issue_type: str = Field(min_length=1)
    severity: Literal["blocking", "non_blocking"]
    instruction: str = Field(min_length=1)


class AuthorityCurationCriticOutput(_StrictModel):
    """Wrapper output for ADK agents that cannot emit bare list schemas."""

    findings: list[AuthorityCurationCriticFinding] = Field(default_factory=list)


class AuthorityCurationRepairPlan(_StrictModel):
    """Bounded repair plan emitted before repair compilation."""

    mode: Literal["targeted", "full_recompile", "fail_no_candidate"]
    target_ids: list[str] = Field(default_factory=list)
    feedback_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)


class AuthorityCurationRepairMenuEntry(_StrictModel):
    """One host-minted repair option for a blocking feedback item."""

    handle: str = Field(min_length=1)
    feedback_id: str = Field(min_length=1)
    target_kind: Literal["invariant", "assumption", "gap"]
    target_id: str = Field(min_length=1)
    target_field: str = Field(min_length=1)
    target_review_label: str = Field(min_length=1)
    overlay_target_key: str = Field(min_length=1)
    allowed_repair_kinds: list[
        Literal["replace_text", "replace_parameter_text", "mark_unresolvable"]
    ]
    target_content_hash: str | None = Field(default=None, min_length=1)
    not_repairable_reason: str | None = Field(default=None, min_length=1)


class AuthorityCurationRepairSelection(_StrictModel):
    """One model selection from the host repair menu."""

    feedback_id: str = Field(min_length=1)
    target_handle: str = Field(min_length=1)
    repair_kind: Literal[
        "replace_text",
        "replace_parameter_text",
        "mark_unresolvable",
    ]
    replacement_text: str | None = Field(default=None, min_length=1)
    reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _require_payload_for_repair_kind(self) -> Self:
        if (
            self.repair_kind in {"replace_text", "replace_parameter_text"}
            and not self.replacement_text
        ):
            msg = "replacement_text is required for text replacement repair kinds"
            raise ValueError(msg)
        if self.repair_kind == "mark_unresolvable" and not self.reason:
            msg = "reason is required when repair_kind is mark_unresolvable"
            raise ValueError(msg)
        return self


class AuthorityCurationRepairSelectionPayload(_StrictModel):
    """Repair selections emitted by the ADK repair node."""

    repairs: list[AuthorityCurationRepairSelection] = Field(default_factory=list)


class AuthorityCurationRepairOutput(_StrictModel):
    """Repair output returned by the ADK workflow to the host."""

    mode: Literal["targeted", "full_recompile", "fail_no_candidate"]
    selection_payload: AuthorityCurationRepairSelectionPayload | None = None
    resolved_feedback_ids: list[str] = Field(default_factory=list)
    unresolved_feedback_ids: list[str] = Field(default_factory=list)
    failure_reason: str | None = None


class AuthorityCurationGateDecision(_StrictModel):
    """Final host-visible gate decision for one loop iteration."""

    status: Literal["pass", "retry", "fail"]
    review_ready: bool
    unresolved_feedback_ids: list[str] = Field(default_factory=list)
    reason: str | None = None

    @model_validator(mode="after")
    def _require_fail_reason(self) -> Self:
        if self.status == "fail" and not (self.reason and self.reason.strip()):
            msg = "reason is required when status is fail"
            raise ValueError(msg)
        return self
