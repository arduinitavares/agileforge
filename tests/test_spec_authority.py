"""
Test Specification Authority v1 — Versioning, Approval, and Compilation.

Tests cover:
- AC1: Spec registry with versioning and immutability after approval
- AC2: Explicit approval gate with metadata
- AC3: Explicit compilation (no auto-compile)
- AC4: Authority status checks (CURRENT/STALE/NOT_COMPILED/PENDING_REVIEW)
- AC5: Story validation requires explicit spec_version_id
- AC6: Deterministic retrieval by version with clear errors
"""

import hashlib
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from agile_sqlmodel import (
    CompiledSpecAuthority,
    Product,
    SpecAuthorityStatus,
    SpecRegistry,
    UserStory,
)
from tests.typing_helpers import make_tool_context, require_id
from tools import spec_tools
from tools.spec_tools import (
    approve_spec_version,
    check_spec_authority_status,
    compile_spec_authority,
    get_compiled_authority_by_version,
    register_spec_version,
)
from utils.spec_schemas import (
    Invariant,
    InvariantType,
    RequiredFieldParams,
    SourceMapEntry,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerOutput,
)


@pytest.fixture
def sample_product(session: Session, engine: Engine) -> Product:
    """Create a product without spec."""
    # Monkey-patch the engine for tools to use test database
    spec_tools.engine = engine

    product = Product(
        name="Test Product",
        description="Product for spec authority tests",
        vision="Build amazing things",
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


@pytest.fixture
def sample_spec_content() -> str:
    """Sample spec content for testing."""
    return """
# Technical Specification v1

## Scope
- Feature A: User authentication
- Feature B: Data export

## Invariants
- All API calls require auth token
- Export formats: CSV, JSON only
"""


def _build_raw_compiler_output(excerpt: str, field_name: str) -> str:
    """Build a raw compiler JSON output (pre-normalization)."""
    invariant = Invariant(
        id="INV-0000000000000000",
        type=InvariantType.REQUIRED_FIELD,
        parameters=RequiredFieldParams(field_name=field_name),
    )
    success = SpecAuthorityCompilationSuccess(
        scope_themes=["Scope"],
        domain="Test Domain",
        invariants=[invariant],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[
            SourceMapEntry(
                invariant_id=invariant.id,
                excerpt=excerpt,
                location=None,
            )
        ],
        compiler_version="0.0.0",
        prompt_hash="0" * 64,
    )
    return SpecAuthorityCompilerOutput(root=success).model_dump_json()


@pytest.fixture
def compiler_stub(monkeypatch: pytest.MonkeyPatch) -> object:
    """Stub compiler agent to avoid real LLM calls."""
    raw_json = _build_raw_compiler_output(
        excerpt="The payload must include user_id.",
        field_name="user_id",
    )
    monkeypatch.setattr(
        spec_tools,
        "_invoke_spec_authority_compiler",
        lambda **_: raw_json,
    )
    return raw_json


class TestSpecRegistryVersioning:
    """AC1 — Spec Registry (Versioning)."""

    def test_register_spec_version_creates_draft(
        self, session: Session, sample_product: Product, sample_spec_content: str
    ) -> None:
        """Test creating a new spec version in draft status."""
        result = register_spec_version(
            {
                "product_id": sample_product.product_id,
                "content": sample_spec_content,
                "content_ref": "specs/test_spec.md",
            },
            tool_context=None,
        )

        assert result["success"] is True
        assert "spec_version_id" in result
        spec_version_id = result["spec_version_id"]

        # Verify in database
        spec = session.get(SpecRegistry, spec_version_id)
        assert spec is not None
        assert spec.product_id == sample_product.product_id
        assert spec.status == "draft"
        assert (
            spec.spec_hash == hashlib.sha256(sample_spec_content.encode()).hexdigest()
        )
        assert spec.content == sample_spec_content
        assert spec.content_ref == "specs/test_spec.md"
        assert spec.approved_at is None
        assert spec.approved_by is None

    def test_register_spec_version_computes_correct_hash(
        self, session: Session, sample_product: Product
    ) -> None:
        """Test that spec_hash is correctly computed."""
        content_v1 = "Version 1 content"
        result_v1 = register_spec_version(
            {"product_id": sample_product.product_id, "content": content_v1},
            tool_context=None,
        )

        expected_hash = hashlib.sha256(content_v1.encode()).hexdigest()
        spec_v1 = session.get(SpecRegistry, result_v1["spec_version_id"])
        assert spec_v1 is not None
        assert spec_v1.spec_hash == expected_hash

    def test_register_multiple_versions_for_same_product(
        self, session: Session, sample_product: Product
    ) -> None:
        """Test creating multiple spec versions for the same product."""
        v1_result = register_spec_version(
            {"product_id": sample_product.product_id, "content": "Version 1"},
            tool_context=None,
        )
        v2_result = register_spec_version(
            {"product_id": sample_product.product_id, "content": "Version 2 (changed)"},
            tool_context=None,
        )

        assert v1_result["success"] is True
        assert v2_result["success"] is True
        assert v1_result["spec_version_id"] != v2_result["spec_version_id"]

        # Both should exist
        v1 = session.get(SpecRegistry, v1_result["spec_version_id"])
        v2 = session.get(SpecRegistry, v2_result["spec_version_id"])
        assert v1 is not None
        assert v2 is not None
        assert v1.spec_hash != v2.spec_hash

    def test_approved_spec_cannot_be_modified(
        self, session: Session, sample_product: Product, sample_spec_content: str
    ) -> None:
        """AC1 — Approved specs are immutable."""
        # Create and approve a spec
        reg_result = register_spec_version(
            {"product_id": sample_product.product_id, "content": sample_spec_content},
            tool_context=None,
        )
        spec_version_id = reg_result["spec_version_id"]

        approve_result = approve_spec_version(
            {
                "spec_version_id": spec_version_id,
                "approved_by": "test_reviewer",
                "approval_notes": "LGTM",
            },
            tool_context=None,
        )
        assert approve_result["success"] is True

        # Attempt to update should fail (this will be enforced by tools)
        # For now, we test that the spec status is 'approved'
        spec = session.get(SpecRegistry, spec_version_id)
        assert spec is not None
        assert spec.status == "approved"
        assert spec.approved_by == "test_reviewer"
        assert spec.approval_notes == "LGTM"
        assert spec.approved_at is not None


class TestExplicitApprovalGate:
    """AC2 — Explicit Approval Gate."""

    def test_register_spec_version_delegates_to_lifecycle_service(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tool entrypoint should remain a thin compatibility adapter."""
        expected = {"success": True, "spec_version_id": 77}
        captured: dict[str, object] = {}

        def fake_service(params: object, *, tool_context: object = None) -> object:
            captured["params"] = params
            captured["tool_context"] = tool_context
            return expected

        monkeypatch.setattr(
            spec_tools,
            "_service_register_spec_version",
            fake_service,
            raising=False,
        )

        sentinel_context = make_tool_context()
        params = {"product_id": 3, "content": "spec-content"}
        result = register_spec_version(
            params,
            tool_context=sentinel_context,
        )

        assert result is expected
        assert captured["params"] == params
        assert captured["tool_context"] is sentinel_context

    def test_approve_spec_version_delegates_to_lifecycle_service(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tool approval entrypoint should delegate through lifecycle service."""
        expected = {"success": True, "spec_version_id": 88}
        captured: dict[str, object] = {}

        def fake_service(params: object, *, tool_context: object = None) -> object:
            captured["params"] = params
            captured["tool_context"] = tool_context
            return expected

        monkeypatch.setattr(
            spec_tools,
            "_service_approve_spec_version",
            fake_service,
            raising=False,
        )

        sentinel_context = make_tool_context()
        params = {"spec_version_id": 88, "approved_by": "adapter@test"}
        result = approve_spec_version(
            params,
            tool_context=sentinel_context,
        )

        assert result is expected
        assert captured["params"] == params
        assert captured["tool_context"] is sentinel_context

    def test_approve_spec_version_records_metadata(
        self, session: Session, sample_product: Product, sample_spec_content: str
    ) -> None:
        """Test that approval records timestamp, approver, and notes."""
        reg_result = register_spec_version(
            {"product_id": sample_product.product_id, "content": sample_spec_content},
            tool_context=None,
        )
        spec_version_id = reg_result["spec_version_id"]

        before_approval = datetime.now(UTC)
        result = approve_spec_version(
            {
                "spec_version_id": spec_version_id,
                "approved_by": "jane.doe@example.com",
                "approval_notes": "Reviewed and approved after team discussion",
            },
            tool_context=None,
        )

        assert result["success"] is True

        spec = session.get(SpecRegistry, spec_version_id)
        assert spec is not None
        assert spec.status == "approved"
        assert spec.approved_by == "jane.doe@example.com"
        assert spec.approval_notes == "Reviewed and approved after team discussion"
        assert spec.approved_at is not None
        # Normalize both datetimes to UTC aware for comparison
        approved_at_aware = (
            spec.approved_at
            if spec.approved_at.tzinfo
            else spec.approved_at.replace(tzinfo=UTC)
        )
        assert approved_at_aware >= before_approval

    def test_cannot_approve_nonexistent_spec(self, engine: Engine) -> None:
        """Test that approving a non-existent spec fails gracefully."""
        spec_tools.engine = engine
        result = approve_spec_version(
            {"spec_version_id": 99999, "approved_by": "test_reviewer"},
            tool_context=None,
        )
        assert result["success"] is False
        assert "not found" in result["error"].lower()


class TestExplicitCompilation:
    """AC3 — Explicit Compilation Produces Cached Authority (No Auto-Compile)."""

    def test_compile_delegates_to_service_boundary_with_legacy_extractor_seam(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """compile_spec_authority remains a thin adapter over compiler service."""
        expected = {"success": True, "authority_id": 123}
        captured: dict[str, object] = {}

        def fake_service(
            params: object,
            *,
            tool_context: object = None,
            extract_authority: object = None,
        ) -> object:
            captured["params"] = params
            captured["tool_context"] = tool_context
            captured["extract_authority"] = extract_authority
            return expected

        legacy_extractor = object()
        monkeypatch.setattr(
            spec_tools,
            "_service_compile_spec_authority",
            fake_service,
            raising=False,
        )
        monkeypatch.setattr(
            spec_tools,
            "_extract_spec_authority_llm",
            legacy_extractor,
            raising=False,
        )

        sentinel_context = make_tool_context()
        params = {"spec_version_id": 99}
        result = compile_spec_authority(
            params,
            tool_context=sentinel_context,
        )

        assert result == expected
        assert captured["params"] == params
        assert captured["tool_context"] is sentinel_context
        assert captured["extract_authority"] is legacy_extractor

    def test_compile_fails_for_unapproved_spec(
        self, session: Session, sample_product: Product, sample_spec_content: str
    ) -> None:
        """Test that compilation is blocked for unapproved specs."""
        del session
        reg_result = register_spec_version(
            {"product_id": sample_product.product_id, "content": sample_spec_content},
            tool_context=None,
        )
        spec_version_id = reg_result["spec_version_id"]

        # Attempt to compile without approval
        result = compile_spec_authority(
            {"spec_version_id": spec_version_id}, tool_context=None
        )

        assert result["success"] is False
        assert "not approved" in result["error"].lower()

    def test_compile_creates_cached_authority_for_approved_spec(
        self,
        session: Session,
        sample_product: Product,
        sample_spec_content: str,
        compiler_stub: object,
    ) -> None:
        """Test successful compilation of an approved spec."""
        # Register and approve
        del compiler_stub
        reg_result = register_spec_version(
            {"product_id": sample_product.product_id, "content": sample_spec_content},
            tool_context=None,
        )
        spec_version_id = reg_result["spec_version_id"]

        approve_spec_version(
            {"spec_version_id": spec_version_id, "approved_by": "test_reviewer"},
            tool_context=None,
        )

        # Compile
        before_compile = datetime.now(UTC)
        result = compile_spec_authority(
            {"spec_version_id": spec_version_id}, tool_context=None
        )

        assert result["success"] is True
        assert "authority_id" in result

        # Verify compiled authority in DB
        authority = session.exec(
            select(CompiledSpecAuthority).where(
                CompiledSpecAuthority.spec_version_id == spec_version_id
            )
        ).first()

        assert authority is not None
        assert authority.spec_version_id == spec_version_id
        assert authority.compiler_version is not None
        assert authority.prompt_hash is not None
        # authority.compiled_at should be timezone-aware
        assert authority.compiled_at is not None
        # Normalize both datetimes to UTC aware for comparison
        compiled_at_aware = (
            authority.compiled_at
            if authority.compiled_at.tzinfo
            else authority.compiled_at.replace(tzinfo=UTC)
        )
        assert compiled_at_aware >= before_compile

        # Verify JSON fields are populated
        scope_themes = json.loads(authority.scope_themes)
        invariants = json.loads(authority.invariants)
        assert isinstance(scope_themes, list)
        assert isinstance(invariants, list)

    def test_compile_stores_compiler_version_and_prompt_hash(
        self,
        session: Session,
        sample_product: Product,
        sample_spec_content: str,
        compiler_stub: object,
    ) -> None:
        """Test that compilation stores reproducibility metadata."""
        del compiler_stub
        reg_result = register_spec_version(
            {"product_id": sample_product.product_id, "content": sample_spec_content},
            tool_context=None,
        )
        spec_version_id = reg_result["spec_version_id"]

        approve_spec_version(
            {"spec_version_id": spec_version_id, "approved_by": "test_reviewer"},
            tool_context=None,
        )

        compile_spec_authority({"spec_version_id": spec_version_id}, tool_context=None)

        authority = session.exec(
            select(CompiledSpecAuthority).where(
                CompiledSpecAuthority.spec_version_id == spec_version_id
            )
        ).first()

        assert authority is not None
        assert authority.compiler_version == "2.0.0"  # Expected constant
        payload = json.loads(authority.compiled_artifact_json)
        assert payload["schema_version"] == "agileforge.compiled_authority.v2"
        assert len(authority.prompt_hash) == 64  # SHA-256 hex digest  # noqa: PLR2004


class TestAuthorityStatusCheck:
    """AC4 — Authority Status Check (Staleness)."""

    def test_status_tool_delegates_to_compiler_service(
        self, sample_product: Product, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tool entrypoint should remain a thin compatibility adapter."""
        expected = {
            "success": True,
            "status": SpecAuthorityStatus.CURRENT.value,
            "status_details": "delegated",
            "message": "Status: CURRENT (authority ID: 999)",
        }
        captured: dict[str, object] = {}

        def fake_service_status(params: object, tool_context: object = None) -> object:
            captured["params"] = params
            captured["tool_context"] = tool_context
            return expected

        monkeypatch.setattr(
            spec_tools,
            "_service_check_spec_authority_status",
            fake_service_status,
            raising=False,
        )

        result = check_spec_authority_status(
            {"product_id": sample_product.product_id},
            tool_context=None,
        )

        assert result is expected
        assert captured["params"] == {"product_id": sample_product.product_id}
        assert captured["tool_context"] is None

    def test_status_not_compiled_when_no_spec_exists(
        self, sample_product: Product
    ) -> None:
        """Test status when no spec version exists."""
        result = check_spec_authority_status(
            {"product_id": sample_product.product_id}, tool_context=None
        )

        assert result["success"] is True
        assert result["status"] == SpecAuthorityStatus.NOT_COMPILED.value

    def test_status_pending_review_when_latest_is_draft(
        self, session: Session, sample_product: Product, sample_spec_content: str
    ) -> None:
        """Test status when latest spec is draft."""
        del session
        register_spec_version(
            {"product_id": sample_product.product_id, "content": sample_spec_content},
            tool_context=None,
        )

        result = check_spec_authority_status(
            {"product_id": sample_product.product_id}, tool_context=None
        )

        assert result["success"] is True
        assert result["status"] == SpecAuthorityStatus.PENDING_REVIEW.value

    def test_status_not_compiled_when_approved_but_not_compiled(
        self, session: Session, sample_product: Product, sample_spec_content: str
    ) -> None:
        """Test status when spec is approved but not compiled."""
        del session
        reg_result = register_spec_version(
            {"product_id": sample_product.product_id, "content": sample_spec_content},
            tool_context=None,
        )
        spec_version_id = reg_result["spec_version_id"]

        approve_spec_version(
            {"spec_version_id": spec_version_id, "approved_by": "test_reviewer"},
            tool_context=None,
        )

        result = check_spec_authority_status(
            {"product_id": sample_product.product_id}, tool_context=None
        )

        assert result["success"] is True
        assert result["status"] == SpecAuthorityStatus.NOT_COMPILED.value

    def test_status_current_when_compiled_matches_latest_approved(
        self,
        session: Session,
        sample_product: Product,
        sample_spec_content: str,
        compiler_stub: object,
    ) -> None:
        """Test status when compiled authority matches latest approved spec."""
        del session, compiler_stub
        reg_result = register_spec_version(
            {"product_id": sample_product.product_id, "content": sample_spec_content},
            tool_context=None,
        )
        spec_version_id = reg_result["spec_version_id"]

        approve_spec_version(
            {"spec_version_id": spec_version_id, "approved_by": "test_reviewer"},
            tool_context=None,
        )

        compile_spec_authority({"spec_version_id": spec_version_id}, tool_context=None)

        result = check_spec_authority_status(
            {"product_id": sample_product.product_id}, tool_context=None
        )

        assert result["success"] is True
        assert result["status"] == SpecAuthorityStatus.CURRENT.value

    def test_status_stale_when_new_approved_spec_after_compilation(
        self, session: Session, sample_product: Product, compiler_stub: object
    ) -> None:
        """Test status transitions to STALE when spec changes after compilation."""
        # Version 1: register, approve, compile
        del session, compiler_stub
        v1_result = register_spec_version(
            {"product_id": sample_product.product_id, "content": "Version 1 content"},
            tool_context=None,
        )
        v1_id = v1_result["spec_version_id"]

        approve_spec_version(
            {"spec_version_id": v1_id, "approved_by": "test_reviewer"},
            tool_context=None,
        )

        compile_spec_authority({"spec_version_id": v1_id}, tool_context=None)

        # Verify CURRENT status
        status_after_v1 = check_spec_authority_status(
            {"product_id": sample_product.product_id}, tool_context=None
        )
        assert status_after_v1["status"] == SpecAuthorityStatus.CURRENT.value

        # Version 2: register and approve (but don't compile)
        v2_result = register_spec_version(
            {
                "product_id": sample_product.product_id,
                "content": "Version 2 content (changed)",
            },
            tool_context=None,
        )
        v2_id = v2_result["spec_version_id"]

        approve_spec_version(
            {"spec_version_id": v2_id, "approved_by": "test_reviewer"},
            tool_context=None,
        )

        # Status should now be STALE (compiled authority is for v1, latest approved is v2)  # noqa: E501
        status_after_v2 = check_spec_authority_status(
            {"product_id": sample_product.product_id}, tool_context=None
        )
        assert status_after_v2["status"] == SpecAuthorityStatus.STALE.value


class TestDeterministicRetrieval:
    """AC6 — Deterministic Retrieval by Version."""

    def test_get_compiled_authority_by_version_success(
        self,
        session: Session,
        sample_product: Product,
        sample_spec_content: str,
        compiler_stub: object,
    ) -> None:
        """Test successful retrieval of compiled authority."""
        del session, compiler_stub
        reg_result = register_spec_version(
            {"product_id": sample_product.product_id, "content": sample_spec_content},
            tool_context=None,
        )
        spec_version_id = reg_result["spec_version_id"]

        approve_spec_version(
            {"spec_version_id": spec_version_id, "approved_by": "test_reviewer"},
            tool_context=None,
        )

        compile_spec_authority({"spec_version_id": spec_version_id}, tool_context=None)

        # Retrieve by version
        result = get_compiled_authority_by_version(
            {
                "product_id": sample_product.product_id,
                "spec_version_id": spec_version_id,
            },
            tool_context=None,
        )

        assert result["success"] is True
        assert result["spec_version_id"] == spec_version_id
        assert "scope_themes" in result
        assert "invariants" in result
        assert isinstance(result["scope_themes"], list)
        assert isinstance(result["invariants"], list)

    def test_get_compiled_authority_fails_if_not_compiled(
        self, session: Session, sample_product: Product, sample_spec_content: str
    ) -> None:
        """Test clear error when compiled authority doesn't exist."""
        del session
        reg_result = register_spec_version(
            {"product_id": sample_product.product_id, "content": sample_spec_content},
            tool_context=None,
        )
        spec_version_id = reg_result["spec_version_id"]

        approve_spec_version(
            {"spec_version_id": spec_version_id, "approved_by": "test_reviewer"},
            tool_context=None,
        )
        # Note: NOT compiling

        result = get_compiled_authority_by_version(
            {
                "product_id": sample_product.product_id,
                "spec_version_id": spec_version_id,
            },
            tool_context=None,
        )

        assert result["success"] is False
        assert "not compiled" in result["error"].lower()

    def test_get_compiled_authority_fails_for_wrong_product(
        self,
        session: Session,
        sample_product: Product,
        sample_spec_content: str,
        compiler_stub: object,
    ) -> None:
        """Test that retrieval validates product_id matches spec_version_id."""
        del compiler_stub
        reg_result = register_spec_version(
            {"product_id": sample_product.product_id, "content": sample_spec_content},
            tool_context=None,
        )
        spec_version_id = reg_result["spec_version_id"]

        approve_spec_version(
            {"spec_version_id": spec_version_id, "approved_by": "test_reviewer"},
            tool_context=None,
        )

        compile_spec_authority({"spec_version_id": spec_version_id}, tool_context=None)

        # Create another product
        other_product = Product(name="Other Product", description="Test")
        session.add(other_product)
        session.commit()
        session.refresh(other_product)

        # Try to retrieve with wrong product_id
        result = get_compiled_authority_by_version(
            {
                "product_id": other_product.product_id,
                "spec_version_id": spec_version_id,
            },
            tool_context=None,
        )

        assert result["success"] is False
        assert (
            "mismatch" in result["error"].lower()
            or "not found" in result["error"].lower()
        )

    def test_get_compiled_authority_adapter_delegates_to_compiler_service(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Adapter path should delegate through the public compiler service boundary."""
        expected = {
            "success": True,
            "authority_id": 123,
            "message": "from service",
        }
        captured: dict[str, object] = {}

        def fake_service(params: object, tool_context: object = None) -> object:
            captured["params"] = params
            captured["tool_context"] = tool_context
            return expected

        monkeypatch.setattr(
            spec_tools,
            "_service_get_compiled_authority_by_version",
            fake_service,
            raising=False,
        )

        params = {"product_id": 9, "spec_version_id": 12}
        tool_context = make_tool_context()

        result = spec_tools.get_compiled_authority_by_version(
            params,
            tool_context=tool_context,
        )

        assert result is expected
        assert captured["params"] == params
        assert captured["tool_context"] is tool_context


class TestValidationRequiresSpecVersion:
    """AC5 — Story Validation Requires Explicit Spec Version (Contract)."""

    def test_user_story_can_store_accepted_spec_version_id(
        self, session: Session, sample_product: Product, sample_spec_content: str
    ) -> None:
        """Test that UserStory model can store accepted_spec_version_id."""
        # Create spec version
        reg_result = register_spec_version(
            {"product_id": sample_product.product_id, "content": sample_spec_content},
            tool_context=None,
        )
        spec_version_id = reg_result["spec_version_id"]

        # Create story with spec version
        story = UserStory(
            title="Test Story",
            story_description="As a user, I want to test",
            product_id=require_id(sample_product.product_id, "product_id"),
            accepted_spec_version_id=spec_version_id,
            validation_evidence=json.dumps(
                {
                    "validated_at": datetime.now(UTC).isoformat(),
                    "rules_checked": ["rule1", "rule2"],
                    "passed": True,
                }
            ),
        )
        session.add(story)
        session.commit()
        session.refresh(story)

        assert story.accepted_spec_version_id == spec_version_id
        assert story.validation_evidence is not None

        # Verify foreign key relationship
        retrieved_story = session.get(
            UserStory,
            require_id(story.story_id, "story_id"),
        )
        assert retrieved_story is not None
        assert retrieved_story.accepted_spec_version_id == spec_version_id

    # Note: Validation tool enforcement will be tested in integration tests
    # after validation tools are updated to require spec_version_id
