# ruff: noqa: E501
# tests/test_authority_gate.py
"""
TDD Tests for the Authority Gate feature.

These tests verify that:
1. Story generation is blocked until an accepted spec authority exists.
2. If no accepted authority exists, ensure_accepted_spec_authority() triggers update_spec_and_compile_authority().
3. The spec_version_id from the accepted authority is injected into story pipeline inputs.
4. Appropriate errors are raised when spec content is missing or authority acceptance fails.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from sqlmodel import Session

from adapters.adk.prompts.specification import (
    SPEC_AUTHORITY_COMPILER_INSTRUCTIONS,
    SPEC_AUTHORITY_COMPILER_VERSION,
)
from agile_sqlmodel import (
    CompiledSpecAuthority,
    Project,
    SpecAuthorityAcceptance,
    SpecRegistry,
)
from services.contracts.specification import (
    compute_invariant_id,
    compute_prompt_hash,
)
from services.specs.authority_selection import pending_authority_fingerprint
from tests.typing_helpers import require_id
from tests.workflow.lifecycle_fixtures import seed_accepted_specification
from tools import spec_tools
from utils.spec_schemas import (
    Invariant,
    InvariantType,
    RequiredFieldParams,
    SourceMapEntry,
    SpecAuthorityCompilationFailure,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerOutput,
)

# --- Fixtures ---


@pytest.fixture
def sample_project(session: Session) -> Project:
    """Create a project for authority gate tests."""
    project = Project(
        name="Authority Gate Project",
        description="Project for authority gate tests",
        vision="Keep authority explicit",
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def _create_compiled_artifact_json() -> str:
    """Create valid compiled authority JSON for test fixtures."""
    prompt_hash = compute_prompt_hash(SPEC_AUTHORITY_COMPILER_INSTRUCTIONS)
    invariant_id = compute_invariant_id(
        "The payload must include user_id.",
        InvariantType.REQUIRED_FIELD,
    )
    invariant = Invariant(
        id=invariant_id,
        type=InvariantType.REQUIRED_FIELD,
        parameters=RequiredFieldParams(field_name="user_id"),
    )
    success = SpecAuthorityCompilationSuccess(
        scope_themes=["Scope"],
        invariants=[invariant],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[
            SourceMapEntry(
                invariant_id=invariant_id,
                excerpt="The payload must include user_id.",
                location=None,
            )
        ],
        compiler_version=SPEC_AUTHORITY_COMPILER_VERSION,
        prompt_hash=prompt_hash,
    )
    return success.model_dump_json()


def _create_spec_and_compiled_authority(
    session: Session,
    project_id: int,
    accepted: bool = False,
) -> tuple[SpecRegistry, CompiledSpecAuthority]:
    """Create a spec version with compiled authority, optionally accepted."""
    spec_content = "# Spec v1\n\n## Scope\n- Feature A\n\n## Invariants\n- The payload must include user_id."
    prompt_hash = compute_prompt_hash(SPEC_AUTHORITY_COMPILER_INSTRUCTIONS)

    spec_version = seed_accepted_specification(
        session,
        project_id=project_id,
        content=json.dumps({"specification": spec_content}),
    ).spec
    spec_hash = spec_version.spec_hash

    compiled = CompiledSpecAuthority(
        spec_version_id=require_id(spec_version.spec_version_id, "spec_version_id"),
        compiler_version=SPEC_AUTHORITY_COMPILER_VERSION,
        prompt_hash=prompt_hash,
        compiled_at=datetime.now(UTC),
        compiled_artifact_json=_create_compiled_artifact_json(),
        scope_themes=json.dumps(["Scope"]),
        invariants=json.dumps(["REQUIRED_FIELD:user_id"]),
        eligible_feature_ids=json.dumps([]),
        rejected_features=json.dumps([]),
        spec_gaps=json.dumps([]),
    )
    session.add(compiled)
    session.commit()
    session.refresh(compiled)

    if accepted:
        acceptance = SpecAuthorityAcceptance(
            project_id=project_id,
            spec_version_id=require_id(spec_version.spec_version_id, "spec_version_id"),
            status="accepted",
            policy="manual",
            decided_by="reviewer",
            decided_at=datetime.now(UTC),
            rationale="Explicitly accepted for test",
            compiler_version=SPEC_AUTHORITY_COMPILER_VERSION,
            prompt_hash=prompt_hash,
            spec_hash=spec_hash,
            pending_authority_id=compiled.authority_id,
            authority_fingerprint=pending_authority_fingerprint(compiled),
        )
        session.add(acceptance)
        session.commit()

    return spec_version, compiled


def _create_failure_artifact_json() -> str:
    """Create a compilation failure artifact JSON for testing."""
    failure = SpecAuthorityCompilationFailure(
        error="COMPILATION_FAILED",
        reason="Spec lacks mandatory sections",
        blocking_gaps=["Missing scope section", "No invariants found"],
    )
    return SpecAuthorityCompilerOutput(root=failure).model_dump_json()


def _create_spec_with_failure_authority(
    session: Session,
    project_id: int,
) -> tuple[SpecRegistry, CompiledSpecAuthority, SpecAuthorityAcceptance]:
    """Create a spec version with accepted status but a FAILURE compiled artifact."""
    spec_content = "# Bad Spec\nIncomplete content."
    prompt_hash = compute_prompt_hash(SPEC_AUTHORITY_COMPILER_INSTRUCTIONS)

    spec_version = seed_accepted_specification(
        session,
        project_id=project_id,
        content=json.dumps({"specification": spec_content}),
    ).spec
    spec_hash = spec_version.spec_hash

    # Create compiled authority with FAILURE artifact
    compiled = CompiledSpecAuthority(
        spec_version_id=require_id(spec_version.spec_version_id, "spec_version_id"),
        compiler_version=SPEC_AUTHORITY_COMPILER_VERSION,
        prompt_hash=prompt_hash,
        compiled_at=datetime.now(UTC),
        compiled_artifact_json=_create_failure_artifact_json(),  # Failure!
        scope_themes=json.dumps([]),
        invariants=json.dumps([]),
        eligible_feature_ids=json.dumps([]),
        rejected_features=json.dumps([]),
        spec_gaps=json.dumps(["Missing scope section"]),
    )
    session.add(compiled)
    session.commit()
    session.refresh(compiled)

    # Still create an acceptance record (simulating a bad state)
    acceptance = SpecAuthorityAcceptance(
        project_id=project_id,
        spec_version_id=require_id(spec_version.spec_version_id, "spec_version_id"),
        status="accepted",
        policy="manual",
        decided_by="reviewer",
        decided_at=datetime.now(UTC),
        rationale="Explicitly accepted for test",
        compiler_version=SPEC_AUTHORITY_COMPILER_VERSION,
        prompt_hash=prompt_hash,
        spec_hash=spec_hash,
        pending_authority_id=compiled.authority_id,
        authority_fingerprint=pending_authority_fingerprint(compiled),
    )
    session.add(acceptance)
    session.commit()

    return spec_version, compiled, acceptance


# =============================================================================
# TEST 1: Existing accepted authority => no spec update call
# =============================================================================


class TestAuthorityGateExistingAccepted:
    """Tests for when an accepted authority already exists."""

    def test_ensure_accepted_spec_authority_delegates_to_service_adapter(
        self,
        sample_project: Project,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Tool adapter should delegate to the service while preserving legacy seams."""
        captured: dict[str, Any] = {}

        def fake_service_ensure(**kwargs: Any) -> int:  # noqa: ANN401
            captured.update(kwargs)
            return 321

        monkeypatch.setattr(
            spec_tools,
            "_service_ensure_accepted_spec_authority",
            fake_service_ensure,
        )

        result = spec_tools.ensure_accepted_spec_authority(
            project_id=require_id(sample_project.project_id, "project_id"),
            spec_content="# Spec",
            recompile=True,
        )

        assert result == 321  # noqa: PLR2004
        assert captured["project_id"] == require_id(
            sample_project.project_id, "project_id"
        )
        assert captured["spec_content"] == "# Spec"
        assert captured["recompile"] is True
        assert captured["_update_spec_and_compile_authority"] is (
            spec_tools.update_spec_and_compile_authority
        )
        assert captured["_logger"] is spec_tools.logger

    def test_ensure_accepted_spec_authority_returns_existing_version_id(
        self, session: Session, sample_project: Project
    ) -> None:
        """When accepted authority exists, return its spec_version_id without calling update."""
        # Import here to test the function we're about to implement
        from tools.spec_tools import ensure_accepted_spec_authority  # noqa: PLC0415

        # Arrange: create accepted authority
        spec_version, _compiled = _create_spec_and_compiled_authority(
            session, require_id(sample_project.project_id, "project_id"), accepted=True
        )
        expected_spec_version_id = require_id(
            spec_version.spec_version_id, "spec_version_id"
        )

        # Act
        with patch.object(
            spec_tools, "update_spec_and_compile_authority"
        ) as mock_update:
            result = ensure_accepted_spec_authority(
                project_id=require_id(sample_project.project_id, "project_id"),
                spec_content="Some new spec content",  # Should be ignored
            )

        # Assert
        assert result == expected_spec_version_id
        mock_update.assert_not_called()

    def test_story_generation_uses_existing_accepted_spec_version_id(
        self, session: Session, sample_project: Project
    ) -> None:
        """Story generation should use existing accepted authority's spec_version_id."""
        from tools.spec_tools import ensure_accepted_spec_authority  # noqa: PLC0415

        # Arrange
        spec_version, _compiled = _create_spec_and_compiled_authority(
            session, require_id(sample_project.project_id, "project_id"), accepted=True
        )

        # Act
        spec_version_id = ensure_accepted_spec_authority(
            project_id=require_id(sample_project.project_id, "project_id"),
        )

        # Assert
        assert spec_version_id == require_id(
            spec_version.spec_version_id, "spec_version_id"
        )


