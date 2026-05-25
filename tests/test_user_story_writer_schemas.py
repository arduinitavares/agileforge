"""TDD tests for User Story Writer schemas."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from orchestrator_agent.agent_tools.user_story_writer_tool.schemes import (
    UserStoryItem,
    UserStoryWriterInput,
    UserStoryWriterOutput,
)

# ---------------------------------------------------------------------------
# UserStoryItem
# ---------------------------------------------------------------------------


class TestUserStoryItem:
    """Tests for the UserStoryItem schema."""

    def test_valid_high_score_item(self) -> None:
        """Verify valid high score item."""
        item = UserStoryItem(
            story_title="Login via email",
            statement=(
                "As a registered member, I want to log in with my email, "
                "so that I can access my dashboard."
            ),
            acceptance_criteria=[
                "Verify that the login form accepts email and password.",
                "Verify that invalid credentials show an error message.",
                "Ensure that response time is under 500ms.",
            ],
            invest_score="High",
            estimated_effort="S",
            produced_artifacts=["dashboard"],
        )
        assert item.invest_score == "High"
        assert item.decomposition_warning is None

    def test_accepts_dependency_candidate(self) -> None:
        """Verify user story items can propose dependency candidates."""
        item = UserStoryItem(
            story_title="Generate recommendation",
            statement=(
                "As a manager, I want a generated recommendation, "
                "so that I can choose a squad before lock."
            ),
            acceptance_criteria=["Verify recommendation uses captured market data."],
            invest_score="High",
            estimated_effort="M",
            dependency_candidates=[
                {
                    "prerequisite_ref": "Capture market data",
                    "reason": "Recommendation needs market data before generation.",
                    "confidence": "explicit",
                }
            ],
        )

        assert item.dependency_candidates[0].prerequisite_ref == "Capture market data"
        assert item.dependency_candidates[0].confidence == "explicit"

    def test_rejects_invalid_dependency_candidate_confidence(self) -> None:
        """Verify dependency candidate confidence is closed-set."""
        with pytest.raises(ValidationError):
            UserStoryItem(
                story_title="Generate recommendation",
                statement=(
                    "As a manager, I want a generated recommendation, "
                    "so that I can choose a squad before lock."
                ),
                acceptance_criteria=[
                    "Verify recommendation uses captured market data."
                ],
                invest_score="High",
                estimated_effort="M",
                dependency_candidates=[
                    {
                        "prerequisite_ref": "Capture market data",
                        "reason": "Recommendation needs market data first.",
                        "confidence": "maybe",
                    }
                ],
            )

    def test_valid_medium_score_item(self) -> None:
        """Verify valid medium score item."""
        item = UserStoryItem(
            story_title="Export CSV report",
            statement=(
                "As a manager, I want to export a CSV report, "
                "so that I can analyze data offline."
            ),
            acceptance_criteria=["Verify that the CSV downloads correctly."],
            invest_score="Medium",
            estimated_effort="M",
            produced_artifacts=["csv_report"],
        )
        assert item.invest_score == "Medium"
        assert item.decomposition_warning is None

    def test_valid_low_score_with_warning(self) -> None:
        """Verify valid low score with warning."""
        item = UserStoryItem(
            story_title="Batch data migration",
            statement=(
                "As a data engineer, I want to migrate legacy records, "
                "so that the new system has historical data."
            ),
            acceptance_criteria=["Verify that all records are transferred."],
            invest_score="Low",
            estimated_effort="XL",
            produced_artifacts=["legacy_data"],
            decomposition_warning="Hard dependency on legacy API availability.",
        )
        assert item.invest_score == "Low"
        assert item.decomposition_warning is not None
        assert "Hard dependency" in item.decomposition_warning

    def test_rejects_invalid_statement_missing_as_a(self) -> None:
        """Verify rejects invalid statement missing as a."""
        with pytest.raises(
            ValidationError, match="Statement must precisely start with"
        ):
            UserStoryItem(
                story_title="Bad story",
                statement="The user wants to log in so that they can access the app.",
                acceptance_criteria=["Verify login works."],
                invest_score="High",
                estimated_effort="S",
                produced_artifacts=[],
            )

    def test_rejects_invalid_statement_missing_i_want(self) -> None:
        """Verify rejects invalid statement missing i want."""
        with pytest.raises(ValidationError, match="I want"):
            UserStoryItem(
                story_title="Bad story",
                statement="As a user, logging in, so that I can access my account.",
                acceptance_criteria=["Verify login works."],
                invest_score="High",
                estimated_effort="S",
                produced_artifacts=[],
            )

    def test_rejects_invalid_statement_missing_so_that(self) -> None:
        """Verify rejects invalid statement missing so that."""
        with pytest.raises(ValidationError, match="so that"):
            UserStoryItem(
                story_title="Bad story",
                statement="As a user, I want to log in.",
                acceptance_criteria=["Verify login works."],
                invest_score="High",
                estimated_effort="S",
                produced_artifacts=[],
            )

    def test_coerces_high_score_with_warning_to_low(self) -> None:
        """Verify coerces high score with warning to low."""
        item = UserStoryItem(
            story_title="Some story title",
            statement="As a user, I want feature X, so that I get benefit Y.",
            acceptance_criteria=["Verify X works."],
            invest_score="High",
            estimated_effort="S",
            produced_artifacts=[],
            decomposition_warning="Dependent on open design decisions being confirmed.",
        )
        assert item.invest_score == "Low"
        assert item.decomposition_warning == (
            "Dependent on open design decisions being confirmed."
        )

    def test_coerces_medium_score_with_warning_to_low(self) -> None:
        """Verify coerces medium score with warning to low."""
        item = UserStoryItem(
            story_title="Some story title",
            statement="As a user, I want feature X, so that I get benefit Y.",
            acceptance_criteria=["Verify X works."],
            invest_score="Medium",
            estimated_effort="S",
            produced_artifacts=[],
            decomposition_warning="Pending confirmation of integration boundaries.",
        )
        assert item.invest_score == "Low"
        assert item.decomposition_warning == (
            "Pending confirmation of integration boundaries."
        )

    def test_accepts_high_score_with_known_placeholder_warning(self) -> None:
        """Verify accepts high score with known placeholder warning."""
        item = UserStoryItem(
            story_title="Some story title",
            statement="As a user, I want feature X, so that I get benefit Y.",
            acceptance_criteria=["Verify X works."],
            invest_score="High",
            estimated_effort="S",
            produced_artifacts=[],
            decomposition_warning="Only include this key if score is Low",
        )
        assert item.decomposition_warning is None

    def test_rejects_low_score_without_warning(self) -> None:
        """Verify rejects low score without warning."""
        with pytest.raises(
            ValidationError, match="required when invest_score is 'Low'"
        ):
            UserStoryItem(
                story_title="Some story title",
                statement="As a user, I want feature X, so that I get benefit Y.",
                acceptance_criteria=["Verify X works."],
                invest_score="Low",
                estimated_effort="S",
                produced_artifacts=[],
            )

    def test_rejects_empty_acceptance_criteria(self) -> None:
        """Verify rejects empty acceptance criteria."""
        with pytest.raises(ValidationError):
            UserStoryItem(
                story_title="Some story title",
                statement="As a user, I want feature X, so that I get benefit Y.",
                acceptance_criteria=[],
                invest_score="High",
                estimated_effort="S",
                produced_artifacts=[],
            )

    def test_json_round_trip(self) -> None:
        """Verify json round trip."""
        item = UserStoryItem(
            story_title="Upload CSV file",
            statement=(
                "As a data analyst, I want to upload a CSV file, "
                "so that I can visualize data."
            ),
            acceptance_criteria=[
                "Verify that CSV files up to 10MB are accepted.",
                "Ensure that upload completes within 3 seconds.",
            ],
            invest_score="Medium",
            estimated_effort="S",
            produced_artifacts=[],
        )
        dumped = item.model_dump_json()
        loaded = UserStoryItem.model_validate_json(dumped)
        assert loaded.story_title == item.story_title
        assert loaded.acceptance_criteria == item.acceptance_criteria
        assert loaded.decomposition_warning is None

    def test_rejects_extra_fields(self) -> None:
        """Verify rejects extra fields."""
        with pytest.raises(ValidationError):
            UserStoryItem.model_validate(
                {
                    "story_title": "Valid title here",
                    "statement": (
                        "As a user, I want feature X, so that I get benefit Y."
                    ),
                    "acceptance_criteria": ["Verify X."],
                    "invest_score": "High",
                    "estimated_effort": "S",
                    "produced_artifacts": [],
                    "unknown_field": "bad",
                }
            )


# ---------------------------------------------------------------------------
# UserStoryWriterInput
# ---------------------------------------------------------------------------


class TestUserStoryWriterInput:
    """Tests for the input schema."""

    def test_valid_input(self) -> None:
        """Verify valid input."""
        inp = UserStoryWriterInput(
            parent_requirement="Offline Dataset Ingestion",
            requirement_context="Must support CSV and Parquet formats.",
            technical_spec="Max file size 500MB. Processing within 60s.",
            compiled_authority="ISO 27001 compliance required.",
        )
        assert inp.parent_requirement == "Offline Dataset Ingestion"

    def test_accepts_extra_fields_for_adk_compat(self) -> None:
        """ADK input schemas must NOT use extra='forbid'."""
        inp = UserStoryWriterInput.model_validate(
            {
                "parent_requirement": "Valid requirement name",
                "requirement_context": "context",
                "technical_spec": "spec",
                "compiled_authority": "auth",
                "extra_field": "tolerated",
            }
        )
        assert inp.parent_requirement == "Valid requirement name"

    def test_json_round_trip(self) -> None:
        """Verify json round trip."""
        payload: dict[str, Any] = {
            "parent_requirement": "User Authentication",
            "requirement_context": "Critical for security.",
            "technical_spec": "OAuth2 + MFA required.",
            "compiled_authority": "SOC2 Type II.",
        }
        model = UserStoryWriterInput.model_validate(payload)
        dumped = model.model_dump_json()
        loaded = UserStoryWriterInput.model_validate_json(dumped)
        assert loaded.parent_requirement == payload["parent_requirement"]


# ---------------------------------------------------------------------------
# UserStoryWriterOutput
# ---------------------------------------------------------------------------


class TestUserStoryWriterOutput:
    """Tests for the output schema."""

    def _make_valid_story(self, **overrides: Any) -> dict[str, Any]:  # noqa: ANN401
        """Helper: build a valid UserStoryItem dict."""  # noqa: D401
        base: dict[str, Any] = {
            "story_title": "Upload CSV file",
            "statement": (
                "As a data engineer, I want to upload a CSV file, "
                "so that raw data enters the system."
            ),
            "acceptance_criteria": ["Verify that CSV is parsed correctly."],
            "invest_score": "High",
            "estimated_effort": "S",
            "produced_artifacts": ["csv_upload"],
        }
        base.update(overrides)
        return base

    def test_valid_complete_output(self) -> None:
        """Verify valid complete output."""
        out = UserStoryWriterOutput(
            parent_requirement="Offline Dataset Ingestion",
            user_stories=[UserStoryItem(**self._make_valid_story())],
            is_complete=True,
        )
        assert out.is_complete is True
        assert len(out.user_stories) == 1
        assert out.clarifying_questions == []

    def test_valid_incomplete_output_with_questions(self) -> None:
        """Verify valid incomplete output with questions."""
        out = UserStoryWriterOutput(
            parent_requirement="Dashboard Analytics",
            user_stories=[
                UserStoryItem(
                    story_title="View sales chart",
                    statement=(
                        "As a manager, I want to view a sales chart, "
                        "so that I can track revenue."
                    ),
                    acceptance_criteria=["Verify chart renders."],
                    invest_score="High",
                    estimated_effort="S",
                    produced_artifacts=["sales_chart"],
                ),
            ],
            is_complete=False,
            clarifying_questions=["What chart types are required?"],
        )
        assert out.is_complete is False
        assert len(out.clarifying_questions) == 1

    def test_rejects_empty_stories(self) -> None:
        """Verify rejects empty stories."""
        with pytest.raises(ValidationError):
            UserStoryWriterOutput(
                parent_requirement="Something valid",
                user_stories=[],
                is_complete=True,
            )

    def test_rejects_extra_fields(self) -> None:
        """Verify rejects extra fields."""
        with pytest.raises(ValidationError):
            UserStoryWriterOutput.model_validate(
                {
                    "parent_requirement": "Valid requirement",
                    "user_stories": [UserStoryItem(**self._make_valid_story())],
                    "is_complete": True,
                    "bonus": "bad",
                }
            )

    def test_json_round_trip(self) -> None:
        """Verify json round trip."""
        out = UserStoryWriterOutput(
            parent_requirement="Authentication",
            user_stories=[
                UserStoryItem(
                    story_title="Login with email",
                    statement=(
                        "As a member, I want to log in, so that I access my account."
                    ),
                    acceptance_criteria=["Verify login succeeds."],
                    invest_score="High",
                    estimated_effort="S",
                    produced_artifacts=[],
                ),
            ],
            is_complete=True,
        )
        dumped = out.model_dump_json()
        loaded = UserStoryWriterOutput.model_validate_json(dumped)
        assert loaded.parent_requirement == "Authentication"
        assert len(loaded.user_stories) == 1
        # Verify no extra text (markdown fences, commentary)
        parsed = json.loads(dumped)
        assert "```" not in dumped
        assert set(parsed.keys()) == {
            "parent_requirement",
            "user_stories",
            "is_complete",
            "clarifying_questions",
        }
