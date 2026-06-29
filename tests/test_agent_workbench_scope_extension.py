"""Tests for project scope extension validation."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from sqlmodel import select

from models.agent_workbench import (
    DiscoveryChallengeArtifact,
    DiscoveryPrd,
    DiscoverySpecAmendmentDraft,
)
from models.core import Product, Sprint, Team, UserStory
from models.enums import SprintStatus, StoryStatus
from models.specs import SpecAuthorityAcceptance, SpecRegistry
from services.agent_workbench.error_codes import ErrorCode
from services.agent_workbench.fingerprints import canonical_hash
from services.agent_workbench.scope_extension import (
    ERR_SCOPE_EXTENSION_BASE_SPEC_MISMATCH,
    ERR_SCOPE_EXTENSION_NO_ADDED_ITEMS,
    ERR_SCOPE_EXTENSION_NOT_ADDITIVE,
    ERR_SCOPE_EXTENSION_NOT_AVAILABLE,
    ERR_SCOPE_EXTENSION_UNRESOLVED_WORK,
    SCOPE_EXTENSION_AVAILABLE,
    SCOPE_EXTENSION_BLOCKED,
    SCOPE_EXTENSION_STARTED,
    SCOPE_EXTENSION_VALID,
    ScopeExtensionIssue,
    ScopeExtensionRunner,
    ScopeExtensionStartRequest,
    ScopeExtensionValidateRequest,
    _recovery_marker_from_notes,
    evaluate_scope_extension_preconditions,
    load_structured_spec_file,
    validate_additive_scope_extension,
)
from services.specs.profile_content import normalize_spec_content_for_registry
from utils.agileforge_spec_profile import TechnicalSpecArtifact

if TYPE_CHECKING:
    from pathlib import Path

    from sqlmodel import Session


def _artifact() -> dict[str, Any]:
    return {
        "schema_version": "agileforge.spec.v1",
        "artifact_id": "SPEC.scope-extension",
        "title": "Scope Extension Fixture",
        "status": "draft",
        "version": "0.1",
        "created_at": "2026-06-14",
        "updated_at": "2026-06-14",
        "summary": "Exercise additive scope extension.",
        "problem_statement": "A mature project needs new accepted scope.",
        "items": [
            {
                "id": "GOAL.existing",
                "type": "GOAL",
                "status": "accepted",
                "title": "Existing goal",
                "statement": "Preserve existing accepted goal.",
            },
            {
                "id": "REQ.existing-capability",
                "type": "REQ",
                "status": "accepted",
                "title": "Existing capability",
                "statement": "The system MUST preserve existing capability.",
                "level": "MUST",
                "verification": "acceptance-test",
                "acceptance": ["Existing capability remains available."],
            },
        ],
        "relations": [
            {
                "from": "REQ.existing-capability",
                "type": "satisfies",
                "to": "GOAL.existing",
                "rationale": "Requirement satisfies the existing goal.",
            }
        ],
        "controlled_terms": [],
        "external_references": [],
        "rendering": {"markdown_profile": "agileforge.spec_markdown.v1"},
    }


def _with_new_item(base: dict[str, Any]) -> dict[str, Any]:
    amended = deepcopy(base)
    amended["items"].append(
        {
            "id": "REQ.new-capability",
            "type": "REQ",
            "status": "accepted",
            "title": "New capability",
            "statement": "The system MUST support a new capability.",
            "level": "MUST",
            "verification": "acceptance-test",
            "acceptance": ["New capability is available."],
        }
    )
    return amended


def _issue_codes(issues: list[ScopeExtensionIssue]) -> set[str]:
    return {issue.code for issue in issues}


def _workflow_state(fsm_state: str = "SPRINT_COMPLETE") -> dict[str, str]:
    return {"fsm_state": fsm_state}


class _WorkflowServiceDouble:
    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.state = state or {}
        self.updates: list[dict[str, Any]] = []

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        _ = session_id
        return dict(self.state)

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, Any],
    ) -> None:
        _ = session_id
        self.updates.append(dict(partial_update))
        self.state.update(partial_update)


class _FailOnceWorkflowServiceDouble(_WorkflowServiceDouble):
    def __init__(self, state: dict[str, Any] | None = None) -> None:
        super().__init__(state)
        self.failures_remaining = 1

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, Any],
    ) -> None:
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            message = "workflow update failed"
            raise RuntimeError(message)
        super().update_session_status(session_id, partial_update)


def _product(session: Session) -> Product:
    product = Product(name="Scope Extension Product")
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


def _product_id(product: Product) -> int:
    product_id = product.product_id
    assert product_id is not None
    return product_id


def _team(session: Session) -> Team:
    team = Team(name="Scope Extension Team")
    session.add(team)
    session.commit()
    session.refresh(team)
    return team


def _story(
    session: Session,
    product_id: int,
    *,
    status: StoryStatus = StoryStatus.TO_DO,
    is_superseded: bool = False,
    archived_reason: str | None = None,
) -> UserStory:
    story = UserStory(
        product_id=product_id,
        title=f"Story {status.value}",
        status=status,
        is_superseded=is_superseded,
        archived_reason=archived_reason,
    )
    session.add(story)
    session.commit()
    session.refresh(story)
    return story


def _sprint(
    session: Session,
    product_id: int,
    *,
    status: SprintStatus,
) -> Sprint:
    team = _team(session)
    sprint = Sprint(product_id=product_id, team_id=team.team_id, status=status)
    session.add(sprint)
    session.commit()
    session.refresh(sprint)
    return sprint


def _write_spec_file(tmp_path: Path, name: str, artifact: dict[str, Any]) -> Path:
    spec_file = tmp_path / name
    spec_file.write_text(json.dumps(artifact), encoding="utf-8")
    return spec_file


def _accepted_base_spec(
    session: Session,
    product_id: int,
    artifact: dict[str, Any] | None = None,
) -> SpecRegistry:
    normalized = normalize_spec_content_for_registry(
        json.dumps(artifact or _artifact())
    )
    spec = SpecRegistry(
        product_id=product_id,
        spec_hash=normalized.spec_hash,
        content=normalized.content,
        content_ref="accepted-base.json",
        status="approved",
        approved_by="test",
        approval_notes="accepted for tests",
    )
    session.add(spec)
    session.commit()
    session.refresh(spec)
    acceptance = SpecAuthorityAcceptance(
        product_id=product_id,
        spec_version_id=spec.spec_version_id or 0,
        status="accepted",
        policy="test",
        decided_by="test",
        compiler_version="test",
        prompt_hash="prompt",
        spec_hash=spec.spec_hash,
    )
    session.add(acceptance)
    session.commit()
    session.refresh(spec)
    return spec


def _spec_rows(session: Session, product_id: int) -> list[SpecRegistry]:
    return list(
        session.exec(
            select(SpecRegistry).where(SpecRegistry.product_id == product_id)
        ).all()
    )


def _accepted_discovery_spec_amendment(
    session: Session,
    product_id: int,
    *,
    amendment_file: Path,
    base_spec: SpecRegistry,
    status: str = "accepted",
) -> DiscoverySpecAmendmentDraft:
    """Persist a discovery amendment artifact with provenance for scope start."""
    artifact = DiscoveryChallengeArtifact(
        project_id=product_id,
        producer="grill-with-docs",
        readiness="ready_for_prd",
        original_idea="Add scope through discovery.",
        content_json="{}",
        artifact_fingerprint="challenge-fingerprint",
        request_hash="challenge-request",
        idempotency_key="challenge-for-amendment",
    )
    session.add(artifact)
    session.commit()
    session.refresh(artifact)
    prd = DiscoveryPrd(
        project_id=product_id,
        challenge_artifact_id=artifact.challenge_artifact_id or 0,
        producer="to-prd",
        status="accepted",
        version="1",
        title="Scope Extension PRD",
        content_json="{}",
        artifact_fingerprint="prd-fingerprint",
        request_hash="prd-request",
        idempotency_key="prd-for-amendment",
    )
    session.add(prd)
    session.commit()
    session.refresh(prd)
    draft = DiscoverySpecAmendmentDraft(
        project_id=product_id,
        prd_id=prd.prd_id or 0,
        challenge_artifact_id=artifact.challenge_artifact_id or 0,
        status=status,
        amendment_file=str(amendment_file),
        content_json=amendment_file.read_text(encoding="utf-8"),
        validation_json=json.dumps({"valid": True, "blocking_issues": []}),
        artifact_fingerprint="amendment-fingerprint",
        request_hash="amendment-request",
        idempotency_key=f"amendment-{status}",
        base_spec_version_id=base_spec.spec_version_id,
        base_spec_hash=base_spec.spec_hash,
        amended_spec_hash="amendment-hash",
    )
    session.add(draft)
    session.commit()
    session.refresh(draft)
    return draft


def test_scope_extension_preconditions_available_when_sprint_complete_and_no_open_work(
    session: Session,
) -> None:
    """Allow extension only after completed workflow state with exhausted scope."""
    product = _product(session)

    result = evaluate_scope_extension_preconditions(
        session=session,
        product_id=_product_id(product),
        workflow_state=_workflow_state(),
        sprint_candidate_count=0,
    )

    assert result.status == SCOPE_EXTENSION_AVAILABLE
    assert result.available is True
    assert result.blocking_reason is None


def test_scope_extension_preconditions_block_active_sprint(
    session: Session,
) -> None:
    """Block extension while an active sprint exists."""
    product = _product(session)
    _sprint(session, _product_id(product), status=SprintStatus.ACTIVE)

    result = evaluate_scope_extension_preconditions(
        session=session,
        product_id=_product_id(product),
        workflow_state=_workflow_state(),
        sprint_candidate_count=0,
    )

    assert result.status == SCOPE_EXTENSION_BLOCKED
    assert result.available is False
    assert result.blocking_reason == "ACTIVE_SPRINT_EXISTS"


def test_scope_extension_preconditions_block_planned_sprint(
    session: Session,
) -> None:
    """Block extension while a planned sprint exists."""
    product = _product(session)
    _sprint(session, _product_id(product), status=SprintStatus.PLANNED)

    result = evaluate_scope_extension_preconditions(
        session=session,
        product_id=_product_id(product),
        workflow_state=_workflow_state(),
        sprint_candidate_count=0,
    )

    assert result.status == SCOPE_EXTENSION_BLOCKED
    assert result.available is False
    assert result.blocking_reason == "PLANNED_SPRINT_EXISTS"


def test_scope_extension_preconditions_block_open_story(
    session: Session,
) -> None:
    """Block extension while any non-terminal story remains."""
    product = _product(session)
    _story(session, _product_id(product), status=StoryStatus.IN_PROGRESS)

    result = evaluate_scope_extension_preconditions(
        session=session,
        product_id=_product_id(product),
        workflow_state=_workflow_state(),
        sprint_candidate_count=0,
    )

    assert result.status == SCOPE_EXTENSION_BLOCKED
    assert result.available is False
    assert result.blocking_reason == "OPEN_STORY_EXISTS"


def test_scope_extension_preconditions_block_remaining_sprint_candidates(
    session: Session,
) -> None:
    """Block extension while sprint planning still has candidate stories."""
    product = _product(session)

    result = evaluate_scope_extension_preconditions(
        session=session,
        product_id=_product_id(product),
        workflow_state=_workflow_state(),
        sprint_candidate_count=1,
    )

    assert result.status == SCOPE_EXTENSION_BLOCKED
    assert result.available is False
    assert result.blocking_reason == "SPRINT_CANDIDATES_EXIST"


def test_scope_extension_preconditions_block_non_sprint_complete_state(
    session: Session,
) -> None:
    """Block extension unless the workflow FSM is SPRINT_COMPLETE."""
    product = _product(session)

    result = evaluate_scope_extension_preconditions(
        session=session,
        product_id=_product_id(product),
        workflow_state=_workflow_state("SPRINT_PLANNING"),
        sprint_candidate_count=0,
    )

    assert result.status == SCOPE_EXTENSION_BLOCKED
    assert result.available is False
    assert result.blocking_reason == "FSM_STATE_NOT_SPRINT_COMPLETE"


def test_scope_extension_preconditions_allow_terminal_stories(
    session: Session,
) -> None:
    """Treat accepted, done, superseded, and archived stories as terminal."""
    product = _product(session)
    _story(session, _product_id(product), status=StoryStatus.DONE)
    _story(session, _product_id(product), status=StoryStatus.ACCEPTED)
    _story(session, _product_id(product), is_superseded=True)
    _story(session, _product_id(product), archived_reason="scope_reset")

    result = evaluate_scope_extension_preconditions(
        session=session,
        product_id=_product_id(product),
        workflow_state=_workflow_state(),
        sprint_candidate_count=0,
    )

    assert result.status == SCOPE_EXTENSION_AVAILABLE
    assert result.available is True
    assert result.blocking_reason is None


def test_additive_scope_extension_accepts_new_source_item() -> None:
    """Accept an amended artifact that only adds a source item."""
    base = _artifact()
    amended = _with_new_item(base)

    result = validate_additive_scope_extension(base, amended)

    assert result.ok is True
    assert result.added_source_item_ids == ["REQ.new-capability"]
    assert result.blocking_issues == []


def test_additive_scope_extension_accepts_loaded_spec_artifacts(
    tmp_path: Path,
) -> None:
    """Accept loaded structured spec artifacts without caller-side dumping."""
    base_file = tmp_path / "base.json"
    amended_file = tmp_path / "amended.json"
    base_file.write_text(json.dumps(_artifact()), encoding="utf-8")
    amended_file.write_text(json.dumps(_with_new_item(_artifact())), encoding="utf-8")
    base_artifact, _, _ = load_structured_spec_file(str(base_file))
    amended_artifact, _, _ = load_structured_spec_file(str(amended_file))

    result = validate_additive_scope_extension(base_artifact, amended_artifact)

    assert result.ok is True
    assert result.added_source_item_ids == ["REQ.new-capability"]
    assert result.blocking_issues == []


def test_additive_scope_extension_accepts_mixed_raw_and_model_artifacts() -> None:
    """Compare raw and parsed artifacts without optional-default false positives."""
    base = _artifact()
    amended = TechnicalSpecArtifact.model_validate(_with_new_item(base))

    result = validate_additive_scope_extension(base, amended)

    assert result.ok is True
    assert result.added_source_item_ids == ["REQ.new-capability"]
    assert result.modified_source_item_ids == []
    assert result.blocking_issues == []


def test_additive_scope_extension_blocks_modified_existing_item() -> None:
    """Block amendments that change an existing source item."""
    base = _artifact()
    amended = _with_new_item(base)
    amended["items"][1]["statement"] = "The system MUST rewrite old scope."

    result = validate_additive_scope_extension(base, amended)

    assert result.ok is False
    assert "EXISTING_SOURCE_ITEM_MODIFIED" in _issue_codes(result.blocking_issues)
    assert result.modified_source_item_ids == ["REQ.existing-capability"]


def test_additive_scope_extension_blocks_removed_existing_item() -> None:
    """Block amendments that remove an existing source item."""
    base = _artifact()
    amended = _with_new_item(base)
    amended["items"] = [
        item for item in amended["items"] if item["id"] != "GOAL.existing"
    ]

    result = validate_additive_scope_extension(base, amended)

    assert result.ok is False
    assert "EXISTING_SOURCE_ITEM_REMOVED" in _issue_codes(result.blocking_issues)
    assert result.removed_source_item_ids == ["GOAL.existing"]


def test_additive_scope_extension_blocks_duplicate_base_source_item_id() -> None:
    """Block base artifacts with duplicate source item IDs."""
    base = _artifact()
    base["items"].append({**base["items"][1], "statement": "Duplicate scope."})
    amended = _with_new_item(base)

    result = validate_additive_scope_extension(base, amended)

    assert result.ok is False
    assert "DUPLICATE_SOURCE_ITEM_ID" in _issue_codes(result.blocking_issues)
    assert any(
        issue.source_item_id == "REQ.existing-capability"
        for issue in result.blocking_issues
        if issue.code == "DUPLICATE_SOURCE_ITEM_ID"
    )


def test_additive_scope_extension_blocks_duplicate_amended_source_item_id() -> None:
    """Block amended artifacts with duplicate source item IDs."""
    base = _artifact()
    amended = _with_new_item(base)
    amended["items"].append({**amended["items"][1], "statement": "Duplicate scope."})

    result = validate_additive_scope_extension(base, amended)

    assert result.ok is False
    assert "DUPLICATE_SOURCE_ITEM_ID" in _issue_codes(result.blocking_issues)
    assert any(
        issue.source_item_id == "REQ.existing-capability"
        for issue in result.blocking_issues
        if issue.code == "DUPLICATE_SOURCE_ITEM_ID"
    )


def test_additive_scope_extension_blocks_changed_existing_relation() -> None:
    """Block amendments that change an existing relation."""
    base = _artifact()
    amended = _with_new_item(base)
    amended["relations"][0]["rationale"] = "Changed rationale."

    result = validate_additive_scope_extension(base, amended)

    assert result.ok is False
    assert "EXISTING_RELATION_MODIFIED" in _issue_codes(result.blocking_issues)


def test_additive_scope_extension_blocks_removed_existing_relation() -> None:
    """Block amendments that remove an existing relation."""
    base = _artifact()
    amended = _with_new_item(base)
    amended["relations"] = []

    result = validate_additive_scope_extension(base, amended)

    assert result.ok is False
    assert "EXISTING_RELATION_REMOVED" in _issue_codes(result.blocking_issues)


def test_additive_scope_extension_blocks_duplicate_base_relation_key() -> None:
    """Block base artifacts with duplicate relation identity keys."""
    base = _artifact()
    base["relations"].append({**base["relations"][0], "rationale": "Duplicate."})
    amended = _with_new_item(base)

    result = validate_additive_scope_extension(base, amended)

    assert result.ok is False
    assert "DUPLICATE_RELATION_KEY" in _issue_codes(result.blocking_issues)


def test_additive_scope_extension_blocks_duplicate_amended_relation_key() -> None:
    """Block amended artifacts with duplicate relation identity keys."""
    base = _artifact()
    amended = _with_new_item(base)
    amended["relations"].append(
        {**amended["relations"][0], "rationale": "Duplicate."}
    )

    result = validate_additive_scope_extension(base, amended)

    assert result.ok is False
    assert "DUPLICATE_RELATION_KEY" in _issue_codes(result.blocking_issues)


def test_additive_scope_extension_blocks_no_new_items() -> None:
    """Block amendments that do not add at least one source item."""
    base = _artifact()

    result = validate_additive_scope_extension(base, deepcopy(base))

    assert result.ok is False
    assert "NO_ADDED_SOURCE_ITEMS" in _issue_codes(result.blocking_issues)


def test_runner_validate_returns_added_items_and_base_guard_info(
    session: Session,
    tmp_path: Path,
) -> None:
    """Validate amended spec against latest accepted base without mutation."""
    product = _product(session)
    base_spec = _accepted_base_spec(session, _product_id(product))
    amended_file = _write_spec_file(
        tmp_path,
        "amended.json",
        _with_new_item(_artifact()),
    )
    runner = ScopeExtensionRunner(
        session=session,
        workflow_service=_WorkflowServiceDouble(),
    )

    result = runner.validate(
        ScopeExtensionValidateRequest(
            project_id=_product_id(product),
            spec_file=str(amended_file),
            base_spec_version_id=base_spec.spec_version_id,
        )
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == SCOPE_EXTENSION_VALID
    assert data["valid"] is True
    assert data["project_id"] == _product_id(product)
    assert data["base_spec_version_id"] == base_spec.spec_version_id
    assert data["base_spec_hash"] == base_spec.spec_hash
    assert data["amended_spec_hash"]
    assert data["added_source_item_ids"] == ["REQ.new-capability"]
    assert data["removed_source_item_ids"] == []
    assert data["modified_source_item_ids"] == []
    assert data["blocking_issues"] == []


def test_runner_validate_blocks_mismatched_base_spec_version_id(
    session: Session,
    tmp_path: Path,
) -> None:
    """Reject stale base guards before loading amendment side effects."""
    mismatched_base_spec_version_id = 999_999
    product = _product(session)
    _accepted_base_spec(session, _product_id(product))
    amended_file = _write_spec_file(
        tmp_path,
        "amended.json",
        _with_new_item(_artifact()),
    )
    runner = ScopeExtensionRunner(
        session=session,
        workflow_service=_WorkflowServiceDouble(),
    )

    result = runner.validate(
        ScopeExtensionValidateRequest(
            project_id=_product_id(product),
            spec_file=str(amended_file),
            base_spec_version_id=mismatched_base_spec_version_id,
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == ERR_SCOPE_EXTENSION_BASE_SPEC_MISMATCH
    assert result["errors"][0]["message"] != "Command failed."
    assert (
        result["errors"][0]["details"]["expected_base_spec_version_id"]
        == mismatched_base_spec_version_id
    )


def test_runner_start_registers_pending_spec_and_routes_to_authority_compile(
    session: Session,
    tmp_path: Path,
) -> None:
    """Start extension by registering amended spec and requiring authority compile."""
    product = _product(session)
    base_spec = _accepted_base_spec(session, _product_id(product))
    amended_file = _write_spec_file(
        tmp_path,
        "amended.json",
        _with_new_item(_artifact()),
    )
    workflow = _WorkflowServiceDouble({"fsm_state": "SPRINT_COMPLETE"})
    runner = ScopeExtensionRunner(session=session, workflow_service=workflow)

    result = runner.start(
        ScopeExtensionStartRequest(
            project_id=_product_id(product),
            spec_file=str(amended_file),
            base_spec_version_id=base_spec.spec_version_id or 0,
            expected_state="SPRINT_COMPLETE",
            idempotency_key="scope-ext-1",
        )
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["status"] == SCOPE_EXTENSION_STARTED
    assert data["setup_status"] == "authority_compile_required"
    assert data["spec_version_id"] != base_spec.spec_version_id
    assert data["next_actions"][0]["command"] == "agileforge authority compile"
    assert data["next_actions"][0]["args"]["project_id"] == _product_id(product)
    pending = session.get(SpecRegistry, data["spec_version_id"])
    assert pending is not None
    assert pending.spec_hash == data["scope_extension_context"]["amended_spec_hash"]
    assert pending.content_ref == str(amended_file.resolve())
    marker = _recovery_marker_from_notes(pending.approval_notes)
    assert marker["base_spec_version_id"] == base_spec.spec_version_id
    assert marker["base_spec_hash"] == base_spec.spec_hash
    assert marker["added_source_item_ids"] == ["REQ.new-capability"]
    assert workflow.state["fsm_state"] == "SETUP_REQUIRED"
    assert workflow.state["setup_spec_version_id"] == data["spec_version_id"]
    assert workflow.state["setup_next_actions"] == data["next_actions"]
    assert workflow.state["scope_extension_context"] == data["scope_extension_context"]
    assert data["scope_extension_context"] == {
        "schema": "agileforge.scope_extension.v1",
        "base_spec_version_id": base_spec.spec_version_id,
        "base_spec_hash": base_spec.spec_hash,
        "amended_spec_version_id": data["spec_version_id"],
        "amended_spec_hash": pending.spec_hash,
        "added_source_item_ids": ["REQ.new-capability"],
        "idempotency_key": "scope-ext-1",
        "request_fingerprint": canonical_hash(
            {
                "request": ScopeExtensionStartRequest(
                    project_id=_product_id(product),
                    spec_file=str(amended_file),
                    base_spec_version_id=base_spec.spec_version_id or 0,
                    expected_state="SPRINT_COMPLETE",
                    idempotency_key="scope-ext-1",
                ).normalized_request_hash(),
                "amended_spec_hash": pending.spec_hash,
            }
        ),
        "spec_file": str(amended_file.resolve()),
    }


def test_runner_start_consumes_accepted_spec_amendment_and_preserves_provenance(
    session: Session,
    tmp_path: Path,
) -> None:
    """Accepted discovery amendments can start scope extension without raw spec args."""
    product = _product(session)
    product_id = _product_id(product)
    base_spec = _accepted_base_spec(session, product_id)
    amended_file = _write_spec_file(
        tmp_path,
        "accepted-amendment.json",
        _with_new_item(_artifact()),
    )
    amendment = _accepted_discovery_spec_amendment(
        session,
        product_id,
        amendment_file=amended_file,
        base_spec=base_spec,
    )
    workflow = _WorkflowServiceDouble({"fsm_state": "SPRINT_COMPLETE"})
    runner = ScopeExtensionRunner(session=session, workflow_service=workflow)

    result = runner.start(
        ScopeExtensionStartRequest(
            project_id=product_id,
            spec_amendment_draft_id=amendment.spec_amendment_draft_id,
            expected_state="SPRINT_COMPLETE",
            idempotency_key="scope-ext-accepted-amendment",
        )
    )

    assert result["ok"] is True
    context = result["data"]["scope_extension_context"]
    assert context["spec_amendment_draft_id"] == amendment.spec_amendment_draft_id
    assert context["prd_id"] == amendment.prd_id
    assert context["challenge_artifact_id"] == amendment.challenge_artifact_id
    assert context["spec_file"] == str(amended_file.resolve())
    assert workflow.state["setup_status"] == "authority_compile_required"


def test_runner_start_rejects_spec_amendment_that_is_not_accepted(
    session: Session,
    tmp_path: Path,
) -> None:
    """Validated-but-unaccepted amendments cannot bypass human acceptance."""
    product = _product(session)
    product_id = _product_id(product)
    base_spec = _accepted_base_spec(session, product_id)
    amended_file = _write_spec_file(
        tmp_path,
        "ready-amendment.json",
        _with_new_item(_artifact()),
    )
    amendment = _accepted_discovery_spec_amendment(
        session,
        product_id,
        amendment_file=amended_file,
        base_spec=base_spec,
        status="ready_for_amendment_acceptance",
    )
    workflow = _WorkflowServiceDouble({"fsm_state": "SPRINT_COMPLETE"})
    runner = ScopeExtensionRunner(session=session, workflow_service=workflow)

    result = runner.start(
        ScopeExtensionStartRequest(
            project_id=product_id,
            spec_amendment_draft_id=amendment.spec_amendment_draft_id,
            expected_state="SPRINT_COMPLETE",
            idempotency_key="scope-ext-ready-amendment",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == ErrorCode.SPEC_AMENDMENT_NOT_ACCEPTED.value
    assert workflow.updates == []
    assert len(_spec_rows(session, product_id)) == 1


def test_runner_start_blocks_active_sprint_without_pending_spec_registration(
    session: Session,
    tmp_path: Path,
) -> None:
    """Reject start while an active Sprint means execution scope is not exhausted."""
    product = _product(session)
    base_spec = _accepted_base_spec(session, _product_id(product))
    _sprint(session, _product_id(product), status=SprintStatus.ACTIVE)
    amended_file = _write_spec_file(
        tmp_path,
        "amended.json",
        _with_new_item(_artifact()),
    )
    workflow = _WorkflowServiceDouble({"fsm_state": "SPRINT_COMPLETE"})
    runner = ScopeExtensionRunner(session=session, workflow_service=workflow)

    result = runner.start(
        ScopeExtensionStartRequest(
            project_id=_product_id(product),
            spec_file=str(amended_file),
            base_spec_version_id=base_spec.spec_version_id or 0,
            expected_state="SPRINT_COMPLETE",
            idempotency_key="scope-ext-active-sprint",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == ERR_SCOPE_EXTENSION_NOT_AVAILABLE
    assert result["errors"][0]["details"]["blocking_reason"] == "ACTIVE_SPRINT_EXISTS"
    assert workflow.updates == []
    spec_version_ids = [
        spec.spec_version_id for spec in _spec_rows(session, _product_id(product))
    ]
    assert spec_version_ids == [base_spec.spec_version_id]


def test_runner_start_blocks_open_story_without_pending_spec_registration(
    session: Session,
    tmp_path: Path,
) -> None:
    """Reject start while unresolved story work still exists."""
    product = _product(session)
    base_spec = _accepted_base_spec(session, _product_id(product))
    _story(session, _product_id(product), status=StoryStatus.IN_PROGRESS)
    amended_file = _write_spec_file(
        tmp_path,
        "amended.json",
        _with_new_item(_artifact()),
    )
    workflow = _WorkflowServiceDouble({"fsm_state": "SPRINT_COMPLETE"})
    runner = ScopeExtensionRunner(session=session, workflow_service=workflow)

    result = runner.start(
        ScopeExtensionStartRequest(
            project_id=_product_id(product),
            spec_file=str(amended_file),
            base_spec_version_id=base_spec.spec_version_id or 0,
            expected_state="SPRINT_COMPLETE",
            idempotency_key="scope-ext-open-story",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == ERR_SCOPE_EXTENSION_UNRESOLVED_WORK
    assert result["errors"][0]["details"]["blocking_reason"] == "OPEN_STORY_EXISTS"
    assert result["errors"][0]["remediation"] == [
        (
            "Reconcile each open Story first, for example: agileforge story "
            "reconcile --project-id <project_id> --story-id <story_id> "
            "--action archive --reason <reason> --idempotency-key <key>."
        )
    ]
    assert workflow.updates == []
    spec_version_ids = [
        spec.spec_version_id for spec in _spec_rows(session, _product_id(product))
    ]
    assert spec_version_ids == [base_spec.spec_version_id]


def test_runner_start_blocks_remaining_candidates_without_pending_spec_registration(
    session: Session,
    tmp_path: Path,
) -> None:
    """Reject start when injected planning candidate count says scope remains."""
    product = _product(session)
    base_spec = _accepted_base_spec(session, _product_id(product))
    amended_file = _write_spec_file(
        tmp_path,
        "amended.json",
        _with_new_item(_artifact()),
    )
    workflow = _WorkflowServiceDouble({"fsm_state": "SPRINT_COMPLETE"})
    runner = ScopeExtensionRunner(
        session=session,
        workflow_service=workflow,
        sprint_candidate_count_resolver=lambda _project_id: 1,
    )

    result = runner.start(
        ScopeExtensionStartRequest(
            project_id=_product_id(product),
            spec_file=str(amended_file),
            base_spec_version_id=base_spec.spec_version_id or 0,
            expected_state="SPRINT_COMPLETE",
            idempotency_key="scope-ext-candidates",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == ERR_SCOPE_EXTENSION_UNRESOLVED_WORK
    assert (
        result["errors"][0]["details"]["blocking_reason"]
        == "SPRINT_CANDIDATES_EXIST"
    )
    assert workflow.updates == []
    spec_version_ids = [
        spec.spec_version_id for spec in _spec_rows(session, _product_id(product))
    ]
    assert spec_version_ids == [base_spec.spec_version_id]


def test_runner_start_replays_same_idempotency_key_without_mutation(
    session: Session,
    tmp_path: Path,
) -> None:
    """Replay successful start when request identity matches stored context."""
    product = _product(session)
    base_spec = _accepted_base_spec(session, _product_id(product))
    amended_file = _write_spec_file(
        tmp_path,
        "amended.json",
        _with_new_item(_artifact()),
    )
    workflow = _WorkflowServiceDouble({"fsm_state": "SPRINT_COMPLETE"})
    runner = ScopeExtensionRunner(session=session, workflow_service=workflow)
    request = ScopeExtensionStartRequest(
        project_id=_product_id(product),
        spec_file=str(amended_file),
        base_spec_version_id=base_spec.spec_version_id or 0,
        expected_state="SPRINT_COMPLETE",
        idempotency_key="scope-ext-replay",
    )
    first = runner.start(request)
    spec_count = len(_spec_rows(session, _product_id(product)))
    update_count = len(workflow.updates)

    replay = runner.start(request)

    assert replay["ok"] is True
    assert replay["data"] == first["data"]
    assert len(_spec_rows(session, _product_id(product))) == spec_count
    assert len(workflow.updates) == update_count


def test_runner_start_rejects_reused_idempotency_key_with_changed_request(
    session: Session,
    tmp_path: Path,
) -> None:
    """Reject same idempotency key when request fingerprint differs."""
    product = _product(session)
    base_spec = _accepted_base_spec(session, _product_id(product))
    first_file = _write_spec_file(
        tmp_path,
        "amended.json",
        _with_new_item(_artifact()),
    )
    changed = _with_new_item(_artifact())
    changed["items"][-1]["id"] = "REQ.different-capability"
    second_file = _write_spec_file(tmp_path, "amended-2.json", changed)
    workflow = _WorkflowServiceDouble({"fsm_state": "SPRINT_COMPLETE"})
    runner = ScopeExtensionRunner(session=session, workflow_service=workflow)
    first = runner.start(
        ScopeExtensionStartRequest(
            project_id=_product_id(product),
            spec_file=str(first_file),
            base_spec_version_id=base_spec.spec_version_id or 0,
            expected_state="SPRINT_COMPLETE",
            idempotency_key="scope-ext-conflict",
        )
    )
    spec_count = len(_spec_rows(session, _product_id(product)))
    update_count = len(workflow.updates)

    conflict = runner.start(
        ScopeExtensionStartRequest(
            project_id=_product_id(product),
            spec_file=str(second_file),
            base_spec_version_id=base_spec.spec_version_id or 0,
            expected_state="SPRINT_COMPLETE",
            idempotency_key="scope-ext-conflict",
        )
    )

    assert first["ok"] is True
    assert conflict["ok"] is False
    assert conflict["errors"][0]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert conflict["errors"][0]["message"] != "Command failed."
    assert len(_spec_rows(session, _product_id(product))) == spec_count
    assert len(workflow.updates) == update_count


def test_runner_start_rejects_reused_idempotency_key_when_same_path_content_changes(
    session: Session,
    tmp_path: Path,
) -> None:
    """Reject same idempotency key when amended spec content changes in place."""
    product = _product(session)
    base_spec = _accepted_base_spec(session, _product_id(product))
    amended_file = _write_spec_file(
        tmp_path,
        "amended.json",
        _with_new_item(_artifact()),
    )
    workflow = _WorkflowServiceDouble({"fsm_state": "SPRINT_COMPLETE"})
    runner = ScopeExtensionRunner(session=session, workflow_service=workflow)
    request = ScopeExtensionStartRequest(
        project_id=_product_id(product),
        spec_file=str(amended_file),
        base_spec_version_id=base_spec.spec_version_id or 0,
        expected_state="SPRINT_COMPLETE",
        idempotency_key="scope-ext-same-path-conflict",
    )
    first = runner.start(request)
    spec_count = len(_spec_rows(session, _product_id(product)))
    update_count = len(workflow.updates)
    changed = _with_new_item(_artifact())
    changed["items"][-1]["id"] = "REQ.same-path-different-capability"
    changed["items"][-1]["title"] = "Different capability"
    amended_file.write_text(json.dumps(changed), encoding="utf-8")

    conflict = runner.start(request)

    assert first["ok"] is True
    assert conflict["ok"] is False
    assert conflict["errors"][0]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert len(_spec_rows(session, _product_id(product))) == spec_count
    assert len(workflow.updates) == update_count


def test_runner_start_rejects_stale_expected_state_without_workflow_mutation(
    session: Session,
    tmp_path: Path,
) -> None:
    """Reject stale workflow guards before pending spec registration."""
    product = _product(session)
    base_spec = _accepted_base_spec(session, _product_id(product))
    amended_file = _write_spec_file(
        tmp_path,
        "amended.json",
        _with_new_item(_artifact()),
    )
    workflow = _WorkflowServiceDouble({"fsm_state": "ROADMAP_READY"})
    runner = ScopeExtensionRunner(session=session, workflow_service=workflow)

    result = runner.start(
        ScopeExtensionStartRequest(
            project_id=_product_id(product),
            spec_file=str(amended_file),
            base_spec_version_id=base_spec.spec_version_id or 0,
            expected_state="SPRINT_COMPLETE",
            idempotency_key="scope-ext-stale",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "STALE_STATE"
    assert workflow.updates == []
    specs = session.exec(
        select(SpecRegistry).where(SpecRegistry.product_id == _product_id(product))
    ).all()
    assert [spec.spec_version_id for spec in specs] == [base_spec.spec_version_id]


def test_runner_start_does_not_mutate_workflow_when_additive_validation_fails(
    session: Session,
    tmp_path: Path,
) -> None:
    """Return invalid validation payload without starting setup."""
    product = _product(session)
    base_spec = _accepted_base_spec(session, _product_id(product))
    invalid_file = _write_spec_file(tmp_path, "invalid.json", deepcopy(_artifact()))
    workflow = _WorkflowServiceDouble({"fsm_state": "SPRINT_COMPLETE"})
    runner = ScopeExtensionRunner(session=session, workflow_service=workflow)

    result = runner.start(
        ScopeExtensionStartRequest(
            project_id=_product_id(product),
            spec_file=str(invalid_file),
            base_spec_version_id=base_spec.spec_version_id or 0,
            expected_state="SPRINT_COMPLETE",
            idempotency_key="scope-ext-invalid",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == ERR_SCOPE_EXTENSION_NO_ADDED_ITEMS
    assert workflow.updates == []
    spec_version_ids = [
        spec.spec_version_id for spec in _spec_rows(session, _product_id(product))
    ]
    assert spec_version_ids == [base_spec.spec_version_id]


def test_runner_start_recovers_after_pending_spec_registered_before_workflow_update(
    session: Session,
    tmp_path: Path,
) -> None:
    """Allow retry to finish setup when first workflow update fails after DB write."""
    product = _product(session)
    base_spec = _accepted_base_spec(session, _product_id(product))
    amended_file = _write_spec_file(
        tmp_path,
        "amended.json",
        _with_new_item(_artifact()),
    )
    workflow = _FailOnceWorkflowServiceDouble({"fsm_state": "SPRINT_COMPLETE"})
    runner = ScopeExtensionRunner(session=session, workflow_service=workflow)
    request = ScopeExtensionStartRequest(
        project_id=_product_id(product),
        spec_file=str(amended_file),
        base_spec_version_id=base_spec.spec_version_id or 0,
        expected_state="SPRINT_COMPLETE",
        idempotency_key="scope-ext-recover",
    )

    first = runner.start(request)
    rows_after_failure = _spec_rows(session, _product_id(product))
    retry = runner.start(request)
    registered_spec_count = 2

    assert first["ok"] is False
    assert first["errors"][0]["code"] == "MUTATION_RECOVERY_REQUIRED"
    assert len(rows_after_failure) == registered_spec_count
    assert retry["ok"] is True
    assert retry["data"]["status"] == SCOPE_EXTENSION_STARTED
    assert len(_spec_rows(session, _product_id(product))) == registered_spec_count
    assert workflow.state["fsm_state"] == "SETUP_REQUIRED"
    assert workflow.state["scope_extension_context"]["idempotency_key"] == (
        "scope-ext-recover"
    )


def test_runner_start_rejects_changed_retry_after_pending_spec_recovery_marker(
    session: Session,
    tmp_path: Path,
) -> None:
    """Reject changed same-key retry after failed workflow update."""
    product = _product(session)
    base_spec = _accepted_base_spec(session, _product_id(product))
    amended_file = _write_spec_file(
        tmp_path,
        "amended.json",
        _with_new_item(_artifact()),
    )
    workflow = _FailOnceWorkflowServiceDouble({"fsm_state": "SPRINT_COMPLETE"})
    runner = ScopeExtensionRunner(session=session, workflow_service=workflow)
    request = ScopeExtensionStartRequest(
        project_id=_product_id(product),
        spec_file=str(amended_file),
        base_spec_version_id=base_spec.spec_version_id or 0,
        expected_state="SPRINT_COMPLETE",
        idempotency_key="scope-ext-recover-conflict",
    )
    first = runner.start(request)
    rows_after_failure = _spec_rows(session, _product_id(product))
    registered_spec_count = 2
    changed = _with_new_item(_artifact())
    changed["items"][-1]["id"] = "REQ.recovery-different-capability"
    changed["items"][-1]["title"] = "Recovery different capability"
    amended_file.write_text(json.dumps(changed), encoding="utf-8")

    retry = runner.start(request)

    assert first["ok"] is False
    assert first["errors"][0]["code"] == "MUTATION_RECOVERY_REQUIRED"
    assert retry["ok"] is False
    assert retry["errors"][0]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert workflow.updates == []
    assert len(rows_after_failure) == registered_spec_count
    assert len(_spec_rows(session, _product_id(product))) == len(rows_after_failure)


def test_runner_start_rejects_changed_path_retry_after_pending_spec_marker(
    session: Session,
    tmp_path: Path,
) -> None:
    """Reject same-key retry with a different path after failed workflow update."""
    product = _product(session)
    base_spec = _accepted_base_spec(session, _product_id(product))
    first_file = _write_spec_file(
        tmp_path,
        "amended.json",
        _with_new_item(_artifact()),
    )
    changed = _with_new_item(_artifact())
    changed["items"][-1]["id"] = "REQ.recovery-path-different-capability"
    changed["items"][-1]["title"] = "Recovery path different capability"
    second_file = _write_spec_file(tmp_path, "amended-2.json", changed)
    workflow = _FailOnceWorkflowServiceDouble({"fsm_state": "SPRINT_COMPLETE"})
    runner = ScopeExtensionRunner(session=session, workflow_service=workflow)
    first = runner.start(
        ScopeExtensionStartRequest(
            project_id=_product_id(product),
            spec_file=str(first_file),
            base_spec_version_id=base_spec.spec_version_id or 0,
            expected_state="SPRINT_COMPLETE",
            idempotency_key="scope-ext-recover-path-conflict",
        )
    )
    rows_after_failure = _spec_rows(session, _product_id(product))

    retry = runner.start(
        ScopeExtensionStartRequest(
            project_id=_product_id(product),
            spec_file=str(second_file),
            base_spec_version_id=base_spec.spec_version_id or 0,
            expected_state="SPRINT_COMPLETE",
            idempotency_key="scope-ext-recover-path-conflict",
        )
    )

    assert first["ok"] is False
    assert first["errors"][0]["code"] == "MUTATION_RECOVERY_REQUIRED"
    assert retry["ok"] is False
    assert retry["errors"][0]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert workflow.updates == []
    assert len(_spec_rows(session, _product_id(product))) == len(rows_after_failure)
    assert all(
        row.content_ref != str(second_file.resolve()) for row in rows_after_failure
    )


def test_runner_start_invalid_modified_scope_uses_not_additive_error(
    session: Session,
    tmp_path: Path,
) -> None:
    """Return a failed mutation envelope for non-additive amended specs."""
    product = _product(session)
    base_spec = _accepted_base_spec(session, _product_id(product))
    invalid = _with_new_item(_artifact())
    invalid["items"][1]["statement"] = "The system MUST rewrite old scope."
    invalid_file = _write_spec_file(tmp_path, "invalid-modified.json", invalid)
    workflow = _WorkflowServiceDouble({"fsm_state": "SPRINT_COMPLETE"})
    runner = ScopeExtensionRunner(session=session, workflow_service=workflow)

    result = runner.start(
        ScopeExtensionStartRequest(
            project_id=_product_id(product),
            spec_file=str(invalid_file),
            base_spec_version_id=base_spec.spec_version_id or 0,
            expected_state="SPRINT_COMPLETE",
            idempotency_key="scope-ext-invalid-modified",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == ERR_SCOPE_EXTENSION_NOT_ADDITIVE
    assert workflow.updates == []
    spec_version_ids = [
        spec.spec_version_id for spec in _spec_rows(session, _product_id(product))
    ]
    assert spec_version_ids == [base_spec.spec_version_id]