# =============================================================================
# TEST 2: No accepted authority => triggers spec update/compile/accept once
# =============================================================================


class TestAuthorityGateNoAcceptedAuthority:
    """Tests for when no accepted authority exists."""

    def test_ensure_accepted_spec_authority_calls_update_when_no_accepted(
        self, session: Session, sample_project: Project
    ) -> None:
        """When no accepted authority exists, call update_spec_and_compile_authority."""
        del session
        from tools.spec_tools import ensure_accepted_spec_authority  # noqa: PLC0415

        # Arrange: no accepted authority exists (project is clean)
        mock_return = {
            "success": True,
            "accepted": True,
            "spec_version_id": 999,
            "project_id": require_id(sample_project.project_id, "project_id"),
        }

        # Act
        with patch.object(
            spec_tools, "update_spec_and_compile_authority", return_value=mock_return
        ) as mock_update:
            result = ensure_accepted_spec_authority(
                project_id=require_id(sample_project.project_id, "project_id"),
                spec_content="# New Spec\nContent here",
            )

        # Assert
        mock_update.assert_called_once()
        call_args = mock_update.call_args
        assert call_args[0][0]["project_id"] == require_id(
            sample_project.project_id, "project_id"
        )
        assert call_args[0][0]["spec_content"] == "# New Spec\nContent here"
        assert result == 999  # noqa: PLR2004

    def test_ensure_accepted_spec_authority_with_content_ref(
        self, session: Session, sample_project: Project
    ) -> None:
        """When content_ref is provided instead of spec_content, pass it through."""
        del session
        from tools.spec_tools import ensure_accepted_spec_authority  # noqa: PLC0415

        mock_return = {
            "success": True,
            "accepted": True,
            "spec_version_id": 888,
            "project_id": require_id(sample_project.project_id, "project_id"),
        }

        with patch.object(
            spec_tools, "update_spec_and_compile_authority", return_value=mock_return
        ) as mock_update:
            result = ensure_accepted_spec_authority(
                project_id=require_id(sample_project.project_id, "project_id"),
                content_ref="specs/my_spec.md",
            )

        mock_update.assert_called_once()
        call_args = mock_update.call_args
        assert call_args[0][0]["content_ref"] == "specs/my_spec.md"
        assert result == 888  # noqa: PLR2004

    def test_ensure_accepted_spec_authority_calls_update_exactly_once(
        self, session: Session, sample_project: Project
    ) -> None:
        """Update should be called exactly once even on repeated calls (after first success)."""
        from tools.spec_tools import ensure_accepted_spec_authority  # noqa: PLC0415

        # Arrange: first call creates accepted authority
        mock_return = {
            "success": True,
            "accepted": True,
            "spec_version_id": 777,
            "project_id": require_id(sample_project.project_id, "project_id"),
        }

        with patch.object(
            spec_tools, "update_spec_and_compile_authority", return_value=mock_return
        ) as mock_update:
            # First call
            first_result = ensure_accepted_spec_authority(
                project_id=require_id(sample_project.project_id, "project_id"),
                spec_content="# Spec",
            )

            # Mock the accepted authority now exists (simulating DB side effect)
            _create_spec_and_compiled_authority(
                session,
                require_id(sample_project.project_id, "project_id"),
                accepted=True,
            )

            # Second call - should find existing and not call update
            second_result = ensure_accepted_spec_authority(  # noqa: F841
                project_id=require_id(sample_project.project_id, "project_id"),
                spec_content="# Different spec",  # Should be ignored
            )

        # First call should have called update
        assert mock_update.call_count == 1
        assert first_result == 777  # noqa: PLR2004


