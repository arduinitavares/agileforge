"""Pure registered-source selectors for Specification structuring and review."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from workflow.contracts import FactReference, InputField, RecommendationKind
from workflow.definitions.product_goal import (
    accepted_current_goal,
    accepted_current_vision,
    lifecycle_is_quiescent,
)
from workflow.graph import AgenticExecutionSpec, NodeSpec, RuleCategory, RuleEvaluation

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.facts import (
        ProductGoalArtifactFact,
        SpecificationCandidateFact,
        SpecificationSourceFact,
        SpecVersionFact,
        VisionArtifactFact,
        WorkflowFactSnapshot,
    )


def _reference(kind: str, identifier: int, fingerprint: str) -> FactReference:
    return FactReference(
        fact_type=kind,
        fact_id=str(identifier),
        fingerprint=fingerprint,
    )


def _lineage_references(
    vision: VisionArtifactFact,
    goal: ProductGoalArtifactFact,
) -> tuple[FactReference, FactReference]:
    return (
        _reference("vision", vision.vision_artifact_id, vision.content_fingerprint),
        _reference(
            "product_goal", goal.product_goal_artifact_id, goal.content_fingerprint
        ),
    )


def _current_specification_source_state(
    snapshot: WorkflowFactSnapshot,
) -> tuple[SpecificationSourceFact | None, bool]:
    """Select one unsuperseded source for the current product/repository facts."""
    vision = accepted_current_vision(snapshot)
    goal = accepted_current_goal(snapshot)
    binding_id = snapshot.project.active_repository_binding_id
    if vision is None or goal is None or binding_id is None:
        return None, False
    superseded = {
        source.supersedes_specification_source_id
        for source in snapshot.specification_sources
        if source.supersedes_specification_source_id is not None
    }
    choices = [
        source
        for source in snapshot.specification_sources
        if source.specification_source_id not in superseded
        and (
            source.repository_binding_id,
            source.vision_artifact_id,
            source.vision_fingerprint,
            source.product_goal_artifact_id,
            source.product_goal_fingerprint,
        )
        == (
            binding_id,
            vision.vision_artifact_id,
            vision.content_fingerprint,
            goal.product_goal_artifact_id,
            goal.content_fingerprint,
        )
    ]
    malformed = any(
        source.specification_source_id not in superseded
        and source.product_goal_artifact_id == goal.product_goal_artifact_id
        and source.repository_binding_id == binding_id
        and (
            source.vision_artifact_id,
            source.vision_fingerprint,
            source.product_goal_fingerprint,
        )
        != (
            vision.vision_artifact_id,
            vision.content_fingerprint,
            goal.content_fingerprint,
        )
        for source in snapshot.specification_sources
    )
    return (choices[0] if len(choices) == 1 else None), (malformed or len(choices) > 1)


def current_specification_source(
    snapshot: WorkflowFactSnapshot,
) -> SpecificationSourceFact | None:
    """Return the sole source registered for current durable facts."""
    source, _conflict = _current_specification_source_state(snapshot)
    return source


def _source_by_id(
    snapshot: WorkflowFactSnapshot,
) -> dict[int, SpecificationSourceFact]:
    return {
        source.specification_source_id: source
        for source in snapshot.specification_sources
    }


def _revision_candidate_on_ancestor(
    snapshot: WorkflowFactSnapshot,
    source: SpecificationSourceFact,
    *,
    source_lineage: tuple[int, str, int, str],
) -> tuple[SpecificationCandidateFact | None, bool, bool]:
    """Resolve one ancestor candidate as (candidate, conflict, chain complete)."""
    malformed = any(
        candidate.specification_source_id == source.specification_source_id
        and candidate.specification_source_fingerprint != source.source_fingerprint
        for candidate in snapshot.specification_candidates
    )
    choices = [
        candidate
        for candidate in snapshot.specification_candidates
        if (
            candidate.specification_source_id,
            candidate.specification_source_fingerprint,
        )
        == (source.specification_source_id, source.source_fingerprint)
    ]
    if malformed or len(choices) > 1:
        return None, True, True
    if not choices:
        return None, False, False
    candidate = choices[0]
    if (
        candidate.vision_artifact_id,
        candidate.vision_fingerprint,
        candidate.product_goal_artifact_id,
        candidate.product_goal_fingerprint,
    ) != source_lineage:
        return None, True, True
    decision, conflict = _candidate_decision(snapshot, candidate)
    if conflict or decision is None:
        return None, True, True
    return (
        candidate if decision in {"rejected", "feedback"} else None,
        False,
        True,
    )


def _revision_candidate_for_source(
    snapshot: WorkflowFactSnapshot,
    source: SpecificationSourceFact,
) -> tuple[SpecificationCandidateFact | None, bool]:
    """Find the nearest exact rejected candidate through candidate-less sources."""
    sources = _source_by_id(snapshot)
    source_lineage = (
        source.vision_artifact_id,
        source.vision_fingerprint,
        source.product_goal_artifact_id,
        source.product_goal_fingerprint,
    )
    parent_id = source.supersedes_specification_source_id
    parent_fingerprint = source.supersedes_source_fingerprint
    visited = {source.specification_source_id}
    prior: SpecificationCandidateFact | None = None
    selection_complete = False
    lineage_active = True
    while parent_id is not None or parent_fingerprint is not None:
        if parent_id is None or parent_fingerprint is None or parent_id in visited:
            return None, True
        visited.add(parent_id)
        parent = sources.get(parent_id)
        if parent is None or parent.source_fingerprint != parent_fingerprint:
            return None, True
        if (
            parent.vision_artifact_id,
            parent.vision_fingerprint,
            parent.product_goal_artifact_id,
            parent.product_goal_fingerprint,
        ) != source_lineage:
            lineage_active = False
        elif lineage_active and not selection_complete:
            candidate, conflict, complete = _revision_candidate_on_ancestor(
                snapshot,
                parent,
                source_lineage=source_lineage,
            )
            if conflict:
                return None, True
            if complete:
                prior = candidate
                selection_complete = True
        parent_id = parent.supersedes_specification_source_id
        parent_fingerprint = parent.supersedes_source_fingerprint
    return prior, False


def _direct_candidates(
    snapshot: WorkflowFactSnapshot,
) -> tuple[tuple[SpecificationCandidateFact, ...], bool]:
    """Return unsuperseded candidates for the exact current source."""
    vision = accepted_current_vision(snapshot)
    goal = accepted_current_goal(snapshot)
    source, source_conflict = _current_specification_source_state(snapshot)
    if vision is None or goal is None or source is None:
        return (), source_conflict
    superseded = {
        candidate.supersedes_specification_candidate_id
        for candidate in snapshot.specification_candidates
        if candidate.supersedes_specification_candidate_id is not None
    }
    direct: list[SpecificationCandidateFact] = []
    conflict = False
    for candidate in snapshot.specification_candidates:
        if candidate.specification_candidate_id in superseded:
            continue
        lineage = (
            candidate.vision_artifact_id,
            candidate.vision_fingerprint,
            candidate.product_goal_artifact_id,
            candidate.product_goal_fingerprint,
            candidate.specification_source_id,
            candidate.specification_source_fingerprint,
        )
        expected = (
            vision.vision_artifact_id,
            vision.content_fingerprint,
            goal.product_goal_artifact_id,
            goal.content_fingerprint,
            source.specification_source_id,
            source.source_fingerprint,
        )
        if lineage == expected:
            direct.append(candidate)
        elif candidate.specification_source_id == source.specification_source_id:
            conflict = True
    return tuple(direct), conflict


def _candidate_decision(
    snapshot: WorkflowFactSnapshot,
    candidate: SpecificationCandidateFact,
) -> tuple[str | None, bool]:
    """Return the sole decision bound to an exact candidate fingerprint."""
    decisions = [
        decision
        for decision in snapshot.specification_decisions
        if decision.specification_candidate_id == candidate.specification_candidate_id
    ]
    exact = [
        decision
        for decision in decisions
        if decision.candidate_fingerprint == candidate.candidate_fingerprint
    ]
    return (exact[0].decision if len(exact) == 1 else None), (
        len(decisions) != len(exact) or len(exact) > 1
    )


def _approved_spec_for_candidate(
    snapshot: WorkflowFactSnapshot,
    candidate: SpecificationCandidateFact,
) -> tuple[SpecVersionFact | None, bool]:
    """Return the one approved SpecRegistry row sourced by this candidate."""
    choices = [
        spec
        for spec in snapshot.spec_versions
        if spec.status == "approved"
        and (
            spec.source_specification_candidate_id,
            spec.source_specification_candidate_fingerprint,
            spec.source_vision_artifact_id,
            spec.source_vision_fingerprint,
            spec.source_product_goal_artifact_id,
            spec.source_product_goal_fingerprint,
        )
        == (
            candidate.specification_candidate_id,
            candidate.candidate_fingerprint,
            candidate.vision_artifact_id,
            candidate.vision_fingerprint,
            candidate.product_goal_artifact_id,
            candidate.product_goal_fingerprint,
        )
    ]
    malformed = any(
        spec.status == "approved"
        and spec.source_specification_candidate_id
        == candidate.specification_candidate_id
        and spec.source_specification_candidate_fingerprint
        != candidate.candidate_fingerprint
        for spec in snapshot.spec_versions
    )
    return (choices[0] if len(choices) == 1 else None), malformed or len(choices) > 1


def _has_superseded_spec(
    snapshot: WorkflowFactSnapshot,
    candidate: SpecificationCandidateFact,
) -> bool:
    """Return whether this accepted candidate's registry row is no longer current."""
    return any(
        spec.status == "superseded"
        and spec.source_specification_candidate_id
        == candidate.specification_candidate_id
        and spec.source_specification_candidate_fingerprint
        == candidate.candidate_fingerprint
        for spec in snapshot.spec_versions
    )


