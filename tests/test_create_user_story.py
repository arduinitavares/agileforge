"""Whole-Story draft persistence and accepted-artifact activation."""

from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, col, create_engine, select

import services.application as application_module
from models.core import Sprint, SprintStory, Team, UserStory
from models.enums import SprintStatus
from models.events import WorkflowEvent
from models.workflow import (
    BacklogArtifact,
    RoadmapArtifact,
    StoryArtifact,
    StoryArtifactDecision,
    WorkflowNodeAttempt,
    WorkflowTransitionReceipt,
)
from services.agent_workbench import story_phase
from services.agent_workbench.roadmap_phase import (
    RecordRoadmapDecisionInput,
    RecordRoadmapDraftInput,
    record_roadmap_decision_in_session,
    record_roadmap_draft_in_session,
)
from services.agent_workbench.story_phase import (
    RecordStoryDecisionInput,
    RecordStoryDecisionResult,
    RecordStoryDraftInput,
    prove_story_decision_winner_in_session,
    record_story_decision_in_session,
    record_story_draft_in_session,
)
from services.application import (
    AgenticActionRequest,
    AgileForgeApplication,
    DeliveryActionInputService,
    DeliveryActionRequest,
    StoryCorrectionRequest,
)
from services.contracts.story import (
    CanonicalStoryItem,
    CanonicalStoryOutput,
    InvestDimensionAssessment,
    StoryInvestAssessment,
    StoryItemEnvelope,
)
from services.node_attempt_replay import (
    DurableNodeAttemptReplayService,
    NodeAttemptReplayQuery,
)
from services.specs.story_validation_service import (
    StorySemanticReview,
    ValidateStoryInput,
)
from tests.workflow.test_planning_transitions import (
    EVALUATED_AT,
    _accepted_backlog,
    _domain,
    _guards,
    _roadmap_content,
    _seed_accepted_backlog,
)
from utils.spec_schemas import ValidationEvidence
from workflow.contracts import JsonObject, TransitionResult
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.handlers.planning import (
    _is_story_decision_uniqueness_race,
    execute_decide_story,
)
from workflow.requests import DecideStory, RecordStoryDraft, StartNodeAttempt

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from sqlalchemy.engine import Engine


def _invest_assessment() -> StoryInvestAssessment:
    return StoryInvestAssessment(
        independent=InvestDimensionAssessment(
            result="pass",
            rationale="Delivers self-contained increment.",
            evidence="No unbuilt dependencies.",
        ),
        negotiable=InvestDimensionAssessment(
            result="pass",
            rationale="Implementation details open to refinement.",
            evidence="Focuses on user outcome.",
        ),
        valuable=InvestDimensionAssessment(
            result="pass",
            rationale="Directly delivers user capability.",
            evidence="Addresses requirement.",
        ),
        estimable=InvestDimensionAssessment(
            result="pass",
            rationale="Scope is clear and bounded.",
            evidence="Discrete criteria.",
        ),
        small=InvestDimensionAssessment(
            result="pass",
            rationale="Sized for single iteration.",
            evidence="Effort is M.",
        ),
        testable=InvestDimensionAssessment(
            result="pass",
            rationale="Verifiable pass/fail criteria.",
            evidence="Observable verification steps.",
        ),
    )


def _story_content(
    *,
    title: str = "Persist planning facts",
    item_count: int = 1,
) -> JsonObject:
    items = tuple(
        CanonicalStoryItem(
            story_item_id=f"US-{ordinal:04d}",
            story_title=title if item_count == 1 else f"{title} {ordinal}",
            statement=(
                "As an operator, I want durable planning facts, so that routing "
                + (
                    "survives restarts."
                    if item_count == 1
                    else f"survives restarts for item {ordinal}."
                )
            ),
            persona="operator",
            acceptance_criteria=(
                "- Preserve the first criterion.\nIncluding its exact continuation.",
                "Preserve Unicode: ação.",
            ),
            spec_item_ids=("REQ.planning-1",),
            invest_assessment=_invest_assessment(),
            estimated_effort="M",
            effort_rationale="Moderate complexity storage routine.",
            order_rationale=f"Item {ordinal} sequencing within parent PBI.",
            produced_artifacts=("planning records",),
            research_caveats=(),
            dependency_candidates=(),
        )
        for ordinal in range(1, item_count + 1)
    )
    output = CanonicalStoryOutput(
        story_items=tuple(
            StoryItemEnvelope(
                item=item,
                item_fingerprint=canonical_hash(item.model_dump(mode="json")),
            )
            for item in items
        ),
        is_complete=True,
        clarifying_questions=(),
    )
    return output.model_dump(mode="json")


