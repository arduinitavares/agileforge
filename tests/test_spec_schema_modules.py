"""Boundary tests for extracted spec/compiler/story-validation schema modules."""

from __future__ import annotations

import subprocess  # nosec B404
from pathlib import Path
from shutil import which
from typing import Any, cast

import pytest
from pydantic import ValidationError


def _python_files_importing_compat_schemes() -> list[str]:
    root = Path(__file__).resolve().parents[1]
    current_file = Path(__file__).resolve()
    compat_import = "from utils.schemes import"
    offenders: list[str] = []
    git = which("git")
    assert git is not None
    tracked = subprocess.run(  # noqa: S603  # nosec B603
        [git, "ls-files", "*.py"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    for relative_path in sorted(tracked.stdout.splitlines()):
        path = root / relative_path
        if path == root / "utils" / "schemes.py" or path.resolve() == current_file:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if compat_import in text:
            offenders.append(relative_path)
    return offenders


def test_spec_schema_module_exports_validation_and_compiler_models() -> None:
    """Verify spec schema module exports validation and compiler models."""
    from utils import schemes, spec_schemas  # noqa: PLC0415

    assert spec_schemas.ValidationFailure.__module__ == "utils.spec_schemas"
    assert spec_schemas.AlignmentFinding.__module__ == "utils.spec_schemas"
    assert spec_schemas.ValidationEvidence.__module__ == "utils.spec_schemas"
    assert spec_schemas.SpecAuthorityCompilerInput.__module__ == "utils.spec_schemas"
    assert spec_schemas.InvariantType.__module__ == "utils.spec_schemas"
    assert spec_schemas.RequiredFieldParams.__module__ == "utils.spec_schemas"
    assert spec_schemas.UserInteractionParams.__module__ == "utils.spec_schemas"
    assert spec_schemas.StateTransitionParams.__module__ == "utils.spec_schemas"
    assert spec_schemas.DataContractParams.__module__ == "utils.spec_schemas"
    assert spec_schemas.RouteContractParams.__module__ == "utils.spec_schemas"
    assert spec_schemas.VisibilityRuleParams.__module__ == "utils.spec_schemas"
    assert spec_schemas.Invariant.__module__ == "utils.spec_schemas"
    assert (
        spec_schemas.SpecAuthorityCompilationSuccess.__module__ == "utils.spec_schemas"
    )
    assert spec_schemas.SpecAuthorityCompilerOutput.__module__ == "utils.spec_schemas"
    assert spec_schemas.StoryDraft.__module__ == "utils.spec_schemas"
    assert spec_schemas.StoryDraftInput.__module__ == "utils.spec_schemas"
    assert spec_schemas.StoryRefinerInput.__module__ == "utils.spec_schemas"

    assert schemes.ValidationFailure is spec_schemas.ValidationFailure
    assert schemes.AlignmentFinding is spec_schemas.AlignmentFinding
    assert schemes.ValidationEvidence is spec_schemas.ValidationEvidence
    assert schemes.SpecAuthorityCompilerInput is spec_schemas.SpecAuthorityCompilerInput
    assert schemes.InvariantType is spec_schemas.InvariantType
    assert schemes.RequiredFieldParams is spec_schemas.RequiredFieldParams
    assert schemes.UserInteractionParams is spec_schemas.UserInteractionParams
    assert schemes.StateTransitionParams is spec_schemas.StateTransitionParams
    assert schemes.DataContractParams is spec_schemas.DataContractParams
    assert schemes.RouteContractParams is spec_schemas.RouteContractParams
    assert schemes.VisibilityRuleParams is spec_schemas.VisibilityRuleParams
    assert schemes.Invariant is spec_schemas.Invariant
    assert (
        schemes.SpecAuthorityCompilationSuccess
        is spec_schemas.SpecAuthorityCompilationSuccess
    )
    assert (
        schemes.SpecAuthorityCompilerOutput is spec_schemas.SpecAuthorityCompilerOutput
    )
    assert schemes.StoryDraft is spec_schemas.StoryDraft
    assert schemes.StoryDraftInput is spec_schemas.StoryDraftInput
    assert schemes.StoryRefinerInput is spec_schemas.StoryRefinerInput


def test_services_and_agents_import_spec_schema_module_boundary() -> None:
    """Verify services and agents import spec schema module boundary."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent import (  # noqa: PLC0415
        agent,
    )
    from services import orchestrator_query_service  # noqa: PLC0415
    from services.specs import (  # noqa: PLC0415
        compiler_service,
        story_validation_service,
    )

    assert (
        orchestrator_query_service.ValidationEvidence.__module__ == "utils.spec_schemas"
    )
    assert (
        compiler_service.SpecAuthorityCompilerInput.__module__ == "utils.spec_schemas"
    )
    assert story_validation_service.AlignmentFinding.__module__ == "utils.spec_schemas"
    assert agent.SpecAuthorityCompilerInput.__module__ == "utils.spec_schemas"


def test_python_modules_do_not_import_compat_schemes_directly() -> None:
    """Verify python modules do not import compat schemes directly."""
    assert _python_files_importing_compat_schemes() == []


def test_spec_authority_success_defaults_v2_schema_version() -> None:
    """Compiled authority success artifacts default to the v2 schema version."""
    from utils.spec_schemas import (  # noqa: PLC0415
        SpecAuthorityCompilationSuccess,
    )

    success = SpecAuthorityCompilationSuccess(
        scope_themes=["Payments"],
        domain=None,
        invariants=[],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[],
        compiler_version="2.0.0",
        prompt_hash="a" * 64,
    )

    assert success.schema_version == "agileforge.compiled_authority.v2"
    assert (
        success.model_dump()["schema_version"]
        == "agileforge.compiled_authority.v2"
    )


def test_behavioral_invariants_keep_provenance_top_level() -> None:
    """Behavioral params stay semantic-only while invariants carry provenance."""
    from utils.spec_schemas import (  # noqa: PLC0415
        Invariant,
        InvariantType,
        UserInteractionParams,
    )

    invariant = Invariant(
        id="INV-0123456789abcdef",
        type=InvariantType.USER_INTERACTION,
        source_item_id="REQ.todo-create",
        source_level="MUST",
        parameters=UserInteractionParams(
            trigger="user presses Enter",
            target="todo input",
            expected_response="create a todo item",
        ),
    )

    assert invariant.source_item_id == "REQ.todo-create"
    assert invariant.source_level == "MUST"
    assert isinstance(invariant.parameters, UserInteractionParams)
    assert invariant.parameters.trigger == "user presses Enter"

    with pytest.raises(ValidationError):
        UserInteractionParams.model_validate(
            {
                "source_item_id": "REQ.todo-create",
                "source_level": "MUST",
                "trigger": "user presses Enter",
                "target": "todo input",
                "expected_response": "create a todo item",
            }
        )


def test_authority_quality_report_schema_is_optional_and_strict() -> None:
    """Compiled authority v2 supports optional quality report metadata."""
    from utils.spec_schemas import (  # noqa: PLC0415
        AuthorityQualityMergedItem,
        AuthorityQualityReport,
        AuthorityQualityReviewGroup,
        AuthorityQualitySummary,
        SpecAuthorityCompilationSuccess,
    )

    success = SpecAuthorityCompilationSuccess(
        scope_themes=["Payments"],
        domain=None,
        invariants=[],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[],
        compiler_version="2.0.0",
        prompt_hash="a" * 64,
    )
    assert success.authority_quality is None

    report = AuthorityQualityReport(
        summary=AuthorityQualitySummary(
            original_invariant_count=2,
            final_invariant_count=1,
            merged_invariant_count=1,
            merged_assumption_count=0,
            review_group_count=1,
            near_duplicate_group_count=0,
            over_split_group_count=1,
            noisy_assumption_group_count=0,
        ),
        merged_items=[
            AuthorityQualityMergedItem(
                merge_id="AQ-MERGE-001",
                item_kind="invariant",
                kept_id="INV-1111111111111111",
                removed_ids=["INV-2222222222222222"],
                reason="exact_semantic_duplicate",
                source_evidence_count=2,
            )
        ],
        review_groups=[
            AuthorityQualityReviewGroup(
                group_id="AQ-GROUP-001",
                group_type="over_split_invariants",
                severity="warning",
                member_ids=["INV-1111111111111111"],
                reason="same source item produced many invariants",
                merge_allowed=False,
            )
        ],
    )
    success.authority_quality = report

    dumped = success.model_dump(mode="json")
    assert dumped["authority_quality"]["schema_version"] == (
        "agileforge.authority_quality.v1"
    )
    assert dumped["authority_quality"]["summary"]["merged_invariant_count"] == 1

    with pytest.raises(ValidationError):
        AuthorityQualityReviewGroup(
            group_id="AQ-GROUP-002",
            group_type=cast("Any", "unsupported"),
            severity="warning",
            member_ids=[],
            reason="bad type",
            merge_allowed=False,
        )


def test_compat_schemes_reexports_authority_quality_models() -> None:
    """Compatibility schema module re-exports authority quality models."""
    from utils import schemes, spec_schemas  # noqa: PLC0415

    assert schemes.AuthorityQualityReport is spec_schemas.AuthorityQualityReport
    assert schemes.AuthorityQualitySummary is spec_schemas.AuthorityQualitySummary
    assert (
        schemes.AuthorityQualityReviewGroup
        is spec_schemas.AuthorityQualityReviewGroup
    )
    assert schemes.AuthorityQualityMergedItem is spec_schemas.AuthorityQualityMergedItem
