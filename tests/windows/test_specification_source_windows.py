"""Native Windows regressions for exact Specification source capture."""

from __future__ import annotations

import base64
import hashlib
import os
import sys
from typing import TYPE_CHECKING

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from adapters.git.repository_probe import GitPythonRepositoryProbe
from api import _workflow_actions
from cli.workflow_commands import render_workflow_next
from models.product_definition import SpecificationSource
from services.application import AgileForgeApplication, RepositoryRefreshRequest
from services.project_lifecycle import ProjectLifecycleService
from services.read_projections import DurableReadProjectionService
from services.specification_source_registration import (
    MAX_SPECIFICATION_SOURCE_DOCUMENT_BYTES,
    SpecificationSourceRegistrationError,
    SpecificationSourceRegistrationErrorCode,
    SpecificationSourceRegistrationService,
    _capture_selected_documents,
)
from services.vision_evidence_windows import _WindowsApi
from tests.services.test_specification_source_registration import (
    _git_repository,
    _request,
    _seed_lineage_and_binding,
)
from tests.windows.test_vision_evidence_windows import _junction
from workflow.clock import SystemClock
from workflow.definitions.root import project_graph
from workflow.domain import WorkflowDomain

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from services.contracts.specification_source import (
        SpecificationContextCapture,
        SpecificationSourceDocument,
    )
    from services.specification_source_registration import (
        PreparedSpecificationSourceRegistration,
        SpecificationSourceRegistrationRequest,
    )

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="requires native Windows handle semantics"
)
_ACCESS_DENIED = 5


@pytest.fixture
def source_engine(tmp_path: Path) -> Iterator[Engine]:
    """Use separate connections for nested capture reads during a write transaction."""
    engine = create_engine(f"sqlite:///{tmp_path / 'source-test.db'}")
    SQLModel.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def test_windows_capture_preserves_selected_source_context_and_adrs(
    tmp_path: Path,
) -> None:
    """Native capture must preserve every selected byte, including BOM and CRLF."""
    context = b"\xef\xbb\xbfContext\r\ntrailing spaces  "
    source = b"\xef\xbb\xbfSpecification\r\nlast line  "
    repository = _git_repository(tmp_path, context=context)
    (repository / "specification.md").write_bytes(source)

    captured, captured_context, adrs = _capture_selected_documents(
        worktree_path=str(repository),
        source_path="specification.md",
        adr_paths=("docs/adr/0001-first.md", "docs/adr/0002-second.md"),
    )

    assert base64.b64decode(captured.content_base64) == source
    assert (
        captured.content_fingerprint == "sha256:" + hashlib.sha256(source).hexdigest()
    )
    assert captured_context.document is not None
    assert base64.b64decode(captured_context.document.content_base64) == context
    assert [base64.b64decode(item.content_base64) for item in adrs] == [
        b"First ADR\n",
        b"Second ADR\n",
    ]


def _capture(
    root: Path, source_path: str = "specification.md"
) -> tuple[
    SpecificationSourceDocument,
    SpecificationContextCapture,
    tuple[SpecificationSourceDocument, ...],
]:
    return _capture_selected_documents(
        worktree_path=str(root),
        source_path=source_path,
        adr_paths=("docs/adr/0001-first.md",),
    )


@pytest.mark.parametrize("path", ["specification.md", "docs/adr/0001-first.md"])
def test_windows_capture_requires_each_selected_document(
    tmp_path: Path, path: str
) -> None:
    """A missing selected source or ADR cannot become optional evidence."""
    root = _git_repository(tmp_path)
    (root / path).unlink()
    with pytest.raises(SpecificationSourceRegistrationError) as caught:
        _capture(root)
    assert caught.value.code is SpecificationSourceRegistrationErrorCode.SOURCE_MISSING