def _legacy_story_content(*, requirement_index: int) -> JsonObject:
    """Return the closed pre-#221 Story shape retained by accepted artifacts."""
    item: JsonObject = {
        "story_item_id": "US-0001",
        "story_title": f"Legacy planning Story {requirement_index}",
        "statement": (
            "As an operator, I want durable planning facts, so that legacy "
            f"correction {requirement_index} remains available."
        ),
        "persona": "operator",
        "acceptance_criteria": ["Preserve the accepted legacy Story."],
        "spec_item_ids": [f"REQ.planning-{requirement_index}"],
        "invest_score": "High",
        "estimated_effort": "M",
        "produced_artifacts": ["planning records"],
        "research_caveats": [],
        "decomposition_warning": None,
        "dependency_candidates": [],
    }
    return {
        "story_items": [
            {
                "item": item,
                "item_fingerprint": canonical_hash(item),
            }
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }


def _record_accepted_legacy_story(  # noqa: PLR0913
    session: Session,
    *,
    project_id: int,
    roadmap_id: int,
    backlog_item_id: str,
    requirement_index: int,
    legacy_content: JsonObject | None = None,
) -> StoryArtifact:
    """Seed one historical accepted Story artifact without current-schema parsing."""
    backlog = session.exec(
        select(BacklogArtifact).where(BacklogArtifact.project_id == project_id)
    ).one()
    roadmap = session.get(RoadmapArtifact, roadmap_id)
    assert roadmap is not None
    content = legacy_content or _legacy_story_content(
        requirement_index=requirement_index
    )
    artifact = StoryArtifact(
        project_id=project_id,
        source_backlog_artifact_id=int(backlog.backlog_artifact_id or 0),
        source_backlog_artifact_fingerprint=backlog.content_fingerprint,
        backlog_item_id=backlog_item_id,
        roadmap_artifact_id=roadmap_id,
        roadmap_artifact_fingerprint=roadmap.content_fingerprint,
        version_number=1,
        canonical_content_json=canonical_json(content),
        content_fingerprint=canonical_hash(content),
        story_item_ids_json=canonical_json(["US-0001"]),
        created_by="operator@example.com",
        created_at=EVALUATED_AT,
    )
    session.add(artifact)
    session.flush()
    assert artifact.story_artifact_id is not None
    session.add(
        StoryArtifactDecision(
            project_id=project_id,
            story_artifact_id=artifact.story_artifact_id,
            artifact_fingerprint=artifact.content_fingerprint,
            decision="accepted",
            rationale="Accepted before the current Story schema.",
            reviewer="operator@example.com",
            idempotency_key=f"accept-legacy-{backlog_item_id}",
            decided_at=EVALUATED_AT,
        )
    )
    return artifact


def _seed_story_parent(
    engine: Engine,
    *,
    requirements: tuple[str, ...] = ("Plan immutable work",),
) -> tuple[int, int]:
    project_id = _seed_accepted_backlog(engine, requirements=requirements)
    backlog = _accepted_backlog(engine, project_id)
    content = _roadmap_content(*requirements)
    with Session(engine) as session:
        roadmap = record_roadmap_draft_in_session(
            session,
            inputs=RecordRoadmapDraftInput(
                project_id=project_id,
                backlog_artifact_id=int(backlog.backlog_artifact_id or 0),
                backlog_artifact_fingerprint=backlog.content_fingerprint,
                canonical_content=content,
                content_fingerprint=canonical_hash(content),
                supersedes_roadmap_artifact_id=None,
                actor="operator@example.com",
                recorded_at=EVALUATED_AT,
            ),
        )
        record_roadmap_decision_in_session(
            session,
            inputs=RecordRoadmapDecisionInput(
                artifact=roadmap,
                decision="accepted",
                rationale="Accepted Roadmap.",
                reviewer="operator@example.com",
                idempotency_key="accept-story-parent",
                decided_at=EVALUATED_AT,
            ),
        )
        session.commit()
        assert roadmap.roadmap_artifact_id is not None
        return project_id, roadmap.roadmap_artifact_id


def _record_story(  # noqa: PLR0913
    session: Session,
    *,
    project_id: int,
    roadmap_id: int,
    title: str = "Persist planning facts",
    item_count: int = 1,
    supersedes_id: int | None = None,
    recorded_offset: int = 1,
) -> StoryArtifact:
    backlog = session.exec(
        select(BacklogArtifact).where(BacklogArtifact.project_id == project_id)
    ).one()
    content = _story_content(title=title, item_count=item_count)
    return record_story_draft_in_session(
        session,
        inputs=RecordStoryDraftInput(
            project_id=project_id,
            source_backlog_artifact_id=int(backlog.backlog_artifact_id or 0),
            source_backlog_artifact_fingerprint=backlog.content_fingerprint,
            backlog_item_id="PBI-000001",
            roadmap_artifact_id=roadmap_id,
            roadmap_artifact_fingerprint=canonical_hash(_roadmap_content()),
            canonical_content=content,
            content_fingerprint=canonical_hash(content),
            supersedes_story_artifact_id=supersedes_id,
            actor="operator@example.com",
            recorded_at=EVALUATED_AT + timedelta(seconds=recorded_offset),
        ),
    )


def _decide_story(
    session: Session,
    artifact: StoryArtifact,
    *,
    decision: str,
    offset: int,
) -> RecordStoryDecisionResult:
    return record_story_decision_in_session(
        session,
        inputs=RecordStoryDecisionInput(
            artifact=artifact,
            decision=decision,
            rationale=f"Review {decision}.",
            reviewer="operator@example.com",
            idempotency_key=f"story-{offset}-{decision}",
            decided_at=EVALUATED_AT + timedelta(seconds=offset),
        ),
    )


def test_story_draft_persists_only_one_complete_immutable_artifact(
    engine: Engine,
) -> None:
    """A draft must not create an operational row or duplicate saved event."""
    project_id, roadmap_id = _seed_story_parent(engine)

    with Session(engine) as session:
        artifact = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
        )
        session.flush()

        assert artifact.version_number == 1
        assert artifact.story_item_ids_json == canonical_json(["US-0001"])
        assert session.exec(select(StoryArtifact)).all() == [artifact]
        assert session.exec(select(UserStory)).all() == []
        assert session.exec(select(WorkflowEvent)).all() == []
        with pytest.raises(ValueError, match="cannot repeat identical content"):
            _record_story(
                session,
                project_id=project_id,
                roadmap_id=roadmap_id,
                supersedes_id=artifact.story_artifact_id,
                recorded_offset=2,
            )
        assert session.exec(select(StoryArtifact)).all() == [artifact]


def test_acceptance_materializes_exact_rows_but_feedback_does_not(
    engine: Engine,
) -> None:
    """Feedback is append-only; acceptance activates exact canonical item bytes."""
    project_id, roadmap_id = _seed_story_parent(engine)

    with Session(engine) as session:
        feedback = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
            title="Feedback-only candidate",
        )
        feedback_result = _decide_story(
            session,
            feedback,
            decision="feedback",
            offset=2,
        )
        assert feedback_result.activated_story_ids == ()
        assert session.exec(select(UserStory)).all() == []

        accepted = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
            title="Accepted candidate",
            supersedes_id=feedback.story_artifact_id,
            recorded_offset=3,
        )
        accepted_result = _decide_story(
            session,
            accepted,
            decision="accepted",
            offset=4,
        )
        assert len(accepted_result.activated_story_ids) == 1
        story = session.get(UserStory, accepted_result.activated_story_ids[0])
        assert story is not None
        assert story.source_story_artifact_id == accepted.story_artifact_id
        assert story.source_story_artifact_fingerprint == accepted.content_fingerprint
        assert story.source_story_item_id == "US-0001"
        assert story.accepted_spec_version_id > 0
        assert story.accepted_spec_hash.startswith("sha256:")
        assert story.spec_item_ids_json == canonical_json(["REQ.planning-1"])
        assert story.title == "Accepted candidate"
        assert story.story_description == (
            "As an operator, I want durable planning facts, so that routing "
            "survives restarts."
        )
        assert story.persona == "operator"
        assert story.acceptance_criteria_json == canonical_json(
            [
                "- Preserve the first criterion.\nIncluding its exact continuation.",
                "Preserve Unicode: ação.",
            ]
        )
        assert story.story_points == 3  # noqa: PLR2004
        assert story.rank == "101"
        assert story.validation_evidence is not None


@pytest.mark.parametrize("intermediate_decision", ["feedback", "rejected"])
def test_accepted_a_terminal_b_accepted_c_switches_only_on_c_acceptance(
    engine: Engine,
    intermediate_decision: str,
) -> None:
    """A remains selectable through terminal B; C acceptance switches atomically."""
    project_id, roadmap_id = _seed_story_parent(engine)

    with Session(engine) as session:
        artifact_a = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
            title="Accepted A",
        )
        accepted_a = _decide_story(
            session,
            artifact_a,
            decision="accepted",
            offset=2,
        )
        story_a = session.get(UserStory, accepted_a.activated_story_ids[0])
        assert story_a is not None
        a_bytes = artifact_a.canonical_content_json
        team = Team(name="Pinned Story Team")
        session.add(team)
        session.flush()
        assert team.team_id is not None
        sprint = Sprint(
            project_id=project_id,
            team_id=team.team_id,
            status=SprintStatus.ACTIVE,
        )
        session.add(sprint)
        session.flush()
        assert sprint.sprint_id is not None
        session.add(
            SprintStory(
                sprint_id=sprint.sprint_id,
                story_id=int(story_a.story_id or 0),
            )
        )

        artifact_b = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
            title="Feedback B",
            supersedes_id=artifact_a.story_artifact_id,
            recorded_offset=3,
        )
        _decide_story(
            session,
            artifact_b,
            decision=intermediate_decision,
            offset=4,
        )
        session.refresh(story_a)
        assert story_a.is_superseded is False
        assert [
            row.story_id
            for row in session.exec(
                select(UserStory).where(UserStory.project_id == project_id)
            ).all()
        ] == [story_a.story_id]
        assert artifact_a.canonical_content_json == a_bytes

        artifact_c = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
            title="Accepted C",
            supersedes_id=artifact_b.story_artifact_id,
            recorded_offset=5,
        )
        accepted_c = _decide_story(
            session,
            artifact_c,
            decision="accepted",
            offset=6,
        )
        session.refresh(story_a)
        assert story_a.is_superseded is True
        story_c = session.get(UserStory, accepted_c.activated_story_ids[0])
        assert story_c is not None
        assert story_c.is_superseded is False
        assert story_c.validation_evidence is not None
        assert session.exec(
            select(SprintStory).where(SprintStory.story_id == story_c.story_id)
        ).all() == []
        pinned = session.exec(
            select(SprintStory).where(
                SprintStory.sprint_id == sprint.sprint_id,
                SprintStory.story_id == story_a.story_id,
            )
        ).one_or_none()
        assert pinned is not None
        assert artifact_a.canonical_content_json == a_bytes
        assert [
            item.version_number for item in session.exec(select(StoryArtifact))
        ] == [1, 2, 3]
        assert len(session.exec(select(StoryArtifactDecision)).all()) == 3  # noqa: PLR2004


