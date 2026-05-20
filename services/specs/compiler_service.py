"""Public helpers for working with compiled spec artifacts."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict, Unpack, cast

from pydantic import BaseModel, Field, ValidationError
from sqlmodel import Session, select

from db.migrations import ensure_schema_current
from models.core import Product
from models.db import get_engine
from models.enums import SpecAuthorityStatus
from models.specs import (
    CompiledSpecAuthority,
    SpecAuthorityAcceptance,
    SpecRegistry,
)
from orchestrator_agent.agent_tools.spec_authority_compiler_agent import (
    compiler_contract,
    instructions_source,
)
from orchestrator_agent.agent_tools.spec_authority_compiler_agent.agent import (
    root_agent as spec_authority_compiler_agent,
)
from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (
    normalize_compiler_output,
)
from services.specs._engine_resolution import resolve_spec_engine
from services.specs.profile_content import (
    STRUCTURED_SPEC_FORMAT,
    SpecContentNormalizationError,
    normalize_spec_content_for_registry,
)
from utils.adk_runner import get_agent_model_info, invoke_agent_to_text
from utils.failure_artifacts import (
    AgentInvocationError,
    FailureMetadataDict,
    write_failure_artifact,
)
from utils.runtime_config import SPEC_AUTHORITY_COMPILER_IDENTITY
from utils.spec_schemas import (
    Invariant,
    InvariantType,
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
_DEFAULT_GET_ENGINE = get_engine
compute_prompt_hash = compiler_contract.compute_prompt_hash
SPEC_AUTHORITY_COMPILER_INSTRUCTIONS = (
    instructions_source.SPEC_AUTHORITY_COMPILER_INSTRUCTIONS
)
SPEC_AUTHORITY_COMPILER_VERSION = instructions_source.SPEC_AUTHORITY_COMPILER_VERSION


class SpecAuthorityAcceptanceError(ValueError):
    """Raised when acceptance persistence preconditions are not satisfied."""

    @classmethod
    def spec_version_not_found(
        cls, spec_version_id: int
    ) -> SpecAuthorityAcceptanceError:
        """Build the canonical missing spec-version error."""
        return cls(f"Spec version {spec_version_id} not found")

    @classmethod
    def wrong_product(
        cls, spec_version_id: int, product_id: int
    ) -> SpecAuthorityAcceptanceError:
        """Build the canonical product-mismatch error."""
        return cls(
            f"Spec version {spec_version_id} does not belong to product {product_id}"
        )

    @classmethod
    def not_compiled(cls, spec_version_id: int) -> SpecAuthorityAcceptanceError:
        """Build the canonical not-compiled error."""
        return cls(f"spec_version_id {spec_version_id} is not compiled")

    @classmethod
    def invalid_artifact(cls, spec_version_id: int) -> SpecAuthorityAcceptanceError:
        """Build the canonical invalid-artifact error."""
        return cls(f"spec_version_id {spec_version_id} compiled artifact invalid")


class SpecAuthorityGateError(RuntimeError):
    """Raised when the story-generation authority gate cannot be satisfied."""

    @classmethod
    def missing_source(cls, product_id: int) -> SpecAuthorityGateError:
        """Build the canonical missing-source error."""
        return cls(
            f"No accepted spec authority exists for product {product_id}, and no "
            "spec_content or content_ref was provided. Please provide the "
            "specification content or a file path to create an authority."
        )

    @classmethod
    def update_failed(
        cls, product_id: int, error_message: str
    ) -> SpecAuthorityGateError:
        """Build the canonical compilation-failed error."""
        return cls(
            f"Failed to create accepted spec authority for product {product_id}: "
            f"{error_message}"
        )

    @classmethod
    def not_accepted(cls, product_id: int) -> SpecAuthorityGateError:
        """Build the canonical not-accepted error."""
        return cls(
            f"Spec authority for product {product_id} was compiled but not "
            "accepted. Authority acceptance is required before story generation "
            "can proceed."
        )

    @classmethod
    def missing_spec_version_id(cls, product_id: int) -> SpecAuthorityGateError:
        """Build the canonical missing-ID error."""
        return cls(
            "Spec authority creation succeeded but no spec_version_id was returned "
            f"for product {product_id}."
        )


class SpecAuthorityCompilationError(TypeError):
    """Raised when normalized compiler output is a failure envelope."""

    @classmethod
    def failed(cls, error: str, reason: str) -> SpecAuthorityCompilationError:
        """Build the canonical normalized-compilation error."""
        return cls(f"Spec authority compilation failed: {error} - {reason}")


class UpdateSpecAuthorityInputError(ValueError):
    """Raised when update+compile input sources are ambiguous."""

    @classmethod
    def exactly_one_source(cls) -> UpdateSpecAuthorityInputError:
        """Build the canonical source-selection error."""
        return cls("Provide exactly one of spec_content or content_ref")


@dataclass(frozen=True)
class _AcceptedAuthorityLookup:
    """State discovered while checking for a reusable accepted authority."""

    reusable_spec_version_id: int | None
    compile_reason: str
    accepted_decision_found: bool
    compiled_row_found: bool
    compiled_artifact_success: bool


@dataclass(frozen=True)
class _CompilerFailureDetails:
    """Structured inputs for failure-artifact persistence."""

    product_id: int | None
    spec_version_id: int | None
    content_ref: str | None
    failure_stage: str
    error: str
    reason: str
    raw_output: str | None = None
    blocking_gaps: list[str] | None = None
    exception: BaseException | None = None


@dataclass(frozen=True)
class _CompilerVersionContext:
    """Resolved compilation inputs for one approved spec version."""

    spec_version: SpecRegistry
    product: Product | None
    existing_authority: CompiledSpecAuthority | None


@dataclass(frozen=True)
class _PersistedCompilation:
    """Stored authority metadata returned after one compile operation."""

    authority_id: int
    compiled_artifact_json: str
    compiler_version: str
    prompt_hash: str
    scope_themes_count: int
    invariants_count: int
    recompiled: bool


class _CompilerFailureOptions(TypedDict, total=False):
    """Keyword arguments accepted by `_compiler_failure_result`."""

    product_id: int | None
    spec_version_id: int | None
    content_ref: str | None
    failure_stage: str
    error: str
    reason: str
    raw_output: str | None
    blocking_gaps: list[str] | None
    exception: BaseException | None


class UpdateSpecAndCompileAuthorityInput(BaseModel):
    """Input schema for update+compile spec workflows."""

    product_id: int = Field(description="Product ID for spec update")
    spec_content: str | None = Field(
        default=None,
        description="Raw specification content to persist and compile",
    )
    content_ref: str | None = Field(
        default=None,
        description="Path/reference to specification content on disk",
    )
    recompile: bool | None = Field(
        default=False,
        description="Force recompilation even if compiled authority already exists",
    )


class CompileSpecAuthorityForVersionInput(BaseModel):
    """Input schema for compile spec authority by version."""

    spec_version_id: int = Field(description="Approved spec version to compile")
    force_recompile: bool | None = Field(
        default=False,
        description="If true, recompile even when cached authority exists",
    )


class PreviewSpecAuthorityInput(BaseModel):
    """Input schema for preview spec authority compilation."""

    content: str = Field(description="The raw specification text to compile.")


class CompileSpecAuthorityInput(BaseModel):
    """Input schema for legacy one-shot spec authority compilation."""

    spec_version_id: int = Field(description="Approved spec version to compile")


class CheckSpecAuthorityStatusInput(BaseModel):
    """Input schema for spec authority status checks."""

    product_id: int = Field(description="Product ID to check status for")


class GetCompiledAuthorityInput(BaseModel):
    """Input schema for deterministic retrieval of compiled authority by version."""

    product_id: int = Field(description="Product ID")
    spec_version_id: int = Field(description="Spec version ID to retrieve")


def _resolve_tool_module() -> object | None:
    """Load the legacy tools module when present."""
    try:
        return importlib.import_module("tools.spec_tools")
    except ImportError:
        return None


def _resolve_tool_override(
    name: str,
    *,
    default_module_name: str,
    default_function_name: str,
) -> object | None:
    """Return a monkeypatched legacy tool callable when it differs from default."""
    spec_tools_module = _resolve_tool_module()
    if spec_tools_module is None:
        return None

    tool_callable = getattr(spec_tools_module, name, None)
    if not callable(tool_callable):
        return None

    module_name = getattr(tool_callable, "__module__", "")
    function_name = getattr(tool_callable, "__name__", "")
    if module_name == default_module_name and function_name == default_function_name:
        return None
    return tool_callable


def _normalize_input_params(params: object) -> dict[str, Any]:
    """Normalize tool params from either a Pydantic model or a raw dict."""
    if isinstance(params, BaseModel):
        dumped = params.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    if isinstance(params, dict):
        return cast("dict[str, Any]", params)
    return {}


def load_compiled_artifact(
    authority: object,
) -> SpecAuthorityCompilationSuccess | None:
    """Load normalized compiled artifact JSON if present and valid."""
    artifact_json = getattr(authority, "compiled_artifact_json", None)
    if not artifact_json:
        return None
    try:
        parsed = SpecAuthorityCompilerOutput.model_validate_json(artifact_json)
    except (ValidationError, ValueError):
        return None
    if isinstance(parsed.root, SpecAuthorityCompilationFailure):
        return None
    return parsed.root


def _load_acceptance_context(
    session: Session,
    *,
    product_id: int,
    spec_version_id: int,
) -> tuple[SpecRegistry, CompiledSpecAuthority]:
    """Load the spec version and compiled authority required for acceptance."""
    spec_version = session.get(SpecRegistry, spec_version_id)
    if not spec_version:
        raise SpecAuthorityAcceptanceError.spec_version_not_found(spec_version_id)
    if spec_version.product_id != product_id:
        raise SpecAuthorityAcceptanceError.wrong_product(
            spec_version_id,
            product_id,
        )

    authority = session.exec(
        select(CompiledSpecAuthority).where(
            CompiledSpecAuthority.spec_version_id == spec_version_id
        )
    ).first()
    if not authority:
        raise SpecAuthorityAcceptanceError.not_compiled(spec_version_id)

    artifact = load_compiled_artifact(authority)
    if not artifact:
        raise SpecAuthorityAcceptanceError.invalid_artifact(spec_version_id)
    return spec_version, authority


def _lookup_reusable_accepted_authority(
    session: Session,
    *,
    product_id: int,
) -> _AcceptedAuthorityLookup:
    """Inspect accepted authority rows and report whether one can be reused."""
    existing_acceptance = session.exec(
        select(SpecAuthorityAcceptance)
        .where(
            SpecAuthorityAcceptance.product_id == product_id,
            SpecAuthorityAcceptance.status == "accepted",
        )
        .order_by(cast("Any", SpecAuthorityAcceptance.decided_at).desc())
    ).first()
    if not existing_acceptance:
        return _AcceptedAuthorityLookup(
            reusable_spec_version_id=None,
            compile_reason="no_accepted_authority",
            accepted_decision_found=False,
            compiled_row_found=False,
            compiled_artifact_success=False,
        )

    compiled = session.exec(
        select(CompiledSpecAuthority).where(
            CompiledSpecAuthority.spec_version_id == existing_acceptance.spec_version_id
        )
    ).first()
    if compiled is None:
        return _AcceptedAuthorityLookup(
            reusable_spec_version_id=None,
            compile_reason="compiled_unusable_or_missing",
            accepted_decision_found=True,
            compiled_row_found=False,
            compiled_artifact_success=False,
        )

    artifact = (
        load_compiled_artifact(compiled) if compiled.compiled_artifact_json else None
    )
    if artifact is None:
        return _AcceptedAuthorityLookup(
            reusable_spec_version_id=None,
            compile_reason="compiled_unusable_or_missing",
            accepted_decision_found=True,
            compiled_row_found=True,
            compiled_artifact_success=False,
        )

    return _AcceptedAuthorityLookup(
        reusable_spec_version_id=existing_acceptance.spec_version_id,
        compile_reason="existing_authority",
        accepted_decision_found=True,
        compiled_row_found=True,
        compiled_artifact_success=True,
    )


def _resolve_gate_path_used(
    *,
    tool_context: ToolContext | None,
    spec_content: str | None,
    content_ref: str | None,
) -> str:
    """Describe whether authority compilation used explicit args or session state."""
    if (
        tool_context
        and tool_context.state
        and (
            tool_context.state.get("pending_spec_content") == spec_content
            or tool_context.state.get("pending_spec_path") == content_ref
        )
    ):
        return "proposal_from_state"
    return "explicit_args"


def _build_update_compile_params(
    *,
    product_id: int,
    spec_content: str | None,
    content_ref: str | None,
    recompile: bool,
) -> tuple[dict[str, Any], str]:
    """Build the update+compile parameter payload and describe its input source."""
    params: dict[str, Any] = {
        "product_id": product_id,
        "recompile": recompile,
    }
    if spec_content is not None:
        params["spec_content"] = spec_content
        return params, "spec_content"

    params["content_ref"] = content_ref
    return params, "content_ref"


def _validate_gate_compile_result(
    *,
    product_id: int,
    result: dict[str, Any],
    gate_logger: logging.Logger,
    session_id: str | None,
    path_used: str,
) -> int:
    """Validate the update+compile response and return a spec version id."""
    if not result.get("success"):
        error_msg = str(result.get("error", "Unknown error"))
        gate_logger.error(
            "authority_gate.fail",
            extra={
                "product_id": product_id,
                "reason": "update_failed",
                "success": result.get("success"),
                "accepted": result.get("accepted"),
                "spec_version_id": result.get("spec_version_id"),
            },
        )
        raise SpecAuthorityGateError.update_failed(product_id, error_msg)

    if not result.get("accepted"):
        gate_logger.error(
            "authority_gate.fail",
            extra={
                "product_id": product_id,
                "reason": "not_accepted",
                "success": result.get("success"),
                "accepted": result.get("accepted"),
                "spec_version_id": result.get("spec_version_id"),
            },
        )
        raise SpecAuthorityGateError.not_accepted(product_id)

    spec_version_id = result.get("spec_version_id")
    if spec_version_id is None:
        gate_logger.error(
            "authority_gate.fail",
            extra={
                "product_id": product_id,
                "reason": "missing_spec_version_id",
                "success": result.get("success"),
                "accepted": result.get("accepted"),
                "spec_version_id": result.get("spec_version_id"),
            },
        )
        raise SpecAuthorityGateError.missing_spec_version_id(product_id)

    gate_logger.info(
        "authority_gate.updated",
        extra={
            "product_id": product_id,
            "session_id": session_id,
            "path_used": path_used,
            "spec_version_id": spec_version_id,
            "accepted": result.get("accepted"),
            "success": result.get("success"),
            "compiler_version": result.get("compiler_version"),
        },
    )
    return int(spec_version_id)


def ensure_spec_authority_accepted(
    *,
    product_id: int,
    spec_version_id: int,
    policy: str,
    decided_by: str,
    rationale: str | None = None,
) -> SpecAuthorityAcceptance:
    """Ensure an accepted authority decision exists for a compiled spec version."""
    with Session(_resolve_engine()) as session:
        spec_version, authority = _load_acceptance_context(
            session,
            product_id=product_id,
            spec_version_id=spec_version_id,
        )

        existing = session.exec(
            select(SpecAuthorityAcceptance).where(
                SpecAuthorityAcceptance.spec_version_id == spec_version_id,
                SpecAuthorityAcceptance.status == "accepted",
            )
        ).first()
        if existing:
            return existing

        acceptance = SpecAuthorityAcceptance(
            product_id=product_id,
            spec_version_id=spec_version_id,
            status="accepted",
            policy=policy,
            decided_by=decided_by,
            decided_at=datetime.now(UTC),
            rationale=rationale,
            compiler_version=authority.compiler_version,
            prompt_hash=authority.prompt_hash,
            spec_hash=spec_version.spec_hash,
        )
        session.add(acceptance)
        session.commit()
        session.refresh(acceptance)
        return acceptance


def ensure_accepted_spec_authority(
    product_id: int,
    *,
    spec_content: str | None = None,
    content_ref: str | None = None,
    recompile: bool = False,
    tool_context: ToolContext | None = None,
    _update_spec_and_compile_authority: Callable[..., dict[str, Any]] | None = None,
    _logger: logging.Logger | None = None,
) -> int:
    """Ensure an accepted spec authority exists for the product."""
    gate_logger = _logger or logger
    update_and_compile = _update_spec_and_compile_authority
    if update_and_compile is None:
        update_and_compile = (
            _resolve_update_spec_and_compile_authority()
            or update_spec_and_compile_authority
        )
    spec_input_provided = spec_content is not None or content_ref is not None

    session_id: str | None = None
    if tool_context and hasattr(tool_context, "session_id"):
        session_id = getattr(tool_context, "session_id", None)

    gate_logger.info(
        "authority_gate.check",
        extra={
            "product_id": product_id,
            "session_id": session_id,
            "recompile": recompile,
            "spec_input_provided": spec_input_provided,
            "has_spec_content": spec_content is not None,
            "has_content_ref": content_ref is not None,
            "tool_context_present": tool_context is not None,
        },
    )

    engine = _resolve_engine()
    ensure_schema_current(cast("Engine", engine))

    with Session(engine) as session:
        lookup = _lookup_reusable_accepted_authority(
            session,
            product_id=product_id,
        )

    if lookup.reusable_spec_version_id is not None:
        gate_logger.info(
            "authority_gate.pass",
            extra={
                "product_id": product_id,
                "session_id": session_id,
                "spec_version_id": lookup.reusable_spec_version_id,
                "path_used": "existing_authority",
                "accepted_decision_found": lookup.accepted_decision_found,
                "compiled_row_found": lookup.compiled_row_found,
                "compiled_artifact_success": lookup.compiled_artifact_success,
                "spec_input_provided": spec_input_provided,
            },
        )
        return lookup.reusable_spec_version_id

    if spec_content is None and content_ref is None:
        gate_logger.error(
            "authority_gate.fail_no_source",
            extra={
                "product_id": product_id,
                "session_id": session_id,
                "path_used": "fail_no_source",
                "accepted_decision_found": lookup.accepted_decision_found,
                "compiled_row_found": lookup.compiled_row_found,
                "compiled_artifact_success": lookup.compiled_artifact_success,
                "spec_input_provided": False,
                "reason": "missing_inputs",
            },
        )
        raise SpecAuthorityGateError.missing_source(product_id)

    path_used = _resolve_gate_path_used(
        tool_context=tool_context,
        spec_content=spec_content,
        content_ref=content_ref,
    )
    params, input_source = _build_update_compile_params(
        product_id=product_id,
        spec_content=spec_content,
        content_ref=content_ref,
        recompile=recompile,
    )

    gate_logger.info(
        "authority_gate.compile_start",
        extra={
            "product_id": product_id,
            "session_id": session_id,
            "path_used": path_used,
            "input_source": input_source,
            "recompile": recompile,
            "reason": lookup.compile_reason,
            "accepted_decision_found": lookup.accepted_decision_found,
            "compiled_row_found": lookup.compiled_row_found,
            "compiled_artifact_success": lookup.compiled_artifact_success,
            "spec_input_provided": True,
        },
    )

    try:
        result = update_and_compile(params, tool_context=tool_context)
    except Exception as exc:
        gate_logger.exception(
            "authority_gate.compile_result",
            extra={
                "product_id": product_id,
                "session_id": session_id,
                "path_used": path_used,
                "reason": "update_failed",
                "success": False,
                "accepted": False,
                "spec_version_id": None,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            },
        )
        raise

    return _validate_gate_compile_result(
        product_id=product_id,
        result=result,
        gate_logger=gate_logger,
        session_id=session_id,
        path_used=path_used,
    )


def _run_async_task[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run an async coroutine from sync code, even if a loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(asyncio.run, coro)
        return cast("T", future.result())


def _extract_compiler_response_text(events: list[Any]) -> str:
    """Extract the first text part from the final agent event."""
    final_event = events[-1] if events else None
    if not final_event or not getattr(final_event, "content", None):
        return ""
    for part in final_event.content.parts:
        text = getattr(part, "text", None)
        if text:
            return text
    return ""


async def _invoke_spec_authority_compiler_async(
    input_payload: SpecAuthorityCompilerInput,
) -> str:
    """Invoke the spec authority compiler agent and return raw JSON text."""
    return await invoke_agent_to_text(
        agent=spec_authority_compiler_agent,
        runner_identity=SPEC_AUTHORITY_COMPILER_IDENTITY,
        payload_json=input_payload.model_dump_json(),
        no_text_error="Compiler agent returned no text response",
    )


def _default_invoke_spec_authority_compiler(
    spec_content: str,
    content_ref: str | None,
    product_id: int | None,
    spec_version_id: int | None,
) -> str:
    """Invoke the compiler agent from sync code and return raw JSON text."""
    del content_ref
    normalize_spec_content_for_registry(spec_content)
    input_payload = SpecAuthorityCompilerInput(
        spec_source=spec_content,
        spec_content_ref=None,
        domain_hint=None,
        product_id=product_id,
        spec_version_id=spec_version_id,
        spec_source_format=cast(
            "Literal['agileforge.spec.v1']",
            STRUCTURED_SPEC_FORMAT,
        ),
    )
    return _run_async_task(_invoke_spec_authority_compiler_async(input_payload))


def _detect_spec_source_format(
    spec_content: str,
) -> Literal["agileforge.spec.v1", "plain_text"]:
    """Return the source format marker for compiler input."""
    try:
        normalize_spec_content_for_registry(spec_content)
    except SpecContentNormalizationError:
        return "plain_text"
    return cast("Literal['agileforge.spec.v1']", STRUCTURED_SPEC_FORMAT)


def _resolve_compiler_invoker() -> Callable[..., str]:
    """Preserve the legacy spec_tools compiler monkeypatch seam for tests."""
    tool_invoke = _resolve_tool_override(
        "_invoke_spec_authority_compiler",
        default_module_name="tools.spec_tools",
        default_function_name="_invoke_spec_authority_compiler",
    )
    if tool_invoke is None:
        return _default_invoke_spec_authority_compiler
    return cast("Callable[..., str]", tool_invoke)


def _invoke_spec_authority_compiler(
    spec_content: str,
    content_ref: str | None,
    product_id: int | None,
    spec_version_id: int | None,
) -> str:
    """Invoke the effective compiler seam, honoring legacy monkeypatch overrides."""
    compiler_invoke = _resolve_compiler_invoker()
    return compiler_invoke(
        spec_content=spec_content,
        content_ref=content_ref,
        product_id=product_id,
        spec_version_id=spec_version_id,
    )


def preview_spec_authority(
    params: dict[str, Any] | PreviewSpecAuthorityInput | None = None,
    *,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Compile spec authority in preview mode without persistence side effects."""
    try:
        parsed = PreviewSpecAuthorityInput.model_validate(
            _normalize_input_params(params)
        )
    except ValidationError as exc:
        return {"success": False, "error": f"Invalid input: {exc}"}

    try:
        raw_json = _invoke_spec_authority_compiler(
            spec_content=parsed.content,
            content_ref=None,
            product_id=None,
            spec_version_id=None,
        )
        normalized = normalize_compiler_output(
            raw_json,
            source_text=parsed.content,
            source_format=_detect_spec_source_format(parsed.content),
        )

        if isinstance(normalized.root, SpecAuthorityCompilationFailure):
            return {
                "success": False,
                "error": "Compilation failed",
                "details": normalized.root.model_dump(),
            }

        compiled_json = normalized.root.model_dump_json()
        if tool_context and tool_context.state is not None:
            tool_context.state["compiled_authority_cached"] = compiled_json
    except RuntimeError as exc:
        logger.exception("preview_spec_authority failed")
        return {"success": False, "error": str(exc)}
    else:
        return {
            "success": True,
            "compiled_authority": compiled_json,
        }


