"""Pure Roadmap, Story, dependency, readiness, and Sprint planning rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from workflow.contracts import (
    GRAPH_VERSION,
    Blocker,
    FactReference,
    InputField,
    RecommendationKind,
)
from workflow.definitions.authority import accepted_current_authority
from workflow.definitions.vision import (
    accepted_current_artifact,
    artifact_reference,
    authority_reference,
    phase_artifact_state,
)
from workflow.fingerprints import canonical_hash
from workflow.graph import (
    ChildGraphSpec,
    NodeSpec,
    RuleCategory,
    RuleEvaluation,
    WorkflowGraph,
)
from workflow.planning_integrity import (
    active_dependency_review_edges,
    current_task_content_fingerprint,
    dependency_review_fingerprint,
)

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.facts import (
        AuthorityFact,
        BacklogRequirementFact,
        PhaseArtifactFact,
        PlanningArtifactFact,
        StoryDependencyFact,
        StoryFact,
        WorkflowFactSnapshot,
    )


@dataclass(frozen=True)
class _ArtifactState:
    latest: PlanningArtifactFact | None
    conflict: bool


@dataclass(frozen=True)
class _BacklogLineage:
    authority: AuthorityFact | None
    backlog: PhaseArtifactFact | None
    conflict: bool


def candidate_set_fingerprint(
    stories: tuple[StoryFact, ...],
    dependencies: tuple[StoryDependencyFact, ...],
) -> str:
    """Hash canonical current Story, dependency, and readiness facts."""
    return canonical_hash(
        {
            "stories": [
                item.model_dump(mode="json", exclude={"sprint_ids"})
                for item in sorted(stories, key=lambda story: story.story_id)
            ],
            "dependencies": [
                item.model_dump(mode="json")
                for item in sorted(
                    dependencies,
                    key=lambda edge: (
                        edge.dependent_story_id,
                        edge.prerequisite_story_id,
                        edge.dependency_id,
                    ),
                )
            ],
        }
    )


def story_dependency_source_fingerprint(stories: tuple[StoryFact, ...]) -> str:
    """Hash exact Story content selected for semantic dependency review."""
    return canonical_hash(
        [
            {
                "story_id": item.story_id,
                "requirement_id": item.requirement_id,
                "content_fingerprint": item.content_fingerprint,
                "content_accepted": item.content_accepted,
                "story_artifact_id": item.story_artifact_id,
                "authority_id": item.authority_id,
                "authority_fingerprint": item.authority_fingerprint,
                "backlog_artifact_id": item.backlog_artifact_id,
                "backlog_artifact_fingerprint": item.backlog_artifact_fingerprint,
                "roadmap_artifact_id": item.roadmap_artifact_id,
                "roadmap_artifact_fingerprint": item.roadmap_artifact_fingerprint,
            }
            for item in sorted(stories, key=lambda story: story.story_id)
            if item.sprint_candidate
        ]
    )


def readiness_fingerprint(stories: tuple[StoryFact, ...]) -> str:
    """Hash exact mutable Story planning metadata before repair."""
    return canonical_hash(
        [
            {
                "story_id": item.story_id,
                "story_points": item.story_points,
                "rank": item.rank,
                "sprint_candidate": item.sprint_candidate,
                "readiness_blockers": item.readiness_blockers,
            }
            for item in sorted(stories, key=lambda story: story.story_id)
        ]
    )


def _blocked(reason: str, message: str) -> tuple[RuleEvaluation, ...]:
    return (
        RuleEvaluation(
            RuleCategory.BLOCKED,
            reason,
            blockers=(Blocker(code=reason, message=message),),
        ),
    )


def _accepted_backlog(snapshot: WorkflowFactSnapshot) -> _BacklogLineage:
    authority, authority_conflict = accepted_current_authority(snapshot)
    if authority_conflict:
        return _BacklogLineage(None, None, True)
    if authority is None:
        return _BacklogLineage(None, None, False)
    state = phase_artifact_state(
        snapshot,
        artifact_type="backlog",
        authority=authority,
    )
    if state.conflict:
        return _BacklogLineage(authority, None, True)
    return _BacklogLineage(
        authority,
        accepted_current_artifact(state, authority),
        False,
    )


def _artifact_matches_lineage(
    artifact: PlanningArtifactFact,
    lineage: _BacklogLineage,
    *,
    roadmap: PlanningArtifactFact | None = None,
) -> bool:
    authority = lineage.authority
    backlog = lineage.backlog
    if authority is None or backlog is None or not isinstance(backlog.artifact_id, int):
        return False
    if (
        artifact.authority_id != authority.authority_id
        or artifact.authority_fingerprint != authority.authority_fingerprint
        or artifact.backlog_artifact_id != backlog.artifact_id
        or artifact.backlog_artifact_fingerprint != backlog.artifact_fingerprint
    ):
        return False
    if roadmap is None:
        return (
            artifact.source_artifact_id == backlog.artifact_id
            and artifact.source_fingerprint == backlog.artifact_fingerprint
        )
    return (
        artifact.source_artifact_id == roadmap.artifact_id
        and artifact.source_fingerprint == roadmap.artifact_fingerprint
        and artifact.roadmap_artifact_id == roadmap.artifact_id
        and artifact.roadmap_artifact_fingerprint == roadmap.artifact_fingerprint
    )


def _lineage_references(lineage: _BacklogLineage) -> tuple[FactReference, ...]:
    if lineage.authority is None or lineage.backlog is None:
        return ()
    return (
        artifact_reference(lineage.backlog),
        authority_reference(lineage.authority),
    )


def _artifact_state(
    snapshot: WorkflowFactSnapshot,
    artifact_type: Literal["roadmap", "story", "sprint_plan"],
    *,
    requirement_id: str | None = None,
) -> _ArtifactState:
    artifacts = tuple(
        item
        for item in snapshot.planning_artifacts
        if item.artifact_type == artifact_type
        and (requirement_id is None or item.requirement_id == requirement_id)
    )
    by_id = {item.artifact_id: item for item in artifacts}
    conflict = len(by_id) != len(artifacts)
    superseded_ids: set[int] = set()
    for item in artifacts:
        parent_id = item.supersedes_artifact_id
        if parent_id is None:
            continue
        if parent_id not in by_id or parent_id >= item.artifact_id:
            conflict = True
        superseded_ids.add(parent_id)
    current = tuple(
        item
        for item in artifacts
        if item.artifact_id not in superseded_ids and item.status != "superseded"
    )
    if len(current) > 1:
        conflict = True
    latest = current[0] if len(current) == 1 else None
    review_type = "sprint" if artifact_type == "sprint_plan" else artifact_type
    decisions = tuple(
        item
        for item in snapshot.review_decisions
        if item.artifact_type == review_type and item.artifact_id in by_id
    )
    decisions_by_artifact: dict[int, list[object]] = {}
    for decision in decisions:
        decisions_by_artifact.setdefault(decision.artifact_id, []).append(decision)
        artifact = by_id[decision.artifact_id]
        if (
            artifact.artifact_fingerprint != decision.artifact_fingerprint
            or (
                artifact.status not in {"superseded", decision.decision}
            )
        ):
            conflict = True
    if any(len(items) > 1 for items in decisions_by_artifact.values()):
        conflict = True
    orphan_decisions = tuple(
        item
        for item in snapshot.review_decisions
        if item.artifact_type == review_type
        and item.artifact_id
        not in {
            artifact.artifact_id
            for artifact in snapshot.planning_artifacts
            if artifact.artifact_type == artifact_type
        }
    )
    return _ArtifactState(latest=latest, conflict=conflict or bool(orphan_decisions))


def _artifact_reference(artifact: PlanningArtifactFact) -> FactReference:
    return FactReference(
        fact_type=artifact.artifact_type,
        fact_id=str(artifact.artifact_id),
        fingerprint=artifact.artifact_fingerprint,
    )


def _roadmap_current_evaluation(
    latest: PlanningArtifactFact | None,
    lineage: _BacklogLineage,
) -> tuple[RuleEvaluation, ...]:
    references = _lineage_references(lineage)
    if latest is None:
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "ROADMAP_GENERATION_REQUIRED",
                fact_references=references,
            ),
        )
    if not _artifact_matches_lineage(latest, lineage):
        return (RuleEvaluation(RuleCategory.INVALID, "ROADMAP_ARTIFACT_STALE"),)
    if latest.status == "pending_review":
        return (RuleEvaluation(RuleCategory.SATISFIED, "ROADMAP_REVIEW_PENDING"),)
    if latest.status in {"rejected", "feedback"}:
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "ROADMAP_REVISION_REQUIRED",
                fact_references=(*references, _artifact_reference(latest)),
                recommendation_kind=RecommendationKind.RECOVERY,
            ),
        )
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "ROADMAP_CORRECTION_AVAILABLE",
            fact_references=(*references, _artifact_reference(latest)),
            recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
        ),
    )


def _roadmap_generate_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    if snapshot.project_abandonments:
        return (RuleEvaluation(RuleCategory.SATISFIED, "PROJECT_ABANDONED"),)
    lineage = _accepted_backlog(snapshot)
    if lineage.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if lineage.authority is None:
        return _blocked(
            "ACCEPTED_CURRENT_AUTHORITY_REQUIRED",
            "Roadmap generation requires the accepted current authority.",
        )
    backlog = lineage.backlog
    if backlog is None:
        return _blocked(
            "ACCEPTED_CURRENT_BACKLOG_REQUIRED",
            "Roadmap generation requires the accepted current Backlog.",
        )
    state = _artifact_state(snapshot, "roadmap")
    if state.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    return _roadmap_current_evaluation(state.latest, lineage)


def _roadmap_review_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    if snapshot.project_abandonments:
        return (RuleEvaluation(RuleCategory.SATISFIED, "PROJECT_ABANDONED"),)
    state = _artifact_state(snapshot, "roadmap")
    if state.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if state.latest is None or state.latest.status != "pending_review":
        return (RuleEvaluation(RuleCategory.SATISFIED, "ROADMAP_REVIEW_NOT_PENDING"),)
    lineage = _accepted_backlog(snapshot)
    if lineage.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if not _artifact_matches_lineage(state.latest, lineage):
        return (RuleEvaluation(RuleCategory.INVALID, "ROADMAP_REVIEW_SOURCE_STALE"),)
    return (
        RuleEvaluation(
            RuleCategory.WAITING,
            "ROADMAP_REVIEW_REQUIRED",
            fact_references=(
                *_lineage_references(lineage),
                _artifact_reference(state.latest),
            ),
        ),
    )


def _accepted_current_roadmap(
    snapshot: WorkflowFactSnapshot,
) -> tuple[PlanningArtifactFact | None, bool]:
    lineage = _accepted_backlog(snapshot)
    state = _artifact_state(snapshot, "roadmap")
    if lineage.conflict or state.conflict:
        return None, True
    roadmap = state.latest
    if (
        lineage.backlog is None
        or roadmap is None
        or roadmap.status != "accepted"
        or not _artifact_matches_lineage(roadmap, lineage)
    ):
        return None, False
    return roadmap, False


def _current_requirements(
    snapshot: WorkflowFactSnapshot,
) -> tuple[BacklogRequirementFact, ...]:
    lineage = _accepted_backlog(snapshot)
    backlog = lineage.backlog
    if lineage.conflict or backlog is None:
        return ()
    return tuple(
        sorted(
            (
                item
                for item in snapshot.backlog_requirements
                if item.backlog_artifact_id == int(backlog.artifact_id)
                and item.backlog_artifact_fingerprint == backlog.artifact_fingerprint
            ),
            key=lambda item: item.requirement_id,
        )
    )


def _story_generate_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    if snapshot.project_abandonments:
        return (RuleEvaluation(RuleCategory.SATISFIED, "PROJECT_ABANDONED"),)
    roadmap, conflict = _accepted_current_roadmap(snapshot)
    if conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if roadmap is None:
        return _blocked(
            "ACCEPTED_ROADMAP_REQUIRED",
            "Story generation requires the accepted current Roadmap.",
        )
    lineage = _accepted_backlog(snapshot)
    requirements = _current_requirements(snapshot)
    if not requirements:
        return (RuleEvaluation(RuleCategory.INVALID, "BACKLOG_REQUIREMENTS_MISSING"),)
    evaluations: list[RuleEvaluation] = []
    for requirement in requirements:
        requirement_id = requirement.requirement_id
        instance_key = f"requirement:{requirement_id}"
        state = _artifact_state(
            snapshot,
            "story",
            requirement_id=requirement_id,
        )
        if state.conflict:
            evaluations.append(
                RuleEvaluation(
                    RuleCategory.INVALID,
                    "WORKFLOW_FACT_CONFLICT",
                    instance_key=instance_key,
                )
            )
            continue
        latest = state.latest
        if latest is not None:
            if not _artifact_matches_lineage(latest, lineage, roadmap=roadmap):
                evaluations.append(
                    RuleEvaluation(
                        RuleCategory.INVALID,
                        "STORY_ARTIFACT_STALE",
                        instance_key=instance_key,
                    )
                )
            elif latest.status in {"rejected", "feedback"}:
                evaluations.append(
                    RuleEvaluation(
                        RuleCategory.AVAILABLE,
                        "STORY_REVISION_REQUIRED",
                        instance_key=instance_key,
                        fact_references=(
                            *_lineage_references(lineage),
                            _artifact_reference(roadmap),
                            _artifact_reference(latest),
                        ),
                        recommendation_kind=RecommendationKind.RECOVERY,
                    )
                )
            continue
        evaluations.append(
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "STORY_GENERATION_REQUIRED",
                instance_key=instance_key,
                fact_references=(
                    *_lineage_references(lineage),
                    _artifact_reference(roadmap),
                    FactReference(
                        fact_type="backlog_requirement",
                        fact_id=requirement_id,
                        fingerprint=requirement.backlog_artifact_fingerprint,
                    ),
                ),
            )
        )
    return tuple(evaluations)


def _story_review_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    if snapshot.project_abandonments:
        return (RuleEvaluation(RuleCategory.SATISFIED, "PROJECT_ABANDONED"),)
    roadmap, roadmap_conflict = _accepted_current_roadmap(snapshot)
    lineage = _accepted_backlog(snapshot)
    evaluations: list[RuleEvaluation] = []
    requirement_ids = sorted(
        {
            item.requirement_id
            for item in snapshot.planning_artifacts
            if item.artifact_type == "story" and item.requirement_id is not None
        }
    )
    for requirement_id in requirement_ids:
        state = _artifact_state(snapshot, "story", requirement_id=requirement_id)
        latest = state.latest
        if state.conflict or roadmap_conflict or lineage.conflict:
            evaluations.append(
                RuleEvaluation(
                    RuleCategory.INVALID,
                    "WORKFLOW_FACT_CONFLICT",
                    instance_key=f"requirement:{requirement_id}",
                )
            )
        elif latest is not None and latest.status == "pending_review" and (
            roadmap is None
            or not _artifact_matches_lineage(latest, lineage, roadmap=roadmap)
            or requirement_id
            not in {item.requirement_id for item in _current_requirements(snapshot)}
        ):
            evaluations.append(
                RuleEvaluation(
                    RuleCategory.INVALID,
                    "STORY_REVIEW_SOURCE_STALE",
                    instance_key=f"requirement:{requirement_id}",
                )
            )
        elif latest is not None and latest.status == "pending_review":
            if roadmap is None:
                evaluations.append(
                    RuleEvaluation(
                        RuleCategory.INVALID,
                        "STORY_REVIEW_SOURCE_STALE",
                        instance_key=f"requirement:{requirement_id}",
                    )
                )
                continue
            evaluations.append(
                RuleEvaluation(
                    RuleCategory.WAITING,
                    "STORY_REVIEW_REQUIRED",
                    instance_key=f"requirement:{requirement_id}",
                    fact_references=(
                        *_lineage_references(lineage),
                        _artifact_reference(roadmap),
                        _artifact_reference(latest),
                    ),
                )
            )
    return tuple(evaluations) or (
        RuleEvaluation(RuleCategory.SATISFIED, "STORY_REVIEW_NOT_PENDING"),
    )


def _candidate_stories(snapshot: WorkflowFactSnapshot) -> tuple[StoryFact, ...]:
    return tuple(
        sorted(
            (item for item in snapshot.stories if item.sprint_candidate),
            key=lambda item: item.story_id,
        )
    )


def _story_lineage_problem(
    snapshot: WorkflowFactSnapshot,
    stories: tuple[StoryFact, ...],
) -> RuleEvaluation | None:
    roadmap, conflict = _accepted_current_roadmap(snapshot)
    lineage = _accepted_backlog(snapshot)
    if conflict or lineage.conflict:
        return RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT")
    authority = lineage.authority
    backlog = lineage.backlog
    if roadmap is None or authority is None or backlog is None:
        return RuleEvaluation(RuleCategory.INVALID, "STORY_PLANNING_LINEAGE_STALE")
    requirement_ids = {item.requirement_id for item in _current_requirements(snapshot)}
    artifacts = {
        item.artifact_id: item
        for item in snapshot.planning_artifacts
        if item.artifact_type == "story"
    }
    for story in stories:
        artifact = (
            None
            if story.story_artifact_id is None
            else artifacts.get(story.story_artifact_id)
        )
        if not story.content_accepted:
            continue
        if (
            story.requirement_id not in requirement_ids
            or story.authority_id != authority.authority_id
            or story.authority_fingerprint != authority.authority_fingerprint
            or story.backlog_artifact_id != backlog.artifact_id
            or story.backlog_artifact_fingerprint
            != backlog.artifact_fingerprint
            or story.roadmap_artifact_id != roadmap.artifact_id
            or story.roadmap_artifact_fingerprint != roadmap.artifact_fingerprint
            or artifact is None
            or artifact.status != "accepted"
            or not _artifact_matches_lineage(artifact, lineage, roadmap=roadmap)
        ):
            return RuleEvaluation(
                RuleCategory.INVALID,
                "STORY_PLANNING_LINEAGE_STALE",
            )
    return None


def _dependency_review_evaluation(
    snapshot: WorkflowFactSnapshot,
    stories: tuple[StoryFact, ...],
) -> RuleEvaluation:
    source = story_dependency_source_fingerprint(stories)
    selected_story_ids = tuple(item.story_id for item in stories)
    matching = tuple(
        item
        for item in snapshot.story_dependency_reviews
        if item.source_fingerprint == source
        and item.selected_story_ids == selected_story_ids
    )
    if len(matching) > 1:
        return RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT")
    if matching:
        review = matching[0]
        try:
            current_edges = active_dependency_review_edges(
                snapshot.story_dependencies
            )
        except ValueError:
            return RuleEvaluation(
                RuleCategory.INVALID,
                "STORY_DEPENDENCY_REVIEW_STALE",
            )
        if (
            review.reviewed_edges != current_edges
            or review.dependency_fingerprint
            != dependency_review_fingerprint(current_edges)
        ):
            return RuleEvaluation(
                RuleCategory.INVALID,
                "STORY_DEPENDENCY_REVIEW_STALE",
            )
        return RuleEvaluation(
            RuleCategory.SATISFIED,
            "STORY_DEPENDENCIES_REVIEWED",
        )
    lineage = _accepted_backlog(snapshot)
    roadmap, _conflict = _accepted_current_roadmap(snapshot)
    references = [*_lineage_references(lineage)]
    if roadmap is not None:
        references.append(_artifact_reference(roadmap))
    references.append(
        FactReference(
            fact_type="story_dependency_source",
            fact_id=str(snapshot.project.project_id),
            fingerprint=source,
        )
    )
    return RuleEvaluation(
        RuleCategory.AVAILABLE,
        "STORY_DEPENDENCY_REVIEW_REQUIRED",
        fact_references=tuple(references),
    )


def _dependency_problem(
    stories: tuple[StoryFact, ...],
    dependencies: tuple[StoryDependencyFact, ...],
) -> tuple[RuleCategory, str] | None:
    story_ids = {item.story_id for item in stories}
    relevant = tuple(
        item for item in dependencies if item.status in {"active", "proposed"}
    )
    if any(
        item.dependent_story_id not in story_ids
        or item.prerequisite_story_id not in story_ids
        or item.dependent_story_id == item.prerequisite_story_id
        for item in relevant
    ):
        return RuleCategory.INVALID, "STORY_DEPENDENCY_INVALID"
    if any(item.status == "proposed" for item in relevant):
        return RuleCategory.BLOCKED, "STORY_DEPENDENCIES_UNREVIEWED"
    edges: dict[int, set[int]] = {}
    for item in relevant:
        edges.setdefault(item.dependent_story_id, set()).add(item.prerequisite_story_id)
    active: set[int] = set()
    visited: set[int] = set()

    def visit(story_id: int) -> bool:
        if story_id in active:
            return True
        if story_id in visited:
            return False
        visited.add(story_id)
        active.add(story_id)
        found = any(visit(parent) for parent in sorted(edges.get(story_id, set())))
        active.remove(story_id)
        return found

    if any(visit(story_id) for story_id in sorted(story_ids)):
        return RuleCategory.INVALID, "STORY_DEPENDENCY_CYCLE"
    return None


def _story_dependencies_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    if snapshot.project_abandonments:
        return (RuleEvaluation(RuleCategory.SATISFIED, "PROJECT_ABANDONED"),)
    stories = _candidate_stories(snapshot)
    if not stories:
        return _blocked(
            "STORY_DEPENDENCY_CANDIDATES_MISSING",
            "Dependency review requires at least one candidate Story.",
        )
    if any(not story.content_accepted for story in stories):
        return _blocked(
            "STORY_CONTENT_NOT_ACCEPTED",
            "Dependency review requires accepted current Story content.",
        )
    lineage_problem = _story_lineage_problem(snapshot, stories)
    if lineage_problem is not None:
        return (lineage_problem,)
    return (_dependency_review_evaluation(snapshot, stories),)


def _story_readiness_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    if snapshot.project_abandonments:
        return (RuleEvaluation(RuleCategory.SATISFIED, "PROJECT_ABANDONED"),)
    stories = _candidate_stories(snapshot)
    if any(not story.content_accepted for story in stories):
        return _blocked(
            "STORY_CONTENT_NOT_ACCEPTED",
            "Readiness repair requires accepted current Story content.",
        )
    if stories:
        lineage_problem = _story_lineage_problem(snapshot, stories)
        if lineage_problem is not None:
            return (lineage_problem,)
    missing = tuple(
        item for item in stories if item.story_points is None or item.rank is None
    )
    if not missing:
        return (RuleEvaluation(RuleCategory.SATISFIED, "STORY_READINESS_COMPLETE"),)
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "STORY_READINESS_REPAIR_REQUIRED",
            fact_references=(
                *_lineage_references(_accepted_backlog(snapshot)),
                *(
                    (_artifact_reference(roadmap),)
                    if (roadmap := _accepted_current_roadmap(snapshot)[0]) is not None
                    else ()
                ),
                FactReference(
                    fact_type="story_readiness",
                    fact_id=str(snapshot.project.project_id),
                    fingerprint=readiness_fingerprint(snapshot.stories),
                ),
            ),
        ),
    )


def _sprint_candidate_problem(
    snapshot: WorkflowFactSnapshot,
    stories: tuple[StoryFact, ...],
) -> RuleEvaluation | None:
    if any(not item.content_accepted for item in stories):
        return RuleEvaluation(
            RuleCategory.BLOCKED,
            "STORY_CONTENT_NOT_ACCEPTED",
            blockers=(
                Blocker(
                    code="STORY_CONTENT_NOT_ACCEPTED",
                    message="Every candidate Story requires accepted content.",
                ),
            ),
        )
    lineage_problem = _story_lineage_problem(snapshot, stories)
    if lineage_problem is not None:
        return lineage_problem
    dependency_problem = _dependency_problem(stories, snapshot.story_dependencies)
    if dependency_problem is not None:
        category, reason = dependency_problem
        return RuleEvaluation(
            category,
            reason,
            blockers=(Blocker(code=reason, message="Story dependencies are invalid."),),
        )
    blockers: list[Blocker] = []
    if any(item.story_points is None for item in stories):
        blockers.append(
            Blocker(code="STORY_POINTS_MISSING", message="Story points are required.")
        )
    if any(item.rank is None for item in stories):
        blockers.append(
            Blocker(code="STORY_RANK_MISSING", message="Story rank is required.")
        )
    if blockers:
        return RuleEvaluation(
            RuleCategory.BLOCKED,
            "STORY_READINESS_INCOMPLETE",
            blockers=tuple(blockers),
        )
    return None


def _sprint_join(
    snapshot: WorkflowFactSnapshot,
) -> tuple[StoryFact, ...] | RuleEvaluation:
    stories = _candidate_stories(snapshot)
    if not stories:
        return RuleEvaluation(
            RuleCategory.BLOCKED,
            "SPRINT_CANDIDATES_MISSING",
            blockers=(
                Blocker(
                    code="SPRINT_CANDIDATES_MISSING",
                    message="Sprint planning requires at least one candidate Story.",
                ),
            ),
        )
    candidate_problem = _sprint_candidate_problem(snapshot, stories)
    if candidate_problem is not None:
        return candidate_problem
    dependency_review = _dependency_review_evaluation(snapshot, stories)
    if dependency_review.category is RuleCategory.INVALID:
        return dependency_review
    if dependency_review.category is not RuleCategory.SATISFIED:
        return RuleEvaluation(
            RuleCategory.BLOCKED,
            "STORY_DEPENDENCIES_UNREVIEWED",
            blockers=(
                Blocker(
                    code="STORY_DEPENDENCIES_UNREVIEWED",
                    message="Current Story dependency semantics require review.",
                ),
            ),
        )
    return stories


def _sprint_plan_freshness_reason(
    snapshot: WorkflowFactSnapshot,
    plan: PlanningArtifactFact,
    stories: tuple[StoryFact, ...],
    *,
    review: bool,
) -> str | None:
    candidate_ids = {item.story_id for item in stories}
    if not plan.story_ids or any(item not in candidate_ids for item in plan.story_ids):
        return (
            "SPRINT_PLAN_REVIEW_SOURCE_STALE"
            if review
            else "SPRINT_PLAN_SELECTED_STORY_INVALID"
        )
    current_candidates = candidate_set_fingerprint(
        stories,
        snapshot.story_dependencies,
    )
    if plan.candidate_set_fingerprint != current_candidates:
        return "SPRINT_PLAN_REVIEW_SOURCE_STALE" if review else "SPRINT_PLAN_STALE"
    if (
        plan.sprint_id is None
        or plan.task_content_fingerprint is None
        or plan.task_content_fingerprint
        != current_task_content_fingerprint(
            snapshot.tasks,
            sprint_id=plan.sprint_id,
            story_ids=plan.story_ids,
        )
    ):
        return (
            "SPRINT_PLAN_REVIEW_TASK_CONTENT_STALE"
            if review
            else "SPRINT_PLAN_TASK_CONTENT_STALE"
        )
    return None


def _sprint_plan_references(
    snapshot: WorkflowFactSnapshot,
    stories: tuple[StoryFact, ...],
    *,
    plan: PlanningArtifactFact | None = None,
) -> tuple[FactReference, ...]:
    lineage = _accepted_backlog(snapshot)
    roadmap, _conflict = _accepted_current_roadmap(snapshot)
    return (
        *((_artifact_reference(plan),) if plan is not None else ()),
        *_lineage_references(lineage),
        *((_artifact_reference(roadmap),) if roadmap is not None else ()),
        FactReference(
            fact_type="candidate_set",
            fact_id=str(snapshot.project.project_id),
            fingerprint=candidate_set_fingerprint(
                stories,
                snapshot.story_dependencies,
            ),
        ),
    )


def _existing_sprint_plan_evaluation(
    snapshot: WorkflowFactSnapshot,
    latest: PlanningArtifactFact,
    stories: tuple[StoryFact, ...],
) -> tuple[RuleEvaluation, ...]:
    stale_reason = _sprint_plan_freshness_reason(
        snapshot,
        latest,
        stories,
        review=False,
    )
    if stale_reason is not None:
        return (RuleEvaluation(RuleCategory.INVALID, stale_reason),)
    if latest.status == "pending_review":
        return (RuleEvaluation(RuleCategory.SATISFIED, "SPRINT_PLAN_REVIEW_PENDING"),)
    references = _sprint_plan_references(snapshot, stories, plan=latest)
    if latest.status in {"rejected", "feedback"}:
        return (
            RuleEvaluation(
                RuleCategory.AVAILABLE,
                "SPRINT_PLAN_REVISION_REQUIRED",
                fact_references=references,
                recommendation_kind=RecommendationKind.RECOVERY,
            ),
        )
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "SPRINT_PLAN_CORRECTION_AVAILABLE",
            fact_references=references,
            recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
        ),
    )


def _sprint_plan_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    if snapshot.project_abandonments:
        return (RuleEvaluation(RuleCategory.SATISFIED, "PROJECT_ABANDONED"),)
    joined = _sprint_join(snapshot)
    if isinstance(joined, RuleEvaluation):
        return (joined,)
    state = _artifact_state(snapshot, "sprint_plan")
    if state.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    latest = state.latest
    if latest is not None:
        return _existing_sprint_plan_evaluation(snapshot, latest, joined)
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "SPRINT_PLANNING_REQUIRED",
            fact_references=_sprint_plan_references(snapshot, joined),
        ),
    )


def _sprint_review_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    if snapshot.project_abandonments:
        return (RuleEvaluation(RuleCategory.SATISFIED, "PROJECT_ABANDONED"),)
    state = _artifact_state(snapshot, "sprint_plan")
    if state.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if state.latest is None or state.latest.status != "pending_review":
        return (RuleEvaluation(RuleCategory.SATISFIED, "SPRINT_REVIEW_NOT_PENDING"),)
    joined = _sprint_join(snapshot)
    if isinstance(joined, RuleEvaluation):
        return (
            RuleEvaluation(
                RuleCategory.INVALID,
                "SPRINT_PLAN_REVIEW_SOURCE_STALE",
            ),
        )
    return _sprint_review_evaluation(snapshot, state.latest, joined)


def _sprint_review_evaluation(
    snapshot: WorkflowFactSnapshot,
    plan: PlanningArtifactFact,
    stories: tuple[StoryFact, ...],
) -> tuple[RuleEvaluation, ...]:
    stale_reason = _sprint_plan_freshness_reason(
        snapshot,
        plan,
        stories,
        review=True,
    )
    if stale_reason is not None:
        return (RuleEvaluation(RuleCategory.INVALID, stale_reason),)
    sprint_id = plan.sprint_id
    if sprint_id is None:
        return (
            RuleEvaluation(
                RuleCategory.INVALID,
                "SPRINT_PLAN_REVIEW_TASK_CONTENT_STALE",
            ),
        )
    current_tasks = current_task_content_fingerprint(
        snapshot.tasks,
        sprint_id=sprint_id,
        story_ids=plan.story_ids,
    )
    return (
        RuleEvaluation(
            RuleCategory.WAITING,
            "SPRINT_PLAN_REVIEW_REQUIRED",
            fact_references=(
                *_sprint_plan_references(snapshot, stories, plan=plan),
                FactReference(
                    fact_type="sprint_plan_tasks",
                    fact_id=str(sprint_id),
                    fingerprint=current_tasks,
                ),
            ),
        ),
    )


def _sprint_start_evaluation(
    snapshot: WorkflowFactSnapshot,
    plan: PlanningArtifactFact,
    stories: tuple[StoryFact, ...],
) -> tuple[RuleEvaluation, ...]:
    stale_reason = _sprint_plan_freshness_reason(
        snapshot,
        plan,
        stories,
        review=False,
    )
    if stale_reason is not None:
        return (RuleEvaluation(RuleCategory.INVALID, stale_reason),)
    sprint_id = plan.sprint_id
    if sprint_id is None:
        return (
            RuleEvaluation(
                RuleCategory.INVALID,
                "SPRINT_PLAN_TASK_CONTENT_STALE",
            ),
        )
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "SPRINT_READY_TO_START",
            fact_references=(
                *_sprint_plan_references(snapshot, stories, plan=plan),
                FactReference(
                    fact_type="sprint_plan_tasks",
                    fact_id=str(sprint_id),
                    fingerprint=current_task_content_fingerprint(
                        snapshot.tasks,
                        sprint_id=sprint_id,
                        story_ids=plan.story_ids,
                    ),
                ),
            ),
        ),
    )


def _sprint_start_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    if snapshot.project_abandonments:
        return (RuleEvaluation(RuleCategory.SATISFIED, "PROJECT_ABANDONED"),)
    if any(item.status == "active" for item in snapshot.sprints):
        return (RuleEvaluation(RuleCategory.SATISFIED, "SPRINT_ALREADY_ACTIVE"),)
    state = _artifact_state(snapshot, "sprint_plan")
    if state.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    plan = state.latest
    if plan is None or plan.status != "accepted":
        return _blocked(
            "ACCEPTED_SPRINT_PLAN_REQUIRED",
            "Sprint start requires an accepted exact Sprint plan.",
        )
    joined = _sprint_join(snapshot)
    if isinstance(joined, RuleEvaluation):
        return (joined,)
    return _sprint_start_evaluation(snapshot, plan, joined)


PLANNING_NODES: tuple[NodeSpec, ...] = (
    NodeSpec(
        node_id="planning.roadmap.generate",
        child_graph_id="planning",
        request_kind="record_roadmap_draft",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="backlog_artifact_id", value_type="integer"),
            InputField(name="backlog_artifact_fingerprint", value_type="string"),
            InputField(name="canonical_content", value_type="object"),
            InputField(name="content_fingerprint", value_type="string"),
        ),
        evaluate_rule=_roadmap_generate_rule,
    ),
    NodeSpec(
        node_id="planning.roadmap.review",
        child_graph_id="planning",
        request_kind="decide_roadmap",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="roadmap_artifact_id", value_type="integer"),
            InputField(name="artifact_fingerprint", value_type="string"),
            InputField(name="decision", value_type="string"),
            InputField(name="rationale", value_type="string"),
        ),
        evaluate_rule=_roadmap_review_rule,
    ),
    NodeSpec(
        node_id="planning.story.generate",
        child_graph_id="planning",
        request_kind="record_story_draft",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="requirement_id", value_type="string"),
            InputField(name="roadmap_artifact_id", value_type="integer"),
            InputField(name="canonical_content", value_type="object"),
            InputField(name="content_fingerprint", value_type="string"),
        ),
        evaluate_rule=_story_generate_rule,
    ),
    NodeSpec(
        node_id="planning.story.review",
        child_graph_id="planning",
        request_kind="decide_story",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="story_artifact_id", value_type="integer"),
            InputField(name="artifact_fingerprint", value_type="string"),
            InputField(name="decision", value_type="string"),
            InputField(name="rationale", value_type="string"),
        ),
        evaluate_rule=_story_review_rule,
    ),
    NodeSpec(
        node_id="planning.story_dependencies",
        child_graph_id="planning",
        request_kind="apply_story_dependencies",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="selected_story_ids", value_type="array"),
            InputField(name="reviewed_edges", value_type="array"),
            InputField(name="source_fingerprint", value_type="string"),
        ),
        evaluate_rule=_story_dependencies_rule,
    ),
    NodeSpec(
        node_id="planning.story_readiness",
        child_graph_id="planning",
        request_kind="repair_story_readiness",
        recommendation_kind=RecommendationKind.RECOVERY,
        required_inputs=(
            InputField(name="story_ids", value_type="array"),
            InputField(name="repairs", value_type="array"),
            InputField(
                name="expected_readiness_fingerprint",
                value_type="string",
            ),
        ),
        evaluate_rule=_story_readiness_rule,
    ),
    NodeSpec(
        node_id="planning.sprint.plan",
        child_graph_id="planning",
        request_kind="record_sprint_plan",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="team_name", value_type="string"),
            InputField(name="selected_story_ids", value_type="array"),
            InputField(name="canonical_task_plan", value_type="object"),
            InputField(name="candidate_set_fingerprint", value_type="string"),
        ),
        evaluate_rule=_sprint_plan_rule,
    ),
    NodeSpec(
        node_id="planning.sprint.review",
        child_graph_id="planning",
        request_kind="decide_sprint_plan",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="sprint_plan_artifact_id", value_type="integer"),
            InputField(name="plan_fingerprint", value_type="string"),
            InputField(name="decision", value_type="string"),
            InputField(name="rationale", value_type="string"),
        ),
        evaluate_rule=_sprint_review_rule,
    ),
    NodeSpec(
        node_id="planning.sprint.start",
        child_graph_id="planning",
        request_kind="start_sprint",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="sprint_plan_artifact_id", value_type="integer"),
            InputField(name="sprint_id", value_type="integer"),
            InputField(name="plan_fingerprint", value_type="string"),
            InputField(name="candidate_set_fingerprint", value_type="string"),
        ),
        evaluate_rule=_sprint_start_rule,
    ),
)


def planning_graph() -> WorkflowGraph:
    """Return the standalone planning graph used by focused tests."""
    return WorkflowGraph(
        graph_version=GRAPH_VERSION,
        root=ChildGraphSpec(child_graph_id="planning", nodes=PLANNING_NODES),
    )


__all__ = [
    "PLANNING_NODES",
    "candidate_set_fingerprint",
    "planning_graph",
    "readiness_fingerprint",
    "story_dependency_source_fingerprint",
]