# =============================================================================
# TEST 3: No accepted authority + no spec input => hard error
# =============================================================================


class TestAuthorityGateMissingSpecContent:
    """Tests for error handling when spec content is missing."""

    def test_ensure_accepted_spec_authority_raises_without_spec_content(
        self, session: Session, sample_project: Project
    ) -> None:
        """When no accepted authority exists and no spec_content/content_ref, raise error."""
        del session
        from tools.spec_tools import ensure_accepted_spec_authority  # noqa: PLC0415

        # Arrange: no accepted authority, no spec content provided

        # Act & Assert
        with pytest.raises(RuntimeError) as exc:
            ensure_accepted_spec_authority(
                project_id=require_id(sample_project.project_id, "project_id"),
                # No spec_content or content_ref provided
            )

        message = str(exc.value).lower()
        assert "spec" in message
        assert any(
            word in message for word in ["content", "file", "provide", "missing"]
        )

    def test_ensure_accepted_spec_authority_error_message_is_helpful(
        self, session: Session, sample_project: Project
    ) -> None:
        """Error message should guide user to provide spec content or file path."""
        del session
        from tools.spec_tools import ensure_accepted_spec_authority  # noqa: PLC0415

        with pytest.raises(RuntimeError) as exc:
            ensure_accepted_spec_authority(
                project_id=require_id(sample_project.project_id, "project_id")
            )

        message = str(exc.value)
        # Should mention what the user needs to do
        assert "spec" in message.lower()