def _candidate_rank(
    snapshot: WorkflowFactSnapshot,
    candidate: SpecificationCandidateFact,
) -> tuple[int, bool]:
    """Rank direct candidates without treating superseded registry rows as conflicts."""
    decision, decision_conflict = _candidate_decision(snapshot, candidate)
    if decision_conflict:
        return 0, True
    if decision is None:
        return 4, False
    if decision in {"rejected", "feedback"}:
        return 3, False
    approved_spec, spec_conflict = _approved_spec_for_candidate(snapshot, candidate)
    if spec_conflict:
        return 0, True
    if _has_superseded_spec(snapshot, candidate):
        return 0, False
    return (1 if approved_spec is not None else 2), False


def _pending_candidate_across_drift_state(
    snapshot: WorkflowFactSnapshot,
) -> tuple[SpecificationCandidateFact | None, bool]:
    """Keep one exact pending leaf reviewable when its source is no longer current."""
    superseded = {
        candidate.supersedes_specification_candidate_id
        for candidate in snapshot.specification_candidates
        if candidate.supersedes_specification_candidate_id is not None
    }
    pending: list[SpecificationCandidateFact] = []
    conflict = False
    for candidate in snapshot.specification_candidates:
        if candidate.specification_candidate_id in superseded:
            continue
        decision, decision_conflict = _candidate_decision(snapshot, candidate)
        conflict = conflict or decision_conflict
        if decision is None and not decision_conflict:
            pending.append(candidate)
    if conflict:
        return None, True
    return (pending[0] if len(pending) == 1 else None), len(pending) > 1


