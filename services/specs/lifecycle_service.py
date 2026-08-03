"""Public lifecycle entrypoints for spec persistence workflows."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, Field
from sqlmodel import Session

from models.core import Project
from models.db import get_engine
from models.specs import SpecRegistry
from services.specs._engine_resolution import resolve_spec_engine
from services.specs.compiler_service import (
    UpdateSpecAndCompileAuthorityInput,
    _resolve_update_spec_and_compile_authority,
    update_spec_and_compile_authority,
)
from workflow.fingerprints import canonical_stored_json_hash

if TYPE_CHECKING:
    from google.adk.tools import ToolContext
    from sqlalchemy.engine import Connection, Engine

logger: logging.Logger = logging.getLogger(name=__name__)
_MAX_SPEC_SIZE_KB = 100

_DEFAULT_GET_ENGINE = get_engine


class LinkSpecToProjectInput(BaseModel):
    """Input schema for linking an on-disk specification to a project."""

    project_id: int = Field(
        description="ID of the project to link the specification to"
    )
    spec_path: str = Field(description="Path to on-disk specification file (.md, .txt)")


class ReadProjectSpecificationInput(BaseModel):
    """Input schema for reading the active project's specification."""


class SaveProjectSpecificationInput(BaseModel):
    """Input schema for saving project specifications."""

    project_id: int = Field(description="ID of project to attach specification to")
    spec_source: str = Field(
        description=(
            'Source type: "file" (load from file path) or "text" (pasted content)'
        )
    )
    content: str = Field(
        description=(
            "File path (if spec_source='file') or raw text content "
            "(if spec_source='text')"
        )
    )


class RegisterSpecVersionInput(BaseModel):
    """Input schema for register_spec_version lifecycle entrypoint."""

    project_id: int = Field(description="Project ID to attach spec version to")
    content: str = Field(description="Full specification content (markdown or text)")
    content_ref: str | None = Field(
        default=None,
        description="Optional reference (file path, URL, or identifier)",
    )


class ApproveSpecVersionInput(BaseModel):
    """Input schema for approve_spec_version lifecycle entrypoint."""

    spec_version_id: int = Field(description="Spec version ID to approve")
    approved_by: str = Field(description="Approver identifier (username, email)")
    approval_notes: str | None = Field(
        default=None,
        description="Review notes or justification",
    )


@dataclass(frozen=True)
class ApprovedCanonicalSpec:
    """Inputs for an approved canonical spec inserted by a domain transaction."""

    project_id: int
    canonical_content_json: str
    content_ref: str | None
    approved_at: datetime
    approved_by: str
    approval_notes: str


def _normalize_input_params(params: object) -> dict[str, Any]:
    """Normalize lifecycle params from either a Pydantic model or a raw dict."""
    if isinstance(params, BaseModel):
        dumped = params.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    if isinstance(params, dict):
        return cast("dict[str, Any]", params)
    return {}


def _resolve_engine() -> Engine | Connection | None:
    """Preserve the legacy spec_tools.engine monkeypatch seam for tests."""
    return cast(
        "Engine | Connection | None",
        resolve_spec_engine(
            service_get_engine=get_engine,
            default_service_get_engine=_DEFAULT_GET_ENGINE,
        ),
    )


def _compile_spec_authority_from_path(
    *,
    project_id: int,
    spec_path: str,
    tool_context: ToolContext | None,
) -> dict[str, Any]:
    compile_spec = (
        _resolve_update_spec_and_compile_authority()
        or update_spec_and_compile_authority
    )
    compile_input = UpdateSpecAndCompileAuthorityInput(
        project_id=project_id,
        content_ref=spec_path,
    )
    return compile_spec(
        compile_input,
        tool_context=tool_context,
    )


def extract_markdown_sections(spec_text: str) -> list[str]:
    """Extract up to 20 markdown headings for navigation."""
    headings = re.findall(r"^#{1,3}\s+(.+)$", spec_text, re.MULTILINE)
    return headings[:20]


def resolve_spec_content(
    *,
    technical_spec: str | None,
    spec_file_path: str | None,
) -> tuple[str | None, str | None]:
    """Resolve spec content using the same precedence as session hydration."""
    if technical_spec:
        return technical_spec, spec_file_path

    if spec_file_path:
        spec_path_obj = Path(spec_file_path)
        if spec_path_obj.exists():
            try:
                return spec_path_obj.read_text(encoding="utf-8"), spec_file_path
            except (OSError, UnicodeDecodeError):
                return None, spec_file_path
        return None, spec_file_path

    return None, None


