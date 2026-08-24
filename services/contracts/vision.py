"""Strict host-prepared contracts for the Vision interview agent."""

from __future__ import annotations

from collections.abc import Hashable
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from services.contracts.vision_evidence import VisionEvidenceBundle


def _strip_required(value: str, label: str) -> str:
    """Normalize one required human or model-facing string."""
    normalized = value.strip()
    if not normalized:
        message = f"{label} must not be blank."
        raise ValueError(message)
    return normalized


def _unique_texts(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    """Reject duplicate references while preserving their declared order."""
    normalized = tuple(_strip_required(value, label) for value in values)
    if len(set(normalized)) != len(normalized):
        msg = f"{label} must not contain duplicates."
        raise ValueError(msg)
    return normalized


def _unique_values[T: Hashable](values: tuple[T, ...], label: str) -> tuple[T, ...]:
    """Reject repeated literal references in one provenance object."""
    if len(set(values)) != len(values):
        msg = f"{label} must not contain duplicates."
        raise ValueError(msg)
    return values


type VisionComponentName = Literal[
    "project_name",
    "target_user",
    "problem",
    "product_category",
    "key_benefit",
    "competitors",
    "differentiator",
]
type VisionBasisSource = Literal["human", "evidence", "inference"]

_EVIDENCE_BUNDLE_ADAPTER: TypeAdapter[VisionEvidenceBundle] = TypeAdapter(
    VisionEvidenceBundle
)


def _parse_evidence(value: object) -> VisionEvidenceBundle:
    """Use the strict evidence model for every operation input."""
    return _EVIDENCE_BUNDLE_ADAPTER.validate_python(value)


class VisionComponents(BaseModel):
    """The complete set of human-defined product Vision components."""

    model_config = ConfigDict(extra="forbid")

    project_name: str | None = None
    target_user: str | None = None
    problem: str | None = None
    product_category: str | None = None
    key_benefit: str | None = None
    competitors: str | None = None
    differentiator: str | None = None

    @field_validator("*", mode="before")
    @classmethod
    def normalize_component(cls, value: object, info: object) -> object:
        """Strip provided component strings and reject ambiguous blanks."""
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        return _strip_required(value, str(getattr(info, "field_name", "component")))

    def is_fully_defined(self) -> bool:
        """Return whether every Vision component has one substantive answer."""
        return all(
            isinstance(value, str) and bool(value)
            for value in self.model_dump().values()
        )


class VisionComponentBasis(BaseModel):
    """Provenance for one non-null Vision component."""

    model_config = ConfigDict(extra="forbid")

    component: VisionComponentName
    source_kinds: tuple[VisionBasisSource, ...] = Field(min_length=1)
    evidence_ids: tuple[str, ...] = ()
    assumption_ids: tuple[str, ...] = ()

    @field_validator("source_kinds")
    @classmethod
    def validate_source_kinds(
        cls, value: tuple[VisionBasisSource, ...]
    ) -> tuple[VisionBasisSource, ...]:
        """Keep a basis source list unambiguous."""
        return _unique_values(value, "source_kinds")

    @field_validator("evidence_ids", "assumption_ids")
    @classmethod
    def validate_references(
        cls, value: tuple[str, ...], info: object
    ) -> tuple[str, ...]:
        """Require distinct substantive provenance references."""
        return _unique_texts(value, str(getattr(info, "field_name", "references")))


class VisionAssumption(BaseModel):
    """One disclosed inference used by the draft."""

    model_config = ConfigDict(extra="forbid")

    assumption_id: str
    text: str
    affected_components: tuple[VisionComponentName, ...] = Field(min_length=1)

    @field_validator("assumption_id", "text")
    @classmethod
    def validate_text(cls, value: str, info: object) -> str:
        """Reject blank assumption identifiers and prose."""
        return _strip_required(value, str(getattr(info, "field_name", "assumption")))

    @field_validator("affected_components")
    @classmethod
    def validate_affected_components(
        cls, value: tuple[VisionComponentName, ...]
    ) -> tuple[VisionComponentName, ...]:
        """Keep one assumption's scope unambiguous."""
        return _unique_values(value, "affected_components")


class VisionConflict(BaseModel):
    """One explicitly represented conflict in the evidence lineage."""

    model_config = ConfigDict(extra="forbid")

    conflict_id: str
    text: str
    status: Literal["unresolved", "resolved"]
    affected_components: tuple[VisionComponentName, ...] = Field(min_length=1)
    evidence_ids: tuple[str, ...] = ()
    assumption_ids: tuple[str, ...] = ()
    resolution: str | None = None

    @field_validator("conflict_id", "text")
    @classmethod
    def validate_text(cls, value: str, info: object) -> str:
        """Reject blank conflict identifiers and prose."""
        return _strip_required(value, str(getattr(info, "field_name", "conflict")))

    @field_validator("affected_components")
    @classmethod
    def validate_affected_components(
        cls, value: tuple[VisionComponentName, ...]
    ) -> tuple[VisionComponentName, ...]:
        """Keep one conflict's scope unambiguous."""
        return _unique_values(value, "affected_components")

    @field_validator("evidence_ids", "assumption_ids")
    @classmethod
    def validate_references(
        cls, value: tuple[str, ...], info: object
    ) -> tuple[str, ...]:
        """Require distinct substantive provenance references."""
        return _unique_texts(value, str(getattr(info, "field_name", "references")))

    @model_validator(mode="after")
    def validate_resolution(self) -> Self:
        """Require a resolution exactly for resolved conflicts."""
        if self.status == "resolved":
            if self.resolution is None:
                msg = "resolved conflicts require a nonblank resolution."
                raise ValueError(msg)
            self.resolution = _strip_required(self.resolution, "resolution")
        elif self.resolution is not None:
            msg = "unresolved conflicts require resolution=None."
            raise ValueError(msg)
        return self


class VisionClarifyingQuestion(BaseModel):
    """One actionable question needed to complete a Vision draft."""

    model_config = ConfigDict(extra="forbid")

    question_id: str
    text: str
    affected_components: tuple[VisionComponentName, ...] = Field(min_length=1)
    conflict_ids: tuple[str, ...] = ()

    @field_validator("question_id", "text")
    @classmethod
    def validate_text(cls, value: str, info: object) -> str:
        """Reject blank question identifiers and prose."""
        return _strip_required(value, str(getattr(info, "field_name", "question")))

    @field_validator("affected_components")
    @classmethod
    def validate_affected_components(
        cls, value: tuple[VisionComponentName, ...]
    ) -> tuple[VisionComponentName, ...]:
        """Keep one question's scope unambiguous."""
        return _unique_values(value, "affected_components")

    @field_validator("conflict_ids")
    @classmethod
    def validate_conflict_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require distinct substantive conflict references."""
        return _unique_texts(value, "conflict_ids")


class VisionDraftOutput(BaseModel):
    """Strict model output for a context-grounded Vision draft."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agileforge.vision-draft.v1"]
    components: VisionComponents
    component_basis: tuple[VisionComponentBasis, ...]
    draft_statement: str
    assumptions: tuple[VisionAssumption, ...]
    conflicts: tuple[VisionConflict, ...]
    clarifying_questions: tuple[VisionClarifyingQuestion, ...]
    is_complete: bool

    @field_validator("draft_statement")
    @classmethod
    def validate_statement(cls, value: str) -> str:
        """Require a substantive draft statement."""
        return _strip_required(value, "draft_statement")