def _current_specification_candidate_state(
    snapshot: WorkflowFactSnapshot,
) -> tuple[SpecificationCandidateFact | None, bool]:
    """Select the current direct candidate by pending/terminal/registry state."""
    direct, conflict = _direct_candidates(snapshot)
    ranked: list[tuple[int, SpecificationCandidateFact]] = []
    for candidate in direct:
        rank, candidate_conflict = _candidate_rank(snapshot, candidate)
        conflict = conflict or candidate_conflict
        if not candidate_conflict and rank > 0:
            ranked.append((rank, candidate))
    if conflict:
        return None, True
    if not ranked:
        fallback, fallback_conflict = _pending_candidate_across_drift_state(snapshot)
        return fallback, fallback_conflict or bool(direct)
    highest = max(rank for rank, _candidate in ranked)
    choices = [candidate for rank, candidate in ranked if rank == highest]
    return (choices[0] if len(choices) == 1 else None), len(choices) > 1


def current_specification_candidate(
    snapshot: WorkflowFactSnapshot,
) -> SpecificationCandidateFact | None:
    """Return the sole direct current candidate or ``None`` on conflict."""
    candidate, _conflict = _current_specification_candidate_state(snapshot)
    return candidate


def _accepted_current_spec_state(
    snapshot: WorkflowFactSnapshot,
) -> tuple[SpecVersionFact | None, bool]:
    """Return the approved row for current Vision/Goal, independent of new source."""
    vision = accepted_current_vision(snapshot)
    goal = accepted_current_goal(snapshot)
    if vision is None or goal is None:
        return None, False
    candidates = {
        (item.specification_candidate_id, item.candidate_fingerprint): item
        for item in snapshot.specification_candidates
    }
    choices = [
        spec
        for spec in snapshot.spec_versions
        if spec.status == "approved"
        and (
            spec.source_vision_artifact_id,
            spec.source_vision_fingerprint,
            spec.source_product_goal_artifact_id,
            spec.source_product_goal_fingerprint,
        )
        == (
            vision.vision_artifact_id,
            vision.content_fingerprint,
            goal.product_goal_artifact_id,
            goal.content_fingerprint,
        )
        and (
            spec.source_specification_candidate_id,
            spec.source_specification_candidate_fingerprint,
        )
        in candidates
    ]
    malformed = any(
        spec.status == "approved"
        and spec.source_product_goal_artifact_id == goal.product_goal_artifact_id
        and spec not in choices
        for spec in snapshot.spec_versions
    )
    return (choices[0] if len(choices) == 1 else None), malformed or len(choices) > 1


