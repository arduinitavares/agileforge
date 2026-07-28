"""Product repository deletion tests."""

from dataclasses import dataclass

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine
from sqlmodel import Session, col, select

from models.agent_workbench import (
    DiscoveryChallengeArtifact,
    DiscoveryPrd,
    DiscoverySpecAmendmentDraft,
    GreenfieldDiscoveryChallengeArtifact,
    GreenfieldDiscoveryContext,
    GreenfieldDiscoveryPrd,
    GreenfieldDiscoverySpecAmendmentDraft,
)
from models.authority_curation import (
    AuthorityCurationAttempt,
    AuthorityFeedbackAttempt,
)
from models.core import (
    Epic,
    Feature,
    Product,
    Sprint,
    SprintStory,
    Task,
    Team,
    Theme,
    UserStory,
    UserStoryDependency,
)
from models.enums import TaskStatus, WorkflowEventType
from models.events import TaskExecutionLog, WorkflowEvent
from models.specs import (
    CompiledSpecAuthority,
    SpecAuthorityAcceptance,
    SpecRegistry,
)
from repositories.product import ProductRepository, ProjectDeletionConflictError


@dataclass(frozen=True)
class _SeededAuthorityProject:
    product_id: int
    spec_version_id: int
    authority_ids: frozenset[int]
    acceptance_id: int
    story_ids: tuple[int, int]
    dependency_id: int


def _seed_authority_project(
    session: Session,
    *,
    name: str,
) -> _SeededAuthorityProject:
    product = Product(name=name)
    session.add(product)
    session.flush()
    assert product.product_id is not None
    product_id = product.product_id

    spec = SpecRegistry(
        product_id=product_id,
        spec_hash="spec-hash",
        content="# Approved spec",
        status="approved",
    )
    session.add(spec)
    session.flush()
    assert spec.spec_version_id is not None
    spec_version_id = spec.spec_version_id

    retained_v2 = CompiledSpecAuthority(
        spec_version_id=spec_version_id,
        compiler_version="2.0.0",
        prompt_hash="v2-prompt",
        scope_themes="[]",
        invariants="[]",
        eligible_feature_ids="[]",
    )
    current_v3 = CompiledSpecAuthority(
        spec_version_id=spec_version_id,
        compiler_version="3.0.0",
        prompt_hash="v3-prompt",
        scope_themes="[]",
        invariants="[]",
        eligible_feature_ids="[]",
    )
    session.add(retained_v2)
    session.add(current_v3)
    session.flush()
    assert retained_v2.authority_id is not None
    assert current_v3.authority_id is not None

    acceptance = SpecAuthorityAcceptance(
        product_id=product_id,
        spec_version_id=spec_version_id,
        status="accepted",
        policy="test",
        decided_by="test",
        compiler_version=current_v3.compiler_version,
        prompt_hash=current_v3.prompt_hash,
        spec_hash=spec.spec_hash,
        pending_authority_id=current_v3.authority_id,
    )
    current_story = UserStory(
        title="Current pinned story",
        product_id=product_id,
        accepted_spec_version_id=spec_version_id,
    )
    superseded_story = UserStory(
        title="Superseded pinned story",
        product_id=product_id,
        accepted_spec_version_id=spec_version_id,
    )
    session.add(acceptance)
    session.add(current_story)
    session.add(superseded_story)
    session.flush()
    assert acceptance.id is not None
    assert current_story.story_id is not None
    assert superseded_story.story_id is not None

    superseded_story.superseded_by_story_id = current_story.story_id
    dependency = UserStoryDependency(
        product_id=product_id,
        dependent_story_id=current_story.story_id,
        prerequisite_story_id=superseded_story.story_id,
        status="active",
        source="manual_review",
        confidence="reviewed",
    )
    session.add(superseded_story)
    session.add(dependency)
    session.commit()
    assert dependency.dependency_id is not None

    return _SeededAuthorityProject(
        product_id=product_id,
        spec_version_id=spec_version_id,
        authority_ids=frozenset({retained_v2.authority_id, current_v3.authority_id}),
        acceptance_id=acceptance.id,
        story_ids=(current_story.story_id, superseded_story.story_id),
        dependency_id=dependency.dependency_id,
    )


def test_delete_project_removes_authority_history_and_pinned_story(
    engine: Engine,
) -> None:
    """Delete all retained authority rows after their dependent records."""
    with Session(engine) as session:
        assert (
            session.connection().exec_driver_sql("PRAGMA foreign_keys").scalar_one()
            == 1
        )

        seeded = _seed_authority_project(session, name="Authority history")

        session.expire_all()
        stored_spec = session.get(SpecRegistry, seeded.spec_version_id)
        assert stored_spec is not None
        assert len(stored_spec.compiled_authority) == len(seeded.authority_ids)

        assert ProductRepository(session).delete_project(seeded.product_id) is True

        assert session.get(Product, seeded.product_id) is None
        for story_id in seeded.story_ids:
            assert session.get(UserStory, story_id) is None
        assert session.get(UserStoryDependency, seeded.dependency_id) is None
        assert session.get(SpecAuthorityAcceptance, seeded.acceptance_id) is None
        assert session.get(SpecRegistry, seeded.spec_version_id) is None
        remaining_authority_ids = set(
            session.exec(
                select(CompiledSpecAuthority.authority_id).where(
                    col(CompiledSpecAuthority.authority_id).in_(seeded.authority_ids)
                )
            ).all()
        )
        assert remaining_authority_ids == set()


