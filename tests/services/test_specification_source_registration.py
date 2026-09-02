"""Exact-byte host capture for registered Specification sources."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from git import Repo
from pydantic import ValidationError
from sqlmodel import Session

from adapters.git.repository_probe import GitPythonRepositoryProbe
from models.core import Project
from models.repository import RepositoryBinding
from services.contracts.specification_source import source_bundle_fingerprint
from services.repository_probe import (
    RepositoryProbeError,
    RepositoryProbeErrorCode,
)
from services.specification_source_registration import (
    MAX_SPECIFICATION_SOURCE_DOCUMENT_BYTES,
    SpecificationSourceRegistrationError,
    SpecificationSourceRegistrationErrorCode,
    SpecificationSourceRegistrationRequest,
    SpecificationSourceRegistrationService,
    _required_open_flags,
)
from tests.workflow.lifecycle_fixtures import _seed_accepted_vision_and_goal
from workflow.fingerprints import canonical_json

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from services.repository_probe import RepositoryProbeResult

_EXPECTED_PROBE_CALLS = 3
_MIDDLE_PROBE_CALL = 2
_WINDOWS_PRIVILEGE_NOT_HELD = 1314


def test_source_capability_reports_missing_posix_open_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unsupported host must report capability rather than unsafe source bytes."""
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    with pytest.raises(SpecificationSourceRegistrationError) as caught:
        _required_open_flags(directory=True)
    assert (
        caught.value.code
        is SpecificationSourceRegistrationErrorCode.CAPABILITY_UNAVAILABLE
    )


class _CountingProbe:
    """Count production probe calls while retaining its real behavior."""

    def __init__(self) -> None:
        self.calls = 0
        self._delegate = GitPythonRepositoryProbe()

    def inspect(self, path: Path | str) -> RepositoryProbeResult:
        self.calls += 1
        return self._delegate.inspect(path)


class _RaisingProbe:
    """Raise one closed repository error on a selected probe call."""

    def __init__(
        self,
        *,
        fail_on_call: int,
        delegate: GitPythonRepositoryProbe,
    ) -> None:
        self.calls = 0
        self._fail_on_call = fail_on_call
        self._delegate = delegate

    def inspect(self, path: Path | str) -> RepositoryProbeResult:
        self.calls += 1
        if self.calls == self._fail_on_call:
            raise RepositoryProbeError(
                RepositoryProbeErrorCode.REPOSITORY_CHANGED_DURING_PROBE,
                str(path),
            )
        return self._delegate.inspect(path)


def _git_repository(tmp_path: Path, *, context: bytes | None = None) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    (root / "docs" / "adr").mkdir(parents=True)
    (root / "specification.md").write_bytes(b"Initial source\n")
    (root / "docs" / "adr" / "0002-second.md").write_bytes(b"Second ADR\n")
    (root / "docs" / "adr" / "0001-first.md").write_bytes(b"First ADR\n")
    if context is not None:
        (root / "CONTEXT.md").write_bytes(context)
    with Repo.init(root) as repo:
        with repo.config_writer() as config:
            config.set_value("user", "name", "Specification Source Test")
            config.set_value("user", "email", "source@example.test")
        repo.index.add(
            [
                item.relative_to(root).as_posix()
                for item in root.rglob("*")
                if item.is_file()
            ]
        )
        repo.index.commit("source fixture")
    return root


def _seed_lineage_and_binding(
    engine: Engine,
    repository: Path,
    *,
    project_name: str = "Registered source",
) -> tuple[str, str]:
    observed = GitPythonRepositoryProbe().inspect(repository)
    with Session(engine) as session:
        project = Project(name=project_name)
        session.add(project)
        session.flush()
        assert project.project_id is not None
        vision, goal = _seed_accepted_vision_and_goal(
            session,
            project_id=project.project_id,
            recorded_at=datetime(2026, 8, 12, 12, tzinfo=UTC),
        )
        binding = RepositoryBinding(
            project_id=project.project_id,
            worktree_path=observed.worktree_path,
            common_git_dir=observed.common_git_dir,
            head_sha=observed.head_sha,
            branch_name=observed.branch_name,
            detached_head=observed.detached_head,
            dirty=observed.dirty,
            status_fingerprint=observed.status_fingerprint,
            status_entries_json=canonical_json(
                [item.model_dump(mode="json") for item in observed.status_entries]
            ),
            remotes_json=canonical_json(list(observed.remotes)),
            warnings_json=canonical_json(
                [item.model_dump(mode="json") for item in observed.warnings]
            ),
            probe_version=observed.probe_version,
            inspected_at=observed.inspected_at,
            recorded_by="operator@example.test",
        )
        session.add(binding)
        session.flush()
        assert binding.repository_binding_id is not None
        project.active_repository_binding_id = binding.repository_binding_id
        session.add(project)
        session.commit()
        return vision.content_fingerprint, goal.content_fingerprint