# =============================================================================
# TEST 4: Update spec returns not accepted or failure => hard error
# =============================================================================


class TestAuthorityGateUpdateFailure:
    """Tests for error handling when update_spec_and_compile_authority fails."""

    def test_ensure_accepted_spec_authority_raises_on_update_failure(
        self, session: Session, sample_project: Project
    ) -> None:
        """When update returns success=False, raise RuntimeError."""
        del session
        from tools.spec_tools import ensure_accepted_spec_authority  # noqa: PLC0415

        mock_return = {
            "success": False,
            "error": "Compilation failed due to invalid spec format",
        }

        with (
            patch.object(
                spec_tools,
                "update_spec_and_compile_authority",
                return_value=mock_return,
            ),
            pytest.raises(RuntimeError) as exc,
        ):
            ensure_accepted_spec_authority(
                project_id=require_id(sample_project.project_id, "project_id"),
                spec_content="# Invalid spec",
            )

        message = str(exc.value).lower()
        assert "failed" in message or "error" in message

    def test_ensure_accepted_spec_authority_raises_on_not_accepted(
        self, session: Session, sample_project: Project
    ) -> None:
        """When update returns accepted=False, raise RuntimeError."""
        del session
        from tools.spec_tools import ensure_accepted_spec_authority  # noqa: PLC0415

        mock_return = {
            "success": True,
            "accepted": False,
            "spec_version_id": 123,
            "message": "Authority compiled but not accepted",
        }

        with (
            patch.object(
                spec_tools,
                "update_spec_and_compile_authority",
                return_value=mock_return,
            ),
            pytest.raises(RuntimeError) as exc,
        ):
            ensure_accepted_spec_authority(
                project_id=require_id(sample_project.project_id, "project_id"),
                spec_content="# Spec",
            )

        message = str(exc.value).lower()
        assert "accepted" in message or "not accepted" in message.replace(" ", "")

    def test_ensure_accepted_spec_authority_does_not_call_story_gen_on_failure(
        self, session: Session, sample_project: Project
    ) -> None:
        """Story generation should not proceed if authority gate fails."""
        del session
        from tools.spec_tools import ensure_accepted_spec_authority  # noqa: PLC0415

        mock_return = {"success": False, "error": "DB error"}

        with patch.object(  # noqa: SIM117
            spec_tools, "update_spec_and_compile_authority", return_value=mock_return
        ):
            # The function should raise before any story generation could happen
            with pytest.raises(RuntimeError):
                ensure_accepted_spec_authority(
                    project_id=require_id(sample_project.project_id, "project_id"),
                    spec_content="# Spec",
                )


