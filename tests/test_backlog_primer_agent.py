"""Schema tests for backlog_primer agent outputs."""

import json
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError
from sqlmodel import Session as SqlSession
from sqlmodel import SQLModel, create_engine, select

from agile_sqlmodel import Product, UserStory
from models.enums import StoryStatus, WorkflowEventType
from models.events import WorkflowEvent
from orchestrator_agent.agent_tools.backlog_primer.schemes import (
    BacklogItem,
    InputSchema,
    OutputSchema,
)
from orchestrator_agent.agent_tools.backlog_primer.tools import (
    SaveBacklogInput,
    save_backlog_tool,
)
from orchestrator_agent.agent_tools.story_linkage import normalize_requirement_key


class TestBacklogPrimerSchemas:
    """Validate input/output schema rules."""

    def test_input_schema_json_roundtrip(self) -> None:
        """Verify input schema json roundtrip."""
        payload: dict[str, Any] = {
            "product_vision_statement": "For teams who need clarity...",
            "technical_spec": "Spec: must support SSO and audit logging.",
            "compiled_authority": '{"scope_themes":["Auth"],"invariants":[]}',
            "prior_backlog_state": "NO_HISTORY",
            "as_built_assessment": "NO_AS_BUILT_ASSESSMENT",
            "implementation_evidence": "NO_EVIDENCE",
            "user_input": "Focus on onboarding and analytics.",
        }
        parsed = InputSchema.model_validate_json(json.dumps(payload))
        assert parsed.product_vision_statement.startswith("For teams")
        assert parsed.as_built_assessment == "NO_AS_BUILT_ASSESSMENT"
        assert parsed.implementation_evidence == "NO_EVIDENCE"

    def test_output_schema_valid_payload(self) -> None:
        """Verify output schema valid payload."""
        payload: dict[str, Any] = {
            "backlog_items": [
                {
                    "priority": 1,
                    "requirement": "User onboarding and account setup",
                    "value_driver": "Customer Satisfaction",
                    "justification": "Unlocks first-time user value",
                    "estimated_effort": "M",
                    "technical_note": "Requires SSO and audit logging.",
                },
                {
                    "priority": 2,
                    "requirement": "Core workflow management",
                    "value_driver": "Revenue",
                    "justification": "Delivers primary business outcome",
                    "estimated_effort": "L",
                    "technical_note": None,
                },
            ],
            "is_complete": False,
            "clarifying_questions": ["Which user segment should be prioritized first?"],
        }

        parsed = OutputSchema.model_validate_json(json.dumps(payload))
        assert len(parsed.backlog_items) == 2  # noqa: PLR2004

    def test_output_schema_accepts_model_owned_brownfield_hint(self) -> None:
        """Backlog items may carry only model-owned brownfield helper fields."""
        payload: dict[str, Any] = {
            "backlog_items": [
                {
                    "priority": 1,
                    "requirement": "Validate Captain-Aware Optimizer Contract",
                    "authority_ref": "REQ.captain-aware-optimization",
                    "capability_hint": "Captain-Aware Squad Optimizer",
                    "value_driver": "Strategic",
                    "justification": (
                        "As-Built evidence indicates the optimizer exists."
                    ),
                    "estimated_effort": "M",
                    "technical_note": "Validate existing captain multiplier behavior.",
                }
            ],
            "is_complete": False,
            "clarifying_questions": [],
        }

        parsed = OutputSchema.model_validate_json(json.dumps(payload))

        item = parsed.backlog_items[0]
        assert item.requirement == "Validate Captain-Aware Optimizer Contract"
        assert item.authority_ref == "REQ.captain-aware-optimization"
        assert item.capability_hint == "Captain-Aware Squad Optimizer"

    def test_output_schema_rejects_model_owned_brownfield_metadata(self) -> None:
        """The model must not own host-derived As-Built metadata fields."""
        payload: dict[str, Any] = {
            "backlog_items": [
                {
                    "priority": 1,
                    "requirement": "Validate Captain-Aware Optimizer Contract",
                    "capability_name": "Captain-Aware Squad Optimizer",
                    "authority_ref": "REQ.captain-aware-optimization",
                    "as_built_status": "observed_with_missing_evidence",
                    "recommended_backlog_treatment": "create_verification_item",
                    "value_driver": "Strategic",
                    "justification": "As-Built evidence indicates existing behavior.",
                    "estimated_effort": "M",
                }
            ],
            "is_complete": False,
            "clarifying_questions": [],
        }

        with pytest.raises(ValidationError):
            OutputSchema.model_validate(payload)

    def test_output_schema_rejects_model_supplied_host_annotation(self) -> None:
        """The model must not emit host-owned As-Built annotations."""
        payload: dict[str, Any] = {
            "backlog_items": [
                {
                    "priority": 1,
                    "requirement": "Validate Captain-Aware Optimizer Contract",
                    "authority_ref": "REQ.captain-aware-optimization",
                    "capability_hint": "Captain-Aware Squad Optimizer",
                    "as_built_annotation": {
                        "schema_version": "agileforge.brownfield_annotation.v1",
                        "source": "host_derived",
                        "match_tier": "exact",
                        "match_basis": ["authority_ref"],
                    },
                    "value_driver": "Strategic",
                    "justification": "As-Built evidence indicates existing behavior.",
                    "estimated_effort": "M",
                }
            ],
            "is_complete": False,
            "clarifying_questions": [],
        }

        with pytest.raises(ValidationError):
            OutputSchema.model_validate(payload)

    def test_backlog_item_rejects_invalid_effort(self) -> None:
        """Verify backlog item rejects invalid effort."""
        with pytest.raises(ValidationError):
            BacklogItem.model_validate(
                {
                    "priority": 1,
                    "requirement": "Notifications",
                    "value_driver": "Strategic",
                    "justification": "Boosts engagement",
                    "estimated_effort": "XXL",
                    "technical_note": None,
                }
            )

    def test_backlog_item_requires_positive_priority(self) -> None:
        """Verify backlog item requires positive priority."""
        with pytest.raises(ValidationError):
            BacklogItem(
                priority=0,
                requirement="Security baseline",
                authority_ref=None,
                capability_hint=None,
                value_driver="Strategic",
                justification="Reduces compliance risk",
                estimated_effort="M",
                technical_note=None,
            )


