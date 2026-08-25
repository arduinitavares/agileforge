"""Explicit validation of one accepted Story against its exact Specification."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sqlmodel import Session, col, select

from models.core import UserStory
from models.db import get_engine
from models.workflow import (
    BacklogArtifact,
    BacklogArtifactDecision,
    StoryArtifact,
    StoryArtifactDecision,
)
from services.contracts.specification_references import (
    AcceptedSpecificationReference,
    canonical_spec_item_ids,
    derived_referenced_spec_item_ids,
)
from services.contracts.specification_validation import (
    StorySpecificationFinding,
    StorySpecificationReviewInput,
    StorySpecificationReviewOutput,
)
from services.contracts.story import CanonicalStoryItem, CanonicalStoryOutput
from services.planning_artifact_content import (
    load_stored_backlog_planning_content,
    load_stored_planning_artifact_content,
)
from services.specs._engine_resolution import resolve_spec_engine
from services.specs.accepted_specification import (
    AcceptedSpecification,
    AcceptedSpecificationIntegrityError,
    load_accepted_specification,
    require_current_accepted_specification,
)
from utils.spec_schemas import StructuralValidationFailure, ValidationEvidence
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine

    from services.contracts.backlog import BacklogItem
    from workflow.contracts import JsonObject

StorySemanticReview = Callable[[StorySpecificationReviewInput], str]
_DEFAULT_GET_ENGINE = get_engine
_STRING_TUPLE = TypeAdapter(tuple[str, ...])
_VALIDATOR_VERSION = "2.0.0"


class ValidateStoryInput(BaseModel):
    """Caller-selected identity and safe validation mode only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    story_id: Annotated[int, Field(gt=0)]
    mode: Literal["structural", "hybrid"] = "structural"


class StoryValidationReadinessError(ValueError):
    """Stored validation evidence is absent, invalid, failed, or stale."""


@dataclass(frozen=True)
class _StructuralContext:
    """Loaded source facts and row-local values from one complete rule pass."""

    story: UserStory
    artifact: StoryArtifact | None
    story_output: CanonicalStoryOutput | None
    story_item: CanonicalStoryItem | None
    specification: AcceptedSpecification | None
    backlog_artifact: BacklogArtifact | None
    backlog_item: BacklogItem | None
    row_spec_item_ids: tuple[str, ...] | None
    row_acceptance_criteria: tuple[str, ...] | None
    failures: tuple[StructuralValidationFailure, ...]


@dataclass(frozen=True)
class _SemanticOutcome:
    """One one-shot semantic state plus its pre-call validation input."""

    state: Literal["not_requested", "valid", "invalid"]
    findings: tuple[StorySpecificationFinding, ...] = ()
    error: str | None = None
    expected_input_payload: JsonObject | None = None


def utc_now() -> datetime:
    """Return a timezone-aware UTC validation timestamp."""
    return datetime.now(UTC)


def _resolve_engine() -> Engine | Connection | None:
    """Resolve a test override or the current application engine."""
    return cast(
        "Engine | Connection | None",
        resolve_spec_engine(
            service_get_engine=get_engine,
            default_service_get_engine=_DEFAULT_GET_ENGINE,
        ),
    )


def _failure(code: str, message: str) -> StructuralValidationFailure:
    return StructuralValidationFailure.model_validate(
        {"code": code, "message": message}
    )


def _parse_canonical_string_tuple(raw: str) -> tuple[str, ...] | None:
    try:
        value = _STRING_TUPLE.validate_json(raw, strict=True)
    except ValidationError:
        return None
    if canonical_json(list(value)) != raw:
        return None
    return value