def test_story_acceptance_materializes_exact_eight_item_sequence(
    engine: Engine,
) -> None:
    """Activate the maximum closed Story artifact without placeholders or loss."""
    project_id, roadmap_id = _seed_story_parent(engine)
    with Session(engine) as session:
        artifact = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
            title="Bounded item",
            item_count=8,
        )
        result = _decide_story(session, artifact, decision="accepted", offset=2)
        rows = session.exec(select(UserStory).order_by(col(UserStory.story_id))).all()

    assert len(result.activated_story_ids) == 8  # noqa: PLR2004
    assert tuple(row.story_id for row in rows) == result.activated_story_ids
    assert [row.source_story_item_id for row in rows] == [
        f"US-{ordinal:04d}" for ordinal in range(1, 9)
    ]
    assert [row.rank for row in rows] == [str(100 + ordinal) for ordinal in range(1, 9)]


def test_story_activation_failure_rolls_back_supersession_decision_and_rows(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restore accepted A if multi-item C activation fails after row insertion."""
    project_id, roadmap_id = _seed_story_parent(engine)
    with Session(engine) as session:
        artifact_a = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
            title="Accepted A",
        )
        accepted_a = _decide_story(session, artifact_a, decision="accepted", offset=2)
        artifact_c = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
            title="Replacement C",
            item_count=3,
            supersedes_id=artifact_a.story_artifact_id,
            recorded_offset=3,
        )
        session.commit()
        artifact_c_id = int(artifact_c.story_artifact_id or 0)
        story_a_id = accepted_a.activated_story_ids[0]

    original = story_phase._materialize_story_rows

    def fail_after_materialization(
        session: Session,
        *,
        artifact: StoryArtifact,
        content: CanonicalStoryOutput,
        parent: story_phase._StoryParentContext,
        accepted_at: datetime,
    ) -> None:
        original(
            session,
            artifact=artifact,
            content=content,
            parent=parent,
            accepted_at=accepted_at,
        )
        message = "forced failure after replacement rows"
        raise RuntimeError(message)

    monkeypatch.setattr(
        story_phase,
        "_materialize_story_rows",
        fail_after_materialization,
    )
    with Session(engine) as session:
        replacement = session.get(StoryArtifact, artifact_c_id)
        assert replacement is not None
        with pytest.raises(RuntimeError, match="forced failure"):
            _decide_story(session, replacement, decision="accepted", offset=4)
        session.rollback()

    with Session(engine) as session:
        story_a = session.get(UserStory, story_a_id)
        assert story_a is not None
        assert story_a.is_superseded is False
        assert (
            session.exec(
                select(StoryArtifactDecision).where(
                    StoryArtifactDecision.story_artifact_id == artifact_c_id
                )
            ).all()
            == []
        )
        assert (
            session.exec(
                select(UserStory).where(
                    UserStory.source_story_artifact_id == artifact_c_id
                )
            ).all()
            == []
        )


def test_story_correction_request_and_host_resolution_bind_exact_accepted_item(  # noqa: PLR0915
    engine: Engine,
) -> None:
    """Resolve only semantic story/guidance input into closed host correction proof."""
    for invalid in (0, -1):
        with pytest.raises(ValidationError):
            StoryCorrectionRequest(
                project_id=1,
                story_id=invalid,
                guidance="Correct it.",
                idempotency_key="invalid-story",
                actor="operator@example.com",
            )
    with pytest.raises(ValidationError):
        StoryCorrectionRequest(
            project_id=1,
            story_id=1,
            guidance="   ",
            idempotency_key="invalid-guidance",
            actor="operator@example.com",
        )

    project_id, roadmap_id = _seed_story_parent(engine)
    with Session(engine) as session:
        artifact = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
        )
        accepted = _decide_story(session, artifact, decision="accepted", offset=2)
        session.commit()
        artifact_id = int(artifact.story_artifact_id or 0)
        artifact_fingerprint = artifact.content_fingerprint
    story_id = accepted.activated_story_ids[0]
    request = StoryCorrectionRequest(
        project_id=project_id,
        story_id=story_id,
        guidance="Preserve the exact first criterion and correct its title.",
        idempotency_key="correct-story",
        actor="operator@example.com",
        correlation_id="correction-1",
    )
    position = _domain(engine).position(project_id)
    decisions = tuple(
        decision
        for decision in position.decisions
        if decision.node_id == "planning.story.generate"
    )

    prepared = DeliveryActionInputService(engine=engine).build_story_correction(
        project_id=project_id,
        decisions=decisions,
        request=request,
    )

    assert isinstance(prepared, tuple)
    decision, payload = prepared
    assert decision.reason_code == "STORY_CORRECTION_AVAILABLE"
    assert decision.instance_key == "backlog_item:PBI-000001"
    correction = payload["correction"]
    assert isinstance(correction, dict)
    assert correction == {
        "story_id": story_id,
        "guidance": request.guidance,
        "source_story_artifact_id": artifact_id,
        "source_story_artifact_fingerprint": artifact_fingerprint,
        "source_story_item_id": "US-0001",
        "source_story_item_fingerprint": correction["source_story_item_fingerprint"],
    }
    writer_input = payload["writer_input"]
    assert isinstance(writer_input, dict)
    assert "US-0001" in str(writer_input["user_input"])
    assert request.guidance in str(writer_input["user_input"])

    application = AgileForgeApplication(
        workflow_domain=_domain(engine),
        delivery_action_input=DeliveryActionInputService(engine=engine),
    )
    ordinary = application.generate_story(
        DeliveryActionRequest(
            project_id=project_id,
            instance_key=decision.instance_key,
            idempotency_key="ordinary-cannot-correct",
            actor="operator@example.com",
        )
    )
    assert ordinary.ok is False
    assert ordinary.error is not None
    assert ordinary.error.code.value == "TRANSITION_NOT_AVAILABLE"

    class CapturingApplication(AgileForgeApplication):
        calls: list[AgenticActionRequest]

        def run_agentic_action(
            self,
            request: AgenticActionRequest,
        ) -> TransitionResult:
            self.calls.append(request)
            return TransitionResult(ok=True, applied_node_id=request.node_id)

    correction_application = CapturingApplication(
        workflow_domain=_domain(engine),
        delivery_action_input=DeliveryActionInputService(engine=engine),
    )
    correction_application.calls = []
    correction_result = correction_application.correct_story(request)
    assert correction_result.ok is True
    assert len(correction_application.calls) == 1
    action = correction_application.calls[0]
    assert action.node_id == "planning.story.generate"
    assert action.instance_key == decision.instance_key
    assert action.input_payload["correction"] == correction

    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        story.title = "Drifted mutable row"
        session.add(story)
        session.commit()
    conflict = DeliveryActionInputService(engine=engine).build_story_correction(
        project_id=project_id,
        decisions=decisions,
        request=request,
    )
    assert conflict is not None
    assert not isinstance(conflict, tuple)
    assert conflict.code.value == "WORKFLOW_FACT_CONFLICT"


def test_story_set_correction_uses_accepted_artifact_when_rows_are_binding_invalid(
    engine: Engine,
) -> None:
    """Recover the complete PBI Story set without trusting a broken row."""
    request_type = getattr(application_module, "StorySetCorrectionRequest", None)
    assert request_type is not None, "Story-set correction request is unavailable."

    project_id, roadmap_id = _seed_story_parent(engine)
    with Session(engine) as session:
        artifact = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
        )
        accepted = _decide_story(session, artifact, decision="accepted", offset=2)
        session.commit()
        artifact_id = int(artifact.story_artifact_id or 0)
        artifact_fingerprint = artifact.content_fingerprint
        story_id = accepted.activated_story_ids[0]
        story = session.get(UserStory, story_id)
        assert story is not None
        story.source_story_item_fingerprint = "sha256:" + ("0" * 64)
        session.add(story)
        session.commit()

    position = _domain(engine).position(project_id)
    correction = next(
        decision
        for decision in position.decisions
        if decision.node_id == "planning.story.generate"
        and decision.reason_code == "STORY_CORRECTION_AVAILABLE"
    )
    request = request_type(
        project_id=project_id,
        instance_key="backlog_item:PBI-000001",
        expected_decision_fingerprint=correction.decision_fingerprint,
        accepted_story_artifact_id=artifact_id,
        accepted_story_artifact_fingerprint=artifact_fingerprint,
        idempotency_key="correct-story-set",
        actor="operator@example.com",
        correlation_id="correction-set-1",
    )

    class CapturingApplication(AgileForgeApplication):
        calls: list[AgenticActionRequest]

        def run_agentic_action(
            self,
            request: AgenticActionRequest,
        ) -> TransitionResult:
            self.calls.append(request)
            return TransitionResult(ok=True, applied_node_id=request.node_id)

    application = CapturingApplication(
        workflow_domain=_domain(engine),
        delivery_action_input=DeliveryActionInputService(engine=engine),
    )
    application.calls = []
    result = application.correct_story_set(request)

    assert result.ok is True
    assert len(application.calls) == 1
    action = application.calls[0]
    assert action.instance_key == "backlog_item:PBI-000001"
    assert action.decision_fingerprint == correction.decision_fingerprint
    assert action.input_payload["story_set_correction"] == {
        "accepted_story_artifact_id": artifact_id,
        "accepted_story_artifact_fingerprint": artifact_fingerprint,
    }
    with Session(engine) as session:
        assert len(session.exec(select(StoryArtifact)).all()) == 1
        rows = session.exec(select(UserStory)).all()
        assert len(rows) == 1
        assert rows[0].is_superseded is False


def test_story_set_correction_accepts_exact_legacy_artifact_only_for_correction(
    engine: Engine,
) -> None:
    """A hash-matching pre-#221 artifact remains correctable, never generatable."""
    expected_legacy_artifact_count = 2
    project_id, roadmap_id = _seed_story_parent(
        engine,
        requirements=("Plan first legacy work", "Plan second legacy work"),
    )
    with Session(engine) as session:
        first = _record_accepted_legacy_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
            backlog_item_id="PBI-000001",
            requirement_index=1,
        )
        second = _record_accepted_legacy_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
            backlog_item_id="PBI-000002",
            requirement_index=2,
        )
        session.commit()
        first_id = int(first.story_artifact_id or 0)
        first_fingerprint = first.content_fingerprint
        first_content = first.canonical_content_json
        second_id = int(second.story_artifact_id or 0)
        second_fingerprint = second.content_fingerprint
        second_content = second.canonical_content_json
        initial_attempt_count = len(session.exec(select(WorkflowNodeAttempt)).all())

    position = _domain(engine).position(project_id)
    corrections = tuple(
        decision
        for decision in position.decisions
        if decision.node_id == "planning.story.generate"
        and decision.reason_code == "STORY_CORRECTION_AVAILABLE"
    )
    assert {decision.instance_key for decision in corrections} == {
        "backlog_item:PBI-000001",
        "backlog_item:PBI-000002",
    }
    first_correction = next(
        decision
        for decision in corrections
        if decision.instance_key == "backlog_item:PBI-000001"
    )
    input_service = DeliveryActionInputService(engine=engine)
    assert (
        input_service.build(
            project_id=project_id,
            decision=first_correction,
            node_id="planning.story.generate",
        )
        is None
    )

    class CapturingApplication(AgileForgeApplication):
        calls: list[AgenticActionRequest]

        def run_agentic_action(
            self,
            request: AgenticActionRequest,
        ) -> TransitionResult:
            self.calls.append(request)
            return TransitionResult(ok=True, applied_node_id=request.node_id)

    application = CapturingApplication(
        workflow_domain=_domain(engine),
        delivery_action_input=input_service,
    )
    application.calls = []
    ordinary = application.generate_story(
        DeliveryActionRequest(
            project_id=project_id,
            instance_key="backlog_item:PBI-000001",
            idempotency_key="ordinary-legacy-story",
            actor="operator@example.com",
        )
    )
    assert ordinary.ok is False
    assert application.calls == []

    result = application.correct_story_set(
        application_module.StorySetCorrectionRequest(
            project_id=project_id,
            instance_key="backlog_item:PBI-000001",
            expected_decision_fingerprint=first_correction.decision_fingerprint,
            accepted_story_artifact_id=first_id,
            accepted_story_artifact_fingerprint=first_fingerprint,
            idempotency_key="correct-legacy-story-set",
            actor="operator@example.com",
            correlation_id="legacy-correction-1",
        )
    )

    assert result.ok is True
    assert len(application.calls) == 1
    action = application.calls[0]
    assert action.instance_key == "backlog_item:PBI-000001"
    assert action.decision_fingerprint == first_correction.decision_fingerprint
    writer_input = action.input_payload["writer_input"]
    assert isinstance(writer_input, dict)
    assert "invest_score" in str(writer_input["user_input"])
    with Session(engine) as session:
        artifacts = {
            int(artifact.story_artifact_id or 0): artifact
            for artifact in session.exec(select(StoryArtifact)).all()
        }
        assert len(artifacts) == expected_legacy_artifact_count
        assert artifacts[first_id].content_fingerprint == first_fingerprint
        assert artifacts[first_id].canonical_content_json == first_content
        assert artifacts[second_id].content_fingerprint == second_fingerprint
        assert artifacts[second_id].canonical_content_json == second_content
        assert (
            len(session.exec(select(StoryArtifactDecision)).all())
            == expected_legacy_artifact_count
        )
        assert (
            len(session.exec(select(WorkflowNodeAttempt)).all())
            == initial_attempt_count
        )