def hydrate_spec_state(
    state: dict[str, Any],
    *,
    technical_spec: str | None,
    spec_file_path: str | None,
) -> None:
    """Populate or clear pending spec fields from resolved spec content."""
    spec_content, resolved_path = resolve_spec_content(
        technical_spec=technical_spec,
        spec_file_path=spec_file_path,
    )

    if resolved_path is not None:
        state["pending_spec_path"] = resolved_path
    elif "pending_spec_path" in state:
        del state["pending_spec_path"]

    if spec_content is not None:
        state["pending_spec_content"] = spec_content
    elif "pending_spec_content" in state:
        del state["pending_spec_content"]


def _load_spec_text_from_file(path_str: str) -> tuple[str, float] | dict[str, Any]:
    path = Path(path_str)
    if not path.exists():
        return {
            "success": False,
            "error": f"Specification file not found: {path_str}",
        }

    file_size_kb = path.stat().st_size / 1024
    if file_size_kb > _MAX_SPEC_SIZE_KB:
        return {
            "success": False,
            "error": (
                f"File too large ({file_size_kb:.1f}KB). Maximum: {_MAX_SPEC_SIZE_KB}KB"
            ),
        }

    try:
        spec_text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {
            "success": False,
            "error": f"File encoding error. Ensure {path_str} is UTF-8",
        }

    return spec_text, file_size_kb


def _write_backup_spec_file(
    *, project_name: str, project_id: int, spec_text: str
) -> tuple[str, float] | dict[str, Any]:
    file_size_kb = len(spec_text) / 1024
    if file_size_kb > _MAX_SPEC_SIZE_KB:
        return {
            "success": False,
            "error": (
                f"Specification too large ({file_size_kb:.1f}KB). "
                f"Maximum: {_MAX_SPEC_SIZE_KB}KB"
            ),
        }

    specs_dir = Path("specs")
    specs_dir.mkdir(exist_ok=True)

    safe_name = re.sub(r"[^\w\s-]", "", project_name.lower())
    safe_name = re.sub(r"[-\s]+", "_", safe_name)
    spec_filename = f"{safe_name}_{project_id}_spec.md"
    spec_path_obj = specs_dir / spec_filename

    try:
        spec_path_obj.write_text(spec_text, encoding="utf-8")
    except OSError as exc:
        return {
            "success": False,
            "error": f"Failed to create backup file: {exc!s}",
        }

    return str(spec_path_obj), file_size_kb


def _resolve_spec_storage(
    *,
    project: Project,
    parsed: SaveProjectSpecificationInput,
) -> tuple[str, str, float, bool] | dict[str, Any]:
    """Resolve the persisted spec text/path pair from file or pasted content."""
    if parsed.spec_source == "file":
        loaded = _load_spec_text_from_file(parsed.content)
        if isinstance(loaded, dict):
            return loaded
        spec_text, file_size_kb = loaded
        return spec_text, parsed.content, file_size_kb, False

    saved = _write_backup_spec_file(
        project_name=project.name,
        project_id=parsed.project_id,
        spec_text=parsed.content,
    )
    if isinstance(saved, dict):
        return saved
    spec_path, file_size_kb = saved
    return parsed.content, spec_path, file_size_kb, True


def register_approved_spec_from_canonical_json(
    session: Session,
    approved: ApprovedCanonicalSpec,
) -> SpecRegistry:
    """Insert an approved spec from canonical JSON in a caller-owned transaction."""
    spec = SpecRegistry(
        project_id=approved.project_id,
        spec_hash=canonical_stored_json_hash(approved.canonical_content_json),
        content=approved.canonical_content_json,
        content_ref=approved.content_ref,
        status="approved",
        created_at=approved.approved_at,
        approved_at=approved.approved_at,
        approved_by=approved.approved_by,
        approval_notes=approved.approval_notes,
    )
    session.add(spec)
    session.flush()
    return spec


