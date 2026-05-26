"""Tests for save_stories_tool: validation error handling.

Covers the bug where Pydantic model-level validators produce errors with
an empty ``loc`` tuple, causing ``IndexError: tuple index out of range``
inside the error formatting code.
"""

import json
from datetime import date
from typing import Any, cast

import pytest
from sqlalchemy import event
from sqlmodel import Session, select

from agile_sqlmodel import (
    Product,
    Sprint,
    SprintStory,
    StoryStatus,
    Team,
    UserStory,
    WorkflowEvent,
    WorkflowEventType,
)
from models.core import UserStoryDependency
from orchestrator_agent.agent_tools.story_linkage import normalize_requirement_key
from orchestrator_agent.agent_tools.user_story_writer_tool import tools as story_tools
from orchestrator_agent.agent_tools.user_story_writer_tool.tools import (
    SaveStoriesInput,
    save_stories_tool,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_product(session: Session, product_id: int = 1) -> Product:
    """Insert a minimal Product row and return it."""
    product = Product(
        product_id=product_id,
        name="Test Product",
        description="For testing save_stories_tool",
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


def _valid_story() -> dict:
    """Return a story dict that passes all UserStoryItem validations."""
    return {
        "story_title": "Enforce attestation gate",
        "statement": (
            "As a System Admin, I want persistence blocked without attestation, "
            "so that no document is persisted without explicit consent."
        ),
        "acceptance_criteria": [
            "Verify that persistence is blocked when attestation is false."
        ],
        "invest_score": "High",
        "estimated_effort": "M",
    }


def _alternate_valid_story() -> dict:
    """Return a second valid story payload with different persisted content."""
    return {
        "story_title": "Audit attestation attempts",
        "statement": (
            "As a Compliance Officer, I want attestation attempts audited, "
            "so that unsafe persistence attempts can be reviewed."
        ),
        "acceptance_criteria": [
            "Verify each blocked persistence attempt creates an audit record."
        ],
        "invest_score": "Medium",
        "estimated_effort": "S",
    }


def _story_missing_so_that() -> dict:
    """Return a story whose statement is missing 'so that' – triggers model validator."""  # noqa: E501, RUF002
    return {
        "story_title": "Prevent storing confirmations with snapshots",
        "statement": (
            "As a Compliance Officer, I want the system to prevent "
            "user confirmation data from being stored together with document snapshots."
        ),
        "acceptance_criteria": [
            "Verify that persisted snapshots do not contain user confirmation fields."
        ],
        "invest_score": "High",
        "estimated_effort": "M",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSaveStoriesTool:
    """Validation and persistence tests for save_stories_tool."""

    def test_model_validator_empty_loc_does_not_crash(self, session: Session) -> None:
        """Regression: model-level validator errors have loc=() which caused.

        IndexError when formatting the error message.
        """
        _seed_product(session)

        payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Attestation Gate",
            idempotency_key="test-empty-loc",
            stories=[_story_missing_so_that()],
        )
        result = save_stories_tool(input_data=payload, tool_context=None)

        # Should return a structured error, NOT crash
        assert result["success"] is False
        assert "error" in result
        assert (
            "so that" in result["error"].lower()
            or "validation" in result["error"].lower()
        )

    def test_mixed_valid_and_invalid_stories(self, session: Session) -> None:
        """When some stories pass and some fail model validation,.

        the tool must report failure without crashing.
        """
        _seed_product(session)

        payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Attestation Gate",
            idempotency_key="test-mixed-valid-invalid",
            stories=[_valid_story(), _story_missing_so_that()],
        )
        result = save_stories_tool(input_data=payload, tool_context=None)

        assert result["success"] is False
        assert result["valid_count"] == 1
        assert result["invalid_count"] == 1

    def test_valid_stories_are_saved(self, session: Session) -> None:
        """Happy path: valid stories are persisted and IDs returned."""
        _seed_product(session)

        payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Attestation Gate",
            idempotency_key="test-valid-save",
            stories=[_valid_story()],
        )
        result = save_stories_tool(input_data=payload, tool_context=None)

        assert result["success"], result.get("error")
        assert result["saved_count"] == 1
        assert len(result["story_ids"]) == 1

    def test_save_persists_resolved_dependency_candidate(
        self,
        session: Session,
    ) -> None:
        """Persist resolved story dependency candidates as proposed edges."""
        _seed_product(session)
        dependent = {
            **_alternate_valid_story(),
            "dependency_candidates": [
                {
                    "prerequisite_ref": "Enforce attestation gate",
                    "reason": "Audit story depends on the gate existing first.",
                    "confidence": "explicit",
                }
            ],
        }
        payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Attestation Gate",
            idempotency_key="test-dependency-candidate",
            parent_rank=2,
            stories=[_valid_story(), dependent],
        )

        result = save_stories_tool(input_data=payload, tool_context=None)

        assert result["success"], result.get("error")
        assert result["dependency_proposed_count"] == 1
        edges = session.exec(select(UserStoryDependency)).all()
        assert len(edges) == 1
        edge = edges[0]
        assert edge.status == "proposed"
        assert edge.source == "story_writer"
        assert edge.confidence == "explicit"
        dependent_story = session.get(UserStory, edge.dependent_story_id)
        prerequisite_story = session.get(UserStory, edge.prerequisite_story_id)
        assert dependent_story is not None
        assert prerequisite_story is not None
        assert dependent_story.title == "Audit attestation attempts"
        assert prerequisite_story.title == "Enforce attestation gate"

    def test_save_purges_stale_proposed_dependency_candidate(
        self,
        session: Session,
    ) -> None:
        """Rewrite removes stale proposed dependency rows for saved stories."""
        _seed_product(session)
        first_payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Attestation Gate",
            idempotency_key="test-stale-dependency-first",
            parent_rank=2,
            stories=[
                _valid_story(),
                {
                    **_alternate_valid_story(),
                    "dependency_candidates": [
                        {
                            "prerequisite_ref": "Enforce attestation gate",
                            "reason": "Audit story depends on the gate first.",
                            "confidence": "explicit",
                        }
                    ],
                },
            ],
        )
        second_payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Attestation Gate",
            idempotency_key="test-stale-dependency-second",
            parent_rank=2,
            stories=[_valid_story(), _alternate_valid_story()],
        )

        first = save_stories_tool(input_data=first_payload, tool_context=None)
        second = save_stories_tool(input_data=second_payload, tool_context=None)

        assert first["success"], first.get("error")
        assert second["success"], second.get("error")
        edges = session.exec(select(UserStoryDependency)).all()
        assert edges == []

    def test_save_blocks_explicit_unresolved_dependency_candidate(
        self,
        session: Session,
    ) -> None:
        """Explicit unresolved candidate blocks the story save."""
        _seed_product(session)
        payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Attestation Gate",
            idempotency_key="test-explicit-unresolved-dependency",
            parent_rank=2,
            stories=[
                {
                    **_valid_story(),
                    "dependency_candidates": [
                        {
                            "prerequisite_ref": "Missing prerequisite",
                            "reason": "This dependency was explicit in the source.",
                            "confidence": "explicit",
                        }
                    ],
                }
            ],
        )

        result = save_stories_tool(input_data=payload, tool_context=None)

        assert result["success"] is False
        assert result["error_code"] == "STORY_DEPENDENCY_CANDIDATE_UNRESOLVED"
        rows = session.exec(select(UserStory)).all()
        assert rows == []

    def test_save_warns_for_inferred_unresolved_dependency_candidate(
        self,
        session: Session,
    ) -> None:
        """Inferred unresolved candidate is skipped with a warning."""
        _seed_product(session)
        payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Attestation Gate",
            idempotency_key="test-inferred-unresolved-dependency",
            parent_rank=2,
            stories=[
                {
                    **_valid_story(),
                    "dependency_candidates": [
                        {
                            "prerequisite_ref": "Missing prerequisite",
                            "reason": "This dependency was inferred by the model.",
                            "confidence": "inferred",
                        }
                    ],
                }
            ],
        )

        result = save_stories_tool(input_data=payload, tool_context=None)

        assert result["success"], result.get("error")
        assert result["dependency_proposed_count"] == 0
        assert result["dependency_warnings"][0]["code"] == (
            "STORY_DEPENDENCY_CANDIDATE_UNRESOLVED"
        )
        edges = session.exec(select(UserStoryDependency)).all()
        assert edges == []

    def test_valid_stories_persist_story_points_from_estimated_effort(
        self,
        session: Session,
    ) -> None:
        """Persist story points from each refined story estimated effort."""
        _seed_product(session)

        payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Attestation Gate",
            idempotency_key="test-persist-story-points",
            parent_rank=2,
            stories=[
                {
                    **_valid_story(),
                    "estimated_effort": "XS",
                },
                {
                    **_alternate_valid_story(),
                    "estimated_effort": "XL",
                },
            ],
        )

        result = save_stories_tool(input_data=payload, tool_context=None)

        assert result["success"], result.get("error")
        rows = session.exec(
            select(UserStory)
            .where(UserStory.product_id == 1)
            .order_by(cast("Any", UserStory.refinement_slot))
        ).all()
        assert [row.story_points for row in rows] == [1, 8]

    def test_valid_stories_persist_rank_from_parent_rank_and_slot(
        self,
        session: Session,
    ) -> None:
        """Persist deterministic child ranks from parent rank and refinement slot."""
        _seed_product(session)

        payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Attestation Gate",
            idempotency_key="test-persist-refined-rank",
            parent_rank=3,
            stories=[_valid_story(), _alternate_valid_story()],
        )

        result = save_stories_tool(input_data=payload, tool_context=None)

        assert result["success"], result.get("error")
        rows = session.exec(
            select(UserStory)
            .where(UserStory.product_id == 1)
            .order_by(cast("Any", UserStory.refinement_slot))
        ).all()
        assert [row.rank for row in rows] == ["301", "302"]

    def test_refinement_update_refreshes_story_points_and_rank(
        self,
        session: Session,
    ) -> None:
        """Refresh story points and rank when updating an existing refined story."""
        _seed_product(session)
        seed = UserStory(
            product_id=1,
            title="Attestation Gate",
            story_description="Backlog seed",
            acceptance_criteria=None,
            source_requirement=normalize_requirement_key("Attestation Gate"),
            refinement_slot=1,
            story_origin="backlog_seed",
            is_refined=False,
            is_superseded=False,
            story_points=None,
            rank=None,
        )
        session.add(seed)
        session.commit()
        session.refresh(seed)

        payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Attestation Gate",
            idempotency_key="test-update-points-rank",
            parent_rank=4,
            stories=[_valid_story()],
        )

        result = save_stories_tool(input_data=payload, tool_context=None)

        assert result["success"], result.get("error")
        session.expire_all()
        refreshed = session.get(UserStory, seed.story_id)
        expected_story_points = 3
        assert refreshed is not None
        assert refreshed.story_points == expected_story_points
        assert refreshed.rank == "401"

    def test_save_without_parent_rank_preserves_child_slot_fallback_rank(
        self,
        session: Session,
    ) -> None:
        """Do not treat old child-slot ranks as parent rank evidence."""
        _seed_product(session)
        for slot in (1, 2):
            session.add(
                UserStory(
                    product_id=1,
                    title=f"Seed {slot}",
                    story_description="Backlog seed",
                    acceptance_criteria=None,
                    source_requirement=normalize_requirement_key("Attestation Gate"),
                    refinement_slot=slot,
                    story_origin="refined",
                    is_refined=True,
                    is_superseded=False,
                    story_points=2,
                    rank=str(slot),
                )
            )
        session.commit()

        payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Attestation Gate",
            idempotency_key="test-no-parent-rank-fallback",
            stories=[_valid_story(), _alternate_valid_story()],
        )

        result = save_stories_tool(input_data=payload, tool_context=None)

        assert result["success"], result.get("error")
        rows = session.exec(
            select(UserStory)
            .where(UserStory.product_id == 1)
            .order_by(cast("Any", UserStory.refinement_slot))
        ).all()
        assert [row.rank for row in rows] == ["1", "2"]

    def test_nonexistent_product_returns_error(self, session: Session) -> None:
        """Calling with a product_id that does not exist returns structured error."""
        del session
        payload = SaveStoriesInput(
            product_id=999,
            parent_requirement="N/A",
            idempotency_key="test-missing-product",
            stories=[_valid_story()],
        )
        result = save_stories_tool(input_data=payload, tool_context=None)

        assert result["success"] is False
        assert "not found" in result["error"].lower()

    def test_extra_field_rejected(self, session: Session) -> None:
        """UserStoryItem has extra='forbid'; story with unknown keys must fail validation."""  # noqa: E501
        _seed_product(session)

        story = _valid_story()
        story["unknown_field"] = "should be rejected"

        payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Attestation Gate",
            idempotency_key="test-extra-field",
            stories=[story],
        )
        result = save_stories_tool(input_data=payload, tool_context=None)

        assert result["success"] is False
        assert "error" in result

    def test_empty_stories_list_handled(self, session: Session) -> None:
        """An empty story list should fail gracefully (no crash)."""
        _seed_product(session)

        payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Attestation Gate",
            idempotency_key="test-empty-list",
            stories=[],
        )
        result = save_stories_tool(input_data=payload, tool_context=None)

        # Should succeed with 0 saved (no stories to persist)
        # OR return a validation error — either way, must not crash
        assert isinstance(result, dict)
        assert "success" in result

    def test_refinement_updates_seed_rows_by_linkage(self, session: Session) -> None:
        """Verify refinement updates seed rows by linkage."""
        _seed_product(session)
        seed = UserStory(
            product_id=1,
            title="Attestation Gate",
            story_description="Backlog seed",
            acceptance_criteria=None,
            source_requirement=normalize_requirement_key("Attestation Gate"),
            refinement_slot=1,
            story_origin="backlog_seed",
            is_refined=False,
            is_superseded=False,
        )
        session.add(seed)
        session.commit()
        session.refresh(seed)
        seed_id = seed.story_id

        payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Attestation Gate",
            idempotency_key="test-refinement-updates-seed",
            stories=[_valid_story()],
        )
        result = save_stories_tool(input_data=payload, tool_context=None)

        assert result["success"], result.get("error")
        assert result["updated_count"] == 1
        assert result["created_count"] == 0
        assert result["updated_story_ids"] == [seed_id]

        session.expire_all()
        refreshed = session.get(UserStory, seed_id)
        assert refreshed is not None
        assert refreshed.is_refined is True
        assert (refreshed.acceptance_criteria or "").strip().startswith("- Verify")
        assert refreshed.story_origin == "refined"

    def test_refinement_repeat_is_idempotent_no_new_rows(
        self, session: Session
    ) -> None:
        """Verify refinement repeat is idempotent no new rows."""
        _seed_product(session)
        seed = UserStory(
            product_id=1,
            title="Attestation Gate",
            story_description="Backlog seed",
            acceptance_criteria=None,
            source_requirement=normalize_requirement_key("Attestation Gate"),
            refinement_slot=1,
            story_origin="backlog_seed",
            is_refined=False,
            is_superseded=False,
        )
        session.add(seed)
        session.commit()

        payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Attestation Gate",
            idempotency_key="test-repeat-idempotent",
            stories=[_valid_story()],
        )
        first = save_stories_tool(input_data=payload, tool_context=None)
        second = save_stories_tool(input_data=payload, tool_context=None)

        assert first["success"] is True
        assert second["success"] is True
        assert second["created_count"] == 0
        assert second["updated_count"] == 1

        rows = session.exec(select(UserStory).where(UserStory.product_id == 1)).all()
        assert len(rows) == 1

    def test_source_requirement_normalization_matches_rows(
        self, session: Session
    ) -> None:
        """Verify source requirement normalization matches rows."""
        _seed_product(session)
        seed = UserStory(
            product_id=1,
            title="Attestation Gate",
            story_description="Backlog seed",
            acceptance_criteria=None,
            source_requirement=normalize_requirement_key("Attestation Gate"),
            refinement_slot=1,
            story_origin="backlog_seed",
            is_refined=False,
            is_superseded=False,
        )
        session.add(seed)
        session.commit()
        session.refresh(seed)

        payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="  attestation   gate  ",
            idempotency_key="test-source-normalization",
            stories=[_valid_story()],
        )
        result = save_stories_tool(input_data=payload, tool_context=None)
        assert result["success"], result.get("error")
        assert result["updated_story_ids"] == [seed.story_id]

    def test_save_stories_tool_replays_idempotency_key(self, session: Session) -> None:
        """Replay same idempotency key from event metadata without touching rows."""
        _seed_product(session)

        payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Attestation Gate",
            idempotency_key="test-replay-key",
            stories=[_valid_story()],
        )
        first = save_stories_tool(input_data=payload, tool_context=None)
        second = save_stories_tool(input_data=payload, tool_context=None)

        assert first["success"] is True
        assert second["success"] is True
        assert second["idempotency_replayed"] is True
        assert second["saved_count"] == first["saved_count"]
        assert second["story_ids"] == first["story_ids"]

        rows = session.exec(select(UserStory).where(UserStory.product_id == 1)).all()
        events = session.exec(
            select(WorkflowEvent).where(
                WorkflowEvent.event_type == WorkflowEventType.STORIES_SAVED
            )
        ).all()
        assert len(rows) == 1
        assert len(events) == 1

    def test_save_stories_tool_rejects_reused_key_for_different_requirement(
        self, session: Session
    ) -> None:
        """Reject same idempotency key when the parent requirement differs."""
        _seed_product(session)

        first_payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Attestation Gate",
            idempotency_key="test-reused-key-requirement",
            stories=[_valid_story()],
        )
        second_payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Audit Gate",
            idempotency_key="test-reused-key-requirement",
            stories=[_valid_story()],
        )
        first = save_stories_tool(input_data=first_payload, tool_context=None)
        second = save_stories_tool(input_data=second_payload, tool_context=None)

        assert first["success"] is True
        assert second["success"] is False
        assert second["error_code"] == "IDEMPOTENCY_KEY_REUSED"
        assert second["idempotency_replayed"] is False

    def test_save_stories_tool_rejects_reused_key_for_different_story_payload(
        self, session: Session
    ) -> None:
        """Reject same idempotency key when validated story payload differs."""
        _seed_product(session)

        first_payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Attestation Gate",
            idempotency_key="test-reused-key-payload",
            stories=[_valid_story()],
        )
        second_payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Attestation Gate",
            idempotency_key="test-reused-key-payload",
            stories=[_alternate_valid_story()],
        )
        first = save_stories_tool(input_data=first_payload, tool_context=None)
        second = save_stories_tool(input_data=second_payload, tool_context=None)

        assert first["success"] is True
        assert second["success"] is False
        assert second["error_code"] == "IDEMPOTENCY_KEY_REUSED"
        assert second["idempotency_replayed"] is False

    def test_save_stories_tool_rejects_reused_key_for_different_parent_rank(
        self,
        session: Session,
    ) -> None:
        """Reject same idempotency key when parent rank differs."""
        _seed_product(session)

        first_payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Attestation Gate",
            idempotency_key="test-reused-key-parent-rank",
            parent_rank=1,
            stories=[_valid_story()],
        )
        second_payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Attestation Gate",
            idempotency_key="test-reused-key-parent-rank",
            parent_rank=2,
            stories=[_valid_story()],
        )

        first = save_stories_tool(input_data=first_payload, tool_context=None)
        second = save_stories_tool(input_data=second_payload, tool_context=None)

        assert first["success"] is True
        assert second["success"] is False
        assert second["error_code"] == "IDEMPOTENCY_KEY_REUSED"
        assert second["idempotency_replayed"] is False

    def test_save_stories_tool_acquires_write_lock_before_reads(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Acquire DB write lock before product lookup and idempotency event read."""
        _seed_product(session)
        order: list[str] = []
        engine = session.get_bind()

        def capture_product_lookup(
            _conn: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: object,
        ) -> None:
            if "FROM products" in statement:
                order.append("product_lookup")

        def fake_acquire_write_lock(_session: Session) -> None:
            order.append("write_lock")

        original_find_event = story_tools._find_story_save_event

        def wrapped_find_event(
            session: Session,
            *,
            product_id: int,
            idempotency_key: str,
        ) -> WorkflowEvent | None:
            order.append("idempotency_check")
            return original_find_event(
                session,
                product_id=product_id,
                idempotency_key=idempotency_key,
            )

        event.listen(engine, "before_cursor_execute", capture_product_lookup)
        monkeypatch.setattr(
            story_tools,
            "_acquire_story_save_write_lock",
            fake_acquire_write_lock,
            raising=False,
        )
        monkeypatch.setattr(story_tools, "_find_story_save_event", wrapped_find_event)
        try:
            payload = SaveStoriesInput(
                product_id=1,
                parent_requirement="Attestation Gate",
                idempotency_key="test-lock-order",
                stories=[_valid_story()],
            )
            result = save_stories_tool(input_data=payload, tool_context=None)
        finally:
            event.remove(engine, "before_cursor_execute", capture_product_lookup)

        assert result["success"], result.get("error")
        assert order[:3] == ["write_lock", "product_lookup", "idempotency_check"]

    def test_save_stories_tool_blocks_replacement_after_sprint_link(
        self, session: Session
    ) -> None:
        """Block replacing active requirement stories that are linked to a sprint."""
        _seed_product(session)
        story = UserStory(
            product_id=1,
            title="Attestation Gate",
            story_description="Backlog seed",
            acceptance_criteria=None,
            source_requirement=normalize_requirement_key("Attestation Gate"),
            refinement_slot=1,
            story_origin="backlog_seed",
            is_refined=False,
            is_superseded=False,
        )
        team = Team(name="Delivery")
        session.add(team)
        session.add(story)
        session.commit()
        session.refresh(story)
        session.refresh(team)
        sprint = Sprint(
            product_id=1,
            team_id=team.team_id,
            goal="Ship guarded persistence",
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 14),
        )
        session.add(sprint)
        session.commit()
        session.refresh(sprint)
        session.add(SprintStory(sprint_id=sprint.sprint_id, story_id=story.story_id))
        session.commit()

        payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Attestation Gate",
            idempotency_key="test-block-sprint-link",
            stories=[_valid_story()],
        )
        result = save_stories_tool(input_data=payload, tool_context=None)

        assert result["success"] is False
        assert result["error_code"] == "STORY_REPLACEMENT_UNSAFE"
        assert result["blockers"]

        session.refresh(story)
        assert story.is_refined is False

    def test_save_stories_tool_blocks_replacement_after_status_progress(
        self, session: Session
    ) -> None:
        """Block replacing active requirement stories beyond To Do status."""
        _seed_product(session)
        story = UserStory(
            product_id=1,
            title="Attestation Gate",
            story_description="Backlog seed",
            acceptance_criteria=None,
            source_requirement=normalize_requirement_key("Attestation Gate"),
            refinement_slot=1,
            story_origin="backlog_seed",
            is_refined=False,
            is_superseded=False,
            status=StoryStatus.IN_PROGRESS,
        )
        session.add(story)
        session.commit()
        session.refresh(story)

        payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Attestation Gate",
            idempotency_key="test-block-status",
            stories=[_valid_story()],
        )
        result = save_stories_tool(input_data=payload, tool_context=None)

        assert result["success"] is False
        assert result["error_code"] == "STORY_REPLACEMENT_UNSAFE"
        assert result["blockers"]

        session.refresh(story)
        assert story.is_refined is False

    def test_save_stories_tool_supersedes_overflow_active_slots(
        self, session: Session
    ) -> None:
        """Mark overflow active slots superseded when fewer stories are saved."""
        _seed_product(session)
        first = UserStory(
            product_id=1,
            title="Attestation Gate",
            story_description="Backlog seed 1",
            acceptance_criteria=None,
            source_requirement=normalize_requirement_key("Attestation Gate"),
            refinement_slot=1,
            story_origin="backlog_seed",
            is_refined=False,
            is_superseded=False,
        )
        second = UserStory(
            product_id=1,
            title="Attestation Gate overflow",
            story_description="Backlog seed 2",
            acceptance_criteria=None,
            source_requirement=normalize_requirement_key("Attestation Gate"),
            refinement_slot=2,
            story_origin="backlog_seed",
            is_refined=False,
            is_superseded=False,
        )
        session.add(first)
        session.add(second)
        session.commit()
        session.refresh(first)
        session.refresh(second)

        payload = SaveStoriesInput(
            product_id=1,
            parent_requirement="Attestation Gate",
            idempotency_key="test-supersede-overflow",
            stories=[_valid_story()],
        )
        result = save_stories_tool(input_data=payload, tool_context=None)

        assert result["success"], result.get("error")
        assert result["updated_story_ids"] == [first.story_id]
        assert result["superseded_count"] == 1
        assert result["superseded_story_ids"] == [second.story_id]

        session.expire_all()
        refreshed_first = session.get(UserStory, first.story_id)
        refreshed_second = session.get(UserStory, second.story_id)
        assert refreshed_first is not None
        assert refreshed_second is not None
        assert refreshed_first.is_superseded is False
        assert refreshed_second.is_superseded is True

        event = session.exec(
            select(WorkflowEvent).where(
                WorkflowEvent.event_type == WorkflowEventType.STORIES_SAVED
            )
        ).one()
        metadata = json.loads(event.event_metadata or "{}")
        assert metadata["idempotency_key"] == "test-supersede-overflow"
        assert metadata["saved_count"] == 1
        assert metadata["updated_count"] == 1
        assert metadata["created_count"] == 0
        assert metadata["superseded_count"] == 1
        assert metadata["story_ids"] == [first.story_id]
        assert metadata["superseded_story_ids"] == [second.story_id]