# =============================================================================
# TEST 5: Implementation detail - spec_version_id injection
# =============================================================================


class TestSpecVersionIdInjection:
    """Tests verifying spec_version_id is properly injected into pipeline inputs."""

    def test_returned_spec_version_id_is_valid_integer(
        self, session: Session, sample_project: Project
    ) -> None:
        """ensure_accepted_spec_authority should return a valid integer spec_version_id."""
        from tools.spec_tools import ensure_accepted_spec_authority  # noqa: PLC0415

        # Arrange: create accepted authority
        spec_version, _compiled = _create_spec_and_compiled_authority(
            session, require_id(sample_project.project_id, "project_id"), accepted=True
        )

        # Act
        result = ensure_accepted_spec_authority(
            project_id=require_id(sample_project.project_id, "project_id")
        )

        # Assert
        assert isinstance(result, int)
        assert result > 0
        assert result == require_id(spec_version.spec_version_id, "spec_version_id")

    def test_recompile_flag_is_passed_through(
        self, session: Session, sample_project: Project
    ) -> None:
        """Recompile flag should be passed to update_spec_and_compile_authority."""
        del session
        from tools.spec_tools import ensure_accepted_spec_authority  # noqa: PLC0415

        mock_return = {
            "success": True,
            "accepted": True,
            "spec_version_id": 555,
        }

        with patch.object(
            spec_tools, "update_spec_and_compile_authority", return_value=mock_return
        ) as mock_update:
            ensure_accepted_spec_authority(
                project_id=require_id(sample_project.project_id, "project_id"),
                spec_content="# Spec",
                recompile=True,
            )

        call_args = mock_update.call_args
        assert call_args[0][0]["recompile"] is True


# =============================================================================
# TEST 6: Accepted authority with FAILURE artifact => must recompile
# =============================================================================


