# tests/workflow/test_dependency_validation_scope.py
"""Bounded validation work must retain fresh dependency and lineage facts."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from sqlmodel import Session

from models.core import UserStory
from repositories.workflow import WorkflowFactRepository
from services import story_dependency_lifecycle as lifecycle
from services.story_dependencies import (
    ApplyStoryDependenciesInput,
    apply_story_dependencies_in_session,
    selected_scope_fingerprint,
)
from services.story_sprint_selection import (
    StorySprintSelectionRequest,
    apply_story_sprint_selection_with_receipt_in_session,
    story_sprint_selection_fact_in_session,
)
from tests.workflow import test_superseded_story_dependencies as fixture
from tests.workflow.test_historical_dependency_retirement import (
    _retire_request,
    _terminal_dependency_edge,
)
from workflow.definitions.planning import story_dependency_source_fingerprint
from workflow.facts import StoryDependencyReviewEdgeFact
from workflow.fingerprints import canonical_hash

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def test_retirement_fingerprint_is_fresh_across_session_boundaries(
    engine: Engine,
) -> None:
    """Flushed, rolled-back and other-session retirements have fresh identities."""
    scenario, dependency_id = _terminal_dependency_edge(engine)
    selected = scenario.story_ids[0]
    fixture._review(engine, scenario, selected)
    domain = fixture._domain(engine)
    request = _retire_request(engine, scenario, dependency_id)
    with Session(engine) as session:
        repository = WorkflowFactRepository(session)
        before = repository.load(scenario.project_id)
        source = story_dependency_source_fingerprint(before.stories)
        result = domain.transition_in_session(session, request)
        assert result.ok, result
        session.flush()
        after = repository.load(scenario.project_id)
        assert story_dependency_source_fingerprint(after.stories) != source
        held = session.get_one(UserStory, selected)
        held.title = "Unflushed caller title"
        session.add(held)
        assert repository.load(scenario.project_id) == after
        assert held.title == "Unflushed caller title"
        assert held in session.dirty
        session.rollback()
        assert repository.load(scenario.project_id) == before
    assert fixture._snapshot(engine, scenario) == before
    with Session(engine) as reader:
        repository = WorkflowFactRepository(reader)
        assert repository.load(scenario.project_id) == before
        assert domain.transition(request).ok
        committed = repository.load(scenario.project_id)
        assert story_dependency_source_fingerprint(committed.stories) != source
    assert fixture._snapshot(engine, scenario) == committed


def test_unrelated_retirement_preserves_selected_scope_identity(engine: Engine) -> None:
    """Retirement outside selected intent does not invalidate its review."""
    scenario, dependency_id = _terminal_dependency_edge(engine)
    fixture._review(engine, scenario, scenario.story_ids[2])
    before = fixture._snapshot(engine, scenario)
    assert (
        fixture._domain(engine)
        .transition(_retire_request(engine, scenario, dependency_id))
        .ok
    )
    after = fixture._snapshot(engine, scenario)
    assert story_dependency_source_fingerprint(after.stories) == (
        story_dependency_source_fingerprint(before.stories)
    )
    assert after.story_dependency_reviews == before.story_dependency_reviews
    assert fixture._story(after, scenario.story_ids[2]).sprint_candidate


def test_empty_retirement_scope_keeps_v1_identity() -> None:
    """The existing empty-scope wire identity remains byte-compatible."""
    assert selected_scope_fingerprint(project_id=7, stories=()) == canonical_hash(
        {
            "schema_version": "agileforge.story-selected-scope.v1",
            "project_id": 7,
            "stories": [],
        }
    )


def _count_validation(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    counts = {"graph": 0, "lineage": 0}
    graph = lifecycle._current_graph
    lineage = lifecycle._load_lineage_proof

    def graph_load(
        session: Session, *, project_id: int, identity_token: object | None = None
    ) -> lifecycle._CurrentGraph:
        counts["graph"] += 1
        return graph(session, project_id=project_id, identity_token=identity_token)

    def lineage_load(
        session: Session, *, project_id: int, identity_token: object | None = None
    ) -> lifecycle._LineageProof:
        counts["lineage"] += 1
        return lineage(session, project_id=project_id, identity_token=identity_token)

    monkeypatch.setattr(lifecycle, "_current_graph", graph_load)
    monkeypatch.setattr(lifecycle, "_load_lineage_proof", lineage_load)
    return counts


def test_fact_load_rechecks_supersession_with_retained_orm_row(engine: Engine) -> None:
    """Retained caller identity cannot hide a committed accepted replacement."""
    scenario = fixture._seed(engine)
    a, b = scenario.story_ids[:2]
    fixture._edge(engine, scenario, b, a)
    fixture._review(engine, scenario, b)
    with Session(engine) as reader:
        held = reader.get_one(UserStory, a)
        repository = WorkflowFactRepository(reader)
        repository.load(scenario.project_id)
        fixture._replace(engine, scenario.artifact_ids[0])
        assert not held.is_superseded
        snapshot = repository.load(scenario.project_id)
        assert fixture._story(snapshot, a).is_superseded
        assert not fixture._story(snapshot, b).dependency_safe


def test_fact_load_preserves_unflushed_lineage_edit(engine: Engine) -> None:
    """A read ignores pending caller lineage edits and leaves them untouched."""
    scenario = fixture._seed(engine)
    a, b = scenario.story_ids[:2]
    fixture._edge(engine, scenario, b, a)
    fixture._review(engine, scenario, b)
    fixture._replace(engine, scenario.artifact_ids[0])
    expected = fixture._snapshot(engine, scenario)
    with Session(engine) as reader:
        held = reader.get_one(UserStory, a)
        held.is_superseded = False
        reader.add(held)
        assert WorkflowFactRepository(reader).load(scenario.project_id) == expected
        assert not held.is_superseded
        assert held in reader.dirty
        reader.rollback()


@pytest.mark.parametrize("boundary", ["load", "inspect", "apply"])
def test_recorded_invalidation_reuses_one_graph_and_lineage_per_operation(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    """Repeated consumers must not rebuild the same validated history."""
    scenario = fixture._seed(engine, 12)
    a, b = scenario.story_ids[:2]
    fixture._edge(engine, scenario, b, a)
    fixture._review(engine, scenario, b)
    _, (replacement,) = fixture._replace(engine, scenario.artifact_ids[0])
    assert len(fixture._events(engine)) == 1
    snapshot = fixture._snapshot(engine, scenario)
    counts = _count_validation(monkeypatch)
    if boundary == "load":
        with Session(engine) as session:
            repository = WorkflowFactRepository(session)
            assert repository.load(scenario.project_id) == snapshot
            assert counts == {"graph": 1, "lineage": 1}
            counts.update(graph=0, lineage=0)
            assert repository.load(scenario.project_id) == snapshot
    elif boundary == "inspect":
        assert fixture._inspect(engine, scenario)["issues"]
    else:
        with Session(engine) as session:
            review = apply_story_dependencies_in_session(
                session,
                inputs=ApplyStoryDependenciesInput(
                    project_id=scenario.project_id,
                    selected_story_ids=(b,),
                    reviewed_edges=(
                        StoryDependencyReviewEdgeFact(
                            dependent_story_id=b,
                            prerequisite_story_id=replacement,
                            reason="Confirm the exact current successor.",
                        ),
                    ),
                    source_fingerprint=story_dependency_source_fingerprint(
                        snapshot.stories
                    ),
                    reviewer="operator@example.com",
                    reviewed_at=fixture.EVALUATED_AT,
                ),
            )
            assert (
                json.loads(review.reviewed_edges_json)[0]["prerequisite_story_id"]
                == replacement
            )
            session.rollback()
    assert counts == {"graph": 1, "lineage": 1}


def test_fact_load_rechecks_selection_after_flush_rollback_and_new_session(
    engine: Engine,
) -> None:
    """The same repository instance must not retain an earlier selection proof."""
    scenario = fixture._seed(engine)
    a, b, _ = scenario.story_ids
    fixture._edge(engine, scenario, b, a)
    fixture._review(engine, scenario, b)
    fixture._replace(engine, scenario.artifact_ids[0])
    with Session(engine) as session:
        repository = WorkflowFactRepository(session)
        before = repository.load(scenario.project_id)
        story = session.get_one(UserStory, b)
        selection = story_sprint_selection_fact_in_session(session, story=story)
        apply_story_sprint_selection_with_receipt_in_session(
            session,
            StorySprintSelectionRequest(
                project_id=scenario.project_id,
                story_id=b,
                intent="defer",
                expected_state_fingerprint=selection.state_fingerprint,
                idempotency_key="scope-proof-defer",
                actor="operator@example.com",
                rationale="Defer this exact selected Story.",
            ),
        )
        session.flush()
        deferred = repository.load(scenario.project_id)
        assert fixture._story(deferred, b).sprint_selection_state == "deferred"
        assert (
            fixture._story(deferred, b).selected_scope_fingerprint
            != fixture._story(before, b).selected_scope_fingerprint
        )
        session.rollback()
        assert repository.load(scenario.project_id) == before
    assert fixture._snapshot(engine, scenario) == before


def test_fact_load_rechecks_supersession_after_another_session_commits(
    engine: Engine,
) -> None:
    """A subsequent load must see the current accepted successor lineage."""
    scenario = fixture._seed(engine)
    a, b, _ = scenario.story_ids
    fixture._edge(engine, scenario, b, a)
    fixture._review(engine, scenario, b)
    with Session(engine) as session:
        repository = WorkflowFactRepository(session)
        before = repository.load(scenario.project_id)
        artifact_id, (replacement,) = fixture._replace(engine, scenario.artifact_ids[0])
        after = repository.load(scenario.project_id)
        assert fixture._story(after, a).is_superseded
        assert fixture._story(after, replacement).story_artifact_id == artifact_id
        assert (
            fixture._story(after, b).selected_scope_fingerprint
            != fixture._story(before, b).selected_scope_fingerprint
        )
    assert fixture._snapshot(engine, scenario) == after


def test_implicit_adoption_is_fresh_after_flush_and_rollback(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adoption must not leave a rolled-back event in later fact-load proof."""
    scenario = fixture._seed(engine)
    a, b, _ = scenario.story_ids
    fixture._edge(engine, scenario, b, a)
    fixture._review(engine, scenario, b)
    fixture._legacy_replace(engine, scenario.artifact_ids[0], monkeypatch)
    assert fixture._events(engine) == ()
    with Session(engine) as session:
        repository = WorkflowFactRepository(session)
        before = repository.load(scenario.project_id)
        anchors = lifecycle.infer_unrecorded_story_dependency_invalidations_in_session(
            session, project_id=scenario.project_id, root_story_ids=(b,)
        )
        lifecycle.adopt_story_dependency_invalidations_in_session(
            session,
            project_id=scenario.project_id,
            anchors=anchors,
            reviewed_at=fixture.EVALUATED_AT,
        )
        session.flush()
        assert (
            len(
                lifecycle.load_story_dependency_invalidations_in_session(
                    session, project_id=scenario.project_id
                )
            )
            == 1
        )
        assert repository.load(scenario.project_id) == before
        session.rollback()
        assert repository.load(scenario.project_id) == before
    assert fixture._events(engine) == ()
    assert fixture._snapshot(engine, scenario) == before


@pytest.mark.parametrize("foreign", ["project", "session", "closed"])
def test_scoped_validation_refuses_foreign_or_finished_operation(
    engine: Engine, foreign: str
) -> None:
    """A retained context must not authorize another Project or operation."""
    scenario = fixture._seed(engine)
    with Session(engine) as session:
        context = lifecycle.StoryDependencyValidationContext(
            session, project_id=scenario.project_id
        )
        with context:
            if foreign == "project":
                with pytest.raises(ValueError, match="operation"):
                    lifecycle.load_story_dependency_invalidations_in_session(
                        session, project_id=scenario.project_id + 1, validation=context
                    )
            elif foreign == "session":
                with (
                    Session(engine) as other,
                    pytest.raises(ValueError, match="operation"),
                ):
                    lifecycle.load_story_dependency_invalidations_in_session(
                        other, project_id=scenario.project_id, validation=context
                    )
        if foreign == "closed":
            with pytest.raises(ValueError, match="operation"):
                lifecycle.load_story_dependency_invalidations_in_session(
                    session, project_id=scenario.project_id, validation=context
                )
