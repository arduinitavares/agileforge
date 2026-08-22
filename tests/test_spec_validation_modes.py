"""Provider-free contract tests for direct-Specification Story validation."""
# ruff: noqa: D103

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from models.core import UserStory
from services.contracts.specification_validation import (
    StorySpecificationFinding,
    StorySpecificationReviewInput,
    StorySpecificationReviewOutput,
)
from services.specs import story_validation_service
from services.specs.story_validation_service import (
    StorySemanticReview,
    ValidateStoryInput,
    require_story_ready_for_sprint,
)
from tests.test_story_validation_service import _accepted_story, _validate
from workflow.fingerprints import canonical_json

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine


def _hybrid(
    engine: Engine,
    story_id: int,
    semantic_review: StorySemanticReview,
) -> dict[str, Any]:
    with patch.object(story_validation_service, "get_engine", return_value=engine):
        return cast(
            "dict[str, Any]",
            story_validation_service.validate_story_with_specification(
                {"story_id": story_id, "mode": "hybrid"},
                semantic_review=semantic_review,
                now=lambda: datetime(2026, 8, 21, 13, tzinfo=UTC),
            ),
        )


def test_validate_story_input_has_safe_structural_default_and_closed_modes() -> None:
    assert ValidateStoryInput(story_id=7).model_dump() == {
        "story_id": 7,
        "mode": "structural",
    }
    assert ValidateStoryInput(story_id=7, mode="hybrid").mode == "hybrid"

    for invalid in (0, -1):
        with pytest.raises(ValidationError):
            ValidateStoryInput(story_id=invalid)
    for invalid_mode in ("deterministic", "llm", "provider"):
        with pytest.raises(ValidationError):
            ValidateStoryInput.model_validate({"story_id": 7, "mode": invalid_mode})


def test_semantic_output_rejects_incomplete_or_contradictory_contract() -> None:
    valid = {
        "schema_version": "agileforge.story-specification-review.v1",
        "compliant": True,
        "complete": True,
        "findings": [],
    }
    assert StorySpecificationReviewOutput.model_validate(valid).findings == ()

    for mutation in (
        {**valid, "complete": False},
        {**valid, "compliant": False},
        {**valid, "unexpected": True},
    ):
        with pytest.raises(ValidationError):
            StorySpecificationReviewOutput.model_validate(mutation)


def test_semantic_output_caps_and_deduplicates_closed_findings() -> None:
    finding = StorySpecificationFinding(
        code="SPEC_ITEM_CONTRADICTION",
        spec_item_id="DATA.001",
        message="The Story contradicts the data contract.",
    ).model_dump(mode="json")
    base = {
        "schema_version": "agileforge.story-specification-review.v1",
        "compliant": False,
        "complete": True,
    }

    with pytest.raises(ValidationError):
        StorySpecificationReviewOutput.model_validate(
            {**base, "findings": [finding] * 2}
        )
    with pytest.raises(ValidationError):
        StorySpecificationReviewOutput.model_validate(
            {
                **base,
                "findings": [
                    {**finding, "spec_item_id": f"REQ.{ordinal:03d}"}
                    for ordinal in range(51)
                ],
            }
        )

    raw = json.dumps({**base, "findings": [finding]})
    parsed = StorySpecificationReviewOutput.model_validate_json(raw, strict=True)
    assert parsed.findings[0].spec_item_id == "DATA.001"
    boundary = StorySpecificationReviewOutput.model_validate(
        {
            **base,
            "findings": [
                {**finding, "spec_item_id": f"REQ.{ordinal:03d}"}
                for ordinal in range(50)
            ],
        }
    )
    assert len(boundary.findings) == 50  # noqa: PLR2004


def test_hybrid_invokes_one_injected_adapter_with_one_exact_specification_root(
    engine: Engine,
) -> None:
    story_id = _accepted_story(engine)
    payloads: list[StorySpecificationReviewInput] = []

    def review(payload: StorySpecificationReviewInput) -> str:
        payloads.append(payload)
        return canonical_json(
            {
                "schema_version": "agileforge.story-specification-review.v1",
                "compliant": True,
                "complete": True,
                "findings": [],
            }
        )

    result = _hybrid(engine, story_id, review)
    assert result["ready_for_sprint"] is True
    assert result["semantic_review_state"] == "valid"
    assert len(payloads) == 1
    dumped = payloads[0].model_dump(mode="json")
    assert tuple(dumped) == (
        "schema_version",
        "accepted_specification_version_id",
        "accepted_specification_hash",
        "accepted_specification_json",
        "parent_backlog_item_id",
        "parent_backlog_spec_item_ids",
        "story",
    )
    assert "authority" not in json.dumps(dumped).casefold()
    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        evidence = require_story_ready_for_sprint(session, story=story)
        assert evidence.semantic_review_state == "valid"