def test_delete_project_neutralizes_external_story_self_reference(
    engine: Engine,
) -> None:
    """Preserve an outside story after deleting the story it referenced."""
    with Session(engine) as session:
        deleted_product = Product(name="Deleted project")
        surviving_product = Product(name="Surviving project")
        session.add(deleted_product)
        session.add(surviving_product)
        session.flush()
        assert deleted_product.product_id is not None
        assert surviving_product.product_id is not None

        deleted_story = UserStory(
            title="Deleted story",
            product_id=deleted_product.product_id,
        )
        surviving_story = UserStory(
            title="Surviving story",
            product_id=surviving_product.product_id,
        )
        session.add(deleted_story)
        session.add(surviving_story)
        session.flush()
        assert deleted_story.story_id is not None
        assert surviving_story.story_id is not None
        surviving_story.superseded_by_story_id = deleted_story.story_id
        cross_project_dependency = UserStoryDependency(
            product_id=surviving_product.product_id,
            dependent_story_id=surviving_story.story_id,
            prerequisite_story_id=deleted_story.story_id,
            status="active",
            source="manual_review",
            confidence="reviewed",
        )
        session.add(surviving_story)
        session.add(cross_project_dependency)
        session.commit()
        assert cross_project_dependency.dependency_id is not None

        assert (
            ProductRepository(session).delete_project(deleted_product.product_id)
            is True
        )

        stored_survivor = session.get(UserStory, surviving_story.story_id)
        assert stored_survivor is not None
        assert stored_survivor.superseded_by_story_id is None
        assert (
            session.get(
                UserStoryDependency,
                cross_project_dependency.dependency_id,
            )
            is None
        )


def test_delete_project_nulls_surviving_story_feature_reference(
    engine: Engine,
) -> None:
    """Preserve a survivor after deleting the foreign feature it referenced."""
    with Session(engine) as session:
        deleted_product = Product(name="Feature target project")
        surviving_product = Product(name="Feature survivor project")
        session.add(deleted_product)
        session.add(surviving_product)
        session.flush()
        assert deleted_product.product_id is not None
        assert surviving_product.product_id is not None

        theme = Theme(title="Target theme", product_id=deleted_product.product_id)
        session.add(theme)
        session.flush()
        assert theme.theme_id is not None
        epic = Epic(title="Target epic", theme_id=theme.theme_id)
        session.add(epic)
        session.flush()
        assert epic.epic_id is not None
        feature = Feature(title="Target feature", epic_id=epic.epic_id)
        session.add(feature)
        session.flush()
        assert feature.feature_id is not None

        surviving_story = UserStory(
            title="Surviving story with foreign feature",
            product_id=surviving_product.product_id,
            feature_id=feature.feature_id,
        )
        session.add(surviving_story)
        session.commit()
        assert surviving_story.story_id is not None

        assert (
            ProductRepository(session).delete_project(deleted_product.product_id)
            is True
        )

        stored_survivor = session.get(UserStory, surviving_story.story_id)
        assert stored_survivor is not None
        assert stored_survivor.feature_id is None
        assert session.get(Product, surviving_product.product_id) is not None
        assert session.get(Feature, feature.feature_id) is None


def test_delete_project_removes_cross_project_sprint_story_links(
    engine: Engine,
) -> None:
    """Delete pure sprint-story links when either linked parent is deleted."""
    with Session(engine) as session:
        deleted_product = Product(name="Sprint-story target")
        surviving_product = Product(name="Sprint-story survivor")
        team = Team(name="Cross-project sprint team")
        session.add(deleted_product)
        session.add(surviving_product)
        session.add(team)
        session.flush()
        assert deleted_product.product_id is not None
        assert surviving_product.product_id is not None
        assert team.team_id is not None

        deleted_story = UserStory(
            title="Target story",
            product_id=deleted_product.product_id,
        )
        surviving_story = UserStory(
            title="Surviving story",
            product_id=surviving_product.product_id,
        )
        deleted_sprint = Sprint(
            product_id=deleted_product.product_id,
            team_id=team.team_id,
        )
        surviving_sprint = Sprint(
            product_id=surviving_product.product_id,
            team_id=team.team_id,
        )
        session.add(deleted_story)
        session.add(surviving_story)
        session.add(deleted_sprint)
        session.add(surviving_sprint)
        session.flush()
        assert deleted_story.story_id is not None
        assert surviving_story.story_id is not None
        assert deleted_sprint.sprint_id is not None
        assert surviving_sprint.sprint_id is not None

        target_story_link = SprintStory(
            sprint_id=surviving_sprint.sprint_id,
            story_id=deleted_story.story_id,
        )
        target_sprint_link = SprintStory(
            sprint_id=deleted_sprint.sprint_id,
            story_id=surviving_story.story_id,
        )
        session.add(target_story_link)
        session.add(target_sprint_link)
        session.commit()

        assert (
            ProductRepository(session).delete_project(deleted_product.product_id)
            is True
        )

        assert session.get(Product, surviving_product.product_id) is not None
        assert session.get(UserStory, surviving_story.story_id) is not None
        assert session.get(Sprint, surviving_sprint.sprint_id) is not None
        assert (
            session.get(
                SprintStory,
                (surviving_sprint.sprint_id, deleted_story.story_id),
            )
            is None
        )
        assert (
            session.get(
                SprintStory,
                (deleted_sprint.sprint_id, surviving_story.story_id),
            )
            is None
        )