def story_validation_input_payload(  # noqa: PLR0913
    *,
    project_id: int,
    story_id: int,
    source_story_artifact_id: int,
    source_story_artifact_fingerprint: str,
    source_story_item_id: str,
    source_story_item_fingerprint: str,
    source_backlog_artifact_id: int,
    source_backlog_artifact_fingerprint: str,
    source_backlog_item_id: str,
    spec_version_id: int,
    spec_hash: str,
    spec_item_ids: tuple[str, ...],
    title: str,
    statement: str,
    persona: str,
    acceptance_criteria: tuple[str, ...],
    story_points: int | None,
    rank: str | None,
) -> JsonObject:
    """Build the sole canonical Story-validation input payload."""
    return cast(
        "JsonObject",
        {
            "schema_version": "agileforge.story-validation-input.v1",
            "project_id": project_id,
            "story_id": story_id,
            "source_story_artifact_id": source_story_artifact_id,
            "source_story_artifact_fingerprint": source_story_artifact_fingerprint,
            "source_story_item_id": source_story_item_id,
            "source_story_item_fingerprint": source_story_item_fingerprint,
            "source_backlog_artifact_id": source_backlog_artifact_id,
            "source_backlog_artifact_fingerprint": source_backlog_artifact_fingerprint,
            "source_backlog_item_id": source_backlog_item_id,
            "spec_version_id": spec_version_id,
            "spec_hash": spec_hash,
            "spec_item_ids": sorted(spec_item_ids),
            "title": title,
            "statement": statement,
            "persona": persona,
            "acceptance_criteria": list(acceptance_criteria),
            "story_points": story_points,
            "rank": rank,
        },
    )


def story_validation_input_fingerprint(  # noqa: PLR0913
    *,
    project_id: int,
    story_id: int,
    source_story_artifact_id: int,
    source_story_artifact_fingerprint: str,
    source_story_item_id: str,
    source_story_item_fingerprint: str,
    source_backlog_artifact_id: int,
    source_backlog_artifact_fingerprint: str,
    source_backlog_item_id: str,
    spec_version_id: int,
    spec_hash: str,
    spec_item_ids: tuple[str, ...],
    title: str,
    statement: str,
    persona: str,
    acceptance_criteria: tuple[str, ...],
    story_points: int | None,
    rank: str | None,
) -> str:
    """Return the canonical fingerprint of one Story-validation input."""
    return canonical_hash(
        story_validation_input_payload(
            project_id=project_id,
            story_id=story_id,
            source_story_artifact_id=source_story_artifact_id,
            source_story_artifact_fingerprint=source_story_artifact_fingerprint,
            source_story_item_id=source_story_item_id,
            source_story_item_fingerprint=source_story_item_fingerprint,
            source_backlog_artifact_id=source_backlog_artifact_id,
            source_backlog_artifact_fingerprint=source_backlog_artifact_fingerprint,
            source_backlog_item_id=source_backlog_item_id,
            spec_version_id=spec_version_id,
            spec_hash=spec_hash,
            spec_item_ids=spec_item_ids,
            title=title,
            statement=statement,
            persona=persona,
            acceptance_criteria=acceptance_criteria,
            story_points=story_points,
            rank=rank,
        )
    )


def _load_story_source(
    session: Session,
    story: UserStory,
) -> tuple[
    StoryArtifact | None,
    CanonicalStoryOutput | None,
    CanonicalStoryItem | None,
]:
    artifact = session.exec(
        select(StoryArtifact).where(
            col(StoryArtifact.project_id) == story.project_id,
            col(StoryArtifact.story_artifact_id) == story.source_story_artifact_id,
            col(StoryArtifact.content_fingerprint)
            == story.source_story_artifact_fingerprint,
        )
    ).one_or_none()
    if artifact is None:
        return None, None, None
    try:
        _content, output = load_stored_planning_artifact_content(
            artifact.canonical_content_json,
            expected_fingerprint=artifact.content_fingerprint,
            content_type=CanonicalStoryOutput,
        )
        item_ids = _parse_canonical_string_tuple(artifact.story_item_ids_json)
        expected_ids = tuple(item.item.story_item_id for item in output.story_items)
        if (
            not output.is_complete
            or output.clarifying_questions
            or item_ids is None
            or item_ids != expected_ids
        ):
            return artifact, None, None
    except (TypeError, ValueError, ValidationError):
        return artifact, None, None
    matches = tuple(
        envelope.item
        for envelope in output.story_items
        if envelope.item.story_item_id == story.source_story_item_id
    )
    return artifact, output, matches[0] if len(matches) == 1 else None