class TestAuthorityGateFailureArtifact:
    """Tests for handling accepted authorities that have compilation FAILURE artifacts."""

    def test_ensure_accepted_spec_authority_ignores_failure_artifact(
        self, session: Session, sample_project: Project
    ) -> None:
        """
        When accepted authority exists but compiled_artifact_json is a FAILURE envelope,.

        the gate should NOT return early - it should trigger recompilation.
        """
        from tools.spec_tools import ensure_accepted_spec_authority  # noqa: PLC0415

        # Arrange: Create an accepted authority with a FAILURE artifact
        _spec_version, compiled, acceptance = _create_spec_with_failure_authority(
            session, require_id(sample_project.project_id, "project_id")
        )

        # Verify fixture setup: we have an acceptance with a failure artifact
        assert acceptance.status == "accepted"
        assert compiled.compiled_artifact_json is not None
        assert "COMPILATION_FAILED" in compiled.compiled_artifact_json

        # Mock update_spec_and_compile_authority to track if it's called
        mock_return = {
            "success": True,
            "accepted": True,
            "spec_version_id": 999,  # New version from recompilation
        }

        with patch.object(
            spec_tools, "update_spec_and_compile_authority", return_value=mock_return
        ) as mock_update:
            result = ensure_accepted_spec_authority(
                project_id=require_id(sample_project.project_id, "project_id"),
                spec_content="# Valid Spec\nWith proper content.",
            )

        # Assert: update SHOULD have been called because the artifact was a failure
        mock_update.assert_called_once()
        assert result == 999  # The new version ID from recompilation  # noqa: PLR2004

    def test_failure_artifact_requires_spec_content_for_recompilation(
        self, session: Session, sample_project: Project
    ) -> None:
        """
        When accepted authority has FAILURE artifact and no spec_content is provided,.

        should raise an error since we can't recompile without spec content.
        """
        from tools.spec_tools import ensure_accepted_spec_authority  # noqa: PLC0415

        # Arrange: Create an accepted authority with a FAILURE artifact
        _create_spec_with_failure_authority(
            session, require_id(sample_project.project_id, "project_id")
        )

        # Act & Assert: without spec_content, we can't proceed
        with pytest.raises(RuntimeError):
            ensure_accepted_spec_authority(
                project_id=require_id(sample_project.project_id, "project_id"),
                # No spec_content - can't recompile
            )


# =============================================================================
# TEST 7: Authority gate logging
# =============================================================================


class TestAuthorityGateLogging:
    """Tests for structured logging in authority gate paths."""

    def test_authority_gate_logs_reuse(
        self,
        session: Session,
        sample_project: Project,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Reuse branch should emit authority_gate.reuse."""
        from tools.spec_tools import ensure_accepted_spec_authority  # noqa: PLC0415

        caplog.set_level(logging.INFO, logger="tools.spec_tools")

        _create_spec_and_compiled_authority(
            session, require_id(sample_project.project_id, "project_id"), accepted=True
        )

        ensure_accepted_spec_authority(
            project_id=require_id(sample_project.project_id, "project_id")
        )

        reuse_records = [
            record
            for record in caplog.records
            if record.message == "authority_gate.pass"
        ]
        assert reuse_records
        assert reuse_records[0].__dict__.get("project_id") == require_id(
            sample_project.project_id, "project_id"
        )

    def test_authority_gate_logs_compile(
        self,
        session: Session,
        sample_project: Project,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Compile branch should emit authority_gate.compile."""
        del session
        from tools.spec_tools import ensure_accepted_spec_authority  # noqa: PLC0415

        caplog.set_level(logging.INFO, logger="tools.spec_tools")

        mock_return = {
            "success": True,
            "accepted": True,
            "spec_version_id": 444,
            "project_id": require_id(sample_project.project_id, "project_id"),
        }

        with patch.object(
            spec_tools, "update_spec_and_compile_authority", return_value=mock_return
        ):
            ensure_accepted_spec_authority(
                project_id=require_id(sample_project.project_id, "project_id"),
                spec_content="# Spec",
            )

        compile_records = [
            record
            for record in caplog.records
            if record.message == "authority_gate.updated"
        ]
        assert compile_records
        assert compile_records[0].__dict__.get("project_id") == require_id(
            sample_project.project_id, "project_id"
        )

    def test_authority_gate_logs_fail(
        self,
        session: Session,
        sample_project: Project,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Failure branch should emit authority_gate.fail."""
        del session
        from tools.spec_tools import ensure_accepted_spec_authority  # noqa: PLC0415

        caplog.set_level(logging.INFO, logger="tools.spec_tools")

        with pytest.raises(RuntimeError):
            ensure_accepted_spec_authority(
                project_id=require_id(sample_project.project_id, "project_id")
            )

        fail_records = [
            record
            for record in caplog.records
            if record.message == "authority_gate.fail_no_source"
        ]
        assert fail_records
        assert fail_records[0].__dict__.get("reason") == "missing_inputs"