def test_delete_project_removes_foreign_product_acceptance_history(
    engine: Engine,
) -> None:
    """Delete acceptance history whose referenced spec is being deleted."""
    with Session(engine) as session:
        seeded = _seed_authority_project(session, name="Acceptance target")
        surviving_product = Product(name="Acceptance survivor")
        session.add(surviving_product)
        session.flush()
        assert surviving_product.product_id is not None
        target_spec = session.get(SpecRegistry, seeded.spec_version_id)
        assert target_spec is not None
        target_authority_id = next(iter(seeded.authority_ids))
        target_authority = session.get(CompiledSpecAuthority, target_authority_id)
        assert target_authority is not None

        foreign_acceptance = SpecAuthorityAcceptance(
            product_id=surviving_product.product_id,
            spec_version_id=seeded.spec_version_id,
            status="accepted",
            policy="test",
            decided_by="test",
            compiler_version=target_authority.compiler_version,
            prompt_hash=target_authority.prompt_hash,
            spec_hash=target_spec.spec_hash,
            pending_authority_id=target_authority_id,
        )
        session.add(foreign_acceptance)
        session.commit()
        assert foreign_acceptance.id is not None

        assert ProductRepository(session).delete_project(seeded.product_id) is True

        assert session.get(Product, surviving_product.product_id) is not None
        assert session.get(SpecAuthorityAcceptance, foreign_acceptance.id) is None
        assert session.get(SpecRegistry, seeded.spec_version_id) is None


def test_delete_project_removes_misowned_story_dependency(
    engine: Engine,
) -> None:
    """Delete a pure dependency row owned by the project, preserving its stories."""
    with Session(engine) as session:
        deleted_product = Product(name="Dependency owner target")
        surviving_product = Product(name="Dependency story survivor")
        session.add(deleted_product)
        session.add(surviving_product)
        session.flush()
        assert deleted_product.product_id is not None
        assert surviving_product.product_id is not None

        dependent_story = UserStory(
            title="Surviving dependent",
            product_id=surviving_product.product_id,
        )
        prerequisite_story = UserStory(
            title="Surviving prerequisite",
            product_id=surviving_product.product_id,
        )
        session.add(dependent_story)
        session.add(prerequisite_story)
        session.flush()
        assert dependent_story.story_id is not None
        assert prerequisite_story.story_id is not None

        dependency = UserStoryDependency(
            product_id=deleted_product.product_id,
            dependent_story_id=dependent_story.story_id,
            prerequisite_story_id=prerequisite_story.story_id,
            status="active",
            source="manual_review",
            confidence="reviewed",
        )
        session.add(dependency)
        session.commit()
        assert dependency.dependency_id is not None

        assert (
            ProductRepository(session).delete_project(deleted_product.product_id)
            is True
        )

        assert session.get(Product, surviving_product.product_id) is not None
        assert session.get(UserStory, dependent_story.story_id) is not None
        assert session.get(UserStory, prerequisite_story.story_id) is not None
        assert session.get(UserStoryDependency, dependency.dependency_id) is None


def test_delete_progressed_project_removes_task_execution_logs(
    engine: Engine,
) -> None:
    """Delete execution history before its task and sprint parents."""
    with Session(engine) as session:
        assert (
            session.connection().exec_driver_sql("PRAGMA foreign_keys").scalar_one()
            == 1
        )

        product = Product(name="Progressed project")
        team = Team(name="Delivery team")
        session.add(product)
        session.add(team)
        session.flush()
        assert product.product_id is not None
        assert team.team_id is not None

        story = UserStory(title="Progressed story", product_id=product.product_id)
        sprint = Sprint(product_id=product.product_id, team_id=team.team_id)
        session.add(story)
        session.add(sprint)
        session.flush()
        assert story.story_id is not None
        assert sprint.sprint_id is not None

        task = Task(description="Progressed task", story_id=story.story_id)
        session.add(task)
        session.add(SprintStory(sprint_id=sprint.sprint_id, story_id=story.story_id))
        session.flush()
        assert task.task_id is not None

        execution_log = TaskExecutionLog(
            task_id=task.task_id,
            sprint_id=sprint.sprint_id,
            old_status=TaskStatus.TO_DO,
            new_status=TaskStatus.IN_PROGRESS,
        )
        session.add(execution_log)
        session.commit()
        assert execution_log.log_id is not None

        product_id = product.product_id
        story_id = story.story_id
        sprint_id = sprint.sprint_id
        task_id = task.task_id
        execution_log_id = execution_log.log_id

        assert ProductRepository(session).delete_project(product_id) is True

        assert session.get(Product, product_id) is None
        assert session.get(UserStory, story_id) is None
        assert session.get(Sprint, sprint_id) is None
        assert session.get(Task, task_id) is None
        assert session.get(TaskExecutionLog, execution_log_id) is None
        assert session.get(Team, team.team_id) is not None