def _load_specification_parent(
    session: Session,
    story: UserStory,
    artifact: StoryArtifact,
) -> tuple[AcceptedSpecification, BacklogArtifact, BacklogItem]:
    specification = load_accepted_specification(
        session,
        project_id=story.project_id,
        spec_version_id=story.accepted_spec_version_id,
        spec_hash=story.accepted_spec_hash,
    )
    backlog = session.exec(
        select(BacklogArtifact).where(
            col(BacklogArtifact.project_id) == story.project_id,
            col(BacklogArtifact.backlog_artifact_id)
            == artifact.source_backlog_artifact_id,
            col(BacklogArtifact.content_fingerprint)
            == artifact.source_backlog_artifact_fingerprint,
        )
    ).one_or_none()
    if backlog is None:
        message = "Exact parent Backlog artifact was not found."
        raise ValueError(message)
    decision = session.exec(
        select(BacklogArtifactDecision).where(
            col(BacklogArtifactDecision.project_id) == story.project_id,
            col(BacklogArtifactDecision.backlog_artifact_id)
            == backlog.backlog_artifact_id,
            col(BacklogArtifactDecision.artifact_fingerprint)
            == backlog.content_fingerprint,
            col(BacklogArtifactDecision.decision) == "accepted",
        )
    ).one_or_none()
    if decision is None:
        message = "Exact parent Backlog artifact is not accepted."
        raise ValueError(message)
    if (
        backlog.spec_version_id != story.accepted_spec_version_id
        or backlog.spec_hash != story.accepted_spec_hash
        or artifact.source_backlog_artifact_id != backlog.backlog_artifact_id
        or artifact.source_backlog_artifact_fingerprint != backlog.content_fingerprint
    ):
        message = "Story and Backlog do not share the exact Specification root."
        raise ValueError(message)
    _content, backlog_output = load_stored_backlog_planning_content(
        backlog.canonical_content_json,
        expected_fingerprint=backlog.content_fingerprint,
        specification=specification,
    )
    matches = tuple(
        item
        for item in backlog_output.backlog_items
        if item.backlog_item_id == artifact.backlog_item_id
    )
    if len(matches) != 1:
        message = "Exact parent Backlog item was not found."
        raise ValueError(message)
    return specification, backlog, matches[0]


def _story_item_matches_row(
    story: UserStory,
    artifact: StoryArtifact,
    item: CanonicalStoryItem,
) -> bool:
    return (
        story.source_story_artifact_id == artifact.story_artifact_id
        and story.source_story_artifact_fingerprint == artifact.content_fingerprint
        and story.source_story_item_id == item.story_item_id
        and story.title == item.story_title
        and story.story_description == item.statement
        and story.persona == item.persona
        and story.acceptance_criteria_json
        == canonical_json(list(item.acceptance_criteria))
        and story.spec_item_ids_json == canonical_json(list(item.spec_item_ids))
    )