class TestSaveBacklogTool:
    """Tests for save_backlog_tool."""

    @pytest.mark.asyncio
    async def test_save_backlog_stores_in_session_state(self) -> None:
        """Valid backlog items are stored in session state."""
        mock_context = MagicMock()
        mock_context.state = {}

        # Create in-memory DB so get_engine() doesn't hit the pytest safety guard
        test_engine = create_engine("sqlite://", echo=False)
        SQLModel.metadata.create_all(test_engine)

        # Insert a product so FK constraint is satisfied
        with SqlSession(test_engine) as session:
            session.add(Product(name="Test Product"))
            session.commit()

        save_input = SaveBacklogInput(
            product_id=1,
            backlog_items=[
                {
                    "priority": 1,
                    "requirement": "User authentication",
                    "value_driver": "Customer Satisfaction",
                    "justification": "Security baseline",
                    "estimated_effort": "M",
                },
                {
                    "priority": 2,
                    "requirement": "Dashboard analytics",
                    "value_driver": "Revenue",
                    "justification": "Drives engagement",
                    "estimated_effort": "L",
                },
            ],
        )

        with patch(
            "orchestrator_agent.agent_tools.backlog_primer.tools.get_engine",
            return_value=test_engine,
        ):
            result = await save_backlog_tool(save_input, tool_context=mock_context)

        assert result["success"] is True
        assert result["saved_count"] == 2  # noqa: PLR2004
        assert "approved_backlog" in mock_context.state
        assert mock_context.state["approved_backlog"]["product_id"] == 1
        assert len(mock_context.state["approved_backlog"]["items"]) == 2  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_save_backlog_rejects_invalid_items(self) -> None:
        """Invalid backlog items fail validation."""
        mock_context = MagicMock()
        mock_context.state = {}

        save_input = SaveBacklogInput(
            product_id=1,
            backlog_items=[
                {
                    "priority": 1,
                    # Missing: requirement, value_driver, justification, estimated_effort  # noqa: E501
                },
            ],
        )

        result = await save_backlog_tool(save_input, tool_context=mock_context)

        assert result["success"] is False
        assert "Validation errors" in result["error"]

    @pytest.mark.asyncio
    async def test_save_backlog_requires_tool_context(self) -> None:
        """Tool returns error when tool_context is None."""
        save_input = SaveBacklogInput(
            product_id=1,
            backlog_items=[
                {
                    "priority": 1,
                    "requirement": "Test",
                    "value_driver": "Revenue",
                    "justification": "Test",
                    "estimated_effort": "S",
                },
            ],
        )

        result = await save_backlog_tool(save_input, tool_context=None)

        assert result["success"] is False
        assert "ToolContext required" in result["error"]

    @pytest.mark.asyncio
    async def test_backlog_save_sets_linkage_fields(self) -> None:
        """Verify backlog save sets linkage fields."""
        mock_context = MagicMock()
        mock_context.state = {}
        test_engine = create_engine("sqlite://", echo=False)
        SQLModel.metadata.create_all(test_engine)
        with SqlSession(test_engine) as session:
            session.add(Product(name="Test Product"))
            session.commit()

        save_input = SaveBacklogInput(
            product_id=1,
            backlog_items=[
                {
                    "priority": 1,
                    "requirement": "User authentication",
                    "value_driver": "Customer Satisfaction",
                    "justification": "Security baseline",
                    "estimated_effort": "M",
                },
            ],
        )

        with patch(
            "orchestrator_agent.agent_tools.backlog_primer.tools.get_engine",
            return_value=test_engine,
        ):
            result = await save_backlog_tool(save_input, tool_context=mock_context)

        assert result["success"] is True
        with SqlSession(test_engine) as session:
            story = session.exec(
                select(UserStory).where(UserStory.product_id == 1)
            ).first()
            assert story is not None
            assert story.source_requirement == normalize_requirement_key(
                "User authentication"
            )
            assert story.refinement_slot == 1
            assert story.story_origin == "backlog_seed"
            assert story.is_refined is False
            assert story.is_superseded is False

    @pytest.mark.asyncio
    async def test_backlog_resave_after_refinement_blocks_when_story_progressed(
        self,
    ) -> None:
        """Backlog replacement must block if a prior seed was already refined."""
        mock_context = MagicMock()
        mock_context.state = {}
        test_engine = create_engine("sqlite://", echo=False)
        SQLModel.metadata.create_all(test_engine)
        with SqlSession(test_engine) as session:
            session.add(Product(name="Test Product"))
            session.commit()
            session.add(
                UserStory(
                    product_id=1,
                    title="Refined auth title",
                    story_description="As a user, I want auth, so that secure login.",
                    acceptance_criteria="- Verify auth",
                    source_requirement=normalize_requirement_key("User authentication"),
                    refinement_slot=1,
                    story_origin="refined",
                    is_refined=True,
                    is_superseded=False,
                )
            )
            session.commit()

        save_input = SaveBacklogInput(
            product_id=1,
            backlog_items=[
                {
                    "priority": 1,
                    "requirement": "User authentication",
                    "value_driver": "Customer Satisfaction",
                    "justification": "Security baseline",
                    "estimated_effort": "M",
                },
            ],
        )

        with patch(
            "orchestrator_agent.agent_tools.backlog_primer.tools.get_engine",
            return_value=test_engine,
        ):
            result = await save_backlog_tool(save_input, tool_context=mock_context)

        assert result["success"] is False
        assert result["error"] == "BACKLOG_REPLACEMENT_BLOCKED"
        assert result["blocked_count"] == 1
        with SqlSession(test_engine) as session:
            count = len(
                session.exec(select(UserStory).where(UserStory.product_id == 1)).all()
            )
            assert count == 1

    @pytest.mark.asyncio
    async def test_backlog_resave_supersedes_prior_backlog_seed_rows(self) -> None:
        """Refined Backlog save replaces earlier active seed rows atomically."""
        mock_context = MagicMock()
        mock_context.state = {}
        test_engine = create_engine("sqlite://", echo=False)
        SQLModel.metadata.create_all(test_engine)
        with SqlSession(test_engine) as session:
            session.add(Product(name="Test Product"))
            session.commit()
            session.add(
                UserStory(
                    product_id=1,
                    title="Old authentication",
                    status=StoryStatus.TO_DO,
                    story_description="Old seed",
                    source_requirement=normalize_requirement_key("Old authentication"),
                    refinement_slot=1,
                    story_origin="backlog_seed",
                    is_refined=False,
                    is_superseded=False,
                )
            )
            session.commit()

        save_input = SaveBacklogInput(
            product_id=1,
            idempotency_key="save-backlog-2",
            backlog_items=[
                {
                    "priority": 1,
                    "requirement": "New authentication",
                    "value_driver": "Customer Satisfaction",
                    "justification": "Security baseline",
                    "estimated_effort": "M",
                },
                {
                    "priority": 2,
                    "requirement": "Lineup comparison",
                    "value_driver": "Strategic",
                    "justification": "Improves weekly decision quality",
                    "estimated_effort": "L",
                },
            ],
        )

        with patch(
            "orchestrator_agent.agent_tools.backlog_primer.tools.get_engine",
            return_value=test_engine,
        ):
            result = await save_backlog_tool(save_input, tool_context=mock_context)

        assert result["success"] is True
        assert result["saved_count"] == 2  # noqa: PLR2004
        assert result["superseded_count"] == 1
        with SqlSession(test_engine) as session:
            rows = session.exec(
                select(UserStory)
                .where(UserStory.product_id == 1)
                .order_by(cast("Any", UserStory.story_id))
            ).all()
            assert len(rows) == 3  # noqa: PLR2004
            assert rows[0].is_superseded is True
            assert [row.title for row in rows if not row.is_superseded] == [
                "New authentication",
                "Lineup comparison",
            ]

    @pytest.mark.asyncio
    async def test_backlog_save_idempotency_key_replays_without_replacing_again(
        self,
    ) -> None:
        """Repeated Backlog save with the same idempotency key is non-mutating."""
        mock_context = MagicMock()
        mock_context.state = {}
        test_engine = create_engine("sqlite://", echo=False)
        SQLModel.metadata.create_all(test_engine)
        with SqlSession(test_engine) as session:
            session.add(Product(name="Test Product"))
            session.commit()

        save_input = SaveBacklogInput(
            product_id=1,
            idempotency_key="save-backlog-3",
            backlog_items=[
                {
                    "priority": 1,
                    "requirement": "User authentication",
                    "value_driver": "Customer Satisfaction",
                    "justification": "Security baseline",
                    "estimated_effort": "M",
                },
            ],
        )

        with patch(
            "orchestrator_agent.agent_tools.backlog_primer.tools.get_engine",
            return_value=test_engine,
        ):
            first = await save_backlog_tool(save_input, tool_context=mock_context)
            second = await save_backlog_tool(save_input, tool_context=mock_context)

        assert first["success"] is True
        assert second["success"] is True
        assert second["idempotent_replay"] is True
        with SqlSession(test_engine) as session:
            rows = session.exec(
                select(UserStory).where(UserStory.product_id == 1)
            ).all()
            assert len(rows) == 1
            assert rows[0].is_superseded is False

    @pytest.mark.asyncio
    async def test_backlog_save_idempotency_ignores_active_reset_events(
        self,
    ) -> None:
        """Reset events share BACKLOG_SAVED type but must not replay save keys."""
        mock_context = MagicMock()
        mock_context.state = {}
        test_engine = create_engine("sqlite://", echo=False)
        SQLModel.metadata.create_all(test_engine)
        with SqlSession(test_engine) as session:
            session.add(Product(name="Test Product"))
            session.commit()
            session.add(
                WorkflowEvent(
                    event_type=WorkflowEventType.BACKLOG_SAVED,
                    product_id=1,
                    event_metadata=json.dumps(
                        {
                            "action": "active_backlog_reset",
                            "idempotency_key": "same-key",
                            "request_fingerprint": "sha256:reset",
                            "created_count": 2,
                        }
                    ),
                )
            )
            session.commit()

        save_input = SaveBacklogInput(
            product_id=1,
            idempotency_key="same-key",
            backlog_items=[
                {
                    "priority": 1,
                    "requirement": "New baseline",
                    "value_driver": "Strategic",
                    "justification": "Use reviewed backlog.",
                    "estimated_effort": "M",
                },
            ],
        )

        with patch(
            "orchestrator_agent.agent_tools.backlog_primer.tools.get_engine",
            return_value=test_engine,
        ):
            result = await save_backlog_tool(save_input, tool_context=mock_context)

        assert result["success"] is True
        assert result.get("idempotent_replay") is not True
        assert result["saved_count"] == 1