def test_story_set_correction_rejects_noncanonical_legacy_source_before_agent(
    engine: Engine,
) -> None:
    """A hash-matching artifact still fails closed when it is not legacy canonical."""
    project_id, roadmap_id = _seed_story_parent(engine)
    malformed_content = _legacy_story_content(requirement_index=1)
    story_items = malformed_content["story_items"]
    assert isinstance(story_items, list)
    envelope = story_items[0]
    assert isinstance(envelope, dict)
    item = envelope["item"]
    assert isinstance(item, dict)
    item["invest_score"] = "Unscored"
    envelope["item_fingerprint"] = canonical_hash(item)
    with Session(engine) as session:
        artifact = _record_accepted_legacy_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
            backlog_item_id="PBI-000001",
            requirement_index=1,
            legacy_content=malformed_content,
        )
        session.commit()
        artifact_id = int(artifact.story_artifact_id or 0)
        artifact_fingerprint = artifact.content_fingerprint
        artifact_count = len(session.exec(select(StoryArtifact)).all())
        decision_count = len(session.exec(select(StoryArtifactDecision)).all())
        attempt_count = len(session.exec(select(WorkflowNodeAttempt)).all())

    position = _domain(engine).position(project_id)
    correction = next(
        decision
        for decision in position.decisions
        if decision.node_id == "planning.story.generate"
        and decision.reason_code == "STORY_CORRECTION_AVAILABLE"
        and decision.instance_key == "backlog_item:PBI-000001"
    )
    input_service = DeliveryActionInputService(engine=engine)
    assert (
        input_service.build(
            project_id=project_id,
            decision=correction,
            node_id="planning.story.generate",
            allow_legacy_story_correction=True,
        )
        is None
    )

    class CapturingApplication(AgileForgeApplication):
        calls: list[AgenticActionRequest]

        def run_agentic_action(
            self,
            request: AgenticActionRequest,
        ) -> TransitionResult:
            self.calls.append(request)
            return TransitionResult(ok=True, applied_node_id=request.node_id)

    application = CapturingApplication(
        workflow_domain=_domain(engine),
        delivery_action_input=input_service,
    )
    application.calls = []
    result = application.correct_story_set(
        application_module.StorySetCorrectionRequest(
            project_id=project_id,
            instance_key="backlog_item:PBI-000001",
            expected_decision_fingerprint=correction.decision_fingerprint,
            accepted_story_artifact_id=artifact_id,
            accepted_story_artifact_fingerprint=artifact_fingerprint,
            idempotency_key="reject-malformed-legacy-story",
            actor="operator@example.com",
        )
    )

    assert result.ok is False
    assert application.calls == []
    with Session(engine) as session:
        assert len(session.exec(select(StoryArtifact)).all()) == artifact_count
        assert len(session.exec(select(StoryArtifactDecision)).all()) == decision_count
        assert len(session.exec(select(WorkflowNodeAttempt)).all()) == attempt_count


