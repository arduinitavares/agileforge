"""Provider and host contracts for immutable, evidence-bound Story items."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.contracts.specification_references import (
    AcceptedSpecificationReference,
    SpecificationReferenceError,
    canonical_spec_item_ids,
    require_nonblank_text,
    validate_accepted_specification_root,
    validate_backlog_item_id,
    validate_canonical_spec_item_ids,
    validate_story_item_id,
)
from workflow.fingerprints import canonical_hash

if TYPE_CHECKING:
    from collections.abc import Iterable


_PERSONA_PATTERN = re.compile(
    r"^as (?:a|an|the) (?P<persona>.+?)(?:,)? i want ", re.IGNORECASE
)
_MAX_PERSONA_LENGTH = 100
_MAX_STORY_ITEMS = 8
_STORY_AUTHORING_SENTINELS: frozenset[str] = frozenset(
    {
        "n/a",
        "not applicable",
        "placeholder",
        "tbd",
        "to be determined",
        "todo",
    }
)
_STORY_SENTINEL_WRAPPER_CHARACTERS = "[]<>{}()'\"`.,:;!?-_*~"
_STORY_REQUIRED_PROSE_FIELDS: tuple[str, ...] = (
    "story_title",
    "statement",
    "effort_rationale",
    "order_rationale",
)
_STORY_INVEST_DIMENSION_NAMES: tuple[str, ...] = (
    "independent",
    "negotiable",
    "valuable",
    "estimable",
    "small",
    "testable",
)
_STORY_VALIDATION_LOCATION_NAMES: frozenset[str] = frozenset(
    {
        "acceptance_criteria",
        "clarifying_questions",
        "confidence",
        "dependency_candidates",
        "effort_rationale",
        "estimated_effort",
        "evidence",
        "independent",
        "invest_assessment",
        "is_complete",
        "negotiable",
        "order_rationale",
        "prerequisite_ref",
        "produced_artifacts",
        "rationale",
        "reason",
        "research_caveats",
        "result",
        "small",
        "spec_item_ids",
        "statement",
        "story_item_id",
        "story_title",
        "testable",
        "valuable",
        "estimable",
    }
)
_STORY_VALIDATION_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
STORY_POINTS_BY_EFFORT: dict[str, int] = {
    "XS": 1,
    "S": 2,
    "M": 3,
    "L": 5,
    "XL": 8,
}


class StorySentinelContentError(ValueError):
    """Exact Story fields that contain authoring sentinels, never their values."""

    def __init__(self, fields: Iterable[str]) -> None:
        """Build a safe error message from field paths only."""
        self.fields: tuple[str, ...] = tuple(fields)
        message = (
            "Story content contains authoring sentinels in fields: "
            + ", ".join(self.fields)
        )
        super().__init__(message)


class StoryReferenceContentError(ValueError):
    """Exact Story reference fields that failed without retaining their values."""

    def __init__(self, fields: Iterable[str]) -> None:
        """Build a safe error message from field paths only."""
        self.fields: tuple[str, ...] = tuple(fields)
        message = (
            "Story references failed validation in fields: " + ", ".join(self.fields)
        )
        super().__init__(message)


def is_story_sentinel_text(value: object) -> bool:
    """Match only a normalized whole field, never a word within substantive prose."""
    if not isinstance(value, str):
        return False
    normalized = " ".join(value.casefold().split())
    prior = None
    while normalized != prior:
        prior = normalized
        normalized = normalized.strip(_STORY_SENTINEL_WRAPPER_CHARACTERS).strip()
    return normalized in _STORY_AUTHORING_SENTINELS


def _story_mapping_sentinel_fields(
    raw_item: Mapping[str, object],
    *,
    index: int,
) -> tuple[str, ...]:
    """Inspect one provider item while emitting only fixed-schema field paths."""
    fields: list[str] = []
    prefix = f"story_items[{index}]"
    fields.extend(
        f"{prefix}.{field_name}"
        for field_name in _STORY_REQUIRED_PROSE_FIELDS
        if is_story_sentinel_text(raw_item.get(field_name))
    )
    criteria = raw_item.get("acceptance_criteria")
    if isinstance(criteria, (list, tuple)):
        fields.extend(
            f"{prefix}.acceptance_criteria[{criterion_index}]"
            for criterion_index, criterion in enumerate(criteria)
            if is_story_sentinel_text(criterion)
        )
    assessment = raw_item.get("invest_assessment")
    if not isinstance(assessment, Mapping):
        return tuple(fields)
    assessment_mapping = cast("Mapping[object, object]", assessment)
    for dimension_name in _STORY_INVEST_DIMENSION_NAMES:
        dimension = assessment_mapping.get(dimension_name)
        if isinstance(dimension, Mapping):
            dimension_mapping = cast("Mapping[object, object]", dimension)
            fields.extend(
                f"{prefix}.invest_assessment.{dimension_name}.{field_name}"
                for field_name in ("rationale", "evidence")
                if is_story_sentinel_text(dimension_mapping.get(field_name))
            )
    return tuple(fields)


def story_output_sentinel_fields(value: object) -> tuple[str, ...]:
    """Inspect an untrusted provider mapping without echoing any field values."""
    if not isinstance(value, Mapping):
        return ()
    value_mapping = cast("Mapping[object, object]", value)
    raw_items = value_mapping.get("user_stories")
    if not isinstance(raw_items, (list, tuple)):
        return ()
    fields: list[str] = []
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, Mapping):
            continue
        fields.extend(
            _story_mapping_sentinel_fields(
                cast("Mapping[str, object]", raw_item),
                index=index,
            )
        )
    return tuple(fields)


def _story_sentinel_leaf_validation_path(
    location: tuple[object, ...],
    value: object,
) -> str | None:
    """Map one leaf validation error to a fixed-schema path when it is sentinel."""
    if not is_story_sentinel_text(value) or len(location) < 3:  # noqa: PLR2004
        return None
    root, item_index, *segments = location
    if (
        root not in {"story_items", "user_stories"}
        or not isinstance(item_index, int)
        or isinstance(item_index, bool)
        or item_index < 0
    ):
        return None
    prefix = f"story_items[{item_index}]"
    if len(segments) == 1 and segments[0] in _STORY_REQUIRED_PROSE_FIELDS:
        return f"{prefix}.{segments[0]}"
    if (
        len(segments) == 2  # noqa: PLR2004
        and segments[0] == "acceptance_criteria"
        and isinstance(segments[1], int)
        and not isinstance(segments[1], bool)
        and segments[1] >= 0
    ):
        return f"{prefix}.acceptance_criteria[{segments[1]}]"
    if (
        len(segments) == 3  # noqa: PLR2004
        and segments[0] == "invest_assessment"
        and segments[1] in _STORY_INVEST_DIMENSION_NAMES
        and segments[2] in {"rationale", "evidence"}
    ):
        return f"{prefix}.invest_assessment.{segments[1]}.{segments[2]}"
    return None


def story_validation_error_sentinel_fields(errors: object) -> tuple[str, ...]:
    """Recover safe sentinel paths from leaf, item, or item-list error inputs."""
    if not isinstance(errors, (list, tuple)):
        return ()
    fields: list[str] = []
    for error in errors:
        if not isinstance(error, Mapping):
            continue
        error_mapping = cast("Mapping[object, object]", error)
        raw_location = error_mapping.get("loc")
        if not isinstance(raw_location, (list, tuple)) or not raw_location:
            continue
        location = tuple(raw_location)
        root = location[0]
        if root not in {"story_items", "user_stories"}:
            continue
        raw_input = error_mapping.get("input")
        if len(location) == 1 and isinstance(raw_input, (list, tuple)):
            fields.extend(
                story_output_sentinel_fields({"user_stories": raw_input})
            )
            continue
        if len(location) == 2:  # noqa: PLR2004
            item_index = location[1]
            if (
                isinstance(item_index, int)
                and not isinstance(item_index, bool)
                and item_index >= 0
                and isinstance(raw_input, Mapping)
            ):
                fields.extend(
                    _story_mapping_sentinel_fields(
                        cast("Mapping[str, object]", raw_input),
                        index=item_index,
                    )
                )
            continue
        leaf_path = _story_sentinel_leaf_validation_path(location, raw_input)
        if leaf_path is not None:
            fields.append(leaf_path)
    return tuple(dict.fromkeys(fields))


def _safe_story_validation_path(value: object) -> str:
    """Normalize a Pydantic location without retaining provider-owned names."""
    if not isinstance(value, (list, tuple)) or not value:
        return "story_output"
    segments = tuple(value)
    root = segments[0]
    if root in {"story_items", "user_stories"}:
        path = "story_items"
    elif isinstance(root, str) and root in _STORY_VALIDATION_LOCATION_NAMES:
        path = root
    else:
        return "story_output"
    for segment in segments[1:]:
        if isinstance(segment, int) and not isinstance(segment, bool) and segment >= 0:
            path = f"{path}[{segment}]"
        elif isinstance(segment, str) and segment in _STORY_VALIDATION_LOCATION_NAMES:
            path = f"{path}.{segment}"
        else:
            return f"{path}.invalid_field"
    return path


def safe_story_validation_errors(errors: object) -> list[dict[str, object]]:
    """Retain only fixed schema paths and bounded error codes for diagnostics."""
    safe_errors: list[dict[str, object]] = []
    if isinstance(errors, (list, tuple)):
        for error in errors:
            if not isinstance(error, Mapping):
                continue
            error_mapping = cast("Mapping[object, object]", error)
            raw_type = error_mapping.get("type")
            error_type = (
                raw_type
                if isinstance(raw_type, str)
                and _STORY_VALIDATION_TYPE_PATTERN.fullmatch(raw_type)
                else "validation_error"
            )
            safe_error: dict[str, object] = {
                "path": _safe_story_validation_path(error_mapping.get("loc")),
                "type": error_type,
            }
            if safe_error not in safe_errors:
                safe_errors.append(safe_error)
    return safe_errors or [{"path": "story_output", "type": "validation_error"}]


def safe_story_validation_message(errors: object) -> str:
    """Describe Story schema failures without provider input or exception text."""
    details = ", ".join(
        f"{error['path']} ({error['type']})"
        for error in safe_story_validation_errors(errors)
    )
    return f"Story output schema validation failed at: {details}"


def parse_story_persona(statement: str) -> str:
    """Derive the sole canonical persona from the approved Story prefix."""
    match = _PERSONA_PATTERN.match(statement.strip().replace("*", ""))
    if match is None:
        message = "statement must start with 'As a|an|the <persona>,? I want '"
        raise ValueError(message)
    persona = match.group("persona").strip()
    if not 1 <= len(persona) <= _MAX_PERSONA_LENGTH:
        message = "Story persona must contain one through 100 characters"
        raise ValueError(message)
    return persona


class StoryDependencyCandidate(BaseModel):
    """One provider-proposed dependency retained in immutable Story content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prerequisite_ref: Annotated[str, Field(min_length=1)]
    reason: Annotated[str, Field(min_length=1)]
    confidence: Literal["explicit", "inferred"]


