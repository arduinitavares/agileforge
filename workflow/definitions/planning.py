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
from workflow.fingerprints import canonical_hash
from workflow.graph import (
    ChildGraphSpec,
    NodeSpec,
    RuleCategory,
    RuleEvaluation,
    WorkflowGraph,
)

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.facts import (
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


def candidate_set_fingerprint(
    stories: tuple[StoryFact, ...],
    dependencies: tuple[StoryDependencyFact, ...],
) -> str:
    """Hash canonical current Story, dependency, and readiness facts."""
    return canonical_hash(
        {
            "stories": [
                item.model_dump(mode="json")
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


def _accepted_backlog(
    snapshot: WorkflowFactSnapshot,
) -> tuple[PhaseArtifactFact | None, bool]:
    accepted = tuple(
        item
        for item in snapshot.phase_artifacts
        if item.artifact_type == "backlog" and item.status == "accepted"
    )
    current = tuple(
        item
        for item in accepted
        if item.artifact_id
        not in {
            candidate.supersedes_artifact_id
            for candidate in snapshot.phase_artifacts
            if candidate.artifact_type == "backlog"
            and candidate.supersedes_artifact_id is not None
        }
    )
    if len(current) > 1:
        return None, True
    return (current[0], False) if current else (None, False)


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
            or artifact.status != decision.decision
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


def _roadmap_generate_rule(  # noqa: PLR0911
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    if snapshot.project_abandonments:
        return (RuleEvaluation(RuleCategory.SATISFIED, "PROJECT_ABANDONED"),)
    backlog, conflict = _accepted_backlog(snapshot)
    if conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if backlog is None:
        return _blocked(
            "ACCEPTED_BACKLOG_REQUIRED",
            "Roadmap generation requires the accepted current Backlog.",
        )
    state = _artifact_state(snapshot, "roadmap")
    if state.conflict:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    backlog_reference = FactReference(
        fact_type="backlog",
        fact_id=str(backlog.artifact_id),
        fingerprint=backlog.artifact_fingerprint,
    )
    latest = state.latest
    if latest is not None:
        if latest.source_fingerprint != backlog.artifact_fingerprint:
            return (RuleEvaluation(RuleCategory.INVALID, "ROADMAP_ARTIFACT_STALE"),)
        if latest.status == "pending_review":
            return (RuleEvaluation(RuleCategory.SATISFIED, "ROADMAP_REVIEW_PENDING"),)
        if latest.status in {"rejected", "feedback"}:
            return (
                RuleEvaluation(
                    RuleCategory.AVAILABLE,
                    "ROADMAP_REVISION_REQUIRED",
                    fact_references=(backlog_reference, _artifact_reference(latest)),
                    recommendation_kind=RecommendationKind.RECOVERY,
                ),
            )
        if latest.status == "accepted":
            return (
                RuleEvaluation(
                    RuleCategory.AVAILABLE,
                    "ROADMAP_CORRECTION_AVAILABLE",
                    fact_references=(backlog_reference, _artifact_reference(latest)),
                    recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
                ),
            )
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "ROADMAP_GENERATION_REQUIRED",
            fact_references=(backlog_reference,),
        ),
    )


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
    return (
        RuleEvaluation(
            RuleCategory.WAITING,
            "ROADMAP_REVIEW_REQUIRED",
            fact_references=(_artifact_reference(state.latest),),
        ),
    )


def _accepted_current_roadmap(
    snapshot: WorkflowFactSnapshot,
) -> tuple[PlanningArtifactFact | None, bool]:
    backlog, backlog_conflict = _accepted_backlog(snapshot)
    state = _artifact_state(snapshot, "roadmap")
    if backlog_conflict or state.conflict:
        return None, True
    roadmap = state.latest
    if (
        backlog is None
        or roadmap is None
        or roadmap.status != "accepted"
        or roadmap.source_fingerprint != backlog.artifact_fingerprint
    ):
        return None, False
    return roadmap, False


def _current_requirements(
    snapshot: WorkflowFactSnapshot,
) -> tuple[BacklogRequirementFact, ...]:
    backlog, conflict = _accepted_backlog(snapshot)
    if conflict or backlog is None:
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
            if latest.source_fingerprint != roadmap.artifact_fingerprint:
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
        if state.conflict:
            evaluations.append(
                RuleEvaluation(
                    RuleCategory.INVALID,
                    "WORKFLOW_FACT_CONFLICT",
                    instance_key=f"requirement:{requirement_id}",
                )
            )
        elif state.latest is not None and state.latest.status == "pending_review":
            evaluations.append(
                RuleEvaluation(
                    RuleCategory.WAITING,
                    "STORY_REVIEW_REQUIRED",
                    instance_key=f"requirement:{requirement_id}",
                    fact_references=(_artifact_reference(state.latest),),
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
    source = story_dependency_source_fingerprint(stories)
    matching = tuple(
        item
        for item in snapshot.story_dependency_reviews
        if item.source_fingerprint == source
        and item.selected_story_ids == tuple(item.story_id for item in stories)
    )
    if len(matching) > 1:
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    if matching:
        return (RuleEvaluation(RuleCategory.SATISFIED, "STORY_DEPENDENCIES_REVIEWED"),)
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "STORY_DEPENDENCY_REVIEW_REQUIRED",
            fact_references=(
                FactReference(
                    fact_type="story_dependency_source",
                    fact_id=str(snapshot.project.project_id),
                    fingerprint=source,
                ),
            ),
        ),
    )


def _story_readiness_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    if snapshot.project_abandonments:
        return (RuleEvaluation(RuleCategory.SATISFIED, "PROJECT_ABANDONED"),)
    stories = _candidate_stories(snapshot)
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
                FactReference(
                    fact_type="story_readiness",
                    fact_id=str(snapshot.project.project_id),
                    fingerprint=readiness_fingerprint(snapshot.stories),
                ),
            ),
        ),
    )


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
    return stories


