"""Tests for project scope extension validation."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from services.agent_workbench.scope_extension import (
    ScopeExtensionIssue,
    load_structured_spec_file,
    validate_additive_scope_extension,
)
from utils.agileforge_spec_profile import TechnicalSpecArtifact

if TYPE_CHECKING:
    from pathlib import Path


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