def _evaluate_structural(  # noqa: C901
    session: Session,
    story: UserStory,
) -> _StructuralContext:
    failures: list[StructuralValidationFailure] = []
    decision = session.exec(
        select(StoryArtifactDecision).where(
            col(StoryArtifactDecision.project_id) == story.project_id,
            col(StoryArtifactDecision.story_artifact_id)
            == story.source_story_artifact_id,
            col(StoryArtifactDecision.artifact_fingerprint)
            == story.source_story_artifact_fingerprint,
            col(StoryArtifactDecision.decision) == "accepted",
        )
    ).one_or_none()
    if decision is None:
        failures.append(
            _failure(
                "STORY_ACCEPTANCE_INVALID",
                "Story does not resolve to one exact accepted Story decision.",
            )
        )

    artifact, story_output, story_item = _load_story_source(session, story)
    item_binding_invalid = (
        artifact is None or story_output is None or story_item is None
    )
    if not item_binding_invalid and artifact is not None and story_item is not None:
        item_fingerprint = next(
            envelope.item_fingerprint
            for envelope in story_output.story_items
            if envelope.item.story_item_id == story_item.story_item_id
        )
        item_binding_invalid = (
            story.source_story_item_fingerprint != item_fingerprint
            or not _story_item_matches_row(story, artifact, story_item)
        )
    if item_binding_invalid:
        failures.append(
            _failure(
                "STORY_ITEM_BINDING_INVALID",
                "Story row does not match its exact immutable Story item.",
            )
        )

    specification: AcceptedSpecification | None = None
    backlog_artifact: BacklogArtifact | None = None
    backlog_item: BacklogItem | None = None
    if artifact is not None and story_output is not None:
        try:
            specification, backlog_artifact, backlog_item = _load_specification_parent(
                session, story, artifact
            )
        except (
            AcceptedSpecificationIntegrityError,
            TypeError,
            ValueError,
            ValidationError,
        ):
            failures.append(
                _failure(
                    "SPECIFICATION_BINDING_INVALID",
                    "Story, Backlog, and accepted Specification lineage is invalid.",
                )
            )

    row_spec_item_ids = _parse_canonical_string_tuple(story.spec_item_ids_json)
    references_invalid = (
        row_spec_item_ids is None
        or not row_spec_item_ids
        or tuple(sorted(set(row_spec_item_ids))) != row_spec_item_ids
    )
    if story_item is not None and row_spec_item_ids != story_item.spec_item_ids:
        references_invalid = True
    if (
        row_spec_item_ids is not None
        and specification is not None
        and backlog_item is not None
    ):
        try:
            canonical_spec_item_ids(
                AcceptedSpecificationReference(
                    spec_version_id=specification.spec_version_id,
                    spec_hash=specification.spec_hash,
                    canonical_specification_json=(
                        specification.canonical_specification_json
                    ),
                    payload=specification.payload,
                ),
                row_spec_item_ids,
                parent_spec_item_ids=backlog_item.spec_item_ids,
            )
        except (TypeError, ValueError, ValidationError):
            references_invalid = True
    if references_invalid:
        failures.append(
            _failure(
                "SPEC_ITEM_REFERENCES_INVALID",
                "Story Specification-item references are not exact and canonical.",
            )
        )

    statement = story.story_description.strip().replace("*", "").casefold()
    if (
        not statement.startswith(("as a ", "as an ", "as the "))
        or " i want " not in statement
        or " so that " not in statement
    ):
        failures.append(
            _failure(
                "STORY_STATEMENT_INVALID",
                (
                    "Story statement must use the closed persona, desire, and "
                    "outcome shape."
                ),
            )
        )

    row_acceptance_criteria = _parse_canonical_string_tuple(
        story.acceptance_criteria_json
    )
    criteria_invalid = (
        row_acceptance_criteria is None
        or not row_acceptance_criteria
        or any(not criterion.strip() for criterion in row_acceptance_criteria)
    )
    if (
        story_item is not None
        and row_acceptance_criteria != story_item.acceptance_criteria
    ):
        criteria_invalid = True
    if criteria_invalid:
        failures.append(
            _failure(
                "ACCEPTANCE_CRITERIA_INVALID",
                "Story acceptance criteria must be exact, ordered, and non-empty.",
            )
        )
    return _StructuralContext(
        story=story,
        artifact=artifact,
        story_output=story_output,
        story_item=story_item,
        specification=specification,
        backlog_artifact=backlog_artifact,
        backlog_item=backlog_item,
        row_spec_item_ids=row_spec_item_ids,
        row_acceptance_criteria=row_acceptance_criteria,
        failures=tuple(failures),
    )