def _compiler_failure_result(
    **kwargs: Unpack[_CompilerFailureOptions],
) -> dict[str, Any]:
    details = _CompilerFailureDetails(
        product_id=kwargs.get("product_id"),
        spec_version_id=kwargs.get("spec_version_id"),
        content_ref=kwargs.get("content_ref"),
        failure_stage=kwargs.get("failure_stage", "unknown"),
        error=kwargs.get("error", "UNKNOWN"),
        reason=kwargs.get("reason", ""),
        raw_output=kwargs.get("raw_output"),
        blocking_gaps=kwargs.get("blocking_gaps"),
        exception=kwargs.get("exception"),
    )
    summary = f"{details.error}: {details.reason}" if details.reason else details.error
    artifact_result = write_failure_artifact(
        phase="spec_authority",
        project_id=details.product_id,
        failure_stage=details.failure_stage,
        failure_summary=summary,
        raw_output=details.raw_output,
        context={
            "product_id": details.product_id,
            "spec_version_id": details.spec_version_id,
            "content_ref": details.content_ref,
        },
        model_info={
            **get_agent_model_info(spec_authority_compiler_agent),
            "app_name": SPEC_AUTHORITY_COMPILER_IDENTITY.app_name,
            "user_id": SPEC_AUTHORITY_COMPILER_IDENTITY.user_id,
        },
        validation_errors=details.blocking_gaps,
        exception=details.exception,
        extra={
            "error": details.error,
            "reason": details.reason,
            "blocking_gaps": details.blocking_gaps or [],
        },
    )
    metadata: FailureMetadataDict = artifact_result["metadata"]
    if details.exception is not None:
        logger.exception(
            "Spec authority compilation failed [artifact_id=%s stage=%s]: %s",
            metadata["failure_artifact_id"],
            details.failure_stage,
            summary,
        )
    else:
        logger.error(
            "Spec authority compilation failed [artifact_id=%s stage=%s]: %s",
            metadata["failure_artifact_id"],
            details.failure_stage,
            summary,
        )
    return {
        "success": False,
        "error": details.error,
        "reason": details.reason,
        "blocking_gaps": details.blocking_gaps or [],
        **metadata,
    }