def _sprint_plan_rule(  # noqa: PLR0911
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
        if latest.status == "pending_review":
            return (
                RuleEvaluation(RuleCategory.SATISFIED, "SPRINT_PLAN_REVIEW_PENDING"),
            )
        if latest.status in {"rejected", "feedback"}:
            return (
                RuleEvaluation(
                    RuleCategory.AVAILABLE,
                    "SPRINT_PLAN_REVISION_REQUIRED",
                    fact_references=(_artifact_reference(latest),),
                    recommendation_kind=RecommendationKind.RECOVERY,
                ),
            )
        if latest.status == "accepted":
            return (
                RuleEvaluation(
                    RuleCategory.AVAILABLE,
                    "SPRINT_PLAN_CORRECTION_AVAILABLE",
                    fact_references=(_artifact_reference(latest),),
                    recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
                ),
            )
    fingerprint = candidate_set_fingerprint(joined, snapshot.story_dependencies)
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "SPRINT_PLANNING_REQUIRED",
            fact_references=(
                FactReference(
                    fact_type="candidate_set",
                    fact_id=str(snapshot.project.project_id),
                    fingerprint=fingerprint,
                ),
            ),
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
    return (
        RuleEvaluation(
            RuleCategory.WAITING,
            "SPRINT_PLAN_REVIEW_REQUIRED",
            fact_references=(_artifact_reference(state.latest),),
        ),
    )


def _sprint_start_rule(  # noqa: PLR0911
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
    stories = _candidate_stories(snapshot)
    candidate_ids = {item.story_id for item in stories}
    if not plan.story_ids or any(item not in candidate_ids for item in plan.story_ids):
        return (
            RuleEvaluation(
                RuleCategory.INVALID,
                "SPRINT_PLAN_SELECTED_STORY_INVALID",
            ),
        )
    current = candidate_set_fingerprint(stories, snapshot.story_dependencies)
    if plan.candidate_set_fingerprint != current:
        return (RuleEvaluation(RuleCategory.INVALID, "SPRINT_PLAN_STALE"),)
    return (
        RuleEvaluation(
            RuleCategory.AVAILABLE,
            "SPRINT_READY_TO_START",
            fact_references=(
                _artifact_reference(plan),
                FactReference(
                    fact_type="candidate_set",
                    fact_id=str(snapshot.project.project_id),
                    fingerprint=current,
                ),
            ),
        ),
    )


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