def _validation_input_payload(context: _StructuralContext) -> JsonObject | None:
    story = context.story
    artifact = context.artifact
    criteria = context.row_acceptance_criteria
    spec_item_ids = context.row_spec_item_ids
    if (
        story.story_id is None
        or artifact is None
        or artifact.story_artifact_id is None
        or criteria is None
        or spec_item_ids is None
    ):
        return None
    return story_validation_input_payload(
        project_id=story.project_id,
        story_id=story.story_id,
        source_story_artifact_id=artifact.story_artifact_id,
        source_story_artifact_fingerprint=artifact.content_fingerprint,
        source_story_item_id=story.source_story_item_id,
        source_story_item_fingerprint=story.source_story_item_fingerprint,
        source_backlog_artifact_id=artifact.source_backlog_artifact_id,
        source_backlog_artifact_fingerprint=(
            artifact.source_backlog_artifact_fingerprint
        ),
        source_backlog_item_id=artifact.backlog_item_id,
        spec_version_id=story.accepted_spec_version_id,
        spec_hash=story.accepted_spec_hash,
        spec_item_ids=spec_item_ids,
        title=story.title,
        statement=story.story_description,
        persona=story.persona,
        acceptance_criteria=criteria,
        story_points=story.story_points,
        rank=story.rank,
    )


def _validation_input_fingerprint(context: _StructuralContext) -> str | None:
    payload = _validation_input_payload(context)
    return None if payload is None else canonical_hash(payload)


def compute_story_validation_input_fingerprint(
    session: Session,
    *,
    story: UserStory,
) -> str:
    """Rebuild the exact closed fingerprint from current controlled values."""
    fingerprint = _validation_input_fingerprint(_evaluate_structural(session, story))
    if fingerprint is None:
        message = (
            "Story validation input identities or canonical content are unavailable."
        )
        raise StoryValidationReadinessError(message)
    return fingerprint


def _semantic_review_input(
    context: _StructuralContext,
) -> StorySpecificationReviewInput:
    if (
        context.specification is None
        or context.backlog_item is None
        or context.story_item is None
    ):
        message = "Exact semantic review source context is unavailable."
        raise ValueError(message)
    return StorySpecificationReviewInput(
        schema_version="agileforge.story-specification-review-input.v1",
        accepted_specification_version_id=context.specification.spec_version_id,
        accepted_specification_hash=context.specification.spec_hash,
        accepted_specification_json=(
            context.specification.canonical_specification_json
        ),
        parent_backlog_item_id=context.backlog_item.backlog_item_id,
        parent_backlog_spec_item_ids=context.backlog_item.spec_item_ids,
        story=context.story_item,
    )


def _parse_semantic_result(
    raw_text: str,
    *,
    context: _StructuralContext,
) -> tuple[StorySpecificationFinding, ...]:
    output = StorySpecificationReviewOutput.model_validate_json(raw_text, strict=True)
    if context.story_item is None or context.backlog_item is None:
        message = "Semantic review source bounds are unavailable."
        raise ValueError(message)
    story_ids = frozenset(context.story_item.spec_item_ids)
    parent_ids = frozenset(context.backlog_item.spec_item_ids)
    for finding in output.findings:
        allowed_ids = parent_ids if finding.code == "SPEC_ITEM_OMISSION" else story_ids
        if finding.spec_item_id not in allowed_ids:
            message = "Semantic finding is outside its exact source boundary."
            raise ValueError(message)
    return tuple(
        sorted(output.findings, key=lambda item: (item.spec_item_id, item.code))
    )


def _source_reference_ids(context: _StructuralContext) -> tuple[str, ...] | None:
    if context.story_item is not None:
        return context.story_item.spec_item_ids
    if context.row_spec_item_ids:
        return tuple(sorted(set(context.row_spec_item_ids)))
    return None


