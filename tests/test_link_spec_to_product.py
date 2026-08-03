"""
TDD Test Suite for link_spec_to_product tool.

This tool replaces save_project_specification for the file-based flow.
Instead of re-reading the spec file and duplicating content into
product.technical_spec, it simply:
  1. Sets product.spec_file_path (the on-disk path)
  2. Sets product.spec_loaded_at timestamp
  3. Triggers authority compilation via update_spec_and_compile_authority

Run with: pytest tests/test_link_spec_to_product.py -v
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

import pytest
from sqlmodel import Session

from agile_sqlmodel import Project
from tests.typing_helpers import make_tool_context, require_id
from tools.spec_tools import link_spec_to_product


class CompileParams(Protocol):
    """Captured compile params used by the test stub."""

    project_id: int
    content_ref: str | None


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def sample_product(session: Session) -> Project:
    """Create a test product WITHOUT specification."""
    product = Project(
        name="Test Link Project",
        vision="A product for link_spec_to_product testing",
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


@pytest.fixture
def sample_product_with_spec(session: Session) -> Project:
    """Create a test product that already has a spec linked."""
    product = Project(
        name="Already Linked Project",
        vision="Already has a spec",
        spec_file_path="specs/existing_spec.md",
        spec_loaded_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


@pytest.fixture
def compile_stub(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Stub authority compilation to avoid LLM calls."""
    calls = {}

    def _stub(params: object, tool_context: object = None) -> dict[str, object]:
        del tool_context
        calls["params"] = params
        return {
            "success": True,
            "spec_version_id": 1,
            "authority_id": 99,
        }

    monkeypatch.setattr(
        "tools.spec_tools.update_spec_and_compile_authority",
        _stub,
    )
    return calls


class MockToolContext:
    """Mock Google ADK ToolContext for testing."""

    def __init__(self, state: dict) -> None:
        """Initialize the test helper."""
        self.state = state


# ============================================================================
# Test Class: link_spec_to_product
# ============================================================================


