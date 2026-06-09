"""Tests for select_project hydration of session state."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypedDict, Unpack, cast

from sqlmodel import Session

from agile_sqlmodel import Product, SpecRegistry
from tests.typing_helpers import require_id
from tools.orchestrator_tools import get_project_details, select_project

JsonDict = dict[str, Any]
COMPILED_AUTHORITY_JSON = '{"schema_version":"agileforge.compiled_authority.v2"}'

if TYPE_CHECKING:
    from google.adk.tools import ToolContext


class MockToolContext:
    """Minimal ToolContext stub with state dict."""

    def __init__(self, state: JsonDict) -> None:
        """Initialize the test helper."""
        self.state = state


class ProductOverrides(TypedDict, total=False):
    """Optional product fields used by test fixtures."""

    name: str
    vision: str | None
    description: str | None
    roadmap: str | None
    technical_spec: str | None
    compiled_authority_json: str | None
    spec_file_path: str | None
    spec_loaded_at: datetime | None


def _create_product(session: Session, **kwargs: Unpack[ProductOverrides]) -> Product:
    product = Product(
        name=kwargs.get("name", "Hydration Project"),
        vision=kwargs.get("vision", "Vision"),
        description=kwargs.get("description"),
        roadmap=kwargs.get("roadmap"),
        technical_spec=kwargs.get("technical_spec"),
        compiled_authority_json=kwargs.get("compiled_authority_json"),
        spec_file_path=kwargs.get("spec_file_path"),
        spec_loaded_at=kwargs.get("spec_loaded_at"),
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


def _create_approved_spec(session: Session, product_id: int) -> SpecRegistry:
    spec = SpecRegistry(
        product_id=product_id,
        spec_hash="hash",
        content="# Spec content",
        content_ref="specs/spec.md",
        status="approved",
        approved_at=datetime.now(UTC),
        approved_by="tester",
    )
    session.add(spec)
    session.commit()
    session.refresh(spec)
    return spec


def test_select_project_hydrates_spec_and_authority(session: Session) -> None:
    """Verify select project hydrates spec and authority."""
    spec_loaded_at = datetime.now(UTC)
    product = _create_product(
        session,
        technical_spec="Spec body",
        compiled_authority_json=COMPILED_AUTHORITY_JSON,
        spec_file_path="specs/spec.md",
        spec_loaded_at=spec_loaded_at,
        description="Desc",
    )
    spec = _create_approved_spec(session, require_id(product.product_id, "product_id"))

    state: JsonDict = {
        "pending_spec_content": "OLD",
        "pending_spec_path": "OLD",
        "compiled_authority_cached": "OLD",
        "latest_spec_version_id": 999,
    }
    context = MockToolContext(state)

    result = select_project(
        require_id(product.product_id, "product_id"), cast("ToolContext", context)
    )

    assert result["success"] is True
    assert context.state["pending_spec_content"] == "Spec body"
    assert context.state["pending_spec_path"] == "specs/spec.md"
    assert context.state["compiled_authority_cached"] == COMPILED_AUTHORITY_JSON
    assert context.state["latest_spec_version_id"] == require_id(
        spec.spec_version_id, "spec_version_id"
    )
    assert context.state["current_project_name"] == product.name

    active_project = context.state["active_project"]
    assert active_project["description"] == "Desc"
    assert active_project["technical_spec"] == "Spec body"
    assert active_project["compiled_authority_json"] == COMPILED_AUTHORITY_JSON
    assert active_project["spec_file_path"] == "specs/spec.md"
    expected_loaded_at = spec_loaded_at.replace(tzinfo=None).isoformat()
    assert active_project["spec_loaded_at"] == expected_loaded_at


def test_select_project_clears_missing_spec_state(session: Session) -> None:
    """Verify select project clears missing spec state."""
    product = _create_product(session)

    state: JsonDict = {
        "pending_spec_content": "OLD",
        "pending_spec_path": "OLD",
        "compiled_authority_cached": "OLD",
        "latest_spec_version_id": 999,
    }
    context = MockToolContext(state)

    result = select_project(
        require_id(product.product_id, "product_id"), cast("ToolContext", context)
    )

    assert result["success"] is True
    assert "pending_spec_content" not in context.state
    assert "pending_spec_path" not in context.state
    assert "compiled_authority_cached" not in context.state
    assert "latest_spec_version_id" not in context.state


def test_get_project_details_includes_spec_fields(session: Session) -> None:
    """Verify get project details includes spec fields."""
    spec_loaded_at = datetime.now(UTC)
    product = _create_product(
        session,
        technical_spec="Spec body",
        compiled_authority_json=COMPILED_AUTHORITY_JSON,
        spec_file_path="specs/spec.md",
        spec_loaded_at=spec_loaded_at,
        description="Desc",
    )
    spec = _create_approved_spec(session, require_id(product.product_id, "product_id"))

    result = get_project_details(require_id(product.product_id, "product_id"))

    assert result["success"] is True
    details = result["product"]
    assert details["technical_spec"] == "Spec body"
    assert details["compiled_authority_json"] == COMPILED_AUTHORITY_JSON
    assert details["spec_file_path"] == "specs/spec.md"
    expected_loaded_at = spec_loaded_at.replace(tzinfo=None).isoformat()
    assert details["spec_loaded_at"] == expected_loaded_at
    assert details["latest_spec_version_id"] == require_id(
        spec.spec_version_id, "spec_version_id"
    )
