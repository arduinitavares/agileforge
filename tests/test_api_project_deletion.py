"""API project-deletion transaction-boundary tests."""

from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import HTTPException
from sqlmodel import Session

import api as api_module
from models.core import Product
from repositories.product import ProductRepository, ProjectDeletionConflictError


@dataclass
class _Product:
    product_id: int


class _ProductRepository:
    def __init__(
        self,
        *,
        calls: list[str],
        failure: Exception | None = None,
    ) -> None:
        self.calls = calls
        self.failure = failure
        self.product: _Product | None = _Product(product_id=7)

    def get_by_id(self, product_id: int) -> _Product | None:
        product = self.product
        if product is None or product_id != product.product_id:
            return None
        return product

    def delete_project(self, product_id: int) -> bool:
        product = self.product
        assert product is not None
        assert product_id == product.product_id
        self.calls.append("repository")
        if self.failure is not None:
            raise self.failure
        self.product = None
        return True


class _WorkflowService:
    def __init__(
        self,
        *,
        calls: list[str],
        failure: Exception | None = None,
    ) -> None:
        self.calls = calls
        self.failure = failure
        self.sessions: dict[str, dict[str, Any]] = {"7": {"fsm_state": "VISION_REVIEW"}}

    def delete_session(self, session_id: str) -> bool:
        self.calls.append("workflow")
        if self.failure is not None:
            raise self.failure
        return self.sessions.pop(session_id, None) is not None


@pytest.mark.asyncio
async def test_delete_project_preserves_session_when_repository_delete_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not remove recoverable workflow state before durable deletion commits."""
    calls: list[str] = []
    repository = _ProductRepository(
        calls=calls,
        failure=RuntimeError("injected repository failure"),
    )
    workflow = _WorkflowService(calls=calls)
    monkeypatch.setattr(api_module, "product_repo", repository)
    monkeypatch.setattr(api_module, "workflow_service", workflow)

    with pytest.raises(HTTPException) as exc_info:
        await api_module.delete_project(7)

    assert exc_info.value.status_code == 500  # noqa: PLR2004
    assert calls == ["repository"]
    assert workflow.sessions["7"] == {"fsm_state": "VISION_REVIEW"}


@pytest.mark.asyncio
async def test_delete_project_returns_success_when_session_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
) -> None:
    """Report the committed durable deletion truthfully after cleanup failure."""
    calls: list[str] = []
    session.add(Product(product_id=7, name="Durable deletion target"))
    session.commit()
    repository = ProductRepository(session)
    workflow = _WorkflowService(
        calls=calls,
        failure=RuntimeError("injected workflow cleanup failure"),
    )
    monkeypatch.setattr(api_module, "product_repo", repository)
    monkeypatch.setattr(api_module, "workflow_service", workflow)

    result = await api_module.delete_project(7)

    assert calls == ["workflow"]
    assert session.get(Product, 7) is None
    assert result == {
        "status": "success",
        "data": {"message": "Project 7 deleted."},
    }


@pytest.mark.asyncio
async def test_delete_project_maps_discovery_dependency_to_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expose a stable conflict without discarding the workflow session."""
    calls: list[str] = []
    conflict = ProjectDeletionConflictError(
        product_id=7,
        references=("discovery_prds.challenge_artifact_id",),
    )
    repository = _ProductRepository(calls=calls, failure=conflict)
    workflow = _WorkflowService(calls=calls)
    monkeypatch.setattr(api_module, "product_repo", repository)
    monkeypatch.setattr(api_module, "workflow_service", workflow)

    with pytest.raises(HTTPException) as exc_info:
        await api_module.delete_project(7)

    assert exc_info.value.status_code == 409  # noqa: PLR2004
    assert exc_info.value.detail == (
        "Project deletion blocked by cross-project discovery references."
    )
    assert calls == ["repository"]
    assert workflow.sessions["7"] == {"fsm_state": "VISION_REVIEW"}
