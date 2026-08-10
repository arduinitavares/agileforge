"""Tests for validation mode routing: deterministic, llm, and hybrid."""

import json
from datetime import UTC, datetime
from typing import Never

import pytest
from sqlmodel import Session

from agile_sqlmodel import (
    CompiledSpecAuthority,
    Project,
    SpecAuthorityAcceptance,
    UserStory,
)
from models.core import Epic, Feature, Theme
from services.specs.authority_selection import pending_authority_fingerprint
from tests.typing_helpers import require_id
from tests.workflow.lifecycle_fixtures import seed_accepted_specification
from tools import spec_tools
from tools.spec_tools import validate_story_with_spec_authority
from utils.spec_schemas import (
    ForbiddenCapabilityParams,
    Invariant,
    InvariantType,
    RequiredFieldParams,
    SourceMapEntry,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerOutput,
)


def _create_compiled_spec(session: Session, project_id: int) -> int:
    spec = seed_accepted_specification(
        session,
        project_id=project_id,
        content=json.dumps({"title": "Spec"}),
    ).spec

    invariant = Invariant(
        id="INV-0000000000000001",
        type=InvariantType.REQUIRED_FIELD,
        parameters=RequiredFieldParams(field_name="user_id"),
    )
    artifact = SpecAuthorityCompilationSuccess(
        scope_themes=["core"],
        domain="test",
        invariants=[invariant],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[
            SourceMapEntry(
                invariant_id=invariant.id,
                excerpt="Must include user_id.",
                location="spec",
            )
        ],
        compiler_version="3.0.0",
        prompt_hash="0" * 64,
    )
    compiled = CompiledSpecAuthority(
        spec_version_id=require_id(spec.spec_version_id, "spec_version_id"),
        compiler_version="3.0.0",
        prompt_hash="0" * 64,
        compiled_at=datetime.now(UTC),
        scope_themes=json.dumps(["core"]),
        invariants=json.dumps(["REQUIRED_FIELD:user_id"]),
        eligible_feature_ids=json.dumps([]),
        rejected_features=json.dumps([]),
        spec_gaps=json.dumps([]),
        compiled_artifact_json=SpecAuthorityCompilerOutput(
            root=artifact
        ).model_dump_json(),
    )
    session.add(compiled)
    session.commit()
    session.refresh(compiled)
    session.add(
        SpecAuthorityAcceptance(
            project_id=project_id,
            spec_version_id=require_id(spec.spec_version_id, "spec_version_id"),
            status="accepted",
            policy="test",
            decided_by="tester",
            compiler_version=compiled.compiler_version,
            prompt_hash=compiled.prompt_hash,
            spec_hash=spec.spec_hash,
            pending_authority_id=compiled.authority_id,
            authority_fingerprint=pending_authority_fingerprint(compiled),
        )
    )
    session.commit()

    return require_id(spec.spec_version_id, "spec_version_id")


def _create_story(session: Session, project_id: int) -> UserStory:
    theme = Theme(project_id=project_id, title="Theme", description="")
    session.add(theme)
    session.commit()
    session.refresh(theme)

    epic = Epic(
        theme_id=require_id(theme.theme_id, "theme_id"), title="Epic", summary=""
    )
    session.add(epic)
    session.commit()
    session.refresh(epic)

    feature = Feature(
        epic_id=require_id(epic.epic_id, "epic_id"), title="Feature", description=""
    )
    session.add(feature)
    session.commit()
    session.refresh(feature)

    story = UserStory(
        project_id=project_id,
        feature_id=require_id(feature.feature_id, "feature_id"),
        title="As a user, I want to export reports",
        story_description="As a user, I want to export reports for audit.",
        acceptance_criteria="Given reports exist, When I export, Then I get CSV.",
    )
    session.add(story)
    session.commit()
    session.refresh(story)
    return story


