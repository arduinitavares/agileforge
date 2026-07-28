"""Service-level tests for orchestrator context selection and detail hydration."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypedDict, Unpack

from agile_sqlmodel import CompiledSpecAuthority, Product, SpecRegistry
from tests.authority_assumption_fixtures import historical_v2_compiled_authority
from tests.typing_helpers import require_id
from utils.spec_schemas import (
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerOutput,
)

JsonDict = dict[str, Any]

if TYPE_CHECKING:
    import pytest
    from sqlmodel import Session


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


def _compiled_v3_json() -> str:
    return SpecAuthorityCompilerOutput(
        root=SpecAuthorityCompilationSuccess(
            scope_themes=[],
            invariants=[],
            eligible_feature_rules=[],
            gaps=[],
            assumptions=[],
            source_map=[],
            compiler_version="3.0.0",
            prompt_hash="b" * 64,
        )
    ).model_dump_json()


def test_context_service_get_project_details_includes_spec_fields(
    session: Session,
) -> None:
    """Verify context service get project details includes spec fields."""
    from services.orchestrator_context_service import (  # noqa: PLC0415
        get_project_details,
    )

    spec_loaded_at = datetime.now(UTC)
    compiled_json = _compiled_v3_json()
    product = _create_product(
        session,
        technical_spec="Spec body",
        compiled_authority_json=compiled_json,
        spec_file_path="specs/spec.md",
        spec_loaded_at=spec_loaded_at,
        description="Desc",
    )
    spec = _create_approved_spec(session, require_id(product.product_id, "product_id"))

    result = get_project_details(require_id(product.product_id, "product_id"))

    assert result["success"] is True
    details = result["product"]
    assert details["technical_spec"] == "Spec body"
    assert details["compiled_authority_json"] == compiled_json
    assert details["spec_file_path"] == "specs/spec.md"
    expected_loaded_at = spec_loaded_at.replace(tzinfo=None).isoformat()
    assert details["spec_loaded_at"] == expected_loaded_at
    assert details["latest_spec_version_id"] == require_id(
        spec.spec_version_id, "spec_version_id"
    )


def test_context_service_select_project_hydrates_spec_and_authority(
    session: Session,
) -> None:
    """Verify context service select project hydrates spec and authority."""
    from services.orchestrator_context_service import select_project  # noqa: PLC0415

    spec_loaded_at = datetime.now(UTC)
    compiled_json = _compiled_v3_json()
    product = _create_product(
        session,
        technical_spec="Spec body",
        compiled_authority_json=compiled_json,
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

    result = select_project(require_id(product.product_id, "product_id"), context)

    assert result["success"] is True
    assert context.state["pending_spec_content"] == "Spec body"
    assert context.state["pending_spec_path"] == "specs/spec.md"
    assert context.state["compiled_authority_cached"] == compiled_json
    assert context.state["latest_spec_version_id"] == require_id(
        spec.spec_version_id, "spec_version_id"
    )
    assert context.state["current_project_name"] == product.name

    active_project = context.state["active_project"]
    assert active_project["description"] == "Desc"
    assert active_project["technical_spec"] == "Spec body"
    assert active_project["compiled_authority_json"] == compiled_json
    assert active_project["spec_file_path"] == "specs/spec.md"
    expected_loaded_at = spec_loaded_at.replace(tzinfo=None).isoformat()
    assert active_project["spec_loaded_at"] == expected_loaded_at


def test_context_service_select_project_clears_missing_spec_state(
    session: Session,
) -> None:
    """Verify context service select project clears missing spec state."""
    from services.orchestrator_context_service import select_project  # noqa: PLC0415

    product = _create_product(session)

    state: JsonDict = {
        "pending_spec_content": "OLD",
        "pending_spec_path": "OLD",
        "compiled_authority_cached": "OLD",
        "latest_spec_version_id": 999,
    }
    context = MockToolContext(state)

    result = select_project(require_id(product.product_id, "product_id"), context)

    assert result["success"] is True
    assert "pending_spec_content" not in context.state
    assert "pending_spec_path" not in context.state
    assert "compiled_authority_cached" not in context.state
    assert "latest_spec_version_id" not in context.state


def test_context_service_select_project_rejects_legacy_authority_without_backfill(
    session: Session,
) -> None:
    """Selecting a project must not cache/backfill unsupported authority artifacts."""
    from services.orchestrator_context_service import select_project  # noqa: PLC0415

    product = _create_product(
        session,
        technical_spec="Spec body",
        spec_file_path="specs/spec.md",
    )
    product_id = require_id(product.product_id, "product_id")
    spec = _create_approved_spec(session, product_id)
    spec_version_id = require_id(spec.spec_version_id, "spec_version_id")
    session.add(
        CompiledSpecAuthority(
            spec_version_id=spec_version_id,
            compiler_version="3.0.0",
            prompt_hash="legacy",
            compiled_artifact_json='{"invariants":[]}',
            scope_themes="[]",
            invariants="[]",
            eligible_feature_ids="[]",
            rejected_features="[]",
            spec_gaps="[]",
        )
    )
    session.commit()

    context = MockToolContext({"compiled_authority_cached": "OLD"})

    result = select_project(product_id, context)

    assert result["success"] is False
    assert result["error"]["code"] == "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED"
    assert result["error"]["message"] == (
        "Compiled authority artifact schema is unsupported."
    )
    assert result["error"]["details"] == {
        "project_id": product_id,
        "spec_version_id": spec_version_id,
        "authority_id": 1,
        "load_status": "schema_unsupported",
        "observed_schema_version": None,
        "required_schema_version": "agileforge.compiled_authority.v3",
    }
    assert result["error"]["remediation"] == [
        (
            "Run agileforge authority regenerate "
            f"--project-id {product_id} "
            f"--spec-version-id {spec_version_id} "
            "--idempotency-key <new-key>."
        )
    ]
    assert "compiled_authority_cached" not in context.state
    refreshed = session.get(Product, product_id)
    assert refreshed is not None
    assert refreshed.compiled_authority_json is None


def test_context_service_rejects_malformed_v3_without_backfill_or_compile(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed selected rows clear caches and never trigger automatic compile."""
    from services import orchestrator_context_service  # noqa: PLC0415

    product = _create_product(
        session,
        technical_spec="Spec body",
        spec_file_path="specs/spec.md",
    )
    product_id = require_id(product.product_id, "product_id")
    spec = _create_approved_spec(session, product_id)
    spec_version_id = require_id(spec.spec_version_id, "spec_version_id")
    authority = CompiledSpecAuthority(
        spec_version_id=spec_version_id,
        compiler_version="3.0.0",
        prompt_hash="malformed",
        compiled_artifact_json=json.dumps(
            {"schema_version": "agileforge.compiled_authority.v3"}
        ),
        scope_themes="[]",
        invariants="[]",
        eligible_feature_ids="[]",
        rejected_features="[]",
        spec_gaps="[]",
    )
    session.add(authority)
    session.commit()
    session.refresh(authority)
    compile_calls: list[object] = []
    monkeypatch.setattr(
        orchestrator_context_service,
        "compile_spec_authority_for_version",
        lambda params, tool_context: compile_calls.append((params, tool_context)),
    )
    context = MockToolContext({"compiled_authority_cached": "OLD"})

    result = orchestrator_context_service.select_project(product_id, context)

    assert result["success"] is False
    error = result["error"]
    assert error["code"] == "COMPILED_AUTHORITY_INVALID"
    assert error["details"]["load_status"] == "schema_invalid"
    assert error["details"]["authority_id"] == authority.authority_id
    assert "compiled_authority_cached" not in context.state
    assert context.state["active_project"]["compiled_authority_json"] is None
    assert compile_calls == []
    refreshed = session.get(Product, product_id)
    assert refreshed is not None
    assert refreshed.compiled_authority_json is None