def _build_evidence(
    context: _StructuralContext,
    *,
    mode: Literal["structural", "hybrid"],
    validated_at: datetime,
    semantic_review_state: Literal["not_requested", "valid", "invalid"],
    semantic_findings: tuple[StorySpecificationFinding, ...],
) -> ValidationEvidence | None:
    story = context.story
    artifact = context.artifact
    input_fingerprint = _validation_input_fingerprint(context)
    source_ids = _source_reference_ids(context)
    if (
        story.story_id is None
        or artifact is None
        or artifact.story_artifact_id is None
        or input_fingerprint is None
        or source_ids is None
    ):
        return None
    reference_ids = derived_referenced_spec_item_ids(
        source_ids,
        (finding.spec_item_id for finding in semantic_findings),
    )
    return ValidationEvidence(
        schema_version="agileforge.story-validation-evidence.v3",
        project_id=story.project_id,
        story_id=story.story_id,
        source_story_artifact_id=artifact.story_artifact_id,
        source_story_artifact_fingerprint=artifact.content_fingerprint,
        source_story_item_id=story.source_story_item_id,
        source_story_item_fingerprint=story.source_story_item_fingerprint,
        source_backlog_artifact_id=artifact.source_backlog_artifact_id,
        source_backlog_artifact_fingerprint=(
            artifact.source_backlog_artifact_fingerprint
        ),
        source_backlog_item_id=artifact.backlog_item_id,
        spec_version_id=story.accepted_spec_version_id,
        spec_hash=story.accepted_spec_hash,
        validated_at=validated_at,
        story_validation_input_fingerprint=input_fingerprint,
        validator_version=_VALIDATOR_VERSION,
        mode=mode,
        structurally_eligible=not context.failures,
        structural_failures=context.failures,
        structural_warnings=(),
        semantic_review_state=semantic_review_state,
        semantic_findings=semantic_findings,
        referenced_spec_item_ids=reference_ids,
    )


def _begin_validation_write(session: Session) -> None:
    """Acquire the existing SQLite writer lock before the final source read."""
    if session.get_bind().dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")


def _story_not_found_result(story_id: int) -> JsonObject:
    return {
        "success": False,
        "error_code": "STORY_NOT_FOUND",
        "message": f"Story {story_id} was not found.",
    }


def _validation_source_stale_result(
    *,
    story_id: int,
    mode: Literal["structural", "hybrid"],
) -> JsonObject:
    return {
        "success": False,
        "error_code": "STORY_VALIDATION_SOURCE_STALE",
        "message": "Story validation source changed before evidence persistence.",
        "story_id": story_id,
        "mode": mode,
        "ready_for_sprint": False,
    }


def _validation_result(
    *,
    story_id: int,
    mode: Literal["structural", "hybrid"],
    context: _StructuralContext,
    evidence: ValidationEvidence | None,
    semantic: _SemanticOutcome,
) -> JsonObject:
    result: JsonObject = {
        "success": True,
        "story_id": story_id,
        "mode": mode,
        "ready_for_sprint": evidence.structurally_eligible if evidence else False,
        "structural_failures": [
            item.model_dump(mode="json") for item in context.failures
        ],
        "structural_warnings": [],
        "semantic_review_state": semantic.state,
        "semantic_findings": [
            item.model_dump(mode="json") for item in semantic.findings
        ],
        "validation_evidence": (
            evidence.model_dump(mode="json") if evidence is not None else None
        ),
    }
    if semantic.error is not None:
        result["semantic_error"] = semantic.error
    return result


def _finalize_validation_in_session(
    session: Session,
    *,
    parsed: ValidateStoryInput,
    semantic: _SemanticOutcome,
    now: Callable[[], datetime],
) -> JsonObject:
    """Reload and persist one final-state snapshot within an existing session."""
    story = session.get(UserStory, parsed.story_id, populate_existing=True)
    if story is None:
        return _story_not_found_result(parsed.story_id)
    with session.no_autoflush:
        context = _evaluate_structural(session, story)
    current_input_payload = _validation_input_payload(context)
    if semantic.expected_input_payload is not None and (
        context.failures
        or current_input_payload is None
        or canonical_json(current_input_payload)
        != canonical_json(semantic.expected_input_payload)
    ):
        return _validation_source_stale_result(
            story_id=parsed.story_id,
            mode=parsed.mode,
        )
    validated_at = now()
    evidence = _build_evidence(
        context,
        mode=parsed.mode,
        validated_at=validated_at,
        semantic_review_state=semantic.state,
        semantic_findings=semantic.findings,
    )
    if evidence is not None:
        story.validation_evidence = canonical_json(evidence.model_dump(mode="json"))
        story.updated_at = validated_at
        session.add(story)
        session.flush()
    return _validation_result(
        story_id=parsed.story_id,
        mode=parsed.mode,
        context=context,
        evidence=evidence,
        semantic=semantic,
    )