def _render_invariant_summary(invariant: Invariant) -> str:
    """Render a structured invariant into a stable string for legacy consumers."""
    if invariant.type == InvariantType.FORBIDDEN_CAPABILITY:
        capability: Any | Literal[""] = getattr(invariant.parameters, "capability", "")
        return f"FORBIDDEN_CAPABILITY:{capability}"
    if invariant.type == InvariantType.REQUIRED_FIELD:
        field_name: Any | Literal[""] = getattr(invariant.parameters, "field_name", "")
        return f"REQUIRED_FIELD:{field_name}"
    if invariant.type == InvariantType.MAX_VALUE:
        field_name: Any | Literal[""] = getattr(invariant.parameters, "field_name", "")
        max_value: Any | Literal[""] = getattr(invariant.parameters, "max_value", "")
        return f"MAX_VALUE:{field_name}<= {max_value}"
    if invariant.type == InvariantType.RELATION_CONSTRAINT:
        expression: Any | Literal[""] = getattr(
            invariant.parameters, "expression", ""
        )
        return f"RELATION_CONSTRAINT:{expression}"
    return f"INVARIANT:{invariant.type}"


def _resolve_engine() -> Engine | Connection | None:
    """Preserve the legacy spec_tools.engine monkeypatch seam for tests."""
    return cast(
        "Engine | Connection | None",
        resolve_spec_engine(
            service_get_engine=get_engine,
            default_service_get_engine=_DEFAULT_GET_ENGINE,
        ),
    )


