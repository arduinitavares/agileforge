"""Tools for the Roadmap Builder agent."""

import json
import time
from typing import Annotated, Any

from google.adk.tools import ToolContext
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from models.core import Product
from models.db import get_engine
from models.enums import WorkflowEventType
from models.events import WorkflowEvent
from orchestrator_agent.agent_tools.roadmap_builder.schemes import RoadmapBuilderOutput


class SaveRoadmapToolInput(BaseModel):
    """Input schema for the save_roadmap_tool."""

    product_id: Annotated[
        int,
        Field(description="The ID of the product to update."),
    ]
    roadmap_data: Annotated[
        RoadmapBuilderOutput,
        Field(description="The comprehensive roadmap data to save."),
    ]
    idempotency_key: Annotated[
        str | None,
        Field(
            default=None,
            description="Optional save idempotency key supplied by guarded callers.",
        ),
    ]


def save_roadmap_tool(
    input_data: SaveRoadmapToolInput,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """
    Saves the generated roadmap to the Product.roadmap field in the database.
    Input must be the full RoadmapBuilderOutput object.
    """
    engine = get_engine()
    start_ts = time.perf_counter()

    with Session(engine) as session:
        replay = _idempotent_roadmap_save_replay(
            session,
            product_id=input_data.product_id,
            idempotency_key=input_data.idempotency_key,
        )
        if replay is not None:
            return replay

        # Retrieve the product
        product = session.exec(
            select(Product).where(Product.product_id == input_data.product_id)
        ).first()

        if not product:
            return {
                "success": False,
                "error": f"Product with ID {input_data.product_id} not found.",
            }

        # Serialize the roadmap data to JSON
        # We save the whole output (releases + summary)
        roadmap_json = input_data.roadmap_data.model_dump_json()

        # Update the product
        product.roadmap = roadmap_json
        session.add(product)
        duration_seconds = None
        if tool_context and tool_context.state:
            duration_seconds = tool_context.state.get("roadmap_generation_duration")
        if duration_seconds is None:
            duration_seconds = round(time.perf_counter() - start_ts, 3)
        session_id = getattr(tool_context, "session_id", None) if tool_context else None
        metadata = json.dumps(
            {
                "action": "roadmap_saved",
                "idempotency_key": input_data.idempotency_key,
                "releases_count": len(input_data.roadmap_data.roadmap_releases),
            }
        )
        session.add(
            WorkflowEvent(
                event_type=WorkflowEventType.ROADMAP_SAVED,
                product_id=input_data.product_id,
                session_id=session_id,
                duration_seconds=float(duration_seconds),
                event_metadata=metadata,
            )
        )
        session.commit()
        session.refresh(product)

        return {
            "success": True,
            "product_id": product.product_id,
            "message": "Roadmap saved successfully to Product.roadmap.",
            "releases_count": len(input_data.roadmap_data.roadmap_releases),
            "saved_roadmap": input_data.roadmap_data.model_dump(mode="json"),
        }


def _idempotent_roadmap_save_replay(
    session: Session,
    *,
    product_id: int,
    idempotency_key: str | None,
) -> dict[str, Any] | None:
    """Return a replay response when this Roadmap save key already succeeded."""
    if not idempotency_key:
        return None
    events = session.exec(
        select(WorkflowEvent)
        .where(WorkflowEvent.product_id == product_id)
        .where(WorkflowEvent.event_type == WorkflowEventType.ROADMAP_SAVED)
    ).all()
    for event in events:
        metadata = _json_object(event.event_metadata)
        if metadata.get("idempotency_key") != idempotency_key:
            continue
        return {
            "success": True,
            "product_id": product_id,
            "releases_count": int(metadata.get("releases_count") or 0),
            "idempotent_replay": True,
            "message": "Roadmap save idempotency key already persisted.",
        }
    return None


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}