def test_story_set_correction_rejects_malformed_stale_and_foreign_guards_before_agent(
    engine: Engine,
) -> None:
    """Fail closed on every caller-owned correction identity before execution."""
    request_type = application_module.StorySetCorrectionRequest
    for invalid in (
        {"instance_key": "PBI-000001"},
        {"accepted_story_artifact_id": 0},
        {"expected_decision_fingerprint": "not-a-fingerprint"},
        {"accepted_story_artifact_fingerprint": "not-a-fingerprint"},
    ):
        payload: dict[str, Any] = {
            "project_id": 1,
            "instance_key": "backlog_item:PBI-000001",
            "expected_decision_fingerprint": "sha256:" + ("b" * 64),
            "accepted_story_artifact_id": 1,
            "accepted_story_artifact_fingerprint": "sha256:" + ("a" * 64),
            "idempotency_key": "invalid-correction",
            "actor": "operator@example.com",
            **invalid,
        }
        with pytest.raises(ValidationError):
            request_type(**payload)

    project_id, roadmap_id = _seed_story_parent(engine)
    with Session(engine) as session:
        artifact = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
        )
        _decide_story(session, artifact, decision="accepted", offset=2)
        session.commit()
        artifact_id = int(artifact.story_artifact_id or 0)
        artifact_fingerprint = artifact.content_fingerprint
    correction = next(
        decision
        for decision in _domain(engine).position(project_id).decisions
        if decision.node_id == "planning.story.generate"
        and decision.reason_code == "STORY_CORRECTION_AVAILABLE"
    )
    foreign_project_id, _foreign_roadmap_id = _seed_story_parent(
        engine,
        requirements=("Plan foreign work",),
    )
    base: dict[str, Any] = {
        "project_id": project_id,
        "instance_key": "backlog_item:PBI-000001",
        "expected_decision_fingerprint": correction.decision_fingerprint,
        "accepted_story_artifact_id": artifact_id,
        "accepted_story_artifact_fingerprint": artifact_fingerprint,
        "idempotency_key": "guarded-correction",
        "actor": "operator@example.com",
    }

    class CapturingApplication(AgileForgeApplication):
        calls: list[AgenticActionRequest]

        def run_agentic_action(
            self,
            request: AgenticActionRequest,
        ) -> TransitionResult:
            self.calls.append(request)
            return TransitionResult(ok=True, applied_node_id=request.node_id)

    for changed in (
        {"instance_key": "backlog_item:PBI-999999"},
        {"expected_decision_fingerprint": "sha256:" + ("c" * 64)},
        {"accepted_story_artifact_id": artifact_id + 1},
        {"accepted_story_artifact_fingerprint": "sha256:" + ("d" * 64)},
        {"project_id": foreign_project_id},
    ):
        application = CapturingApplication(
            workflow_domain=_domain(engine),
            delivery_action_input=DeliveryActionInputService(engine=engine),
        )
        application.calls = []

        result = application.correct_story_set(
            request_type.model_validate({**base, **changed})
        )

        assert result.ok is False
        assert result.error is not None
        assert result.error.code.value == "TRANSITION_NOT_AVAILABLE"
        assert application.calls == []


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "UNIQUE constraint failed: "
            "story_artifact_decisions.project_id, "
            "story_artifact_decisions.story_artifact_id",
            True,
        ),
        (
            "UNIQUE constraint failed: user_stories.project_id, "
            "user_stories.source_story_artifact_id, "
            "user_stories.source_story_item_id",
            True,
        ),
        (
            "UNIQUE constraint failed: "
            "story_artifact_decisions.project_id, "
            "story_artifact_decisions.story_artifact_id, projects.name",
            False,
        ),
        (
            "UNIQUE constraint failed: user_stories.project_id, "
            "user_stories.source_story_artifact_id, "
            "user_stories.source_story_item_id, user_stories.title",
            False,
        ),
        ("UNIQUE constraint failed: projects.name", False),
    ],
)
def test_story_race_classifier_requires_exact_sqlite_constraint_identity(
    message: str,
    expected: bool,
) -> None:
    """SQLite fallback matches one complete authorized constraint message."""
    error = IntegrityError("insert", {}, sqlite3.IntegrityError(message))

    assert _is_story_decision_uniqueness_race(error) is expected


@pytest.mark.parametrize(
    ("constraint_name", "expected"),
    [
        ("uq_story_artifact_decision", True),
        ("uq_user_story_artifact_item", True),
        ("uq_unrelated_story_prefix", False),
    ],
)
def test_story_race_classifier_uses_named_backend_constraint_only(
    constraint_name: str,
    expected: bool,
) -> None:
    """Backend metadata wins without falling through to SQLite message text."""

    class Diagnostic:
        def __init__(self, name: str) -> None:
            self.constraint_name = name

    class DriverIntegrityError(Exception):
        def __init__(self, name: str) -> None:
            super().__init__(
                "UNIQUE constraint failed: "
                "story_artifact_decisions.project_id, "
                "story_artifact_decisions.story_artifact_id"
            )
            self.diag = Diagnostic(name)

    error = IntegrityError("insert", {}, DriverIntegrityError(constraint_name))

    assert _is_story_decision_uniqueness_race(error) is expected


