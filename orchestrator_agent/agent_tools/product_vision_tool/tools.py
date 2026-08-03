# orchestrator_agent/agent_tools/product_vision_tool/tools.py
"""
product_tools.py
"""

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from google.adk.tools import ToolContext
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from models.core import Product
from models.db import get_engine
from models.enums import WorkflowEventType
from models.events import WorkflowEvent
from services.contracts.vision import SaveVisionInput
from tools.spec_tools import (
    UpdateSpecAndCompileAuthorityInput,
    update_spec_and_compile_authority,
)

# --- Tool for SAVING the vision ---


def save_vision_tool(
    vision_input: SaveVisionInput, tool_context: ToolContext
) -> dict[str, Any]:
    """
    COMMITS the finalized Product Vision to the Business Database.
    Creates a NEW project if product_id is None, otherwise updates EXISTING.
    """
    print(
        f"\n[Tool: save_vision_tool] Processing '{vision_input.project_name}' (ID: {vision_input.product_id})..."
    )

    start_ts = time.perf_counter()
    try:
        with Session(get_engine()) as session:
            p_id = vision_input.product_id

            # Defensive normalization: treat non-positive IDs as "create new".
            if isinstance(p_id, int) and p_id <= 0:
                p_id = None
            is_create_mode = p_id is None

            # Defensive guard: only allow updates when an active project is selected
            # and the requested product_id matches that selection.
            active_project = tool_context.state.get("active_project", {})
            active_project_id = None
            if isinstance(active_project, dict):
                active_project_id = active_project.get("product_id")
            if (
                p_id is not None
                and active_project_id is not None
                and int(active_project_id) != int(p_id)
            ):
                return {
                    "success": False,
                    "error": (
                        "Unsafe update blocked: product_id does not match the active project. "
                        "Select the target project first, or pass product_id=None to create a new project."
                    ),
                }
            if p_id is not None and active_project_id is None:
                existing_by_id = session.get(Product, p_id)
                if not existing_by_id:
                    return {
                        "success": False,
                        "error": (
                            f"Product with ID {p_id} not found. "
                            "Pass product_id=None to create a new project."
                        ),
                    }
                # Without an active selection, only allow updates when the
                # caller targets the same named project to reduce overwrite risk.
                if existing_by_id.name != vision_input.project_name:
                    return {
                        "success": False,
                        "error": (
                            "Unsafe update blocked: product_id exists but project_name does not match. "
                            "Select the target project first, or pass product_id=None to create a new project."
                        ),
                    }

            active_record: Product | None = None

            if p_id is None:
                # Check for existing project by name to prevent duplicates
                statement = select(Product).where(
                    Product.name == vision_input.project_name
                )
                existing_by_name = session.exec(statement).first()

                if existing_by_name:
                    # Fallback to UPDATE mode using the found ID
                    p_id = existing_by_name.product_id
                    print(
                        f"   [DB] Found existing project by name '{vision_input.project_name}' (ID: {p_id}). Updating."
                    )
                    existing_by_name.vision = vision_input.product_vision_statement
                    session.add(existing_by_name)
                    session.commit()
                    session.refresh(existing_by_name)
                    active_record = existing_by_name
                    roadmap_content = existing_by_name.roadmap
                else:
                    # CREATE MODE
                    print(
                        f"   [DB] Creating NEW project: '{vision_input.project_name}'"
                    )
                    new_project = Product(
                        name=vision_input.project_name,
                        vision=vision_input.product_vision_statement,
                    )
                    session.add(new_project)
                    session.commit()
                    session.refresh(new_project)
                    p_id = new_project.product_id
                    active_record = new_project
                    roadmap_content = new_project.roadmap
                    print(f"   [DB] Created with ID: {p_id}")
            else:
                # UPDATE MODE
                existing_project = session.get(Product, p_id)
                if not existing_project:
                    return {
                        "success": False,
                        "error": f"Product with ID {p_id} not found. Pass product_id=None to create a new project.",
                    }

                print(f"   [DB] Updating ID: {p_id}")
                existing_project.vision = vision_input.product_vision_statement
                existing_project.name = vision_input.project_name  # Allow renaming
                session.add(existing_project)
                session.commit()
                active_record = existing_project
                roadmap_content = existing_project.roadmap

            # Update the context state so subsequent tools know the active project
            tool_context.state["current_project_name"] = vision_input.project_name
            # Also set it as the active selection.
            # Simplified Update: We only set what we know. We do not attempt to preserve 'structure'
            # or other fields from potentially non-existent previous state, as this caused crashes.
            tool_context.state["active_project"] = {
                "product_id": p_id,
                "name": vision_input.project_name,
                "description": active_record.description if active_record else None,
                "vision": vision_input.product_vision_statement,
                "roadmap": roadmap_content,
                "technical_spec": active_record.technical_spec
                if active_record
                else None,
                "compiled_authority_json": active_record.compiled_authority_json
                if active_record
                else None,
                "spec_file_path": active_record.spec_file_path
                if active_record
                else None,
                "spec_loaded_at": (
                    active_record.spec_loaded_at.isoformat()
                    if active_record and active_record.spec_loaded_at
                    else None
                ),
                "latest_spec_version_id": None,
                "structure": {
                    "themes": 0,
                    "epics": 0,
                    "features": 0,
                    "user_stories": 0,
                },
            }

            # --- Link spec & compile authority (Pre-Phase completion) ---
            _link_spec_if_pending(p_id, tool_context)

            duration_seconds = tool_context.state.get("vision_generation_duration")
            if duration_seconds is None:
                duration_seconds = round(time.perf_counter() - start_ts, 3)
            session_id = getattr(tool_context, "session_id", None)
            metadata = json.dumps(
                {
                    "mode": "create" if is_create_mode else "update",
                    "project_name": vision_input.project_name,
                }
            )
            session.add(
                WorkflowEvent(
                    event_type=WorkflowEventType.VISION_SAVED,
                    product_id=p_id,
                    session_id=session_id,
                    duration_seconds=float(duration_seconds),
                    event_metadata=metadata,
                )
            )
            session.commit()

            return {
                "success": True,
                "product_id": p_id,
                "message": f"SUCCESS: Saved project '{vision_input.project_name}' vision (ID: {p_id}).",
                "project_name": vision_input.project_name,
            }

    except SQLAlchemyError as e:
        print(f"   [DB Error] {e}")
        return {"success": False, "error": f"Database Error: {e!s}"}


