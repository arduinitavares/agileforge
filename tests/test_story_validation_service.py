"""Tests for story validation service."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlmodel import Session

from agile_sqlmodel import CompiledSpecAuthority, Product, SpecRegistry, UserStory
from tests.typing_helpers import require_id
from utils.spec_schemas import (
    ForbiddenCapabilityParams,
    Invariant,
    InvariantType,
    RequiredFieldParams,
    SourceMapEntry,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerOutput,
    ValidationEvidence,
)


def test_services_package_exports_validate_story_with_spec_authority() -> None:
    """Verify services package exports validate story with spec authority."""
    from services import specs  # noqa: PLC0415
    from services.specs import story_validation_service  # noqa: PLC0415

    assert (
        specs.validate_story_with_spec_authority
        is story_validation_service.validate_story_with_spec_authority
    )
    assert (
        specs.compute_story_input_hash
        is story_validation_service.compute_story_input_hash
    )


def test_validate_story_with_spec_authority_returns_missing_story_error(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify validate story with spec authority returns missing story error."""
    from services.specs import story_validation_service  # noqa: PLC0415

    monkeypatch.setattr(
        story_validation_service,
        "get_engine",
        session.get_bind,
    )

    result = story_validation_service.validate_story_with_spec_authority(
        {"story_id": 999999, "spec_version_id": 123},
        tool_context=None,
    )

    assert result == {
        "success": False,
        "error": "Story 999999 not found",
    }


def test_resolve_engine_honors_legacy_spec_tools_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify resolve engine honors legacy spec tools engine."""
    from services.specs import story_validation_service  # noqa: PLC0415
    from tools import spec_tools  # noqa: PLC0415

    sentinel_engine = object()
    monkeypatch.setattr(spec_tools, "engine", sentinel_engine, raising=False)
    monkeypatch.setattr(
        spec_tools,
        "get_engine",
        story_validation_service.get_engine,
    )

    resolved = story_validation_service._resolve_engine()

    assert resolved is sentinel_engine


def test_compute_story_input_hash_is_stable_for_same_story_content() -> None:
    """Verify compute story input hash is stable for same story content."""
    from services.specs.story_validation_service import (  # noqa: PLC0415
        compute_story_input_hash,
    )

    story_a = SimpleNamespace(
        title="Story",
        story_description="Description",
        acceptance_criteria="Criteria",
    )
    story_b = SimpleNamespace(
        title="Story",
        story_description="Description",
        acceptance_criteria="Criteria",
    )

    assert compute_story_input_hash(story_a) == compute_story_input_hash(story_b)


def test_compute_story_input_hash_changes_when_story_content_changes() -> None:
    """Verify compute story input hash changes when story content changes."""
    from services.specs.story_validation_service import (  # noqa: PLC0415
        compute_story_input_hash,
    )

    story_a = SimpleNamespace(
        title="Story",
        story_description="Description",
        acceptance_criteria="Criteria",
    )
    story_b = SimpleNamespace(
        title="Story",
        story_description="Changed",
        acceptance_criteria="Criteria",
    )

    assert compute_story_input_hash(story_a) != compute_story_input_hash(story_b)


def test_render_invariant_summary_formats_required_field() -> None:
    """Verify render invariant summary formats required field."""
    from services.specs.story_validation_service import (  # noqa: PLC0415
        render_invariant_summary,
    )

    invariant = Invariant(
        id="INV-0000000000000001",
        type=InvariantType.REQUIRED_FIELD,
        parameters=RequiredFieldParams(field_name="user_id"),
    )

    assert render_invariant_summary(invariant) == "REQUIRED_FIELD:user_id"


def test_parse_llm_validator_response_parses_compliant_payload() -> None:
    """Verify parse llm validator response parses compliant payload."""
    from services.specs.story_validation_service import (  # noqa: PLC0415
        parse_llm_validator_response,
    )

    result = parse_llm_validator_response(
        """
        {"is_compliant": true, "issues": [], "suggestions": [],
         "verdict": "Compliant", "domain_compliance": null}
        """
    )

    assert result == {
        "passed": True,
        "issues": [],
        "suggestions": [],
        "verdict": "Compliant",
        "critical_gaps": [],
    }


def test_run_llm_spec_validation_uses_injected_helpers() -> None:
    """Verify run llm spec validation uses injected helpers."""
    from services.specs.story_validation_service import (  # noqa: PLC0415
        LlmValidationResult,
        run_llm_spec_validation,
    )

    captured = {}

    async def fake_invoke(payload_text: str) -> str:
        captured["payload"] = payload_text
        return '{"is_compliant": true, "issues": [], "suggestions": [], "verdict": "Compliant"}'  # noqa: E501

    def fake_parse(raw_text: str) -> LlmValidationResult:
        captured["raw_text"] = raw_text
        return {
            "passed": True,
            "issues": [],
            "suggestions": [],
            "verdict": "Compliant",
            "critical_gaps": [],
        }

    story = UserStory(
        product_id=1,
        title="As a user, I want exports",
        story_description="Export data for audit.",
        acceptance_criteria="Given reports, when exported, then CSV is generated.",
    )
    authority = CompiledSpecAuthority(
        spec_version_id=42,
        compiler_version="1.0.0",
        prompt_hash="0" * 64,
        scope_themes="[]",
        invariants="[]",
        eligible_feature_ids="[]",
        compiled_artifact_json='{"compiled": true}',
    )
    artifact = SimpleNamespace(
        model_dump_json=lambda: '{"compiled": "from artifact"}',
    )

    result = run_llm_spec_validation(
        story,
        authority,
        artifact,
        feature=None,
        invoke_spec_validator_async_fn=fake_invoke,
        parse_llm_validator_response_fn=fake_parse,
    )

    assert result["passed"] is True
    assert (
        '"compiled_authority_json": "{\\"compiled\\": \\"from artifact\\"}"'
        in captured["payload"]
    )
    assert captured["raw_text"].startswith('{"is_compliant": true')


def test_resolve_default_validation_mode_uses_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify resolve default validation mode uses environment."""
    from services.specs.story_validation_service import (  # noqa: PLC0415
        resolve_default_validation_mode,
    )

    monkeypatch.setenv("SPEC_VALIDATION_DEFAULT_MODE", "hybrid")

    assert resolve_default_validation_mode() == "hybrid"