class TestLinkSpecToProduct:
    """Test suite for the link_spec_to_product tool."""

    def test_links_existing_file_to_product(
        self, session: Session, sample_product: Project, compile_stub: dict[str, object]
    ) -> None:
        """
        GIVEN: A product exists and a valid spec file path.

        WHEN: link_spec_to_product is called
        THEN:
            - product.spec_file_path is set to the given path
            - product.spec_loaded_at is populated
            - product.technical_spec is NOT written (file is source of truth)
            - Authority compilation is triggered with content_ref
            - Returns success.
        """
        del compile_stub
        if not link_spec_to_product:
            pytest.fail("Tool not implemented yet")

        spec_path = "test_specs/test_quadra.md"
        # Ensure test file exists
        p = Path(spec_path)
        p.parent.mkdir(exist_ok=True)
        if not p.exists():
            p.write_text("# Test Spec\n\n## Features\n- F1", encoding="utf-8")

        result = link_spec_to_product(
            {
                "project_id": require_id(sample_product.project_id, "project_id"),
                "spec_path": spec_path,
            },
            tool_context=None,
        )

        assert result["success"] is True
        assert result["spec_path"] == spec_path
        assert result["compile_success"] is True
        assert result["authority_id"] == 99  # noqa: PLR2004

        # Verify DB state
        session.expire_all()
        product = session.get(
            Project, require_id(sample_product.project_id, "project_id")
        )
        assert product is not None
        assert product.spec_file_path == spec_path
        assert product.spec_loaded_at is not None
        # Key assertion: technical_spec is NOT populated
        assert product.technical_spec is None

    def test_rejects_missing_file(
        self, session: Session, sample_product: Project, compile_stub: dict[str, object]
    ) -> None:
        """
        GIVEN: A path to a file that does not exist.

        WHEN: link_spec_to_product is called
        THEN: Returns error, does not modify product.
        """
        del session, compile_stub
        if not link_spec_to_product:
            pytest.fail("Tool not implemented yet")

        result = link_spec_to_product(
            {
                "project_id": require_id(sample_product.project_id, "project_id"),
                "spec_path": "nonexistent/fake_spec.md",
            },
            tool_context=None,
        )

        assert result["success"] is False
        assert "not found" in result["error"].lower()

    def test_rejects_missing_product(
        self, session: Session, compile_stub: dict[str, object]
    ) -> None:
        """
        GIVEN: A project_id that does not exist.

        WHEN: link_spec_to_product is called
        THEN: Returns error.
        """
        del session, compile_stub
        if not link_spec_to_product:
            pytest.fail("Tool not implemented yet")

        result = link_spec_to_product(
            {"project_id": 9999, "spec_path": "test_specs/test_quadra.md"},
            tool_context=None,
        )

        assert result["success"] is False
        assert "not found" in result["error"].lower()

    def test_updates_existing_spec_link(
        self,
        session: Session,
        sample_product_with_spec: Project,
        compile_stub: dict[str, object],
    ) -> None:
        """
        GIVEN: A product that already has a spec linked.

        WHEN: link_spec_to_product is called with a new path
        THEN:
            - spec_file_path is updated
            - spec_loaded_at is refreshed
            - Returns success with updated metadata.
        """
        del compile_stub
        if not link_spec_to_product:
            pytest.fail("Tool not implemented yet")

        new_path = "test_specs/test_quadra.md"
        p = Path(new_path)
        p.parent.mkdir(exist_ok=True)
        if not p.exists():
            p.write_text("# New Spec\n", encoding="utf-8")

        old_loaded_at = sample_product_with_spec.spec_loaded_at
        assert old_loaded_at is not None

        result = link_spec_to_product(
            {
                "project_id": require_id(
                    sample_product_with_spec.project_id, "project_id"
                ),
                "spec_path": new_path,
            },
            tool_context=None,
        )

        assert result["success"] is True

        session.expire_all()
        product = session.get(
            Project, require_id(sample_product_with_spec.project_id, "project_id")
        )
        assert product is not None
        assert product.spec_file_path == new_path
        assert product.spec_loaded_at is not None
        assert product.spec_loaded_at > old_loaded_at

    def test_sets_spec_persisted_in_state(
        self, session: Session, sample_product: Project, compile_stub: dict[str, object]
    ) -> None:
        """
        GIVEN: A valid tool_context with mutable state.

        WHEN: link_spec_to_product succeeds
        THEN: tool_context.state["spec_persisted"] is set to True.
        """
        del session, compile_stub
        if not link_spec_to_product:
            pytest.fail("Tool not implemented yet")

        spec_path = "test_specs/test_quadra.md"
        p = Path(spec_path)
        p.parent.mkdir(exist_ok=True)
        if not p.exists():
            p.write_text("# Test\n", encoding="utf-8")

        ctx = make_tool_context(state={"spec_persisted": False})

        result = link_spec_to_product(
            {
                "project_id": require_id(sample_product.project_id, "project_id"),
                "spec_path": spec_path,
            },
            tool_context=ctx,
        )

        assert result["success"] is True
        assert ctx.state["spec_persisted"] is True

    def test_delegates_to_compile_authority(
        self, session: Session, sample_product: Project, compile_stub: dict[str, object]
    ) -> None:
        """
        GIVEN: A valid product and spec file.

        WHEN: link_spec_to_product is called
        THEN: update_spec_and_compile_authority is called with
              (project_id, content_ref=spec_path).
        """
        del session
        if not link_spec_to_product:
            pytest.fail("Tool not implemented yet")

        spec_path = "test_specs/test_quadra.md"
        p = Path(spec_path)
        p.parent.mkdir(exist_ok=True)
        if not p.exists():
            p.write_text("# Test\n", encoding="utf-8")

        result = link_spec_to_product(
            {
                "project_id": require_id(sample_product.project_id, "project_id"),
                "spec_path": spec_path,
            },
            tool_context=None,
        )

        assert result["success"] is True
        assert "params" in compile_stub

        compile_params = cast("CompileParams", compile_stub["params"])
        assert compile_params.project_id == require_id(
            sample_product.project_id, "project_id"
        )
        assert compile_params.content_ref == spec_path

    def test_no_backup_file_created(
        self, session: Session, sample_product: Project, compile_stub: dict[str, object]
    ) -> None:
        """
        GIVEN: A valid spec file.

        WHEN: link_spec_to_product is called
        THEN: No backup file is created (unlike save_project_specification).
        """
        del session, compile_stub
        if not link_spec_to_product:
            pytest.fail("Tool not implemented yet")

        spec_path = "test_specs/test_quadra.md"
        p = Path(spec_path)
        p.parent.mkdir(exist_ok=True)
        if not p.exists():
            p.write_text("# Test\n", encoding="utf-8")

        result = link_spec_to_product(
            {
                "project_id": require_id(sample_product.project_id, "project_id"),
                "spec_path": spec_path,
            },
            tool_context=None,
        )

        assert result["success"] is True
        assert result.get("file_created") is False

    def test_rejects_oversized_file(
        self, session: Session, sample_product: Project, tmp_path: Path
    ) -> None:
        """
        GIVEN: A spec file larger than 100KB.

        WHEN: link_spec_to_product is called
        THEN: Returns error without modifying product.
        """
        del session
        if not link_spec_to_product:
            pytest.fail("Tool not implemented yet")

        big_file = tmp_path / "huge_spec.md"
        big_file.write_text("x" * 110_000, encoding="utf-8")

        result = link_spec_to_product(
            {
                "project_id": require_id(sample_product.project_id, "project_id"),
                "spec_path": str(big_file),
            },
            tool_context=None,
        )

        assert result["success"] is False
        assert "too large" in result["error"].lower()

    def test_handles_compile_failure_gracefully(
        self, session: Session, sample_product: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        GIVEN: Authority compilation fails.

        WHEN: link_spec_to_product is called
        THEN:
            - spec_file_path is still set (link succeeded)
            - compile_success is False
            - Overall success is True (link itself worked).
        """
        if not link_spec_to_product:
            pytest.fail("Tool not implemented yet")

        def _fail_compile(params: object, tool_context: object = None) -> object:
            del params, tool_context
            return {"success": False, "error": "Compilation error"}

        monkeypatch.setattr(
            "tools.spec_tools.update_spec_and_compile_authority",
            _fail_compile,
        )

        spec_path = "test_specs/test_quadra.md"
        p = Path(spec_path)
        p.parent.mkdir(exist_ok=True)
        if not p.exists():
            p.write_text("# Test\n", encoding="utf-8")

        result = link_spec_to_product(
            {
                "project_id": require_id(sample_product.project_id, "project_id"),
                "spec_path": spec_path,
            },
            tool_context=None,
        )

        assert result["success"] is True
        assert result["compile_success"] is False
        assert "compile_error" in result

        session.expire_all()
        product = session.get(
            Project, require_id(sample_product.project_id, "project_id")
        )
        assert product is not None
        assert product.spec_file_path == spec_path
