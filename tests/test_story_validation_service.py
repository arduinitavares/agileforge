"""Deep provider-free structural validation of accepted Story artifacts."""
# ruff: noqa: D103

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest
from sqlalchemy import event
from sqlmodel import Session, select

from models.core import UserStory
from models.product_definition import SpecificationCandidate
from models.workflow import BacklogArtifact, StoryArtifactDecision
from services.specs import story_validation_service
from services.specs.story_validation_service import (
    StorySemanticReview,
    validate_story_with_specification,
)
from tests.test_create_user_story import (
    _decide_story,
    _record_story,
    _seed_story_parent,
)
from utils.spec_schemas import ValidationEvidence
from workflow.fingerprints import canonical_json

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


VALIDATED_AT = datetime(2026, 8, 21, 12, tzinfo=UTC)


def _accepted_story(engine: Engine) -> int:
    project_id, roadmap_id = _seed_story_parent(engine)
    with Session(engine) as session:
        artifact = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
            title="Accepted validation target",
        )
        result = _decide_story(session, artifact, decision="accepted", offset=2)
        session.commit()
        return result.activated_story_ids[0]


def _validate(
    engine: Engine,
    story_id: int,
    *,
    semantic_review: StorySemanticReview | None = None,
) -> dict[str, Any]:
    with patch.object(story_validation_service, "get_engine", return_value=engine):
        return cast(
            "dict[str, Any]",
            validate_story_with_specification(
                {"story_id": story_id},
                now=lambda: VALIDATED_AT,
                semantic_review=semantic_review,
            ),
        )


def test_structural_validation_persists_exact_v2_snapshot_without_provider(
    engine: Engine,
) -> None:
    story_id = _accepted_story(engine)
    semantic_calls = 0

    def forbidden_semantic_call(_payload: object) -> str:
        nonlocal semantic_calls
        semantic_calls += 1
        message = "structural validation must not call a provider"
        raise AssertionError(message)

    statements: list[str] = []

    def record_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        *_args: object,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        result = _validate(
            engine,
            story_id,
            semantic_review=forbidden_semantic_call,
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)
    assert semantic_calls == 0
    assert statements[0] == "BEGIN IMMEDIATE"
    assert result["success"] is True
    assert result["ready_for_sprint"] is True
    assert result["structural_failures"] == []
    assert result["structural_warnings"] == []

    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        assert story.validation_evidence is not None
        evidence = ValidationEvidence.model_validate_json(
            story.validation_evidence,
            strict=True,
        )
        assert story.validation_evidence == canonical_json(
            evidence.model_dump(mode="json")
        )
        assert evidence.schema_version == "agileforge.story-validation-evidence.v2"
        assert evidence.mode == "structural"
        assert evidence.semantic_review_state == "not_requested"
        assert evidence.referenced_spec_item_ids == ("REQ.planning-1",)
        assert evidence.validated_at == VALIDATED_AT


def test_missing_exact_acceptance_emits_only_acceptance_finding_and_stays_unready(
    engine: Engine,
) -> None:
    story_id = _accepted_story(engine)
    with Session(engine) as session:
        decision = session.exec(select(StoryArtifactDecision)).one()
        session.delete(decision)
        session.commit()

    result = _validate(engine, story_id)
    assert result["ready_for_sprint"] is False
    assert [item["code"] for item in result["structural_failures"]] == [
        "STORY_ACCEPTANCE_INVALID"
    ]


def test_missing_story_artifact_runs_only_applicable_local_rules(
    engine: Engine,
) -> None:
    story_id = _accepted_story(engine)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("DELETE FROM story_artifact_decisions")
        connection.exec_driver_sql("DELETE FROM story_artifacts")
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")

    result = _validate(engine, story_id)
    assert [item["code"] for item in result["structural_failures"]] == [
        "STORY_ACCEPTANCE_INVALID",
        "STORY_ITEM_BINDING_INVALID",
    ]


def test_missing_story_item_keeps_exact_parent_checks_applicable(
    engine: Engine,
) -> None:
    story_id = _accepted_story(engine)
    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        story.source_story_item_id = "US-9999"
        session.commit()

    result = _validate(engine, story_id)
    assert [item["code"] for item in result["structural_failures"]] == [
        "STORY_ITEM_BINDING_INVALID"
    ]


def test_failed_specification_load_does_not_guess_parent_reference_failure(
    engine: Engine,
) -> None:
    story_id = _accepted_story(engine)
    with Session(engine) as session:
        specification = session.exec(select(SpecificationCandidate)).one()
        specification.canonical_envelope_json = "{}"
        session.add(specification)
        session.commit()

    result = _validate(engine, story_id)
    assert [item["code"] for item in result["structural_failures"]] == [
        "SPECIFICATION_BINDING_INVALID"
    ]


def test_failed_backlog_load_does_not_fabricate_reference_failure(
    engine: Engine,
) -> None:
    story_id = _accepted_story(engine)
    with Session(engine) as session:
        backlog = session.exec(select(BacklogArtifact)).one()
        backlog.canonical_content_json = "{}"
        session.add(backlog)
        session.commit()

    result = _validate(engine, story_id)
    assert [item["code"] for item in result["structural_failures"]] == [
        "SPECIFICATION_BINDING_INVALID"
    ]


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        (
            "spec_item_ids_json",
            canonical_json(["REQ.unknown", "REQ.unknown"]),
            "SPEC_ITEM_REFERENCES_INVALID",
        ),
        ("story_description", "Deliver it", "STORY_STATEMENT_INVALID"),
        (
            "acceptance_criteria_json",
            canonical_json(["   "]),
            "ACCEPTANCE_CRITERIA_INVALID",
        ),
    ],
)
def test_each_row_local_rule_has_an_independent_defect_fixture(
    engine: Engine,
    field: str,
    value: str,
    expected_code: str,
) -> None:
    story_id = _accepted_story(engine)
    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        setattr(story, field, value)
        session.add(story)
        session.commit()

    result = _validate(engine, story_id)
    assert [item["code"] for item in result["structural_failures"]] == [
        "STORY_ITEM_BINDING_INVALID",
        expected_code,
    ]


def test_unrelated_local_defects_accumulate_once_in_canonical_rule_order(
    engine: Engine,
) -> None:
    story_id = _accepted_story(engine)
    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        story.title = "Drifted operational title"
        story.spec_item_ids_json = canonical_json(["REQ.unknown", "REQ.unknown"])
        story.story_description = "Deliver it"
        story.acceptance_criteria_json = canonical_json(["   "])
        session.commit()

    result = _validate(engine, story_id)
    assert [item["code"] for item in result["structural_failures"]] == [
        "STORY_ITEM_BINDING_INVALID",
        "SPEC_ITEM_REFERENCES_INVALID",
        "STORY_STATEMENT_INVALID",
        "ACCEPTANCE_CRITERIA_INVALID",
    ]
    assert result["structural_warnings"] == []