class InvestDimensionAssessment(BaseModel):
    """Assessment of one INVEST quality dimension with inspectable evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    result: Literal["pass", "concern", "fail"]
    rationale: Annotated[str, Field(min_length=1)]
    evidence: Annotated[str, Field(min_length=1)]

    @field_validator("rationale", "evidence")
    @classmethod
    def validate_nonblank_text(cls, value: str) -> str:
        """Reject blank text while preserving valid content."""
        return require_nonblank_text(value, field_name="INVEST dimension field")


class StoryInvestAssessment(BaseModel):
    """Complete, explainable assessment across all six INVEST dimensions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    independent: InvestDimensionAssessment
    negotiable: InvestDimensionAssessment
    valuable: InvestDimensionAssessment
    estimable: InvestDimensionAssessment
    small: InvestDimensionAssessment
    testable: InvestDimensionAssessment


class UserStoryAgentItem(BaseModel):
    """ID-free provider Story output before host validation and canonicalization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    story_title: Annotated[str, Field(min_length=1)]
    statement: Annotated[str, Field(min_length=1)]
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    spec_item_ids: tuple[str, ...] = Field(
        description=(
            "Non-empty Specification item IDs selected strictly from "
            "parent_backlog_spec_item_ids."
        ),
    )
    invest_assessment: StoryInvestAssessment
    estimated_effort: Literal["XS", "S", "M", "L", "XL"]
    effort_rationale: Annotated[str, Field(min_length=1)]
    order_rationale: Annotated[str, Field(min_length=1)]
    produced_artifacts: tuple[str, ...]
    research_caveats: tuple[str, ...]
    dependency_candidates: tuple[StoryDependencyCandidate, ...]

    @field_validator("story_title", "effort_rationale", "order_rationale")
    @classmethod
    def validate_nonblank_text_fields(cls, value: str) -> str:
        """Reject whitespace-only fields from agent output."""
        return require_nonblank_text(value, field_name="User story field")

    @field_validator("acceptance_criteria")
    @classmethod
    def validate_exact_nonblank_criteria(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Reject blank criteria without altering other bytes or ordering."""
        if any(not criterion.strip() for criterion in value):
            message = "acceptance criterion must not be empty or whitespace-only"
            raise ValueError(message)
        return value

    @model_validator(mode="after")
    def validate_statement_persona(self) -> Self:
        """Use the sole host persona parser at the provider-output boundary."""
        parse_story_persona(self.statement)
        return self


