"""Immutable Story artifact persistence and accepted-item activation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from sqlmodel import Session, col, select

from models.core import Sprint, SprintStory, UserStory
from models.enums import StoryStatus
from models.workflow import (
    BacklogArtifact,
    RoadmapArtifact,
    StoryArtifact,
    StoryArtifactDecision,
)
from services.agent_workbench.backlog_phase import _require_current_root
from services.agent_workbench.roadmap_phase import (
    _current_accepted_backlog,
    _roadmap_lineage_nodes,
)
from services.contracts.specification_references import (
    AcceptedSpecificationReference,
)
from services.contracts.story import (
    STORY_POINTS_BY_EFFORT,
    CanonicalStoryOutput,
    StoryItemEnvelope,
    UserStoryAgentItem,
    canonicalize_story_items,
)
from services.planning_artifact_content import (
    load_stored_backlog_planning_content,
    load_stored_planning_artifact_content,
    load_stored_roadmap_planning_content,
    validate_canonical_planning_content,
)
from services.planning_lineage import (
    ArtifactLineageNode,
    PlanningLineageError,
    accepted_ancestor_ids,
    next_artifact_version,
    select_current_accepted_artifact,
)
from services.specs.story_validation_service import (
    compute_story_validation_input_fingerprint,
    validate_story_with_specification_in_session,
)
from services.story_rank import parse_story_rank
from utils.spec_schemas import ValidationEvidence
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from datetime import datetime

    from services.contracts.backlog import BacklogItem
    from services.planning_lineage import Decision
    from services.specs.accepted_specification import AcceptedSpecification
    from workflow.contracts import JsonObject


_STORY_POINTS: dict[str, int] = STORY_POINTS_BY_EFFORT


@dataclass(frozen=True)
class RecordStoryDraftInput:
    """Exact immutable values used to record one complete Story draft."""

    project_id: int
    source_backlog_artifact_id: int
    source_backlog_artifact_fingerprint: str
    backlog_item_id: str
    roadmap_artifact_id: int
    roadmap_artifact_fingerprint: str
    canonical_content: JsonObject
    content_fingerprint: str
    supersedes_story_artifact_id: int | None
    actor: str
    recorded_at: datetime


@dataclass(frozen=True)
class RecordStoryDecisionInput:
    """Exact append-only values used to decide one complete Story draft."""

    artifact: StoryArtifact
    decision: str
    rationale: str
    reviewer: str
    idempotency_key: str
    decided_at: datetime


@dataclass(frozen=True)
class RecordStoryDecisionResult:
    """Terminal decision plus the stable operational rows it activated."""

    decision: StoryArtifactDecision
    activated_story_ids: tuple[int, ...]


@dataclass(frozen=True)
class _StoryParentContext:
    """Strict current parent rows and content for one Story chain."""

    backlog: BacklogArtifact
    backlog_item: BacklogItem
    roadmap: RoadmapArtifact
    specification: AcceptedSpecification


@dataclass(frozen=True)
class StoryCorrectionTarget:
    """Exact accepted operational row and immutable artifact item it projects."""

    story: UserStory
    artifact: StoryArtifact
    content: CanonicalStoryOutput
    item: StoryItemEnvelope


def _required_id(value: int | None, *, label: str) -> int:
    if value is None:
        message = f"{label} has no durable identity."
        raise ValueError(message)
    return value


def _story_lineage_nodes(
    session: Session,
    *,
    project_id: int,
) -> tuple[ArtifactLineageNode, ...]:
    artifacts = session.exec(
        select(StoryArtifact).where(col(StoryArtifact.project_id) == project_id)
    ).all()
    decisions = session.exec(
        select(StoryArtifactDecision).where(
            col(StoryArtifactDecision.project_id) == project_id
        )
    ).all()
    artifacts_by_id = {
        _required_id(row.story_artifact_id, label="Story artifact"): row
        for row in artifacts
    }
    decisions_by_artifact: dict[int, Decision] = {}
    for decision in decisions:
        artifact = artifacts_by_id.get(decision.story_artifact_id)
        if (
            artifact is None
            or artifact.content_fingerprint != decision.artifact_fingerprint
            or decision.story_artifact_id in decisions_by_artifact
            or decision.decision not in {"accepted", "feedback", "rejected"}
        ):
            message = "Stored Story decision lineage is invalid."
            raise ValueError(message)
        decisions_by_artifact[decision.story_artifact_id] = cast(
            "Decision", decision.decision
        )
    return tuple(
        ArtifactLineageNode(
            artifact_id=artifact_id,
            chain_key=(
                row.project_id,
                row.source_backlog_artifact_id,
                row.backlog_item_id,
            ),
            version_number=row.version_number,
            supersedes_artifact_id=row.supersedes_story_artifact_id,
            decision=decisions_by_artifact.get(artifact_id),
        )
        for artifact_id, row in artifacts_by_id.items()
    )


def _story_parent_context(  # noqa: PLR0913
    session: Session,
    *,
    project_id: int,
    source_backlog_artifact_id: int,
    source_backlog_artifact_fingerprint: str,
    backlog_item_id: str,
    roadmap_artifact_id: int,
    roadmap_artifact_fingerprint: str,
    require_current_roadmap: bool = True,
) -> _StoryParentContext:
    backlog, _parent_item_ids = _current_accepted_backlog(
        session,
        project_id=project_id,
        backlog_artifact_id=source_backlog_artifact_id,
        backlog_artifact_fingerprint=source_backlog_artifact_fingerprint,
    )
    specification = _require_current_root(
        session,
        project_id=project_id,
        spec_version_id=backlog.spec_version_id,
        spec_hash=backlog.spec_hash,
        product_goal_artifact_id=backlog.product_goal_artifact_id,
        product_goal_fingerprint=backlog.product_goal_fingerprint,
    )
    _backlog_json, backlog_content = load_stored_backlog_planning_content(
        backlog.canonical_content_json,
        expected_fingerprint=backlog.content_fingerprint,
        specification=specification,
    )
    backlog_items = tuple(
        item
        for item in backlog_content.backlog_items
        if item.backlog_item_id == backlog_item_id
    )
    if len(backlog_items) != 1:
        message = "Story source Backlog item is missing or duplicated."
        raise ValueError(message)

    roadmap = session.exec(
        select(RoadmapArtifact).where(
            col(RoadmapArtifact.project_id) == project_id,
            col(RoadmapArtifact.roadmap_artifact_id) == roadmap_artifact_id,
            col(RoadmapArtifact.content_fingerprint) == roadmap_artifact_fingerprint,
            col(RoadmapArtifact.backlog_artifact_id) == source_backlog_artifact_id,
            col(RoadmapArtifact.backlog_artifact_fingerprint)
            == source_backlog_artifact_fingerprint,
        )
    ).one_or_none()
    if roadmap is None:
        message = "Story source Roadmap does not match one exact Backlog parent."
        raise ValueError(message)
    try:
        roadmap_nodes = _roadmap_lineage_nodes(session, project_id=project_id)
        current_roadmap = select_current_accepted_artifact(
            roadmap_nodes,
            chain_key=(
                project_id,
                source_backlog_artifact_id,
                source_backlog_artifact_fingerprint,
            ),
        )
    except PlanningLineageError as error:
        raise ValueError(str(error)) from error
    accepted_roadmap_ids = {
        current_roadmap.artifact_id,
        *accepted_ancestor_ids(roadmap_nodes),
    }
    if (
        require_current_roadmap and current_roadmap.artifact_id != roadmap_artifact_id
    ) or (
        not require_current_roadmap and roadmap_artifact_id not in accepted_roadmap_ids
    ):
        message = "Story requires the sole current accepted Roadmap parent."
        raise ValueError(message)
    _roadmap_json, roadmap_content = load_stored_roadmap_planning_content(
        roadmap.canonical_content_json,
        expected_fingerprint=roadmap.content_fingerprint,
        parent_backlog_item_ids=tuple(
            item.backlog_item_id for item in backlog_content.backlog_items
        ),
    )
    occurrences = sum(
        release.backlog_item_ids.count(backlog_item_id)
        for release in roadmap_content.roadmap_releases
    )
    if occurrences != 1:
        message = "Story source Roadmap must contain the exact Backlog item once."
        raise ValueError(message)
    return _StoryParentContext(
        backlog=backlog,
        backlog_item=backlog_items[0],
        roadmap=roadmap,
        specification=specification,
    )


def validate_story_planning_content(
    canonical_content: JsonObject,
    *,
    content_fingerprint: str,
    specification: AcceptedSpecification,
    backlog_item: BacklogItem,
) -> CanonicalStoryOutput:
    """Validate one complete closed Story artifact against its exact parents."""
    content = validate_canonical_planning_content(
        canonical_content,
        content_type=CanonicalStoryOutput,
    )
    if canonical_hash(canonical_content) != content_fingerprint:
        message = "Story content fingerprint does not match canonical content."
        raise ValueError(message)
    if (
        not content.is_complete
        or not content.story_items
        or content.clarifying_questions
    ):
        message = "Story output is incomplete and cannot enter review."
        raise ValueError(message)
    provider_items = tuple(
        UserStoryAgentItem.model_validate(
            envelope.item.model_dump(
                mode="json",
                exclude={"story_item_id", "persona"},
            )
        )
        for envelope in content.story_items
    )
    canonical_items = canonicalize_story_items(
        AcceptedSpecificationReference(
            spec_version_id=specification.spec_version_id,
            spec_hash=specification.spec_hash,
            canonical_specification_json=specification.canonical_specification_json,
            payload=specification.payload,
        ),
        parent_backlog_spec_item_ids=backlog_item.spec_item_ids,
        agent_items=provider_items,
    )
    if canonical_items != content.story_items:
        message = "Story items do not match the exact host-minted canonical sequence."
        raise ValueError(message)
    return content


def load_stored_story_planning_content(
    canonical_content_json: str,
    *,
    expected_fingerprint: str,
    specification: AcceptedSpecification,
    backlog_item: BacklogItem,
) -> tuple[JsonObject, CanonicalStoryOutput]:
    """Load exact immutable Story bytes against their pinned parent evidence."""
    canonical_content, _content = load_stored_planning_artifact_content(
        canonical_content_json,
        expected_fingerprint=expected_fingerprint,
        content_type=CanonicalStoryOutput,
    )
    content = validate_story_planning_content(
        canonical_content,
        content_fingerprint=expected_fingerprint,
        specification=specification,
        backlog_item=backlog_item,
    )
    return canonical_content, content


def _load_story_content(
    artifact: StoryArtifact,
    *,
    parent: _StoryParentContext,
) -> CanonicalStoryOutput:
    _canonical_content, content = load_stored_story_planning_content(
        artifact.canonical_content_json,
        expected_fingerprint=artifact.content_fingerprint,
        specification=parent.specification,
        backlog_item=parent.backlog_item,
    )
    item_ids = tuple(envelope.item.story_item_id for envelope in content.story_items)
    if artifact.story_item_ids_json != canonical_json(list(item_ids)):
        message = "Story artifact item IDs are not the exact canonical sequence."
        raise ValueError(message)
    return content


def load_story_correction_target_in_session(
    session: Session,
    *,
    project_id: int,
    story_id: int,
) -> StoryCorrectionTarget:
    """Prove one active row is an exact item of the current accepted Story leaf."""
    story = session.get(UserStory, story_id)
    if story is None or story.project_id != project_id or story.is_superseded:
        message = "Story correction target is missing, foreign, or superseded."
        raise ValueError(message)
    artifact = session.exec(
        select(StoryArtifact).where(
            col(StoryArtifact.project_id) == project_id,
            col(StoryArtifact.story_artifact_id) == story.source_story_artifact_id,
            col(StoryArtifact.content_fingerprint)
            == story.source_story_artifact_fingerprint,
        )
    ).one_or_none()
    if artifact is None:
        message = "Story correction target has no exact immutable artifact."
        raise ValueError(message)
    decision = session.exec(
        select(StoryArtifactDecision).where(
            col(StoryArtifactDecision.project_id) == project_id,
            col(StoryArtifactDecision.story_artifact_id)
            == story.source_story_artifact_id,
        )
    ).one_or_none()
    if (
        decision is None
        or decision.decision != "accepted"
        or decision.artifact_fingerprint != artifact.content_fingerprint
    ):
        message = "Story correction requires one exact accepted artifact decision."
        raise ValueError(message)
    parent = _story_parent_context(
        session,
        project_id=project_id,
        source_backlog_artifact_id=artifact.source_backlog_artifact_id,
        source_backlog_artifact_fingerprint=(
            artifact.source_backlog_artifact_fingerprint
        ),
        backlog_item_id=artifact.backlog_item_id,
        roadmap_artifact_id=artifact.roadmap_artifact_id,
        roadmap_artifact_fingerprint=artifact.roadmap_artifact_fingerprint,
    )
    content = _load_story_content(artifact, parent=parent)
    chain_key = (
        project_id,
        artifact.source_backlog_artifact_id,
        artifact.backlog_item_id,
    )
    try:
        accepted = select_current_accepted_artifact(
            _story_lineage_nodes(session, project_id=project_id),
            chain_key=chain_key,
        )
    except PlanningLineageError as error:
        raise ValueError(str(error)) from error
    artifact_id = _required_id(artifact.story_artifact_id, label="Story artifact")
    if accepted.artifact_id != artifact_id:
        message = "Story correction target is not the current accepted artifact."
        raise ValueError(message)
    matches = tuple(
        envelope
        for envelope in content.story_items
        if envelope.item.story_item_id == story.source_story_item_id
        and envelope.item_fingerprint == story.source_story_item_fingerprint
    )
    if len(matches) != 1:
        message = "Story correction target item identity is missing or ambiguous."
        raise ValueError(message)
    item = matches[0]
    ordinal = content.story_items.index(item) + 1
    expected_rank = str((parent.backlog_item.priority * 100) + ordinal)
    if (
        story.accepted_spec_version_id != parent.specification.spec_version_id
        or story.accepted_spec_hash != parent.specification.spec_hash
        or story.spec_item_ids_json != canonical_json(list(item.item.spec_item_ids))
        or story.title != item.item.story_title
        or story.story_description != item.item.statement
        or story.acceptance_criteria_json
        != canonical_json(list(item.item.acceptance_criteria))
        or story.persona != item.item.persona
        or story.story_points != _STORY_POINTS[item.item.estimated_effort]
        or story.rank != expected_rank
    ):
        message = "Story correction target row drifted from its immutable item."
        raise ValueError(message)
    return StoryCorrectionTarget(
        story=story,
        artifact=artifact,
        content=content,
        item=item,
    )


def record_story_draft_in_session(
    session: Session,
    *,
    inputs: RecordStoryDraftInput,
) -> StoryArtifact:
    """Append one complete immutable Story artifact and no operational rows."""
    parent = _story_parent_context(
        session,
        project_id=inputs.project_id,
        source_backlog_artifact_id=inputs.source_backlog_artifact_id,
        source_backlog_artifact_fingerprint=(
            inputs.source_backlog_artifact_fingerprint
        ),
        backlog_item_id=inputs.backlog_item_id,
        roadmap_artifact_id=inputs.roadmap_artifact_id,
        roadmap_artifact_fingerprint=inputs.roadmap_artifact_fingerprint,
    )
    content = validate_story_planning_content(
        inputs.canonical_content,
        content_fingerprint=inputs.content_fingerprint,
        specification=parent.specification,
        backlog_item=parent.backlog_item,
    )
    chain_key = (
        inputs.project_id,
        inputs.source_backlog_artifact_id,
        inputs.backlog_item_id,
    )
    duplicate = session.exec(
        select(StoryArtifact).where(
            col(StoryArtifact.project_id) == inputs.project_id,
            col(StoryArtifact.source_backlog_artifact_id)
            == inputs.source_backlog_artifact_id,
            col(StoryArtifact.backlog_item_id) == inputs.backlog_item_id,
            col(StoryArtifact.content_fingerprint) == inputs.content_fingerprint,
        )
    ).first()
    if duplicate is not None:
        message = "Story lineage cannot repeat identical content in one chain."
        raise ValueError(message)
    try:
        version_number = next_artifact_version(
            _story_lineage_nodes(session, project_id=inputs.project_id),
            chain_key=chain_key,
            supersedes_id=inputs.supersedes_story_artifact_id,
        )
    except PlanningLineageError as error:
        raise ValueError(str(error)) from error
    row = StoryArtifact(
        project_id=inputs.project_id,
        source_backlog_artifact_id=inputs.source_backlog_artifact_id,
        source_backlog_artifact_fingerprint=(
            inputs.source_backlog_artifact_fingerprint
        ),
        backlog_item_id=inputs.backlog_item_id,
        roadmap_artifact_id=inputs.roadmap_artifact_id,
        roadmap_artifact_fingerprint=inputs.roadmap_artifact_fingerprint,
        version_number=version_number,
        canonical_content_json=canonical_json(inputs.canonical_content),
        content_fingerprint=inputs.content_fingerprint,
        story_item_ids_json=canonical_json(
            [envelope.item.story_item_id for envelope in content.story_items]
        ),
        supersedes_story_artifact_id=inputs.supersedes_story_artifact_id,
        created_by=inputs.actor,
        created_at=inputs.recorded_at,
    )
    session.add(row)
    session.flush()
    return row


def _materialize_story_rows(
    session: Session,
    *,
    artifact: StoryArtifact,
    content: CanonicalStoryOutput,
    parent: _StoryParentContext,
    accepted_at: datetime,
) -> tuple[int, ...]:
    artifact_id = _required_id(artifact.story_artifact_id, label="Story artifact")
    chain_key = (
        artifact.project_id,
        artifact.source_backlog_artifact_id,
        artifact.backlog_item_id,
    )
    nodes = tuple(
        node
        for node in _story_lineage_nodes(session, project_id=artifact.project_id)
        if node.chain_key == chain_key
    )
    try:
        superseded_artifact_ids = accepted_ancestor_ids(nodes)
    except PlanningLineageError as error:
        raise ValueError(str(error)) from error
    prior_rows = session.exec(
        select(UserStory).where(
            col(UserStory.project_id) == artifact.project_id,
            col(UserStory.source_story_artifact_id).in_(superseded_artifact_ids),
            col(UserStory.is_superseded).is_(False),
        )
    ).all()
    for prior in prior_rows:
        prior.is_superseded = True
        prior.updated_at = accepted_at
        session.add(prior)

    rows: list[UserStory] = []
    for ordinal, envelope in enumerate(content.story_items, start=1):
        item = envelope.item
        rank = str((parent.backlog_item.priority * 100) + ordinal)
        parse_story_rank(rank)
        row = UserStory(
            project_id=artifact.project_id,
            source_story_artifact_id=artifact_id,
            source_story_artifact_fingerprint=artifact.content_fingerprint,
            source_story_item_id=item.story_item_id,
            source_story_item_fingerprint=envelope.item_fingerprint,
            accepted_spec_version_id=parent.specification.spec_version_id,
            accepted_spec_hash=parent.specification.spec_hash,
            spec_item_ids_json=canonical_json(list(item.spec_item_ids)),
            title=item.story_title,
            story_description=item.statement,
            acceptance_criteria_json=canonical_json(list(item.acceptance_criteria)),
            persona=item.persona,
            status=StoryStatus.TO_DO,
            story_points=_STORY_POINTS[item.estimated_effort],
            rank=rank,
            is_superseded=False,
            validation_evidence=None,
            created_at=accepted_at,
            updated_at=accepted_at,
        )
        session.add(row)
        rows.append(row)
    session.flush()
    story_ids = tuple(_required_id(row.story_id, label="User Story") for row in rows)
    for story_id in story_ids:
        validate_story_with_specification_in_session(
            session,
            {"story_id": story_id, "mode": "structural"},
            now=lambda accepted_at=accepted_at: accepted_at,
        )
    return story_ids


def record_story_decision_in_session(
    session: Session,
    *,
    inputs: RecordStoryDecisionInput,
) -> RecordStoryDecisionResult:
    """Append one terminal decision and atomically activate accepted items."""
    if inputs.decision not in {"accepted", "rejected", "feedback"}:
        message = "Story decision is invalid."
        raise ValueError(message)
    artifact_id = _required_id(
        inputs.artifact.story_artifact_id,
        label="Story artifact",
    )
    artifact = session.exec(
        select(StoryArtifact).where(
            col(StoryArtifact.project_id) == inputs.artifact.project_id,
            col(StoryArtifact.story_artifact_id) == artifact_id,
            col(StoryArtifact.content_fingerprint)
            == inputs.artifact.content_fingerprint,
        )
    ).one_or_none()
    if artifact is None:
        message = "Story decision does not match one exact artifact."
        raise ValueError(message)
    if (
        session.exec(
            select(StoryArtifactDecision).where(
                col(StoryArtifactDecision.project_id) == artifact.project_id,
                col(StoryArtifactDecision.story_artifact_id) == artifact_id,
            )
        ).one_or_none()
        is not None
    ):
        message = "Story artifact already has a terminal decision."
        raise ValueError(message)
    parent = _story_parent_context(
        session,
        project_id=artifact.project_id,
        source_backlog_artifact_id=artifact.source_backlog_artifact_id,
        source_backlog_artifact_fingerprint=(
            artifact.source_backlog_artifact_fingerprint
        ),
        backlog_item_id=artifact.backlog_item_id,
        roadmap_artifact_id=artifact.roadmap_artifact_id,
        roadmap_artifact_fingerprint=artifact.roadmap_artifact_fingerprint,
    )
    content = _load_story_content(artifact, parent=parent)
    chain_key = (
        artifact.project_id,
        artifact.source_backlog_artifact_id,
        artifact.backlog_item_id,
    )
    try:
        next_artifact_version(
            _story_lineage_nodes(session, project_id=artifact.project_id),
            chain_key=chain_key,
            supersedes_id=artifact_id,
        )
    except PlanningLineageError as error:
        raise ValueError(str(error)) from error
    decision = StoryArtifactDecision(
        project_id=artifact.project_id,
        story_artifact_id=artifact_id,
        artifact_fingerprint=artifact.content_fingerprint,
        decision=inputs.decision,
        rationale=inputs.rationale,
        reviewer=inputs.reviewer,
        idempotency_key=inputs.idempotency_key,
        decided_at=inputs.decided_at,
    )
    session.add(decision)
    session.flush()
    activated_story_ids = (
        _materialize_story_rows(
            session,
            artifact=artifact,
            content=content,
            parent=parent,
            accepted_at=inputs.decided_at,
        )
        if inputs.decision == "accepted"
        else ()
    )
    return RecordStoryDecisionResult(
        decision=decision,
        activated_story_ids=activated_story_ids,
    )


def prove_story_decision_winner_in_session(  # noqa: PLR0911
    session: Session,
    *,
    project_id: int,
    story_artifact_id: int,
    artifact_fingerprint: str,
) -> bool:
    """Prove a committed decision winner and its complete activation projection."""
    try:
        artifact = session.exec(
            select(StoryArtifact).where(
                col(StoryArtifact.project_id) == project_id,
                col(StoryArtifact.story_artifact_id) == story_artifact_id,
                col(StoryArtifact.content_fingerprint) == artifact_fingerprint,
            )
        ).one_or_none()
        decision = session.exec(
            select(StoryArtifactDecision).where(
                col(StoryArtifactDecision.project_id) == project_id,
                col(StoryArtifactDecision.story_artifact_id) == story_artifact_id,
            )
        ).one_or_none()
        if (
            artifact is None
            or decision is None
            or decision.artifact_fingerprint != artifact_fingerprint
            or decision.decision not in {"accepted", "feedback", "rejected"}
        ):
            return False
        rows = _story_artifact_rows(session, artifact=artifact)
        if decision.decision != "accepted":
            return not rows
        if not _story_projection_matches_artifact(
            session,
            artifact=artifact,
            decision=decision,
            expected_superseded=False,
            require_current_roadmap=True,
            require_fresh_activation=True,
        ):
            return False
        chain_key = (
            artifact.project_id,
            artifact.source_backlog_artifact_id,
            artifact.backlog_item_id,
        )
        nodes = tuple(
            node
            for node in _story_lineage_nodes(session, project_id=project_id)
            if node.chain_key == chain_key
        )
        current = select_current_accepted_artifact(nodes, chain_key=chain_key)
        if current.artifact_id != story_artifact_id:
            return False
        superseded_artifact_ids = accepted_ancestor_ids(nodes)
        for ancestor_id in superseded_artifact_ids:
            ancestor = session.get(StoryArtifact, ancestor_id)
            ancestor_decision = session.exec(
                select(StoryArtifactDecision).where(
                    col(StoryArtifactDecision.project_id) == project_id,
                    col(StoryArtifactDecision.story_artifact_id) == ancestor_id,
                )
            ).one_or_none()
            if (
                ancestor is None
                or ancestor_decision is None
                or ancestor_decision.decision != "accepted"
                or ancestor_decision.artifact_fingerprint
                != ancestor.content_fingerprint
                or not _story_projection_matches_artifact(
                    session,
                    artifact=ancestor,
                    decision=ancestor_decision,
                    expected_superseded=True,
                    require_current_roadmap=False,
                    require_fresh_activation=False,
                )
            ):
                return False
        return True  # noqa: TRY300
    except (PlanningLineageError, ValueError):
        return False


def _story_artifact_rows(
    session: Session,
    *,
    artifact: StoryArtifact,
) -> list[UserStory]:
    return list(
        session.exec(
            select(UserStory).where(
                col(UserStory.project_id) == artifact.project_id,
                col(UserStory.source_story_artifact_id) == artifact.story_artifact_id,
            )
        ).all()
    )


def _story_projection_matches_artifact(  # noqa: PLR0913
    session: Session,
    *,
    artifact: StoryArtifact,
    decision: StoryArtifactDecision,
    expected_superseded: bool,
    require_current_roadmap: bool,
    require_fresh_activation: bool,
) -> bool:
    parent = _story_parent_context(
        session,
        project_id=artifact.project_id,
        source_backlog_artifact_id=artifact.source_backlog_artifact_id,
        source_backlog_artifact_fingerprint=(
            artifact.source_backlog_artifact_fingerprint
        ),
        backlog_item_id=artifact.backlog_item_id,
        roadmap_artifact_id=artifact.roadmap_artifact_id,
        roadmap_artifact_fingerprint=artifact.roadmap_artifact_fingerprint,
        require_current_roadmap=require_current_roadmap,
    )
    content = _load_story_content(artifact, parent=parent)
    rows = _story_artifact_rows(session, artifact=artifact)
    if len(rows) != len(content.story_items):
        return False
    by_item_id = {row.source_story_item_id: row for row in rows}
    if len(by_item_id) != len(rows):
        return False
    return all(
        _story_row_matches_item(
            session,
            by_item_id.get(envelope.item.story_item_id),
            artifact=artifact,
            envelope=envelope,
            parent=parent,
            ordinal=ordinal,
            accepted_at=decision.decided_at,
            expected_superseded=expected_superseded,
            require_fresh_activation=require_fresh_activation,
        )
        for ordinal, envelope in enumerate(content.story_items, start=1)
    )


def _story_row_matches_item(  # noqa: PLR0913
    session: Session,
    row: UserStory | None,
    *,
    artifact: StoryArtifact,
    envelope: StoryItemEnvelope,
    parent: _StoryParentContext,
    ordinal: int,
    accepted_at: datetime,
    expected_superseded: bool = False,
    require_fresh_activation: bool = True,
) -> bool:
    """Compare every immutable materialized field with one canonical item."""
    if row is None:
        return False
    item = envelope.item
    immutable_projection_matches = (
        row.project_id == artifact.project_id
        and row.source_story_artifact_id == artifact.story_artifact_id
        and row.source_story_artifact_fingerprint == artifact.content_fingerprint
        and row.source_story_item_id == item.story_item_id
        and row.source_story_item_fingerprint == envelope.item_fingerprint
        and row.accepted_spec_version_id == parent.specification.spec_version_id
        and row.accepted_spec_hash == parent.specification.spec_hash
        and row.spec_item_ids_json == canonical_json(list(item.spec_item_ids))
        and row.title == item.story_title
        and row.story_description == item.statement
        and row.acceptance_criteria_json
        == canonical_json(list(item.acceptance_criteria))
        and row.persona == item.persona
        and row.is_superseded is expected_superseded
        and row.created_at == accepted_at
    )
    if not immutable_projection_matches:
        return False
    return not require_fresh_activation or (
        row.story_points == _STORY_POINTS[item.estimated_effort]
        and row.rank == str((parent.backlog_item.priority * 100) + ordinal)
        and row.status is StoryStatus.TO_DO
        and _acceptance_evidence_is_current(
            session=session,
            story=row,
            accepted_at=accepted_at,
        )
    )


def _acceptance_evidence_is_current(
    *,
    session: Session,
    story: UserStory,
    accepted_at: datetime,
) -> bool:
    """Prove a fresh activation retained exact v3 structural evidence."""
    raw_evidence = story.validation_evidence
    if raw_evidence is None:
        return False
    try:
        evidence = ValidationEvidence.model_validate_json(raw_evidence, strict=True)
        current_fingerprint = compute_story_validation_input_fingerprint(
            session,
            story=story,
        )
    except ValueError:
        return False
    return (
        raw_evidence == canonical_json(evidence.model_dump(mode="json"))
        and evidence.mode == "structural"
        and evidence.validated_at.replace(tzinfo=None) == accepted_at.replace(
            tzinfo=None
        )
        and evidence.story_validation_input_fingerprint == current_fingerprint
    )


def repair_story_readiness_in_session(
    session: Session,
    *,
    project_id: int,
    repairs: tuple[tuple[int, int, str], ...],
    repaired_at: datetime,
) -> tuple[int, ...]:
    """Repair exact Story points and rank under the caller-owned transaction."""
    for _story_id, _story_points, rank in repairs:
        parse_story_rank(rank)
    _assert_repair_readiness_safe_in_session(session, project_id=project_id)
    story_ids = tuple(item[0] for item in repairs)
    rows = session.exec(
        select(UserStory).where(col(UserStory.story_id).in_(story_ids))
    ).all()
    by_id = {item.story_id: item for item in rows if item.story_id is not None}
    if set(by_id) != set(story_ids):
        message = "Story readiness repair does not target exact Project stories."
        raise ValueError(message)
    for story_id, story_points, rank in repairs:
        story = by_id[story_id]
        if story.project_id != project_id or story.is_superseded:
            message = "Story readiness repair targets an inactive Story."
            raise ValueError(message)
        story.story_points = story_points
        story.rank = rank
        story.updated_at = repaired_at
        session.add(story)
    session.flush()
    return tuple(sorted(story_ids))


def _assert_repair_readiness_safe_in_session(
    session: Session,
    *,
    project_id: int,
) -> None:
    """Block Story readiness repair if current rows already feed any Sprint."""
    active_story_ids = [
        story_id
        for story_id in session.exec(
            select(UserStory.story_id).where(
                UserStory.project_id == project_id,
                UserStory.is_superseded == False,  # noqa: E712
            )
        ).all()
        if story_id is not None
    ]
    if not active_story_ids:
        return
    sprint_link = session.exec(
        select(SprintStory.story_id)
        .join(Sprint, col(Sprint.sprint_id) == col(SprintStory.sprint_id))
        .where(
            Sprint.project_id == project_id,
            col(SprintStory.story_id).in_(active_story_ids),
        )
    ).first()
    if sprint_link is not None:
        message = "Story readiness repair is unsafe after Sprint work exists."
        raise ValueError(message)


__all__ = [
    "RecordStoryDecisionInput",
    "RecordStoryDecisionResult",
    "RecordStoryDraftInput",
    "StoryCorrectionTarget",
    "load_stored_story_planning_content",
    "load_story_correction_target_in_session",
    "prove_story_decision_winner_in_session",
    "record_story_decision_in_session",
    "record_story_draft_in_session",
    "repair_story_readiness_in_session",
    "validate_story_planning_content",
]