def _compile_spec_authority_for_version(
    *,
    spec_version_id: int,
    force_recompile: bool,
    tool_context: ToolContext | None,
) -> dict[str, Any]:
    tool_compile = _resolve_compile_spec_authority_for_version()
    if tool_compile is not None:
        return tool_compile(
            {
                "spec_version_id": spec_version_id,
                "force_recompile": force_recompile,
            },
            tool_context=tool_context,
        )
    return compile_spec_authority_for_version(
        spec_version_id=spec_version_id,
        force_recompile=force_recompile,
        tool_context=tool_context,
    )


def _extract_spec_authority_llm(
    *,
    spec_content: str,
    content_ref: str | None,
    product_id: int,
    spec_version_id: int,
) -> SpecAuthorityCompilationSuccess:
    """Extract spec authority using the compiler and normalize to success output."""
    raw_json = _invoke_spec_authority_compiler(
        spec_content=spec_content,
        content_ref=content_ref,
        product_id=product_id,
        spec_version_id=spec_version_id,
    )

    normalized: SpecAuthorityCompilerOutput = normalize_compiler_output(
        raw_json,
        source_text=spec_content,
        source_format=_detect_spec_source_format(spec_content),
    )
    if isinstance(normalized.root, SpecAuthorityCompilationFailure):
        raise SpecAuthorityCompilationError.failed(
            normalized.root.error,
            normalized.root.reason,
        )
    return normalized.root