class CanonicalStoryItem(BaseModel):
    """Closed host Story content; its fingerprint is stored only in an envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    story_item_id: str
    story_title: Annotated[str, Field(min_length=1)]
    statement: Annotated[str, Field(min_length=1)]
    persona: Annotated[str, Field(min_length=1, max_length=_MAX_PERSONA_LENGTH)]
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    spec_item_ids: tuple[str, ...]
    invest_assessment: StoryInvestAssessment
    estimated_effort: Literal["XS", "S", "M", "L", "XL"]
    effort_rationale: Annotated[str, Field(min_length=1)]
    order_rationale: Annotated[str, Field(min_length=1)]
    produced_artifacts: tuple[str, ...]
    research_caveats: tuple[str, ...]
    dependency_candidates: tuple[StoryDependencyCandidate, ...]

    @field_validator("story_item_id")
    @classmethod
    def validate_host_item_id(cls, value: str) -> str:
        """Reject impossible host Story IDs during canonical-item deserialization."""
        return validate_story_item_id(value)

    @field_validator(
        "story_title",
        "statement",
        "persona",
        "effort_rationale",
        "order_rationale",
    )
    @classmethod
    def validate_nonblank_content(cls, value: str) -> str:
        """Reject blank host Story content without rewriting valid bytes."""
        return require_nonblank_text(value, field_name="Story item content")

    @field_validator("acceptance_criteria")
    @classmethod
    def validate_host_criteria(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank criteria while preserving exact valid criteria bytes/order."""
        if any(not criterion.strip() for criterion in value):
            message = "acceptance criterion must not be empty or whitespace-only"
            raise ValueError(message)
        return value

    @field_validator("spec_item_ids")
    @classmethod
    def validate_canonical_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject noncanonical evidence in persisted/hashable host Story content."""
        return validate_canonical_spec_item_ids(value)

    @model_validator(mode="after")
    def validate_derived_persona(self) -> Self:
        """Require the host persona to equal the sole parser result exactly."""
        if self.persona != parse_story_persona(self.statement):
            message = "Story persona must equal the parsed statement persona"
            raise ValueError(message)
        return self


class StoryItemEnvelope(BaseModel):
    """Canonical Story item beside its non-recursive immutable fingerprint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    item: CanonicalStoryItem
    item_fingerprint: str

    @model_validator(mode="after")
    def validate_fingerprint(self) -> Self:
        """Ensure the fingerprint covers the complete closed item and nothing else."""
        if self.item_fingerprint != canonical_hash(self.item.model_dump(mode="json")):
            message = "Story item fingerprint does not match canonical item"
            raise ValueError(message)
        return self