@pytest.mark.parametrize(
    "corruption",
    [
        "missing",
        "extra",
        "drifted",
        "validator_version",
        "identity",
        "references",
        "structural_diagnostics",
    ],
)
def test_story_winner_proof_requires_exact_superseded_ancestor_rows(
    engine: Engine,
    corruption: str,
) -> None:
    """Every accepted ancestor keeps one exact, superseded operational row set."""
    project_id, roadmap_id = _seed_story_parent(engine)
    with Session(engine) as session:
        artifact_a = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
            title="Accepted ancestor A",
        )
        accepted_a = _decide_story(
            session,
            artifact_a,
            decision="accepted",
            offset=2,
        )
        artifact_c = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
            title="Accepted winner C",
            supersedes_id=artifact_a.story_artifact_id,
            recorded_offset=3,
        )
        _decide_story(session, artifact_c, decision="accepted", offset=4)
        session.commit()
        artifact_a_id = int(artifact_a.story_artifact_id or 0)
        artifact_c_id = int(artifact_c.story_artifact_id or 0)
        artifact_c_fingerprint = artifact_c.content_fingerprint
        ancestor = session.get(UserStory, accepted_a.activated_story_ids[0])
        assert ancestor is not None
        assert ancestor.is_superseded is True
        assert prove_story_decision_winner_in_session(
            session,
            project_id=project_id,
            story_artifact_id=artifact_c_id,
            artifact_fingerprint=artifact_c_fingerprint,
        )

        if corruption == "missing":
            session.delete(ancestor)
        elif corruption == "extra":
            extra_values = ancestor.model_dump(exclude={"story_id"})
            extra_values.update(
                {
                    "source_story_item_id": "US-9999",
                    "source_story_item_fingerprint": "sha256:unexpected-item",
                }
            )
            session.add(UserStory.model_validate(extra_values))
        elif corruption == "drifted":
            ancestor.title = "Drifted accepted ancestor"
            session.add(ancestor)
        else:
            winner = session.exec(
                select(UserStory).where(
                    col(UserStory.source_story_artifact_id) == artifact_c_id
                )
            ).one()
            evidence = json.loads(winner.validation_evidence or "{}")
            if corruption == "validator_version":
                evidence["validator_version"] = "obsolete-validator"
            elif corruption == "identity":
                evidence["source_backlog_item_id"] = "PBI-999999"
            elif corruption == "references":
                evidence["referenced_spec_item_ids"] = ["REQ.other"]
            else:
                evidence["structural_failures"] = [
                    {
                        "code": "STORY_STATEMENT_INVALID",
                        "message": "Tampered structural finding.",
                    }
                ]
                evidence["structurally_eligible"] = False
            winner.validation_evidence = canonical_json(evidence)
            session.add(winner)
        session.commit()

    with Session(engine) as session:
        assert (
            session.exec(
                select(StoryArtifact).where(
                    col(StoryArtifact.story_artifact_id) == artifact_a_id
                )
            ).one_or_none()
            is not None
        )
        assert not prove_story_decision_winner_in_session(
            session,
            project_id=project_id,
            story_artifact_id=artifact_c_id,
            artifact_fingerprint=artifact_c_fingerprint,
        )


def test_decide_story_translates_only_a_fully_proven_named_uniqueness_race(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rollback/reload a named loser; re-raise unrelated integrity failures."""
    project_id, roadmap_id = _seed_story_parent(engine)
    with Session(engine) as session:
        artifact = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
        )
        session.commit()
        artifact_id = int(artifact.story_artifact_id or 0)
        artifact_fingerprint = artifact.content_fingerprint
    domain = _domain(engine)
    position = domain.position(project_id)
    review = next(
        decision
        for decision in position.decisions
        if decision.node_id == "planning.story.review"
    )
    request = DecideStory(
        **_guards(position, "planning.story.review", review.instance_key),
        idempotency_key="losing-story-review",
        backlog_item_id="PBI-000001",
        story_artifact_id=artifact_id,
        artifact_fingerprint=artifact_fingerprint,
        decision="feedback",
        rationale="Concurrent loser.",
    )
    with Session(engine) as session:
        winner_artifact = session.get(StoryArtifact, artifact_id)
        assert winner_artifact is not None
        _decide_story(session, winner_artifact, decision="accepted", offset=3)
        session.commit()

    named = IntegrityError(
        "insert",
        {},
        sqlite3.IntegrityError(
            "UNIQUE constraint failed: "
            "story_artifact_decisions.project_id, "
            "story_artifact_decisions.story_artifact_id"
        ),
    )

    def raise_named(*_args: object, **_kwargs: object) -> None:
        raise named

    monkeypatch.setattr(story_phase, "record_story_decision_in_session", raise_named)
    with Session(engine) as session:
        result = execute_decide_story(session, request, review, EVALUATED_AT)
    assert result.ok is False
    assert result.error is not None
    assert result.error.code.value == "WORKFLOW_FACT_CONFLICT"
    assert result.error.message == (
        "The Story artifact was decided by another workflow transition."
    )
    with Session(engine) as session:
        assert len(session.exec(select(StoryArtifactDecision)).all()) == 1
        assert len(session.exec(select(UserStory)).all()) == 1

    with Session(engine) as session:
        committed = session.exec(select(UserStory)).one()
        committed.title = "Unproven committed winner"
        session.add(committed)
        session.commit()
    with Session(engine) as session, pytest.raises(IntegrityError) as unproven:
        execute_decide_story(session, request, review, EVALUATED_AT)
    assert unproven.value is named

    unrelated = IntegrityError(
        "insert",
        {},
        sqlite3.IntegrityError("UNIQUE constraint failed: projects.name"),
    )

    def raise_unrelated(*_args: object, **_kwargs: object) -> None:
        raise unrelated

    monkeypatch.setattr(
        story_phase,
        "record_story_decision_in_session",
        raise_unrelated,
    )
    with Session(engine) as session, pytest.raises(IntegrityError) as raised:
        execute_decide_story(session, request, review, EVALUATED_AT)
    assert raised.value is unrelated


def test_decide_story_rolls_back_when_structural_evaluator_raises_value_error(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected validation errors leave no accepted decision or receipt."""
    project_id, roadmap_id = _seed_story_parent(engine)
    with Session(engine) as session:
        artifact = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
        )
        session.commit()
        artifact_id = int(artifact.story_artifact_id or 0)
        artifact_fingerprint = artifact.content_fingerprint
    domain = _domain(engine)
    position = domain.position(project_id)
    review = next(
        item
        for item in position.decisions
        if item.node_id == "planning.story.review"
    )
    request = DecideStory(
        **_guards(position, "planning.story.review", review.instance_key),
        idempotency_key="rollback-story-validation-value-error",
        backlog_item_id="PBI-000001",
        story_artifact_id=artifact_id,
        artifact_fingerprint=artifact_fingerprint,
        decision="accepted",
        rationale="Acceptance must be atomic.",
    )

    def raise_evaluator_value_error(*_args: object, **_kwargs: object) -> None:
        message = "unexpected structural evaluator failure"
        raise ValueError(message)

    monkeypatch.setattr(
        story_phase,
        "validate_story_with_specification_in_session",
        raise_evaluator_value_error,
    )
    with pytest.raises(ValueError, match="unexpected structural evaluator failure"):
        domain.transition(request)

    with Session(engine) as session:
        assert session.exec(select(StoryArtifactDecision)).all() == []
        assert session.exec(select(UserStory)).all() == []
        assert session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.idempotency_key)
                == request.idempotency_key
            )
        ).all() == []