def _finalize_validation(
    *,
    engine: Engine | Connection | None,
    parsed: ValidateStoryInput,
    semantic: _SemanticOutcome,
    now: Callable[[], datetime],
) -> JsonObject:
    """Reload and persist one final-state snapshot under the writer lock."""
    with Session(engine) as session:
        _begin_validation_write(session)
        result = _finalize_validation_in_session(
            session,
            parsed=parsed,
            semantic=semantic,
            now=now,
        )
        if result.get("success", False):
            session.commit()
        else:
            session.rollback()
        return result


def validate_story_with_specification_in_session(
    session: Session,
    params: ValidateStoryInput | Mapping[str, object],
    *,
    semantic_review: StorySemanticReview | None = None,
    now: Callable[[], datetime] = utc_now,
) -> JsonObject:
    """Validate one exact accepted Story within a caller-owned session transaction."""
    parsed = ValidateStoryInput.model_validate(params)
    if parsed.mode == "structural":
        return _finalize_validation_in_session(
            session,
            parsed=parsed,
            semantic=_SemanticOutcome(state="not_requested"),
            now=now,
        )

    expected_input_payload: JsonObject | None = None
    review_input: StorySpecificationReviewInput | None = None
    story = session.get(UserStory, parsed.story_id)
    if story is None:
        return _story_not_found_result(parsed.story_id)
    with session.no_autoflush:
        context = _evaluate_structural(session, story)
    if not context.failures and semantic_review is not None:
        expected_input_payload = _validation_input_payload(context)
        review_input = _semantic_review_input(context)

    review_adapter = semantic_review
    if review_input is None or review_adapter is None:
        semantic = _SemanticOutcome(
            state="invalid",
            error="STORY_SPECIFICATION_REVIEW_INVALID",
        )
    else:
        try:
            raw_text = review_adapter(review_input)
            semantic = _SemanticOutcome(
                state="valid",
                findings=_parse_semantic_result(raw_text, context=context),
                expected_input_payload=expected_input_payload,
            )
        except Exception:  # noqa: BLE001
            semantic = _SemanticOutcome(
                state="invalid",
                error="STORY_SPECIFICATION_REVIEW_INVALID",
                expected_input_payload=expected_input_payload,
            )
    return _finalize_validation_in_session(
        session,
        parsed=parsed,
        semantic=semantic,
        now=now,
    )


def validate_story_with_specification(
    params: ValidateStoryInput | Mapping[str, object],
    *,
    semantic_review: StorySemanticReview | None = None,
    now: Callable[[], datetime] = utc_now,
) -> JsonObject:
    """Validate one exact accepted Story; hybrid is explicit and one-shot."""
    parsed = ValidateStoryInput.model_validate(params)
    engine = _resolve_engine()
    if parsed.mode == "structural":
        return _finalize_validation(
            engine=engine,
            parsed=parsed,
            semantic=_SemanticOutcome(state="not_requested"),
            now=now,
        )

    expected_input_payload: JsonObject | None = None
    review_input: StorySpecificationReviewInput | None = None
    with Session(engine) as session:
        story = session.get(UserStory, parsed.story_id)
        if story is None:
            return _story_not_found_result(parsed.story_id)
        with session.no_autoflush:
            context = _evaluate_structural(session, story)
        if not context.failures and semantic_review is not None:
            expected_input_payload = _validation_input_payload(context)
            review_input = _semantic_review_input(context)

    review_adapter = semantic_review
    if review_input is None or review_adapter is None:
        semantic = _SemanticOutcome(
            state="invalid",
            error="STORY_SPECIFICATION_REVIEW_INVALID",
        )
    else:
        try:
            raw_text = review_adapter(review_input)
            semantic = _SemanticOutcome(
                state="valid",
                findings=_parse_semantic_result(raw_text, context=context),
                expected_input_payload=expected_input_payload,
            )
        except Exception:  # noqa: BLE001
            semantic = _SemanticOutcome(
                state="invalid",
                error="STORY_SPECIFICATION_REVIEW_INVALID",
                expected_input_payload=expected_input_payload,
            )
    return _finalize_validation(
        engine=engine,
        parsed=parsed,
        semantic=semantic,
        now=now,
    )