def compile_spec_authority(
    params: dict[str, Any] | CompileSpecAuthorityInput | None = None,
    *,
    tool_context: ToolContext | None = None,  # pylint: disable=unused-argument
    extract_authority: (Callable[..., SpecAuthorityCompilationSuccess] | None) = None,
) -> dict[str, Any]:
    """Compile an approved spec version into cached authority (legacy one-shot path)."""
    del tool_context
    parsed: CompileSpecAuthorityInput = CompileSpecAuthorityInput.model_validate(
        _normalize_input_params(params)
    )

    extractor = extract_authority or _extract_spec_authority_llm

    with Session(_resolve_engine()) as session:
        spec_version: SpecRegistry | None = session.get(
            SpecRegistry, parsed.spec_version_id
        )
        if not spec_version:
            return {
                "success": False,
                "error": f"Spec version {parsed.spec_version_id} not found",
            }

        if spec_version.status != "approved":
            return {
                "success": False,
                "error": (
                    f"Spec version {parsed.spec_version_id} is not approved "
                    f"(status: {spec_version.status}). "
                    "Only approved specs can be compiled."
                ),
            }

        spec_version_id: int | None = spec_version.spec_version_id
        if spec_version_id is None:
            return {
                "success": False,
                "error": "Spec version is missing its primary key",
            }

        existing_authority: CompiledSpecAuthority | None = session.exec(
            select(CompiledSpecAuthority).where(
                CompiledSpecAuthority.spec_version_id == parsed.spec_version_id
            )
        ).first()
        if existing_authority:
            return {
                "success": False,
                "error": (
                    f"Spec version {parsed.spec_version_id} is already compiled "
                    f"(authority_id: {existing_authority.authority_id})"
                ),
            }

        try:
            success_artifact: SpecAuthorityCompilationSuccess = extractor(
                spec_content=spec_version.content,
                content_ref=spec_version.content_ref,
                product_id=spec_version.product_id,
                spec_version_id=spec_version_id,
            )
        except (SpecAuthorityCompilationError, ValueError) as exc:
            return {
                "success": False,
                "error": str(exc),
            }

        prompt_hash: str = compute_prompt_hash(SPEC_AUTHORITY_COMPILER_INSTRUCTIONS)
        scope_themes: list[str] = success_artifact.scope_themes
        invariants: list[str] = [
            _render_invariant_summary(inv) for inv in success_artifact.invariants
        ]
        spec_gaps = success_artifact.gaps
        compiled_artifact_json = success_artifact.model_dump_json()

        authority = CompiledSpecAuthority(
            spec_version_id=parsed.spec_version_id,
            compiler_version=SPEC_AUTHORITY_COMPILER_VERSION,
            prompt_hash=prompt_hash,
            compiled_at=datetime.now(UTC),
            scope_themes=json.dumps(scope_themes),
            invariants=json.dumps(invariants),
            eligible_feature_ids=json.dumps([]),
            rejected_features=json.dumps([]),
            spec_gaps=json.dumps(spec_gaps),
            compiled_artifact_json=compiled_artifact_json,
        )
        session.add(authority)
        session.commit()
        session.refresh(authority)

        return {
            "success": True,
            "authority_id": authority.authority_id,
            "spec_version_id": parsed.spec_version_id,
            "compiler_version": SPEC_AUTHORITY_COMPILER_VERSION,
            "prompt_hash": prompt_hash[:8],
            "scope_themes_count": len(scope_themes),
            "invariants_count": len(invariants),
            "message": (
                f"Compiled spec version {parsed.spec_version_id} "
                f"(authority ID: {authority.authority_id})"
            ),
        }


def _normalize_compile_version_input(
    params: dict[str, Any] | CompileSpecAuthorityForVersionInput | None,
    *,
    spec_version_id: int | None,
    force_recompile: bool | None,
) -> CompileSpecAuthorityForVersionInput:
    """Merge direct kwargs into the tool payload and validate the input model."""
    merged_params: dict[str, Any] = dict(_normalize_input_params(params))
    if spec_version_id is not None:
        merged_params["spec_version_id"] = spec_version_id
    if force_recompile is not None:
        merged_params["force_recompile"] = force_recompile
    return CompileSpecAuthorityForVersionInput.model_validate(merged_params)


def _load_compile_version_context(
    session: Session,
    *,
    spec_version_id: int,
) -> _CompilerVersionContext | dict[str, Any]:
    """Load the approved spec version, product, and any cached authority row."""
    spec_version = session.get(SpecRegistry, spec_version_id)
    if not spec_version:
        return {
            "success": False,
            "error": f"Spec version {spec_version_id} not found",
        }
    if spec_version.status != "approved":
        return {
            "success": False,
            "error": (
                f"Spec version {spec_version_id} is not approved "
                f"(status: {spec_version.status}). "
                "Only approved specs can be compiled."
            ),
        }

    product = session.get(Product, spec_version.product_id)
    existing_authority = session.exec(
        select(CompiledSpecAuthority).where(
            CompiledSpecAuthority.spec_version_id == spec_version_id
        )
    ).first()
    return _CompilerVersionContext(
        spec_version=spec_version,
        product=product,
        existing_authority=existing_authority,
    )


def _update_product_compiled_authority_cache(
    session: Session,
    *,
    product: Product | None,
    compiled_artifact_json: str,
    lease_guard: Callable[[str], bool] | None = None,
    record_progress: Callable[[str], bool] | None = None,
) -> dict[str, Any] | None:
    """Backfill the product-level compiled authority cache when a product exists."""
    if product is None:
        return None
    boundary = "product_authority_cache_persisted"
    if lease_guard is not None and not lease_guard(boundary):
        session.rollback()
        return _mutation_lease_lost_result()
    product.compiled_authority_json = compiled_artifact_json
    session.add(product)
    session.commit()
    return _record_mutation_progress(record_progress, boundary)


def _mutation_lease_lost_result() -> dict[str, Any]:
    """Return the canonical mutation lease-loss envelope."""
    return {
        "success": False,
        "error": "MUTATION_LEASE_LOST",
        "error_code": "MUTATION_IN_PROGRESS",
    }


def _mutation_progress_failed_result(boundary: str) -> dict[str, Any]:
    """Return the canonical mutation progress failure envelope."""
    return {
        "success": False,
        "error": "MUTATION_PROGRESS_RECORD_FAILED",
        "error_code": "MUTATION_RECOVERY_REQUIRED",
        "boundary": boundary,
    }


def _record_mutation_progress(
    record_progress: Callable[[str], bool] | None,
    boundary: str,
) -> dict[str, Any] | None:
    """Record a completed mutation boundary and normalize failures."""
    if record_progress is None:
        return None
    try:
        if record_progress(boundary) is False:
            return _mutation_progress_failed_result(boundary)
    except Exception:  # noqa: BLE001
        return _mutation_progress_failed_result(boundary)
    return None


def _cached_compilation_result(
    session: Session,
    *,
    context: _CompilerVersionContext,
    tool_context: ToolContext | None,
    lease_guard: Callable[[str], bool] | None = None,
    record_progress: Callable[[str], bool] | None = None,
) -> dict[str, Any] | None:
    """Return the cached-authority envelope when a reusable compiled artifact exists."""
    existing_authority = context.existing_authority
    if existing_authority is None or not existing_authority.compiled_artifact_json:
        return None

    artifact = load_compiled_artifact(existing_authority)
    if artifact:
        scope_themes_count = len(artifact.scope_themes)
        invariants_count = len(artifact.invariants)
    else:
        scope_themes_count = len(json.loads(existing_authority.scope_themes))
        invariants_count = len(json.loads(existing_authority.invariants))

    cache_error = _update_product_compiled_authority_cache(
        session,
        product=context.product,
        compiled_artifact_json=existing_authority.compiled_artifact_json,
        lease_guard=lease_guard,
        record_progress=record_progress,
    )
    if cache_error is not None:
        return cache_error
    if tool_context and tool_context.state is not None:
        tool_context.state["compiled_authority_cached"] = (
            existing_authority.compiled_artifact_json
        )

    return {
        "success": True,
        "cached": True,
        "authority_id": existing_authority.authority_id,
        "spec_version_id": context.spec_version.spec_version_id,
        "compiler_version": existing_authority.compiler_version,
        "prompt_hash": existing_authority.prompt_hash,
        "scope_themes_count": scope_themes_count,
        "invariants_count": invariants_count,
        "content_ref": context.spec_version.content_ref,
        "content_source": "content",
        "message": (
            f"Spec version {context.spec_version.spec_version_id} is already compiled "
            f"(authority ID: {existing_authority.authority_id})."
        ),
    }


def _load_spec_content_for_compile(
    spec_version: SpecRegistry,
) -> tuple[str, str] | dict[str, Any]:
    """Load spec content from the DB row or its content_ref fallback."""
    spec_content = spec_version.content or ""
    if spec_content.strip():
        return spec_content, "content"

    if not spec_version.content_ref:
        return {
            "success": False,
            "error": "Spec content is empty; cannot compile authority.",
        }

    content_path = Path(spec_version.content_ref)
    if not content_path.exists():
        return {
            "success": False,
            "error": (
                "Spec content is empty and content_ref was not found: "
                f"{spec_version.content_ref}"
            ),
        }
    try:
        return content_path.read_text(encoding="utf-8"), "content_ref"
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "success": False,
            "error": f"Failed to read content_ref: {exc}",
        }


