"""Tests for story dependency persistence."""

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, create_engine

from db.migrations import migrate_user_story_dependencies
from models.core import Product, UserStory, UserStoryDependency
from services.story_dependencies import (
    dependency_inspect_payload,
    detect_dependency_cycles,
    load_story_dependency_graph,
)


def _story_pair(session: Session) -> tuple[int, int, int]:
    product = Product(name="Dependency Test Product")
    session.add(product)
    session.commit()
    session.refresh(product)
    assert product.product_id is not None

    prerequisite = UserStory(
        title="Capture market data",
        product_id=product.product_id,
        rank="101",
        source_requirement="REQ.live",
        refinement_slot=1,
        story_origin="refined",
        is_refined=True,
        story_points=2,
    )
    dependent = UserStory(
        title="Generate recommendation",
        product_id=product.product_id,
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
    return product.product_id, dependent.story_id, prerequisite.story_id


def _make_story(
    session: Session,
    *,
    product_id: int,
    title: str,
    slot: int,
) -> int:
    story = UserStory(
        title=title,
        product_id=product_id,
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


def test_dependency_table_accepts_proposed_edge(session: Session) -> None:
    """Persist a proposed dependency edge with review metadata."""
    product_id, dependent_story_id, prerequisite_story_id = _story_pair(session)

    edge = UserStoryDependency(
        product_id=product_id,
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
    product_id, dependent_story_id, prerequisite_story_id = _story_pair(session)
    session.add(
        UserStoryDependency(
            product_id=product_id,
            dependent_story_id=dependent_story_id,
            prerequisite_story_id=prerequisite_story_id,
        )
    )
    session.commit()

    session.add(
        UserStoryDependency(
            product_id=product_id,
            dependent_story_id=dependent_story_id,
            prerequisite_story_id=prerequisite_story_id,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_dependency_validation_rejects_self_edge(session: Session) -> None:
    """Reject dependency edges where a story blocks itself."""
    product_id, dependent_story_id, _ = _story_pair(session)

    session.add(
        UserStoryDependency(
            product_id=product_id,
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


def test_story_dependency_migration_creates_table_and_indexes() -> None:
    """Create dependency table and lookup indexes through migration."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE products (
                    product_id INTEGER PRIMARY KEY,
                    name VARCHAR NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE user_stories (
                    story_id INTEGER PRIMARY KEY,
                    product_id INTEGER NOT NULL REFERENCES products(product_id),
                    title VARCHAR NOT NULL
                )
                """
            )
        )

    actions = migrate_user_story_dependencies(engine)

    assert "created table: user_story_dependencies" in actions
    columns = {
        column["name"]
        for column in inspect(engine).get_columns("user_story_dependencies")
    }
    assert {
        "dependency_id",
        "product_id",
        "dependent_story_id",
        "prerequisite_story_id",
        "status",
        "source",
        "confidence",
        "reason",
        "created_at",
        "updated_at",
    }.issubset(columns)
    index_names = {
        index["name"]
        for index in inspect(engine).get_indexes("user_story_dependencies")
    }
    assert "ix_user_story_dependencies_product_status" in index_names
    assert "ix_user_story_dependencies_dependent_story_id" in index_names
    assert "ix_user_story_dependencies_prerequisite_story_id" in index_names


def test_build_dependency_graph_reports_missing_story(
    engine: Engine,
    session: Session,
) -> None:
    """Report orphaned dependency edges without crashing graph load."""
    product_id, dependent_story_id, _ = _story_pair(session)
    session.close()
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.execute(
            text(
                """
                INSERT INTO user_story_dependencies
                    (
                        product_id,
                        dependent_story_id,
                        prerequisite_story_id,
                        status,
                        source,
                        confidence
                    )
                VALUES
                    (
                        :product_id,
                        :dependent_story_id,
                        999999,
                        'active',
                        'manual_review',
                        'reviewed'
                    )
                """
            ),
            {
                "product_id": product_id,
                "dependent_story_id": dependent_story_id,
            },
        )
        conn.commit()
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")

    with Session(engine) as fresh_session:
        graph = load_story_dependency_graph(fresh_session, project_id=product_id)

    assert graph.active_edges == {}
    assert [issue.code for issue in graph.issues] == ["STORY_DEPENDENCY_ORPHAN"]
    assert graph.issues[0].story_ids == [999999]


def test_build_dependency_graph_reports_superseded_story(session: Session) -> None:
    """Report active edges pointing at superseded stories."""
    product_id, dependent_story_id, prerequisite_story_id = _story_pair(session)
    prerequisite = session.get(UserStory, prerequisite_story_id)
    assert prerequisite is not None
    prerequisite.is_superseded = True
    session.add(prerequisite)
    session.add(
        UserStoryDependency(
            product_id=product_id,
            dependent_story_id=dependent_story_id,
            prerequisite_story_id=prerequisite_story_id,
            status="active",
        )
    )
    session.commit()

    graph = load_story_dependency_graph(session, project_id=product_id)

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
    product = Product(name="Dependency Inspect Product")
    session.add(product)
    session.commit()
    session.refresh(product)
    assert product.product_id is not None
    story_a = _make_story(session, product_id=product.product_id, title="A", slot=1)
    story_b = _make_story(session, product_id=product.product_id, title="B", slot=2)
    story_c = _make_story(session, product_id=product.product_id, title="C", slot=3)
    session.add(
        UserStoryDependency(
            product_id=product.product_id,
            dependent_story_id=story_b,
            prerequisite_story_id=story_a,
            status="active",
            confidence="reviewed",
            source="manual_review",
        )
    )
    session.add(
        UserStoryDependency(
            product_id=product.product_id,
            dependent_story_id=story_c,
            prerequisite_story_id=story_b,
            status="proposed",
            confidence="explicit",
            source="story_writer",
        )
    )
    session.commit()

    payload = dependency_inspect_payload(session, project_id=product.product_id)

    assert payload["active_edge_count"] == 1
    assert payload["proposed_edge_count"] == 1
    assert payload["active_edges"][0]["dependent_story_id"] == story_b
    assert payload["proposed_edges"][0]["dependent_story_id"] == story_c
    assert payload["cycle_count"] == 0
    assert payload["issues"] == []