class VisionBootstrapInput(BaseModel):
    """First-turn input containing newly collected evidence."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agileforge.vision-input.v1"]
    operation: Literal["bootstrap"]
    project_name: str
    project_description: str | None
    evidence: VisionEvidenceBundle

    @field_validator("evidence", mode="before")
    @classmethod
    def validate_evidence(cls, value: object) -> VisionEvidenceBundle:
        """Parse the strict evidence bundle before ADK receives it."""
        return _parse_evidence(value)

    @field_validator("project_name")
    @classmethod
    def validate_project_name(cls, value: str) -> str:
        """Require a project identity for the draft."""
        return _strip_required(value, "project_name")

    @field_validator("project_description")
    @classmethod
    def validate_project_description(cls, value: str | None) -> str | None:
        """Normalize optional project description context."""
        return None if value is None else _strip_required(value, "project_description")


class VisionClarificationInput(BaseModel):
    """A response against an existing Vision evidence snapshot."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agileforge.vision-input.v1"]
    operation: Literal["clarification"]
    project_name: str
    project_description: str | None
    vision_evidence_snapshot_id: int = Field(gt=0)
    evidence: VisionEvidenceBundle
    current_components: VisionComponents
    current_statement: str
    current_component_basis: tuple[VisionComponentBasis, ...]
    current_assumptions: tuple[VisionAssumption, ...]
    current_conflicts: tuple[VisionConflict, ...]
    current_questions: tuple[VisionClarifyingQuestion, ...]
    human_response: str
    addressed_question_ids: tuple[str, ...]

    @field_validator("evidence", mode="before")
    @classmethod
    def validate_evidence(cls, value: object) -> VisionEvidenceBundle:
        """Parse the persisted strict evidence bundle."""
        return _parse_evidence(value)

    @field_validator("project_name", "current_statement", "human_response")
    @classmethod
    def validate_required_text(cls, value: str, info: object) -> str:
        """Normalize required host and human text."""
        return _strip_required(value, str(getattr(info, "field_name", "value")))

    @field_validator("project_description")
    @classmethod
    def validate_project_description(cls, value: str | None) -> str | None:
        """Normalize optional project description context."""
        return None if value is None else _strip_required(value, "project_description")

    @field_validator("addressed_question_ids")
    @classmethod
    def validate_addressed_questions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require distinct substantive addressed-question references."""
        return _unique_texts(value, "addressed_question_ids")


class VisionRevisionInput(BaseModel):
    """Revision input carrying new evidence and accepted Vision lineage."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agileforge.vision-input.v1"]
    operation: Literal["revision"]
    project_name: str
    project_description: str | None
    evidence: VisionEvidenceBundle
    accepted_components: VisionComponents
    accepted_statement: str
    accepted_vision_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    revision_reason: str
    active_product_goal_status: Literal["none"]
    prior_review_feedback: str | None

    @field_validator("evidence", mode="before")
    @classmethod
    def validate_evidence(cls, value: object) -> VisionEvidenceBundle:
        """Parse the newly collected strict evidence bundle."""
        return _parse_evidence(value)

    @field_validator("project_name", "accepted_statement", "revision_reason")
    @classmethod
    def validate_required_text(cls, value: str, info: object) -> str:
        """Normalize revision-lineage text."""
        return _strip_required(value, str(getattr(info, "field_name", "value")))

    @field_validator("project_description", "prior_review_feedback")
    @classmethod
    def validate_optional_text(cls, value: str | None, info: object) -> str | None:
        """Normalize optional revision context when supplied."""
        return (
            None
            if value is None
            else _strip_required(value, str(getattr(info, "field_name", "value")))
        )


