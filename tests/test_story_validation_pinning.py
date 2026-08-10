# tests/test_story_validation_pinning.py
"""
Tests for Story Validation Pinning v2.

These tests validate that:
- Validation REQUIRES explicit spec_version_id (no defaults)
- Validation fails fast if spec is not compiled
- Evidence is ALWAYS persisted (pass or fail)
- accepted_spec_version_id is only set on pass
- Input hashing is deterministic
- Wrong spec_version_id fails deterministically
"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import Engine
from sqlmodel import Session

from agile_sqlmodel import (
    CompiledSpecAuthority,
    Project,
    SpecAuthorityAcceptance,
    SpecRegistry,
    UserStory,
)
from models.core import Epic, Feature, Theme
from services.agent_workbench.authority_projection import (
    pending_authority_fingerprint,
)
from tests.workflow.lifecycle_fixtures import seed_accepted_specification
from tools import spec_tools
from tools.spec_tools import (
    VALIDATOR_VERSION,
    ValidateStoryInput,
    compile_spec_authority,
    validate_story_with_spec_authority,
)
from utils.spec_schemas import (
    ForbiddenCapabilityParams,
    Invariant,
    InvariantType,
    SourceMapEntry,
    SpecAuthorityCompilationSuccess,
    ValidationEvidence,
)


def _require_id(value: int | None, label: str) -> int:
    """Narrow an optional persisted ID for static and runtime safety."""
    assert value is not None, f"{label} should be persisted"
    return value


def _require_story(session: Session, story_id: int | None) -> UserStory:
    """Fetch a persisted story and narrow away None."""
    persisted_story = session.get(UserStory, _require_id(story_id, "story_id"))
    assert persisted_story is not None
    return persisted_story


def _load_validation_evidence(story: UserStory | None) -> dict[str, Any]:
    """Load persisted validation evidence from a non-null story."""
    assert story is not None
    evidence_json = story.validation_evidence
    assert evidence_json is not None
    return cast("dict[str, Any]", json.loads(evidence_json))


def _fake_compilation_artifact() -> SpecAuthorityCompilationSuccess:
    """Return a deterministic compiled authority artifact for tests."""
    return SpecAuthorityCompilationSuccess(
        scope_themes=["API", "Auth"],
        domain=None,
        invariants=[
            Invariant(
                id="INV-0000000000000001",
                type=InvariantType.FORBIDDEN_CAPABILITY,
                parameters=ForbiddenCapabilityParams(capability="redis"),
            )
        ],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[
            SourceMapEntry(
                invariant_id="INV-0000000000000001",
                excerpt="Auth token required",
                location="spec:line:1",
            )
        ],
        compiler_version="3.0.0",
        prompt_hash="a" * 64,
    )


def _accepted_spec(
    session: Session,
    *,
    project_id: int,
    content: str,
) -> SpecRegistry:
    """Persist an accepted specification through the current lifecycle."""
    return seed_accepted_specification(
        session,
        project_id=project_id,
        content=json.dumps({"specification": content}),
    ).spec


def _create_feature_hierarchy(
    session: Session,
    *,
    project_id: int,
    prefix: str,
    detail: str,
) -> Feature:
    """Create theme/epic/feature records for a test project."""
    theme = Theme(
        project_id=project_id,
        title=f"{prefix} Theme",
        description=f"Theme for {detail}",
    )
    session.add(theme)
    session.commit()
    session.refresh(theme)

    epic = Epic(
        theme_id=_require_id(theme.theme_id, f"{prefix} theme_id"),
        title=f"{prefix} Epic",
        summary=f"Epic for {detail}",
    )
    session.add(epic)
    session.commit()
    session.refresh(epic)

    feature = Feature(
        epic_id=_require_id(epic.epic_id, f"{prefix} epic_id"),
        title=f"{prefix} Feature",
        description=f"Feature for {detail}",
    )
    session.add(feature)
    session.commit()
    session.refresh(feature)
    return feature


@pytest.fixture
def sample_project(session: Session, engine: Engine) -> Project:
    """Create a project for testing."""
    spec_tools.engine = engine

    project = Project(
        name="Validation Test Project",
        description="Project for validation pinning tests",
        vision="Test validation pinning",
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


@pytest.fixture
def compiled_spec(session: Session, sample_project: Project) -> SpecRegistry:
    """Create a registered, approved, and compiled spec version."""
    spec_content = """
# Test Specification