def test_persist_validation_evidence_updates_story_and_acceptance(
    session: Session,
) -> None:
    """Verify persist validation evidence updates story and acceptance."""
    from services.specs.story_validation_service import (  # noqa: PLC0415
        persist_validation_evidence,
    )

    product = Product(name="Evidence Product", vision="Test")
    session.add(product)
    session.commit()
    session.refresh(product)
    product_id = require_id(product.product_id, "product_id")

    story = UserStory(
        product_id=product_id,
        title="Story",
        story_description="Description",
        acceptance_criteria="Criteria",
    )
    session.add(story)
    session.commit()
    session.refresh(story)

    spec_version = SpecRegistry(
        product_id=product_id,
        content="# Spec",
        content_ref=None,
        spec_hash="a" * 64,
        status="approved",
        approved_at=datetime.now(UTC),
        approved_by="tester",
        approval_notes=None,
    )
    session.add(spec_version)
    session.commit()
    session.refresh(spec_version)
    spec_version_id = require_id(spec_version.spec_version_id, "spec_version_id")

    evidence = ValidationEvidence(
        spec_version_id=spec_version_id,
        validated_at=datetime.now(UTC),
        passed=True,
        rules_checked=["SPEC_VERSION_EXISTS"],
        invariants_checked=[],
        validator_version="1.0.0",
        input_hash="abc123",
    )

    persist_validation_evidence(session, story, evidence, passed=True)

    session.expire(story)
    updated = session.get(UserStory, require_id(story.story_id, "story_id"))

    assert updated is not None
    assert updated.validation_evidence == evidence.model_dump_json()
    assert updated.accepted_spec_version_id == spec_version_id