def _create_orphan_story(session: Session, project_id: int) -> UserStory:
    story = UserStory(
        project_id=project_id,
        feature_id=None,
        title="As a reviewer, I want attestation visibility",
        story_description="As a reviewer, I want to confirm attestation state.",
        acceptance_criteria="Given an item, When I open details, Then attestation is visible.",  # noqa: E501
    )
    session.add(story)
    session.commit()
    session.refresh(story)
    return story


def _build_authority_for_alignment(
    invariants: list[Invariant],
    source_map: list[SourceMapEntry],
) -> CompiledSpecAuthority:
    artifact = SpecAuthorityCompilationSuccess(
        scope_themes=["core"],
        domain="test",
        invariants=invariants,
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=source_map,
        compiler_version="3.0.0",
        prompt_hash="0" * 64,
    )
    return CompiledSpecAuthority(
        spec_version_id=1,
        compiler_version="3.0.0",
        prompt_hash="0" * 64,
        compiled_at=datetime.now(UTC),
        scope_themes=json.dumps(["core"]),
        invariants=json.dumps([f"{inv.type}:{inv.id}" for inv in invariants]),
        eligible_feature_ids=json.dumps([]),
        rejected_features=json.dumps([]),
        spec_gaps=json.dumps([]),
        compiled_artifact_json=SpecAuthorityCompilerOutput(
            root=artifact
        ).model_dump_json(),
    )


@pytest.fixture
def setup_validation_case(session: Session) -> tuple[UserStory, int]:
    """Create one project/spec/story tuple for mode tests."""
    project = Project(name="Validation Modes", vision="Test")
    session.add(project)
    session.commit()
    session.refresh(project)

    spec_version_id = _create_compiled_spec(
        session, require_id(project.project_id, "project_id")
    )
    story = _create_story(session, require_id(project.project_id, "project_id"))
    return story, spec_version_id


