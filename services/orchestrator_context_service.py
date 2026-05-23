"""Service helpers for project detail loading and active-project hydration."""

from __future__ import annotations

import logging
from typing import Any, Protocol, cast

from sqlalchemy import desc, func
from sqlmodel import Session, select

from models.core import Epic, Feature, Theme
from models.core import Product, Sprint, UserStory
from models.db import get_engine
from models.specs import (
    CompiledSpecAuthority,
    SpecRegistry,
)
from services.specs.compiler_service import (
    CompileSpecAuthorityForVersionInput,
    compile_spec_authority_for_version,
)
from services.specs.lifecycle_service import hydrate_spec_state

logger: logging.Logger = logging.getLogger(name=__name__)


class _SupportsState(Protocol):
    state: dict[str, Any]


def get_project_details(product_id: int) -> dict[str, Any]:
    """Load a project's detail snapshot and structure counts."""
    logger.debug("Loading project details for product_id=%s", product_id)
    with Session(get_engine()) as session:
        product = session.get(Product, product_id)
        if not product:
            logger.debug("Product %s not found while loading details.", product_id)
            return {
                "success": False,
                "error": f"Product {product_id} not found",
            }

        theme_count = session.exec(
            select(func.count(cast("Any", Theme.theme_id))).where(
                Theme.product_id == product_id
            )
        ).one()
        epic_count = session.exec(
            select(func.count(cast("Any", Epic.epic_id)))
            .join(Theme)
            .where(Theme.product_id == product_id)
        ).one()
        feature_count = session.exec(
            select(func.count(cast("Any", Feature.feature_id)))
            .join(Epic)
            .join(Theme)
            .where(Theme.product_id == product_id)
        ).one()
        story_count = session.exec(
            select(func.count(cast("Any", UserStory.story_id)))
            .where(UserStory.product_id == product_id)
            .where(UserStory.is_superseded == False)  # noqa: E712
        ).one()
        sprint_count = session.exec(
            select(func.count(cast("Any", Sprint.sprint_id))).where(
                Sprint.product_id == product_id
            )
        ).one()

        latest_spec_version_id = session.exec(
            select(SpecRegistry.spec_version_id)
            .where(
                SpecRegistry.product_id == product_id,
                SpecRegistry.status == "approved",
            )
            .order_by(
                desc(cast("Any", SpecRegistry.approved_at)),
                desc(cast("Any", SpecRegistry.spec_version_id)),
            )
            .limit(1)
        ).first()

        logger.debug("Loaded project details for '%s'.", product.name)
        return {
            "success": True,
            "product": {
                "id": product.product_id,
                "name": product.name,
                "description": product.description,
                "vision": product.vision,
                "roadmap": product.roadmap,
                "technical_spec": product.technical_spec,
                "compiled_authority_json": product.compiled_authority_json,
                "spec_file_path": product.spec_file_path,
                "spec_loaded_at": (
                    product.spec_loaded_at.isoformat()
                    if product.spec_loaded_at
                    else None
                ),
                "latest_spec_version_id": latest_spec_version_id,
            },
            "structure": {
                "themes": theme_count,
                "epics": epic_count,
                "features": feature_count,
                "user_stories": story_count,
                "sprints": sprint_count,
            },
            "message": f"Loaded details for project '{product.name}'",
        }


def select_project(product_id: int, tool_context: _SupportsState) -> dict[str, Any]:
    """Hydrate active-project session state for the selected project."""
    logger.debug("Selecting product_id=%s as the active project.", product_id)

    details = get_project_details(product_id)
    if not details["success"]:
        logger.debug(
            "Failed to select active project because project_id=%s was not found.",
            product_id,
        )
        return details

    state = tool_context.state
    product_details = details["product"]

    state["active_project"] = {
        "product_id": product_id,
        "name": product_details["name"],
        "description": product_details.get("description"),
        "vision": product_details.get("vision"),
        "roadmap": product_details.get("roadmap"),
        "technical_spec": product_details.get("technical_spec"),
        "compiled_authority_json": product_details.get("compiled_authority_json"),
        "spec_file_path": product_details.get("spec_file_path"),
        "spec_loaded_at": product_details.get("spec_loaded_at"),
        "latest_spec_version_id": product_details.get("latest_spec_version_id"),
        "structure": details["structure"],
    }
    state["current_project_name"] = product_details["name"]

    hydrate_spec_state(
        state,
        technical_spec=product_details.get("technical_spec"),
        spec_file_path=product_details.get("spec_file_path"),
    )

    authority_json = product_details.get("compiled_authority_json")
    if authority_json is None and product_details.get("latest_spec_version_id"):
        authority_json = _load_authority_fallback(
            product_id, product_details["latest_spec_version_id"]
        )
        if authority_json:
            state["active_project"]["compiled_authority_json"] = authority_json
    _set_or_clear(state, "compiled_authority_cached", authority_json)
    _set_or_clear(
        state,
        "latest_spec_version_id",
        product_details.get("latest_spec_version_id"),
    )

    if product_details.get("spec_file_path") or product_details.get("technical_spec"):
        state["spec_persisted"] = True

    state["current_context"] = "project_selected"

    logger.debug("Active project set to '%s'.", product_details["name"])
    return {
        "success": True,
        "active_project": state["active_project"],
        "message": f"Selected '{product_details['name']}' as active project",
    }


def _set_or_clear(state: dict[str, Any], key: str, value: object) -> None:
    """Set *key* in state when *value* is not None, otherwise delete it."""
    if value is not None:
        state[key] = value
    elif key in state:
        del state[key]


def _load_authority_fallback(product_id: int, spec_version_id: int) -> str | None:
    """Load compiled authority for the given version, compiling on demand if needed."""
    with Session(get_engine()) as session:
        authority = session.exec(
            select(CompiledSpecAuthority)
            .where(CompiledSpecAuthority.spec_version_id == spec_version_id)
            .limit(1)
        ).first()
        if authority and authority.compiled_artifact_json:
            product = session.get(Product, product_id)
            if product:
                product.compiled_authority_json = authority.compiled_artifact_json
                session.add(product)
                session.commit()
                logger.debug(
                    "Backfilled compiled_authority_json from authority table for "
                    "product_id=%s.",
                    product_id,
                )
            return authority.compiled_artifact_json

    try:
        logger.debug(
            "No compiled authority found; compiling spec_version_id=%s on demand.",
            spec_version_id,
        )
        result = compile_spec_authority_for_version(
            CompileSpecAuthorityForVersionInput(
                spec_version_id=spec_version_id, force_recompile=False
            ),
            tool_context=None,
        )
        if result.get("success"):
            with Session(get_engine()) as session:
                product = session.get(Product, product_id)
                if product and product.compiled_authority_json:
                    logger.debug(
                        "Compiled and cached authority on demand for product_id=%s.",
                        product_id,
                    )
                    return product.compiled_authority_json
    except (RuntimeError, TypeError, ValueError) as exc:
        logger.warning(
            "On-demand authority compilation failed for product_id=%s "
            "spec_version_id=%s: %s",
            product_id,
            spec_version_id,
            exc,
        )

    return None