class VisionPreflight(BaseModel):
    """Freshly recollected evidence for an existing Vision lineage preflight."""

    model_config = ConfigDict(extra="forbid")

    expected_evidence_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    observed_evidence: VisionEvidenceBundle

    @field_validator("observed_evidence", mode="before")
    @classmethod
    def validate_observed_evidence(cls, value: object) -> VisionEvidenceBundle:
        """Parse freshly recollected evidence through the strict bundle contract."""
        return _parse_evidence(value)


class VisionHostMetadata(BaseModel):
    """Trusted host identities that never cross the provider boundary."""

    model_config = ConfigDict(extra="forbid")

    repository_binding_id: int | None = Field(default=None, gt=0)
    supersedes_vision_evidence_snapshot_id: int | None = Field(default=None, gt=0)


type VisionOperationInput = Annotated[
    VisionBootstrapInput | VisionClarificationInput | VisionRevisionInput,
    Field(discriminator="operation"),
]


class VisionAgentInput(BaseModel):
    """ADK-compatible envelope around one Vision operation."""

    model_config = ConfigDict(extra="forbid")

    request: VisionOperationInput
    preflight: VisionPreflight | None = None
    host: VisionHostMetadata = Field(default_factory=VisionHostMetadata)

    @model_validator(mode="after")
    def validate_preflight_scope(self) -> Self:
        """Bind clarification preflight to its persisted request evidence."""
        if isinstance(self.request, VisionClarificationInput):
            if self.preflight is None:
                msg = "clarification requires a preflight."
                raise ValueError(msg)
            if (
                self.preflight.expected_evidence_fingerprint
                != self.request.evidence.evidence_fingerprint
            ):
                msg = "expected_evidence_fingerprint must match request evidence."
                raise ValueError(msg)
        elif self.preflight is not None:
            msg = "preflight is only valid for clarification."
            raise ValueError(msg)
        return self


class VisionModelInput(BaseModel):
    """Strict model-facing envelope containing no host preflight metadata."""

    model_config = ConfigDict(extra="forbid")

    request: VisionOperationInput


class VisionRepairInput(BaseModel):
    """Host-constrained repair request for an invalid draft."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agileforge.vision-repair.v1"]
    operation: Literal["repair"]
    validation_findings: tuple[str, ...] = Field(min_length=1)
    invalid_output: VisionDraftOutput
    allowed_evidence_ids: tuple[str, ...]
    human_input_available: bool

    @field_validator("validation_findings", "allowed_evidence_ids")
    @classmethod
    def validate_text_references(
        cls, value: tuple[str, ...], info: object
    ) -> tuple[str, ...]:
        """Require distinct substantive repair inputs."""
        return _unique_texts(value, str(getattr(info, "field_name", "references")))


__all__ = [
    "VisionAgentInput",
    "VisionAssumption",
    "VisionBasisSource",
    "VisionBootstrapInput",
    "VisionClarificationInput",
    "VisionClarifyingQuestion",
    "VisionComponentBasis",
    "VisionComponentName",
    "VisionComponents",
    "VisionConflict",
    "VisionDraftOutput",
    "VisionHostMetadata",
    "VisionModelInput",
    "VisionOperationInput",
    "VisionPreflight",
    "VisionRepairInput",
    "VisionRevisionInput",
]