def test_context_fallback_backfills_newest_v3_row_over_historical_v2(
    session: Session,
) -> None:
    """Fallback chooses the newest v3 row rather than older unsupported history."""
    from services.orchestrator_context_service import select_project  # noqa: PLC0415

    product = _create_product(
        session,
        technical_spec="Spec body",
        spec_file_path="specs/spec.md",
    )
    product_id = require_id(product.product_id, "product_id")
    spec = _create_approved_spec(session, product_id)
    spec_version_id = require_id(spec.spec_version_id, "spec_version_id")
    newest_json = _compiled_v3_json()
    historical = historical_v2_compiled_authority(prompt_hash="a" * 64)
    session.add_all(
        [
            CompiledSpecAuthority(
                spec_version_id=spec_version_id,
                compiler_version=str(historical["compiler_version"]),
                prompt_hash=str(historical["prompt_hash"]),
                compiled_artifact_json=json.dumps(historical),
                scope_themes="[]",
                invariants="[]",
                eligible_feature_ids="[]",
                rejected_features="[]",
                spec_gaps="[]",
            ),
            CompiledSpecAuthority(
                spec_version_id=spec_version_id,
                compiler_version="3.0.0",
                prompt_hash="b" * 64,
                compiled_artifact_json=newest_json,
                scope_themes="[]",
                invariants="[]",
                eligible_feature_ids="[]",
                rejected_features="[]",
                spec_gaps="[]",
            ),
        ]
    )
    session.commit()

    context = MockToolContext({})
    result = select_project(product_id, context)

    assert result["success"] is True
    assert context.state["compiled_authority_cached"] == newest_json
    refreshed = session.get(Product, product_id)
    assert refreshed is not None
    assert refreshed.compiled_authority_json == newest_json