def test_hybrid_source_change_during_callback_preserves_prior_evidence(
    tmp_path: Path,
) -> None:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'task-9-hybrid-race.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    try:
        story_id = _accepted_story(engine)
        _validate(engine, story_id)
        with Session(engine) as session:
            story = session.get(UserStory, story_id)
            assert story is not None
            prior_evidence = story.validation_evidence
            assert prior_evidence is not None
        calls = 0
        statements: list[str] = []

        def record_statement(
            _connection: object,
            _cursor: object,
            statement: str,
            *_args: object,
        ) -> None:
            statements.append(statement)

        def review(_payload: StorySpecificationReviewInput) -> str:
            nonlocal calls
            calls += 1
            assert "BEGIN IMMEDIATE" not in statements
            with Session(engine) as concurrent_session:
                concurrent_story = concurrent_session.get(UserStory, story_id)
                assert concurrent_story is not None
                assert concurrent_story.story_points is not None
                concurrent_story.story_points += 1
                concurrent_session.add(concurrent_story)
                concurrent_session.commit()
            return canonical_json(
                {
                    "schema_version": "agileforge.story-specification-review.v1",
                    "compliant": False,
                    "complete": True,
                    "findings": [
                        {
                            "code": "SPEC_ITEM_CONTRADICTION",
                            "spec_item_id": "REQ.planning-1",
                            "message": "This old review must not be applied.",
                            "suggested_change": None,
                        }
                    ],
                }
            )

        event.listen(engine, "before_cursor_execute", record_statement)
        try:
            result = _hybrid(engine, story_id, review)
        finally:
            event.remove(engine, "before_cursor_execute", record_statement)
        assert calls == 1
        assert statements.count("BEGIN IMMEDIATE") == 1
        assert result == {
            "success": False,
            "error_code": "STORY_VALIDATION_SOURCE_STALE",
            "message": "Story validation source changed before evidence persistence.",
            "story_id": story_id,
            "mode": "hybrid",
            "ready_for_sprint": False,
        }
        with Session(engine) as session:
            story = session.get(UserStory, story_id)
            assert story is not None
            assert story.validation_evidence == prior_evidence
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "response",
    [
        "not json",
        "```json\n{}\n```",
        '{"schema_version":"agileforge.story-specification-review.v1"',
        canonical_json(
            {
                "schema_version": "agileforge.story-specification-review.v1",
                "compliant": True,
                "complete": False,
                "findings": [],
            }
        ),
        canonical_json(
            {
                "schema_version": "agileforge.story-specification-review.v1",
                "compliant": True,
                "complete": True,
                "findings": [
                    {
                        "code": "SPEC_ITEM_OMISSION",
                        "spec_item_id": "REQ.unknown",
                        "message": "Out of bounds.",
                        "suggested_change": None,
                    }
                ],
            }
        ),
    ],
)
def test_malformed_or_out_of_bound_hybrid_response_is_one_call_and_no_repair(
    engine: Engine,
    response: str,
) -> None:
    story_id = _accepted_story(engine)
    calls = 0

    def review(_payload: object) -> str:
        nonlocal calls
        calls += 1
        return response

    result = _hybrid(engine, story_id, review)
    assert calls == 1
    assert result["ready_for_sprint"] is False
    assert result["semantic_review_state"] == "invalid"
    assert result["semantic_findings"] == []
    assert result["semantic_error"] == "STORY_SPECIFICATION_REVIEW_INVALID"


def test_semantic_findings_are_blocking_and_sorted_by_item_and_code(
    engine: Engine,
) -> None:
    story_id = _accepted_story(engine)

    def review(_payload: object) -> str:
        return canonical_json(
            {
                "schema_version": "agileforge.story-specification-review.v1",
                "compliant": False,
                "complete": True,
                "findings": [
                    {
                        "code": "SPEC_ITEM_OMISSION",
                        "spec_item_id": "REQ.planning-1",
                        "message": "Required behavior is absent.",
                        "suggested_change": None,
                    },
                    {
                        "code": "SPEC_ITEM_UNTESTABLE",
                        "spec_item_id": "REQ.planning-1",
                        "message": "Not testable.",
                        "suggested_change": None,
                    },
                    {
                        "code": "SPEC_ITEM_CONTRADICTION",
                        "spec_item_id": "REQ.planning-1",
                        "message": "Contradiction.",
                        "suggested_change": "Replace the complete Story artifact.",
                    },
                ],
            }
        )

    result = _hybrid(engine, story_id, review)
    assert result["ready_for_sprint"] is False
    assert [item["code"] for item in result["semantic_findings"]] == [
        "SPEC_ITEM_CONTRADICTION",
        "SPEC_ITEM_OMISSION",
        "SPEC_ITEM_UNTESTABLE",
    ]


def test_structural_rejection_stops_before_hybrid_adapter(engine: Engine) -> None:
    story_id = _accepted_story(engine)
    from sqlmodel import Session  # noqa: PLC0415

    from models.core import UserStory  # noqa: PLC0415

    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        story.story_description = "Invalid"
        session.commit()
    calls = 0

    def review(_payload: object) -> str:
        nonlocal calls
        calls += 1
        raise AssertionError

    result = _hybrid(engine, story_id, review)
    assert calls == 0
    assert result["ready_for_sprint"] is False


def test_direct_service_and_tool_exports_have_no_authority_compatibility_name() -> None:
    tool_module = importlib.import_module("tools.spec_tools")
    service_package = importlib.import_module("services.specs")
    assert "validate_story_with_specification" in tool_module.__all__
    assert "validate_story_with_specification" in service_package.__all__
    assert "validate_story_with_spec_authority" not in tool_module.__all__
    assert "validate_story_with_spec_authority" not in service_package.__all__