def _request(
    *,
    project_id: int = 1,
    source_path: str = "specification.md",
    adr_paths: tuple[str, ...] = (
        "docs/adr/0002-second.md",
        "docs/adr/0001-first.md",
    ),
) -> SpecificationSourceRegistrationRequest:
    return SpecificationSourceRegistrationRequest(
        project_id=project_id,
        source_path=source_path,
        preparation_capability="grill-with-docs",
        adr_paths=adr_paths,
        idempotency_key="register-source-1",
        actor="operator@example.test",
        correlation_id="source-correlation-1",
    )


def _raw_sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def test_prepare_preserves_exact_utf8_bytes_and_canonicalizes_adrs(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Capture BOM, CRLF, and trailing bytes without text normalization."""
    context = b"\xef\xbb\xbfContext\r\ntrailing spaces  "
    source = b"\xef\xbb\xbfSpecification\r\nlast line  "
    repository = _git_repository(tmp_path, context=context)
    (repository / "specification.md").write_bytes(source)
    Repo(repository).index.add(["specification.md"])
    Repo(repository).index.commit("exact source bytes")
    vision_fingerprint, goal_fingerprint = _seed_lineage_and_binding(
        engine,
        repository,
    )
    probe = _CountingProbe()

    prepared = SpecificationSourceRegistrationService(
        engine=engine,
        repository_probe=probe,
    ).prepare(_request())

    assert base64.b64decode(prepared.bundle.source.content_base64) == source
    assert prepared.bundle.source.byte_length == len(source)
    assert prepared.bundle.source.content_fingerprint == _raw_sha256(source)
    assert prepared.bundle.context.state == "present"
    assert prepared.bundle.context.document is not None
    assert base64.b64decode(prepared.bundle.context.document.content_base64) == context
    assert tuple(item.relative_path for item in prepared.bundle.adrs) == (
        "docs/adr/0001-first.md",
        "docs/adr/0002-second.md",
    )
    assert prepared.bundle.accepted_vision_fingerprint == vision_fingerprint
    assert prepared.bundle.accepted_product_goal_fingerprint == goal_fingerprint
    assert prepared.bundle.preparation_capability == "grill-with-docs"
    assert prepared.bundle.repository_revision.branch_name is not None
    assert prepared.bundle.repository_revision.probe_version == (
        "agileforge.repository-probe.v1"
    )
    assert probe.calls == _EXPECTED_PROBE_CALLS


def test_prepare_records_context_absence(engine: Engine, tmp_path: Path) -> None:
    """Missing root CONTEXT.md is explicit canonical input, not an error."""
    repository = _git_repository(tmp_path)
    _seed_lineage_and_binding(engine, repository)

    prepared = SpecificationSourceRegistrationService(
        engine=engine,
        repository_probe=GitPythonRepositoryProbe(),
    ).prepare(_request())

    assert prepared.bundle.context.state == "absent"
    assert prepared.bundle.context.document is None


def test_registered_bundle_fingerprint_is_portable_across_checkout_roots(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Equivalent captured semantics have one identity on another checkout root."""
    original = _git_repository(tmp_path)
    relocated = tmp_path / "relocated-repository"
    shutil.copytree(original, relocated)
    _seed_lineage_and_binding(engine, original, project_name="Original source")
    _seed_lineage_and_binding(engine, relocated, project_name="Relocated source")
    service = SpecificationSourceRegistrationService(
        engine=engine,
        repository_probe=GitPythonRepositoryProbe(),
    )

    first = service.prepare(_request(project_id=1))
    second = service.prepare(_request(project_id=2))

    assert first.bundle.repository_revision.status_fingerprint == (
        second.bundle.repository_revision.status_fingerprint
    )
    assert source_bundle_fingerprint(first.bundle) == source_bundle_fingerprint(
        second.bundle
    )


@pytest.mark.parametrize(
    "source_path",
    [
        "/absolute.md",
        "../escape.md",
        "docs/../specification.md",
        "./specification.md",
        "docs//specification.md",
        "docs\\specification.md",
        "specification.md\x00suffix",
    ],
)
def test_request_rejects_noncanonical_source_paths(source_path: str) -> None:
    """Only exact repository-relative POSIX paths cross the public boundary."""
    with pytest.raises(ValidationError):
        _request(source_path=source_path)


@pytest.mark.parametrize(
    "adr_paths",
    [
        ("docs/adr/0001-first.md", "docs/adr/0001-first.md"),
        ("specification.md",),
        ("CONTEXT.md",),
        ("docs/notes.md",),
    ],
)
def test_request_rejects_duplicate_or_reserved_paths(
    adr_paths: tuple[str, ...],
) -> None:
    """A physical source may have only one semantic role."""
    with pytest.raises(ValidationError):
        _request(adr_paths=adr_paths)


def test_prepare_rejects_invalid_utf8(engine: Engine, tmp_path: Path) -> None:
    """Registered documents must decode as strict UTF-8 without replacement."""
    repository = _git_repository(tmp_path)
    (repository / "specification.md").write_bytes(b"invalid: \xff")
    Repo(repository).index.add(["specification.md"])
    Repo(repository).index.commit("invalid UTF-8")
    _seed_lineage_and_binding(engine, repository)

    with pytest.raises(SpecificationSourceRegistrationError) as caught:
        SpecificationSourceRegistrationService(
            engine=engine,
            repository_probe=GitPythonRepositoryProbe(),
        ).prepare(_request())

    assert caught.value.code is SpecificationSourceRegistrationErrorCode.INVALID_UTF8


def test_prepare_rejects_symlink_source(engine: Engine, tmp_path: Path) -> None:
    """Source capture never follows a symbolic link, even within the worktree."""
    repository = _git_repository(tmp_path)
    (repository / "real-source.md").write_bytes(b"real source\n")
    (repository / "specification.md").unlink()
    try:
        (repository / "specification.md").symlink_to("real-source.md")
    except OSError as error:
        if getattr(error, "winerror", None) == _WINDOWS_PRIVILEGE_NOT_HELD:
            raise pytest.skip.Exception(
                msg="Windows user lacks symbolic-link creation privilege"
            ) from error
        raise
    repo = Repo(repository)
    repo.index.remove(["specification.md"])
    repo.index.add(["real-source.md", "specification.md"])
    repo.index.commit("symlink source")
    _seed_lineage_and_binding(engine, repository)

    with pytest.raises(SpecificationSourceRegistrationError) as caught:
        SpecificationSourceRegistrationService(
            engine=engine,
            repository_probe=GitPythonRepositoryProbe(),
        ).prepare(_request())

    assert caught.value.code is SpecificationSourceRegistrationErrorCode.UNSAFE_FILE


def test_prepare_rejects_filesystem_path_spelling_alias(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Case-insensitive lookup cannot create a second source path identity."""
    repository = _git_repository(tmp_path)
    alias = repository / "SPECIFICATION.md"
    if not alias.exists():
        pytest.skip(
            "filesystem uses case-sensitive path lookup"  # ty: ignore[too-many-positional-arguments]
        )
    _seed_lineage_and_binding(engine, repository)

    with pytest.raises(SpecificationSourceRegistrationError) as caught:
        SpecificationSourceRegistrationService(
            engine=engine,
            repository_probe=GitPythonRepositoryProbe(),
        ).prepare(_request(source_path="SPECIFICATION.md"))

    assert caught.value.code is SpecificationSourceRegistrationErrorCode.UNSAFE_FILE


def test_prepare_rejects_nonregular_source(engine: Engine, tmp_path: Path) -> None:
    """Special files are rejected before open so capture cannot block on a FIFO."""
    repository = _git_repository(tmp_path)
    (repository / "specification.md").unlink()
    mkfifo = getattr(os, "mkfifo", None)
    if mkfifo is None:
        pytest.skip(
            "FIFO creation is unavailable on this platform"  # ty: ignore[too-many-positional-arguments]
        )
    mkfifo(repository / "specification.md")
    _seed_lineage_and_binding(engine, repository)

    with pytest.raises(SpecificationSourceRegistrationError) as caught:
        SpecificationSourceRegistrationService(
            engine=engine,
            repository_probe=GitPythonRepositoryProbe(),
        ).prepare(_request())

    assert caught.value.code is SpecificationSourceRegistrationErrorCode.UNSAFE_FILE


def test_prepare_rejects_oversize_source(engine: Engine, tmp_path: Path) -> None:
    """Oversize documents fail rather than entering a truncated registration."""
    repository = _git_repository(tmp_path)
    (repository / "specification.md").write_bytes(
        b"x" * (MAX_SPECIFICATION_SOURCE_DOCUMENT_BYTES + 1)
    )
    Repo(repository).index.add(["specification.md"])
    Repo(repository).index.commit("oversize source")
    _seed_lineage_and_binding(engine, repository)

    with pytest.raises(SpecificationSourceRegistrationError) as caught:
        SpecificationSourceRegistrationService(
            engine=engine,
            repository_probe=GitPythonRepositoryProbe(),
        ).prepare(_request())

    assert (
        caught.value.code is SpecificationSourceRegistrationErrorCode.SOURCE_TOO_LARGE
    )


def test_prepare_rejects_physical_file_alias(engine: Engine, tmp_path: Path) -> None:
    """Hard-linked paths cannot assign two semantic roles to the same inode."""
    repository = _git_repository(tmp_path)
    alias = repository / "docs" / "adr" / "source-alias.md"
    alias.unlink(missing_ok=True)
    os.link(repository / "specification.md", alias)
    Repo(repository).index.add(["docs/adr/source-alias.md"])
    Repo(repository).index.commit("hard link alias")
    _seed_lineage_and_binding(engine, repository)

    with pytest.raises(SpecificationSourceRegistrationError) as caught:
        SpecificationSourceRegistrationService(
            engine=engine,
            repository_probe=GitPythonRepositoryProbe(),
        ).prepare(_request(adr_paths=("docs/adr/source-alias.md",)))

    assert (
        caught.value.code is SpecificationSourceRegistrationErrorCode.DUPLICATE_SOURCE
    )


def test_prepare_rejects_repository_drift_between_probes(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """The completed bundle is discarded when repository provenance changes."""
    repository = _git_repository(tmp_path)
    _seed_lineage_and_binding(engine, repository)
    first = GitPythonRepositoryProbe().inspect(repository)

    class _DriftingProbe:
        calls = 0

        def inspect(self, path: Path | str) -> RepositoryProbeResult:
            del path
            self.calls += 1
            if self.calls == 1:
                return first
            return first.model_copy(update={"head_sha": "f" * 40})

    with pytest.raises(SpecificationSourceRegistrationError) as caught:
        SpecificationSourceRegistrationService(
            engine=engine,
            repository_probe=_DriftingProbe(),
        ).prepare(_request())

    assert (
        caught.value.code
        is SpecificationSourceRegistrationErrorCode.REPOSITORY_CHANGED_DURING_CAPTURE
    )


@pytest.mark.parametrize(
    ("fail_on_call", "expected"),
    [
        (1, SpecificationSourceRegistrationErrorCode.REPOSITORY_PROVENANCE_STALE),
        (2, SpecificationSourceRegistrationErrorCode.REPOSITORY_CHANGED_DURING_CAPTURE),
        (3, SpecificationSourceRegistrationErrorCode.REPOSITORY_CHANGED_DURING_CAPTURE),
    ],
)
def test_prepare_translates_repository_probe_failures(
    engine: Engine,
    tmp_path: Path,
    fail_on_call: int,
    expected: SpecificationSourceRegistrationErrorCode,
) -> None:
    """Every repository probe failure stays inside the closed capture boundary."""
    repository = _git_repository(tmp_path)
    _seed_lineage_and_binding(engine, repository)

    with pytest.raises(SpecificationSourceRegistrationError) as caught:
        SpecificationSourceRegistrationService(
            engine=engine,
            repository_probe=_RaisingProbe(
                fail_on_call=fail_on_call,
                delegate=GitPythonRepositoryProbe(),
            ),
        ).prepare(_request())

    assert caught.value.code is expected


def test_prepare_rejects_selected_bytes_changed_between_full_captures(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """A stable Git status cannot hide mixed bytes across the capture set."""
    repository = _git_repository(tmp_path)
    (repository / "specification.md").write_bytes(b"Dirty version one\n")
    _seed_lineage_and_binding(engine, repository)

    class _MutatingProbe:
        calls = 0
        delegate = GitPythonRepositoryProbe()

        def inspect(self, path: Path | str) -> RepositoryProbeResult:
            self.calls += 1
            result = self.delegate.inspect(path)
            if self.calls == _MIDDLE_PROBE_CALL:
                (repository / "specification.md").write_bytes(b"Changed after read\n")
            return result

    with pytest.raises(SpecificationSourceRegistrationError) as caught:
        SpecificationSourceRegistrationService(
            engine=engine,
            repository_probe=_MutatingProbe(),
        ).prepare(_request())

    assert (
        caught.value.code
        is SpecificationSourceRegistrationErrorCode.SOURCE_CHANGED_DURING_CAPTURE
    )
