# tools/spec_tools.py
"""Typed Authority selection and downstream story-validation tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from services.contracts.specification import render_invariant_summary
from services.specs.compiler_service import (
    CheckSpecAuthorityStatusInput,
    CompiledArtifactLoadResult,
    CompileSpecAuthorityForVersionInput,
    GetCompiledAuthorityInput,
    load_compiled_artifact,
)
from services.specs.compiler_service import (
    check_spec_authority_status as _check_spec_authority_status,
)
from services.specs.compiler_service import (
    compile_spec_authority_for_version as _compile_spec_authority_for_version,
)
from services.specs.compiler_service import (
    ensure_accepted_spec_authority as _ensure_accepted_spec_authority,
)
from services.specs.compiler_service import (
    get_compiled_authority_by_version as _get_compiled_authority_by_version,
)
from services.specs.story_validation_service import (
    LlmValidationResult,
    ValidateStoryInput,
    compute_story_input_hash,
    invoke_spec_validator_async,
    parse_llm_validator_response,
    persist_validation_evidence,
    resolve_default_validation_mode,
    run_deterministic_alignment_checks,
    run_llm_spec_validation,
    run_structural_story_checks,
)
from services.specs.story_validation_service import (
    validate_story_with_spec_authority as _validate_story_with_spec_authority,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from google.adk.tools import ToolContext
    from sqlmodel import Session

    from models.core import Feature, UserStory
    from models.specs import CompiledSpecAuthority
    from utils.spec_schemas import (
        AlignmentFinding,
        Invariant,
        ValidationEvidence,
        ValidationFailure,
    )


class CompileSpecAuthorityForVersionToolInput(BaseModel):
    """Select one approved Specification version; never upload its content."""

    model_config = ConfigDict(extra="forbid")

    spec_version_id: int = Field(gt=0)
    force_recompile: bool = False


def compile_spec_authority_for_version(
    params: (
        dict[str, Any]
        | CompileSpecAuthorityForVersionInput
        | CompileSpecAuthorityForVersionToolInput
    ),
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Compile the exact accepted typed candidate behind one registry version."""
    normalized: dict[str, Any] | CompileSpecAuthorityForVersionInput
    if isinstance(params, CompileSpecAuthorityForVersionToolInput):
        normalized = params.model_dump()
    else:
        normalized = params
    return _compile_spec_authority_for_version(
        normalized,
        tool_context=tool_context,
    )


def ensure_accepted_spec_authority(
    project_id: int,
    *,
    recompile: bool = False,
    tool_context: ToolContext | None = None,
) -> int:
    """Preserve the independent human Authority acceptance gate."""
    return _ensure_accepted_spec_authority(
        project_id,
        recompile=recompile,
        tool_context=tool_context,
    )


def check_spec_authority_status(
    params: dict[str, Any] | CheckSpecAuthorityStatusInput,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Return compiled-Authority status for one project."""
    return _check_spec_authority_status(params, tool_context=tool_context)


def get_compiled_authority_by_version(
    params: dict[str, Any] | GetCompiledAuthorityInput,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Retrieve compiled Authority for one project-owned Specification."""
    return _get_compiled_authority_by_version(params, tool_context=tool_context)


def _load_compiled_artifact(
    authority: CompiledSpecAuthority,
) -> CompiledArtifactLoadResult:
    return load_compiled_artifact(authority)


def _render_invariant_summary(invariant: Invariant) -> str:
    return render_invariant_summary(invariant)


VALIDATOR_VERSION = "1.0.0"


def _resolve_default_validation_mode() -> str:
    return resolve_default_validation_mode()


def _compute_story_input_hash(story: object) -> str:
    return compute_story_input_hash(story)


def _persist_validation_evidence(
    session: Session,
    story: UserStory,
    evidence: ValidationEvidence,
    passed: bool,
) -> None:
    persist_validation_evidence(session, story, evidence, passed)


def _run_structural_story_checks(
    story: UserStory,
) -> tuple[list[str], list[ValidationFailure], list[str]]:
    return run_structural_story_checks(story)


def _run_deterministic_alignment_checks(
    story: UserStory,
    authority: CompiledSpecAuthority,
    *,
    load_compiled_artifact_fn: Callable[[CompiledSpecAuthority], Any | None]
    | None = None,
) -> tuple[list[AlignmentFinding], list[AlignmentFinding], list[str]]:
    return run_deterministic_alignment_checks(
        story,
        authority,
        load_compiled_artifact_fn=load_compiled_artifact_fn or _load_compiled_artifact,
    )


async def _invoke_spec_validator_async(payload_text: str) -> str:
    return await invoke_spec_validator_async(payload_text)


def _parse_llm_validator_response(raw_text: str) -> LlmValidationResult:
    return parse_llm_validator_response(raw_text)


def _run_llm_spec_validation(
    story: UserStory,
    authority: CompiledSpecAuthority,
    artifact: object | None,
    feature: Feature | None = None,
) -> LlmValidationResult:
    return run_llm_spec_validation(
        story,
        authority,
        artifact,
        feature=feature,
        invoke_spec_validator_async_fn=_invoke_spec_validator_async,
        parse_llm_validator_response_fn=_parse_llm_validator_response,
    )


def validate_story_with_spec_authority(
    params: dict[str, Any] | ValidateStoryInput,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Validate one story against a separately accepted compiled Authority."""
    return _validate_story_with_spec_authority(
        params,
        tool_context=tool_context,
        resolve_default_validation_mode=_resolve_default_validation_mode,
        compute_story_input_hash_fn=_compute_story_input_hash,
        persist_validation_evidence=_persist_validation_evidence,
        run_structural_story_checks=_run_structural_story_checks,
        run_deterministic_alignment_checks=_run_deterministic_alignment_checks,
        run_llm_spec_validation=_run_llm_spec_validation,
        load_compiled_artifact_fn=_load_compiled_artifact,
        render_invariant_summary_fn=_render_invariant_summary,
        validator_version=VALIDATOR_VERSION,
    )


__all__ = [
    "CheckSpecAuthorityStatusInput",
    "CompileSpecAuthorityForVersionInput",
    "CompileSpecAuthorityForVersionToolInput",
    "GetCompiledAuthorityInput",
    "ValidateStoryInput",
    "check_spec_authority_status",
    "compile_spec_authority_for_version",
    "ensure_accepted_spec_authority",
    "get_compiled_authority_by_version",
    "validate_story_with_spec_authority",
]