def test_llm_mode_passes_with_stubbed_compliant_result(
    setup_validation_case: tuple[UserStory, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify llm mode passes with stubbed compliant result."""
    story, spec_version_id = setup_validation_case

    monkeypatch.setattr(
        spec_tools,
        "_run_llm_spec_validation",
        lambda *_args, **_kwargs: {
            "passed": True,
            "issues": [],
            "suggestions": [],
            "verdict": "Compliant",
            "critical_gaps": [],
        },
    )

    result = validate_story_with_spec_authority(
        {
            "story_id": require_id(story.story_id, "story_id"),
            "spec_version_id": spec_version_id,
            "mode": "llm",
        },
        tool_context=None,
    )

    assert result["success"] is True
    assert result["mode"] == "llm"
    assert result["passed"] is True


def test_llm_payload_includes_feature_context(
    setup_validation_case: tuple[UserStory, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify llm payload includes feature context."""
    story, spec_version_id = setup_validation_case
    captured_payload: dict = {}

    async def _capture_payload(payload_text: str) -> str:
        captured_payload.update(json.loads(payload_text))
        return json.dumps(
            {
                "is_compliant": True,
                "issues": [],
                "suggestions": [],
                "domain_compliance": None,
                "verdict": "Compliant",
            }
        )

    monkeypatch.setattr(spec_tools, "_invoke_spec_validator_async", _capture_payload)

    result = validate_story_with_spec_authority(
        {
            "story_id": require_id(story.story_id, "story_id"),
            "spec_version_id": spec_version_id,
            "mode": "llm",
        },
        tool_context=None,
    )

    assert result["success"] is True
    assert result["passed"] is True
    assert captured_payload["spec_version_id"] == spec_version_id
    assert captured_payload["feature_title"] == "Feature"
    assert captured_payload["feature_description"] == ""


def test_llm_payload_includes_feature_context_orphan_story(
    setup_validation_case: tuple[UserStory, int],
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
) -> None:
    """Verify llm payload includes feature context orphan story."""
    base_story, spec_version_id = setup_validation_case
    orphan_story = _create_orphan_story(
        session, require_id(base_story.project_id, "project_id")
    )
    captured_payload: dict = {}

    async def _capture_payload(payload_text: str) -> str:
        captured_payload.update(json.loads(payload_text))
        return json.dumps(
            {
                "is_compliant": True,
                "issues": [],
                "suggestions": [],
                "domain_compliance": None,
                "verdict": "Compliant",
            }
        )

    monkeypatch.setattr(spec_tools, "_invoke_spec_validator_async", _capture_payload)

    result = validate_story_with_spec_authority(
        {
            "story_id": require_id(orphan_story.story_id, "story_id"),
            "spec_version_id": spec_version_id,
            "mode": "llm",
        },
        tool_context=None,
    )

    assert result["success"] is True
    assert result["passed"] is True
    assert captured_payload["spec_version_id"] == spec_version_id
    assert captured_payload["feature_title"] is None
    assert captured_payload["feature_description"] is None


def test_llm_critical_gaps_remain_hard_failures_and_do_not_pin_spec_version(
    setup_validation_case: tuple[UserStory, int],
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
) -> None:
    """Verify llm critical gaps remain hard failures and do not pin spec version."""
    story, spec_version_id = setup_validation_case

    monkeypatch.setattr(
        spec_tools,
        "_run_llm_spec_validation",
        lambda *_args, **_kwargs: {
            "passed": False,
            "issues": ["Story misses domain detail"],
            "suggestions": ["Mention user_id in acceptance criteria"],
            "verdict": "Non-compliant",
            "critical_gaps": ["Missing required user_id in acceptance criteria"],
        },
    )

    result = validate_story_with_spec_authority(
        {
            "story_id": require_id(story.story_id, "story_id"),
            "spec_version_id": spec_version_id,
            "mode": "llm",
        },
        tool_context=None,
    )
    assert result["success"] is True
    assert result["passed"] is False
    assert any(f["rule"] == "RULE_LLM_SPEC_VALIDATION" for f in result["failures"])

    session.expire(story)
    refreshed = session.get(UserStory, require_id(story.story_id, "story_id"))
    assert refreshed is not None
    assert refreshed.accepted_spec_version_id is None


@pytest.mark.parametrize("mode", ["llm", "hybrid"])
def test_llm_soft_issues_become_warnings_not_failures(
    setup_validation_case: tuple[UserStory, int],
    monkeypatch: pytest.MonkeyPatch,
    mode: object,
) -> None:
    """Verify llm soft issues become warnings not failures."""
    story, spec_version_id = setup_validation_case

    monkeypatch.setattr(
        spec_tools,
        "_run_deterministic_alignment_checks",
        lambda *_args, **_kwargs: ([], [], []),
    )
    monkeypatch.setattr(
        spec_tools,
        "_run_llm_spec_validation",
        lambda *_args, **_kwargs: {
            "passed": False,
            "issues": ["Soft issue: mention testability in AC"],
            "suggestions": ["Clarify an observable output"],
            "verdict": "Needs clarification",
            "critical_gaps": [],
        },
    )

    result = validate_story_with_spec_authority(
        {
            "story_id": require_id(story.story_id, "story_id"),
            "spec_version_id": spec_version_id,
            "mode": mode,
        },
        tool_context=None,
    )

    assert result["success"] is True
    assert result["passed"] is True
    assert not any(f["rule"] == "RULE_LLM_SPEC_VALIDATION" for f in result["failures"])


def test_hybrid_mode_fails_when_deterministic_alignment_fails(
    setup_validation_case: tuple[UserStory, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify hybrid mode fails when deterministic alignment fails."""
    story, spec_version_id = setup_validation_case

    monkeypatch.setattr(
        spec_tools,
        "_run_deterministic_alignment_checks",
        lambda *_args, **_kwargs: (
            [
                spec_tools.AlignmentFinding(
                    code="FORBIDDEN_CAPABILITY",
                    invariant="INV-1",
                    capability="cloud",
                    message="cloud capability is forbidden",
                    severity="failure",
                    created_at=datetime.now(UTC),
                )
            ],
            [],
            [],
        ),
    )
    monkeypatch.setattr(
        spec_tools,
        "_run_llm_spec_validation",
        lambda *_args, **_kwargs: {
            "passed": True,
            "issues": [],
            "suggestions": [],
            "verdict": "Compliant",
            "critical_gaps": [],
        },
    )

    result = validate_story_with_spec_authority(
        {
            "story_id": require_id(story.story_id, "story_id"),
            "spec_version_id": spec_version_id,
            "mode": "hybrid",
        },
        tool_context=None,
    )
    assert result["success"] is True
    assert result["mode"] == "hybrid"
    assert result["passed"] is False


def test_deterministic_mode_does_not_call_llm_adapter(
    setup_validation_case: tuple[UserStory, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify deterministic mode does not call llm adapter."""
    story, spec_version_id = setup_validation_case

    def _should_not_be_called(*_args: object, **_kwargs: object) -> Never:
        msg = "LLM adapter should not run in deterministic mode"
        raise AssertionError(msg)

    monkeypatch.setattr(spec_tools, "_run_llm_spec_validation", _should_not_be_called)

    result = validate_story_with_spec_authority(
        {
            "story_id": require_id(story.story_id, "story_id"),
            "spec_version_id": spec_version_id,
        },
        tool_context=None,
    )
    assert result["success"] is True
    assert result["mode"] == "deterministic"


def test_env_default_mode_uses_hybrid_when_mode_omitted(
    setup_validation_case: tuple[UserStory, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify env default mode uses hybrid when mode omitted."""
    story, spec_version_id = setup_validation_case
    monkeypatch.setenv("SPEC_VALIDATION_DEFAULT_MODE", "hybrid")
    monkeypatch.setattr(
        spec_tools,
        "_run_deterministic_alignment_checks",
        lambda *_args, **_kwargs: ([], [], []),
    )
    monkeypatch.setattr(
        spec_tools,
        "_run_llm_spec_validation",
        lambda *_args, **_kwargs: {
            "passed": True,
            "issues": [],
            "suggestions": [],
            "verdict": "Compliant",
            "critical_gaps": [],
        },
    )

    result = validate_story_with_spec_authority(
        {
            "story_id": require_id(story.story_id, "story_id"),
            "spec_version_id": spec_version_id,
        },
        tool_context=None,
    )

    assert result["success"] is True
    assert result["mode"] == "hybrid"


def test_env_default_mode_invalid_falls_back_to_deterministic(
    setup_validation_case: tuple[UserStory, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify env default mode invalid falls back to deterministic."""
    story, spec_version_id = setup_validation_case
    monkeypatch.setenv("SPEC_VALIDATION_DEFAULT_MODE", "not-a-mode")

    def _should_not_be_called(*_args: object, **_kwargs: object) -> Never:
        msg = "LLM adapter should not run when env default mode is invalid"
        raise AssertionError(msg)

    monkeypatch.setattr(spec_tools, "_run_llm_spec_validation", _should_not_be_called)

    result = validate_story_with_spec_authority(
        {
            "story_id": require_id(story.story_id, "story_id"),
            "spec_version_id": spec_version_id,
        },
        tool_context=None,
    )

    assert result["success"] is True
    assert result["mode"] == "deterministic"


def test_deterministic_forbidden_capability_keyword_match() -> None:
    """Verify deterministic forbidden capability keyword match."""
    story = UserStory(
        project_id=1,
        feature_id=None,
        title="Web dashboard",
        story_description="Build dashboard UI for reviews.",
        acceptance_criteria="Given authenticated user, when opening dashboard, then widgets appear.",  # noqa: E501
    )
    invariant = Invariant(
        id="INV-0000000000000001",
        type=InvariantType.FORBIDDEN_CAPABILITY,
        parameters=ForbiddenCapabilityParams(capability="web"),
    )
    authority = _build_authority_for_alignment(
        invariants=[invariant],
        source_map=[
            SourceMapEntry(
                invariant_id=invariant.id,
                excerpt="The system must not include web interfaces.",
                location="spec",
            )
        ],
    )

    failures, warnings, messages = spec_tools._run_deterministic_alignment_checks(  # pylint: disable=protected-access
        story,
        authority,
    )

    assert not warnings
    assert not messages
    assert any(f.code == "FORBIDDEN_CAPABILITY" for f in failures)


def test_deterministic_forbidden_capability_ignores_policy_boilerplate_context() -> (
    None
):
    """Verify deterministic forbidden capability ignores policy boilerplate context."""
    story = UserStory(
        project_id=1,
        feature_id=None,
        title="Risk-focused codebase assessment",
        story_description="Review the current codebase and identify architectural risks.",  # noqa: E501
        acceptance_criteria=(
            "Given the final report, when external references are used, "
            "then they are appropriately cited to comply with the plagiarism policy."
        ),
    )
    invariant = Invariant(
        id="INV-0000000000000003",
        type=InvariantType.FORBIDDEN_CAPABILITY,
        parameters=ForbiddenCapabilityParams(capability="plagiarism"),
    )
    authority = _build_authority_for_alignment(
        invariants=[invariant],
        source_map=[
            SourceMapEntry(
                invariant_id=invariant.id,
                excerpt=(
                    "Knowingly representing the works of others as one's own or "
                    "referencing the works of others without appropriate citation is prohibited."  # noqa: E501
                ),
                location="Plagiarism Policy",
            )
        ],
    )

    failures, warnings, messages = spec_tools._run_deterministic_alignment_checks(  # pylint: disable=protected-access
        story,
        authority,
    )

    assert failures == []
    assert warnings == []
    assert messages == []


def test_deterministic_forbidden_capability_still_fails_for_integrity_enforcement_feature() -> (  # noqa: E501
    None
):
    """Verify deterministic forbidden capability still fails for integrity enforcement feature."""  # noqa: E501
    story = UserStory(
        project_id=1,
        feature_id=None,
        title="Plagiarism detection workflow",
        story_description="Add automated plagiarism detection during submission review.",  # noqa: E501
        acceptance_criteria=(
            "Given a new submission, when plagiarism is detected, "
            "then the system flags it for manual review."
        ),
    )
    invariant = Invariant(
        id="INV-0000000000000004",
        type=InvariantType.FORBIDDEN_CAPABILITY,
        parameters=ForbiddenCapabilityParams(capability="plagiarism"),
    )
    authority = _build_authority_for_alignment(
        invariants=[invariant],
        source_map=[
            SourceMapEntry(
                invariant_id=invariant.id,
                excerpt="The project must not implement plagiarism detection.",
                location="Project Constraints",
            )
        ],
    )

    failures, warnings, messages = spec_tools._run_deterministic_alignment_checks(  # pylint: disable=protected-access
        story,
        authority,
    )

    assert not warnings
    assert not messages
    assert any(f.code == "FORBIDDEN_CAPABILITY" for f in failures)


def test_deterministic_forbidden_capability_not_suppressed_by_generic_references_word() -> (  # noqa: E501
    None
):
    """Verify deterministic forbidden capability not suppressed by generic references word."""  # noqa: E501
    story = UserStory(
        project_id=1,
        feature_id=None,
        title="Web dashboard references",
        story_description="Create a report that references web dashboard metrics.",
        acceptance_criteria=(
            "Given a generated report, when opened, "
            "then it references data from the web dashboard."
        ),
    )
    invariant = Invariant(
        id="INV-0000000000000006",
        type=InvariantType.FORBIDDEN_CAPABILITY,
        parameters=ForbiddenCapabilityParams(capability="web dashboard"),
    )
    authority = _build_authority_for_alignment(
        invariants=[invariant],
        source_map=[
            SourceMapEntry(
                invariant_id=invariant.id,
                excerpt="The project must not include a web dashboard.",
                location="Project Constraints",
            )
        ],
    )

    failures, warnings, messages = spec_tools._run_deterministic_alignment_checks(  # pylint: disable=protected-access
        story,
        authority,
    )

    assert not warnings
    assert not messages
    assert any(f.code == "FORBIDDEN_CAPABILITY" for f in failures)


def test_deterministic_required_field_no_false_positive() -> None:
    """Verify deterministic required field no false positive."""
    story = UserStory(
        project_id=1,
        feature_id=None,
        title="Form validations",
        story_description="Collect user contact data.",
        acceptance_criteria="Given valid input, when saving, then the email field is persisted.",  # noqa: E501
    )
    invariant = Invariant(
        id="INV-0000000000000002",
        type=InvariantType.REQUIRED_FIELD,
        parameters=RequiredFieldParams(field_name="email"),
    )
    authority = _build_authority_for_alignment(
        invariants=[invariant],
        source_map=[
            SourceMapEntry(
                invariant_id=invariant.id,
                excerpt="Payload must include email.",
                location="spec",
            )
        ],
    )

    failures, warnings, messages = spec_tools._run_deterministic_alignment_checks(  # pylint: disable=protected-access
        story,
        authority,
    )

    assert not failures
    assert not warnings
    assert not messages


def test_deterministic_alignment_no_invariants() -> None:
    """Verify deterministic alignment no invariants."""
    story = UserStory(
        project_id=1,
        feature_id=None,
        title="Any story",
        story_description="No special constraints",
        acceptance_criteria="Given item, when processed, then done.",
    )
    authority = _build_authority_for_alignment(
        invariants=[],
        source_map=[],
    )

    failures, warnings, messages = spec_tools._run_deterministic_alignment_checks(  # pylint: disable=protected-access
        story,
        authority,
    )

    assert failures == []
    assert warnings == []
    assert messages == []


def test_hybrid_mode_ignores_policy_boilerplate_when_llm_passes(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify hybrid mode ignores policy boilerplate when llm passes."""
    project = Project(name="Policy Context", vision="Test")
    session.add(project)
    session.commit()
    session.refresh(project)

    spec = seed_accepted_specification(
        session,
        project_id=require_id(project.project_id, "project_id"),
        content=json.dumps({"title": "Policy spec"}),
    ).spec

    story = _create_orphan_story(session, require_id(project.project_id, "project_id"))
    story.acceptance_criteria = (
        "Given the final report, when external references are used, "
        "then they are appropriately cited to comply with the plagiarism policy."
    )
    session.add(story)
    session.commit()
    session.refresh(story)

    invariant = Invariant(
        id="INV-0000000000000005",
        type=InvariantType.FORBIDDEN_CAPABILITY,
        parameters=ForbiddenCapabilityParams(capability="plagiarism"),
    )
    artifact = SpecAuthorityCompilationSuccess(
        scope_themes=["assignment policy"],
        domain="test",
        invariants=[invariant],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[
            SourceMapEntry(
                invariant_id=invariant.id,
                excerpt=(
                    "Knowingly representing the works of others as one's own or "
                    "referencing the works of others without appropriate citation is prohibited."  # noqa: E501
                ),
                location="Plagiarism Policy",
            )
        ],
        compiler_version="3.0.0",
        prompt_hash="0" * 64,
    )
    compiled = CompiledSpecAuthority(
        spec_version_id=require_id(spec.spec_version_id, "spec_version_id"),
        compiler_version="3.0.0",
        prompt_hash="0" * 64,
        compiled_at=datetime.now(UTC),
        scope_themes=json.dumps(["assignment policy"]),
        invariants=json.dumps(["FORBIDDEN_CAPABILITY:plagiarism"]),
        eligible_feature_ids=json.dumps([]),
        rejected_features=json.dumps([]),
        spec_gaps=json.dumps([]),
        compiled_artifact_json=SpecAuthorityCompilerOutput(
            root=artifact
        ).model_dump_json(),
    )
    session.add(compiled)
    session.commit()
    session.refresh(compiled)
    session.add(
        SpecAuthorityAcceptance(
            project_id=require_id(project.project_id, "project_id"),
            spec_version_id=require_id(spec.spec_version_id, "spec_version_id"),
            status="accepted",
            policy="test",
            decided_by="tester",
            compiler_version=compiled.compiler_version,
            prompt_hash=compiled.prompt_hash,
            spec_hash=spec.spec_hash,
            pending_authority_id=compiled.authority_id,
            authority_fingerprint=pending_authority_fingerprint(compiled),
        )
    )
    session.commit()

    monkeypatch.setattr(
        spec_tools,
        "_run_llm_spec_validation",
        lambda *_args, **_kwargs: {
            "passed": True,
            "issues": [],
            "suggestions": [],
            "verdict": "Compliant",
            "critical_gaps": [],
        },
    )

    result = validate_story_with_spec_authority(
        {
            "story_id": require_id(story.story_id, "story_id"),
            "spec_version_id": require_id(spec.spec_version_id, "spec_version_id"),
            "mode": "hybrid",
        },
        tool_context=None,
    )

    assert result["success"] is True
    assert result["mode"] == "hybrid"
    assert result["passed"] is True
    assert not any(
        f["code"] == "FORBIDDEN_CAPABILITY" for f in result["alignment_failures"]
    )


def test_structural_rule_detects_offline_cloud_connectivity_contradiction() -> None:
    """Verify structural rule detects offline cloud connectivity contradiction."""
    story = UserStory(
        project_id=1,
        feature_id=None,
        title="Connectivity constraints",
        story_description="Must run fully offline in all environments.",
        acceptance_criteria="Given setup, when syncing, then cloud sync is required.",
    )

    _rules_checked, failures, _warnings = spec_tools._run_structural_story_checks(  # pylint: disable=protected-access
        story
    )

    assert any(
        failure.rule == "RULE_CONTRADICTORY_CONNECTIVITY_REQUIREMENTS"
        for failure in failures
    )


def test_structural_rule_detects_impossible_latency_requirement() -> None:
    """Verify structural rule detects impossible latency requirement."""
    story = UserStory(
        project_id=1,
        feature_id=None,
        title="Latency target",
        story_description="As a user, I want immediate response.",
        acceptance_criteria="Given a request, when processed, then latency is under 0ms.",  # noqa: E501
    )

    _rules_checked, failures, _warnings = spec_tools._run_structural_story_checks(  # pylint: disable=protected-access
        story
    )

    assert any(
        failure.rule == "RULE_IMPOSSIBLE_LATENCY_REQUIREMENT" for failure in failures
    )


def test_structural_rule_detects_scope_mismatch_placeholder_acceptance_criteria() -> (
    None
):
    """Verify structural rule detects scope mismatch placeholder acceptance criteria."""
    story = UserStory(
        project_id=1,
        feature_id=None,
        title="As a user, I want to stream video from my security cameras.",
        story_description="Out of scope feature request.",
        acceptance_criteria="Given item, When add, Then in cart.",
    )

    _rules_checked, failures, _warnings = spec_tools._run_structural_story_checks(  # pylint: disable=protected-access
        story
    )

    assert any(
        failure.rule == "RULE_ACCEPTANCE_CRITERIA_SCOPE_MISMATCH"
        for failure in failures
    )


def test_parse_truncated_json_recovers_non_compliant() -> None:
    """Verify parse truncated json recovers non compliant."""
    raw_text = (
        '{"is_compliant": false, '
        '"issues": ["Missing in-scope requirement"], '
        '"critical_gaps": ["Missing in-scope requirement"], '
        '"suggestions": ["Add explicit acceptance criteria for attestation"]'
    )

    parsed = spec_tools._parse_llm_validator_response(raw_text)  # pylint: disable=protected-access

    assert parsed["passed"] is False
    assert parsed["issues"]
    assert parsed["critical_gaps"]


def test_parse_truncated_json_unrecoverable_raises() -> None:
    """Verify parse truncated json unrecoverable raises."""
    raw_text = '{"issues": ["Missing in-scope requirement"]'

    with pytest.raises(ValueError):  # noqa: PT011
        spec_tools._parse_llm_validator_response(raw_text)  # pylint: disable=protected-access
