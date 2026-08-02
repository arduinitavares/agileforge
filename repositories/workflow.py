"""Read canonical durable facts for one workflow Project."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import TypeAdapter, ValidationError
from sqlmodel import Session, col, select

from models.core import (
    Product,
    Sprint,
    SprintStory,
    Task,
    UserStory,
    UserStoryDependency,
)
from models.enums import SprintStatus, StoryStatus
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from models.workflow import (
    ChallengeArtifact,
    DiscoveryRun,
    DiscoveryRunAbandonment,
    InitialScopeRegistration,
    PrdDecision,
    PrdVersion,
    ProjectAbandonment,
    SpecDraft,
    SpecDraftDecision,
    WorkflowNodeAttempt,
    WorkflowNodeAttemptOutcome,
)
from services.specs.authority_selection import pending_authority_fingerprint
from utils.spec_schemas import SpecAuthorityCompilationSuccess
from workflow.contracts import JsonValue
from workflow.facts import (
    AuthorityFact,
    ChallengeArtifactFact,
    DiscoveryRunAbandonmentFact,
    DiscoveryRunFact,
    InitialScopeRegistrationFact,
    NodeAttemptFact,
    PrdVersionFact,
    ProjectAbandonmentFact,
    ProjectFact,
    ReviewDecisionFact,
    SpecDraftFact,
    SprintFact,
    StoryFact,
    TaskFact,
    WorkflowFactSnapshot,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
type _AuthorityStatus = Literal["pending_review", "accepted", "rejected", "stale"]
type _AttemptOutcome = Literal["success", "failure", "obsolete"]
type _DiscoveryPurpose = Literal["initial", "extension"]
type _ProjectOrigin = Literal["greenfield", "brownfield"]
type _ReviewArtifactType = Literal["prd", "spec_draft", "authority"]
type _ReviewOutcome = Literal["accepted", "rejected", "feedback"]
type _SpecDraftKind = Literal["initial", "amendment"]
type _SprintFactStatus = Literal["planned", "active", "completed"]

_SPRINT_STATUSES: dict[SprintStatus, _SprintFactStatus] = {
    SprintStatus.PLANNED: "planned",
    SprintStatus.ACTIVE: "active",
    SprintStatus.COMPLETED: "completed",
}
_DONE_STORY_STATUSES: frozenset[StoryStatus] = frozenset(
    {StoryStatus.DONE, StoryStatus.ACCEPTED}
)


@dataclass(frozen=True)
class _ReviewDecisionSource:
    """Validated persistence values needed to build one review fact."""

    decision_id: int
    artifact_type: _ReviewArtifactType
    artifact_id: int
    artifact_fingerprint: str
    decision: str
    decided_at: datetime


@dataclass(frozen=True)
class _AuthorityAcceptanceSource:
    """Validated authority decision tied to one exact compiled artifact."""

    decision_id: int
    authority_id: int
    authority_fingerprint: str
    status: str
    decided_at: datetime


@dataclass(frozen=True)
class _AuthorityLoad:
    """Authority facts and their validated review facts from one row set."""

    facts: tuple[AuthorityFact, ...]
    reviews: tuple[ReviewDecisionFact, ...]


class WorkflowFactLoadError(RuntimeError):
    """Raised when stored rows cannot form one consistent Project snapshot."""


class WorkflowFactRepository:
    """Map caller-owned-session rows into immutable workflow facts."""

    def __init__(self, session: Session) -> None:
        """Retain the session whose transaction lifecycle the caller owns."""
        self._session = session
        self._identity_token: object = object()

    def load(self, project_id: int) -> WorkflowFactSnapshot:
        """Load every currently persisted workflow fact for one Project."""
        self._identity_token = object()
        with self._session.no_autoflush:
            return self._load(project_id)

    def _load(self, project_id: int) -> WorkflowFactSnapshot:
        """Build one snapshot inside the read-only session query boundary."""
        project = self._project(project_id)
        discovery_runs = self._discovery_runs(project_id)
        discovery_run_ids = frozenset(item.discovery_run_id for item in discovery_runs)
        spec_versions = self._spec_versions(project_id)
        prd_versions = self._prd_versions(project_id, discovery_run_ids)
        spec_drafts = self._spec_drafts(
            project_id,
            discovery_run_ids,
            spec_versions,
        )
        authority_load = self._authorities(project_id, spec_versions)
        sprints = self._sprints(project_id)
        stories = self._stories(project_id, frozenset(spec_versions))

        return WorkflowFactSnapshot(
            project=project,
            project_abandonments=self._project_abandonments(project_id),
            discovery_runs=discovery_runs,
            discovery_run_abandonments=self._discovery_run_abandonments(
                project_id,
                discovery_run_ids,
            ),
            challenge_artifacts=self._challenge_artifacts(
                project_id,
                discovery_run_ids,
            ),
            prd_versions=prd_versions,
            review_decisions=self._review_decisions(
                project_id,
                discovery_run_ids,
                {
                    item.prd_version_id: (
                        item.discovery_run_id,
                        item.content_fingerprint,
                    )
                    for item in prd_versions
                },
                {
                    item.spec_draft_id: (
                        item.discovery_run_id,
                        item.content_fingerprint,
                    )
                    for item in spec_drafts
                },
                authority_load.reviews,
            ),
            spec_drafts=spec_drafts,
            initial_registrations=self._initial_registrations(
                project_id,
                discovery_run_ids,
                {item.spec_draft_id: item.discovery_run_id for item in spec_drafts},
                spec_versions,
            ),
            authorities=authority_load.facts,
            phase_artifacts=(),
            sprints=sprints,
            stories=stories,
            tasks=self._tasks(
                project_id,
                frozenset(item.sprint_id for item in sprints),
                stories,
            ),
            post_sprint_triage=(),
            node_attempts=self._node_attempts(project_id),
        )

    def _query_options(self) -> dict[str, object]:
        """Isolate canonical reads from pending caller identity-map state."""
        return {
            "autoflush": False,
            "identity_token": self._identity_token,
        }

    def _project(self, project_id: int) -> ProjectFact:
        row = self._session.exec(
            select(Product)
            .where(col(Product.product_id) == project_id)
            .order_by(col(Product.product_id)),
            execution_options=self._query_options(),
        ).one_or_none()
        if row is None:
            message = f"Project {project_id} does not exist."
            raise self._error(message)
        if row.product_id is None:
            message = "Project row has no product_id."
            raise self._error(message)
        if row.origin not in {"greenfield", "brownfield"}:
            message = f"Project {project_id} has invalid origin {row.origin!r}."
            raise self._error(message)
        return ProjectFact(
            project_id=row.product_id,
            name=row.name,
            origin=self._project_origin(row.origin),
            created_at=row.created_at,
        )

    def _project_abandonments(
        self,
        project_id: int,
    ) -> tuple[ProjectAbandonmentFact, ...]:
        rows = self._session.exec(
            select(ProjectAbandonment)
            .where(col(ProjectAbandonment.project_id) == project_id)
            .order_by(
                col(ProjectAbandonment.abandoned_at),
                col(ProjectAbandonment.project_abandonment_id),
            ),
            execution_options=self._query_options(),
        ).all()
        return tuple(
            ProjectAbandonmentFact(
                project_abandonment_id=self._required_id(
                    row.project_abandonment_id,
                    "project abandonment",
                ),
                project_id=row.project_id,
                reason=row.reason,
                abandoned_by=row.abandoned_by,
                abandoned_at=row.abandoned_at,
            )
            for row in rows
        )

    def _discovery_runs(self, project_id: int) -> tuple[DiscoveryRunFact, ...]:
        rows = self._session.exec(
            select(DiscoveryRun)
            .where(col(DiscoveryRun.project_id) == project_id)
            .order_by(col(DiscoveryRun.ordinal), col(DiscoveryRun.discovery_run_id)),
            execution_options=self._query_options(),
        ).all()
        facts: list[DiscoveryRunFact] = []
        for row in rows:
            if row.purpose not in {"initial", "extension"}:
                message = (
                    f"Discovery run {row.discovery_run_id} has invalid purpose "
                    f"{row.purpose!r}."
                )
                raise self._error(message)
            facts.append(
                DiscoveryRunFact(
                    discovery_run_id=self._required_id(
                        row.discovery_run_id,
                        "discovery run",
                    ),
                    project_id=row.project_id,
                    purpose=self._discovery_purpose(row.purpose),
                    ordinal=row.ordinal,
                    created_at=row.created_at,
                    closed_at=row.closed_at,
                )
            )
        return tuple(facts)

    def _discovery_run_abandonments(
        self,
        project_id: int,
        discovery_run_ids: frozenset[int],
    ) -> tuple[DiscoveryRunAbandonmentFact, ...]:
        rows = self._session.exec(
            select(DiscoveryRunAbandonment)
            .where(col(DiscoveryRunAbandonment.project_id) == project_id)
            .order_by(
                col(DiscoveryRunAbandonment.abandoned_at),
                col(DiscoveryRunAbandonment.discovery_run_abandonment_id),
            ),
            execution_options=self._query_options(),
        ).all()
        facts: list[DiscoveryRunAbandonmentFact] = []
        for row in rows:
            self._require_project_run(
                row.discovery_run_id,
                discovery_run_ids,
                "discovery-run abandonment",
            )
            facts.append(
                DiscoveryRunAbandonmentFact(
                    discovery_run_abandonment_id=self._required_id(
                        row.discovery_run_abandonment_id,
                        "discovery-run abandonment",
                    ),
                    project_id=row.project_id,
                    discovery_run_id=row.discovery_run_id,
                    reason=row.reason,
                    abandoned_by=row.abandoned_by,
                    abandoned_at=row.abandoned_at,
                )
            )
        return tuple(facts)

    def _challenge_artifacts(
        self,
        project_id: int,
        discovery_run_ids: frozenset[int],
    ) -> tuple[ChallengeArtifactFact, ...]:
        rows = self._session.exec(
            select(ChallengeArtifact)
            .where(col(ChallengeArtifact.project_id) == project_id)
            .order_by(
                col(ChallengeArtifact.discovery_run_id),
                col(ChallengeArtifact.version_number),
                col(ChallengeArtifact.challenge_artifact_id),
            ),
            execution_options=self._query_options(),
        ).all()
        facts: list[ChallengeArtifactFact] = []
        for row in rows:
            self._require_project_run(
                row.discovery_run_id,
                discovery_run_ids,
                "challenge artifact",
            )
            artifact_id = self._required_id(
                row.challenge_artifact_id,
                "challenge artifact",
            )
            self._validate_canonical_json(
                row.canonical_content_json,
                "challenge artifact",
                artifact_id,
            )
            facts.append(
                ChallengeArtifactFact(
                    challenge_artifact_id=artifact_id,
                    discovery_run_id=row.discovery_run_id,
                    content_fingerprint=row.content_fingerprint,
                    supersedes_id=row.supersedes_challenge_artifact_id,
                )
            )
        runs_by_artifact = {
            item.challenge_artifact_id: item.discovery_run_id for item in facts
        }
        for item in facts:
            self._require_same_run_reference(
                item.supersedes_id,
                item.discovery_run_id,
                runs_by_artifact,
                "challenge artifact supersession",
            )
        return tuple(facts)

    def _prd_versions(
        self,
        project_id: int,
        discovery_run_ids: frozenset[int],
    ) -> tuple[PrdVersionFact, ...]:
        rows = self._session.exec(
            select(PrdVersion)
            .where(col(PrdVersion.project_id) == project_id)
            .order_by(
                col(PrdVersion.discovery_run_id),
                col(PrdVersion.version_number),
                col(PrdVersion.prd_version_id),
            ),
            execution_options=self._query_options(),
        ).all()
        facts: list[PrdVersionFact] = []
        for row in rows:
            self._require_project_run(row.discovery_run_id, discovery_run_ids, "PRD")
            prd_version_id = self._required_id(row.prd_version_id, "PRD")
            self._validate_canonical_json(
                row.canonical_content_json,
                "PRD",
                prd_version_id,
            )
            facts.append(
                PrdVersionFact(
                    prd_version_id=prd_version_id,
                    discovery_run_id=row.discovery_run_id,
                    content_fingerprint=row.content_fingerprint,
                    supersedes_id=row.supersedes_prd_version_id,
                )
            )
        runs_by_prd = {item.prd_version_id: item.discovery_run_id for item in facts}
        for item in facts:
            self._require_same_run_reference(
                item.supersedes_id,
                item.discovery_run_id,
                runs_by_prd,
                "PRD supersession",
            )
        return tuple(facts)

    def _spec_drafts(
        self,
        project_id: int,
        discovery_run_ids: frozenset[int],
        spec_versions: dict[int, str],
    ) -> tuple[SpecDraftFact, ...]:
        rows = self._session.exec(
            select(SpecDraft)
            .where(col(SpecDraft.project_id) == project_id)
            .order_by(
                col(SpecDraft.discovery_run_id),
                col(SpecDraft.version_number),
                col(SpecDraft.spec_draft_id),
            ),
            execution_options=self._query_options(),
        ).all()
        facts: list[SpecDraftFact] = []
        for row in rows:
            self._require_project_run(
                row.discovery_run_id,
                discovery_run_ids,
                "specification draft",
            )
            if row.kind not in {"initial", "amendment"}:
                message = (
                    f"Specification draft {row.spec_draft_id} has invalid kind "
                    f"{row.kind!r}."
                )
                raise self._error(message)
            self._validate_spec_draft_base(row, spec_versions)
            spec_draft_id = self._required_id(
                row.spec_draft_id,
                "specification draft",
            )
            self._validate_canonical_json(
                row.canonical_content_json,
                "specification draft",
                spec_draft_id,
            )
            facts.append(
                SpecDraftFact(
                    spec_draft_id=spec_draft_id,
                    discovery_run_id=row.discovery_run_id,
                    kind=self._spec_draft_kind(row.kind),
                    content_fingerprint=row.content_fingerprint,
                    base_spec_version_id=row.base_spec_version_id,
                    base_spec_hash=row.base_spec_hash,
                    supersedes_id=row.supersedes_spec_draft_id,
                )
            )
        runs_by_draft = {item.spec_draft_id: item.discovery_run_id for item in facts}
        for item in facts:
            self._require_same_run_reference(
                item.supersedes_id,
                item.discovery_run_id,
                runs_by_draft,
                "specification draft supersession",
            )
        return tuple(facts)

    def _spec_versions(self, project_id: int) -> dict[int, str]:
        rows = self._session.exec(
            select(SpecRegistry)
            .where(col(SpecRegistry.product_id) == project_id)
            .order_by(col(SpecRegistry.spec_version_id)),
            execution_options=self._query_options(),
        ).all()
        return {
            self._required_id(row.spec_version_id, "specification registry row"): (
                row.spec_hash
            )
            for row in rows
        }

    def _review_decisions(
        self,
        project_id: int,
        discovery_run_ids: frozenset[int],
        prd_versions: dict[int, tuple[int, str]],
        spec_drafts: dict[int, tuple[int, str]],
        authority_reviews: tuple[ReviewDecisionFact, ...],
    ) -> tuple[ReviewDecisionFact, ...]:
        decisions: list[ReviewDecisionFact] = list(authority_reviews)
        prd_rows = self._session.exec(
            select(PrdDecision)
            .where(col(PrdDecision.project_id) == project_id)
            .order_by(
                col(PrdDecision.decided_at),
                col(PrdDecision.prd_decision_id),
            ),
            execution_options=self._query_options(),
        ).all()
        for row in prd_rows:
            self._require_project_run(
                row.discovery_run_id,
                discovery_run_ids,
                "PRD decision",
            )
            self._require_artifact_parent(
                row.prd_version_id,
                row.discovery_run_id,
                row.artifact_fingerprint,
                prd_versions,
                "PRD decision",
            )
            decisions.append(
                self._review_decision_fact(
                    _ReviewDecisionSource(
                        decision_id=self._required_id(
                            row.prd_decision_id,
                            "PRD decision",
                        ),
                        artifact_type="prd",
                        artifact_id=row.prd_version_id,
                        artifact_fingerprint=row.artifact_fingerprint,
                        decision=row.decision,
                        decided_at=row.decided_at,
                    )
                )
            )
        draft_rows = self._session.exec(
            select(SpecDraftDecision)
            .where(col(SpecDraftDecision.project_id) == project_id)
            .order_by(
                col(SpecDraftDecision.decided_at),
                col(SpecDraftDecision.spec_draft_decision_id),
            ),
            execution_options=self._query_options(),
        ).all()
        for row in draft_rows:
            self._require_project_run(
                row.discovery_run_id,
                discovery_run_ids,
                "specification draft decision",
            )
            self._require_artifact_parent(
                row.spec_draft_id,
                row.discovery_run_id,
                row.artifact_fingerprint,
                spec_drafts,
                "specification draft decision",
            )
            decisions.append(
                self._review_decision_fact(
                    _ReviewDecisionSource(
                        decision_id=self._required_id(
                            row.spec_draft_decision_id,
                            "specification draft decision",
                        ),
                        artifact_type="spec_draft",
                        artifact_id=row.spec_draft_id,
                        artifact_fingerprint=row.artifact_fingerprint,
                        decision=row.decision,
                        decided_at=row.decided_at,
                    )
                )
            )
        return tuple(
            sorted(
                decisions,
                key=lambda item: (
                    item.decided_at,
                    item.artifact_type,
                    item.artifact_id,
                    item.decision_id,
                ),
            )
        )

    def _initial_registrations(
        self,
        project_id: int,
        discovery_run_ids: frozenset[int],
        spec_drafts: dict[int, int],
        spec_versions: dict[int, str],
    ) -> tuple[InitialScopeRegistrationFact, ...]:
        rows = self._session.exec(
            select(InitialScopeRegistration)
            .where(col(InitialScopeRegistration.project_id) == project_id)
            .order_by(col(InitialScopeRegistration.initial_scope_registration_id)),
            execution_options=self._query_options(),
        ).all()
        facts: list[InitialScopeRegistrationFact] = []
        for row in rows:
            self._require_project_run(
                row.discovery_run_id,
                discovery_run_ids,
                "initial-scope registration",
            )
            self._require_same_run_reference(
                row.spec_draft_id,
                row.discovery_run_id,
                spec_drafts,
                "initial-scope registration",
            )
            self._require_fingerprint_reference(
                row.spec_version_id,
                row.spec_hash,
                spec_versions,
                "initial-scope registration",
            )
            facts.append(
                InitialScopeRegistrationFact(
                    registration_id=self._required_id(
                        row.initial_scope_registration_id,
                        "initial-scope registration",
                    ),
                    discovery_run_id=row.discovery_run_id,
                    spec_draft_id=row.spec_draft_id,
                    spec_version_id=row.spec_version_id,
                    spec_hash=row.spec_hash,
                )
            )
        return tuple(facts)

    def _authorities(
        self,
        project_id: int,
        spec_versions: dict[int, str],
    ) -> _AuthorityLoad:
        rows = self._session.exec(
            select(CompiledSpecAuthority, SpecRegistry)
            .join(
                SpecRegistry,
                col(CompiledSpecAuthority.spec_version_id)
                == col(SpecRegistry.spec_version_id),
            )
            .where(col(SpecRegistry.product_id) == project_id)
            .order_by(col(CompiledSpecAuthority.authority_id)),
            execution_options=self._query_options(),
        ).all()
        authority_records: list[
            tuple[int, CompiledSpecAuthority, SpecRegistry, str]
        ] = []
        authorities_by_id: dict[
            int,
            tuple[CompiledSpecAuthority, SpecRegistry, str],
        ] = {}
        for authority, spec in rows:
            authority_id = self._required_id(authority.authority_id, "authority")
            self._require_fingerprint_reference(
                authority.spec_version_id,
                spec.spec_hash,
                spec_versions,
                "authority specification",
            )
            self._validate_authority_json(
                authority.compiled_artifact_json,
                authority_id,
                authority.compiler_version,
                authority.prompt_hash,
            )
            authority_fingerprint = pending_authority_fingerprint(authority)
            if authority_fingerprint is None:
                message = (
                    "Forced relationship corruption in authority: "
                    f"compiled authority {authority_id} has no fingerprint."
                )
                raise self._error(message)
            record = (authority, spec, authority_fingerprint)
            authorities_by_id[authority_id] = record
            authority_records.append((authority_id, *record))

        acceptance_rows = self._session.exec(
            select(SpecAuthorityAcceptance)
            .where(col(SpecAuthorityAcceptance.product_id) == project_id)
            .order_by(
                col(SpecAuthorityAcceptance.decided_at),
                col(SpecAuthorityAcceptance.id),
            ),
            execution_options=self._query_options(),
        ).all()
        acceptances = tuple(
            self._authority_acceptance_source(
                row,
                spec_versions,
                authorities_by_id,
            )
            for row in acceptance_rows
        )
        facts: list[AuthorityFact] = []
        for authority_id, authority, spec, authority_fingerprint in authority_records:
            acceptance = self._latest_acceptance(authority_id, acceptances)
            status, decided_at = self._authority_state(
                spec.status,
                acceptance,
            )
            facts.append(
                AuthorityFact(
                    authority_id=authority_id,
                    spec_version_id=authority.spec_version_id,
                    authority_fingerprint=authority_fingerprint,
                    status=status,
                    decided_at=decided_at,
                )
            )
        reviews = tuple(
            self._review_decision_fact(
                _ReviewDecisionSource(
                    decision_id=item.decision_id,
                    artifact_type="authority",
                    artifact_id=item.authority_id,
                    artifact_fingerprint=item.authority_fingerprint,
                    decision=item.status,
                    decided_at=item.decided_at,
                )
            )
            for item in acceptances
        )
        return _AuthorityLoad(facts=tuple(facts), reviews=reviews)

    def _sprints(self, project_id: int) -> tuple[SprintFact, ...]:
        rows = self._session.exec(
            select(Sprint)
            .where(col(Sprint.product_id) == project_id)
            .order_by(col(Sprint.completed_at), col(Sprint.sprint_id)),
            execution_options=self._query_options(),
        ).all()
        facts: list[SprintFact] = []
        for row in rows:
            status = _SPRINT_STATUSES.get(row.status)
            if status is None:
                message = f"Sprint {row.sprint_id} has invalid status {row.status!r}."
                raise self._error(message)
            facts.append(
                SprintFact(
                    sprint_id=self._required_id(row.sprint_id, "sprint"),
                    status=status,
                    completed_at=row.completed_at,
                )
            )
        return tuple(facts)

    def _stories(
        self,
        project_id: int,
        spec_version_ids: frozenset[int],
    ) -> tuple[StoryFact, ...]:
        rows = self._session.exec(
            select(UserStory)
            .where(col(UserStory.product_id) == project_id)
            .order_by(col(UserStory.rank), col(UserStory.story_id)),
            execution_options=self._query_options(),
        ).all()
        dependencies = self._session.exec(
            select(UserStoryDependency)
            .where(col(UserStoryDependency.product_id) == project_id)
            .order_by(
                col(UserStoryDependency.dependent_story_id),
                col(UserStoryDependency.prerequisite_story_id),
                col(UserStoryDependency.dependency_id),
            ),
            execution_options=self._query_options(),
        ).all()
        stories_by_id = {self._required_id(row.story_id, "story"): row for row in rows}
        story_ids = frozenset(stories_by_id)
        for row in rows:
            if row.accepted_spec_version_id is not None:
                self._require_member(
                    row.accepted_spec_version_id,
                    spec_version_ids,
                    "story accepted specification",
                )
            if row.superseded_by_story_id is not None:
                self._require_member(
                    row.superseded_by_story_id,
                    story_ids,
                    "story supersession",
                )
        for dependency in dependencies:
            self._require_member(
                dependency.dependent_story_id,
                story_ids,
                "story dependency",
            )
            self._require_member(
                dependency.prerequisite_story_id,
                story_ids,
                "story dependency",
            )
        blockers = self._story_readiness_blockers(rows, dependencies, stories_by_id)
        return tuple(
            StoryFact(
                story_id=self._required_id(row.story_id, "story"),
                status=row.status.value,
                sprint_candidate=not blockers[self._required_id(row.story_id, "story")],
                readiness_blockers=blockers[self._required_id(row.story_id, "story")],
            )
            for row in rows
        )

    def _tasks(
        self,
        project_id: int,
        sprint_ids: frozenset[int],
        stories: tuple[StoryFact, ...],
    ) -> tuple[TaskFact, ...]:
        rows = self._session.exec(
            select(Task, UserStory)
            .join(UserStory, col(Task.story_id) == col(UserStory.story_id))
            .where(col(UserStory.product_id) == project_id)
            .order_by(col(Task.task_id)),
            execution_options=self._query_options(),
        ).all()
        stories_by_id = {item.story_id: item for item in stories}
        story_ids = frozenset(stories_by_id)
        memberships = (
            self._session.exec(
                select(SprintStory)
                .where(col(SprintStory.story_id).in_(story_ids))
                .order_by(col(SprintStory.sprint_id), col(SprintStory.story_id)),
                execution_options=self._query_options(),
            ).all()
            if story_ids
            else []
        )
        sprint_ids_by_story: dict[int, list[int]] = {
            story_id: [] for story_id in story_ids
        }
        for membership in memberships:
            self._require_member(
                membership.sprint_id,
                sprint_ids,
                "task sprint relationship",
            )
            sprint_ids_by_story[membership.story_id].append(membership.sprint_id)
        facts: list[TaskFact] = []
        for task, _story in rows:
            task_id = self._required_id(task.task_id, "task")
            task_sprint_ids = sprint_ids_by_story[task.story_id]
            if not task_sprint_ids:
                message = (
                    "Forced relationship corruption in task sprint relationship: "
                    f"task {task_id} has no sprint membership."
                )
                raise self._error(message)
            story = stories_by_id[task.story_id]
            facts.extend(
                TaskFact(
                    task_id=task_id,
                    sprint_id=sprint_id,
                    story_id=task.story_id,
                    status=task.status.value,
                    dependencies_satisfied=not story.readiness_blockers,
                )
                for sprint_id in task_sprint_ids
            )
        return tuple(sorted(facts, key=lambda item: (item.sprint_id, item.task_id)))

    def _node_attempts(self, project_id: int) -> tuple[NodeAttemptFact, ...]:
        attempts = self._session.exec(
            select(WorkflowNodeAttempt)
            .where(col(WorkflowNodeAttempt.project_id) == project_id)
            .order_by(
                col(WorkflowNodeAttempt.started_at),
                col(WorkflowNodeAttempt.workflow_node_attempt_id),
            ),
            execution_options=self._query_options(),
        ).all()
        outcomes = self._session.exec(
            select(WorkflowNodeAttemptOutcome)
            .where(col(WorkflowNodeAttemptOutcome.project_id) == project_id)
            .order_by(
                col(WorkflowNodeAttemptOutcome.workflow_node_attempt_id),
                col(WorkflowNodeAttemptOutcome.workflow_node_attempt_outcome_id),
            ),
            execution_options=self._query_options(),
        ).all()
        attempt_ids = frozenset(
            self._required_id(row.workflow_node_attempt_id, "workflow node attempt")
            for row in attempts
        )
        outcomes_by_attempt: dict[int, WorkflowNodeAttemptOutcome] = {}
        for outcome in outcomes:
            self._require_member(
                outcome.workflow_node_attempt_id,
                attempt_ids,
                "workflow node attempt outcome",
            )
            self._attempt_outcome(outcome.status)
            outcomes_by_attempt[outcome.workflow_node_attempt_id] = outcome
        return tuple(
            NodeAttemptFact(
                attempt_id=self._required_id(
                    row.workflow_node_attempt_id,
                    "workflow node attempt",
                ),
                node_id=row.node_id,
                instance_key=row.instance_key,
                graph_version=row.graph_version,
                input_fingerprint=row.input_fingerprint,
                fact_fingerprint=row.fact_fingerprint,
                business_fact_fingerprint=row.business_fact_fingerprint,
                decision_fingerprint=row.decision_fingerprint,
                attempt_fingerprint=row.attempt_fingerprint,
                model_id=row.model_id,
                lease_expires_at=row.lease_expires_at,
                outcome=self._outcome_for_attempt(row, outcomes_by_attempt),
            )
            for row in attempts
        )

    @staticmethod
    def _required_id(value: int | None, label: str) -> int:
        if value is None:
            message = f"Stored {label} has no primary key."
            raise WorkflowFactRepository._error(message)
        return value

    @staticmethod
    def _require_project_run(
        discovery_run_id: int,
        discovery_run_ids: frozenset[int],
        label: str,
    ) -> None:
        if discovery_run_id not in discovery_run_ids:
            message = (
                f"Forced cross-project corruption in {label}: "
                f"discovery run {discovery_run_id} is not owned by this Project."
            )
            raise WorkflowFactRepository._error(message)

    @staticmethod
    def _require_member(value: int, values: frozenset[int], label: str) -> None:
        if value not in values:
            message = (
                f"Forced cross-project corruption in {label}: "
                f"reference {value} is not owned by this Project."
            )
            raise WorkflowFactRepository._error(message)

    @staticmethod
    def _require_same_run_reference(
        value: int | None,
        discovery_run_id: int,
        runs_by_id: dict[int, int],
        label: str,
    ) -> None:
        if value is None:
            return
        referenced_run_id = runs_by_id.get(value)
        if referenced_run_id != discovery_run_id:
            message = (
                f"Forced relationship corruption in {label}: reference {value} "
                f"does not belong to discovery run {discovery_run_id}."
            )
            raise WorkflowFactRepository._error(message)

    @staticmethod
    def _require_fingerprint_reference(
        value: int,
        fingerprint: str,
        fingerprints_by_id: dict[int, str],
        label: str,
    ) -> None:
        if fingerprints_by_id.get(value) != fingerprint:
            message = (
                f"Forced relationship corruption in {label}: reference {value} "
                "does not match its persisted fingerprint."
            )
            raise WorkflowFactRepository._error(message)

    @staticmethod
    def _require_artifact_parent(
        value: int,
        discovery_run_id: int,
        fingerprint: str,
        artifacts_by_id: dict[int, tuple[int, str]],
        label: str,
    ) -> None:
        if artifacts_by_id.get(value) != (discovery_run_id, fingerprint):
            message = (
                f"Forced relationship corruption in {label}: artifact {value} "
                "does not match its discovery run and fingerprint."
            )
            raise WorkflowFactRepository._error(message)

    @staticmethod
    def _validate_spec_draft_base(
        row: SpecDraft,
        spec_versions: dict[int, str],
    ) -> None:
        if row.kind == "initial":
            if row.base_spec_version_id is None and row.base_spec_hash is None:
                return
        elif row.base_spec_version_id is not None and row.base_spec_hash is not None:
            WorkflowFactRepository._require_fingerprint_reference(
                row.base_spec_version_id,
                row.base_spec_hash,
                spec_versions,
                "specification draft base",
            )
            return
        message = (
            "Forced relationship corruption in specification draft base: "
            f"draft {row.spec_draft_id} has an invalid base relationship."
        )
        raise WorkflowFactRepository._error(message)

    @staticmethod
    def _validate_canonical_json(content: str, label: str, identifier: int) -> None:
        try:
            _JSON_OBJECT.validate_json(content)
        except ValidationError as exc:
            message = f"Stored canonical {label} {identifier} JSON is invalid."
            raise WorkflowFactRepository._error(message) from exc

    @staticmethod
    def _validate_authority_json(
        content: str | None,
        authority_id: int,
        compiler_version: str,
        prompt_hash: str,
    ) -> None:
        if content is None:
            message = f"Stored canonical authority {authority_id} JSON is missing."
            raise WorkflowFactRepository._error(message)
        try:
            artifact = SpecAuthorityCompilationSuccess.model_validate_json(content)
        except ValidationError as exc:
            message = f"Stored canonical authority {authority_id} JSON is invalid."
            raise WorkflowFactRepository._error(message) from exc
        duplicated_provenance = (
            ("compiler_version", artifact.compiler_version, compiler_version),
            ("prompt_hash", artifact.prompt_hash, prompt_hash),
        )
        for field_name, artifact_value, authoritative_value in duplicated_provenance:
            if artifact_value != authoritative_value:
                message = (
                    f"Stored canonical authority {authority_id} {field_name} "
                    "does not match the authoritative compiled authority row."
                )
                raise WorkflowFactRepository._error(message)

    @staticmethod
    def _review_decision_fact(source: _ReviewDecisionSource) -> ReviewDecisionFact:
        return ReviewDecisionFact(
            decision_id=source.decision_id,
            artifact_type=source.artifact_type,
            artifact_id=source.artifact_id,
            artifact_fingerprint=source.artifact_fingerprint,
            decision=WorkflowFactRepository._review_outcome(source.decision),
            decided_at=source.decided_at,
        )

    @staticmethod
    def _authority_acceptance_source(
        row: SpecAuthorityAcceptance,
        spec_versions: dict[int, str],
        authorities_by_id: dict[
            int,
            tuple[CompiledSpecAuthority, SpecRegistry, str],
        ],
    ) -> _AuthorityAcceptanceSource:
        decision_id = WorkflowFactRepository._required_id(
            row.id,
            "authority acceptance",
        )
        WorkflowFactRepository._require_fingerprint_reference(
            row.spec_version_id,
            row.spec_hash,
            spec_versions,
            "authority acceptance specification",
        )
        authority_id = row.pending_authority_id
        if authority_id is None:
            message = (
                "Forced relationship corruption in authority acceptance: "
                f"decision {decision_id} has no compiled authority."
            )
            raise WorkflowFactRepository._error(message)
        authority_record = authorities_by_id.get(authority_id)
        if authority_record is None:
            message = (
                "Forced relationship corruption in authority acceptance: "
                f"authority {authority_id} is not owned by this Project."
            )
            raise WorkflowFactRepository._error(message)
        authority, spec, expected_fingerprint = authority_record
        if (
            authority.spec_version_id != row.spec_version_id
            or spec.spec_version_id != row.spec_version_id
            or authority.compiler_version != row.compiler_version
            or authority.prompt_hash != row.prompt_hash
        ):
            message = (
                "Forced relationship corruption in authority acceptance: "
                f"decision {decision_id} does not match authority {authority_id}."
            )
            raise WorkflowFactRepository._error(message)
        if row.authority_fingerprint != expected_fingerprint:
            message = (
                "Forced relationship corruption in authority acceptance: "
                f"decision {decision_id} has the wrong authority fingerprint."
            )
            raise WorkflowFactRepository._error(message)
        WorkflowFactRepository._authority_status(row.status)
        return _AuthorityAcceptanceSource(
            decision_id=decision_id,
            authority_id=authority_id,
            authority_fingerprint=expected_fingerprint,
            status=row.status,
            decided_at=row.decided_at,
        )

    @staticmethod
    def _latest_acceptance(
        authority_id: int,
        acceptances: Iterable[_AuthorityAcceptanceSource],
    ) -> _AuthorityAcceptanceSource | None:
        matching = (item for item in acceptances if item.authority_id == authority_id)
        return next(reversed(tuple(matching)), None)

    @staticmethod
    def _authority_state(
        spec_status: str,
        acceptance: _AuthorityAcceptanceSource | None,
    ) -> tuple[_AuthorityStatus, datetime | None]:
        if spec_status == "superseded":
            return "stale", None
        if acceptance is None:
            return "pending_review", None
        return (
            WorkflowFactRepository._authority_status(acceptance.status),
            acceptance.decided_at,
        )

    @staticmethod
    def _review_outcome(value: str) -> _ReviewOutcome:
        if value == "accepted":
            return "accepted"
        if value == "rejected":
            return "rejected"
        if value == "feedback":
            return "feedback"
        message = f"Invalid review decision {value!r}."
        raise WorkflowFactRepository._error(message)

    @staticmethod
    def _project_origin(value: str) -> _ProjectOrigin:
        if value == "greenfield":
            return "greenfield"
        if value == "brownfield":
            return "brownfield"
        message = f"Project has invalid origin {value!r}."
        raise WorkflowFactRepository._error(message)

    @staticmethod
    def _discovery_purpose(value: str) -> _DiscoveryPurpose:
        if value == "initial":
            return "initial"
        if value == "extension":
            return "extension"
        message = f"Discovery run has invalid purpose {value!r}."
        raise WorkflowFactRepository._error(message)

    @staticmethod
    def _spec_draft_kind(value: str) -> _SpecDraftKind:
        if value == "initial":
            return "initial"
        if value == "amendment":
            return "amendment"
        message = f"Specification draft has invalid kind {value!r}."
        raise WorkflowFactRepository._error(message)

    @staticmethod
    def _authority_status(value: str) -> Literal["accepted", "rejected"]:
        if value == "accepted":
            return "accepted"
        if value == "rejected":
            return "rejected"
        message = f"Authority acceptance has invalid status {value!r}."
        raise WorkflowFactRepository._error(message)

    @staticmethod
    def _attempt_outcome(value: str) -> _AttemptOutcome:
        if value == "success":
            return "success"
        if value == "failure":
            return "failure"
        if value == "obsolete":
            return "obsolete"
        message = f"Workflow node attempt outcome has invalid status {value!r}."
        raise WorkflowFactRepository._error(message)

    @staticmethod
    def _outcome_for_attempt(
        attempt: WorkflowNodeAttempt,
        outcomes_by_attempt: dict[int, WorkflowNodeAttemptOutcome],
    ) -> _AttemptOutcome | None:
        attempt_id = WorkflowFactRepository._required_id(
            attempt.workflow_node_attempt_id,
            "workflow node attempt",
        )
        outcome = outcomes_by_attempt.get(attempt_id)
        return (
            WorkflowFactRepository._attempt_outcome(outcome.status)
            if outcome is not None
            else None
        )

    @staticmethod
    def _error(message: str) -> WorkflowFactLoadError:
        return WorkflowFactLoadError(message)

    @staticmethod
    def _story_readiness_blockers(
        stories: Iterable[UserStory],
        dependencies: Iterable[UserStoryDependency],
        stories_by_id: dict[int, UserStory],
    ) -> dict[int, tuple[str, ...]]:
        blockers: dict[int, list[str]] = {story_id: [] for story_id in stories_by_id}
        for story in stories:
            story_id = WorkflowFactRepository._required_id(story.story_id, "story")
            if story.is_superseded:
                blockers[story_id].append("STORY_SUPERSEDED")
            if story.accepted_spec_version_id is None:
                blockers[story_id].append("SPECIFICATION_NOT_ACCEPTED")
        for dependency in dependencies:
            if dependency.status != "active":
                continue
            prerequisite = stories_by_id[dependency.prerequisite_story_id]
            if prerequisite.status not in _DONE_STORY_STATUSES:
                blockers[dependency.dependent_story_id].append(
                    f"PREREQUISITE_STORY_{dependency.prerequisite_story_id}_INCOMPLETE"
                )
        return {
            story_id: tuple(values) for story_id, values in sorted(blockers.items())
        }