def test_delete_project_removes_sprint_only_workflow_events(engine: Engine) -> None:
    """Delete events linked through a target sprint even without a product ID."""
    with Session(engine) as session:
        assert (
            session.connection().exec_driver_sql("PRAGMA foreign_keys").scalar_one()
            == 1
        )

        deleted_product = Product(name="Event target project")
        surviving_product = Product(name="Event survivor project")
        team = Team(name="Event delivery team")
        session.add(deleted_product)
        session.add(surviving_product)
        session.add(team)
        session.flush()
        assert deleted_product.product_id is not None
        assert surviving_product.product_id is not None
        assert team.team_id is not None

        deleted_sprint = Sprint(
            product_id=deleted_product.product_id,
            team_id=team.team_id,
        )
        surviving_sprint = Sprint(
            product_id=surviving_product.product_id,
            team_id=team.team_id,
        )
        session.add(deleted_sprint)
        session.add(surviving_sprint)
        session.flush()
        assert deleted_sprint.sprint_id is not None
        assert surviving_sprint.sprint_id is not None

        deleted_event = WorkflowEvent(
            event_type=WorkflowEventType.SPRINT_STARTED,
            product_id=None,
            sprint_id=deleted_sprint.sprint_id,
        )
        surviving_event = WorkflowEvent(
            event_type=WorkflowEventType.SPRINT_STARTED,
            product_id=surviving_product.product_id,
            sprint_id=surviving_sprint.sprint_id,
        )
        session.add(deleted_event)
        session.add(surviving_event)
        session.commit()
        assert deleted_event.event_id is not None
        assert surviving_event.event_id is not None

        deleted_product_id = deleted_product.product_id
        deleted_sprint_id = deleted_sprint.sprint_id
        deleted_event_id = deleted_event.event_id
        surviving_product_id = surviving_product.product_id
        surviving_sprint_id = surviving_sprint.sprint_id
        surviving_event_id = surviving_event.event_id

        assert ProductRepository(session).delete_project(deleted_product_id) is True

        assert session.get(Product, deleted_product_id) is None
        assert session.get(Sprint, deleted_sprint_id) is None
        assert session.get(WorkflowEvent, deleted_event_id) is None
        assert session.get(Product, surviving_product_id) is not None
        assert session.get(Sprint, surviving_sprint_id) is not None
        assert session.get(WorkflowEvent, surviving_event_id) is not None


def test_delete_project_removes_authority_curation_rows(engine: Engine) -> None:
    """Delete curation attempts that directly reference the project."""
    with Session(engine) as session:
        assert (
            session.connection().exec_driver_sql("PRAGMA foreign_keys").scalar_one()
            == 1
        )

        product = Product(name="Curated project")
        session.add(product)
        session.flush()
        assert product.product_id is not None

        feedback = AuthorityFeedbackAttempt(
            project_id=product.product_id,
            feedback_attempt_id="feedback-delete",
            source_authority_id=1,
            source_authority_fingerprint="sha256:authority",
            feedback_fingerprint="sha256:feedback",
            feedback_json="{}",
            request_hash="sha256:feedback-request",
            idempotency_key="feedback-delete",
        )
        curation = AuthorityCurationAttempt(
            project_id=product.product_id,
            curation_attempt_id="curation-delete",
            source_authority_id=1,
            source_authority_fingerprint="sha256:authority",
            spec_version_id=1,
            feedback_attempt_id=feedback.feedback_attempt_id,
            request_hash="sha256:curation-request",
            idempotency_key="curation-delete",
        )
        session.add(feedback)
        session.add(curation)
        session.commit()
        assert feedback.feedback_row_id is not None
        assert curation.curation_row_id is not None

        product_id = product.product_id
        feedback_row_id = feedback.feedback_row_id
        curation_row_id = curation.curation_row_id

        assert ProductRepository(session).delete_project(product_id) is True

        assert session.get(Product, product_id) is None
        assert session.get(AuthorityFeedbackAttempt, feedback_row_id) is None
        assert session.get(AuthorityCurationAttempt, curation_row_id) is None