def test_acceptance_persists_expected_structural_failure_evidence(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expected structural diagnostics commit as canonical in-transaction evidence."""
    project_id, roadmap_id = _seed_story_parent(engine)
    original_validate = story_phase.validate_story_with_specification_in_session

    def persist_structural_failure(
        session: Session,
        params: ValidateStoryInput | Mapping[str, object],
        *,
        semantic_review: StorySemanticReview | None = None,
        now: Callable[[], datetime] = datetime.now,
    ) -> object:
        story_id = (
            params.story_id
            if isinstance(params, ValidateStoryInput)
            else cast("int", params["story_id"])
        )
        story = session.get(UserStory, story_id)
        assert story is not None
        story.story_description = "Not a valid Story statement"
        session.add(story)
        return original_validate(
            session,
            params,
            semantic_review=semantic_review,
            now=now,
        )

    monkeypatch.setattr(
        story_phase,
        "validate_story_with_specification_in_session",
        persist_structural_failure,
    )
    with Session(engine) as session:
        artifact = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
        )
        result = _decide_story(session, artifact, decision="accepted", offset=2)
        session.commit()
        story_id = result.activated_story_ids[0]

    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        evidence = ValidationEvidence.model_validate_json(
            story.validation_evidence or "",
            strict=True,
        )
    assert evidence.structurally_eligible is False
    assert [item.code for item in evidence.structural_failures] == [
        "STORY_ITEM_BINDING_INVALID",
        "STORY_STATEMENT_INVALID",
    ]


def test_decide_story_rolls_back_when_evidence_persistence_flush_fails(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flush failure after evidence assignment rolls back the whole acceptance."""
    project_id, roadmap_id = _seed_story_parent(engine)
    with Session(engine) as session:
        artifact = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
        )
        session.commit()
        artifact_id = int(artifact.story_artifact_id or 0)
        artifact_fingerprint = artifact.content_fingerprint
    domain = _domain(engine)
    position = domain.position(project_id)
    review = next(
        item
        for item in position.decisions
        if item.node_id == "planning.story.review"
    )
    request = DecideStory(
        **_guards(position, "planning.story.review", review.instance_key),
        idempotency_key="rollback-story-evidence-flush",
        backlog_item_id="PBI-000001",
        story_artifact_id=artifact_id,
        artifact_fingerprint=artifact_fingerprint,
        decision="accepted",
        rationale="Evidence persistence must be atomic.",
    )
    original_flush = Session.flush

    def fail_evidence_flush(
        session: Session,
        objects: Sequence[Any] | None = None,
    ) -> None:
        if any(
            isinstance(row, UserStory) and row.validation_evidence is not None
            for row in session.identity_map.values()
        ):
            message = "simulated evidence flush failure"
            raise RuntimeError(message)
        original_flush(session, objects)

    monkeypatch.setattr(Session, "flush", fail_evidence_flush)
    with pytest.raises(
        story_phase.StoryAcceptanceValidationError,
        match="simulated evidence flush failure",
    ):
        domain.transition(request)

    with Session(engine) as session:
        assert session.exec(select(StoryArtifactDecision)).all() == []
        assert session.exec(select(UserStory)).all() == []
        assert session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.idempotency_key)
                == request.idempotency_key
            )
        ).all() == []


def test_concurrent_distinct_story_decisions_commit_one_complete_winner(
    tmp_path: Path,
) -> None:
    """Serialize two review keys to one accepted decision and complete row set."""
    race_engine = create_engine(
        f"sqlite:///{tmp_path / 'task-8-story-decision-race.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(race_engine)
    try:
        project_id, roadmap_id = _seed_story_parent(race_engine)
        with Session(race_engine) as session:
            artifact = _record_story(
                session,
                project_id=project_id,
                roadmap_id=roadmap_id,
            )
            session.commit()
            artifact_id = int(artifact.story_artifact_id or 0)
            artifact_fingerprint = artifact.content_fingerprint
        domain = _domain(race_engine)
        position = domain.position(project_id)
        review = next(
            decision
            for decision in position.decisions
            if decision.node_id == "planning.story.review"
        )
        barrier = threading.Barrier(2)

        def decide(index: int) -> TransitionResult:
            request = DecideStory(
                **_guards(position, "planning.story.review", review.instance_key),
                idempotency_key=f"concurrent-story-decision-{index}",
                backlog_item_id="PBI-000001",
                story_artifact_id=artifact_id,
                artifact_fingerprint=artifact_fingerprint,
                decision="accepted",
                rationale=f"Concurrent acceptance {index}.",
            )
            barrier.wait()
            return domain.transition(request)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(decide, (1, 2)))

        successes = [result for result in results if result.ok]
        failures = [result for result in results if not result.ok]
        assert len(successes) == 1
        assert len(failures) == 1
        assert failures[0].error is not None
        assert failures[0].error.code.value == "WORKFLOW_FACT_CONFLICT"
        with Session(race_engine) as session:
            assert len(session.exec(select(StoryArtifactDecision)).all()) == 1
            rows = session.exec(select(UserStory)).all()
            assert len(rows) == 1
            assert rows[0].source_story_artifact_id == artifact_id
            assert rows[0].source_story_item_id == "US-0001"
    finally:
        race_engine.dispose()


def test_story_correction_replay_binds_semantics_and_stored_instance(
    engine: Engine,
) -> None:
    """Replay correction before reads while ordinary omitted selectors conflict."""
    correction: JsonObject = {
        "story_id": 42,
        "guidance": "Keep exact evidence.",
        "source_story_artifact_id": 91,
        "source_story_artifact_fingerprint": "sha256:artifact",
        "source_story_item_id": "US-0002",
        "source_story_item_fingerprint": "sha256:item",
    }
    stored = StartNodeAttempt(
        project_id=41,
        graph_version="agileforge.workflow.v2",
        fact_fingerprint="facts-story-correction",
        decision_fingerprint="decision-story-correction",
        idempotency_key="story-correction-replay",
        actor="operator@example.com",
        correlation_id="story-correction-correlation",
        target_node_id="planning.story.generate",
        target_instance_key="backlog_item:PBI-000001",
        normalized_input={"correction": correction},
        model_id="fake/model",
        execution_settings={"timeout_seconds": 5.0, "max_attempts": 1},
        lease_seconds=60,
    )
    persisted = TransitionResult(ok=True, applied_node_id=stored.target_node_id)
    with Session(engine) as session:
        session.add(
            WorkflowTransitionReceipt(
                request_kind="start_node_attempt",
                idempotency_key=stored.idempotency_key,
                request_fingerprint=canonical_hash(stored.model_dump(mode="json")),
                request_json=canonical_json(stored.model_dump(mode="json")),
                result_json=canonical_json(persisted.model_dump(mode="json")),
                started_at=EVALUATED_AT,
                completed_at=EVALUATED_AT,
            )
        )
        session.commit()
    replay_service = DurableNodeAttemptReplayService(engine=engine)

    def replay(
        *, story_id: int, guidance: str, stored_selector: bool
    ) -> TransitionResult | None:
        return replay_service.replay(
            NodeAttemptReplayQuery(
                project_id=stored.project_id,
                graph_version=None,
                fact_fingerprint=None,
                decision_fingerprint=None,
                node_id=stored.target_node_id,
                idempotency_key=stored.idempotency_key,
                actor=stored.actor,
                correlation_id=stored.correlation_id,
                semantic_input={
                    "correction": {"story_id": story_id, "guidance": guidance}
                },
                reuse_stored_instance_key=stored_selector,
            )
        )

    exact = replay(
        story_id=42,
        guidance="Keep exact evidence.",
        stored_selector=True,
    )
    assert exact == persisted.model_copy(update={"replayed": True})
    for conflict in (
        replay(story_id=43, guidance="Keep exact evidence.", stored_selector=True),
        replay(story_id=42, guidance="Changed.", stored_selector=True),
        replay(story_id=42, guidance="Keep exact evidence.", stored_selector=False),
    ):
        assert conflict is not None
        assert conflict.error is not None
        assert conflict.error.code.value == "WORKFLOW_FACT_CONFLICT"


