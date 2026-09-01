"""Real Windows runtime tests for handle-anchored Vision evidence reads."""

from __future__ import annotations

import shutil
import subprocess  # nosec B404
import sys
from typing import TYPE_CHECKING

import pytest
from git import Repo
from sqlmodel import Session

from adapters.git.repository_probe import GitPythonRepositoryProbe
from models.core import Project
from models.repository import RepositoryBinding
from services.vision_evidence import (
    VisionEvidenceCollectionError,
    VisionEvidenceCollector,
    VisionEvidenceErrorCode,
)
from services.vision_evidence_windows import (
    WindowsRepositoryEvidenceReader,
    _WindowsApi,
    _WindowsCapabilityError,
)

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Never

    from sqlalchemy.engine import Engine

    from services.repository_probe import RepositoryProbeResult

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="requires real Windows file-handle semantics",
)


@pytest.fixture
def windows_repository(tmp_path: Path) -> Path:
    """Create one committed synthetic Windows Git repository."""
    root = tmp_path / "repository"
    root.mkdir()
    with Repo.init(root) as repo:
        with repo.config_writer() as config:
            config.set_value("user", "name", "Windows Vision Evidence Test")
            config.set_value("user", "email", "windows-evidence@example.com")
        (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        repo.index.add(["tracked.txt"])
        repo.index.commit("windows vision evidence fixture")
    return root


def _add_project(engine: Engine) -> int:
    with Session(engine) as session:
        project = Project(name="Windows Evidence", description="Synthetic only.")
        session.add(project)
        session.commit()
        assert project.project_id is not None
        return project.project_id


def _bind_repository(engine: Engine, project_id: int, repository: Path) -> None:
    observed = GitPythonRepositoryProbe().inspect(repository)
    with Session(engine) as session:
        binding = RepositoryBinding(
            project_id=project_id,
            worktree_path=observed.worktree_path,
            common_git_dir=observed.common_git_dir,
            head_sha=observed.head_sha,
            branch_name=observed.branch_name,
            detached_head=observed.detached_head,
            dirty=observed.dirty,
            status_fingerprint=observed.status_fingerprint,
            remotes_json="[]",
        )
        session.add(binding)
        session.commit()
        assert binding.repository_binding_id is not None
        project = session.get(Project, project_id)
        assert project is not None
        project.active_repository_binding_id = binding.repository_binding_id
        session.add(project)
        session.commit()


def _junction(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    command = shutil.which("cmd.exe")
    assert command is not None
    subprocess.run(  # noqa: S603  # nosec B603
        [command, "/c", "mklink", "/J", str(link), str(target)],
        check=True,
        capture_output=True,
        text=True,
    )


class _StableProbe:
    """Hold repository provenance stable while a race fixture changes paths."""

    def __init__(self, observed: RepositoryProbeResult) -> None:
        self.observed = observed

    def inspect(self, path: Path | str) -> RepositoryProbeResult:
        del path
        return self.observed


def test_windows_reader_collects_ordinary_repository_evidence(
    engine: Engine,
    windows_repository: Path,
) -> None:
    """Collect ordinary allowlisted bytes through real Windows handles."""
    (windows_repository / "README.md").write_text(
        "Windows evidence.\n", encoding="utf-8"
    )
    project_id = _add_project(engine)
    _bind_repository(engine, project_id, windows_repository)

    bundle = VisionEvidenceCollector(
        engine=engine,
        repository_probe=GitPythonRepositoryProbe(),
    ).collect(project_id)

    readme = next(item for item in bundle.items if item.relative_path == "README.md")
    assert readme.content == "Windows evidence."


def test_windows_reader_rejects_external_junction_escape(
    engine: Engine,
    tmp_path: Path,
    windows_repository: Path,
) -> None:
    """Never include bytes reached through a junction outside the worktree."""
    outside = tmp_path / "outside-spec"
    outside.mkdir()
    (outside / "spec.md").write_text("outside sentinel\n", encoding="utf-8")
    _junction(windows_repository / "docs/spec", outside)
    project_id = _add_project(engine)
    _bind_repository(engine, project_id, windows_repository)

    bundle = VisionEvidenceCollector(
        engine=engine,
        repository_probe=GitPythonRepositoryProbe(),
    ).collect(project_id)

    assert "outside sentinel" not in bundle.model_dump_json()
    assert "SYMLINK_ESCAPE" in {warning.code for warning in bundle.warnings}


def test_windows_reader_rejects_leaf_replacement_after_resolution(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    windows_repository: Path,
) -> None:
    """Discard collection when an approved leaf is replaced before its open."""
    readme = windows_repository / "README.md"
    readme.write_text("inside\n", encoding="utf-8")
    replacement = windows_repository / "replacement.md"
    replacement.write_text("replacement sentinel\n", encoding="utf-8")
    project_id = _add_project(engine)
    _bind_repository(engine, project_id, windows_repository)
    original = WindowsRepositoryEvidenceReader._open_relative
    swapped = False

    def swap_then_open(
        self: WindowsRepositoryEvidenceReader,
        parent_handle: int,
        component: str,
        *,
        directory: bool,
    ) -> int:
        nonlocal swapped
        if not directory and component == "README.md" and not swapped:
            swapped = True
            replacement.replace(readme)
        return original(self, parent_handle, component, directory=directory)

    monkeypatch.setattr(
        WindowsRepositoryEvidenceReader,
        "_open_relative",
        swap_then_open,
    )

    with pytest.raises(VisionEvidenceCollectionError) as caught:
        VisionEvidenceCollector(
            engine=engine,
            repository_probe=GitPythonRepositoryProbe(),
        ).collect(project_id)

    assert (
        caught.value.code
        is VisionEvidenceErrorCode.REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION
    )


def test_windows_reader_retains_parent_when_intermediate_is_replaced(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    windows_repository: Path,
) -> None:
    """Keep traversal on a retained directory handle after path replacement."""
    inside = windows_repository / "docs/spec/spec.md"
    inside.parent.mkdir(parents=True)
    inside.write_text("inside retained parent\n", encoding="utf-8")
    outside_docs = tmp_path / "outside-docs"
    (outside_docs / "spec").mkdir(parents=True)
    (outside_docs / "spec/spec.md").write_text(
        "outside sentinel\n",
        encoding="utf-8",
    )
    project_id = _add_project(engine)
    _bind_repository(engine, project_id, windows_repository)
    observed = GitPythonRepositoryProbe().inspect(windows_repository)
    original = WindowsRepositoryEvidenceReader._open_relative
    swapped = False

    def swap_then_open(
        self: WindowsRepositoryEvidenceReader,
        parent_handle: int,
        component: str,
        *,
        directory: bool,
    ) -> int:
        nonlocal swapped
        if directory and component == "spec" and not swapped:
            swapped = True
            (windows_repository / "docs").rename(windows_repository / "docs-original")
            _junction(windows_repository / "docs", outside_docs)
        return original(self, parent_handle, component, directory=directory)

    monkeypatch.setattr(
        WindowsRepositoryEvidenceReader,
        "_open_relative",
        swap_then_open,
    )
    bundle = VisionEvidenceCollector(
        engine=engine,
        repository_probe=_StableProbe(observed),
    ).collect(project_id)

    spec = next(
        item for item in bundle.items if item.relative_path == "docs/spec/spec.md"
    )
    assert spec.content == "inside retained parent"
    assert "outside sentinel" not in bundle.model_dump_json()


def test_windows_reader_detects_change_during_bounded_read(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    windows_repository: Path,
) -> None:
    """Reject a file whose size or timestamps change while its handle is read."""
    readme = windows_repository / "README.md"
    readme.write_text("inside\n", encoding="utf-8")
    project_id = _add_project(engine)
    _bind_repository(engine, project_id, windows_repository)
    original = WindowsRepositoryEvidenceReader._read_handle
    changed = False

    def change_after_read(
        self: WindowsRepositoryEvidenceReader,
        handle: int,
        byte_limit: int,
    ) -> bytes:
        nonlocal changed
        content = original(self, handle, byte_limit)
        if content and not changed:
            readme.write_text("changed during read\n", encoding="utf-8")
            changed = True
        return content

    monkeypatch.setattr(
        WindowsRepositoryEvidenceReader,
        "_read_handle",
        change_after_read,
    )

    with pytest.raises(VisionEvidenceCollectionError) as caught:
        VisionEvidenceCollector(
            engine=engine,
            repository_probe=GitPythonRepositoryProbe(),
        ).collect(project_id)

    assert (
        caught.value.code
        is VisionEvidenceErrorCode.REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION
    )


def test_windows_reader_allows_compatible_internal_junction(
    engine: Engine,
    windows_repository: Path,
) -> None:
    """Preserve approved internal-link semantics for a compatible junction."""
    target = windows_repository / "specs"
    target.mkdir()
    (target / "spec.md").write_text("internal junction\n", encoding="utf-8")
    _junction(windows_repository / "docs/spec", target)
    project_id = _add_project(engine)
    _bind_repository(engine, project_id, windows_repository)

    bundle = VisionEvidenceCollector(
        engine=engine,
        repository_probe=GitPythonRepositoryProbe(),
    ).collect(project_id)

    spec = next(
        item for item in bundle.items if item.relative_path == "docs/spec/spec.md"
    )
    assert spec.content == "internal junction"
    assert bundle.warnings == ()


def test_windows_reader_reports_unusable_file_identity_as_capability_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    windows_repository: Path,
) -> None:
    """Fail closed when the filesystem cannot provide stable handle identity."""

    def unavailable_identity(self: _WindowsApi, handle: int) -> Never:
        del self, handle
        message = "file identity unavailable"
        raise _WindowsCapabilityError(message)

    monkeypatch.setattr(_WindowsApi, "identity", unavailable_identity)

    capability = WindowsRepositoryEvidenceReader().capability(windows_repository)

    assert capability.available is False
    assert capability.code == "REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE"