def require_story_validation_evidence(
    session: Session,
    *,
    story: UserStory,
    require_current_spec: bool,
) -> ValidationEvidence:
    """Prove complete canonical evidence against exact immutable Story sources."""
    raw_evidence = story.validation_evidence
    if raw_evidence is None:
        message = "Story validation evidence is required."
        raise StoryValidationReadinessError(message)
    try:
        evidence = ValidationEvidence.model_validate_json(raw_evidence, strict=True)
    except ValidationError as error:
        message = "Story validation evidence is not strict v3."
        raise StoryValidationReadinessError(message) from error
    if canonical_json(evidence.model_dump(mode="json")) != raw_evidence:
        message = "Story validation evidence bytes are not canonical."
        raise StoryValidationReadinessError(message)
    with session.no_autoflush:
        context = _evaluate_structural(session, story)
    input_fingerprint = _validation_input_fingerprint(context)
    source_ids = _source_reference_ids(context)
    artifact = context.artifact
    if (
        story.story_id is None
        or artifact is None
        or artifact.story_artifact_id is None
        or input_fingerprint is None
        or source_ids is None
        or context.failures
    ):
        message = "Story validation source context is invalid."
        raise StoryValidationReadinessError(message)
    expected_references = derived_referenced_spec_item_ids(
        source_ids,
        (finding.spec_item_id for finding in evidence.semantic_findings),
    )
    exact_identities = (
        evidence.project_id == story.project_id
        and evidence.story_id == story.story_id
        and evidence.source_story_artifact_id == artifact.story_artifact_id
        and evidence.source_story_artifact_fingerprint == artifact.content_fingerprint
        and evidence.source_story_item_id == story.source_story_item_id
        and evidence.source_story_item_fingerprint
        == story.source_story_item_fingerprint
        and evidence.source_backlog_artifact_id == artifact.source_backlog_artifact_id
        and evidence.source_backlog_artifact_fingerprint
        == artifact.source_backlog_artifact_fingerprint
        and evidence.source_backlog_item_id == artifact.backlog_item_id
        and evidence.spec_version_id == story.accepted_spec_version_id
        and evidence.spec_hash == story.accepted_spec_hash
        and evidence.story_validation_input_fingerprint == input_fingerprint
        and evidence.validator_version == _VALIDATOR_VERSION
        and evidence.referenced_spec_item_ids == expected_references
    )
    if not exact_identities or not evidence.structurally_eligible:
        message = "Story validation evidence is failed or stale."
        raise StoryValidationReadinessError(message)
    if require_current_spec:
        try:
            require_current_accepted_specification(
                session,
                project_id=story.project_id,
                spec_version_id=story.accepted_spec_version_id,
                spec_hash=story.accepted_spec_hash,
            )
        except AcceptedSpecificationIntegrityError as error:
            raise StoryValidationReadinessError(str(error)) from error
    return evidence


def require_story_ready_for_sprint(
    session: Session,
    *,
    story: UserStory,
) -> ValidationEvidence:
    """Require deep validation evidence against the current Specification."""
    return require_story_validation_evidence(
        session,
        story=story,
        require_current_spec=True,
    )


__all__ = [
    "StorySemanticReview",
    "StoryValidationReadinessError",
    "ValidateStoryInput",
    "compute_story_validation_input_fingerprint",
    "require_story_ready_for_sprint",
    "require_story_validation_evidence",
    "story_validation_input_fingerprint",
    "story_validation_input_payload",
    "validate_story_with_specification",
]