def test_delete_project_removes_discovery_rows(  # noqa: PLR0915
    engine: Engine,
) -> None:
    """Delete project and linked-greenfield discovery artifact chains."""
    with Session(engine) as session:
        assert (
            session.connection().exec_driver_sql("PRAGMA foreign_keys").scalar_one()
            == 1
        )

        product = Product(name="Discovered project")
        session.add(product)
        session.flush()
        assert product.product_id is not None

        challenge = DiscoveryChallengeArtifact(
            project_id=product.product_id,
            producer="test",
            readiness="ready_for_prd",
            original_idea="Delete this project.",
            content_json="{}",
            artifact_fingerprint="challenge-fingerprint",
            request_hash="challenge-request",
            idempotency_key="challenge-delete",
        )
        session.add(challenge)
        session.flush()
        assert challenge.challenge_artifact_id is not None

        prd = DiscoveryPrd(
            project_id=product.product_id,
            challenge_artifact_id=challenge.challenge_artifact_id,
            producer="test",
            status="accepted",
            version="1",
            title="Delete project PRD",
            content_json="{}",
            artifact_fingerprint="prd-fingerprint",
            request_hash="prd-request",
            idempotency_key="prd-delete",
        )
        session.add(prd)
        session.flush()
        assert prd.prd_id is not None

        draft = DiscoverySpecAmendmentDraft(
            project_id=product.product_id,
            prd_id=prd.prd_id,
            challenge_artifact_id=challenge.challenge_artifact_id,
            status="accepted",
            amendment_file="spec.md",
            content_json="{}",
            validation_json="{}",
            artifact_fingerprint="draft-fingerprint",
            request_hash="draft-request",
            idempotency_key="draft-delete",
        )
        session.add(draft)

        context = GreenfieldDiscoveryContext(
            context_key="greenfield-delete",
            project_id=product.product_id,
            status="project_created",
            request_hash="greenfield-context-request",
            idempotency_key="greenfield-context-delete",
        )
        session.add(context)
        session.flush()
        assert context.greenfield_context_id is not None

        greenfield_challenge = GreenfieldDiscoveryChallengeArtifact(
            greenfield_context_id=context.greenfield_context_id,
            producer="test",
            readiness="ready_for_prd",
            original_idea="Create then delete this project.",
            content_json="{}",
            artifact_fingerprint="greenfield-challenge-fingerprint",
            request_hash="greenfield-challenge-request",
            idempotency_key="greenfield-challenge-delete",
        )
        session.add(greenfield_challenge)
        session.flush()
        assert greenfield_challenge.challenge_artifact_id is not None

        greenfield_prd = GreenfieldDiscoveryPrd(
            greenfield_context_id=context.greenfield_context_id,
            challenge_artifact_id=greenfield_challenge.challenge_artifact_id,
            producer="test",
            status="accepted",
            version="1",
            title="Greenfield delete project PRD",
            content_json="{}",
            artifact_fingerprint="greenfield-prd-fingerprint",
            request_hash="greenfield-prd-request",
            idempotency_key="greenfield-prd-delete",
        )
        session.add(greenfield_prd)
        session.flush()
        assert greenfield_prd.prd_id is not None

        greenfield_draft = GreenfieldDiscoverySpecAmendmentDraft(
            greenfield_context_id=context.greenfield_context_id,
            prd_id=greenfield_prd.prd_id,
            challenge_artifact_id=greenfield_challenge.challenge_artifact_id,
            status="accepted",
            amendment_file="greenfield-spec.md",
            content_json="{}",
            validation_json="{}",
            artifact_fingerprint="greenfield-draft-fingerprint",
            request_hash="greenfield-draft-request",
            idempotency_key="greenfield-draft-delete",
        )
        session.add(greenfield_draft)
        session.commit()
        assert draft.spec_amendment_draft_id is not None
        assert greenfield_draft.spec_amendment_draft_id is not None

        product_id = product.product_id
        challenge_id = challenge.challenge_artifact_id
        prd_id = prd.prd_id
        draft_id = draft.spec_amendment_draft_id
        context_id = context.greenfield_context_id
        greenfield_challenge_id = greenfield_challenge.challenge_artifact_id
        greenfield_prd_id = greenfield_prd.prd_id
        greenfield_draft_id = greenfield_draft.spec_amendment_draft_id

        assert ProductRepository(session).delete_project(product_id) is True

        assert session.get(Product, product_id) is None
        assert session.get(DiscoveryChallengeArtifact, challenge_id) is None
        assert session.get(DiscoveryPrd, prd_id) is None
        assert session.get(DiscoverySpecAmendmentDraft, draft_id) is None
        assert session.get(GreenfieldDiscoveryContext, context_id) is None
        assert (
            session.get(
                GreenfieldDiscoveryChallengeArtifact,
                greenfield_challenge_id,
            )
            is None
        )
        assert session.get(GreenfieldDiscoveryPrd, greenfield_prd_id) is None
        assert (
            session.get(
                GreenfieldDiscoverySpecAmendmentDraft,
                greenfield_draft_id,
            )
            is None
        )


