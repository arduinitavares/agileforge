"""Workflow-domain contracts for non-positioned Project creation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlmodel import Session

from models.core import Project
from workflow.clock import FixedClock
from workflow.definitions.root import project_graph
from workflow.domain import WorkflowDomain
from workflow.requests import CreateProject, TransitionRequest

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def test_create_project_is_a_closed_non_positioned_request() -> None:
    """Accept creation only through the closed non-positioned request union."""
    request = TypeAdapter(TransitionRequest).validate_python(
        {
            "kind": "create_project",
            "name": "Created from request",
            "description": "A description",
            "repository_binding": None,
            "idempotency_key": "create-1",
            "actor": "operator@example.com",
        }
    )

    assert isinstance(request, CreateProject)


def test_create_project_requires_probe_input_for_a_requested_repository() -> None:
    """Reject an under-prepared repository-backed creation request."""
    with pytest.raises(ValidationError):
        CreateProject(
            name="Under-prepared",
            description=None,
            requested_repository_path="repository",
            repository_binding=None,
            idempotency_key="create-under-prepared",
            actor="operator@example.com",
        )


def test_project_model_has_only_current_identity_state() -> None:
    """Keep only durable Project identity and active repository pointer state."""
    removed = {
        "origin",
        "vision",
        "roadmap",
        "technical_spec",
        "compiled_authority_json",
        "spec_file_path",
        "spec_loaded_at",
    }

    assert removed.isdisjoint(Project.model_fields)
    assert {"name", "description", "active_repository_binding_id"}.issubset(
        Project.model_fields
    )


def test_domain_create_evaluates_the_v2_vision_position(engine: Engine) -> None:
    """Evaluate creation directly into the v2 Vision bootstrap position."""
    domain = WorkflowDomain(
        engine=engine,
        graph=project_graph(),
        clock=FixedClock(now_value=datetime(2026, 8, 9, 12, tzinfo=UTC)),
    )
    result = domain.transition(
        CreateProject(
            name="Created by domain",
            description=None,
            repository_binding=None,
            idempotency_key="create-domain-1",
            actor="operator@example.com",
        )
    )

    assert result.ok is True
    assert result.position is not None
    assert any(item.node_id == "vision.bootstrap" for item in result.position.decisions)
    project_id = result.output["project_id"]
    assert isinstance(project_id, int)
    with Session(engine) as session:
        assert session.get(Project, project_id) is not None