class IntermediateCanonicalStoryItem(BaseModel):
    """Closed post-INVEST, pre-rationale item retained only for correction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    story_item_id: str
    story_title: Annotated[str, Field(min_length=1)]
    statement: Annotated[str, Field(min_length=1)]
    persona: Annotated[str, Field(min_length=1, max_length=_MAX_PERSONA_LENGTH)]
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    spec_item_ids: tuple[str, ...]
    invest_assessment: StoryInvestAssessment
    estimated_effort: Literal["XS", "S", "M", "L", "XL"]
    produced_artifacts: tuple[str, ...]
    research_caveats: tuple[str, ...]
    dependency_candidates: tuple[StoryDependencyCandidate, ...]

    @field_validator("story_item_id")
    @classmethod
    def validate_host_item_id(cls, value: str) -> str:
        """Reject impossible historical host Story IDs."""
        return validate_story_item_id(value)

    @field_validator("story_title", "statement", "persona")
    @classmethod
    def validate_nonblank_content(cls, value: str) -> str:
        """Reject blank historical content without rewriting accepted bytes."""
        return require_nonblank_text(
            value,
            field_name="Intermediate Story item content",
        )

    @field_validator("acceptance_criteria")
    @classmethod
    def validate_host_criteria(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank historical criteria while preserving exact ordering."""
        if any(not criterion.strip() for criterion in value):
            message = "acceptance criterion must not be empty or whitespace-only"
            raise ValueError(message)
        return value

    @field_validator("spec_item_ids")
    @classmethod
    def validate_canonical_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require stored historical evidence IDs to remain canonical."""
        return validate_canonical_spec_item_ids(value)

    @model_validator(mode="after")
    def validate_derived_persona(self) -> Self:
        """Require the historical persona to match the same host parser."""
        if self.persona != parse_story_persona(self.statement):
            message = "Story persona must equal the parsed statement persona"
            raise ValueError(message)
        return self


class IntermediateStoryItemEnvelope(BaseModel):
    """Historical non-recursive fingerprint envelope for the intermediate shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    item: IntermediateCanonicalStoryItem
    item_fingerprint: str

    @model_validator(mode="after")
    def validate_fingerprint(self) -> Self:
        """Verify the stored fingerprint against the exact intermediate item."""
        if self.item_fingerprint != canonical_hash(self.item.model_dump(mode="json")):
            message = "Story item fingerprint does not match canonical item"
            raise ValueError(message)
        return self


