"""Exact lineage, fingerprint, readiness, and replacement validation tests."""
# ruff: noqa: D103

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlmodel import Session

from models.core import UserStory
from services.contracts.specification_validation import StorySpecificationReviewInput
from services.contracts.story import CanonicalStoryOutput
from services.specs.story_validation_service import (
    StoryValidationReadinessError,
    require_story_ready_for_sprint,
)
from tests.test_create_user_story import (
    _decide_story,
    _record_story,
    _seed_story_parent,
    _story_content,
)
from tests.test_story_validation_service import _accepted_story, _validate
from tests.workflow.test_planning_transitions import (
    _replace_specification_and_backlog,
)
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def test_gold_specification_fixture_contains_exact_37_item_direct_contract() -> None:
    payload = json.loads(
        Path("tests/fixtures/issue_210/gold/canonical-specification.json").read_text()
    )
    item_ids = tuple(item["id"] for item in payload["items"])
    assert payload["schema_version"] == "agileforge.spec.v2"
    assert len(item_ids) == 37  # noqa: PLR2004
    assert len(set(item_ids)) == 37  # noqa: PLR2004
    assert "DATA.001" in item_ids
    story = CanonicalStoryOutput.model_validate(_story_content()).story_items[0].item
    review_input = StorySpecificationReviewInput(
        schema_version="agileforge.story-specification-review-input.v1",
        accepted_specification_version_id=1,
        accepted_specification_hash=canonical_hash(payload),
        accepted_specification_json=canonical_json(payload),
        parent_backlog_item_id="PBI-000001",
        parent_backlog_spec_item_ids=("DATA.001",),
        story=story.model_copy(update={"spec_item_ids": ("DATA.001",)}),
    )
    encoded = review_input.model_dump(mode="json")
    assert tuple(encoded).count("accepted_specification_json") == 1
    assert len(json.loads(review_input.accepted_specification_json)["items"]) == 37  # noqa: PLR2004


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("project_id", 2),
        ("story_id", 3),
        ("source_story_artifact_id", 5),
        ("source_story_artifact_fingerprint", "sha256:" + "a" * 64),
        ("source_story_item_id", "US-0002"),
        ("source_story_item_fingerprint", "sha256:" + "b" * 64),
        ("source_backlog_artifact_id", 7),
        ("source_backlog_artifact_fingerprint", "sha256:" + "c" * 64),
        ("source_backlog_item_id", "PBI-000002"),
        ("spec_version_id", 11),
        ("spec_hash", "sha256:" + "d" * 64),
        ("spec_item_ids", ["DATA.002"]),
        ("title", "Changed title"),
        ("statement", "As a user I want another result so that it changes."),
        ("persona", "user"),
        ("acceptance_criteria", ["Changed criterion"]),
        ("story_points", 8),
        ("rank", "999"),
    ],
)
def test_each_closed_validation_input_member_changes_fingerprint(
    field: str,
    replacement: object,
) -> None:
    payload: dict[str, object] = {
        "schema_version": "agileforge.story-validation-input.v1",
        "project_id": 1,
        "story_id": 2,
        "source_story_artifact_id": 4,
        "source_story_artifact_fingerprint": "sha256:" + "1" * 64,
        "source_story_item_id": "US-0001",
        "source_story_item_fingerprint": "sha256:" + "2" * 64,
        "source_backlog_artifact_id": 6,
        "source_backlog_artifact_fingerprint": "sha256:" + "3" * 64,
        "source_backlog_item_id": "PBI-000001",
        "spec_version_id": 10,
        "spec_hash": "sha256:" + "4" * 64,
        "spec_item_ids": ["DATA.001"],
        "title": "Title",
        "statement": "As an operator I want a result so that it is useful.",
        "persona": "operator",
        "acceptance_criteria": ["Criterion"],
        "story_points": 3,
        "rank": "101",
    }
    original = canonical_hash(payload)
    payload[field] = replacement
    assert canonical_hash(payload) != original


@pytest.mark.parametrize(
    ("field", "replacement"), [("story_points", 8), ("rank", "999")]
)
def test_points_or_rank_change_stales_previous_evidence(
    engine: Engine,
    field: str,
    replacement: object,
) -> None:
    story_id = _accepted_story(engine)
    _validate(engine, story_id)
    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        setattr(story, field, replacement)
        session.commit()
        with pytest.raises(StoryValidationReadinessError, match="failed or stale"):
            require_story_ready_for_sprint(session, story=story)


@pytest.mark.parametrize(
    "raw_evidence",
    [
        None,
        "not-json",
        '{"spec_version_id":1,"passed":true}',
    ],
)
def test_readiness_rejects_missing_malformed_or_v1_evidence(
    engine: Engine,
    raw_evidence: str | None,
) -> None:
    story_id = _accepted_story(engine)
    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        story.validation_evidence = raw_evidence
        with pytest.raises(StoryValidationReadinessError):
            require_story_ready_for_sprint(session, story=story)


def test_validation_allows_exact_historical_pin_but_new_sprint_readiness_does_not(
    engine: Engine,
) -> None:
    story_id = _accepted_story(engine)
    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        project_id = story.project_id
    _replace_specification_and_backlog(engine, project_id)

    result = _validate(engine, story_id)
    assert result["ready_for_sprint"] is True
    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        with pytest.raises(StoryValidationReadinessError, match="current"):
            require_story_ready_for_sprint(session, story=story)


def test_feedback_preserves_a_evidence_and_accepted_c_starts_unvalidated(
    engine: Engine,
) -> None:
    project_id, roadmap_id = _seed_story_parent(engine)
    with Session(engine) as session:
        artifact_a = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
            title="Accepted A",
        )
        accepted_a = _decide_story(session, artifact_a, decision="accepted", offset=2)
        artifact_a_id = artifact_a.story_artifact_id
        session.commit()
        story_a_id = accepted_a.activated_story_ids[0]
    _validate(engine, story_a_id)
    with Session(engine) as session:
        story_a = session.get(UserStory, story_a_id)
        assert story_a is not None
        assert story_a.validation_evidence is not None
        evidence_a = story_a.validation_evidence
        artifact_b = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
            title="Feedback B",
            supersedes_id=artifact_a_id,
            recorded_offset=3,
        )
        _decide_story(session, artifact_b, decision="feedback", offset=4)
        assert story_a.validation_evidence == evidence_a
        artifact_c = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
            title="Accepted C",
            supersedes_id=artifact_b.story_artifact_id,
            recorded_offset=5,
        )
        accepted_c = _decide_story(session, artifact_c, decision="accepted", offset=6)
        story_c = session.get(UserStory, accepted_c.activated_story_ids[0])
        assert story_c is not None
        assert story_c.validation_evidence is None
        assert story_a.validation_evidence == evidence_a
        with pytest.raises(StoryValidationReadinessError):
            require_story_ready_for_sprint(session, story=story_c)