def test_story_set_correction_replay_requires_exact_artifact_and_graph_guards(
    engine: Engine,
) -> None:
    """Replay only the same PBI, decision, artifact, actor, and correlation."""
    correction_identity: JsonObject = {
        "accepted_story_artifact_id": 91,
        "accepted_story_artifact_fingerprint": "sha256:" + ("a" * 64),
    }
    stored = StartNodeAttempt(
        project_id=41,
        graph_version="agileforge.workflow.v2",
        fact_fingerprint="sha256:" + ("f" * 64),
        decision_fingerprint="sha256:" + ("b" * 64),
        idempotency_key="story-set-correction-replay",
        actor="operator@example.com",
        correlation_id="story-set-correction-correlation",
        target_node_id="planning.story.generate",
        target_instance_key="backlog_item:PBI-000001",
        normalized_input={"story_set_correction": correction_identity},
        model_id="fake/model",
        execution_settings={"timeout_seconds": 5.0, "max_attempts": 1},
        lease_seconds=60,
    )
    persisted = TransitionResult(ok=True, applied_node_id=stored.target_node_id)
    with Session(engine) as session:
        session.add(
            WorkflowTransitionReceipt(
                request_kind="start_node_attempt",
                idempotency_key=stored.idempotency_key,
                request_fingerprint=canonical_hash(stored.model_dump(mode="json")),
                request_json=canonical_json(stored.model_dump(mode="json")),
                result_json=canonical_json(persisted.model_dump(mode="json")),
                started_at=EVALUATED_AT,
                completed_at=EVALUATED_AT,
            )
        )
        session.commit()
    service = DurableNodeAttemptReplayService(engine=engine)

    def replay(**changes: object) -> TransitionResult | None:
        values: dict[str, Any] = {
            "project_id": stored.project_id,
            "graph_version": None,
            "fact_fingerprint": None,
            "decision_fingerprint": stored.decision_fingerprint,
            "node_id": stored.target_node_id,
            "instance_key": stored.target_instance_key,
            "idempotency_key": stored.idempotency_key,
            "actor": stored.actor,
            "correlation_id": stored.correlation_id,
            "semantic_input": {"story_set_correction": correction_identity},
            **changes,
        }
        return service.replay(NodeAttemptReplayQuery(**values))

    assert replay() == persisted.model_copy(update={"replayed": True})
    for conflict in (
        replay(instance_key="backlog_item:PBI-000002"),
        replay(decision_fingerprint="sha256:" + ("c" * 64)),
        replay(actor="another@example.com"),
        replay(
            semantic_input={
                "story_set_correction": {
                    **correction_identity,
                    "accepted_story_artifact_id": 92,
                }
            }
        ),
    ):
        assert conflict is not None
        assert conflict.error is not None
        assert conflict.error.code.value == "WORKFLOW_FACT_CONFLICT"


def test_story_record_and_accept_replay_exact_identities_and_changed_input_conflicts(
    engine: Engine,
) -> None:
    """Reuse exact receipt results without duplicate artifacts, decisions, or rows."""
    project_id, _roadmap_id = _seed_story_parent(engine)

    class AdvancingClock:
        def __init__(self) -> None:
            self.value = EVALUATED_AT

        def now(self) -> datetime:
            current = self.value
            self.value += timedelta(seconds=1)
            return current

    domain = _domain(engine)
    domain._clock = AdvancingClock()
    position = domain.position(project_id)
    generate = next(
        decision
        for decision in position.decisions
        if decision.node_id == "planning.story.generate"
    )
    references = {item.fact_type: item for item in generate.fact_references}
    content = _story_content(title="Replay candidate")
    request = RecordStoryDraft(
        **_guards(position, generate.node_id, generate.instance_key),
        idempotency_key="record-story-replay",
        backlog_item_id=references["backlog_item"].fact_id,
        source_backlog_artifact_id=int(references["backlog"].fact_id),
        source_backlog_artifact_fingerprint=references["backlog"].fingerprint,
        roadmap_artifact_id=int(references["roadmap"].fact_id),
        roadmap_artifact_fingerprint=references["roadmap"].fingerprint,
        canonical_content=content,
        content_fingerprint=canonical_hash(content),
    )

    recorded = domain.transition(request)
    replayed_record = domain.transition(request)
    assert recorded.ok is True
    assert replayed_record == recorded.model_copy(update={"replayed": True})
    changed = _story_content(title="Changed candidate")
    changed_result = domain.transition(
        request.model_copy(
            update={
                "canonical_content": changed,
                "content_fingerprint": canonical_hash(changed),
            }
        )
    )
    assert changed_result.ok is False
    assert changed_result.error is not None
    assert changed_result.error.code.value == "WORKFLOW_FACT_CONFLICT"

    artifact_id_value = recorded.output["story_artifact_id"]
    assert isinstance(artifact_id_value, int)
    artifact_id = artifact_id_value
    position = domain.position(project_id)
    review = next(
        decision
        for decision in position.decisions
        if decision.node_id == "planning.story.review"
    )
    decision_request = DecideStory(
        **_guards(position, review.node_id, review.instance_key),
        idempotency_key="accept-story-replay",
        backlog_item_id=references["backlog_item"].fact_id,
        story_artifact_id=artifact_id,
        artifact_fingerprint=str(recorded.output["content_fingerprint"]),
        decision="accepted",
        rationale="Accept exact replay candidate.",
    )
    accepted = domain.transition(decision_request)
    with Session(engine) as session:
        accepted_story = session.exec(select(UserStory)).one()
        evidence_before_replay = accepted_story.validation_evidence
        assert evidence_before_replay is not None
        validated_at_before_replay = ValidationEvidence.model_validate_json(
            evidence_before_replay,
            strict=True,
        ).validated_at
    replayed_accept = domain.transition(decision_request)
    assert accepted.ok is True
    assert replayed_accept == accepted.model_copy(update={"replayed": True})
    with Session(engine) as session:
        assert len(session.exec(select(StoryArtifact)).all()) == 1
        assert len(session.exec(select(StoryArtifactDecision)).all()) == 1
        stories = session.exec(select(UserStory)).all()
        assert len(stories) == 1
        assert stories[0].validation_evidence == evidence_before_replay
        assert ValidationEvidence.model_validate_json(
            stories[0].validation_evidence or "",
            strict=True,
        ).validated_at == validated_at_before_replay