class IntermediateCanonicalStoryOutput(BaseModel):
    """Closed post-INVEST artifact accepted only as a correction source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    story_items: tuple[IntermediateStoryItemEnvelope, ...] = Field(
        min_length=1,
        max_length=_MAX_STORY_ITEMS,
    )
    is_complete: bool
    clarifying_questions: tuple[str, ...] = ()


class LegacyCanonicalStoryItem(BaseModel):
    """Closed pre-#221 Story item retained only for correction source validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    story_item_id: str
    story_title: Annotated[str, Field(min_length=1)]
    statement: Annotated[str, Field(min_length=1)]
    persona: Annotated[str, Field(min_length=1, max_length=_MAX_PERSONA_LENGTH)]
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    spec_item_ids: tuple[str, ...]
    invest_score: Literal["High", "Medium", "Low"]
    estimated_effort: Literal["XS", "S", "M", "L", "XL"]
    produced_artifacts: tuple[str, ...]
    research_caveats: tuple[str, ...]
    decomposition_warning: str | None
    dependency_candidates: tuple[StoryDependencyCandidate, ...]

    @field_validator("story_item_id")
    @classmethod
    def validate_host_item_id(cls, value: str) -> str:
        """Reject impossible historical host Story IDs."""
        return validate_story_item_id(value)

    @field_validator("story_title", "statement", "persona")
    @classmethod
    def validate_nonblank_content(cls, value: str) -> str:
        """Reject blank historical content without rewriting accepted bytes."""
        return require_nonblank_text(value, field_name="Legacy Story item content")

    @field_validator("acceptance_criteria")
    @classmethod
    def validate_host_criteria(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank historical criteria while preserving exact ordering."""
        if any(not criterion.strip() for criterion in value):
            message = "acceptance criterion must not be empty or whitespace-only"
            raise ValueError(message)
        return value

    @field_validator("spec_item_ids")
    @classmethod
    def validate_canonical_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require the stored historical evidence IDs to remain canonical."""
        return validate_canonical_spec_item_ids(value)

    @model_validator(mode="after")
    def validate_derived_persona(self) -> Self:
        """Require the historical persona to match the same host parser."""
        if self.persona != parse_story_persona(self.statement):
            message = "Story persona must equal the parsed statement persona"
            raise ValueError(message)
        return self


class LegacyStoryItemEnvelope(BaseModel):
    """Historical non-recursive Story fingerprint envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    item: LegacyCanonicalStoryItem
    item_fingerprint: str

    @model_validator(mode="after")
    def validate_fingerprint(self) -> Self:
        """Verify the stored fingerprint against the historical closed item shape."""
        if self.item_fingerprint != canonical_hash(self.item.model_dump(mode="json")):
            message = "Story item fingerprint does not match canonical item"
            raise ValueError(message)
        return self


class LegacyCanonicalStoryOutput(BaseModel):
    """Closed pre-#221 artifact shape accepted only as a correction source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    story_items: tuple[LegacyStoryItemEnvelope, ...] = Field(
        min_length=1,
        max_length=_MAX_STORY_ITEMS,
    )
    is_complete: bool
    clarifying_questions: tuple[str, ...] = ()


