"""Tests for story dependency persistence."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from models.core import UserStory, UserStoryDependency
from models.events import WorkflowEvent
from models.workflow import BacklogArtifact, StoryDependencyReview
from repositories.workflow import WorkflowFactRepository
from services.agent_workbench.story_phase import (
    RecordStoryDecisionInput,
    RecordStoryDraftInput,
    record_story_decision_in_session,
    record_story_draft_in_session,
)
from services.contracts.story import (
    CanonicalStoryItem,
    CanonicalStoryOutput,
    InvestDimensionAssessment,
    StoryInvestAssessment,
    StoryItemEnvelope,
)
from services.story_dependencies import (
    ApplyStoryDependenciesInput,
    StoryDependencyGraphError,
    apply_story_dependencies_in_session,
    dependency_inspect_payload,
    detect_dependency_cycles,
    load_story_dependency_graph,
)
from tests.test_create_user_story import (
    _decide_story,
    _record_story,
    _seed_story_parent,
)
from tests.test_story_validation_service import _validate
from tests.workflow.test_planning_transitions import EVALUATED_AT, _roadmap_content
from workflow.facts import StoryDependencyReviewEdgeFact
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.planning_integrity import (
    canonical_dependency_edges,
    dependency_edges_payload,
    dependency_review_fingerprint,
)

REVIEWED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)


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
            evidence="Effort is S.",
        ),
        testable=InvestDimensionAssessment(
            result="pass",
            rationale="Verifiable pass/fail criteria.",
            evidence="Observable verification steps.",
        ),
    )


def _story_set(session: Session, *, titles: tuple[str, ...]) -> tuple[int, ...]:
    engine = session.get_bind()
    assert isinstance(engine, Engine)
    project_id, roadmap_id = _seed_story_parent(engine)
    backlog = session.exec(
        select(BacklogArtifact).where(BacklogArtifact.project_id == project_id)
    ).one()
    items = tuple(
        CanonicalStoryItem(
            story_item_id=f"US-{ordinal:04d}",
            story_title=title,
            statement=(
                f"As an operator, I want {title.lower()}, so that delivery is exact."
            ),
            persona="operator",
            acceptance_criteria=(f"Verify {title}.",),
            spec_item_ids=("REQ.planning-1",),
            invest_assessment=_invest_assessment(),
            estimated_effort="S",
            produced_artifacts=(),
            research_caveats=(),
            dependency_candidates=(),
        )
        for ordinal, title in enumerate(titles, start=1)
    )
    content = CanonicalStoryOutput(
        story_items=tuple(
            StoryItemEnvelope(
                item=item,
                item_fingerprint=canonical_hash(item.model_dump(mode="json")),
            )
            for item in items
        ),
        is_complete=True,
    ).model_dump(mode="json")
    artifact = record_story_draft_in_session(
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
            supersedes_story_artifact_id=None,
            actor="dependency-reviewer",
            recorded_at=EVALUATED_AT,
        ),
    )
    result = record_story_decision_in_session(
        session,
        inputs=RecordStoryDecisionInput(
            artifact=artifact,
            decision="accepted",
            rationale="Accepted dependency Story set.",
            reviewer="dependency-reviewer",
            idempotency_key=f"accept-dependency-{project_id}",
            decided_at=REVIEWED_AT,
        ),
    )
    session.commit()
    return (project_id, *result.activated_story_ids)


def _story_pair(session: Session) -> tuple[int, int, int]:
    project_id, prerequisite_id, dependent_id, _final_id = _story_set(
        session,
        titles=(
            "Capture market data",
            "Generate recommendation",
            "Deliver recommendation",
        ),
    )
    return project_id, dependent_id, prerequisite_id


def _make_story(
    session: Session,
    *,
    project_id: int,
    title: str,
    slot: int,
) -> int:
    story = session.exec(
        select(UserStory).where(
            UserStory.project_id == project_id,
            UserStory.source_story_item_id == f"US-{slot:04d}",
        )
    ).one()
    assert story.title == title
    assert story.story_id is not None
    return story.story_id


def _chain_edges(
    *,
    dependent_story_id: int,
    prerequisite_story_id: int,
    final_story_id: int,
) -> tuple[StoryDependencyReviewEdgeFact, ...]:
    return canonical_dependency_edges(
        (
            StoryDependencyReviewEdgeFact(
                dependent_story_id=dependent_story_id,
                prerequisite_story_id=prerequisite_story_id,
                reason="Recommendation needs captured market data.",
            ),
            StoryDependencyReviewEdgeFact(
                dependent_story_id=final_story_id,
                prerequisite_story_id=dependent_story_id,
                reason="Delivery needs the generated recommendation.",
            ),
        )
    )


def _apply_dependency_review(
    session: Session,
    *,
    project_id: int,
    selected_story_ids: tuple[int, ...],
    reviewed_edges: tuple[StoryDependencyReviewEdgeFact, ...],
) -> StoryDependencyReview:
    return apply_story_dependencies_in_session(
        session,
        inputs=ApplyStoryDependenciesInput(
            project_id=project_id,
            selected_story_ids=selected_story_ids,
            reviewed_edges=reviewed_edges,
            source_fingerprint="sha256:dependency-source",
            reviewer="dependency-reviewer",
            reviewed_at=REVIEWED_AT,
        ),
    )


def test_caller_session_writer_canonicalizes_reversed_edge_order(
    session: Session,
) -> None:
    """Persist one canonical dependency review regardless of caller edge order."""
    project_id, dependent_story_id, prerequisite_story_id = _story_pair(session)
    final_story_id = _make_story(
        session,
        project_id=project_id,
        title="Deliver recommendation",
        slot=3,
    )
    selected_story_ids = tuple(
        sorted((prerequisite_story_id, dependent_story_id, final_story_id))
    )
    canonical_edges = _chain_edges(
        dependent_story_id=dependent_story_id,
        prerequisite_story_id=prerequisite_story_id,
        final_story_id=final_story_id,
    )
    expected_json = canonical_json(dependency_edges_payload(canonical_edges))
    expected_fingerprint = dependency_review_fingerprint(canonical_edges)

    first = _apply_dependency_review(
        session,
        project_id=project_id,
        selected_story_ids=selected_story_ids,
        reviewed_edges=canonical_edges,
    )
    first_json = first.reviewed_edges_json
    first_fingerprint = first.dependency_fingerprint
    session.rollback()

    reversed_review = _apply_dependency_review(
        session,
        project_id=project_id,
        selected_story_ids=selected_story_ids,
        reviewed_edges=tuple(reversed(canonical_edges)),
    )
    session.commit()

    assert first_json == expected_json
    assert reversed_review.reviewed_edges_json == expected_json
    assert first_fingerprint == expected_fingerprint
    assert reversed_review.dependency_fingerprint == expected_fingerprint


def test_reversed_dependency_review_round_trips_repository(
    engine: Engine,
    session: Session,
) -> None:
    """Reload a reversed caller edge set with its canonical fingerprint intact."""
    project_id, dependent_story_id, prerequisite_story_id = _story_pair(session)
    final_story_id = _make_story(
        session,
        project_id=project_id,
        title="Deliver recommendation",
        slot=3,
    )
    selected_story_ids = tuple(
        sorted((prerequisite_story_id, dependent_story_id, final_story_id))
    )
    canonical_edges = _chain_edges(
        dependent_story_id=dependent_story_id,
        prerequisite_story_id=prerequisite_story_id,
        final_story_id=final_story_id,
    )
    review = _apply_dependency_review(
        session,
        project_id=project_id,
        selected_story_ids=selected_story_ids,
        reviewed_edges=tuple(reversed(canonical_edges)),
    )
    session.commit()

    with Session(engine) as reload_session:
        snapshot = WorkflowFactRepository(reload_session).load(project_id)

    assert review.reviewed_edges_json == canonical_json(
        dependency_edges_payload(canonical_edges)
    )
    assert review.dependency_fingerprint == dependency_review_fingerprint(
        canonical_edges
    )
    assert len(snapshot.story_dependency_reviews) == 1
    assert snapshot.story_dependency_reviews[0].reviewed_edges == canonical_edges
    assert (
        snapshot.story_dependency_reviews[0].dependency_fingerprint
        == review.dependency_fingerprint
    )


def test_caller_session_writer_rejects_duplicate_edges_before_write(
    session: Session,
) -> None:
    """Reject duplicate reviewed endpoints before flushing any durable row."""
    project_id, dependent_story_id, prerequisite_story_id = _story_pair(session)
    edge = StoryDependencyReviewEdgeFact(
        dependent_story_id=dependent_story_id,
        prerequisite_story_id=prerequisite_story_id,
        reason="Recommendation needs captured market data.",
    )

    with pytest.raises(StoryDependencyGraphError):
        _apply_dependency_review(
            session,
            project_id=project_id,
            selected_story_ids=tuple(
                sorted((prerequisite_story_id, dependent_story_id))
            ),
            reviewed_edges=(edge, edge),
        )

    assert session.exec(select(UserStoryDependency)).all() == []
    assert session.exec(select(StoryDependencyReview)).all() == []
    assert session.exec(select(WorkflowEvent)).all() == []


def test_dependency_review_rejects_cross_specification_story_roots(
    session: Session,
) -> None:
    """Fail closed before persisting an edge across mixed accepted Spec roots."""
    project_id, dependent_story_id, prerequisite_story_id = _story_pair(session)
    prerequisite = session.get(UserStory, prerequisite_story_id)
    assert prerequisite is not None
    prerequisite.accepted_spec_version_id += 1
    prerequisite.accepted_spec_hash = "sha256:" + ("f" * 64)
    edge = StoryDependencyReviewEdgeFact(
        dependent_story_id=dependent_story_id,
        prerequisite_story_id=prerequisite_story_id,
        reason="This mixed root must never persist.",
    )

    with session.no_autoflush, pytest.raises(StoryDependencyGraphError) as raised:
        _apply_dependency_review(
            session,
            project_id=project_id,
            selected_story_ids=tuple(
                sorted((dependent_story_id, prerequisite_story_id))
            ),
            reviewed_edges=(edge,),
        )

    assert [issue.code for issue in raised.value.issues] == [
        "STORY_DEPENDENCY_CROSS_SPECIFICATION"
    ]
    session.rollback()
    assert session.exec(select(UserStoryDependency)).all() == []
    assert session.exec(select(StoryDependencyReview)).all() == []


def test_dependency_table_accepts_proposed_edge(session: Session) -> None:
    """Persist a proposed dependency edge with review metadata."""
    project_id, dependent_story_id, prerequisite_story_id = _story_pair(session)

    edge = UserStoryDependency(
        project_id=project_id,
        dependent_story_id=dependent_story_id,
        prerequisite_story_id=prerequisite_story_id,
        status="proposed",
        source="story_writer",
        confidence="explicit",
        reason="Recommendation needs captured market data.",
    )
    session.add(edge)
    session.commit()
    session.refresh(edge)

    assert edge.dependency_id is not None
    assert edge.status == "proposed"
    assert edge.source == "story_writer"
    assert edge.confidence == "explicit"


def test_dependency_table_prevents_duplicate_edge(session: Session) -> None:
    """Reject duplicate dependency edges for one project and story pair."""
    project_id, dependent_story_id, prerequisite_story_id = _story_pair(session)
    session.add(
        UserStoryDependency(
            project_id=project_id,
            dependent_story_id=dependent_story_id,
            prerequisite_story_id=prerequisite_story_id,
        )
    )
    session.commit()

    session.add(
        UserStoryDependency(
            project_id=project_id,
            dependent_story_id=dependent_story_id,
            prerequisite_story_id=prerequisite_story_id,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_dependency_validation_rejects_self_edge(session: Session) -> None:
    """Reject dependency edges where a story blocks itself."""
    project_id, dependent_story_id, _ = _story_pair(session)

    session.add(
        UserStoryDependency(
            project_id=project_id,
            dependent_story_id=dependent_story_id,
            prerequisite_story_id=dependent_story_id,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_dependency_test_engine_enforces_sqlite_foreign_keys(engine: Engine) -> None:
    """Verify test engines enable SQLite foreign-key enforcement."""
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def test_build_dependency_graph_reports_missing_story(
    engine: Engine,
    session: Session,
) -> None:
    """Report orphaned dependency edges without crashing graph load."""
    project_id, dependent_story_id, _ = _story_pair(session)
    session.close()
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.execute(
            text(
                """
                INSERT INTO user_story_dependencies
                    (
                        project_id,
                        dependent_story_id,
                        prerequisite_story_id,
                        status,
                        source,
                        confidence
                    )
                VALUES
                    (
                        :project_id,
                        :dependent_story_id,
                        999999,
                        'active',
                        'manual_review',
                        'reviewed'
                    )
                """
            ),
            {
                "project_id": project_id,
                "dependent_story_id": dependent_story_id,
            },
        )
        conn.commit()
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")

    with Session(engine) as fresh_session:
        graph = load_story_dependency_graph(fresh_session, project_id=project_id)

    assert graph.active_edges == {}
    assert [issue.code for issue in graph.issues] == ["STORY_DEPENDENCY_ORPHAN"]
    assert graph.issues[0].story_ids == [999999]


def test_build_dependency_graph_reports_superseded_story(session: Session) -> None:
    """Report active edges pointing at superseded stories."""
    project_id, dependent_story_id, prerequisite_story_id = _story_pair(session)
    prerequisite = session.get(UserStory, prerequisite_story_id)
    assert prerequisite is not None
    prerequisite.is_superseded = True
    session.add(prerequisite)
    session.add(
        UserStoryDependency(
            project_id=project_id,
            dependent_story_id=dependent_story_id,
            prerequisite_story_id=prerequisite_story_id,
            status="active",
        )
    )
    session.commit()

    graph = load_story_dependency_graph(session, project_id=project_id)

    assert graph.active_edges == {}
    assert [issue.code for issue in graph.issues] == [
        "STORY_DEPENDENCY_SUPERSEDED_STORY"
    ]
    assert graph.issues[0].story_ids == [prerequisite_story_id]


def test_detect_cycle_returns_cycle_path() -> None:
    """Return deterministic cycle paths from dependency adjacency."""
    assert detect_dependency_cycles({1: {2}, 2: {3}, 3: {1}}) == [[1, 2, 3, 1]]


def test_inspect_payload_separates_active_and_proposed_edges(session: Session) -> None:
    """Expose active and proposed dependency edges in separate inspect buckets."""
    project_id, story_a, story_b, story_c = _story_set(
        session,
        titles=("A", "B", "C"),
    )
    session.add(
        UserStoryDependency(
            project_id=project_id,
            dependent_story_id=story_b,
            prerequisite_story_id=story_a,
            status="active",
            confidence="reviewed",
            source="manual_review",
        )
    )
    session.add(
        UserStoryDependency(
            project_id=project_id,
            dependent_story_id=story_c,
            prerequisite_story_id=story_b,
            status="proposed",
            confidence="explicit",
            source="story_writer",
        )
    )
    session.commit()

    payload = dependency_inspect_payload(session, project_id=project_id)

    assert payload["active_edge_count"] == 1
    assert payload["proposed_edge_count"] == 1
    assert payload["active_edges"][0]["dependent_story_id"] == story_b
    assert payload["proposed_edges"][0]["dependent_story_id"] == story_c
    assert payload["cycle_count"] == 0
    assert payload["issues"] == []


def test_story_fact_keeps_validation_status_separate_from_dependency_blockers(
    engine: Engine,
) -> None:
    """Validate that structural evidence is distinct from dependency blockers."""
    project_id, roadmap_id = _seed_story_parent(engine)
    with Session(engine) as session:
        artifact = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
            title="Accepted validation target",
            item_count=2,
        )
        result = _decide_story(session, artifact, decision="accepted", offset=2)
        story_id_1 = result.activated_story_ids[0]
        story_id_2 = result.activated_story_ids[1]
        session.commit()

    _validate(engine, story_id_1)
    _validate(engine, story_id_2)

    with Session(engine) as s:
        # Add active dependency edge making story_id_2 depend on incomplete story_id_1
        s.add(
            UserStoryDependency(
                project_id=project_id,
                dependent_story_id=story_id_2,
                prerequisite_story_id=story_id_1,
                status="active",
                confidence="reviewed",
                source="manual_review",
            )
        )
        s.commit()

        # Load facts
        facts = WorkflowFactRepository(s).load(project_id)
        fact_2 = next(item for item in facts.stories if item.story_id == story_id_2)

        # story_id_2 has an unsatisfied prerequisite, so sprint_candidate is False
        assert fact_2.sprint_candidate is False
        assert any("PREREQUISITE" in b for b in fact_2.readiness_blockers)
        # BUT its validation_status MUST be 'validated', NOT 'failed'!
        assert fact_2.validation_status == "validated"
        assert fact_2.validation_failures == ()