def test_delete_project_nulls_surviving_discovery_prd_reference(
    engine: Engine,
) -> None:
    """Preserve a PRD after deleting the cross-project PRD it superseded."""
    with Session(engine) as session:
        assert (
            session.connection().exec_driver_sql("PRAGMA foreign_keys").scalar_one()
            == 1
        )

        deleted_product = Product(name="Discovery target project")
        surviving_product = Product(name="Discovery survivor project")
        session.add(deleted_product)
        session.add(surviving_product)
        session.flush()
        assert deleted_product.product_id is not None
        assert surviving_product.product_id is not None

        deleted_challenge = DiscoveryChallengeArtifact(
            project_id=deleted_product.product_id,
            producer="test",
            readiness="ready_for_prd",
            original_idea="Delete this discovery graph.",
            content_json="{}",
            artifact_fingerprint="deleted-challenge-fingerprint",
            request_hash="deleted-challenge-request",
            idempotency_key="deleted-challenge",
        )
        surviving_challenge = DiscoveryChallengeArtifact(
            project_id=surviving_product.product_id,
            producer="test",
            readiness="ready_for_prd",
            original_idea="Preserve this discovery graph.",
            content_json="{}",
            artifact_fingerprint="surviving-challenge-fingerprint",
            request_hash="surviving-challenge-request",
            idempotency_key="surviving-challenge",
        )
        session.add(deleted_challenge)
        session.add(surviving_challenge)
        session.flush()
        assert deleted_challenge.challenge_artifact_id is not None
        assert surviving_challenge.challenge_artifact_id is not None

        deleted_prd = DiscoveryPrd(
            project_id=deleted_product.product_id,
            challenge_artifact_id=deleted_challenge.challenge_artifact_id,
            producer="test",
            status="accepted",
            version="1",
            title="Deleted PRD",
            content_json="{}",
            artifact_fingerprint="deleted-prd-fingerprint",
            request_hash="deleted-prd-request",
            idempotency_key="deleted-prd",
        )
        session.add(deleted_prd)
        session.flush()
        assert deleted_prd.prd_id is not None

        surviving_prd = DiscoveryPrd(
            project_id=surviving_product.product_id,
            challenge_artifact_id=surviving_challenge.challenge_artifact_id,
            producer="test",
            status="accepted",
            version="2",
            title="Surviving PRD",
            content_json="{}",
            supersedes_prd_id=deleted_prd.prd_id,
            artifact_fingerprint="surviving-prd-fingerprint",
            request_hash="surviving-prd-request",
            idempotency_key="surviving-prd",
        )
        session.add(surviving_prd)
        session.commit()
        assert surviving_prd.prd_id is not None

        deleted_product_id = deleted_product.product_id
        deleted_prd_id = deleted_prd.prd_id
        surviving_product_id = surviving_product.product_id
        surviving_challenge_id = surviving_challenge.challenge_artifact_id
        surviving_prd_id = surviving_prd.prd_id

        assert ProductRepository(session).delete_project(deleted_product_id) is True

        assert session.get(Product, deleted_product_id) is None
        assert session.get(DiscoveryPrd, deleted_prd_id) is None
        assert session.get(Product, surviving_product_id) is not None
        assert (
            session.get(DiscoveryChallengeArtifact, surviving_challenge_id) is not None
        )
        stored_survivor = session.get(DiscoveryPrd, surviving_prd_id)
        assert stored_survivor is not None
        assert stored_survivor.supersedes_prd_id is None


def test_delete_project_nulls_surviving_story_spec_pin(engine: Engine) -> None:
    """Preserve another project's story after deleting its pinned spec."""
    with Session(engine) as session:
        seeded = _seed_authority_project(session, name="Pinned spec target")
        surviving_product = Product(name="Pinned story survivor")
        session.add(surviving_product)
        session.flush()
        assert surviving_product.product_id is not None

        surviving_story = UserStory(
            title="Cross-project pinned story",
            product_id=surviving_product.product_id,
            accepted_spec_version_id=seeded.spec_version_id,
        )
        session.add(surviving_story)
        session.commit()
        assert surviving_story.story_id is not None

        assert ProductRepository(session).delete_project(seeded.product_id) is True

        stored_survivor = session.get(UserStory, surviving_story.story_id)
        assert stored_survivor is not None
        assert stored_survivor.accepted_spec_version_id is None
        assert session.get(Product, surviving_product.product_id) is not None
        assert session.get(SpecRegistry, seeded.spec_version_id) is None