def accepted_current_spec(snapshot: WorkflowFactSnapshot) -> SpecVersionFact | None:
    """Return the current approved SpecRegistry row or ``None`` on conflict."""
    spec, _conflict = _accepted_current_spec_state(snapshot)
    return spec


@dataclass(frozen=True)
class ProductDefinitionSelection:
    """Direct current candidate and approved specification selection."""

    specification_source: SpecificationSourceFact | None
    specification_source_conflict: bool
    specification_candidate: SpecificationCandidateFact | None
    specification_candidate_conflict: bool
    accepted_spec: SpecVersionFact | None
    accepted_spec_conflict: bool

    @property
    def has_conflict(self) -> bool:
        """Return whether direct specification lineage is malformed."""
        return (
            self.specification_source_conflict
            or self.specification_candidate_conflict
            or self.accepted_spec_conflict
        )


def select_product_definition_state(
    snapshot: WorkflowFactSnapshot,
) -> ProductDefinitionSelection:
    """Select direct current candidate and exact approved base once."""
    source, source_conflict = _current_specification_source_state(snapshot)
    candidate, candidate_conflict = _current_specification_candidate_state(snapshot)
    spec, spec_conflict = _accepted_current_spec_state(snapshot)
    return ProductDefinitionSelection(
        specification_source=source,
        specification_source_conflict=source_conflict,
        specification_candidate=candidate,
        specification_candidate_conflict=candidate_conflict,
        accepted_spec=spec,
        accepted_spec_conflict=spec_conflict,
    )


def _fact_conflict(snapshot: WorkflowFactSnapshot) -> bool:
    """Report malformed direct product-definition lineage."""
    return select_product_definition_state(snapshot).has_conflict


def _amendment_is_available(
    snapshot: WorkflowFactSnapshot,
    candidate: SpecificationCandidateFact,
    spec: SpecVersionFact,
) -> bool:
    """Permit one amendment after a later quiescent completed Sprint."""
    if not lifecycle_is_quiescent(snapshot) or spec.approved_at is None:
        return False
    baseline = max(spec.approved_at, candidate.recorded_at)
    return any(
        sprint.status == "completed"
        and sprint.completed_at is not None
        and sprint.completed_at > baseline
        for sprint in snapshot.sprints
    )


def _candidate_authoring_rule(
    snapshot: WorkflowFactSnapshot,
    candidate: SpecificationCandidateFact,
) -> tuple[RuleEvaluation, ...]:
    """Wait for review/acceptance of the candidate built from this source."""
    decision, _decision_conflict = _candidate_decision(snapshot, candidate)
    if decision is None:
        return (RuleEvaluation(RuleCategory.SATISFIED, "SPECIFICATION_REVIEW_PENDING"),)
    if decision in {"rejected", "feedback"}:
        return (
            RuleEvaluation(
                RuleCategory.SATISFIED,
                "SPECIFICATION_SOURCE_REVISION_REQUIRED",
            ),
        )
    spec = accepted_current_spec(snapshot)
    if spec is None:
        return (
            RuleEvaluation(RuleCategory.SATISFIED, "SPECIFICATION_ACCEPTANCE_PENDING"),
        )
    return (RuleEvaluation(RuleCategory.SATISFIED, "SPECIFICATION_ACCEPTED"),)