def _link_spec_if_pending(
    product_id: Optional[int],
    tool_context: ToolContext,
) -> None:
    """Link the spec file to the product and compile authority.

    Called by save_vision_tool after the product is created/updated.
    Only acts when ``pending_spec_path`` is present in state and
    ``spec_persisted`` is not yet True.
    """
    if product_id is None:
        return

    state = tool_context.state
    if state.get("spec_persisted"):
        return

    spec_path = state.get("pending_spec_path")
    if not spec_path:
        return

    # Validate the file still exists
    if not Path(spec_path).exists():
        print(
            f"   [save_vision_tool] pending_spec_path '{spec_path}' "
            "not found on disk — skipping spec link."
        )
        return

    # Persist the link (path + timestamp only, no blob)
    with Session(get_engine()) as session:
        product = session.get(Product, product_id)
        if not product:
            return
        product.spec_file_path = spec_path
        product.spec_loaded_at = datetime.now(UTC)
        session.add(product)
        session.commit()
        print(f"   [save_vision_tool] Spec linked → {spec_path}")

    state["spec_persisted"] = True

    # Compile authority (creates SpecRegistry + CompiledSpecAuthority)
    compile_start_ts = time.perf_counter()
    try:
        compile_input = UpdateSpecAndCompileAuthorityInput(
            product_id=product_id,
            content_ref=spec_path,
        )
        compile_result = update_spec_and_compile_authority(
            compile_input,
            tool_context=tool_context,
        )
        if compile_result.get("success"):
            print(
                f"   [save_vision_tool] Authority compiled "
                f"(version={compile_result.get('spec_version_id')})"
            )
        else:
            print(
                f"   [save_vision_tool] Authority compile failed: "
                f"{compile_result.get('error')}"
            )
    except Exception as exc:  # pylint: disable=broad-except
        print(f"   [save_vision_tool] Authority compile error: {exc}")
    finally:
        duration_seconds = round(time.perf_counter() - compile_start_ts, 3)
        session_id = getattr(tool_context, "session_id", None)
        metadata = json.dumps({"spec_path": spec_path})
        with Session(get_engine()) as session:
            session.add(
                WorkflowEvent(
                    event_type=WorkflowEventType.SPEC_COMPILED,
                    product_id=product_id,
                    session_id=session_id,
                    duration_seconds=duration_seconds,
                    event_metadata=metadata,
                )
            )
            session.commit()