def test_delete_project_rejects_cross_project_discovery_dependency(
    engine: Engine,
) -> None:
    """Reject deletion before mutation when another project requires its challenge."""
    with Session(engine) as session:
        deleted_product = Product(name="Discovery dependency target")
        surviving_product = Product(name="Discovery dependency survivor")
        session.add(deleted_product)
        session.add(surviving_product)
        session.flush()
        assert deleted_product.product_id is not None
        assert surviving_product.product_id is not None

        deleted_challenge = DiscoveryChallengeArtifact(
            project_id=deleted_product.product_id,
            producer="test",
            readiness="ready_for_prd",
            original_idea="A challenge referenced by another project.",
            content_json="{}",
            artifact_fingerprint="cross-project-challenge-fingerprint",
            request_hash="cross-project-challenge-request",
            idempotency_key="cross-project-challenge",
        )
        session.add(deleted_challenge)
        session.flush()
        assert deleted_challenge.challenge_artifact_id is not None

        surviving_prd = DiscoveryPrd(
            project_id=surviving_product.product_id,
            challenge_artifact_id=deleted_challenge.challenge_artifact_id,
            producer="test",
            status="accepted",
            version="1",
            title="Cross-project dependent PRD",
            content_json="{}",
            artifact_fingerprint="cross-project-prd-fingerprint",
            request_hash="cross-project-prd-request",
            idempotency_key="cross-project-prd",
        )
        session.add(surviving_prd)
        session.commit()
        assert surviving_prd.prd_id is not None

        dml_statements: list[str] = []

        def capture_dml(
            _connection: Connection,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            operation = statement.lstrip().partition(" ")[0].upper()
            if operation in {"DELETE", "UPDATE"}:
                dml_statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture_dml)
        try:
            with pytest.raises(ProjectDeletionConflictError) as exc_info:
                ProductRepository(session).delete_project(deleted_product.product_id)
        finally:
            event.remove(engine, "before_cursor_execute", capture_dml)

        assert str(exc_info.value) == (
            "Project deletion blocked by cross-project discovery references."
        )
        assert exc_info.value.references == ("discovery_prds.challenge_artifact_id",)
        assert dml_statements == []
        assert session.get(Product, deleted_product.product_id) is not None
        assert (
            session.get(
                DiscoveryChallengeArtifact,
                deleted_challenge.challenge_artifact_id,
            )
            is not None
        )
        assert session.get(Product, surviving_product.product_id) is not None
        assert session.get(DiscoveryPrd, surviving_prd.prd_id) is not None