def _invoke_compiler_for_version(
    spec_version: SpecRegistry,
    *,
    spec_content: str,
) -> SpecAuthorityCompilationSuccess | dict[str, Any]:
    """Invoke the compiler and normalize either a success artifact or failure result."""
    try:
        raw_json = _invoke_spec_authority_compiler(
            spec_content=spec_content,
            content_ref=spec_version.content_ref,
            product_id=spec_version.product_id,
            spec_version_id=spec_version.spec_version_id,
        )
    except AgentInvocationError as exc:
        return _compiler_failure_result(
            product_id=spec_version.product_id,
            spec_version_id=spec_version.spec_version_id,
            content_ref=spec_version.content_ref,
            failure_stage="invocation_exception",
            error="SPEC_COMPILER_INVOCATION_FAILED",
            reason=str(exc),
            raw_output=exc.partial_output,
            exception=exc,
        )
    except (RuntimeError, TypeError, ValueError) as exc:
        return _compiler_failure_result(
            product_id=spec_version.product_id,
            spec_version_id=spec_version.spec_version_id,
            content_ref=spec_version.content_ref,
            failure_stage="invocation_exception",
            error="SPEC_COMPILER_INVOCATION_FAILED",
            reason=str(exc),
            exception=exc,
        )

    normalized = normalize_compiler_output(
        raw_json,
        source_text=spec_content,
        source_format=_detect_spec_source_format(spec_content),
    )
    if isinstance(normalized.root, SpecAuthorityCompilationFailure):
        failure_stage = (
            "invalid_json"
            if normalized.root.reason == "INVALID_JSON"
            else "output_validation"
        )
        return _compiler_failure_result(
            product_id=spec_version.product_id,
            spec_version_id=spec_version.spec_version_id,
            content_ref=spec_version.content_ref,
            failure_stage=failure_stage,
            error=normalized.root.error,
            reason=normalized.root.reason,
            raw_output=raw_json,
            blocking_gaps=normalized.root.blocking_gaps,
        )
    return normalized.root


def _persist_compiled_authority(  # noqa: PLR0913
    session: Session,
    *,
    context: _CompilerVersionContext,
    spec_version_id: int,
    force_recompile: bool,
    success: SpecAuthorityCompilationSuccess,
    lease_guard: Callable[[str], bool] | None = None,
    record_progress: Callable[[str], bool] | None = None,
) -> _PersistedCompilation | dict[str, Any]:
    """Persist a compiled artifact, either by updating or inserting a row."""
    compiled_artifact_json = success.model_dump_json()
    prompt_hash = compute_prompt_hash(SPEC_AUTHORITY_COMPILER_INSTRUCTIONS)
    compiler_version = SPEC_AUTHORITY_COMPILER_VERSION
    scope_themes = success.scope_themes
    invariants = [_render_invariant_summary(inv) for inv in success.invariants]
    spec_gaps = success.gaps

    boundary = "compiled_authority_persisted"
    if context.existing_authority and force_recompile:
        if lease_guard is not None and not lease_guard(boundary):
            session.rollback()
            return _mutation_lease_lost_result()
        authority = context.existing_authority
        authority.compiler_version = compiler_version
        authority.prompt_hash = prompt_hash
        authority.compiled_at = datetime.now(UTC)
        authority.compiled_artifact_json = compiled_artifact_json
        authority.scope_themes = json.dumps(scope_themes)
        authority.invariants = json.dumps(invariants)
        authority.eligible_feature_ids = json.dumps([])
        authority.rejected_features = json.dumps([])
        authority.spec_gaps = json.dumps(spec_gaps)
        session.add(authority)
        session.commit()
        session.refresh(authority)
        recompiled = True
    else:
        if lease_guard is not None and not lease_guard(boundary):
            session.rollback()
            return _mutation_lease_lost_result()
        authority = CompiledSpecAuthority(
            spec_version_id=spec_version_id,
            compiler_version=compiler_version,
            prompt_hash=prompt_hash,
            compiled_at=datetime.now(UTC),
            compiled_artifact_json=compiled_artifact_json,
            scope_themes=json.dumps(scope_themes),
            invariants=json.dumps(invariants),
            eligible_feature_ids=json.dumps([]),
            rejected_features=json.dumps([]),
            spec_gaps=json.dumps(spec_gaps),
        )
        session.add(authority)
        session.commit()
        session.refresh(authority)
        recompiled = False

    progress_error = _record_mutation_progress(record_progress, boundary)
    if progress_error is not None:
        return progress_error

    cache_error = _update_product_compiled_authority_cache(
        session,
        product=context.product,
        compiled_artifact_json=compiled_artifact_json,
        lease_guard=lease_guard,
        record_progress=record_progress,
    )
    if cache_error is not None:
        return cache_error
    authority_id = authority.authority_id
    if authority_id is None:
        error_message = "Compiled authority did not receive a primary key"
        raise RuntimeError(error_message)
    return _PersistedCompilation(
        authority_id=authority_id,
        compiled_artifact_json=compiled_artifact_json,
        compiler_version=compiler_version,
        prompt_hash=prompt_hash,
        scope_themes_count=len(scope_themes),
        invariants_count=len(invariants),
        recompiled=recompiled,
    )