def _source_reference(source: SpecificationSourceFact) -> FactReference:
    return _reference(
        "specification_source",
        source.specification_source_id,
        source.source_fingerprint,
    )


def _source_registration_rule(
    snapshot: WorkflowFactSnapshot,
    _at: datetime,
) -> tuple[RuleEvaluation, ...]:
    """Require immutable external source preparation before each structure call."""
    selection = select_product_definition_state(snapshot)
    vision = accepted_current_vision(snapshot)
    goal = accepted_current_goal(snapshot)
    source = selection.specification_source
    candidate = selection.specification_candidate
    candidate_decision = (
        None if candidate is None else _candidate_decision(snapshot, candidate)[0]
    )
    if selection.has_conflict:
        evaluation = RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT")
    elif candidate is not None and candidate_decision is None:
        evaluation = RuleEvaluation(
            RuleCategory.SATISFIED,
            "SPECIFICATION_REVIEW_PENDING",
        )
    elif (
        vision is None
        or goal is None
        or snapshot.project.active_repository_binding_id is None
    ):
        evaluation = RuleEvaluation(
            RuleCategory.SATISFIED,
            "SPECIFICATION_SOURCE_NOT_READY",
        )
    elif source is None:
        evaluation = RuleEvaluation(
            RuleCategory.AVAILABLE,
            "SPECIFICATION_SOURCE_REQUIRED",
            fact_references=_lineage_references(vision, goal),
        )
    elif candidate is None:
        evaluation = RuleEvaluation(
            RuleCategory.AVAILABLE,
            "SPECIFICATION_SOURCE_REPLACEMENT_AVAILABLE",
            recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
            fact_references=(
                *_lineage_references(vision, goal),
                _source_reference(source),
            ),
        )
    else:
        lineage = _lineage_references(vision, goal)
        source_ref = _source_reference(source)
        decision = candidate_decision
        spec = selection.accepted_spec
        if decision in {"rejected", "feedback"}:
            evaluation = RuleEvaluation(
                RuleCategory.AVAILABLE,
                f"SPECIFICATION_{decision.upper()}_SOURCE_REVISION_REQUIRED",
                fact_references=(
                    *lineage,
                    source_ref,
                    _reference(
                        "specification_candidate",
                        candidate.specification_candidate_id,
                        candidate.candidate_fingerprint,
                    ),
                ),
            )
        elif (
            decision == "accepted"
            and spec is not None
            and _amendment_is_available(snapshot, candidate, spec)
        ):
            evaluation = RuleEvaluation(
                RuleCategory.AVAILABLE,
                "SPECIFICATION_SOURCE_AMENDMENT_AVAILABLE",
                recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
                fact_references=(
                    *lineage,
                    source_ref,
                    _reference("specification", spec.spec_version_id, spec.spec_hash),
                ),
            )
        else:
            evaluation = RuleEvaluation(
                RuleCategory.SATISFIED,
                "SPECIFICATION_SOURCE_REGISTERED",
            )
    return (evaluation,)


def _available_structuring_evaluation(
    snapshot: WorkflowFactSnapshot,
    *,
    vision: VisionArtifactFact,
    goal: ProductGoalArtifactFact,
    source: SpecificationSourceFact,
) -> RuleEvaluation:
    """Build one initial, revision, or amendment structuring decision."""
    references: tuple[FactReference, ...] = (
        *_lineage_references(vision, goal),
        _source_reference(source),
    )
    spec = accepted_current_spec(snapshot)
    prior, ancestor_conflict = _revision_candidate_for_source(snapshot, source)
    if ancestor_conflict:
        return RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT")
    if prior is not None:
        references = (
            *references,
            _reference(
                "specification_candidate",
                prior.specification_candidate_id,
                prior.candidate_fingerprint,
            ),
        )
        reason = "SPECIFICATION_REVISION_REQUIRED"
    elif spec is not None:
        references = (
            *references,
            _reference("specification", spec.spec_version_id, spec.spec_hash),
        )
        reason = "SPECIFICATION_AMENDMENT_REQUIRED"
    else:
        reason = "SPECIFICATION_INITIAL_REQUIRED"
    return RuleEvaluation(
        RuleCategory.AVAILABLE,
        reason,
        fact_references=references,
    )