def test_delete_project_inventories_all_cross_project_discovery_dependencies(
    engine: Engine,
) -> None:
    """Report every non-null discovery FK that would orphan surviving data."""
    with Session(engine) as session:
        deleted_product = Product(name="Discovery inventory target")
        surviving_product = Product(name="Discovery inventory survivor")
        session.add(deleted_product)
        session.add(surviving_product)
        session.flush()
        assert deleted_product.product_id is not None
        assert surviving_product.product_id is not None

        target_challenge = DiscoveryChallengeArtifact(
            project_id=deleted_product.product_id,
            producer="test",
            readiness="ready_for_prd",
            original_idea="Inventory inbound references.",
            content_json="{}",
            artifact_fingerprint="inventory-target-challenge",
            request_hash="inventory-target-challenge-request",
            idempotency_key="inventory-target-challenge",
        )
        session.add(target_challenge)
        session.flush()
        assert target_challenge.challenge_artifact_id is not None

        target_prd = DiscoveryPrd(
            project_id=deleted_product.product_id,
            challenge_artifact_id=target_challenge.challenge_artifact_id,
            producer="test",
            status="accepted",
            version="1",
            title="Inventory target PRD",
            content_json="{}",
            artifact_fingerprint="inventory-target-prd",
            request_hash="inventory-target-prd-request",
            idempotency_key="inventory-target-prd",
        )
        survivor_prd = DiscoveryPrd(
            project_id=surviving_product.product_id,
            challenge_artifact_id=target_challenge.challenge_artifact_id,
            producer="test",
            status="accepted",
            version="1",
            title="Inventory survivor PRD",
            content_json="{}",
            artifact_fingerprint="inventory-survivor-prd",
            request_hash="inventory-survivor-prd-request",
            idempotency_key="inventory-survivor-prd",
        )
        session.add(target_prd)
        session.add(survivor_prd)
        session.flush()
        assert target_prd.prd_id is not None

        survivor_draft = DiscoverySpecAmendmentDraft(
            project_id=surviving_product.product_id,
            prd_id=target_prd.prd_id,
            challenge_artifact_id=target_challenge.challenge_artifact_id,
            status="ready_for_review",
            amendment_file="inventory.md",
            content_json="{}",
            validation_json="{}",
            artifact_fingerprint="inventory-survivor-draft",
            request_hash="inventory-survivor-draft-request",
            idempotency_key="inventory-survivor-draft",
        )
        target_context = GreenfieldDiscoveryContext(
            context_key="inventory-target-context",
            project_id=deleted_product.product_id,
            status="project_created",
            request_hash="inventory-target-context-request",
            idempotency_key="inventory-target-context",
        )
        survivor_context = GreenfieldDiscoveryContext(
            context_key="inventory-survivor-context",
            project_id=surviving_product.product_id,
            status="project_created",
            request_hash="inventory-survivor-context-request",
            idempotency_key="inventory-survivor-context",
        )
        session.add(survivor_draft)
        session.add(target_context)
        session.add(survivor_context)
        session.flush()
        assert target_context.greenfield_context_id is not None
        assert survivor_context.greenfield_context_id is not None

        target_greenfield_challenge = GreenfieldDiscoveryChallengeArtifact(
            greenfield_context_id=target_context.greenfield_context_id,
            producer="test",
            readiness="ready_for_prd",
            original_idea="Inventory greenfield inbound references.",
            content_json="{}",
            artifact_fingerprint="inventory-target-greenfield-challenge",
            request_hash="inventory-target-greenfield-challenge-request",
            idempotency_key="inventory-target-greenfield-challenge",
        )
        session.add(target_greenfield_challenge)
        session.flush()
        assert target_greenfield_challenge.challenge_artifact_id is not None

        target_greenfield_prd = GreenfieldDiscoveryPrd(
            greenfield_context_id=target_context.greenfield_context_id,
            challenge_artifact_id=target_greenfield_challenge.challenge_artifact_id,
            producer="test",
            status="accepted",
            version="1",
            title="Inventory target greenfield PRD",
            content_json="{}",
            artifact_fingerprint="inventory-target-greenfield-prd",
            request_hash="inventory-target-greenfield-prd-request",
            idempotency_key="inventory-target-greenfield-prd",
        )
        survivor_greenfield_prd = GreenfieldDiscoveryPrd(
            greenfield_context_id=survivor_context.greenfield_context_id,
            challenge_artifact_id=target_greenfield_challenge.challenge_artifact_id,
            producer="test",
            status="accepted",
            version="1",
            title="Inventory survivor greenfield PRD",
            content_json="{}",
            artifact_fingerprint="inventory-survivor-greenfield-prd",
            request_hash="inventory-survivor-greenfield-prd-request",
            idempotency_key="inventory-survivor-greenfield-prd",
        )
        session.add(target_greenfield_prd)
        session.add(survivor_greenfield_prd)
        session.flush()
        assert target_greenfield_prd.prd_id is not None

        survivor_greenfield_draft = GreenfieldDiscoverySpecAmendmentDraft(
            greenfield_context_id=survivor_context.greenfield_context_id,
            prd_id=target_greenfield_prd.prd_id,
            challenge_artifact_id=target_greenfield_challenge.challenge_artifact_id,
            status="ready_for_review",
            amendment_file="inventory-greenfield.md",
            content_json="{}",
            validation_json="{}",
            artifact_fingerprint="inventory-survivor-greenfield-draft",
            request_hash="inventory-survivor-greenfield-draft-request",
            idempotency_key="inventory-survivor-greenfield-draft",
        )
        session.add(survivor_greenfield_draft)
        session.commit()

        with pytest.raises(ProjectDeletionConflictError) as exc_info:
            ProductRepository(session).delete_project(deleted_product.product_id)

        assert exc_info.value.references == (
            "discovery_prds.challenge_artifact_id",
            "discovery_spec_amendment_drafts.challenge_artifact_id",
            "discovery_spec_amendment_drafts.prd_id",
            "greenfield_discovery_prds.challenge_artifact_id",
            "greenfield_discovery_spec_amendment_drafts.challenge_artifact_id",
            "greenfield_discovery_spec_amendment_drafts.prd_id",
        )
        assert session.get(Product, deleted_product.product_id) is not None
        assert session.get(Product, surviving_product.product_id) is not None
        assert (
            session.get(
                DiscoveryChallengeArtifact,
                target_challenge.challenge_artifact_id,
            )
            is not None
        )
        assert session.get(DiscoveryPrd, survivor_prd.prd_id) is not None
        assert (
            session.get(
                GreenfieldDiscoveryContext,
                target_context.greenfield_context_id,
            )
            is not None
        )


def test_delete_project_rolls_back_when_commit_fails(engine: Engine) -> None:
    """Leave persisted project data intact when the transaction cannot commit."""
    with Session(engine) as session:
        seeded = _seed_authority_project(session, name="Rollback project")
        dml_statements: list[str] = []

        def fail_commit(_session: Session) -> None:
            msg = "injected commit failure"
            raise RuntimeError(msg)

        def capture_dml(
            _connection: Connection,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            operation = statement.lstrip().partition(" ")[0].upper()
            if operation in {"DELETE", "UPDATE"}:
                dml_statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture_dml)
        event.listen(session, "before_commit", fail_commit)
        try:
            with pytest.raises(RuntimeError, match="injected commit failure"):
                ProductRepository(session).delete_project(seeded.product_id)
        finally:
            event.remove(session, "before_commit", fail_commit)
            event.remove(engine, "before_cursor_execute", capture_dml)

        assert dml_statements
        assert session.in_transaction() is False
        assert session.get(Product, seeded.product_id) is not None
        stored_spec = session.get(SpecRegistry, seeded.spec_version_id)
        assert stored_spec is not None
        assert len(stored_spec.compiled_authority) == len(seeded.authority_ids)
        assert session.get(SpecAuthorityAcceptance, seeded.acceptance_id) is not None
        for story_id in seeded.story_ids:
            assert session.get(UserStory, story_id) is not None
        stored_superseded_story = session.get(UserStory, seeded.story_ids[1])
        assert stored_superseded_story is not None
        assert stored_superseded_story.superseded_by_story_id == seeded.story_ids[0]
        assert session.get(UserStoryDependency, seeded.dependency_id) is not None
