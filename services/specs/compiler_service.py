# services/specs/compiler_service.py
"""Compile accepted typed Specifications into reviewable Authority artifacts."""

from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlmodel import Session, col, select

from adapters.adk.prompts import specification as instructions_source
from models.core import Project
from models.db import get_engine
from models.enums import SpecAuthorityStatus
from models.product_definition import SpecificationCandidate
from models.specs import (
    CompiledSpecAuthority,
    SpecAuthorityAcceptance,
    SpecRegistry,
)
from services.agent_workbench.error_codes import ErrorCode
from services.contracts.authority_input_v2 import (
    AuthorityInputV2,
    build_authority_input_v2,
)
from services.contracts.specification import render_invariant_summary
from services.contracts.specification_normalizer import normalize_compiler_output
from services.specs._engine_resolution import resolve_spec_engine
from services.specs.authority_quality import apply_authority_quality_gate
from services.specs.authority_selection import (
    accepted_compiled_authority,
    latest_compiled_authority,
    latest_compiled_authority_for_project,
)
from services.specs.candidate_contract import load_candidate_contract
from utils.runtime_config import SPEC_AUTHORITY_COMPILER_IDENTITY
from utils.spec_schemas import (
    SpecAuthorityCompilationFailure,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerInput,
    SpecAuthorityCompilerOutput,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from google.adk.tools import ToolContext
    from sqlalchemy.engine import Connection, Engine

logger: logging.Logger = logging.getLogger(name=__name__)
SPEC_AUTHORITY_COMPILER_INSTRUCTIONS = (
    instructions_source.SPEC_AUTHORITY_COMPILER_INSTRUCTIONS
)
SPEC_AUTHORITY_COMPILER_VERSION = instructions_source.SPEC_AUTHORITY_COMPILER_VERSION
COMPILED_AUTHORITY_SCHEMA_VERSION: str = "agileforge.compiled_authority.v3"
DEFAULT_AUTHORITY_COMPILE_HEARTBEAT_SECONDS: float = 60.0
DEFAULT_AUTHORITY_COMPILE_TIMEOUT_SECONDS: float = 1800.0
_DEFAULT_GET_ENGINE: Callable[[], Engine | Connection] = get_engine

CompiledAuthorityLoadStatus = Literal[
    "success",
    "missing",
    "invalid_json",
    "schema_invalid",
    "schema_unsupported",
    "compiler_failure",
]


class SpecAuthorityGateError(RuntimeError):
    """Raised when downstream work has no separately accepted Authority."""

    @classmethod
    def requires_review(cls, project_id: int) -> SpecAuthorityGateError:
        """Build the independent human-review gate failure."""
        return cls(
            f"Project {project_id} has no accepted Authority. Run "
            f"`agileforge workflow next --project-id {project_id}` and complete "
            "the separate Authority review."
        )


class AuthorityPersistenceError(ValueError):
    """Raised when typed Authority cannot be bound to an approved Specification."""

    @classmethod
    def not_approved(cls) -> AuthorityPersistenceError:
        """Build the approved-registry precondition failure."""
        return cls("Only an approved Specification can compile.")

    @classmethod
    def candidate_identity(cls) -> AuthorityPersistenceError:
        """Build the exact candidate identity failure."""
        return cls(
            "Approved Specification candidate identity does not match the registry."
        )

    @classmethod
    def candidate_envelope(cls) -> AuthorityPersistenceError:
        """Build the canonical candidate-envelope failure."""
        return cls("Approved Specification candidate envelope is invalid.")

    @classmethod
    def candidate_lineage(cls) -> AuthorityPersistenceError:
        """Build the direct Vision and Product Goal lineage failure."""
        return cls(
            "Approved Specification candidate lineage does not match the registry."
        )

    @classmethod
    def typed_source(cls, details: str) -> AuthorityPersistenceError:
        """Build the compiler-output citation failure."""
        return cls(f"Compiled Authority failed typed source validation: {details}")

    @classmethod
    def missing_identity(cls) -> AuthorityPersistenceError:
        """Build the durable primary-key failure."""
        return cls("Compiled Authority has no durable identity.")

    @classmethod
    def operation(cls, message: str) -> AuthorityPersistenceError:
        """Wrap a bounded internal persistence failure."""
        return cls(message)


class _CompilerInputTypeError(TypeError):
    def __init__(self) -> None:
        super().__init__("Compiler tool input must be a mapping or Pydantic model.")


class CompileSpecAuthorityForVersionInput(BaseModel):
    """Select one approved Specification for typed Authority compilation."""

    model_config = ConfigDict(extra="forbid")

    spec_version_id: int = Field(gt=0)
    force_recompile: bool = False


class CheckSpecAuthorityStatusInput(BaseModel):
    """Input for current compiled-Authority status."""

    model_config = ConfigDict(extra="forbid")

    project_id: int = Field(gt=0)


class GetCompiledAuthorityInput(BaseModel):
    """Input for exact compiled-Authority retrieval."""

    model_config = ConfigDict(extra="forbid")

    project_id: int = Field(gt=0)
    spec_version_id: int = Field(gt=0)


@dataclass(frozen=True)
class CompiledArtifactLoadResult:
    """Typed result for stored compiled-Authority loading."""

    status: CompiledAuthorityLoadStatus
    artifact: SpecAuthorityCompilationSuccess | None = None
    error_code: str | None = None
    message: str | None = None
    observed_schema_version: str | None = None
    validation_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "success"

    @property
    def unsupported(self) -> bool:
        return self.status == "schema_unsupported"


@dataclass(frozen=True)
class CompiledAuthorityReadFailure:
    """Stable public failure for a selected stored Authority row."""

    error_code: str
    message: str
    details: dict[str, Any]
    remediation: tuple[str, ...]


@dataclass(frozen=True)
class _CompilerVersionContext:
    spec_version: SpecRegistry
    project: Project | None
    existing_authority: CompiledSpecAuthority | None
    compiler_input: SpecAuthorityCompilerInput


@dataclass(frozen=True)
class _PersistedCompilation:
    authority_id: int
    compiled_artifact_json: str
    compiler_version: str
    prompt_hash: str
    scope_themes_count: int
    invariants_count: int
    recompiled: bool


@dataclass(frozen=True)
class _PersistenceOptions:
    force_recompile: bool
    lease_guard: Callable[[str], bool] | None = None
    record_progress: Callable[[str], bool] | None = None


@dataclass(frozen=True)
class _CompileExecution:
    compiled_at: datetime
    persistence: _PersistenceOptions
    compiler_model: str | None = None
    tool_context: ToolContext | None = None


def _spec_authority_compiler_agent(
    *,
    compiler_model: str | None = None,
) -> object:
    """Load the ADK compiler agent only on an invocation path."""
    from adapters.adk.agents.specification import (  # noqa: PLC0415
        build_spec_authority_compiler_agent,
        root_agent,
    )

    if compiler_model is not None:
        return build_spec_authority_compiler_agent(compiler_model=compiler_model)
    return root_agent


async def invoke_agent_to_text(*args: Any, **kwargs: Any) -> str:  # noqa: ANN401
    """Lazily invoke one ADK agent."""
    from utils.adk_runner import invoke_agent_to_text as invoke  # noqa: PLC0415

    return await invoke(*args, **kwargs)


def get_agent_model_info(agent: object) -> dict[str, Any]:
    """Lazily read ADK model metadata."""
    from utils.adk_runner import get_agent_model_info as get_info  # noqa: PLC0415

    return get_info(agent)


def compiled_authority_schema_unsupported_details(
    *,
    project_id: int,
    spec_version_id: int | None,
    observed_schema_version: str | None,
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "spec_version_id": spec_version_id,
        "observed_schema_version": observed_schema_version,
        "required_schema_version": COMPILED_AUTHORITY_SCHEMA_VERSION,
    }


def compiled_authority_schema_unsupported_remediation(
    *,
    project_id: int,
    spec_version_id: int | None,
) -> list[str]:
    del spec_version_id
    return [f"agileforge workflow next --project-id {project_id}"]


def compiled_authority_read_failure(
    load_result: CompiledArtifactLoadResult,
    *,
    project_id: int,
    spec_version_id: int | None,
    authority_id: int | None,
) -> CompiledAuthorityReadFailure | None:
    """Describe a selected stored row unless it is a valid v3 success."""
    if load_result.ok and load_result.artifact is not None:
        return None
    unsupported = load_result.status == "schema_unsupported"
    error_code = (
        ErrorCode.COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED.value
        if unsupported
        else ErrorCode.COMPILED_AUTHORITY_INVALID.value
    )
    return CompiledAuthorityReadFailure(
        error_code=error_code,
        message=(
            "Compiled authority artifact schema is unsupported."
            if unsupported
            else "Compiled authority artifact is invalid."
        ),
        details={
            "project_id": project_id,
            "spec_version_id": spec_version_id,
            "authority_id": authority_id,
            "load_status": load_result.status,
            "observed_schema_version": load_result.observed_schema_version,
            "required_schema_version": COMPILED_AUTHORITY_SCHEMA_VERSION,
        },
        remediation=tuple(
            compiled_authority_schema_unsupported_remediation(
                project_id=project_id,
                spec_version_id=spec_version_id,
            )
        ),
    )


def _compiled_authority_read_failure_envelope(
    failure: CompiledAuthorityReadFailure,
    *,
    cached: bool | None = None,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "success": False,
        "error": failure.message,
        "error_code": failure.error_code,
        "details": failure.details,
        "remediation": list(failure.remediation),
    }
    if cached is not None:
        envelope["cached"] = cached
    return envelope


def load_compiled_artifact(authority: object) -> CompiledArtifactLoadResult:
    """Load stored compiled JSON and reject every non-v3 artifact."""
    artifact_json = getattr(authority, "compiled_artifact_json", None)
    if not artifact_json:
        result = CompiledArtifactLoadResult(
            status="missing",
            message="Compiled authority artifact is missing.",
        )
    else:
        try:
            parsed = json.loads(artifact_json)
        except (TypeError, ValueError) as error:
            result = CompiledArtifactLoadResult(
                status="invalid_json",
                message="Compiled authority artifact JSON is invalid.",
                validation_error=str(error),
            )
        else:
            if not isinstance(parsed, dict):
                result = CompiledArtifactLoadResult(
                    status="schema_invalid",
                    message="Compiled authority artifact must be a JSON object.",
                )
            else:
                observed = parsed.get("schema_version")
                if observed != COMPILED_AUTHORITY_SCHEMA_VERSION:
                    result = CompiledArtifactLoadResult(
                        status="schema_unsupported",
                        error_code=(
                            ErrorCode.COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED.value
                        ),
                        message="Compiled authority artifact schema is unsupported.",
                        observed_schema_version=(
                            observed if isinstance(observed, str) else None
                        ),
                    )
                else:
                    try:
                        output = SpecAuthorityCompilerOutput.model_validate(parsed)
                    except ValidationError as error:
                        result = CompiledArtifactLoadResult(
                            status="schema_invalid",
                            message=(
                                "Compiled authority artifact failed schema validation."
                            ),
                            observed_schema_version=(COMPILED_AUTHORITY_SCHEMA_VERSION),
                            validation_error=str(error),
                        )
                    else:
                        if isinstance(
                            output.root,
                            SpecAuthorityCompilationFailure,
                        ):
                            result = CompiledArtifactLoadResult(
                                status="compiler_failure",
                                message=(
                                    "Compiled authority artifact is a compiler failure."
                                ),
                                observed_schema_version=(
                                    COMPILED_AUTHORITY_SCHEMA_VERSION
                                ),
                            )
                        else:
                            result = CompiledArtifactLoadResult(
                                status="success",
                                artifact=output.root,
                                observed_schema_version=(
                                    COMPILED_AUTHORITY_SCHEMA_VERSION
                                ),
                            )
    return result


def _canonical_artifact_json(success: SpecAuthorityCompilationSuccess) -> str:
    return json.dumps(
        success.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _normalize_input_params(params: object) -> dict[str, Any]:
    if params is None:
        return {}
    if isinstance(params, BaseModel):
        return params.model_dump(exclude_none=True)
    if isinstance(params, dict):
        return cast("dict[str, Any]", params).copy()
    raise _CompilerInputTypeError


def _resolve_engine() -> Engine | Connection | None:
    return cast(
        "Engine | Connection | None",
        resolve_spec_engine(
            service_get_engine=get_engine,
            default_service_get_engine=_DEFAULT_GET_ENGINE,
        ),
    )


def _candidate_for_spec(
    session: Session,
    spec: SpecRegistry,
) -> SpecificationCandidate | None:
    """Select the one candidate named by the registry's complete identity."""
    return session.exec(
        select(SpecificationCandidate).where(
            SpecificationCandidate.project_id == spec.project_id,
            SpecificationCandidate.specification_candidate_id
            == spec.source_specification_candidate_id,
            SpecificationCandidate.candidate_fingerprint
            == spec.source_specification_candidate_fingerprint,
            SpecificationCandidate.payload_fingerprint == spec.spec_hash,
        )
    ).one_or_none()


def _compiler_input_for_spec(
    session: Session,
    spec: SpecRegistry,
) -> SpecAuthorityCompilerInput:
    """Build the only compiler input from one exact approved candidate."""
    if spec.status != "approved" or spec.spec_version_id is None:
        raise AuthorityPersistenceError.not_approved()
    candidate = _candidate_for_spec(session, spec)
    if candidate is None:
        raise AuthorityPersistenceError.candidate_identity()
    try:
        payload, envelope = load_candidate_contract(
            candidate.canonical_envelope_json,
            expected_candidate_fingerprint=candidate.candidate_fingerprint,
        )
    except (TypeError, ValueError) as error:
        raise AuthorityPersistenceError.candidate_envelope() from error
    if not (
        candidate.payload_fingerprint == spec.spec_hash == envelope.payload_fingerprint
        and candidate.vision_artifact_id
        == spec.source_vision_artifact_id
        == envelope.accepted_vision_id
        and candidate.vision_fingerprint
        == spec.source_vision_fingerprint
        == envelope.accepted_vision_fingerprint
        and candidate.product_goal_artifact_id
        == spec.source_product_goal_artifact_id
        == envelope.accepted_product_goal_id
        and candidate.product_goal_fingerprint
        == spec.source_product_goal_fingerprint
        == envelope.accepted_product_goal_fingerprint
    ):
        raise AuthorityPersistenceError.candidate_lineage()
    return SpecAuthorityCompilerInput(
        authority_input=build_authority_input_v2(payload),
        project_id=spec.project_id,
        spec_version_id=spec.spec_version_id,
        specification_fingerprint=spec.spec_hash,
    )


def _load_compile_version_context(
    session: Session,
    *,
    spec_version_id: int,
) -> _CompilerVersionContext | dict[str, Any]:
    spec = session.get(SpecRegistry, spec_version_id)
    if spec is None:
        return {"success": False, "error": f"Spec version {spec_version_id} not found"}
    try:
        compiler_input = _compiler_input_for_spec(session, spec)
    except AuthorityPersistenceError as error:
        return {"success": False, "error": str(error)}
    return _CompilerVersionContext(
        spec_version=spec,
        project=session.get(Project, spec.project_id),
        existing_authority=latest_compiled_authority(
            session,
            spec_version_id=spec_version_id,
        ),
        compiler_input=compiler_input,
    )


def _run_async_task[T](coro: Coroutine[Any, Any, T]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as executor:
        return cast("T", executor.submit(asyncio.run, coro).result())


async def _invoke_spec_authority_compiler_async(
    input_payload: SpecAuthorityCompilerInput,
    *,
    compiler_model: str | None = None,
) -> str:
    return await invoke_agent_to_text(
        agent=_spec_authority_compiler_agent(compiler_model=compiler_model),
        runner_identity=SPEC_AUTHORITY_COMPILER_IDENTITY,
        payload_json=input_payload.model_dump_json(),
        no_text_error="Compiler agent returned no text response",
    )


def _invoke_spec_authority_compiler(
    input_payload: SpecAuthorityCompilerInput,
    compiler_model: str | None = None,
) -> str:
    """Invoke the compiler with no raw Specification or file-reference arguments."""
    return _run_async_task(
        _invoke_spec_authority_compiler_async(
            input_payload,
            compiler_model=compiler_model,
        )
    )


def _normalized_success(
    raw_json: str,
    *,
    authority_input: AuthorityInputV2,
) -> SpecAuthorityCompilationSuccess | dict[str, Any]:
    normalized = normalize_compiler_output(
        raw_json,
        authority_input=authority_input,
    )
    if isinstance(normalized.root, SpecAuthorityCompilationFailure):
        return {
            "success": False,
            "error": normalized.root.error,
            "reason": normalized.root.reason,
            "blocking_gaps": normalized.root.blocking_gaps,
        }
    return normalized.root


def _invoke_compiler_for_version(
    context: _CompilerVersionContext,
    *,
    compiler_model: str | None = None,
) -> SpecAuthorityCompilationSuccess | dict[str, Any]:
    try:
        raw_json = _invoke_spec_authority_compiler(
            context.compiler_input,
            compiler_model,
        )
    except Exception as error:  # provider boundary is converted to a stable failure
        logger.exception("Typed Authority compiler invocation failed")
        return {
            "success": False,
            "error": "SPEC_COMPILATION_FAILED",
            "reason": type(error).__name__,
        }
    return _normalized_success(
        raw_json,
        authority_input=context.compiler_input.authority_input,
    )


def _validate_precomputed_authority(
    compiled_authority: SpecAuthorityCompilationSuccess,
    *,
    authority_input: AuthorityInputV2,
) -> SpecAuthorityCompilationSuccess:
    """Recheck workflow-produced output before any durable write."""
    normalized = normalize_compiler_output(
        SpecAuthorityCompilerOutput(root=compiled_authority).model_dump_json(),
        authority_input=authority_input,
    )
    if isinstance(normalized.root, SpecAuthorityCompilationFailure):
        details = "; ".join(normalized.root.blocking_gaps)
        raise AuthorityPersistenceError.typed_source(details)
    return apply_authority_quality_gate(normalized.root)


def _persist_compiled_authority(
    session: Session,
    *,
    context: _CompilerVersionContext,
    success: SpecAuthorityCompilationSuccess,
    compiled_at: datetime,
    options: _PersistenceOptions,
) -> _PersistedCompilation | dict[str, Any]:
    success = _validate_precomputed_authority(
        success,
        authority_input=context.compiler_input.authority_input,
    )
    boundary = "compiled_authority_persisted"
    if options.lease_guard is not None and not options.lease_guard(boundary):
        return {
            "success": False,
            "error": "MUTATION_LEASE_LOST",
            "error_code": "MUTATION_IN_PROGRESS",
        }
    artifact_json = _canonical_artifact_json(success)
    authority = CompiledSpecAuthority(
        spec_version_id=context.compiler_input.spec_version_id,
        compiler_version=SPEC_AUTHORITY_COMPILER_VERSION,
        prompt_hash=instructions_source.SPEC_AUTHORITY_COMPILER_PROMPT_HASH,
        compiled_at=compiled_at,
        compiled_artifact_json=artifact_json,
        scope_themes=json.dumps(success.scope_themes),
        invariants=json.dumps(
            [render_invariant_summary(item) for item in success.invariants]
        ),
        eligible_feature_ids=json.dumps([]),
        rejected_features=json.dumps(success.rejected_features),
        spec_gaps=json.dumps(success.gaps),
    )
    session.add(authority)
    session.flush()
    if options.record_progress is not None and not options.record_progress(boundary):
        return {
            "success": False,
            "error": "MUTATION_PROGRESS_FAILED",
            "error_code": "MUTATION_IN_PROGRESS",
        }
    if authority.authority_id is None:
        raise AuthorityPersistenceError.missing_identity()
    return _PersistedCompilation(
        authority_id=authority.authority_id,
        compiled_artifact_json=artifact_json,
        compiler_version=authority.compiler_version,
        prompt_hash=authority.prompt_hash,
        scope_themes_count=len(success.scope_themes),
        invariants_count=len(success.invariants),
        recompiled=options.force_recompile,
    )


def _cached_compilation_result(
    context: _CompilerVersionContext,
) -> dict[str, Any] | None:
    authority = context.existing_authority
    if authority is None:
        return None
    load_result = load_compiled_artifact(authority)
    failure = compiled_authority_read_failure(
        load_result,
        project_id=context.spec_version.project_id,
        spec_version_id=context.spec_version.spec_version_id,
        authority_id=authority.authority_id,
    )
    if failure is not None:
        return _compiled_authority_read_failure_envelope(failure, cached=True)
    return {
        "success": True,
        "cached": True,
        "recompiled": False,
        "authority_id": authority.authority_id,
        "spec_version_id": context.spec_version.spec_version_id,
        "compiler_version": authority.compiler_version,
        "prompt_hash": authority.prompt_hash,
        "message": (
            f"Spec version {context.spec_version.spec_version_id} is already compiled "
            f"(authority ID: {authority.authority_id})."
        ),
    }


def _normalize_compile_version_input(
    params: dict[str, Any] | CompileSpecAuthorityForVersionInput | None,
    *,
    spec_version_id: int | None,
    force_recompile: bool | None,
) -> CompileSpecAuthorityForVersionInput:
    merged = _normalize_input_params(params)
    if spec_version_id is not None:
        merged["spec_version_id"] = spec_version_id
    if force_recompile is not None:
        merged["force_recompile"] = force_recompile
    return CompileSpecAuthorityForVersionInput.model_validate(merged)


def _compile_spec_authority_for_version_in_session(
    session: Session,
    *,
    parsed: CompileSpecAuthorityForVersionInput,
    execution: _CompileExecution,
) -> dict[str, Any]:
    context = _load_compile_version_context(
        session,
        spec_version_id=parsed.spec_version_id,
    )
    if not isinstance(context, _CompilerVersionContext):
        return context
    if not parsed.force_recompile:
        cached = _cached_compilation_result(context)
        if cached is not None:
            return cached
    compiled = _invoke_compiler_for_version(
        context,
        compiler_model=execution.compiler_model,
    )
    if isinstance(compiled, dict):
        return compiled
    persisted = _persist_compiled_authority(
        session,
        context=context,
        success=compiled,
        compiled_at=execution.compiled_at,
        options=execution.persistence,
    )
    if not isinstance(persisted, _PersistedCompilation):
        return cast("dict[str, Any]", persisted)
    if execution.tool_context is not None and execution.tool_context.state is not None:
        execution.tool_context.state["compiled_authority_cached"] = (
            persisted.compiled_artifact_json
        )
    return {
        "success": True,
        "cached": False,
        "recompiled": persisted.recompiled,
        "authority_id": persisted.authority_id,
        "spec_version_id": parsed.spec_version_id,
        "compiler_version": persisted.compiler_version,
        "prompt_hash": persisted.prompt_hash,
        "scope_themes_count": persisted.scope_themes_count,
        "invariants_count": persisted.invariants_count,
        "message": (
            f"Compiled spec version {parsed.spec_version_id} "
            f"(authority ID: {persisted.authority_id})"
        ),
    }


def compile_spec_authority_for_version_in_session(
    session: Session,
    *,
    spec_version_id: int,
    compiled_at: datetime,
    force_recompile: bool = False,
    compiler_model: str | None = None,
) -> dict[str, Any]:
    """Compile one accepted typed Specification in a caller-owned transaction."""
    parsed = CompileSpecAuthorityForVersionInput(
        spec_version_id=spec_version_id,
        force_recompile=force_recompile,
    )
    return _compile_spec_authority_for_version_in_session(
        session,
        parsed=parsed,
        execution=_CompileExecution(
            compiled_at=compiled_at,
            compiler_model=compiler_model,
            persistence=_PersistenceOptions(force_recompile=force_recompile),
        ),
    )


def persist_compiled_authority_for_version_in_session(
    session: Session,
    *,
    spec_version_id: int,
    compiled_authority: SpecAuthorityCompilationSuccess,
    compiled_at: datetime,
    force_recompile: bool = False,
) -> int:
    """Validate and persist workflow-produced Authority in the caller transaction."""
    context = _load_compile_version_context(
        session,
        spec_version_id=spec_version_id,
    )
    if not isinstance(context, _CompilerVersionContext):
        raise AuthorityPersistenceError.operation(
            str(context.get("error") or "Authority context is unavailable.")
        )
    persisted = _persist_compiled_authority(
        session,
        context=context,
        success=compiled_authority,
        compiled_at=compiled_at,
        options=_PersistenceOptions(force_recompile=force_recompile),
    )
    if not isinstance(persisted, _PersistedCompilation):
        failure = cast("dict[str, Any]", persisted)
        raise AuthorityPersistenceError.operation(
            str(failure.get("error") or "Authority persistence failed.")
        )
    return persisted.authority_id


def compile_spec_authority_for_version(
    params: dict[str, Any] | CompileSpecAuthorityForVersionInput | None = None,
    *,
    spec_version_id: int | None = None,
    force_recompile: bool | None = None,
    tool_context: ToolContext | None = None,
    compiler_model: str | None = None,
) -> dict[str, Any]:
    """Compile only the exact approved v2 candidate selected by version."""
    parsed = _normalize_compile_version_input(
        params,
        spec_version_id=spec_version_id,
        force_recompile=force_recompile,
    )
    with Session(_resolve_engine()) as session:
        result = _compile_spec_authority_for_version_in_session(
            session,
            parsed=parsed,
            execution=_CompileExecution(
                compiled_at=datetime.now(UTC),
                compiler_model=compiler_model,
                tool_context=tool_context,
                persistence=_PersistenceOptions(force_recompile=parsed.force_recompile),
            ),
        )
        if result.get("success") is True and result.get("cached") is not True:
            session.commit()
        return result


def compile_spec_authority_for_version_with_engine(  # noqa: PLR0913
    *,
    engine: Engine,
    spec_version_id: int,
    force_recompile: bool | None = None,
    tool_context: ToolContext | None = None,
    compiler_model: str | None = None,
    lease_guard: Callable[[str], bool] | None = None,
    record_progress: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    parsed = CompileSpecAuthorityForVersionInput(
        spec_version_id=spec_version_id,
        force_recompile=bool(force_recompile),
    )
    with Session(engine) as session:
        result = _compile_spec_authority_for_version_in_session(
            session,
            parsed=parsed,
            execution=_CompileExecution(
                compiled_at=datetime.now(UTC),
                compiler_model=compiler_model,
                tool_context=tool_context,
                persistence=_PersistenceOptions(
                    force_recompile=parsed.force_recompile,
                    lease_guard=lease_guard,
                    record_progress=record_progress,
                ),
            ),
        )
        if result.get("success") is True and result.get("cached") is not True:
            session.commit()
        return result


def ensure_accepted_spec_authority(
    project_id: int,
    *,
    recompile: bool = False,
    tool_context: ToolContext | None = None,
) -> int:
    """Return an accepted Authority or stop at the independent review gate."""
    del recompile, tool_context
    with Session(_resolve_engine()) as session:
        current_spec = session.exec(
            select(SpecRegistry).where(
                SpecRegistry.project_id == project_id,
                SpecRegistry.status == "approved",
            )
        ).one_or_none()
        if current_spec is None or current_spec.spec_version_id is None:
            raise SpecAuthorityGateError.requires_review(project_id)
        acceptance = session.exec(
            select(SpecAuthorityAcceptance)
            .where(
                SpecAuthorityAcceptance.project_id == project_id,
                SpecAuthorityAcceptance.spec_version_id
                == current_spec.spec_version_id,
                SpecAuthorityAcceptance.status == "accepted",
            )
            .order_by(
                col(SpecAuthorityAcceptance.decided_at).desc(),
                col(SpecAuthorityAcceptance.id).desc(),
            )
        ).first()
        if acceptance is not None:
            authority = accepted_compiled_authority(
                session,
                project_id=project_id,
                spec_version_id=acceptance.spec_version_id,
            )
            latest_authority = latest_compiled_authority(
                session,
                spec_version_id=acceptance.spec_version_id,
            )
            if (
                authority is not None
                and latest_authority is not None
                and authority.authority_id == latest_authority.authority_id
                and load_compiled_artifact(authority).ok
            ):
                return acceptance.spec_version_id
    raise SpecAuthorityGateError.requires_review(project_id)


def _source_metadata_retry_commands(spec_version: SpecRegistry) -> list[str]:
    """Orient any compiler recovery through the registered workflow."""
    return [f"agileforge workflow next --project-id {spec_version.project_id}"]


def check_spec_authority_status(
    params: dict[str, Any] | CheckSpecAuthorityStatusInput | None = None,
    *,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Return current typed Authority compilation status for one project."""
    del tool_context
    parsed = CheckSpecAuthorityStatusInput.model_validate(
        _normalize_input_params(params)
    )
    with Session(_resolve_engine()) as session:
        latest_spec = session.exec(
            select(SpecRegistry)
            .where(SpecRegistry.project_id == parsed.project_id)
            .order_by(col(SpecRegistry.spec_version_id).desc())
        ).first()
        if latest_spec is None:
            return {
                "success": True,
                "status": SpecAuthorityStatus.NOT_COMPILED.value,
                "status_details": "No spec versions exist for this project",
            }
        authority = latest_compiled_authority_for_project(
            session,
            project_id=parsed.project_id,
        )
        if authority is None:
            return {
                "success": True,
                "status": SpecAuthorityStatus.NOT_COMPILED.value,
                "latest_approved_spec_version_id": latest_spec.spec_version_id,
            }
        if authority.spec_version_id != latest_spec.spec_version_id:
            return {
                "success": True,
                "status": SpecAuthorityStatus.STALE.value,
                "compiled_spec_version_id": authority.spec_version_id,
                "latest_approved_spec_version_id": latest_spec.spec_version_id,
            }
        failure = compiled_authority_read_failure(
            load_compiled_artifact(authority),
            project_id=parsed.project_id,
            spec_version_id=authority.spec_version_id,
            authority_id=authority.authority_id,
        )
        if failure is not None:
            return _compiled_authority_read_failure_envelope(failure)
        return {
            "success": True,
            "status": SpecAuthorityStatus.CURRENT.value,
            "latest_approved_spec_version_id": latest_spec.spec_version_id,
            "authority_id": authority.authority_id,
            "compiled_at": authority.compiled_at.isoformat(),
        }


def get_compiled_authority_by_version(
    params: dict[str, Any] | GetCompiledAuthorityInput | None = None,
    *,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Retrieve the newest compiled Authority for one exact Specification."""
    del tool_context
    parsed = GetCompiledAuthorityInput.model_validate(_normalize_input_params(params))
    with Session(_resolve_engine()) as session:
        spec = session.get(SpecRegistry, parsed.spec_version_id)
        if spec is None or spec.project_id != parsed.project_id:
            return {
                "success": False,
                "error": "Specification version was not found for this project.",
            }
        authority = latest_compiled_authority(
            session,
            spec_version_id=parsed.spec_version_id,
        )
        if authority is None:
            return {
                "success": False,
                "error_code": ErrorCode.AUTHORITY_NOT_COMPILED.value,
                "error": f"Spec version {parsed.spec_version_id} is not compiled.",
            }
        load_result = load_compiled_artifact(authority)
        failure = compiled_authority_read_failure(
            load_result,
            project_id=parsed.project_id,
            spec_version_id=parsed.spec_version_id,
            authority_id=authority.authority_id,
        )
        if failure is not None:
            return _compiled_authority_read_failure_envelope(failure)
        artifact = cast("SpecAuthorityCompilationSuccess", load_result.artifact)
        return {
            "success": True,
            "spec_version_id": parsed.spec_version_id,
            "authority_id": authority.authority_id,
            "compiler_version": authority.compiler_version,
            "compiled_at": authority.compiled_at.isoformat(),
            "scope_themes": artifact.scope_themes,
            "invariants": [
                render_invariant_summary(invariant) for invariant in artifact.invariants
            ],
            "eligible_feature_ids": json.loads(authority.eligible_feature_ids),
            "rejected_features": artifact.rejected_features,
            "spec_gaps": artifact.gaps,
            "compiled_artifact_json": authority.compiled_artifact_json,
        }


__all__ = [
    "AuthorityPersistenceError",
    "CheckSpecAuthorityStatusInput",
    "CompileSpecAuthorityForVersionInput",
    "GetCompiledAuthorityInput",
    "check_spec_authority_status",
    "compile_spec_authority_for_version",
    "compile_spec_authority_for_version_in_session",
    "compiled_authority_read_failure",
    "ensure_accepted_spec_authority",
    "get_compiled_authority_by_version",
    "load_compiled_artifact",
    "persist_compiled_authority_for_version_in_session",
]