@pytest.mark.parametrize("context_kind", ["directory", "alias", "invalid_utf8"])
def test_windows_capture_rejects_unsafe_optional_context(
    tmp_path: Path, context_kind: str
) -> None:
    """Only actual CONTEXT absence is optional; invalid present input must fail."""
    root = _git_repository(tmp_path)
    if context_kind == "directory":
        (root / "CONTEXT.md").mkdir()
    elif context_kind == "alias":
        (root / "context.md").write_bytes(b"case alias")
    else:
        (root / "CONTEXT.md").write_bytes(b"invalid \xff")
    with pytest.raises(SpecificationSourceRegistrationError) as caught:
        _capture(root)
    expected = (
        SpecificationSourceRegistrationErrorCode.INVALID_UTF8
        if context_kind == "invalid_utf8"
        else SpecificationSourceRegistrationErrorCode.UNSAFE_FILE
    )
    assert caught.value.code is expected


@pytest.mark.parametrize(
    "path",
    [
        "specification.md:stream",
        "specification.md::$DATA",
        "specification.md.",
        "specification.md ",
        "NUL.md",
        "C:specification.md",
        "../specification.md",
        "docs\\adr\\0001-first.md",
        "DOCS/adr/0001-first.md",
    ],
)
def test_windows_capture_rejects_alternate_paths(tmp_path: Path, path: str) -> None:
    """Streams, devices, traversal, and spelling aliases never become sources."""
    root = _git_repository(tmp_path)
    (root / "specification.md:stream").write_bytes(b"alternate stream")
    with pytest.raises(SpecificationSourceRegistrationError) as caught:
        _capture(root, path)
    assert caught.value.code is SpecificationSourceRegistrationErrorCode.UNSAFE_FILE


@pytest.mark.parametrize("external", [False, True])
def test_windows_capture_rejects_all_directory_junctions(
    tmp_path: Path, external: bool
) -> None:
    """Specification must reject even internal reparses accepted by Vision."""
    root = _git_repository(tmp_path)
    target = tmp_path / "external" if external else root / "internal"
    target.mkdir()
    (target / "source.md").write_bytes(b"reparse target")
    _junction(root / "selected", target)
    with pytest.raises(SpecificationSourceRegistrationError) as caught:
        _capture(root, "selected/source.md")
    assert caught.value.code is SpecificationSourceRegistrationErrorCode.UNSAFE_FILE


def test_windows_capture_rejects_reparse_root(tmp_path: Path) -> None:
    """A final root junction cannot silently change the trusted anchor."""
    root = _git_repository(tmp_path)
    alias = tmp_path / "root-alias"
    _junction(alias, root)
    with pytest.raises(SpecificationSourceRegistrationError) as caught:
        _capture(alias)
    assert (
        caught.value.code
        is SpecificationSourceRegistrationErrorCode.CAPABILITY_UNAVAILABLE
    )


def test_windows_capture_rejects_context_hardlink_duplicate(tmp_path: Path) -> None:
    """Native volume/file IDs prevent a second role for the same physical file."""
    root = _git_repository(tmp_path)
    os.link(root / "specification.md", root / "CONTEXT.md")
    with pytest.raises(SpecificationSourceRegistrationError) as caught:
        _capture(root)
    assert (
        caught.value.code is SpecificationSourceRegistrationErrorCode.DUPLICATE_SOURCE
    )


def test_windows_capture_rejects_short_filename_alias(tmp_path: Path) -> None:
    """An enabled 8.3 alias must not change canonical source selection."""
    root = _git_repository(tmp_path)
    alias = "SPECIF~1.MD"
    if not (root / alias).exists():
        raise pytest.skip.Exception(msg="8.3 aliases are disabled on this volume")
    with pytest.raises(SpecificationSourceRegistrationError) as caught:
        _capture(root, alias)
    assert caught.value.code is SpecificationSourceRegistrationErrorCode.UNSAFE_FILE