def _specification_rule(
    snapshot: WorkflowFactSnapshot,
    _at: datetime,
) -> tuple[RuleEvaluation, ...]:
    """Expose structuring only after the exact source has been registered."""
    vision = accepted_current_vision(snapshot)
    goal = accepted_current_goal(snapshot)
    source = current_specification_source(snapshot)
    candidate = current_specification_candidate(snapshot)
    if _fact_conflict(snapshot):
        evaluation = RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT")
    elif candidate is not None and _candidate_decision(snapshot, candidate)[0] is None:
        evaluation = _candidate_authoring_rule(snapshot, candidate)[0]
    elif vision is None or goal is None:
        evaluation = RuleEvaluation(
            RuleCategory.SATISFIED,
            "SPECIFICATION_NOT_READY",
        )
    elif source is None:
        evaluation = RuleEvaluation(
            RuleCategory.SATISFIED,
            "SPECIFICATION_SOURCE_NOT_REGISTERED",
        )
    elif candidate is not None:
        decision, _decision_conflict = _candidate_decision(snapshot, candidate)
        if decision in {"rejected", "feedback"}:
            evaluation = RuleEvaluation(
                RuleCategory.SATISFIED,
                "SPECIFICATION_SOURCE_REVISION_REQUIRED",
            )
        elif (
            decision == "accepted"
            and (spec := accepted_current_spec(snapshot)) is not None
            and _amendment_is_available(snapshot, candidate, spec)
        ):
            evaluation = RuleEvaluation(
                RuleCategory.SATISFIED,
                "SPECIFICATION_SOURCE_AMENDMENT_REQUIRED",
            )
        else:
            evaluation = _candidate_authoring_rule(snapshot, candidate)[0]
    else:
        evaluation = _available_structuring_evaluation(
            snapshot,
            vision=vision,
            goal=goal,
            source=source,
        )
    return (evaluation,)


def _review_rule(
    snapshot: WorkflowFactSnapshot,
    _at: datetime,
) -> tuple[RuleEvaluation, ...]:
    """Wait for one exact pending candidate decision."""
    if _fact_conflict(snapshot):
        return (RuleEvaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT"),)
    candidate = current_specification_candidate(snapshot)
    if candidate is None:
        return (
            RuleEvaluation(RuleCategory.SATISFIED, "SPECIFICATION_REVIEW_NOT_PENDING"),
        )
    decision, _decision_conflict = _candidate_decision(snapshot, candidate)
    if decision is not None:
        return (
            RuleEvaluation(
                RuleCategory.SATISFIED,
                f"SPECIFICATION_REVIEW_{decision.upper()}",
            ),
        )
    return (
        RuleEvaluation(
            RuleCategory.WAITING,
            "SPECIFICATION_REVIEW_REQUIRED",
            fact_references=(
                _reference(
                    "specification_candidate",
                    candidate.specification_candidate_id,
                    candidate.candidate_fingerprint,
                ),
            ),
        ),
    )


SPECIFICATION_NODES: tuple[NodeSpec, ...] = (
    NodeSpec(
        node_id="specification.source.register",
        child_graph_id="specification",
        request_kind="register_specification_source",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="source_path", value_type="string"),
            InputField(name="preparation_capability", value_type="string"),
            InputField(name="adr_path", value_type="string", required=False),
        ),
        evaluate_rule=_source_registration_rule,
    ),
    NodeSpec(
        node_id="specification.structure",
        child_graph_id="specification",
        request_kind="structure_specification",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(),
        evaluate_rule=_specification_rule,
        agentic_execution=AgenticExecutionSpec(
            active_reason="SPECIFICATION_STRUCTURER_ACTIVE",
            failure_reason="SPECIFICATION_STRUCTURER_FAILED",
            recovery_reason="SPECIFICATION_STRUCTURER_RECOVERY_REQUIRED",
        ),
    ),
    NodeSpec(
        node_id="specification.review",
        child_graph_id="specification",
        request_kind="decide_specification",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="decision", value_type="string"),
            InputField(name="rationale", value_type="string"),
        ),
        evaluate_rule=_review_rule,
    ),
)
