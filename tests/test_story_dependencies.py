"""Tests for story dependency persistence."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from models.core import UserStory, UserStoryDependency
from models.enums import StoryStatus
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
from services.story_sprint_selection import (
    StorySprintSelectionRequest,
    apply_story_sprint_selection_in_session,
    story_sprint_selection_fact_in_session,
)
from tests.test_create_user_story import (
    _decide_story,
    _record_story,
    _seed_story_parent,
)
from tests.test_story_validation_service import _validate
from tests.workflow.test_planning_transitions import (
    EVALUATED_AT,
    _domain,
    _guards,
    _roadmap_content,
)
from workflow.contracts import WorkflowErrorCode
from workflow.definitions.planning import story_dependency_source_fingerprint
from workflow.facts import StoryDependencyReviewEdgeFact
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.execution_integrity import selected_story_dependency_snapshot
from workflow.planning_integrity import (
    canonical_dependency_edges,
    dependency_edges_payload,
    dependency_review_fingerprint,
)
from workflow.requests import ApplyStoryDependencies
from workflow.requests.planning import ReviewedDependencyEdge

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
            effort_rationale="Straightforward single operation.",
            order_rationale=f"Step {ordinal} in sequence.",
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
            source_fingerprint="sha256:" + ("d" * 64),
            reviewer="dependency-reviewer",
            reviewed_at=REVIEWED_AT,
        ),
    )


def _select_for_sprint(session: Session, *, story_id: int) -> None:
    story = session.get_one(UserStory, story_id)
    current = story_sprint_selection_fact_in_session(session, story=story)
    apply_story_sprint_selection_in_session(
        session,
        StorySprintSelectionRequest(
            project_id=story.project_id,
            story_id=story_id,
            intent="select",
            expected_state_fingerprint=current.state_fingerprint,
            idempotency_key=f"select-dependency-story-{story_id}",
            actor="dependency-reviewer",
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


def test_selected_scope_review_preserves_unrelated_edges_and_external_visibility(
    session: Session,
) -> None:
    """Mutate selected dependents only and retain external prerequisite edges."""
    project_id, prerequisite_id, dependent_id, unrelated_id = _story_set(
        session,
        titles=(
            "External prerequisite",
            "Selected dependent",
            "Unrelated future work",
        ),
    )
    session.add(
        UserStoryDependency(
            project_id=project_id,
            dependent_story_id=unrelated_id,
            prerequisite_story_id=prerequisite_id,
            status="proposed",
            confidence="explicit",
            source="story_writer",
            reason="Future work also observes the external prerequisite.",
        )
    )
    session.commit()
    reviewed = StoryDependencyReviewEdgeFact(
        dependent_story_id=dependent_id,
        prerequisite_story_id=prerequisite_id,
        reason="Selected work requires the external prerequisite.",
    )

    _apply_dependency_review(
        session,
        project_id=project_id,
        selected_story_ids=(dependent_id,),
        reviewed_edges=(reviewed,),
    )
    session.commit()

    rows = {
        (row.dependent_story_id, row.prerequisite_story_id): row
        for row in session.exec(select(UserStoryDependency)).all()
    }
    selected_edge = rows[(dependent_id, prerequisite_id)]
    preserved = rows[(unrelated_id, prerequisite_id)]
    assert selected_edge.status == "active"
    assert selected_edge.source == "manual_review"
    assert preserved.status == "proposed"
    assert preserved.source == "story_writer"
    payload = dependency_inspect_payload(session, project_id=project_id)
    assert any(
        edge["prerequisite_story_id"] == prerequisite_id
        for edge in payload["active_edges"]
    )


def test_external_prerequisite_blocks_until_complete_without_joining_scope(
    engine: Engine,
) -> None:
    """Keep an external prerequisite visible but outside final candidacy."""
    with Session(engine) as session:
        project_id, prerequisite_id, dependent_id, _unrelated_id = _story_set(
            session,
            titles=(
                "External prerequisite",
                "Selected dependent",
                "Unrelated future work",
            ),
        )
    _validate(engine, dependent_id)
    with Session(engine) as session:
        _select_for_sprint(session, story_id=dependent_id)
        session.commit()
        snapshot = WorkflowFactRepository(session).load(project_id)
        source_fingerprint = snapshot.stories[0].selected_scope_fingerprint
        assert source_fingerprint is not None
        apply_story_dependencies_in_session(
            session,
            inputs=ApplyStoryDependenciesInput(
                project_id=project_id,
                selected_story_ids=(dependent_id,),
                reviewed_edges=(
                    StoryDependencyReviewEdgeFact(
                        dependent_story_id=dependent_id,
                        prerequisite_story_id=prerequisite_id,
                        reason="Selected work requires the external prerequisite.",
                    ),
                ),
                source_fingerprint=source_fingerprint,
                reviewer="dependency-reviewer",
                reviewed_at=REVIEWED_AT,
            ),
        )
        session.commit()

        incomplete = WorkflowFactRepository(session).load(project_id)
        incomplete_by_id = {story.story_id: story for story in incomplete.stories}
        assert incomplete_by_id[dependent_id].dependency_safe is False
        assert incomplete_by_id[dependent_id].sprint_candidate is False
        assert incomplete_by_id[prerequisite_id].sprint_candidate is False

        prerequisite = session.get_one(UserStory, prerequisite_id)
        prerequisite.status = StoryStatus.DONE
        session.add(prerequisite)
        session.commit()
        completed = WorkflowFactRepository(session).load(project_id)

    completed_by_id = {story.story_id: story for story in completed.stories}
    assert completed_by_id[dependent_id].dependency_safe is True
    assert completed_by_id[dependent_id].sprint_candidate is True
    assert completed_by_id[prerequisite_id].sprint_candidate is False
    assert any(
        edge.dependent_story_id == dependent_id
        and edge.prerequisite_story_id == prerequisite_id
        and edge.status == "active"
        for edge in completed.story_dependencies
    )
    execution_scope = selected_story_dependency_snapshot(
        completed,
        (dependent_id,),
    )
    assert execution_scope.source_fingerprint == (
        completed_by_id[dependent_id].selected_scope_fingerprint
    )
    assert tuple(edge.dependency_id for edge in execution_scope.dependencies)


def test_selected_dependency_closure_cycle_blocks_candidacy(engine: Engine) -> None:
    """Reject a cycle that returns through one preserved external-dependent row."""
    with Session(engine) as session:
        project_id, selected_id, external_id, _unrelated_id = _story_set(
            session,
            titles=("Selected work", "External prerequisite", "Future work"),
        )
    _validate(engine, selected_id)
    with Session(engine) as session:
        external = session.get_one(UserStory, external_id)
        external.status = StoryStatus.DONE
        session.add(external)
        _select_for_sprint(session, story_id=selected_id)
        session.commit()
        selected_scope = WorkflowFactRepository(session).load(project_id)
        source_fingerprint = next(
            story.selected_scope_fingerprint
            for story in selected_scope.stories
            if story.story_id == selected_id
        )
        assert source_fingerprint is not None
        apply_story_dependencies_in_session(
            session,
            inputs=ApplyStoryDependenciesInput(
                project_id=project_id,
                selected_story_ids=(selected_id,),
                reviewed_edges=(
                    StoryDependencyReviewEdgeFact(
                        dependent_story_id=selected_id,
                        prerequisite_story_id=external_id,
                        reason="Selected work requires the external prerequisite.",
                    ),
                ),
                source_fingerprint=source_fingerprint,
                reviewer="dependency-reviewer",
                reviewed_at=REVIEWED_AT,
            ),
        )
        session.add(
            UserStoryDependency(
                project_id=project_id,
                dependent_story_id=external_id,
                prerequisite_story_id=selected_id,
                status="active",
                confidence="reviewed",
                source="manual_review",
                reason="Preserved external work depends on selected work.",
            )
        )
        session.commit()

        snapshot = WorkflowFactRepository(session).load(project_id)

    selected = next(story for story in snapshot.stories if story.story_id == selected_id)
    assert selected.dependency_safe is False
    assert selected.sprint_candidate is False
    assert any("CYCLE" in blocker for blocker in selected.readiness_blockers)


def test_unrelated_external_cycle_does_not_block_selected_scope(engine: Engine) -> None:
    """Ignore a preserved cycle that is unreachable from selected dependents."""
    with Session(engine) as session:
        project_id, selected_id, first_external_id, second_external_id = _story_set(
            session,
            titles=("Selected work", "Future work one", "Future work two"),
        )
    _validate(engine, selected_id)
    with Session(engine) as session:
        _select_for_sprint(session, story_id=selected_id)
        session.commit()
        selected_scope = WorkflowFactRepository(session).load(project_id)
        source_fingerprint = next(
            story.selected_scope_fingerprint
            for story in selected_scope.stories
            if story.story_id == selected_id
        )
        assert source_fingerprint is not None
        apply_story_dependencies_in_session(
            session,
            inputs=ApplyStoryDependenciesInput(
                project_id=project_id,
                selected_story_ids=(selected_id,),
                reviewed_edges=(),
                source_fingerprint=source_fingerprint,
                reviewer="dependency-reviewer",
                reviewed_at=REVIEWED_AT,
            ),
        )
        session.add_all(
            (
                UserStoryDependency(
                    project_id=project_id,
                    dependent_story_id=first_external_id,
                    prerequisite_story_id=second_external_id,
                    status="active",
                    confidence="reviewed",
                    source="manual_review",
                ),
                UserStoryDependency(
                    project_id=project_id,
                    dependent_story_id=second_external_id,
                    prerequisite_story_id=first_external_id,
                    status="active",
                    confidence="reviewed",
                    source="manual_review",
                ),
            )
        )
        session.commit()

        snapshot = WorkflowFactRepository(session).load(project_id)

    selected = next(story for story in snapshot.stories if story.story_id == selected_id)
    assert selected.dependency_safe is True
    assert selected.sprint_candidate is True


def test_selection_change_invalidates_review_until_exact_scope_is_confirmed(
    engine: Engine,
) -> None:
    """Never infer a new selected scope from an older dependency review."""
    with Session(engine) as session:
        project_id, first_id, second_id, _third_id = _story_set(
            session,
            titles=("First selected", "Second selected", "Future work"),
        )
    _validate(engine, first_id)
    _validate(engine, second_id)
    with Session(engine) as session:
        _select_for_sprint(session, story_id=first_id)
        session.commit()
        first_scope = WorkflowFactRepository(session).load(project_id)
        first_fingerprint = first_scope.stories[0].selected_scope_fingerprint
        assert first_fingerprint is not None
        apply_story_dependencies_in_session(
            session,
            inputs=ApplyStoryDependenciesInput(
                project_id=project_id,
                selected_story_ids=(first_id,),
                reviewed_edges=(),
                source_fingerprint=first_fingerprint,
                reviewer="dependency-reviewer",
                reviewed_at=REVIEWED_AT,
            ),
        )
        session.commit()
        _select_for_sprint(session, story_id=second_id)
        session.commit()
        changed = WorkflowFactRepository(session).load(project_id)

    selected = tuple(
        story
        for story in changed.stories
        if story.sprint_selection_state == "selected"
        and story.structurally_eligible
    )
    assert tuple(story.story_id for story in selected) == tuple(
        sorted((first_id, second_id))
    )
    assert selected[0].selected_scope_fingerprint != first_fingerprint
    assert all(story.dependency_safe is False for story in selected)
    assert all(story.sprint_candidate is False for story in selected)


def test_dependency_review_duplicate_replays_and_changed_payload_conflicts(
    engine: Engine,
) -> None:
    """Replay one exact review and reject a changed duplicate submit."""
    with Session(engine) as session:
        project_id, first_id, second_id, _third_id = _story_set(
            session,
            titles=("First selected", "Second selected", "Future work"),
        )
    _validate(engine, first_id)
    _validate(engine, second_id)
    with Session(engine) as session:
        _select_for_sprint(session, story_id=first_id)
        _select_for_sprint(session, story_id=second_id)
        session.commit()
        snapshot = WorkflowFactRepository(session).load(project_id)
    domain = _domain(engine)
    position = domain.position(project_id)
    request = ApplyStoryDependencies(
        **_guards(position, "planning.story_dependencies"),
        idempotency_key="selected-scope-review-replay",
        selected_story_ids=tuple(sorted((first_id, second_id))),
        reviewed_edges=(),
        source_fingerprint=story_dependency_source_fingerprint(snapshot.stories),
    )

    first = domain.transition(request)
    replay = domain.transition(request)
    conflict = domain.transition(
        ApplyStoryDependencies(
            **_guards(position, "planning.story_dependencies"),
            idempotency_key=request.idempotency_key,
            selected_story_ids=request.selected_story_ids,
            reviewed_edges=(
                ReviewedDependencyEdge(
                    dependent_story_id=second_id,
                    prerequisite_story_id=first_id,
                    reason="Changed duplicate payload.",
                ),
            ),
            source_fingerprint=request.source_fingerprint,
        )
    )

    assert first.ok is True
    assert replay == first.model_copy(update={"replayed": True})
    assert conflict.ok is False
    assert conflict.error is not None
    assert conflict.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