def test_windows_capture_enforces_aggregate_limit(tmp_path: Path) -> None:
    """Individually legal documents cannot exceed the combined byte limit."""
    content = b"x" * MAX_SPECIFICATION_SOURCE_DOCUMENT_BYTES
    root = _git_repository(tmp_path, context=content)
    for path in (
        "specification.md",
        "docs/adr/0001-first.md",
        "docs/adr/0002-second.md",
    ):
        (root / path).write_bytes(content)
    with pytest.raises(SpecificationSourceRegistrationError) as caught:
        _capture_selected_documents(
            worktree_path=str(root),
            source_path="specification.md",
            adr_paths=("docs/adr/0001-first.md", "docs/adr/0002-second.md"),
        )
    assert (
        caught.value.code is SpecificationSourceRegistrationErrorCode.SOURCE_TOO_LARGE
    )


@pytest.mark.parametrize(
    "path", ["specification.md", "CONTEXT.md", "docs/adr/0001-first.md"]
)
def test_windows_capture_rejects_content_changed_during_native_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Each role detects mutation after its bytes are read from the real handle."""
    root = _git_repository(tmp_path, context=b"Context\n")
    original_read = _WindowsApi.read

    def read_then_change(api: _WindowsApi, handle: int, limit: int) -> bytes:
        content = original_read(api, handle, limit)
        if api.final_path(handle).endswith(path.replace("/", "\\")):
            (root / path).write_bytes(b"Changed after native read\n")
        return content

    monkeypatch.setattr(_WindowsApi, "read", read_then_change)
    with pytest.raises(SpecificationSourceRegistrationError) as caught:
        _capture(root)
    assert (
        caught.value.code
        is SpecificationSourceRegistrationErrorCode.SOURCE_CHANGED_DURING_CAPTURE
    )


@pytest.mark.parametrize("replacement", ["root", "directory", "file"])
def test_windows_capture_detects_or_prevents_replacement_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    """Retained handles must detect replacement or prevent the replacement itself."""
    root = _git_repository(tmp_path)
    selected = root / "docs" / "source.md"
    selected.write_bytes(b"Nested source\n")
    original_read = _WindowsApi.read
    prevented = False
    replaced = False

    def read_then_replace(api: _WindowsApi, handle: int, limit: int) -> bytes:
        nonlocal prevented, replaced
        content = original_read(api, handle, limit)
        if replaced or prevented:
            return content
        target = {"root": root, "directory": selected.parent, "file": selected}[
            replacement
        ]
        moved = target.with_name(target.name + "-retained")
        try:
            target.rename(moved)
        except PermissionError as error:
            if getattr(error, "winerror", None) != _ACCESS_DENIED:
                raise
            prevented = True
            return content
        replaced = True
        if replacement == "file":
            target.write_bytes(b"Replacement file\n")
        else:
            target.mkdir()
        return content

    monkeypatch.setattr(_WindowsApi, "read", read_then_replace)
    try:
        _capture(root, "docs/source.md")
    except SpecificationSourceRegistrationError:
        assert replaced or prevented
    else:
        assert prevented


def _application(
    engine: Engine,
) -> tuple[AgileForgeApplication, SpecificationSourceRegistrationService]:
    probe = GitPythonRepositoryProbe()
    registration = SpecificationSourceRegistrationService(engine, probe)
    domain = WorkflowDomain(
        engine=engine,
        graph=project_graph(),
        clock=SystemClock(),
        specification_registration_check=registration.verify_prepared,
    )
    application = AgileForgeApplication(
        workflow_domain=domain,
        read_projection=DurableReadProjectionService(engine=engine),
        specification_source_registration=registration,
    )
    application.set_project_lifecycle(
        ProjectLifecycleService(
            engine=engine, workflow_domain=domain, repository_probe=probe
        )
    )
    return application, registration


def test_windows_registration_succeeds_after_explicit_dirty_binding_refresh(
    source_engine: Engine, tmp_path: Path
) -> None:
    """The shared application persists exact dirty docs only after explicit refresh."""
    root = _git_repository(tmp_path, context=b"Context\r\n")
    _seed_lineage_and_binding(source_engine, root)
    application, registration = _application(source_engine)
    dirty_source = b"Intentional uncommitted Specification\r\n"
    (root / "specification.md").write_bytes(dirty_source)
    stale = application.register_specification_source(_request())
    assert stale.ok is False
    assert stale.error is not None
    assert "provenance differs" in stale.error.message

    refreshed = application.refresh_repository(
        RepositoryRefreshRequest(
            project_id=1,
            idempotency_key="explicit-dirty-refresh",
            actor="operator@example.test",
        )
    )
    assert refreshed.ok is True
    registered = application.register_specification_source(_request())
    assert registered.ok is True
    assert registered.applied_node_id == "specification.source.register"
    with Session(source_engine) as session:
        assert len(session.exec(select(SpecificationSource)).all()) == 1
    prepared = registration.prepare(_request())
    assert prepared.bundle.repository_revision.dirty is True
    assert base64.b64decode(prepared.bundle.source.content_base64) == dirty_source
    assert registration.verify_prepared(prepared) is None
    (root / "specification.md").write_bytes(b"Changed while still dirty\r\n")
    changed = registration.verify_prepared(prepared)
    assert changed is not None
    assert (
        changed.code
        is SpecificationSourceRegistrationErrorCode.SOURCE_CHANGED_DURING_CAPTURE
    )


def test_windows_registration_revalidates_before_database_write(
    source_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changed source bytes between preparation and persistence leave no source row."""
    root = _git_repository(tmp_path)
    (root / "specification.md").write_bytes(b"Already dirty before binding\n")
    _seed_lineage_and_binding(source_engine, root)
    application, _ = _application(source_engine)
    original_prepare = SpecificationSourceRegistrationService.prepare
    prepared_once = False

    def prepare_then_change(
        service: SpecificationSourceRegistrationService,
        request: SpecificationSourceRegistrationRequest,
    ) -> PreparedSpecificationSourceRegistration:
        nonlocal prepared_once
        prepared = original_prepare(service, request)
        if not prepared_once:
            prepared_once = True
            (root / "specification.md").write_bytes(b"Changed before database write\n")
        return prepared

    monkeypatch.setattr(
        SpecificationSourceRegistrationService, "prepare", prepare_then_change
    )
    result = application.register_specification_source(_request())
    assert result.ok is False
    assert result.error is not None
    assert "changed before persistence" in result.error.message
    with Session(source_engine) as session:
        assert session.exec(select(SpecificationSource)).all() == []


