"""Pure greenfield and brownfield onboarding graph rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from workflow.contracts import (
    GRAPH_VERSION,
    FactReference,
    InputField,
    RecommendationKind,
)
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
        ChallengeArtifactFact,
        InitialScopeRegistrationFact,
        PrdVersionFact,
        RepositoryBaselineFact,
        RepositoryInventoryFact,
        ReviewDecisionFact,
        SpecDraftFact,
        WorkflowFactSnapshot,
    )


@dataclass(frozen=True)
class _PrdSelection:
    active: PrdVersionFact | None
    conflict: bool


@dataclass(frozen=True)
class _SpecSelection:
    active: SpecDraftFact | None
    conflict: bool


def _evaluation(
    category: RuleCategory,
    reason_code: str,
    *,
    fact_references: tuple[FactReference, ...] = (),
) -> tuple[RuleEvaluation, ...]:
    return (
        RuleEvaluation(
            category=category,
            reason_code=reason_code,
            fact_references=fact_references,
        ),
    )


def _invalid() -> tuple[RuleEvaluation, ...]:
    return _evaluation(RuleCategory.INVALID, "WORKFLOW_FACT_CONFLICT")


def _required_run_id(value: int | None) -> int:
    if value is None:
        msg = "A non-terminal greenfield context has no initial run identity."
        raise RuntimeError(msg)
    return value


def has_historical_accepted_authority(snapshot: WorkflowFactSnapshot) -> bool:
    """Return whether any append-only authority decision records acceptance."""
    return any(
        decision.artifact_type == "authority" and decision.decision == "accepted"
        for decision in snapshot.review_decisions
    )


def _run_context(
    snapshot: WorkflowFactSnapshot,
) -> tuple[int | None, tuple[RuleEvaluation, ...] | None]:
    if snapshot.project.origin != "greenfield":
        return None, _evaluation(RuleCategory.SATISFIED, "NOT_GREENFIELD")
    return _initial_run_context(snapshot)


def _brownfield_run_context(
    snapshot: WorkflowFactSnapshot,
) -> tuple[int | None, tuple[RuleEvaluation, ...] | None]:
    if snapshot.project.origin != "brownfield":
        return None, _evaluation(RuleCategory.SATISFIED, "NOT_BROWNFIELD")
    return _initial_run_context(snapshot)


def _initial_run_context(
    snapshot: WorkflowFactSnapshot,
) -> tuple[int | None, tuple[RuleEvaluation, ...] | None]:
    if len(snapshot.project_abandonments) > 1 or (
        snapshot.project_abandonments and has_historical_accepted_authority(snapshot)
    ):
        return None, _invalid()
    if snapshot.project_abandonments:
        return None, _evaluation(RuleCategory.SATISFIED, "PROJECT_ABANDONED")
    initial_runs = tuple(
        run
        for run in snapshot.discovery_runs
        if run.project_id == snapshot.project.project_id and run.purpose == "initial"
    )
    if len(initial_runs) != 1:
        return None, _invalid()
    if initial_runs[0].closed_at is not None:
        return None, _invalid()
    return initial_runs[0].discovery_run_id, None


def _challenge(
    snapshot: WorkflowFactSnapshot,
    run_id: int,
) -> tuple[ChallengeArtifactFact | None, bool]:
    artifacts = tuple(
        artifact
        for artifact in snapshot.challenge_artifacts
        if artifact.discovery_run_id == run_id
    )
    if len(artifacts) > 1:
        return None, True
    if artifacts and artifacts[0].supersedes_id is not None:
        return None, True
    return (artifacts[0] if artifacts else None), False


def _active_prd(
    snapshot: WorkflowFactSnapshot,
    run_id: int,
) -> _PrdSelection:
    versions = tuple(
        version
        for version in snapshot.prd_versions
        if version.discovery_run_id == run_id
    )
    active: PrdVersionFact | None = None
    conflict = False
    if versions:
        by_id = {version.prd_version_id: version for version in versions}
        referenced = {
            version.supersedes_id
            for version in versions
            if version.supersedes_id is not None
        }
        leaves = tuple(
            version for version in versions if version.prd_version_id not in referenced
        )
        conflict = (
            len(by_id) != len(versions)
            or not referenced <= by_id.keys()
            or len(leaves) != 1
        )
        if not conflict:
            active = leaves[0]
            visited: set[int] = set()
            current: PrdVersionFact | None = active
            while current is not None and current.prd_version_id not in visited:
                visited.add(current.prd_version_id)
                current = (
                    by_id.get(current.supersedes_id)
                    if current.supersedes_id is not None
                    else None
                )
            conflict = current is not None or len(visited) != len(versions)
    return _PrdSelection(active=None if conflict else active, conflict=conflict)


def _active_spec(
    snapshot: WorkflowFactSnapshot,
    run_id: int,
) -> _SpecSelection:
    drafts = tuple(
        draft for draft in snapshot.spec_drafts if draft.discovery_run_id == run_id
    )
    active: SpecDraftFact | None = None
    conflict = any(
        draft.kind != "initial"
        or draft.base_spec_version_id is not None
        or draft.base_spec_hash is not None
        for draft in drafts
    )
    if drafts and not conflict:
        by_id = {draft.spec_draft_id: draft for draft in drafts}
        referenced = {
            draft.supersedes_id for draft in drafts if draft.supersedes_id is not None
        }
        leaves = tuple(
            draft for draft in drafts if draft.spec_draft_id not in referenced
        )
        conflict = (
            len(by_id) != len(drafts)
            or not referenced <= by_id.keys()
            or len(leaves) != 1
        )
        if not conflict:
            active = leaves[0]
            visited: set[int] = set()
            current: SpecDraftFact | None = active
            while current is not None and current.spec_draft_id not in visited:
                visited.add(current.spec_draft_id)
                current = (
                    by_id.get(current.supersedes_id)
                    if current.supersedes_id is not None
                    else None
                )
            conflict = current is not None or len(visited) != len(drafts)
    return _SpecSelection(active=None if conflict else active, conflict=conflict)


def _decision_for(
    snapshot: WorkflowFactSnapshot,
    *,
    artifact_type: str,
    artifact_id: int,
    artifact_fingerprint: str,
) -> tuple[ReviewDecisionFact | None, bool]:
    decisions = tuple(
        decision
        for decision in snapshot.review_decisions
        if decision.artifact_type == artifact_type
        and decision.artifact_id == artifact_id
    )
    if len(decisions) > 1:
        return None, True
    if decisions and decisions[0].artifact_fingerprint != artifact_fingerprint:
        return None, True
    return (decisions[0] if decisions else None), False


def _prd_chain_conflict(
    snapshot: WorkflowFactSnapshot,
    run_id: int,
    active_id: int,
) -> bool:
    for version in snapshot.prd_versions:
        if version.discovery_run_id != run_id or version.prd_version_id == active_id:
            continue
        decision, conflict = _decision_for(
            snapshot,
            artifact_type="prd",
            artifact_id=version.prd_version_id,
            artifact_fingerprint=version.content_fingerprint,
        )
        if conflict or decision is None or decision.decision == "accepted":
            return True
    return False


def _spec_chain_conflict(
    snapshot: WorkflowFactSnapshot,
    run_id: int,
    active_id: int,
) -> bool:
    for draft in snapshot.spec_drafts:
        if draft.discovery_run_id != run_id or draft.spec_draft_id == active_id:
            continue
        decision, conflict = _decision_for(
            snapshot,
            artifact_type="spec_draft",
            artifact_id=draft.spec_draft_id,
            artifact_fingerprint=draft.content_fingerprint,
        )
        if conflict or decision is None or decision.decision == "accepted":
            return True
    return False


def _reference(
    fact_type: str,
    fact_id: int,
    fingerprint: str,
) -> FactReference:
    return FactReference(
        fact_type=fact_type,
        fact_id=str(fact_id),
        fingerprint=fingerprint,
    )


def _challenge_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    run_id, terminal = _run_context(snapshot)
    if terminal is not None:
        return terminal
    run_id = _required_run_id(run_id)
    challenge, conflict = _challenge(snapshot, run_id)
    if conflict:
        return _invalid()
    downstream_exists = any(
        version.discovery_run_id == run_id for version in snapshot.prd_versions
    ) or any(draft.discovery_run_id == run_id for draft in snapshot.spec_drafts)
    if challenge is None and downstream_exists:
        return _invalid()
    if challenge is None:
        return _evaluation(RuleCategory.AVAILABLE, "CHALLENGE_REQUIRED")
    return _evaluation(RuleCategory.SATISFIED, "CHALLENGE_RECORDED")


def _prd_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    run_id, terminal = _run_context(snapshot)
    result = terminal or _invalid()
    if terminal is None:
        run_id = _required_run_id(run_id)
        challenge, challenge_conflict = _challenge(snapshot, run_id)
        selection = _active_prd(snapshot, run_id)
        if challenge_conflict or selection.conflict:
            result = _invalid()
        elif challenge is None:
            result = _evaluation(RuleCategory.WAITING, "WAITING_FOR_CHALLENGE")
        elif selection.active is None:
            result = _evaluation(
                RuleCategory.AVAILABLE,
                "PRD_REQUIRED",
                fact_references=(
                    _reference(
                        "challenge_artifact",
                        challenge.challenge_artifact_id,
                        challenge.content_fingerprint,
                    ),
                ),
            )
        elif _prd_chain_conflict(
            snapshot,
            run_id,
            selection.active.prd_version_id,
        ):
            result = _invalid()
        else:
            decision, conflict = _decision_for(
                snapshot,
                artifact_type="prd",
                artifact_id=selection.active.prd_version_id,
                artifact_fingerprint=selection.active.content_fingerprint,
            )
            if (
                not conflict
                and decision is not None
                and decision.decision != "accepted"
            ):
                result = _evaluation(
                    RuleCategory.AVAILABLE,
                    "PRD_REPLACEMENT_REQUIRED",
                    fact_references=(
                        _reference(
                            "prd",
                            selection.active.prd_version_id,
                            selection.active.content_fingerprint,
                        ),
                    ),
                )
            else:
                result = _evaluation(
                    RuleCategory.SATISFIED,
                    "PRD_VERSION_RECORDED",
                )
    return result


def _prd_review_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    run_id, terminal = _run_context(snapshot)
    result = terminal or _invalid()
    if terminal is None:
        run_id = _required_run_id(run_id)
        selection = _active_prd(snapshot, run_id)
        if selection.conflict:
            result = _invalid()
        elif selection.active is None:
            result = _evaluation(RuleCategory.WAITING, "WAITING_FOR_PRD")
        elif _prd_chain_conflict(
            snapshot,
            run_id,
            selection.active.prd_version_id,
        ):
            result = _invalid()
        else:
            decision, conflict = _decision_for(
                snapshot,
                artifact_type="prd",
                artifact_id=selection.active.prd_version_id,
                artifact_fingerprint=selection.active.content_fingerprint,
            )
            if conflict:
                result = _invalid()
            elif decision is not None:
                result = _evaluation(RuleCategory.SATISFIED, "PRD_REVIEWED")
            else:
                result = _evaluation(
                    RuleCategory.AVAILABLE,
                    "PRD_REVIEW_REQUIRED",
                    fact_references=(
                        _reference(
                            "prd",
                            selection.active.prd_version_id,
                            selection.active.content_fingerprint,
                        ),
                    ),
                )
    return result


def _accepted_prd(
    snapshot: WorkflowFactSnapshot,
    run_id: int,
) -> tuple[PrdVersionFact | None, bool]:
    selection = _active_prd(snapshot, run_id)
    if selection.conflict or selection.active is None:
        return None, selection.conflict
    if _prd_chain_conflict(snapshot, run_id, selection.active.prd_version_id):
        return None, True
    decision, conflict = _decision_for(
        snapshot,
        artifact_type="prd",
        artifact_id=selection.active.prd_version_id,
        artifact_fingerprint=selection.active.content_fingerprint,
    )
    if conflict:
        return None, True
    if decision is None or decision.decision != "accepted":
        return None, False
    return selection.active, False


def _initial_spec_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    run_id, terminal = _run_context(snapshot)
    result = terminal or _invalid()
    if terminal is None:
        run_id = _required_run_id(run_id)
        accepted_prd, prd_conflict = _accepted_prd(snapshot, run_id)
        selection = _active_spec(snapshot, run_id)
        if selection.conflict:
            result = _invalid()
        elif accepted_prd is None:
            result = (
                _invalid()
                if selection.active is not None
                else _evaluation(
                    RuleCategory.WAITING,
                    "WAITING_FOR_VALID_PRD_REVIEW"
                    if prd_conflict
                    else "WAITING_FOR_ACCEPTED_PRD",
                )
            )
        elif selection.active is None:
            result = _evaluation(
                RuleCategory.AVAILABLE,
                "INITIAL_SPEC_REQUIRED",
                fact_references=(
                    _reference(
                        "prd",
                        accepted_prd.prd_version_id,
                        accepted_prd.content_fingerprint,
                    ),
                ),
            )
        elif _spec_chain_conflict(
            snapshot,
            run_id,
            selection.active.spec_draft_id,
        ):
            result = _invalid()
        else:
            decision, conflict = _decision_for(
                snapshot,
                artifact_type="spec_draft",
                artifact_id=selection.active.spec_draft_id,
                artifact_fingerprint=selection.active.content_fingerprint,
            )
            if (
                not conflict
                and decision is not None
                and decision.decision != "accepted"
            ):
                result = _evaluation(
                    RuleCategory.AVAILABLE,
                    "INITIAL_SPEC_REPLACEMENT_REQUIRED",
                    fact_references=(
                        _reference(
                            "spec_draft",
                            selection.active.spec_draft_id,
                            selection.active.content_fingerprint,
                        ),
                    ),
                )
            else:
                result = _evaluation(
                    RuleCategory.SATISFIED,
                    "INITIAL_SPEC_RECORDED",
                )
    return result


def _initial_spec_review_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    run_id, terminal = _run_context(snapshot)
    result = terminal or _invalid()
    if terminal is None:
        run_id = _required_run_id(run_id)
        selection = _active_spec(snapshot, run_id)
        if selection.conflict:
            result = _invalid()
        elif selection.active is None:
            result = _evaluation(
                RuleCategory.WAITING,
                "WAITING_FOR_INITIAL_SPEC",
            )
        elif _spec_chain_conflict(
            snapshot,
            run_id,
            selection.active.spec_draft_id,
        ):
            result = _invalid()
        else:
            decision, conflict = _decision_for(
                snapshot,
                artifact_type="spec_draft",
                artifact_id=selection.active.spec_draft_id,
                artifact_fingerprint=selection.active.content_fingerprint,
            )
            if conflict:
                result = _invalid()
            elif decision is not None:
                result = _evaluation(
                    RuleCategory.SATISFIED,
                    "INITIAL_SPEC_REVIEWED",
                )
            else:
                result = _evaluation(
                    RuleCategory.AVAILABLE,
                    "INITIAL_SPEC_REVIEW_REQUIRED",
                    fact_references=(
                        _reference(
                            "spec_draft",
                            selection.active.spec_draft_id,
                            selection.active.content_fingerprint,
                        ),
                    ),
                )
    return result


def _accepted_spec(
    snapshot: WorkflowFactSnapshot,
    run_id: int,
) -> tuple[SpecDraftFact | None, bool]:
    selection = _active_spec(snapshot, run_id)
    if selection.conflict or selection.active is None:
        return None, selection.conflict
    if _spec_chain_conflict(snapshot, run_id, selection.active.spec_draft_id):
        return None, True
    decision, conflict = _decision_for(
        snapshot,
        artifact_type="spec_draft",
        artifact_id=selection.active.spec_draft_id,
        artifact_fingerprint=selection.active.content_fingerprint,
    )
    if conflict:
        return None, True
    if decision is None or decision.decision != "accepted":
        return None, False
    return selection.active, False


def _brownfield_evidence(
    snapshot: WorkflowFactSnapshot,
) -> tuple[RepositoryBaselineFact | None, RepositoryInventoryFact | None, bool]:
    baselines = snapshot.repository_baselines
    inventories = snapshot.repository_inventories
    if len(baselines) > 1 or len(inventories) > 1:
        return None, None, True
    baseline = baselines[0] if baselines else None
    inventory = inventories[0] if inventories else None
    conflict = inventory is not None and (
        baseline is None
        or inventory.repository_baseline_id != baseline.repository_baseline_id
    )
    return baseline, inventory, conflict


def _brownfield_baseline_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    _run_id, terminal = _brownfield_run_context(snapshot)
    if terminal is not None:
        return terminal
    baseline, inventory, conflict = _brownfield_evidence(snapshot)
    downstream_exists = (
        inventory is not None
        or bool(snapshot.spec_drafts)
        or bool(snapshot.initial_registrations)
    )
    if conflict or (baseline is None and downstream_exists):
        return _invalid()
    if baseline is None:
        return _evaluation(RuleCategory.AVAILABLE, "REPOSITORY_BASELINE_REQUIRED")
    return _evaluation(RuleCategory.SATISFIED, "REPOSITORY_BASELINE_RECORDED")


def _brownfield_inventory_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    _run_id, terminal = _brownfield_run_context(snapshot)
    if terminal is not None:
        return terminal
    baseline, inventory, conflict = _brownfield_evidence(snapshot)
    if conflict or (baseline is None and inventory is not None):
        return _invalid()
    if baseline is None:
        return _evaluation(RuleCategory.WAITING, "WAITING_FOR_REPOSITORY_BASELINE")
    if inventory is None and (snapshot.spec_drafts or snapshot.initial_registrations):
        return _invalid()
    if inventory is None:
        return _evaluation(
            RuleCategory.AVAILABLE,
            "REPOSITORY_INVENTORY_REQUIRED",
            fact_references=(
                _reference(
                    "repository_baseline",
                    baseline.repository_baseline_id,
                    baseline.content_fingerprint,
                ),
            ),
        )
    return _evaluation(RuleCategory.SATISFIED, "REPOSITORY_INVENTORY_RECORDED")


def _brownfield_curation_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    run_id, terminal = _brownfield_run_context(snapshot)
    result = terminal or _invalid()
    if terminal is None:
        run_id = _required_run_id(run_id)
        _baseline, inventory, conflict = _brownfield_evidence(snapshot)
        selection = _active_spec(snapshot, run_id)
        if conflict or selection.conflict:
            result = _invalid()
        elif inventory is None:
            result = (
                _invalid()
                if selection.active is not None
                else _evaluation(
                    RuleCategory.WAITING,
                    "WAITING_FOR_REPOSITORY_INVENTORY",
                )
            )
        elif selection.active is None:
            result = _evaluation(
                RuleCategory.AVAILABLE,
                "BROWNFIELD_SPEC_DRAFT_REQUIRED",
                fact_references=(
                    _reference(
                        "repository_inventory",
                        inventory.repository_inventory_id,
                        inventory.content_fingerprint,
                    ),
                ),
            )
        else:
            decision, decision_conflict = _decision_for(
                snapshot,
                artifact_type="spec_draft",
                artifact_id=selection.active.spec_draft_id,
                artifact_fingerprint=selection.active.content_fingerprint,
            )
            if decision_conflict:
                result = _invalid()
            elif decision is not None and decision.decision != "accepted":
                result = _evaluation(
                    RuleCategory.AVAILABLE,
                    "BROWNFIELD_SPEC_REPLACEMENT_REQUIRED",
                    fact_references=(
                        _reference(
                            "spec_draft",
                            selection.active.spec_draft_id,
                            selection.active.content_fingerprint,
                        ),
                    ),
                )
            else:
                result = _evaluation(
                    RuleCategory.SATISFIED,
                    "BROWNFIELD_SPEC_DRAFT_RECORDED",
                )
    return result


def _brownfield_initial_spec_review_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    run_id, terminal = _brownfield_run_context(snapshot)
    result = terminal or _invalid()
    if terminal is None:
        run_id = _required_run_id(run_id)
        _baseline, inventory, evidence_conflict = _brownfield_evidence(snapshot)
        selection = _active_spec(snapshot, run_id)
        if evidence_conflict or selection.conflict:
            result = _invalid()
        elif selection.active is None:
            result = _evaluation(RuleCategory.WAITING, "WAITING_FOR_BROWNFIELD_SPEC")
        elif inventory is None:
            result = _invalid()
        else:
            decision, decision_conflict = _decision_for(
                snapshot,
                artifact_type="spec_draft",
                artifact_id=selection.active.spec_draft_id,
                artifact_fingerprint=selection.active.content_fingerprint,
            )
            if decision_conflict:
                result = _invalid()
            elif decision is not None:
                result = _evaluation(
                    RuleCategory.SATISFIED,
                    "BROWNFIELD_SPEC_REVIEWED",
                )
            else:
                result = _evaluation(
                    RuleCategory.AVAILABLE,
                    "BROWNFIELD_SPEC_REVIEW_REQUIRED",
                    fact_references=(
                        _reference(
                            "spec_draft",
                            selection.active.spec_draft_id,
                            selection.active.content_fingerprint,
                        ),
                    ),
                )
    return result


def _registration_waiting_for_prd(
    *,
    downstream_exists: bool,
    conflict: bool,
) -> tuple[RuleEvaluation, ...]:
    if downstream_exists:
        return _invalid()
    reason = "WAITING_FOR_VALID_PRD_REVIEW" if conflict else "WAITING_FOR_ACCEPTED_PRD"
    return _evaluation(RuleCategory.WAITING, reason)


def _registration_waiting_for_spec(
    *,
    registration_exists: bool,
    conflict: bool,
) -> tuple[RuleEvaluation, ...]:
    if registration_exists:
        return _invalid()
    reason = (
        "WAITING_FOR_VALID_INITIAL_SPEC_REVIEW"
        if conflict
        else "WAITING_FOR_ACCEPTED_INITIAL_SPEC"
    )
    return _evaluation(RuleCategory.WAITING, reason)


def _registration_rule(
    snapshot: WorkflowFactSnapshot,
    _evaluated_at: datetime,
) -> tuple[RuleEvaluation, ...]:
    run_id, terminal = _initial_run_context(snapshot)
    if terminal is not None:
        return terminal
    run_id = _required_run_id(run_id)
    accepted_spec, spec_conflict = _accepted_spec(snapshot, run_id)
    registrations = tuple(
        registration
        for registration in snapshot.initial_registrations
        if registration.discovery_run_id == run_id
    )
    if len(registrations) > 1:
        return _invalid()
    if snapshot.project.origin == "greenfield":
        return _greenfield_registration_result(
            snapshot,
            run_id=run_id,
            accepted_spec=accepted_spec,
            spec_conflict=spec_conflict,
            registrations=registrations,
        )
    return _brownfield_registration_result(
        snapshot,
        accepted_spec=accepted_spec,
        spec_conflict=spec_conflict,
        registrations=registrations,
    )


def _greenfield_registration_result(
    snapshot: WorkflowFactSnapshot,
    *,
    run_id: int,
    accepted_spec: SpecDraftFact | None,
    spec_conflict: bool,
    registrations: tuple[InitialScopeRegistrationFact, ...],
) -> tuple[RuleEvaluation, ...]:
    accepted_prd, prd_conflict = _accepted_prd(snapshot, run_id)
    if accepted_prd is None:
        return _registration_waiting_for_prd(
            downstream_exists=accepted_spec is not None or bool(registrations),
            conflict=prd_conflict,
        )
    if accepted_spec is None:
        return _registration_waiting_for_spec(
            registration_exists=bool(registrations),
            conflict=spec_conflict,
        )
    return _registration_for_accepted_spec(accepted_spec, registrations)


def _brownfield_registration_result(
    snapshot: WorkflowFactSnapshot,
    *,
    accepted_spec: SpecDraftFact | None,
    spec_conflict: bool,
    registrations: tuple[InitialScopeRegistrationFact, ...],
) -> tuple[RuleEvaluation, ...]:
    baseline, inventory, evidence_conflict = _brownfield_evidence(snapshot)
    evidence_missing = baseline is None or inventory is None
    if evidence_conflict or (
        evidence_missing and (accepted_spec is not None or bool(registrations))
    ):
        return _invalid()
    if evidence_missing:
        return _evaluation(RuleCategory.WAITING, "WAITING_FOR_REPOSITORY_INVENTORY")
    if accepted_spec is None:
        return _registration_waiting_for_spec(
            registration_exists=bool(registrations),
            conflict=spec_conflict,
        )
    return _registration_for_accepted_spec(accepted_spec, registrations)


def _registration_for_accepted_spec(
    accepted_spec: SpecDraftFact,
    registrations: tuple[InitialScopeRegistrationFact, ...],
) -> tuple[RuleEvaluation, ...]:
    if registrations:
        return (
            _evaluation(RuleCategory.SATISFIED, "INITIAL_SCOPE_REGISTERED")
            if registrations[0].spec_draft_id == accepted_spec.spec_draft_id
            else _invalid()
        )
    return _registration_available(accepted_spec)


def _registration_available(
    accepted_spec: SpecDraftFact,
) -> tuple[RuleEvaluation, ...]:
    return _evaluation(
        RuleCategory.AVAILABLE,
        "INITIAL_SCOPE_REGISTRATION_REQUIRED",
        fact_references=(
            _reference(
                "spec_draft",
                accepted_spec.spec_draft_id,
                accepted_spec.content_fingerprint,
            ),
        ),
    )


_INITIAL_SCOPE_REGISTRATION_NODE = NodeSpec(
    node_id="onboarding.initial_scope_registration",
    child_graph_id="onboarding",
    request_kind="register_initial_scope",
    recommendation_kind=RecommendationKind.REQUIRED,
    required_inputs=(InputField(name="spec_draft_id", value_type="integer"),),
    evaluate_rule=_registration_rule,
)

BROWNFIELD_ONBOARDING_NODES: tuple[NodeSpec, ...] = (
    NodeSpec(
        node_id="onboarding.brownfield.baseline",
        child_graph_id="onboarding",
        request_kind="record_repository_baseline",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="repository_path", value_type="string"),
            InputField(name="git_commit", value_type="string", required=False),
            InputField(name="dirty", value_type="boolean"),
            InputField(name="baseline_fingerprint", value_type="string"),
        ),
        evaluate_rule=_brownfield_baseline_rule,
    ),
    NodeSpec(
        node_id="onboarding.brownfield.inventory",
        child_graph_id="onboarding",
        request_kind="record_repository_inventory",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="repository_baseline_id", value_type="integer"),
            InputField(name="files", value_type="array"),
            InputField(name="selected_for_model", value_type="array"),
            InputField(name="total_bytes", value_type="integer"),
            InputField(name="inventory_fingerprint", value_type="string"),
        ),
        evaluate_rule=_brownfield_inventory_rule,
    ),
    NodeSpec(
        node_id="onboarding.brownfield.curation",
        child_graph_id="onboarding",
        request_kind="record_brownfield_spec_draft",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="repository_inventory_id", value_type="integer"),
            InputField(name="canonical_content", value_type="object"),
            InputField(
                name="supersedes_spec_draft_id",
                value_type="integer",
                required=False,
            ),
            InputField(
                name="provenance_path",
                value_type="string",
                required=False,
            ),
        ),
        evaluate_rule=_brownfield_curation_rule,
    ),
    NodeSpec(
        node_id="onboarding.brownfield.initial_spec_review",
        child_graph_id="onboarding",
        request_kind="decide_brownfield_initial_spec",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="spec_draft_id", value_type="integer"),
            InputField(name="artifact_fingerprint", value_type="string"),
            InputField(name="decision", value_type="string"),
            InputField(name="notes", value_type="string"),
        ),
        evaluate_rule=_brownfield_initial_spec_review_rule,
    ),
)


GREENFIELD_ONBOARDING_NODES: tuple[NodeSpec, ...] = (
    NodeSpec(
        node_id="onboarding.greenfield.challenge",
        child_graph_id="onboarding",
        request_kind="record_challenge_artifact",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="canonical_content", value_type="object"),
            InputField(
                name="provenance_path",
                value_type="string",
                required=False,
            ),
        ),
        evaluate_rule=_challenge_rule,
    ),
    NodeSpec(
        node_id="onboarding.greenfield.prd",
        child_graph_id="onboarding",
        request_kind="record_prd_version",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="challenge_artifact_id", value_type="integer"),
            InputField(name="canonical_content", value_type="object"),
            InputField(
                name="supersedes_prd_version_id",
                value_type="integer",
                required=False,
            ),
            InputField(
                name="provenance_path",
                value_type="string",
                required=False,
            ),
        ),
        evaluate_rule=_prd_rule,
    ),
    NodeSpec(
        node_id="onboarding.greenfield.prd_review",
        child_graph_id="onboarding",
        request_kind="decide_prd",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="prd_version_id", value_type="integer"),
            InputField(name="artifact_fingerprint", value_type="string"),
            InputField(name="decision", value_type="string"),
            InputField(name="notes", value_type="string"),
        ),
        evaluate_rule=_prd_review_rule,
    ),
    NodeSpec(
        node_id="onboarding.greenfield.initial_spec",
        child_graph_id="onboarding",
        request_kind="record_initial_spec_draft",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="prd_version_id", value_type="integer"),
            InputField(name="canonical_content", value_type="object"),
            InputField(
                name="supersedes_spec_draft_id",
                value_type="integer",
                required=False,
            ),
            InputField(
                name="provenance_path",
                value_type="string",
                required=False,
            ),
        ),
        evaluate_rule=_initial_spec_rule,
    ),
    NodeSpec(
        node_id="onboarding.greenfield.initial_spec_review",
        child_graph_id="onboarding",
        request_kind="decide_initial_spec_draft",
        recommendation_kind=RecommendationKind.REQUIRED,
        required_inputs=(
            InputField(name="spec_draft_id", value_type="integer"),
            InputField(name="artifact_fingerprint", value_type="string"),
            InputField(name="decision", value_type="string"),
            InputField(name="notes", value_type="string"),
        ),
        evaluate_rule=_initial_spec_review_rule,
    ),
    _INITIAL_SCOPE_REGISTRATION_NODE,
)


def greenfield_graph() -> WorkflowGraph:
    """Return the isolated pure greenfield onboarding graph."""
    return WorkflowGraph(
        graph_version=GRAPH_VERSION,
        root=ChildGraphSpec(
            child_graph_id="product_lifecycle",
            nodes=(),
            children=(
                ChildGraphSpec(
                    child_graph_id="onboarding",
                    nodes=GREENFIELD_ONBOARDING_NODES,
                ),
            ),
        ),
    )


def brownfield_graph() -> WorkflowGraph:
    """Return the isolated pure brownfield onboarding graph."""
    return WorkflowGraph(
        graph_version=GRAPH_VERSION,
        root=ChildGraphSpec(
            child_graph_id="product_lifecycle",
            nodes=(),
            children=(
                ChildGraphSpec(
                    child_graph_id="onboarding",
                    nodes=(
                        *BROWNFIELD_ONBOARDING_NODES,
                        _INITIAL_SCOPE_REGISTRATION_NODE,
                    ),
                ),
            ),
        ),
    )


__all__ = [
    "BROWNFIELD_ONBOARDING_NODES",
    "GREENFIELD_ONBOARDING_NODES",
    "brownfield_graph",
    "greenfield_graph",
    "has_historical_accepted_authority",
]