class StoryReplacementSource(BaseModel):
    """Exact accepted artifact identity embedded by the host in a replacement."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    story_artifact_id: Annotated[int, Field(gt=0)]
    artifact_fingerprint: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class CanonicalStoryOutput(BaseModel):
    """Closed host Story envelope persisted for later review and planning."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    story_items: tuple[StoryItemEnvelope, ...] = Field(
        min_length=1,
        max_length=_MAX_STORY_ITEMS,
    )
    is_complete: bool
    clarifying_questions: tuple[str, ...] = ()
    replacement_source: StoryReplacementSource | None = Field(
        default=None,
        exclude_if=lambda source: source is None,
    )


class UserStoryWriterOutput(BaseModel):
    """Provider output with no provider-owned Story item identities."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_stories: tuple[UserStoryAgentItem, ...] = Field(
        min_length=1, max_length=_MAX_STORY_ITEMS
    )
    is_complete: bool
    clarifying_questions: tuple[str, ...] = ()


class UserStoryWriterInput(BaseModel):
    """Story invocation root carrying one exact accepted Specification contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted_specification_version_id: Annotated[int, Field(gt=0)]
    accepted_specification_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    accepted_specification_json: Annotated[str, Field(min_length=1)]
    parent_backlog_item_id: str
    parent_backlog_spec_item_ids: tuple[str, ...] = Field(
        description=(
            "Exact allow-list of Specification item IDs that generated User "
            "Stories may cite."
        ),
    )
    roadmap_context: str = ""
    user_input: str | None = None

    @field_validator("parent_backlog_item_id")
    @classmethod
    def validate_parent_backlog_item_id(cls, value: str) -> str:
        """Reject impossible parent IDs before the Story provider is invoked."""
        return validate_backlog_item_id(value)

    @model_validator(mode="after")
    def validate_specification_root_and_parent_evidence(self) -> Self:
        """Prove root bytes/hash and canonical qualifying parent evidence once."""
        payload = validate_accepted_specification_root(
            spec_hash=self.accepted_specification_hash,
            canonical_specification_json=self.accepted_specification_json,
        )
        specification = AcceptedSpecificationReference(
            spec_version_id=self.accepted_specification_version_id,
            spec_hash=self.accepted_specification_hash,
            canonical_specification_json=self.accepted_specification_json,
            payload=payload,
        )
        parent_ids = validate_canonical_spec_item_ids(self.parent_backlog_spec_item_ids)
        canonical_spec_item_ids(specification, parent_ids)
        return self


def _story_sentinel_fields(
    items: Iterable[UserStoryAgentItem],
) -> tuple[str, ...]:
    """Return deterministic current-schema field paths with sentinel-only prose."""
    fields: list[str] = []
    for index, item in enumerate(items):
        prefix = f"story_items[{index}]"
        fields.extend(
            f"{prefix}.{field_name}"
            for field_name in _STORY_REQUIRED_PROSE_FIELDS
            if is_story_sentinel_text(getattr(item, field_name))
        )
        fields.extend(
            f"{prefix}.acceptance_criteria[{criterion_index}]"
            for criterion_index, criterion in enumerate(item.acceptance_criteria)
            if is_story_sentinel_text(criterion)
        )
        for dimension_name in _STORY_INVEST_DIMENSION_NAMES:
            dimension = getattr(item.invest_assessment, dimension_name)
            fields.extend(
                f"{prefix}.invest_assessment.{dimension_name}.{field_name}"
                for field_name in ("rationale", "evidence")
                if is_story_sentinel_text(getattr(dimension, field_name))
            )
    return tuple(fields)