def register_spec_version(
    params: dict[str, Any] | RegisterSpecVersionInput,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Register a new spec version with DRAFT status and SHA-256 hash."""
    del tool_context
    parsed = RegisterSpecVersionInput.model_validate(_normalize_input_params(params))

    spec_hash = hashlib.sha256(parsed.content.encode("utf-8")).hexdigest()

    with Session(_resolve_engine()) as session:
        project = session.get(Project, parsed.project_id)
        if not project:
            return {
                "success": False,
                "error": f"Project ID {parsed.project_id} not found",
            }

        spec_version = SpecRegistry(
            project_id=parsed.project_id,
            spec_hash=spec_hash,
            content=parsed.content,
            content_ref=parsed.content_ref,
            status="draft",
            created_at=datetime.now(UTC),
        )
        session.add(spec_version)
        session.commit()
        session.refresh(spec_version)

        return {
            "success": True,
            "spec_version_id": spec_version.spec_version_id,
            "spec_hash": spec_hash,
            "status": spec_version.status,
            "message": (
                f"Registered spec version {spec_version.spec_version_id} "
                f"(status: {spec_version.status})"
            ),
        }


def approve_spec_version(
    params: dict[str, Any] | ApproveSpecVersionInput,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Approve a spec version so it becomes eligible for compilation."""
    del tool_context
    parsed = ApproveSpecVersionInput.model_validate(_normalize_input_params(params))

    with Session(_resolve_engine()) as session:
        spec_version = session.get(SpecRegistry, parsed.spec_version_id)
        if not spec_version:
            return {
                "success": False,
                "error": f"Spec version {parsed.spec_version_id} not found",
            }

        spec_version.status = "approved"
        spec_version.approved_at = datetime.now(UTC)
        spec_version.approved_by = parsed.approved_by
        spec_version.approval_notes = parsed.approval_notes

        session.add(spec_version)
        session.commit()

        return {
            "success": True,
            "spec_version_id": parsed.spec_version_id,
            "approved_by": parsed.approved_by,
            "approved_at": spec_version.approved_at.isoformat(),
            "message": (
                f"Spec version {parsed.spec_version_id} approved "
                f"by {parsed.approved_by}"
            ),
        }


def link_spec_to_project(
    params: dict[str, Any],
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Link an on-disk specification file to a project and compile authority."""
    try:
        parsed = LinkSpecToProjectInput.model_validate(_normalize_input_params(params))
    except ValueError as exc:
        return {"success": False, "error": f"Invalid parameters: {exc}"}

    path = Path(parsed.spec_path)

    if not path.exists():
        return {
            "success": False,
            "error": f"Specification file not found: {parsed.spec_path}",
        }

    file_size_kb = path.stat().st_size / 1024
    if file_size_kb > _MAX_SPEC_SIZE_KB:
        return {
            "success": False,
            "error": (
                f"File too large ({file_size_kb:.1f}KB). Maximum: {_MAX_SPEC_SIZE_KB}KB"
            ),
        }

    with Session(_resolve_engine()) as session:
        project = session.get(Project, parsed.project_id)
        if not project:
            return {
                "success": False,
                "error": f"Project {parsed.project_id} not found",
            }

        is_update = project.spec_file_path is not None
        project.spec_file_path = parsed.spec_path
        project.spec_loaded_at = datetime.now(UTC)
        # NOTE: project.technical_spec is intentionally NOT written.
        # The file on disk + SpecRegistry are the sources of truth.

        session.add(project)
        session.commit()

        project_name = project.name

    action = "updated" if is_update else "linked"
    logger.info(
        "Spec %s for %r -> %s (%.1fKB)",
        action,
        project_name,
        parsed.spec_path,
        file_size_kb,
    )

    if tool_context and tool_context.state is not None:
        tool_context.state["spec_persisted"] = True

    compile_result = _compile_spec_authority_from_path(
        project_id=parsed.project_id,
        spec_path=parsed.spec_path,
        tool_context=tool_context,
    )

    if not compile_result.get("success"):
        return {
            "success": True,
            "project_id": parsed.project_id,
            "spec_path": parsed.spec_path,
            "file_created": False,
            "spec_size_kb": round(file_size_kb, 2),
            "compile_success": False,
            "compile_error": compile_result.get("error"),
            "message": (
                f"Specification {action} ({file_size_kb:.1f}KB) "
                "but authority compilation failed."
            ),
            "failure_artifact_id": compile_result.get("failure_artifact_id"),
            "failure_stage": compile_result.get("failure_stage"),
            "failure_summary": compile_result.get("failure_summary"),
            "raw_output_preview": compile_result.get("raw_output_preview"),
            "has_full_artifact": compile_result.get("has_full_artifact", False),
        }

    return {
        "success": True,
        "project_id": parsed.project_id,
        "spec_path": parsed.spec_path,
        "file_created": False,
        "spec_size_kb": round(file_size_kb, 2),
        "compile_success": True,
        "spec_version_id": compile_result.get("spec_version_id"),
        "authority_id": compile_result.get("authority_id"),
        "message": (
            f"Specification {action} ({file_size_kb:.1f}KB) and authority compiled."
        ),
    }


def save_project_specification(
    params: dict[str, Any],
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Save or update a project's specification and compile authority."""
    try:
        parsed = SaveProjectSpecificationInput.model_validate(
            _normalize_input_params(params)
        )
    except ValueError as exc:
        return {
            "success": False,
            "error": f"Invalid parameters: {exc!s}",
        }

    if parsed.spec_source not in ["file", "text"]:
        return {
            "success": False,
            "error": (
                f"Invalid spec_source: '{parsed.spec_source}'. Must be 'file' or 'text'"
            ),
        }

    with Session(_resolve_engine()) as session:
        project = session.get(Project, parsed.project_id)
        if not project:
            return {
                "success": False,
                "error": f"Project {parsed.project_id} not found",
            }

        storage = _resolve_spec_storage(project=project, parsed=parsed)
        if isinstance(storage, dict):
            return storage
        spec_text, spec_path, file_size_kb, file_created = storage

        is_update = project.technical_spec is not None
        project.technical_spec = spec_text
        project.spec_file_path = spec_path
        project.spec_loaded_at = datetime.now(UTC)

        session.add(project)
        session.commit()

        action = "updated" if is_update else "saved"

    if tool_context and tool_context.state is not None:
        tool_context.state["spec_persisted"] = True

    compile_result = _compile_spec_authority_from_path(
        project_id=parsed.project_id,
        spec_path=spec_path,
        tool_context=tool_context,
    )

    if not compile_result.get("success"):
        return {
            "success": True,
            "project_id": parsed.project_id,
            "spec_saved": True,
            "spec_path": spec_path,
            "spec_size_kb": round(file_size_kb, 2),
            "file_created": file_created,
            "compile_success": False,
            "compile_error": compile_result.get("error"),
            "message": (
                f"Specification {action} successfully ({file_size_kb:.1f}KB), "
                "but authority compilation failed."
            ),
        }

    return {
        "success": True,
        "project_id": parsed.project_id,
        "spec_saved": True,
        "spec_path": spec_path,
        "spec_size_kb": round(file_size_kb, 2),
        "file_created": file_created,
        "compile_success": True,
        "spec_version_id": compile_result.get("spec_version_id"),
        "authority_id": compile_result.get("authority_id"),
        "message": (
            f"Specification {action} successfully ({file_size_kb:.1f}KB) "
            "and authority compiled."
        ),
    }


def read_project_specification(
    params: dict[str, Any] | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Read the active project's specification through the lifecycle boundary."""
    ReadProjectSpecificationInput.model_validate(_normalize_input_params(params))

    if not tool_context or not tool_context.state:
        return {
            "success": False,
            "error": "No context provided. Tool requires active project.",
            "spec_content": None,
        }

    state = cast("dict[str, Any]", tool_context.state)
    active_project = state.get("active_project")
    if not active_project:
        return {
            "success": False,
            "error": "No active project selected. Use select_project first.",
            "spec_content": None,
        }

    project_id = active_project.get("project_id")
    with Session(_resolve_engine()) as session:
        project = session.get(Project, project_id)
        if not project:
            project_name = active_project.get("name")
            return {
                "success": False,
                "error": f"Project '{project_name}' has no specification saved",
                "spec_content": None,
                "hint": (
                    "Spec may have been created without a specification file. "
                    "Ask user to provide one."
                ),
            }

        spec_content, spec_path = resolve_spec_content(
            technical_spec=project.technical_spec,
            spec_file_path=project.spec_file_path,
        )
        if not spec_content:
            project_name = active_project.get("name")
            return {
                "success": False,
                "error": f"Project '{project_name}' has no specification saved",
                "spec_content": None,
                "hint": (
                    "Spec may have been created without a specification file. "
                    "Ask user to provide one."
                ),
            }

        hydrate_spec_state(
            state,
            technical_spec=project.technical_spec,
            spec_file_path=project.spec_file_path,
        )

        sections = extract_markdown_sections(spec_content)
        token_estimate = len(spec_content) // 4

        logger.info(
            "Loaded spec for %r (~%s tokens)",
            project.name,
            token_estimate,
        )

        return {
            "success": True,
            "spec_content": spec_content,
            "spec_path": spec_path,
            "token_estimate": token_estimate,
            "sections": sections,
            "message": (
                f"Loaded specification (~{token_estimate} tokens, "
                f"{len(sections)} sections)"
            ),
        }


__all__ = [
    "ApproveSpecVersionInput",
    "ApprovedCanonicalSpec",
    "LinkSpecToProjectInput",
    "ReadProjectSpecificationInput",
    "RegisterSpecVersionInput",
    "SaveProjectSpecificationInput",
    "approve_spec_version",
    "extract_markdown_sections",
    "hydrate_spec_state",
    "link_spec_to_project",
    "read_project_specification",
    "register_approved_spec_from_canonical_json",
    "register_spec_version",
    "resolve_spec_content",
    "save_project_specification",
]
