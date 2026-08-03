"""Tests for story dependency persistence."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from models.core import Project, UserStory, UserStoryDependency
from models.events import WorkflowEvent
from models.workflow import StoryDependencyReview
from repositories.workflow import WorkflowFactRepository
from services.story_dependencies import (
    ApplyStoryDependenciesInput,
    StoryDependencyGraphError,
    apply_story_dependencies_in_session,
    dependency_inspect_payload,
    detect_dependency_cycles,
    load_story_dependency_graph,
)
from workflow.facts import StoryDependencyReviewEdgeFact
from workflow.fingerprints import canonical_json
from workflow.planning_integrity import (
    canonical_dependency_edges,
    dependency_edges_payload,
    dependency_review_fingerprint,
)

REVIEWED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)


def _story_pair(session: Session) -> tuple[int, int, int]:
    product = Project(name="Dependency Test Project")
    session.add(product)
    session.commit()
    session.refresh(product)
    assert product.project_id is not None

    prerequisite = UserStory(
        title="Capture market data",
        project_id=product.project_id,
        rank="101",
        source_requirement="REQ.live",
        refinement_slot=1,
        story_origin="refined",
        is_refined=True,
        story_points=2,
    )
    dependent = UserStory(
        title="Generate recommendation",
        project_id=product.project_id,
        rank="102",
        source_requirement="REQ.live",
        refinement_slot=2,
        story_origin="refined",
        is_refined=True,
        story_points=3,
    )
    session.add(prerequisite)
    session.add(dependent)
    session.commit()
    session.refresh(prerequisite)
    session.refresh(dependent)
    assert prerequisite.story_id is not None
    assert dependent.story_id is not None
    return product.project_id, dependent.story_id, prerequisite.story_id


def _make_story(
    session: Session,
    *,
    project_id: int,
    title: str,
    slot: int,
) -> int:
    story = UserStory(
        title=title,
        project_id=project_id,
        rank=f"10{slot}",
        source_requirement="REQ.live",
        refinement_slot=slot,
        story_origin="refined",
        is_refined=True,
        story_points=1,
    )
    session.add(story)
    session.commit()
    session.refresh(story)
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
    """Reject duplicate dependency edges for one product and story pair."""
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
    product = Project(name="Dependency Inspect Project")
    session.add(product)
    session.commit()
    session.refresh(product)
    assert product.project_id is not None
    story_a = _make_story(session, project_id=product.project_id, title="A", slot=1)
    story_b = _make_story(session, project_id=product.project_id, title="B", slot=2)
    story_c = _make_story(session, project_id=product.project_id, title="C", slot=3)
    session.add(
        UserStoryDependency(
            project_id=product.project_id,
            dependent_story_id=story_b,
            prerequisite_story_id=story_a,
            status="active",
            confidence="reviewed",
            source="manual_review",
        )
    )
    session.add(
        UserStoryDependency(
            project_id=product.project_id,
            dependent_story_id=story_c,
            prerequisite_story_id=story_b,
            status="proposed",
            confidence="explicit",
            source="story_writer",
        )
    )
    session.commit()

    payload = dependency_inspect_payload(session, project_id=product.project_id)

    assert payload["active_edge_count"] == 1
    assert payload["proposed_edge_count"] == 1
    assert payload["active_edges"][0]["dependent_story_id"] == story_b
    assert payload["proposed_edges"][0]["dependent_story_id"] == story_c
    assert payload["cycle_count"] == 0
    assert payload["issues"] == []
