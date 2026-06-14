"""Tests for project scope extension validation."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from models.core import Product, Sprint, Team, UserStory
from models.enums import SprintStatus, StoryStatus
from services.agent_workbench.scope_extension import (
    SCOPE_EXTENSION_AVAILABLE,
    SCOPE_EXTENSION_BLOCKED,
    ScopeExtensionIssue,
    evaluate_scope_extension_preconditions,
    load_structured_spec_file,
    validate_additive_scope_extension,
)
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


def _product(session: Session) -> Product:
    product = Product(name="Scope Extension Product")
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


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


def test_scope_extension_preconditions_available_when_sprint_complete_and_no_open_work(
    session: Session,
) -> None:
    """Allow extension only after completed workflow state with exhausted scope."""
    product = _product(session)

    result = evaluate_scope_extension_preconditions(
        session=session,
        product_id=product.product_id,
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
    _sprint(session, product.product_id, status=SprintStatus.ACTIVE)

    result = evaluate_scope_extension_preconditions(
        session=session,
        product_id=product.product_id,
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
    _sprint(session, product.product_id, status=SprintStatus.PLANNED)

    result = evaluate_scope_extension_preconditions(
        session=session,
        product_id=product.product_id,
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
    _story(session, product.product_id, status=StoryStatus.IN_PROGRESS)

    result = evaluate_scope_extension_preconditions(
        session=session,
        product_id=product.product_id,
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
        product_id=product.product_id,
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
        product_id=product.product_id,
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
    _story(session, product.product_id, status=StoryStatus.DONE)
    _story(session, product.product_id, status=StoryStatus.ACCEPTED)
    _story(session, product.product_id, is_superseded=True)
    _story(session, product.product_id, archived_reason="scope_reset")

    result = evaluate_scope_extension_preconditions(
        session=session,
        product_id=product.product_id,
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