## Requirements
- All stories MUST have acceptance criteria
- Stories MUST follow the "As a [persona]" format
- Export formats: JSON only

## Invariants
- Auth token required for all operations
- Maximum 10 items per page
"""
    spec = _accepted_spec(
        session,
        project_id=_require_id(sample_project.project_id, "project_id"),
        content=spec_content,
    )
    spec_version_id = _require_id(spec.spec_version_id, "spec_version_id")

    with patch(
        "tools.spec_tools._extract_spec_authority_llm",
        return_value=_fake_compilation_artifact(),
    ):
        compile_result = compile_spec_authority(
            {"spec_version_id": spec_version_id},
            tool_context=None,
        )

    authority = session.get(CompiledSpecAuthority, compile_result["authority_id"])
    assert authority is not None
    session.add(
        SpecAuthorityAcceptance(
            project_id=_require_id(sample_project.project_id, "project_id"),
            spec_version_id=spec_version_id,
            status="accepted",
            policy="test",
            decided_by="test_reviewer",
            compiler_version=authority.compiler_version,
            prompt_hash=authority.prompt_hash,
            spec_hash=spec.spec_hash,
            pending_authority_id=authority.authority_id,
            authority_fingerprint=pending_authority_fingerprint(authority),
        )
    )
    session.commit()
    session.refresh(spec)
    return spec


@pytest.fixture
def sample_story(session: Session, sample_project: Project) -> UserStory:
    """Create a user story for testing (with full hierarchy)."""
    project_id = _require_id(sample_project.project_id, "sample_project.project_id")
    feature = _create_feature_hierarchy(
        session,
        project_id=project_id,
        prefix="Test",
        detail="validation tests",
    )

    story = UserStory(
        project_id=project_id,
        feature_id=_require_id(feature.feature_id, "test feature_id"),
        title="As a user, I want to export data",
        story_description=(
            "As a user, I want to export my data in JSON format so I can use it elsewhere."  # noqa: E501
        ),
        acceptance_criteria=(
            "Given I have data, When I click export, Then I receive a JSON file"
        ),
    )
    session.add(story)
    session.commit()
    session.refresh(story)
    return story


class TestFailFastWithoutSpecVersionId:
    """Tests that validation fails immediately without spec_version_id."""

    def test_validation_tool_delegates_to_story_validation_service(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify validation tool delegates to story validation service."""
        expected = {"success": True, "passed": True, "message": "from service"}
        captured = {}

        def fake_service_validate(params: object, **kwargs: object) -> object:
            captured["params"] = params
            captured["kwargs"] = kwargs
            return expected

        monkeypatch.setattr(
            spec_tools,
            "_service_validate_story_with_spec_authority",
            fake_service_validate,
            raising=False,
        )

        result = validate_story_with_spec_authority(
            {"story_id": 1, "spec_version_id": 2},
            tool_context=None,
        )

        assert result is expected
        assert captured["params"] == {"story_id": 1, "spec_version_id": 2}
        assert captured["kwargs"]["tool_context"] is None
        assert (
            captured["kwargs"]["compute_story_input_hash_fn"]
            is spec_tools._compute_story_input_hash
        )
        assert (
            captured["kwargs"]["render_invariant_summary_fn"]
            is spec_tools._render_invariant_summary
        )
        assert (
            captured["kwargs"]["run_structural_story_checks"]
            is spec_tools._run_structural_story_checks
        )
        assert (
            captured["kwargs"]["run_llm_spec_validation"]
            is spec_tools._run_llm_spec_validation
        )
        assert (
            captured["kwargs"]["run_deterministic_alignment_checks"]
            is spec_tools._run_deterministic_alignment_checks
        )

    def test_resolve_default_validation_mode_wrapper_delegates_to_service(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify resolve default validation mode wrapper delegates to service."""
        called = {}

        def fake_service_resolver() -> str:
            called["value"] = True
            return "llm"

        monkeypatch.setattr(
            spec_tools,
            "_service_resolve_default_validation_mode",
            fake_service_resolver,
            raising=False,
        )

        assert spec_tools._resolve_default_validation_mode() == "llm"
        assert called["value"] is True

    def test_persist_validation_evidence_wrapper_delegates_to_service(
        self, monkeypatch: pytest.MonkeyPatch, session: Session, sample_story: UserStory
    ) -> None:
        """Verify persist validation evidence wrapper delegates to service."""
        captured = {}

        def fake_service_persist(
            session_arg: object,
            story_arg: object,
            evidence_arg: object,
            passed_arg: object,
        ) -> None:
            captured["session"] = session_arg
            captured["story"] = story_arg
            captured["evidence"] = evidence_arg
            captured["passed"] = passed_arg

        monkeypatch.setattr(
            spec_tools,
            "_service_persist_validation_evidence",
            fake_service_persist,
            raising=False,
        )

        evidence = ValidationEvidence(
            spec_version_id=1,
            validated_at=datetime.now(UTC),
            passed=True,
            rules_checked=["SPEC_VERSION_EXISTS"],
            invariants_checked=[],
            evaluated_invariant_ids=[],
            finding_invariant_ids=[],
            failures=[],
            warnings=[],
            alignment_warnings=[],
            alignment_failures=[],
            validator_version=VALIDATOR_VERSION,
            input_hash="abc123",
        )

        spec_tools._persist_validation_evidence(session, sample_story, evidence, True)

        assert captured["session"] is session
        assert captured["story"] is sample_story
        assert captured["evidence"] is evidence
        assert captured["passed"] is True

    def test_compute_story_input_hash_wrapper_delegates_to_service(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify compute story input hash wrapper delegates to service."""
        called = {}

        def fake_service_hash(story_arg: object) -> str:
            called["story"] = story_arg
            return "service-hash"

        monkeypatch.setattr(
            spec_tools,
            "_service_compute_story_input_hash",
            fake_service_hash,
            raising=False,
        )

        story = SimpleNamespace(
            title="Story",
            story_description="Description",
            acceptance_criteria="Criteria",
        )

        assert spec_tools._compute_story_input_hash(story) == "service-hash"
        assert called["story"] is story

    def test_validation_requires_spec_version_id_in_schema(self) -> None:
        """Input schema requires spec_version_id (no default)."""
        with pytest.raises(ValidationError) as exc_info:
            ValidateStoryInput.model_validate({"story_id": 1})

        errors = exc_info.value.errors()
        assert any("spec_version_id" in str(e) for e in errors)

    def test_validation_rejects_none_spec_version_id(self) -> None:
        """spec_version_id=None is rejected."""
        with pytest.raises(ValidationError):
            ValidateStoryInput.model_validate({"story_id": 1, "spec_version_id": None})


class TestFailFastIfNotCompiled:
    """Tests that validation fails if spec is not compiled."""

    def test_validation_fails_for_nonexistent_spec_version(
        self, sample_story: UserStory, engine: Engine
    ) -> None:
        """Clear error when spec_version_id doesn't exist."""
        spec_tools.engine = engine

        result = validate_story_with_spec_authority(
            {"story_id": sample_story.story_id, "spec_version_id": 99999},
            tool_context=None,
        )

        assert result["success"] is False
        assert "not found" in result["error"].lower()

    def test_validation_fails_for_approved_but_uncompiled_spec(
        self,
        session: Session,
        sample_project: Project,
        sample_story: UserStory,
        engine: Engine,
    ) -> None:
        """Approved but uncompiled spec fails with clear message."""
        spec_tools.engine = engine

        spec = _accepted_spec(
            session,
            project_id=_require_id(sample_project.project_id, "project_id"),
            content="Approved but not compiled",
        )
        spec_version_id = _require_id(spec.spec_version_id, "spec_version_id")

        result = validate_story_with_spec_authority(
            {"story_id": sample_story.story_id, "spec_version_id": spec_version_id},
            tool_context=None,
        )

        assert result["success"] is False
        assert "not compiled" in result["error"].lower()


class TestEvidencePersistence:
    """Tests that validation evidence is ALWAYS persisted."""

    def test_evidence_persisted_on_validation_pass(
        self,
        session: Session,
        sample_story: UserStory,
        compiled_spec: SpecRegistry,
        engine: Engine,
    ) -> None:
        """Evidence is stored when validation passes."""
        spec_tools.engine = engine

        result = validate_story_with_spec_authority(
            {
                "story_id": sample_story.story_id,
                "spec_version_id": compiled_spec.spec_version_id,
            },
            tool_context=None,
        )

        session.expire(sample_story)
        story = _require_story(session, sample_story.story_id)
        evidence = _load_validation_evidence(story)

        assert evidence["spec_version_id"] == compiled_spec.spec_version_id
        assert "validated_at" in evidence
        assert "passed" in evidence
        assert "rules_checked" in evidence
        assert "invariants_checked" in evidence
        assert "validator_version" in evidence
        assert "input_hash" in evidence

        if result.get("passed", False):
            assert story.accepted_spec_version_id == compiled_spec.spec_version_id

    def test_evidence_persisted_on_validation_fail(
        self,
        session: Session,
        sample_project: Project,
        compiled_spec: SpecRegistry,
        engine: Engine,
    ) -> None:
        """Evidence is stored even when validation fails."""
        spec_tools.engine = engine

        feature = _create_feature_hierarchy(
            session,
            project_id=_require_id(
                sample_project.project_id,
                "sample_project.project_id",
            ),
            prefix="Fail",
            detail="fail test",
        )

        bad_story = UserStory(
            project_id=_require_id(
                sample_project.project_id,
                "sample_project.project_id",
            ),
            feature_id=_require_id(feature.feature_id, "fail feature_id"),
            title="Bad story title",
            story_description="This is a poorly formatted story",
            acceptance_criteria="",
        )
        session.add(bad_story)
        session.commit()
        session.refresh(bad_story)

        original_accepted = bad_story.accepted_spec_version_id

        validate_story_with_spec_authority(
            {
                "story_id": bad_story.story_id,
                "spec_version_id": compiled_spec.spec_version_id,
            },
            tool_context=None,
        )

        session.expire(bad_story)
        story = _require_story(session, bad_story.story_id)
        evidence = _load_validation_evidence(story)
        assert evidence["spec_version_id"] == compiled_spec.spec_version_id
        assert "failures" in evidence

        assert story.accepted_spec_version_id == original_accepted

    def test_evidence_contains_all_required_fields(
        self,
        session: Session,
        sample_story: UserStory,
        compiled_spec: SpecRegistry,
        engine: Engine,
    ) -> None:
        """Evidence contains all required fields per schema."""
        spec_tools.engine = engine

        validate_story_with_spec_authority(
            {
                "story_id": sample_story.story_id,
                "spec_version_id": compiled_spec.spec_version_id,
            },
            tool_context=None,
        )

        session.expire(sample_story)
        story = _require_story(session, sample_story.story_id)
        evidence = _load_validation_evidence(story)

        validated = ValidationEvidence.model_validate(evidence)

        assert validated.spec_version_id == compiled_spec.spec_version_id
        assert validated.validator_version == VALIDATOR_VERSION
        assert isinstance(validated.rules_checked, list)
        assert isinstance(validated.invariants_checked, list)
        assert len(validated.invariants_checked) == 1
        assert isinstance(validated.evaluated_invariant_ids, list)
        assert "INV-0000000000000001" in validated.evaluated_invariant_ids
        assert isinstance(validated.finding_invariant_ids, list)
        assert len(validated.finding_invariant_ids) == 0
        assert isinstance(validated.failures, list)
        assert isinstance(validated.warnings, list)
        assert validated.input_hash is not None


class TestDeterministicInputHashing:
    """Tests that input hashing is deterministic and reproducible."""

    def test_same_story_content_produces_same_hash(
        self,
        session: Session,
        sample_project: Project,
        compiled_spec: SpecRegistry,
        engine: Engine,
    ) -> None:
        """Identical story content produces identical input_hash."""
        spec_tools.engine = engine

        project_id = _require_id(sample_project.project_id, "sample_project.project_id")
        feature = _create_feature_hierarchy(
            session,
            project_id=project_id,
            prefix="Hash",
            detail="hash test",
        )
        feature_id = _require_id(feature.feature_id, "hash feature_id")

        story_content = {
            "title": "As a tester, I want determinism",
            "description": "As a tester, I want deterministic hashing for reproducibility.",  # noqa: E501
            "acceptance_criteria": "Given input X, When hashed, Then hash is always Y",
        }

        story1 = UserStory(
            project_id=project_id,
            feature_id=feature_id,
            title=story_content["title"],
            story_description=story_content["description"],
            acceptance_criteria=story_content["acceptance_criteria"],
        )
        session.add(story1)
        session.commit()
        session.refresh(story1)

        story2 = UserStory(
            project_id=project_id,
            feature_id=feature_id,
            title=story_content["title"],
            story_description=story_content["description"],
            acceptance_criteria=story_content["acceptance_criteria"],
        )
        session.add(story2)
        session.commit()
        session.refresh(story2)

        validate_story_with_spec_authority(
            {
                "story_id": story1.story_id,
                "spec_version_id": compiled_spec.spec_version_id,
            },
            tool_context=None,
        )
        validate_story_with_spec_authority(
            {
                "story_id": story2.story_id,
                "spec_version_id": compiled_spec.spec_version_id,
            },
            tool_context=None,
        )

        session.expire_all()
        s1 = _require_story(session, story1.story_id)
        s2 = _require_story(session, story2.story_id)

        e1 = _load_validation_evidence(s1)
        e2 = _load_validation_evidence(s2)

        assert e1["input_hash"] == e2["input_hash"]

    def test_different_story_content_produces_different_hash(
        self,
        session: Session,
        sample_project: Project,
        compiled_spec: SpecRegistry,
        engine: Engine,
    ) -> None:
        """Different story content produces different input_hash."""
        spec_tools.engine = engine

        project_id = _require_id(sample_project.project_id, "sample_project.project_id")
        feature = _create_feature_hierarchy(
            session,
            project_id=project_id,
            prefix="Diff",
            detail="diff test",
        )
        feature_id = _require_id(feature.feature_id, "diff feature_id")

        story1 = UserStory(
            project_id=project_id,
            feature_id=feature_id,
            title="Story A",
            story_description="Description A",
            acceptance_criteria="AC A",
        )
        session.add(story1)
        session.commit()
        session.refresh(story1)

        story2 = UserStory(
            project_id=project_id,
            feature_id=feature_id,
            title="Story B",
            story_description="Description B",
            acceptance_criteria="AC B",
        )
        session.add(story2)
        session.commit()
        session.refresh(story2)

        validate_story_with_spec_authority(
            {
                "story_id": story1.story_id,
                "spec_version_id": compiled_spec.spec_version_id,
            },
            tool_context=None,
        )
        validate_story_with_spec_authority(
            {
                "story_id": story2.story_id,
                "spec_version_id": compiled_spec.spec_version_id,
            },
            tool_context=None,
        )

        session.expire_all()
        s1 = _require_story(session, story1.story_id)
        s2 = _require_story(session, story2.story_id)

        e1 = _load_validation_evidence(s1)
        e2 = _load_validation_evidence(s2)

        assert e1["input_hash"] != e2["input_hash"]


class TestWrongSpecVersionIdFails:
    """Tests that using wrong spec_version_id fails deterministically."""

    def test_validation_fails_for_spec_from_different_project(
        self,
        session: Session,
        sample_project: Project,
        sample_story: UserStory,
        compiled_spec: SpecRegistry,
        engine: Engine,
    ) -> None:
        """Validation fails if spec belongs to different project."""
        del sample_project, compiled_spec
        spec_tools.engine = engine

        other_project = Project(
            name="Other Project",
            description="Different project",
            vision="Different vision",
        )
        session.add(other_project)
        session.commit()
        session.refresh(other_project)

        other_spec = _accepted_spec(
            session,
            project_id=_require_id(other_project.project_id, "other project_id"),
            content="Other project spec",
        )
        other_spec_id = _require_id(
            other_spec.spec_version_id,
            "other spec_version_id",
        )
        with patch(
            "tools.spec_tools._extract_spec_authority_llm",
            return_value=_fake_compilation_artifact(),
        ):
            compile_spec_authority(
                {"spec_version_id": other_spec_id},
                tool_context=None,
            )

        result = validate_story_with_spec_authority(
            {"story_id": sample_story.story_id, "spec_version_id": other_spec_id},
            tool_context=None,
        )

        assert result["success"] is False
        assert (
            "project" in result["error"].lower()
            or "mismatch" in result["error"].lower()
        )


class TestValidatorVersion:
    """Tests for validator versioning."""

    def test_validator_version_constant_exists(self) -> None:
        """VALIDATOR_VERSION constant exists."""
        assert VALIDATOR_VERSION is not None
        assert isinstance(VALIDATOR_VERSION, str)
        parts = VALIDATOR_VERSION.split(".")
        assert len(parts) >= 2  # noqa: PLR2004

    def test_validator_version_in_evidence(
        self,
        session: Session,
        sample_story: UserStory,
        compiled_spec: SpecRegistry,
        engine: Engine,
    ) -> None:
        """validator_version is stored in evidence."""
        spec_tools.engine = engine

        validate_story_with_spec_authority(
            {
                "story_id": sample_story.story_id,
                "spec_version_id": compiled_spec.spec_version_id,
            },
            tool_context=None,
        )

        session.expire(sample_story)
        story = _require_story(session, sample_story.story_id)
        evidence = _load_validation_evidence(story)

        assert evidence["validator_version"] == VALIDATOR_VERSION
