"""Tests for specs lifecycle service."""

import hashlib
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from agile_sqlmodel import Project, SpecRegistry
from tests.typing_helpers import make_tool_context


def test_link_spec_to_product_persists_link_and_delegates_compile(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify link spec to product persists link and delegates compile."""
    from services.specs import lifecycle_service  # noqa: PLC0415

    product = Project(name="Lifecycle Project", vision="vision")
    session.add(product)
    session.commit()
    session.refresh(product)

    spec_path = tmp_path / "linked_spec.md"
    spec_path.write_text("# Linked spec\n", encoding="utf-8")

    calls: dict[str, object] = {}

    def fake_compile(
        *, project_id: int, spec_path: str, tool_context: object
    ) -> object:
        calls["project_id"] = project_id
        calls["spec_path"] = spec_path
        calls["tool_context"] = tool_context
        return {
            "success": True,
            "spec_version_id": 12,
            "authority_id": 34,
        }

    monkeypatch.setattr(
        lifecycle_service,
        "_compile_linked_spec_authority",
        fake_compile,
    )

    ctx = make_tool_context(state={"spec_persisted": False})
    result = lifecycle_service.link_spec_to_product(
        {
            "project_id": product.project_id,
            "spec_path": str(spec_path),
        },
        tool_context=ctx,
    )

    assert result["success"] is True
    assert result["compile_success"] is True
    assert result["authority_id"] == 34  # noqa: PLR2004
    assert calls["project_id"] == product.project_id
    assert calls["spec_path"] == str(spec_path)
    assert calls["tool_context"] is ctx
    assert ctx.state["spec_persisted"] is True

    session.expire_all()
    stored = session.get(Project, product.project_id)
    assert stored is not None
    assert stored.spec_file_path == str(spec_path)
    assert stored.spec_loaded_at is not None
    assert stored.technical_spec is None


def test_save_project_specification_from_file_persists_content(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify save project specification from file persists content."""
    from services.specs import lifecycle_service  # noqa: PLC0415

    product = Project(name="Save File Project", vision="vision")
    session.add(product)
    session.commit()
    session.refresh(product)

    spec_path = tmp_path / "save_file_spec.md"
    spec_path.write_text("# File Spec\n\nBody", encoding="utf-8")

    calls: dict[str, object] = {}

    def fake_compile(
        *, project_id: int, spec_path: str, tool_context: object
    ) -> object:
        calls["project_id"] = project_id
        calls["spec_path"] = spec_path
        calls["tool_context"] = tool_context
        return {
            "success": True,
            "spec_version_id": 21,
            "authority_id": 22,
        }

    monkeypatch.setattr(
        lifecycle_service,
        "_compile_spec_authority_from_path",
        fake_compile,
        raising=False,
    )

    ctx = make_tool_context(state={"spec_persisted": False})
    result = lifecycle_service.save_project_specification(
        {
            "project_id": product.project_id,
            "spec_source": "file",
            "content": str(spec_path),
        },
        tool_context=ctx,
    )

    assert result["success"] is True
    assert result["file_created"] is False
    assert result["compile_success"] is True
    assert result["authority_id"] == 22  # noqa: PLR2004
    assert calls["project_id"] == product.project_id
    assert calls["spec_path"] == str(spec_path)
    assert ctx.state["spec_persisted"] is True

    session.expire_all()
    stored = session.get(Project, product.project_id)
    assert stored is not None
    assert stored.technical_spec == "# File Spec\n\nBody"
    assert stored.spec_file_path == str(spec_path)
    assert stored.spec_loaded_at is not None


def test_save_project_specification_from_text_creates_backup_file(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify save project specification from text creates backup file."""
    from services.specs import lifecycle_service  # noqa: PLC0415

    product = Project(name="Save Text Project", vision="vision")
    session.add(product)
    session.commit()
    session.refresh(product)

    calls: dict[str, object] = {}

    def fake_compile(
        *, project_id: int, spec_path: str, tool_context: object
    ) -> object:
        calls["project_id"] = project_id
        calls["spec_path"] = spec_path
        calls["tool_context"] = tool_context
        return {
            "success": True,
            "spec_version_id": 31,
            "authority_id": 32,
        }

    monkeypatch.setattr(
        lifecycle_service,
        "_compile_spec_authority_from_path",
        fake_compile,
        raising=False,
    )

    monkeypatch.chdir(tmp_path)
    pasted_spec = "# Text Spec\n\nBody"
    ctx = make_tool_context(state={"spec_persisted": False})

    result = lifecycle_service.save_project_specification(
        {
            "project_id": product.project_id,
            "spec_source": "text",
            "content": pasted_spec,
        },
        tool_context=ctx,
    )

    assert result["success"] is True
    assert result["file_created"] is True
    assert result["compile_success"] is True
    assert result["authority_id"] == 32  # noqa: PLR2004
    assert "specs" in result["spec_path"]
    assert calls["project_id"] == product.project_id
    assert calls["spec_path"] == result["spec_path"]
    assert ctx.state["spec_persisted"] is True

    created_file = Path(result["spec_path"])
    assert created_file.exists()
    assert created_file.read_text(encoding="utf-8") == pasted_spec

    session.expire_all()
    stored = session.get(Project, product.project_id)
    assert stored is not None
    assert stored.technical_spec == pasted_spec
    assert stored.spec_file_path == result["spec_path"]
    assert stored.spec_loaded_at is not None


def test_link_spec_to_product_rejects_missing_file(session: Session) -> None:
    """Verify link spec to product rejects missing file."""
    from services.specs import lifecycle_service  # noqa: PLC0415

    product = Project(name="Missing File Project", vision="vision")
    session.add(product)
    session.commit()
    session.refresh(product)

    result = lifecycle_service.link_spec_to_product(
        {
            "project_id": product.project_id,
            "spec_path": "missing/spec.md",
        }
    )

    assert result["success"] is False
    assert "not found" in result["error"].lower()


def test_link_spec_to_product_handles_compile_failure_after_link(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify link spec to product handles compile failure after link."""
    from services.specs import lifecycle_service  # noqa: PLC0415

    product = Project(
        name="Compile Failure Project",
        vision="vision",
        spec_file_path=None,
        spec_loaded_at=None,
    )
    session.add(product)
    session.commit()
    session.refresh(product)

    spec_path = tmp_path / "compile_failure.md"
    spec_path.write_text("# Linked spec\n", encoding="utf-8")

    def fake_compile(
        *, project_id: int, spec_path: str, tool_context: object
    ) -> object:
        del project_id, spec_path, tool_context
        return {
            "success": False,
            "error": "Compilation error",
            "failure_artifact_id": "artifact-1",
            "failure_stage": "output_validation",
            "failure_summary": "Compiler failed",
            "raw_output_preview": '{"bad":true}',
            "has_full_artifact": True,
        }

    monkeypatch.setattr(
        lifecycle_service,
        "_compile_linked_spec_authority",
        fake_compile,
    )

    result = lifecycle_service.link_spec_to_product(
        {
            "project_id": product.project_id,
            "spec_path": str(spec_path),
        }
    )

    assert result["success"] is True
    assert result["compile_success"] is False
    assert result["compile_error"] == "Compilation error"
    assert result["failure_artifact_id"] == "artifact-1"
    assert result["has_full_artifact"] is True

    session.expire_all()
    stored = session.get(Project, product.project_id)
    assert stored is not None
    assert stored.spec_file_path == str(spec_path)
    assert stored.spec_loaded_at is not None


def test_read_project_specification_prefers_db_blob_and_updates_context_state(
    session: Session, tmp_path: Path
) -> None:
    """Verify read project specification prefers db blob and updates context state."""
    from services.specs import lifecycle_service  # noqa: PLC0415

    spec_path = tmp_path / "backing_spec.md"
    spec_path.write_text("# File Spec\n\nFile body", encoding="utf-8")

    product = Project(
        name="Read Project",
        vision="vision",
        technical_spec="# DB Spec\n\nDatabase body",
        spec_file_path=str(spec_path),
    )
    session.add(product)
    session.commit()
    session.refresh(product)

    ctx = make_tool_context(
        state={
            "active_project": {
                "project_id": product.project_id,
                "name": product.name,
            }
        }
    )

    result = lifecycle_service.read_project_specification({}, ctx)

    assert result["success"] is True
    assert result["spec_content"] == "# DB Spec\n\nDatabase body"
    assert result["spec_path"] == str(spec_path)
    assert ctx.state["pending_spec_content"] == "# DB Spec\n\nDatabase body"
    assert ctx.state["pending_spec_path"] == str(spec_path)
    assert "DB Spec" in result["sections"][0]


def test_read_project_specification_falls_back_to_file_when_db_blob_is_empty(
    session: Session, tmp_path: Path
) -> None:
    """Verify read project specification falls back to file when db blob is empty."""
    from services.specs import lifecycle_service  # noqa: PLC0415

    spec_path = tmp_path / "empty_db_fallback.md"
    spec_path.write_text("# File Spec\n\nFile body", encoding="utf-8")

    product = Project(
        name="Fallback Project",
        vision="vision",
        technical_spec="",
        spec_file_path=str(spec_path),
    )
    session.add(product)
    session.commit()
    session.refresh(product)

    ctx = make_tool_context(
        state={
            "active_project": {
                "project_id": product.project_id,
                "name": product.name,
            }
        }
    )

    result = lifecycle_service.read_project_specification({}, ctx)

    assert result["success"] is True
    assert result["spec_content"] == "# File Spec\n\nFile body"
    assert result["spec_path"] == str(spec_path)
    assert ctx.state["pending_spec_content"] == "# File Spec\n\nFile body"
    assert ctx.state["pending_spec_path"] == str(spec_path)


def test_read_project_specification_requires_active_project_context() -> None:
    """Verify read project specification requires active project context."""
    from services.specs import lifecycle_service  # noqa: PLC0415

    ctx = make_tool_context(state={})

    result = lifecycle_service.read_project_specification({}, ctx)

    assert result["success"] is False
    assert "active project" in result["error"].lower()


def test_register_spec_version_creates_draft_from_service_boundary(
    session: Session,
) -> None:
    """Verify register spec version creates draft from service boundary."""
    from services.specs import lifecycle_service  # noqa: PLC0415

    product = Project(name="Service Register Project", vision="vision")
    session.add(product)
    session.commit()
    session.refresh(product)

    content = "# Service Spec\n\nBody"
    result = lifecycle_service.register_spec_version(
        {
            "project_id": product.project_id,
            "content": content,
            "content_ref": "specs/service.md",
        },
        tool_context=None,
    )

    assert result["success"] is True
    assert result["status"] == "draft"
    assert result["message"] == (
        f"Registered spec version {result['spec_version_id']} (status: draft)"
    )

    spec = session.get(SpecRegistry, result["spec_version_id"])
    assert spec is not None
    assert spec.project_id == product.project_id
    assert spec.status == "draft"
    assert spec.spec_hash == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert spec.content_ref == "specs/service.md"
    assert spec.approved_at is None
    assert spec.approved_by is None


def test_approve_spec_version_updates_metadata_from_service_boundary(
    session: Session,
) -> None:
    """Verify approve spec version updates metadata from service boundary."""
    from services.specs import lifecycle_service  # noqa: PLC0415

    product = Project(name="Service Approve Project", vision="vision")
    session.add(product)
    session.commit()
    session.refresh(product)

    register_result = lifecycle_service.register_spec_version(
        {
            "project_id": product.project_id,
            "content": "# Service Spec\n\nBody",
        },
        tool_context=None,
    )
    spec_version_id = register_result["spec_version_id"]

    approve_result = lifecycle_service.approve_spec_version(
        {
            "spec_version_id": spec_version_id,
            "approved_by": "service.reviewer@example.com",
            "approval_notes": "Approved via lifecycle boundary",
        },
        tool_context=None,
    )

    assert approve_result["success"] is True
    assert approve_result["spec_version_id"] == spec_version_id
    assert approve_result["approved_by"] == "service.reviewer@example.com"
    assert approve_result["message"] == (
        f"Spec version {spec_version_id} approved by service.reviewer@example.com"
    )

    spec = session.get(SpecRegistry, spec_version_id)
    assert spec is not None
    assert spec.status == "approved"
    assert spec.approved_by == "service.reviewer@example.com"
    assert spec.approval_notes == "Approved via lifecycle boundary"
    assert spec.approved_at is not None


def test_register_spec_version_honors_legacy_spec_tools_engine_override(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify register spec version honors legacy spec tools engine override."""
    from services.specs import lifecycle_service  # noqa: PLC0415
    from tools import spec_tools  # noqa: PLC0415

    isolated_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(isolated_engine)

    # Keep get_engine on the default path so explicit spec_tools.engine override wins.
    monkeypatch.setattr(spec_tools, "get_engine", lifecycle_service.get_engine)

    previous_engine: Engine = spec_tools.engine
    spec_tools.engine = isolated_engine
    try:
        with Session(isolated_engine) as isolated_session:
            product = Project(name="Engine Seam Project", vision="vision")
            isolated_session.add(product)
            isolated_session.commit()
            isolated_session.refresh(product)
            project_id = product.project_id

        result = lifecycle_service.register_spec_version(
            {
                "project_id": project_id,
                "content": "# isolated",
            },
            tool_context=None,
        )

        assert result["success"] is True
        with Session(isolated_engine) as isolated_session:
            spec = isolated_session.get(SpecRegistry, result["spec_version_id"])
            assert spec is not None
            assert spec.project_id == project_id

        with Session(engine) as default_session:
            assert default_session.get(SpecRegistry, result["spec_version_id"]) is None
    finally:
        spec_tools.engine = previous_engine
        SQLModel.metadata.drop_all(isolated_engine)


def test_resolve_engine_prefers_spec_tools_get_engine_override_over_stale_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify resolve engine prefers spec tools get engine override over stale engine."""  # noqa: E501
    from services.specs import lifecycle_service  # noqa: PLC0415
    from tools import spec_tools  # noqa: PLC0415

    preferred_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    stale_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(preferred_engine)
    SQLModel.metadata.create_all(stale_engine)

    monkeypatch.setattr(spec_tools, "get_engine", lambda: preferred_engine)
    monkeypatch.setattr(spec_tools, "engine", stale_engine, raising=False)

    resolved = lifecycle_service._resolve_engine()

    assert resolved is preferred_engine

    SQLModel.metadata.drop_all(preferred_engine)
    SQLModel.metadata.drop_all(stale_engine)


def test_register_and_approve_prefer_spec_tools_get_engine_override_over_stale_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify register and approve prefer spec tools get engine override over stale engine."""  # noqa: E501
    from services.specs import lifecycle_service  # noqa: PLC0415
    from tools import spec_tools  # noqa: PLC0415

    preferred_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    stale_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(preferred_engine)
    SQLModel.metadata.create_all(stale_engine)

    monkeypatch.setattr(spec_tools, "get_engine", lambda: preferred_engine)
    monkeypatch.setattr(spec_tools, "engine", stale_engine, raising=False)

    with Session(preferred_engine) as preferred_session:
        product = Project(name="Preferred Engine Project", vision="vision")
        preferred_session.add(product)
        preferred_session.commit()
        preferred_session.refresh(product)
        project_id = product.project_id

    register_result = lifecycle_service.register_spec_version(
        {
            "project_id": project_id,
            "content": "# preferred",
        },
        tool_context=None,
    )
    assert register_result["success"] is True
    spec_version_id = register_result["spec_version_id"]

    approve_result = lifecycle_service.approve_spec_version(
        {
            "spec_version_id": spec_version_id,
            "approved_by": "preferred.engine@test",
        },
        tool_context=None,
    )
    assert approve_result["success"] is True

    with Session(preferred_engine) as preferred_session:
        spec = preferred_session.get(SpecRegistry, spec_version_id)
        assert spec is not None
        assert spec.status == "approved"

    with Session(stale_engine) as stale_session:
        assert stale_session.get(SpecRegistry, spec_version_id) is None


def test_tool_lifecycle_input_models_alias_service_models() -> None:
    """Verify tool lifecycle input models alias service models."""
    from services.specs.lifecycle_service import (  # noqa: PLC0415
        ApproveSpecVersionInput as ServiceApproveSpecVersionInput,
    )
    from services.specs.lifecycle_service import (  # noqa: PLC0415
        LinkSpecToProductInput as ServiceLinkSpecToProductInput,
    )
    from services.specs.lifecycle_service import (  # noqa: PLC0415
        ReadProjectSpecificationInput as ServiceReadProjectSpecificationInput,
    )
    from services.specs.lifecycle_service import (  # noqa: PLC0415
        RegisterSpecVersionInput as ServiceRegisterSpecVersionInput,
    )
    from services.specs.lifecycle_service import (  # noqa: PLC0415
        SaveProjectSpecificationInput as ServiceSaveProjectSpecificationInput,
    )
    from tools.spec_tools import (  # noqa: PLC0415
        ApproveSpecVersionInput,
        LinkSpecToProductInput,
        ReadProjectSpecificationInput,
        RegisterSpecVersionInput,
        SaveProjectSpecificationInput,
    )

    assert SaveProjectSpecificationInput is ServiceSaveProjectSpecificationInput
    assert LinkSpecToProductInput is ServiceLinkSpecToProductInput
    assert ReadProjectSpecificationInput is ServiceReadProjectSpecificationInput
    assert RegisterSpecVersionInput is ServiceRegisterSpecVersionInput
    assert ApproveSpecVersionInput is ServiceApproveSpecVersionInput


def test_save_project_specification_honors_legacy_spec_tools_engine_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify save project specification honors legacy spec tools engine override."""
    from services.specs import lifecycle_service  # noqa: PLC0415
    from tools import spec_tools  # noqa: PLC0415

    isolated_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(isolated_engine)

    monkeypatch.setattr(spec_tools, "get_engine", lifecycle_service.get_engine)
    previous_engine: Engine = spec_tools.engine
    spec_tools.engine = isolated_engine

    spec_path = tmp_path / "isolated_save.md"
    spec_path.write_text("# Save\n\nBody", encoding="utf-8")

    monkeypatch.setattr(
        lifecycle_service,
        "_compile_spec_authority_from_path",
        lambda **_: {"success": True, "spec_version_id": 1, "authority_id": 2},
        raising=False,
    )

    try:
        with Session(isolated_engine) as isolated_session:
            product = Project(name="Isolated Save Project", vision="vision")
            isolated_session.add(product)
            isolated_session.commit()
            isolated_session.refresh(product)
            project_id = product.project_id

        result = lifecycle_service.save_project_specification(
            {
                "project_id": project_id,
                "spec_source": "file",
                "content": str(spec_path),
            },
            tool_context=None,
        )

        assert result["success"] is True
        with Session(isolated_engine) as isolated_session:
            product = isolated_session.get(Project, project_id)
            assert product is not None
            assert product.spec_file_path == str(spec_path)
            assert product.technical_spec == "# Save\n\nBody"
    finally:
        spec_tools.engine = previous_engine
        SQLModel.metadata.drop_all(isolated_engine)


def test_link_and_read_specification_honor_legacy_spec_tools_engine_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify link and read specification honor legacy spec tools engine override."""
    from services.specs import lifecycle_service  # noqa: PLC0415
    from tools import spec_tools  # noqa: PLC0415

    isolated_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(isolated_engine)

    monkeypatch.setattr(spec_tools, "get_engine", lifecycle_service.get_engine)
    previous_engine: Engine = spec_tools.engine
    spec_tools.engine = isolated_engine

    spec_path = tmp_path / "isolated_link.md"
    spec_path.write_text("# Linked\n\nBody", encoding="utf-8")

    monkeypatch.setattr(
        lifecycle_service,
        "_compile_linked_spec_authority",
        lambda **_: {"success": True, "spec_version_id": 1, "authority_id": 2},
        raising=False,
    )

    try:
        with Session(isolated_engine) as isolated_session:
            product = Project(name="Isolated Link Project", vision="vision")
            isolated_session.add(product)
            isolated_session.commit()
            isolated_session.refresh(product)
            project_id = product.project_id

        link_result = lifecycle_service.link_spec_to_product(
            {
                "project_id": project_id,
                "spec_path": str(spec_path),
            },
            tool_context=None,
        )

        assert link_result["success"] is True

        ctx = make_tool_context(
            state={
                "active_project": {
                    "project_id": project_id,
                    "name": "Isolated Link Project",
                }
            }
        )
        read_result = lifecycle_service.read_project_specification(
            {},
            tool_context=ctx,
        )

        assert read_result["success"] is True
        assert read_result["spec_path"] == str(spec_path)
        assert "# Linked\n\nBody" in read_result["spec_content"]
    finally:
        spec_tools.engine = previous_engine
        SQLModel.metadata.drop_all(isolated_engine)