def canonicalize_story_items(
    specification: AcceptedSpecificationReference,
    *,
    parent_backlog_spec_item_ids: Iterable[str],
    agent_items: Iterable[UserStoryAgentItem],
) -> tuple[StoryItemEnvelope, ...]:
    """Validate provider items, preserve their order, and mint host Story IDs."""
    items = tuple(agent_items)
    if not 1 <= len(items) <= _MAX_STORY_ITEMS:
        message = "Story output must contain one through eight items"
        raise ValueError(message)
    sentinel_fields = _story_sentinel_fields(items)
    if sentinel_fields:
        raise StorySentinelContentError(sentinel_fields)
    return _mint_story_items(
        specification,
        parent_backlog_spec_item_ids=parent_backlog_spec_item_ids,
        items=items,
    )


def inspect_story_items_for_review(
    specification: AcceptedSpecificationReference,
    *,
    parent_backlog_spec_item_ids: Iterable[str],
    agent_items: Iterable[UserStoryAgentItem],
) -> tuple[tuple[StoryItemEnvelope, ...], tuple[str, ...]]:
    """Validate immutable item structure and return unsafe authoring field paths."""
    items = tuple(agent_items)
    if not 1 <= len(items) <= _MAX_STORY_ITEMS:
        message = "Story output must contain one through eight items"
        raise ValueError(message)
    canonical_items = _mint_story_items(
        specification,
        parent_backlog_spec_item_ids=parent_backlog_spec_item_ids,
        items=items,
    )
    return canonical_items, _story_sentinel_fields(items)


def _mint_story_items(
    specification: AcceptedSpecificationReference,
    *,
    parent_backlog_spec_item_ids: Iterable[str],
    items: tuple[UserStoryAgentItem, ...],
) -> tuple[StoryItemEnvelope, ...]:
    """Mint exact host items after the caller selects an eligibility policy."""
    parent_ids = tuple(parent_backlog_spec_item_ids)
    canonical_spec_item_ids(specification, parent_ids)
    validated_reference_sets: list[tuple[str, ...]] = []
    invalid_reference_fields: list[str] = []
    for index, item in enumerate(items):
        try:
            validated_reference_sets.append(
                canonical_spec_item_ids(
                    specification,
                    item.spec_item_ids,
                    parent_spec_item_ids=parent_ids,
                )
            )
        except SpecificationReferenceError:
            invalid_reference_fields.append(f"story_items[{index}].spec_item_ids")
    if invalid_reference_fields:
        raise StoryReferenceContentError(invalid_reference_fields)

    canonical_items: list[StoryItemEnvelope] = []
    for ordinal, (item, spec_item_ids) in enumerate(
        zip(items, validated_reference_sets, strict=True),
        start=1,
    ):
        canonical_item = CanonicalStoryItem(
            story_item_id=f"US-{ordinal:04d}",
            story_title=item.story_title,
            statement=item.statement,
            persona=parse_story_persona(item.statement),
            acceptance_criteria=item.acceptance_criteria,
            spec_item_ids=spec_item_ids,
            invest_assessment=item.invest_assessment,
            estimated_effort=item.estimated_effort,
            effort_rationale=item.effort_rationale,
            order_rationale=item.order_rationale,
            produced_artifacts=item.produced_artifacts,
            research_caveats=item.research_caveats,
            dependency_candidates=item.dependency_candidates,
        )
        canonical_items.append(
            StoryItemEnvelope(
                item=canonical_item,
                item_fingerprint=canonical_hash(
                    canonical_item.model_dump(mode="json")
                ),
            )
        )
    return tuple(canonical_items)


__all__ = [
    "STORY_POINTS_BY_EFFORT",
    "CanonicalStoryItem",
    "CanonicalStoryOutput",
    "InvestDimensionAssessment",
    "StoryDependencyCandidate",
    "StoryInvestAssessment",
    "StoryItemEnvelope",
    "StoryReferenceContentError",
    "StoryReplacementSource",
    "StorySentinelContentError",
    "UserStoryAgentItem",
    "UserStoryWriterInput",
    "UserStoryWriterOutput",
    "canonicalize_story_items",
    "inspect_story_items_for_review",
    "is_story_sentinel_text",
    "parse_story_persona",
    "safe_story_validation_errors",
    "safe_story_validation_message",
    "story_output_sentinel_fields",
    "story_validation_error_sentinel_fields",
]