def test_validate_story_with_spec_authority_uses_service_owned_defaults(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify validate story with spec authority uses service owned defaults."""
    from services.specs import story_validation_service  # noqa: PLC0415

    product = Product(name="Validation Product", vision="Test")
    session.add(product)
    session.commit()
    session.refresh(product)
    product_id = require_id(product.product_id, "product_id")

    story = UserStory(
        product_id=product_id,
        title="Story",
        story_description="Description",
        acceptance_criteria="Criteria",
    )
    session.add(story)
    session.commit()
    session.refresh(story)

    spec_version = SpecRegistry(
        product_id=product_id,
        content="# Spec",
        content_ref=None,
        spec_hash="b" * 64,
        status="approved",
        approved_at=datetime.now(UTC),
        approved_by="tester",
        approval_notes=None,
    )
    session.add(spec_version)
    session.commit()
    session.refresh(spec_version)
    spec_version_id = require_id(spec_version.spec_version_id, "spec_version_id")

    authority_artifact = SpecAuthorityCompilationSuccess(
        scope_themes=["core"],
        invariants=[],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[
            SourceMapEntry(
                invariant_id="INV-0000000000000001",
                excerpt="Spec excerpt",
                location="spec",
            )
        ],
        compiler_version="1.0.0",
        prompt_hash="0" * 64,
    )
    authority = CompiledSpecAuthority(
        spec_version_id=spec_version_id,
        compiler_version="1.0.0",
        prompt_hash="0" * 64,
        scope_themes='["core"]',
        invariants="[]",
        eligible_feature_ids="[]",
        rejected_features="[]",
        spec_gaps="[]",
        compiled_artifact_json=SpecAuthorityCompilerOutput(
            root=authority_artifact
        ).model_dump_json(),
    )
    session.add(authority)
    session.commit()

    monkeypatch.setattr(
        story_validation_service,
        "_resolve_engine",
        session.get_bind,
    )

    monkeypatch.setattr(
        story_validation_service,
        "resolve_default_validation_mode",
        lambda: "llm",
    )

    llm_calls = {}

    def fake_run_llm_validation(
        story_arg: UserStory,
        authority_arg: CompiledSpecAuthority,
        artifact_arg: object,
        feature: object = None,
    ) -> dict[str, object]:
        llm_calls["story_id"] = story_arg.story_id
        llm_calls["authority_id"] = authority_arg.authority_id
        llm_calls["artifact"] = artifact_arg
        llm_calls["feature"] = feature
        return {
            "passed": True,
            "issues": [],
            "suggestions": [],
            "verdict": "Compliant",
            "critical_gaps": [],
        }

    monkeypatch.setattr(
        story_validation_service,
        "run_llm_spec_validation",
        fake_run_llm_validation,
    )

    persisted = {}

    def fake_persist(
        session_arg: object, story_arg: object, evidence_arg: object, passed: object
    ) -> None:
        persisted["session"] = session_arg
        persisted["story"] = story_arg
        persisted["evidence"] = evidence_arg
        persisted["passed"] = passed

    monkeypatch.setattr(
        story_validation_service,
        "persist_validation_evidence",
        fake_persist,
    )

    result = story_validation_service.validate_story_with_spec_authority(
        {
            "story_id": require_id(story.story_id, "story_id"),
            "spec_version_id": spec_version_id,
        },
        tool_context=None,
    )

    assert result["success"] is True
    assert result["passed"] is True
    assert result["mode"] == "llm"
    assert llm_calls["story_id"] == story.story_id
    assert persisted["story"].story_id == story.story_id
    assert persisted["passed"] is True
    assert persisted["evidence"].spec_version_id == spec_version_id


def test_run_deterministic_alignment_checks_unwraps_loader_result_success() -> None:
    """Deterministic checks should inspect invariants from loader success results."""
    from services.specs.compiler_service import CompiledArtifactLoadResult  # noqa: PLC0415
    from services.specs.story_validation_service import (  # noqa: PLC0415
        run_deterministic_alignment_checks,
    )

    story = UserStory(
        product_id=1,
        title="Avoid direct DOM access",
        story_description="Story must not use direct DOM access.",
        acceptance_criteria="No direct DOM access is allowed.",
    )
    authority_artifact = SpecAuthorityCompilationSuccess(
        scope_themes=["ui"],
        invariants=[
            Invariant(
                id="INV-0123456789abcdef",
                type=InvariantType.FORBIDDEN_CAPABILITY,
                parameters=ForbiddenCapabilityParams(capability="direct DOM access"),
            )
        ],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[],
        compiler_version="1.0.0",
        prompt_hash="0" * 64,
    )
    authority = CompiledSpecAuthority(
        spec_version_id=1,
        compiler_version="1.0.0",
        prompt_hash="0" * 64,
        scope_themes='["ui"]',
        invariants='["FORBIDDEN_CAPABILITY:direct DOM access"]',
        eligible_feature_ids="[]",
        rejected_features="[]",
        spec_gaps="[]",
        compiled_artifact_json="{}",
    )

    failures, warnings, messages = run_deterministic_alignment_checks(
        story,
        authority,
        load_compiled_artifact_fn=lambda _authority: CompiledArtifactLoadResult(
            status="success",
            artifact=authority_artifact,
        ),
    )

    assert len(failures) == 1
    assert failures[0].code == "FORBIDDEN_CAPABILITY"
    assert failures[0].invariant == "INV-0123456789abcdef"
    assert warnings == []
    assert messages == []