def _compile_spec_authority_for_version_in_session(  # noqa: PLR0913
    session: Session,
    *,
    parsed: CompileSpecAuthorityForVersionInput,
    should_recompile: bool,
    tool_context: ToolContext | None,
    lease_guard: Callable[[str], bool] | None = None,
    record_progress: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Compile one approved spec version using the caller's DB session."""
    context = _load_compile_version_context(
        session,
        spec_version_id=parsed.spec_version_id,
    )
    if not isinstance(context, _CompilerVersionContext):
        return context

    if not should_recompile:
        cached_result = _cached_compilation_result(
            session,
            context=context,
            tool_context=tool_context,
            lease_guard=lease_guard,
            record_progress=record_progress,
        )
        if cached_result is not None:
            return cached_result

    spec_content_result = _load_spec_content_for_compile(context.spec_version)
    if isinstance(spec_content_result, dict):
        return spec_content_result
    spec_content, content_source = spec_content_result

    compiled = _invoke_compiler_for_version(
        context.spec_version,
        spec_content=spec_content,
    )
    if isinstance(compiled, dict):
        return compiled

    persisted_result = _persist_compiled_authority(
        session,
        context=context,
        spec_version_id=parsed.spec_version_id,
        force_recompile=should_recompile,
        success=compiled,
        lease_guard=lease_guard,
        record_progress=record_progress,
    )
    if isinstance(persisted_result, dict):
        return cast("dict[str, Any]", persisted_result)
    persisted = cast("_PersistedCompilation", persisted_result)
    if tool_context and tool_context.state is not None:
        tool_context.state["compiled_authority_cached"] = (
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
        "content_ref": context.spec_version.content_ref,
        "content_source": content_source,
        "message": (
            f"Compiled spec version {parsed.spec_version_id} "
            f"(authority ID: {persisted.authority_id})"
        ),
    }


def compile_spec_authority_for_version(
    params: dict[str, Any] | CompileSpecAuthorityForVersionInput | None = None,
    *,
    spec_version_id: int | None = None,
    force_recompile: bool | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Compile an approved spec version into cached authority (idempotent)."""
    parsed = _normalize_compile_version_input(
        params,
        spec_version_id=spec_version_id,
        force_recompile=force_recompile,
    )
    should_recompile = bool(parsed.force_recompile)

    with Session(_resolve_engine()) as session:
        return _compile_spec_authority_for_version_in_session(
            session,
            parsed=parsed,
            should_recompile=should_recompile,
            tool_context=tool_context,
        )


def compile_spec_authority_for_version_with_engine(  # noqa: PLR0913
    *,
    engine: Engine,
    spec_version_id: int,
    force_recompile: bool | None = None,
    tool_context: ToolContext | None = None,
    lease_guard: Callable[[str], bool] | None = None,
    record_progress: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Compile an approved spec version using the supplied business DB engine."""
    ensure_schema_current(engine)
    parsed = _normalize_compile_version_input(
        None,
        spec_version_id=spec_version_id,
        force_recompile=force_recompile,
    )
    with Session(engine) as session:
        return _compile_spec_authority_for_version_in_session(
            session,
            parsed=parsed,
            should_recompile=bool(parsed.force_recompile),
            tool_context=tool_context,
            lease_guard=lease_guard,
            record_progress=record_progress,
        )


def _ensure_spec_authority_accepted(
    *,
    product_id: int,
    spec_version_id: int,
    policy: str,
    decided_by: str,
    rationale: str | None = None,
) -> SpecAuthorityAcceptance:
    tool_ensure = _resolve_ensure_spec_authority_accepted()
    if tool_ensure is not None:
        return tool_ensure(
            product_id=product_id,
            spec_version_id=spec_version_id,
            policy=policy,
            decided_by=decided_by,
            rationale=rationale,
        )
    return ensure_spec_authority_accepted(
        product_id=product_id,
        spec_version_id=spec_version_id,
        policy=policy,
        decided_by=decided_by,
        rationale=rationale,
    )


def _resolve_compile_spec_authority_for_version() -> (
    Callable[..., dict[str, Any]] | None
):
    """Preserve legacy tool-level monkeypatch seam for compile delegation."""
    tool_compile = _resolve_tool_override(
        "compile_spec_authority_for_version",
        default_module_name="tools.spec_tools",
        default_function_name="compile_spec_authority_for_version",
    )
    return cast("Callable[..., dict[str, Any]] | None", tool_compile)


def _resolve_ensure_spec_authority_accepted() -> (
    Callable[..., SpecAuthorityAcceptance] | None
):
    """Preserve legacy tool-level monkeypatch seam for acceptance delegation."""
    tool_ensure = _resolve_tool_override(
        "ensure_spec_authority_accepted",
        default_module_name="tools.spec_tools",
        default_function_name="ensure_spec_authority_accepted",
    )
    return cast("Callable[..., SpecAuthorityAcceptance] | None", tool_ensure)


def _resolve_update_spec_and_compile_authority() -> (
    Callable[..., dict[str, Any]] | None
):
    """Preserve legacy tool-level monkeypatch seam for update+compile delegation."""
    tool_update = _resolve_tool_override(
        "update_spec_and_compile_authority",
        default_module_name="tools.spec_tools",
        default_function_name="update_spec_and_compile_authority",
    )
    return cast("Callable[..., dict[str, Any]] | None", tool_update)


def _load_update_spec_content(
    parsed: UpdateSpecAndCompileAuthorityInput,
) -> str | dict[str, Any]:
    """Load raw specification text from exactly one configured input source."""
    has_content = parsed.spec_content is not None
    has_ref = parsed.content_ref is not None
    if has_content == has_ref:
        raise UpdateSpecAuthorityInputError.exactly_one_source()

    if parsed.content_ref is None:
        return parsed.spec_content or ""

    content_path = Path(parsed.content_ref)
    if not content_path.exists():
        return {
            "success": False,
            "error": f"Specification file not found: {parsed.content_ref}",
        }
    try:
        return content_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "success": False,
            "error": f"Failed to read specification file: {exc}",
        }


def _resolve_or_create_spec_version(
    session: Session,
    *,
    parsed: UpdateSpecAndCompileAuthorityInput,
    spec_content: str,
    spec_hash: str,
) -> int | dict[str, Any]:
    """Return the latest matching approved spec version, creating one if needed."""
    product = session.get(Product, parsed.product_id)
    if not product:
        return {
            "success": False,
            "error": f"Product ID {parsed.product_id} not found",
        }

    latest_spec = session.exec(
        select(SpecRegistry)
        .where(SpecRegistry.product_id == parsed.product_id)
        .order_by(cast("Any", SpecRegistry.spec_version_id).desc())
    ).first()
    if latest_spec and latest_spec.spec_hash == spec_hash:
        spec_version_id = latest_spec.spec_version_id
        if spec_version_id is None:
            return {
                "success": False,
                "error": "Latest spec is missing its primary key",
            }
        return spec_version_id

    new_spec = SpecRegistry(
        product_id=parsed.product_id,
        spec_hash=spec_hash,
        content=spec_content,
        content_ref=parsed.content_ref,
        status="approved",
        approved_at=datetime.now(UTC),
        approved_by="implicit",
        approval_notes="Implicit approval",
    )
    session.add(new_spec)
    session.commit()
    session.refresh(new_spec)
    spec_version_id = new_spec.spec_version_id
    if spec_version_id is None:
        return {
            "success": False,
            "error": "New spec did not receive a primary key",
        }
    return spec_version_id


def _load_compiled_authority_or_error(
    session: Session,
    *,
    spec_version_id: int,
) -> CompiledSpecAuthority | dict[str, Any]:
    """Load the compiled authority row created for the given spec version."""
    authority = session.exec(
        select(CompiledSpecAuthority).where(
            CompiledSpecAuthority.spec_version_id == spec_version_id
        )
    ).first()
    if authority is None:
        return {
            "success": False,
            "error": f"Compiled authority missing for spec version {spec_version_id}",
        }
    return authority


def _compiled_authority_metrics(
    authority: CompiledSpecAuthority,
) -> tuple[int, int, int]:
    """Return counts used by the update+compile success payload."""
    artifact = load_compiled_artifact(authority)
    if artifact:
        return (
            len(artifact.scope_themes),
            len(artifact.invariants),
            len(json.loads(authority.eligible_feature_ids)),
        )
    return (
        len(json.loads(authority.scope_themes or "[]")),
        len(json.loads(authority.invariants or "[]")),
        len(json.loads(authority.eligible_feature_ids or "[]")),
    )


def update_spec_and_compile_authority(  # noqa: PLR0911
    params: dict[str, Any] | UpdateSpecAndCompileAuthorityInput,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Persist spec content, compile authority, and auto-accept the result."""
    parsed = UpdateSpecAndCompileAuthorityInput.model_validate(
        _normalize_input_params(params)
    )
    force_recompile = bool(parsed.recompile)

    spec_content_result = _load_update_spec_content(parsed)
    if isinstance(spec_content_result, dict):
        return spec_content_result
    spec_content = spec_content_result

    try:
        normalized_spec = normalize_spec_content_for_registry(spec_content)
    except SpecContentNormalizationError as exc:
        return {
            "success": False,
            "error_code": exc.error_code,
            "error": str(exc),
        }
    spec_content = normalized_spec.content
    spec_hash = normalized_spec.spec_hash

    with Session(_resolve_engine()) as session:
        spec_version_result = _resolve_or_create_spec_version(
            session,
            parsed=parsed,
            spec_content=spec_content,
            spec_hash=spec_hash,
        )
        if isinstance(spec_version_result, dict):
            return spec_version_result
        spec_version_id = spec_version_result

    compile_result = _compile_spec_authority_for_version(
        spec_version_id=spec_version_id,
        force_recompile=force_recompile,
        tool_context=tool_context,
    )

    if not compile_result.get("success"):
        return compile_result

    with Session(_resolve_engine()) as session:
        authority_result = _load_compiled_authority_or_error(
            session,
            spec_version_id=spec_version_id,
        )
        if isinstance(authority_result, dict):
            return authority_result
        authority = authority_result

    try:
        acceptance = _ensure_spec_authority_accepted(
            product_id=parsed.product_id,
            spec_version_id=spec_version_id,
            policy="auto",
            decided_by="system",
            rationale="Auto-accepted on compile success",
        )
    except ValueError as exc:
        return {
            "success": False,
            "error": str(exc),
            "accepted": False,
        }

    scope_themes_count, invariants_count, eligible_feature_ids_count = (
        _compiled_authority_metrics(authority)
    )

    return {
        "success": True,
        "product_id": parsed.product_id,
        "spec_version_id": spec_version_id,
        "authority_id": authority.authority_id,
        "spec_hash": spec_hash,
        "compiled_at": authority.compiled_at.isoformat(),
        "compiler_version": authority.compiler_version,
        "num_scope_themes": scope_themes_count,
        "num_invariants": invariants_count,
        "num_eligible_feature_ids": eligible_feature_ids_count,
        "cache_hit": bool(compile_result.get("cached")) and not force_recompile,
        "accepted": acceptance.status == "accepted",
        "acceptance_policy": acceptance.policy,
        "acceptance_decided_at": acceptance.decided_at.isoformat(),
        "acceptance_decided_by": acceptance.decided_by,
        "message": (
            f"Spec v{spec_version_id} ready. Use this spec_version_id for "
            "story validation and generation."
        ),
    }


def check_spec_authority_status(
    params: dict[str, Any] | CheckSpecAuthorityStatusInput | None = None,
    *,
    tool_context: ToolContext | None = None,  # pylint: disable=unused-argument
) -> dict[str, Any]:
    """Check whether compiled authority is current for a product."""
    del tool_context
    parsed = CheckSpecAuthorityStatusInput.model_validate(
        _normalize_input_params(params)
    )

    with Session(_resolve_engine()) as session:
        spec_versions = session.exec(
            select(SpecRegistry)
            .where(SpecRegistry.product_id == parsed.product_id)
            .order_by(cast("Any", SpecRegistry.spec_version_id).desc())
        ).all()

        if not spec_versions:
            return {
                "success": True,
                "status": SpecAuthorityStatus.NOT_COMPILED.value,
                "status_details": "No spec versions exist for this product",
                "message": "Status: NOT_COMPILED (no specs)",
            }

        latest_spec = spec_versions[0]
        if latest_spec.status != "approved":
            return {
                "success": True,
                "status": SpecAuthorityStatus.PENDING_REVIEW.value,
                "status_details": (
                    f"Latest spec version {latest_spec.spec_version_id} "
                    f"is {latest_spec.status}"
                ),
                "latest_spec_version_id": latest_spec.spec_version_id,
                "message": "Status: PENDING_REVIEW (latest spec not approved)",
            }

        latest_approved = latest_spec
        latest_approved_spec_version_id = latest_approved.spec_version_id
        if latest_approved_spec_version_id is None:
            return {
                "success": False,
                "error": "Latest approved spec is missing its primary key",
            }
        latest_authority = session.exec(
            select(CompiledSpecAuthority)
            .join(SpecRegistry)
            .where(SpecRegistry.product_id == parsed.product_id)
            .order_by(cast("Any", CompiledSpecAuthority.spec_version_id).desc())
        ).first()

        if not latest_authority:
            return {
                "success": True,
                "status": SpecAuthorityStatus.NOT_COMPILED.value,
                "status_details": (
                    f"Latest approved spec version {latest_approved_spec_version_id} "
                    "is not compiled"
                ),
                "latest_approved_spec_version_id": latest_approved_spec_version_id,
                "message": "Status: NOT_COMPILED (approved but not compiled)",
            }

        if latest_authority.spec_version_id < latest_approved_spec_version_id:
            return {
                "success": True,
                "status": SpecAuthorityStatus.STALE.value,
                "status_details": (
                    "Compiled authority is stale (newer approved spec exists)"
                ),
                "compiled_spec_version_id": latest_authority.spec_version_id,
                "latest_approved_spec_version_id": latest_approved_spec_version_id,
                "message": "Status: STALE (compiled for older spec)",
            }

        return {
            "success": True,
            "status": SpecAuthorityStatus.CURRENT.value,
            "status_details": (
                "Compiled authority exists for latest approved spec "
                f"version {latest_approved_spec_version_id}"
            ),
            "latest_approved_spec_version_id": latest_approved_spec_version_id,
            "authority_id": latest_authority.authority_id,
            "compiled_at": latest_authority.compiled_at.isoformat(),
            "message": (
                f"Status: CURRENT (authority ID: {latest_authority.authority_id})"
            ),
        }


def get_compiled_authority_by_version(
    params: dict[str, Any] | GetCompiledAuthorityInput | None = None,
    *,
    tool_context: ToolContext | None = None,  # pylint: disable=unused-argument
) -> dict[str, Any]:
    """Retrieve compiled authority for a specific spec version."""
    del tool_context
    parsed = GetCompiledAuthorityInput.model_validate(_normalize_input_params(params))

    with Session(_resolve_engine()) as session:
        spec_version = session.get(SpecRegistry, parsed.spec_version_id)
        if not spec_version:
            return {
                "success": False,
                "error": f"Spec version {parsed.spec_version_id} not found",
            }

        if spec_version.product_id != parsed.product_id:
            return {
                "success": False,
                "error": (
                    f"Spec version {parsed.spec_version_id} does not belong to "
                    f"product {parsed.product_id} (mismatch)"
                ),
            }

        authority = session.exec(
            select(CompiledSpecAuthority).where(
                CompiledSpecAuthority.spec_version_id == parsed.spec_version_id
            )
        ).first()

        if not authority:
            return {
                "success": False,
                "error": (
                    f"Spec version {parsed.spec_version_id} is not compiled. "
                    "Use compile_spec_authority to compile it."
                ),
            }

        artifact = load_compiled_artifact(authority)
        if artifact:
            scope_themes = artifact.scope_themes
            invariants = [_render_invariant_summary(inv) for inv in artifact.invariants]
            spec_gaps = artifact.gaps
        else:
            scope_themes = json.loads(authority.scope_themes)
            invariants = json.loads(authority.invariants)
            spec_gaps = json.loads(authority.spec_gaps) if authority.spec_gaps else []

        eligible_feature_ids = json.loads(authority.eligible_feature_ids)
        rejected_features = (
            json.loads(authority.rejected_features)
            if authority.rejected_features
            else []
        )

        return {
            "success": True,
            "spec_version_id": parsed.spec_version_id,
            "authority_id": authority.authority_id,
            "compiler_version": authority.compiler_version,
            "compiled_at": authority.compiled_at.isoformat(),
            "scope_themes": scope_themes,
            "invariants": invariants,
            "eligible_feature_ids": eligible_feature_ids,
            "rejected_features": rejected_features,
            "spec_gaps": spec_gaps,
            "compiled_artifact_json": authority.compiled_artifact_json,
            "message": (
                f"Retrieved compiled authority for spec version "
                f"{parsed.spec_version_id}"
            ),
        }


__all__ = [
    "CheckSpecAuthorityStatusInput",
    "CompileSpecAuthorityForVersionInput",
    "GetCompiledAuthorityInput",
    "PreviewSpecAuthorityInput",
    "UpdateSpecAndCompileAuthorityInput",
    "check_spec_authority_status",
    "compile_spec_authority_for_version",
    "ensure_accepted_spec_authority",
    "get_compiled_authority_by_version",
    "load_compiled_artifact",
    "preview_spec_authority",
    "update_spec_and_compile_authority",
]