def test_windows_registration_reports_unsupported_filesystem(
    source_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing filesystem capability must not be reported as stale source input."""
    root = _git_repository(tmp_path)
    _seed_lineage_and_binding(source_engine, root)
    application, _ = _application(source_engine)
    monkeypatch.setattr(_WindowsApi, "filesystem_name", lambda _api, _handle: "FAT32")
    result = application.register_specification_source(_request())
    assert result.ok is False
    assert result.error is not None
    assert result.error.code.value == "REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE"


def test_windows_registration_withholds_unsupported_api_and_cli_actions(
    source_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two transport projections expose the same non-executable capability."""
    root = _git_repository(tmp_path)
    _seed_lineage_and_binding(source_engine, root)
    application, _ = _application(source_engine)
    monkeypatch.setattr(_WindowsApi, "filesystem_name", lambda _api, _handle: "FAT32")
    position = application.position(project_id=1)
    cli = render_workflow_next(position, application=application)
    assert cli["commands"] == []
    assert cli["blocked_commands"][0]["reason_code"] == (
        "REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE"
    )
    actions = _workflow_actions(position, application=application)
    action = next(
        a for a in actions if a["request_kind"] == "register_specification_source"
    )
    assert action["availability"] == "locked"
    assert action["reason_code"] == "REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE"
