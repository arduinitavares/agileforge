"""Tests for deterministic, bounded Vision repository evidence collection."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from git import Repo
from sqlmodel import Session

import services.vision_evidence as vision_evidence_module
from adapters.git.repository_probe import GitPythonRepositoryProbe
from models.core import Project
from models.repository import RepositoryBinding
from services.contracts.vision_evidence import (
    MAX_EVIDENCE_ITEM_BYTES,
    MAX_EVIDENCE_ITEMS,
    MAX_EVIDENCE_TOTAL_BYTES,
    VisionEvidenceWarning,
)
from services.vision_evidence import (
    VisionEvidenceCollectionError,
    VisionEvidenceCollector,
    VisionEvidenceErrorCode,
)
from workflow.fingerprints import canonical_json

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from services.repository_probe import RepositoryProbeResult


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """Create one committed repository under the pytest temporary directory."""
    root = tmp_path / "repository"
    root.mkdir()
    with Repo.init(root) as repo:
        with repo.config_writer() as config:
            config.set_value("user", "name", "Vision Evidence Test")
            config.set_value("user", "email", "vision-evidence@example.com")
        (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        repo.index.add(["tracked.txt"])
        repo.index.commit("vision evidence fixture")
    return root


@pytest.fixture
def collector(engine: Engine) -> VisionEvidenceCollector:
    """Return the collector with the concrete adapter for temporary repositories."""
    return VisionEvidenceCollector(
        engine=engine,
        repository_probe=GitPythonRepositoryProbe(),
    )


def _add_project(
    engine: Engine,
    *,
    name: str = "Evidence Project",
    description: str | None = "Collect bounded repository context.",
) -> int:
    """Persist one project and return its durable identity."""
    with Session(engine) as session:
        project = Project(name=name, description=description)
        session.add(project)
        session.commit()
        assert project.project_id is not None
        return project.project_id


def _bind_repository(
    engine: Engine,
    *,
    project_id: int,
    repository: Path,
    activate: bool = True,
) -> int:
    """Persist one immutable binding and optionally select it for the Project."""
    result = GitPythonRepositoryProbe().inspect(repository)
    with Session(engine) as session:
        binding = RepositoryBinding(
            project_id=project_id,
            worktree_path=result.worktree_path,
            common_git_dir=result.common_git_dir,
            head_sha=result.head_sha,
            branch_name=result.branch_name,
            detached_head=result.detached_head,
            dirty=result.dirty,
            status_fingerprint=result.status_fingerprint,
            status_entries_json=canonical_json(
                [entry.model_dump(mode="json") for entry in result.status_entries]
            ),
            remotes_json=canonical_json(list(result.remotes)),
            warnings_json=canonical_json(
                [warning.model_dump(mode="json") for warning in result.warnings]
            ),
            probe_version=result.probe_version,
            inspected_at=result.inspected_at,
            recorded_by="vision-evidence@example.com",
        )
        session.add(binding)
        session.flush()
        assert binding.repository_binding_id is not None
        binding_id = binding.repository_binding_id
        if activate:
            project = session.get(Project, project_id)
            assert project is not None
            project.active_repository_binding_id = binding_id
            session.add(project)
        session.commit()
        return binding_id


def _artifact_payload(
    *, artifact_id: str = "SPEC.vision-evidence"
) -> dict[str, object]:
    """Return the smallest valid structured AgileForge specification."""
    return {
        "schema_version": "agileforge.spec.v1",
        "artifact_id": artifact_id,
        "title": "Vision Evidence Spec",
        "status": "draft",
        "version": "0.1",
        "created_at": "2026-08-10",
        "updated_at": "2026-08-10",
        "summary": "Bounded evidence is deterministic.",
        "problem_statement": "Vision bootstrap needs repository context.",
        "items": [],
    }


def _write_json_spec(repository: Path, relative_path: str, payload: object) -> None:
    """Write one JSON spec below the temporary repository root."""
    destination = repository / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload), encoding="utf-8")


def test_project_without_repository_collects_only_project_metadata(
    collector: VisionEvidenceCollector,
    engine: Engine,
) -> None:
    """Projects without an active binding expose only operator-provided metadata."""
    project_id = _add_project(engine)

    bundle = collector.collect(project_id)

    assert [item.evidence_id for item in bundle.items] == ["project:metadata"]
    assert bundle.items[0].trust == "operator_provided"
    assert "/Users/" not in bundle.model_dump_json()


def test_complete_allowlist_uses_exact_priority_and_excludes_arbitrary_files(
    collector: VisionEvidenceCollector,
    engine: Engine,
    repository: Path,
) -> None:
    """Collect only approved paths in priority order without status-path traversal."""
    _write_json_spec(repository, "docs/spec/spec.json", _artifact_payload())
    _write_json_spec(repository, "specs/spec.json", _artifact_payload())
    (repository / "CONTEXT.md").write_text("Context\n", encoding="utf-8")
    (repository / "README.md").write_text("Readme\n", encoding="utf-8")
    (repository / "pyproject.toml").write_text(
        "[project]\nname = 'evidence'\nkeywords = ['z', 'a']\n",
        encoding="utf-8",
    )
    (repository / ".env").write_text("SECRET=never-read\n", encoding="utf-8")
    (repository / "src").mkdir()
    (repository / "src/private.py").write_text("secret = True\n", encoding="utf-8")
    (repository / "notes.txt").write_text("not allowlisted\n", encoding="utf-8")
    project_id = _add_project(engine)
    _bind_repository(engine, project_id=project_id, repository=repository)

    bundle = collector.collect(project_id)

    assert [item.evidence_id for item in bundle.items] == [
        "project:metadata",
        "repository:provenance",
        "file:docs/spec/spec.json",
        "file:specs/spec.json",
        "file:CONTEXT.md",
        "file:README.md",
        "file:pyproject.toml",
    ]
    serialized = bundle.model_dump_json()
    assert ".env" not in serialized
    assert "src/private.py" not in serialized
    assert "notes.txt" not in serialized
    assert "status_entries" not in serialized
    assert bundle.items[-1].content == {"keywords": ["a", "z"], "name": "evidence"}


def test_remote_credentials_query_and_fragment_are_removed(
    collector: VisionEvidenceCollector,
    engine: Engine,
    repository: Path,
) -> None:
    """Expose sanitized network remote provenance only."""
    repo = Repo(repository)
    repo.create_remote(
        "origin",
        "https://user:secret@example.test/repo.git?token=x#fragment",
    )
    project_id = _add_project(engine)
    _bind_repository(engine, project_id=project_id, repository=repository)

    bundle = collector.collect(project_id)
    serialized = bundle.model_dump_json()

    assert "user:secret" not in serialized
    assert "token=x" not in serialized
    assert "fragment" not in serialized
    assert "https://example.test/repo.git" in serialized


def test_local_and_scp_remotes_are_omitted_or_normalized(
    collector: VisionEvidenceCollector,
    engine: Engine,
    repository: Path,
    tmp_path: Path,
) -> None:
    """Never expose local remote paths and normalize SCP-style remote URLs."""
    repo = Repo(repository)
    repo.create_remote("local", str(tmp_path / "private-repository"))
    repo.create_remote("origin", "git@example.test:team/repo.git")
    project_id = _add_project(engine)
    _bind_repository(engine, project_id=project_id, repository=repository)

    bundle = collector.collect(project_id)

    provenance = bundle.items[1]
    assert isinstance(provenance.content, dict)
    assert provenance.content["remotes"] == ["ssh://example.test/team/repo.git"]
    assert str(tmp_path) not in bundle.model_dump_json()
    assert [warning.code for warning in bundle.warnings] == ["REMOTE_OMITTED"]


def test_scp_remote_credentials_query_and_fragment_are_removed(
    collector: VisionEvidenceCollector,
    engine: Engine,
    repository: Path,
) -> None:
    """Apply parsed-URL sanitization rules to SCP-style network remotes."""
    Repo(repository).create_remote(
        "origin",
        "git@example.test:team/repo.git?token=x#fragment",
    )
    project_id = _add_project(engine)
    _bind_repository(engine, project_id=project_id, repository=repository)

    bundle = collector.collect(project_id)

    provenance = bundle.items[1]
    assert isinstance(provenance.content, dict)
    assert provenance.content["remotes"] == ["ssh://example.test/team/repo.git"]


def test_invalid_optional_inputs_warn_and_markdown_fallback_has_priority(
    collector: VisionEvidenceCollector,
    engine: Engine,
    repository: Path,
) -> None:
    """Keep valid fallback text when optional structured files cannot be parsed."""
    _write_json_spec(repository, "docs/spec/spec.json", {"artifact_id": "bad"})
    _write_json_spec(repository, "specs/spec.json", {"artifact_id": "also-bad"})
    (repository / "pyproject.toml").write_text("[project\n", encoding="utf-8")
    (repository / "docs/spec/spec.md").write_text("Docs fallback\n", encoding="utf-8")
    (repository / "specs/spec.md").parent.mkdir(parents=True, exist_ok=True)
    (repository / "specs/spec.md").write_text("Second fallback\n", encoding="utf-8")
    project_id = _add_project(engine)
    _bind_repository(engine, project_id=project_id, repository=repository)

    bundle = collector.collect(project_id)

    assert [item.evidence_id for item in bundle.items] == [
        "project:metadata",
        "repository:provenance",
        "file:docs/spec/spec.md",
    ]
    assert [warning.code for warning in bundle.warnings] == [
        "INVALID_SPECIFICATION",
        "INVALID_TOML",
        "INVALID_SPECIFICATION",
    ]


def test_conflicting_valid_specs_are_both_collected_with_a_stable_warning(
    collector: VisionEvidenceCollector,
    engine: Engine,
    repository: Path,
) -> None:
    """Preserve both valid structured sources when their canonical contents differ."""
    _write_json_spec(repository, "docs/spec/spec.json", _artifact_payload())
    _write_json_spec(
        repository,
        "specs/spec.json",
        _artifact_payload(artifact_id="SPEC.other-evidence"),
    )
    project_id = _add_project(engine)
    _bind_repository(engine, project_id=project_id, repository=repository)

    bundle = collector.collect(project_id)

    assert [item.evidence_id for item in bundle.items] == [
        "project:metadata",
        "repository:provenance",
        "file:docs/spec/spec.json",
        "file:specs/spec.json",
    ]
    assert [warning.code for warning in bundle.warnings] == [
        "CONFLICTING_SPECIFICATIONS"
    ]


def test_unreadable_and_escape_paths_warn_without_becoming_evidence(
    collector: VisionEvidenceCollector,
    engine: Engine,
    repository: Path,
    tmp_path: Path,
) -> None:
    """Reject invalid UTF-8 and symlink targets outside the bound worktree."""
    (repository / "README.md").write_bytes(b"\xff\xfe")
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    (repository / "CONTEXT.md").symlink_to(outside)
    project_id = _add_project(engine)
    _bind_repository(engine, project_id=project_id, repository=repository)

    bundle = collector.collect(project_id)

    assert [item.evidence_id for item in bundle.items] == [
        "project:metadata",
        "repository:provenance",
    ]
    assert [warning.code for warning in bundle.warnings] == [
        "SYMLINK_ESCAPE",
        "INVALID_UTF8",
    ]


def test_stable_in_worktree_symlink_uses_the_approved_source_identity(
    collector: VisionEvidenceCollector,
    engine: Engine,
    repository: Path,
) -> None:
    """Allow a compatible approved target while retaining the logical identity."""
    target = repository / "specs/spec.md"
    target.parent.mkdir()
    target.write_text("Approved technical specification.\n", encoding="utf-8")
    logical_source = repository / "docs/spec/spec.md"
    logical_source.parent.mkdir(parents=True)
    logical_source.symlink_to("../../specs/spec.md")
    project_id = _add_project(engine)
    _bind_repository(engine, project_id=project_id, repository=repository)

    bundle = collector.collect(project_id)

    specification = next(
        item for item in bundle.items if item.kind == "technical_specification"
    )
    assert specification.evidence_id == "file:docs/spec/spec.md"
    assert specification.relative_path == "docs/spec/spec.md"
    assert specification.content == "Approved technical specification."
    assert bundle.warnings == ()


@pytest.mark.parametrize(
    ("target_path", "content"),
    [
        (".env", "SECRET=must-not-leak\n"),
        ("src/private.py", "secret = True\n"),
        ("notes/private.md", "Arbitrary private document.\n"),
        ("pyproject.toml", "[project]\nname = 'private-package'\n"),
    ],
)
def test_allowlisted_symlink_rejects_incompatible_or_unapproved_targets(
    collector: VisionEvidenceCollector,
    engine: Engine,
    repository: Path,
    target_path: str,
    content: str,
) -> None:
    """Do not relabel forbidden or cross-policy content as README evidence."""
    target = repository / target_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    (repository / "README.md").symlink_to(target_path)
    project_id = _add_project(engine)
    _bind_repository(engine, project_id=project_id, repository=repository)

    bundle = collector.collect(project_id)

    assert "file:README.md" not in {item.evidence_id for item in bundle.items}
    assert content.strip() not in bundle.model_dump_json()
    assert "EVIDENCE_UNREADABLE" in {warning.code for warning in bundle.warnings}


def test_json_spec_symlink_rejects_approved_markdown_target(
    collector: VisionEvidenceCollector,
    engine: Engine,
    repository: Path,
) -> None:
    """Do not parse an approved Markdown spec through the JSON-spec policy."""
    target = repository / "specs/spec.md"
    target.parent.mkdir()
    target.write_text("Approved Markdown specification.\n", encoding="utf-8")
    logical_source = repository / "docs/spec/spec.json"
    logical_source.parent.mkdir(parents=True)
    logical_source.symlink_to("../../specs/spec.md")
    project_id = _add_project(engine)
    _bind_repository(engine, project_id=project_id, repository=repository)

    bundle = collector.collect(project_id)

    assert "file:docs/spec/spec.json" not in {item.evidence_id for item in bundle.items}
    assert "EVIDENCE_UNREADABLE" in {warning.code for warning in bundle.warnings}


def test_materially_large_source_reads_only_the_item_limit_plus_sentinel(
    collector: VisionEvidenceCollector,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
) -> None:
    """Apply the source ceiling before buffering a materially large file."""
    readme = repository / "README.md"
    readme.write_text("A" * (2 * 1024 * 1024), encoding="utf-8")
    project_id = _add_project(engine)
    _bind_repository(engine, project_id=project_id, repository=repository)
    identity = (readme.stat().st_dev, readme.stat().st_ino)
    original_read = vision_evidence_module.os.read
    bytes_read = 0

    def counting_read(descriptor: int, size: int) -> bytes:
        nonlocal bytes_read
        content = original_read(descriptor, size)
        observed = vision_evidence_module.os.fstat(descriptor)
        if (observed.st_dev, observed.st_ino) == identity:
            bytes_read += len(content)
        return content

    monkeypatch.setattr(vision_evidence_module.os, "read", counting_read)

    bundle = collector.collect(project_id)

    assert bytes_read <= MAX_EVIDENCE_ITEM_BYTES + 1
    readme_item = next(item for item in bundle.items if item.kind == "readme")
    assert readme_item.truncated is True
    assert len(str(readme_item.content).encode("utf-8")) == MAX_EVIDENCE_ITEM_BYTES


def test_growth_during_bounded_read_fails_closed_without_reading_past_sentinel(
    collector: VisionEvidenceCollector,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
) -> None:
    """Retain identity checks while keeping a growing source read bounded."""
    readme = repository / "README.md"
    readme.write_text("A" * (2 * MAX_EVIDENCE_ITEM_BYTES), encoding="utf-8")
    project_id = _add_project(engine)
    _bind_repository(engine, project_id=project_id, repository=repository)
    identity = (readme.stat().st_dev, readme.stat().st_ino)
    original_read = vision_evidence_module.os.read
    bytes_read = 0
    grew = False

    def grow_after_read(descriptor: int, size: int) -> bytes:
        nonlocal bytes_read, grew
        content = original_read(descriptor, size)
        observed = vision_evidence_module.os.fstat(descriptor)
        if (observed.st_dev, observed.st_ino) == identity:
            bytes_read += len(content)
            if content and not grew:
                with readme.open("a", encoding="utf-8") as stream:
                    stream.write("B")
                grew = True
        return content

    monkeypatch.setattr(vision_evidence_module.os, "read", grow_after_read)

    with pytest.raises(VisionEvidenceCollectionError) as caught:
        collector.collect(project_id)

    assert (
        caught.value.code
        is VisionEvidenceErrorCode.REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION
    )
    assert bytes_read <= MAX_EVIDENCE_ITEM_BYTES + 1


def test_allowlisted_fifo_is_rejected_without_a_blocking_open(
    collector: VisionEvidenceCollector,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
) -> None:
    """Open a special leaf nonblocking and reject it before any read."""
    if not hasattr(vision_evidence_module.os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable on this platform.")
    nonblock = getattr(vision_evidence_module.os, "O_NONBLOCK", None)
    if not isinstance(nonblock, int):
        pytest.skip("Nonblocking file opens are unavailable on this platform.")
    fifo = repository / "README.md"
    vision_evidence_module.os.mkfifo(fifo)
    project_id = _add_project(engine)
    _bind_repository(engine, project_id=project_id, repository=repository)
    original_open = vision_evidence_module.os.open

    def require_nonblocking_leaf(
        path: Path | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if str(path) == fifo.name and dir_fd is not None:
            assert flags & nonblock
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(vision_evidence_module.os, "open", require_nonblocking_leaf)

    bundle = collector.collect(project_id)

    assert "file:README.md" not in {item.evidence_id for item in bundle.items}
    assert [warning.code for warning in bundle.warnings] == ["EVIDENCE_UNREADABLE"]


def test_missing_nonblocking_open_capability_fails_before_leaf_open(
    collector: VisionEvidenceCollector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed without opening a leaf when O_NONBLOCK is unavailable."""
    warnings: list[VisionEvidenceWarning] = []
    open_calls: list[str] = []

    def unexpected_open(*_args: object, **_kwargs: object) -> int:
        open_calls.append("open")
        message = "os.open must not run without O_NONBLOCK"
        raise AssertionError(message)

    monkeypatch.delattr(vision_evidence_module.os, "O_NONBLOCK", raising=False)
    monkeypatch.setattr(vision_evidence_module.os, "open", unexpected_open)

    descriptor = collector._open_regular_leaf(
        parent_descriptor=0,
        leaf_name="README.md",
        source_path="README.md",
        no_follow=0,
        warnings=warnings,
    )

    assert descriptor is None
    assert open_calls == []
    assert [warning.code for warning in warnings] == ["EVIDENCE_UNREADABLE"]


def test_descriptor_read_rejects_a_symlink_swapped_after_resolution(
    collector: VisionEvidenceCollector,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    tmp_path: Path,
) -> None:
    """Do not follow a source swapped to an outside symlink during collection."""
    readme = repository / "README.md"
    readme.write_text("inside\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    project_id = _add_project(engine)
    _bind_repository(engine, project_id=project_id, repository=repository)
    original_open = vision_evidence_module.os.open

    def swap_then_open(
        path: Path | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if str(path) in {str(readme), readme.name}:
            readme.unlink()
            readme.symlink_to(outside)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(vision_evidence_module.os, "open", swap_then_open)

    bundle = collector.collect(project_id)

    assert "outside" not in bundle.model_dump_json()
    assert [warning.code for warning in bundle.warnings] == ["EVIDENCE_UNREADABLE"]


def test_descriptor_read_never_follows_a_swapped_intermediate_directory(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    tmp_path: Path,
) -> None:
    """Keep traversal anchored when an intermediate directory is replaced."""
    inside = repository / "docs/spec/spec.md"
    inside.parent.mkdir(parents=True)
    inside.write_text("inside\n", encoding="utf-8")
    outside_docs = tmp_path / "outside-docs"
    (outside_docs / "spec").mkdir(parents=True)
    (outside_docs / "spec/spec.md").write_text("outside\n", encoding="utf-8")
    project_id = _add_project(engine)
    _bind_repository(engine, project_id=project_id, repository=repository)
    observed = GitPythonRepositoryProbe().inspect(repository)
    collector = VisionEvidenceCollector(
        engine=engine,
        repository_probe=_ChangingProbe(observed, observed),
    )
    original_open = vision_evidence_module.os.open
    swapped = False

    def swap_then_open(
        path: Path | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and str(path) in {str(inside), inside.name}:
            swapped = True
            (repository / "docs").rename(repository / "docs-original")
            (repository / "docs").symlink_to(outside_docs, target_is_directory=True)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(vision_evidence_module.os, "open", swap_then_open)

    bundle = collector.collect(project_id)

    assert bundle.items[-1].evidence_id == "file:docs/spec/spec.md"
    assert bundle.items[-1].content == "inside"
    assert "outside" not in bundle.model_dump_json()


def test_text_is_truncated_at_utf8_boundaries_and_bundle_is_reproducible(
    collector: VisionEvidenceCollector,
    engine: Engine,
    repository: Path,
) -> None:
    """Bound oversized text while preserving valid Unicode and deterministic hashes."""
    (repository / "README.md").write_text(
        "A" * (33 * 1024) + "\U0001f642",
        encoding="utf-8",
    )
    project_id = _add_project(engine)
    _bind_repository(engine, project_id=project_id, repository=repository)

    first = collector.collect(project_id)
    second = collector.collect(project_id)

    readme = first.items[-1]
    assert readme.evidence_id == "file:README.md"
    assert readme.truncated is True
    assert isinstance(readme.content, str)
    assert len(readme.content.encode("utf-8")) == 32 * 1024
    assert first.evidence_fingerprint == second.evidence_fingerprint
    assert first.warnings == second.warnings
    assert [warning.code for warning in first.warnings] == ["TEXT_EVIDENCE_TRUNCATED"]


def test_oversized_structured_evidence_is_omitted_with_a_stable_warning(
    collector: VisionEvidenceCollector,
    engine: Engine,
    repository: Path,
) -> None:
    """Never truncate structured specification content beyond the item limit."""
    payload = _artifact_payload()
    payload["summary"] = "A" * (33 * 1024)
    _write_json_spec(repository, "docs/spec/spec.json", payload)
    project_id = _add_project(engine)
    _bind_repository(engine, project_id=project_id, repository=repository)

    bundle = collector.collect(project_id)

    assert [item.evidence_id for item in bundle.items] == [
        "project:metadata",
        "repository:provenance",
    ]
    assert [warning.code for warning in bundle.warnings] == [
        "STRUCTURED_EVIDENCE_TOO_LARGE"
    ]


def test_total_byte_limit_truncates_late_text_without_exceeding_96_kib(
    collector: VisionEvidenceCollector,
    engine: Engine,
    repository: Path,
) -> None:
    """Apply the aggregate evidence budget after deterministic source ordering."""
    large_text = "A" * (32 * 1024)
    (repository / "CONTEXT.md").write_text(large_text, encoding="utf-8")
    (repository / "README.md").write_text(large_text, encoding="utf-8")
    (repository / "pyproject.toml").write_text(
        "[project]\nname = 'evidence'\n",
        encoding="utf-8",
    )
    (repository / "docs/spec").mkdir(parents=True)
    (repository / "docs/spec/spec.md").write_text(large_text, encoding="utf-8")
    project_id = _add_project(engine)
    _bind_repository(engine, project_id=project_id, repository=repository)

    bundle = collector.collect(project_id)

    assert (
        sum(
            len(
                (
                    item.content
                    if isinstance(item.content, str)
                    else canonical_json(item.content)
                ).encode("utf-8")
            )
            for item in bundle.items
        )
        == MAX_EVIDENCE_TOTAL_BYTES
    )
    assert bundle.items[-1].evidence_id == "file:docs/spec/spec.md"
    assert bundle.items[-1].truncated is True
    assert [warning.code for warning in bundle.warnings] == ["TEXT_EVIDENCE_TRUNCATED"]


def test_structured_candidate_over_remaining_budget_does_not_stop_later_items(
    collector: VisionEvidenceCollector,
) -> None:
    """Omit one structured candidate while evaluating later candidates that fit."""
    candidate = vision_evidence_module._CandidateEvidence
    large_text_size = 32 * 1024
    candidates = [
        candidate(
            "file:CONTEXT.md",
            "context",
            "CONTEXT.md",
            "unreviewed_repository_evidence",
            "A" * large_text_size,
        ),
        candidate(
            "file:README.md",
            "readme",
            "README.md",
            "unreviewed_repository_evidence",
            "B" * large_text_size,
        ),
        candidate(
            "file:docs/spec/spec.md",
            "technical_specification",
            "docs/spec/spec.md",
            "unreviewed_repository_evidence",
            "C" * (MAX_EVIDENCE_TOTAL_BYTES - (2 * large_text_size) - 128),
        ),
        candidate(
            "file:pyproject.toml",
            "package_metadata",
            "pyproject.toml",
            "unreviewed_repository_evidence",
            {"description": "D" * 200},
        ),
        candidate(
            "file:specs/spec.md",
            "technical_specification",
            "specs/spec.md",
            "unreviewed_repository_evidence",
            "small",
        ),
    ]

    bundle = collector._bounded_bundle(candidates, [])

    assert [item.evidence_id for item in bundle.items][-1] == "file:specs/spec.md"
    assert "file:pyproject.toml" not in {item.evidence_id for item in bundle.items}
    assert [warning.code for warning in bundle.warnings] == [
        "EVIDENCE_TOTAL_LIMIT_REACHED"
    ]


def test_item_limit_caps_defensive_candidate_input_at_eight_items(
    collector: VisionEvidenceCollector,
) -> None:
    """Keep the contract cap even if future sources add extra candidates."""
    candidate = collector._project_candidate(Project(name="Cap Project"))

    bundle = collector._bounded_bundle([candidate] * 9, [])

    assert len(bundle.items) == MAX_EVIDENCE_ITEMS
    assert [warning.code for warning in bundle.warnings] == [
        "EVIDENCE_TOTAL_LIMIT_REACHED"
    ]


def test_stale_binding_and_project_lookup_fail_closed(
    collector: VisionEvidenceCollector,
    engine: Engine,
    repository: Path,
) -> None:
    """Require the requested project and exact recorded repository provenance."""
    with pytest.raises(VisionEvidenceCollectionError) as missing:
        collector.collect(999_999)
    assert missing.value.code is VisionEvidenceErrorCode.PROJECT_NOT_FOUND

    project_id = _add_project(engine)
    _bind_repository(engine, project_id=project_id, repository=repository)
    (repository / "untracked.txt").write_text("changed\n", encoding="utf-8")

    with pytest.raises(VisionEvidenceCollectionError) as stale:
        collector.collect(project_id)
    assert stale.value.code is VisionEvidenceErrorCode.REPOSITORY_PROVENANCE_STALE


def test_invalid_active_binding_fails_closed(
    collector: VisionEvidenceCollector,
    engine: Engine,
    repository: Path,
) -> None:
    """Reject a project reference that does not resolve to its own binding row."""
    project_id = _add_project(engine)
    other_project_id = _add_project(engine, name="Other Evidence Project")
    _bind_repository(engine, project_id=other_project_id, repository=repository)
    with Session(engine) as session:
        project = session.get(Project, project_id)
        other_project = session.get(Project, other_project_id)
        assert project is not None
        assert other_project is not None
        assert other_project.active_repository_binding_id is not None
        project.active_repository_binding_id = (
            other_project.active_repository_binding_id
        )
        session.add(project)
        session.commit()

    with pytest.raises(VisionEvidenceCollectionError) as caught:
        collector.collect(project_id)
    assert caught.value.code is VisionEvidenceErrorCode.REPOSITORY_BINDING_INVALID


class _ChangingProbe:
    """Return a changed second observation without touching a user repository."""

    def __init__(
        self,
        first: RepositoryProbeResult,
        second: RepositoryProbeResult,
    ) -> None:
        self._results = iter((first, second))

    def inspect(self, path: Path | str) -> RepositoryProbeResult:
        """Return the next deterministic observation."""
        del path
        return next(self._results)


class _SelectionChangingProbe:
    """Change the active binding during the first deterministic repository probe."""

    def __init__(
        self,
        engine: Engine,
        project_id: int,
        selected_binding_id: int | None,
        observed: RepositoryProbeResult,
    ) -> None:
        self._engine = engine
        self._project_id = project_id
        self._selected_binding_id = selected_binding_id
        self._observed = observed
        self._changed = False

    def inspect(self, path: Path | str) -> RepositoryProbeResult:
        """Switch selection once while returning stable worktree observations."""
        del path
        if not self._changed:
            with Session(self._engine) as session:
                project = session.get(Project, self._project_id)
                assert project is not None
                project.active_repository_binding_id = self._selected_binding_id
                session.add(project)
                session.commit()
            self._changed = True
        return self._observed


def test_active_binding_switch_during_collection_fails_closed(
    engine: Engine,
    repository: Path,
    tmp_path: Path,
) -> None:
    """Reject evidence from binding A when the Project selects binding B."""
    other_repository = tmp_path / "other-repository"
    other_repository.mkdir()
    with Repo.init(other_repository) as repo:
        with repo.config_writer() as config:
            config.set_value("user", "name", "Vision Evidence Test")
            config.set_value("user", "email", "vision-evidence@example.com")
        (other_repository / "tracked.txt").write_text("other\n", encoding="utf-8")
        repo.index.add(["tracked.txt"])
        repo.index.commit("other evidence fixture")
    project_id = _add_project(engine)
    _bind_repository(engine, project_id=project_id, repository=repository)
    other_binding_id = _bind_repository(
        engine,
        project_id=project_id,
        repository=other_repository,
        activate=False,
    )
    observed = GitPythonRepositoryProbe().inspect(repository)
    switching_collector = VisionEvidenceCollector(
        engine=engine,
        repository_probe=_SelectionChangingProbe(
            engine,
            project_id,
            other_binding_id,
            observed,
        ),
    )

    with pytest.raises(VisionEvidenceCollectionError) as caught:
        switching_collector.collect(project_id)

    assert (
        caught.value.code
        is VisionEvidenceErrorCode.REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION
    )


def test_active_binding_detach_during_collection_fails_closed(
    engine: Engine,
    repository: Path,
) -> None:
    """Reject repository evidence when the active binding is detached mid-read."""
    project_id = _add_project(engine)
    _bind_repository(engine, project_id=project_id, repository=repository)
    observed = GitPythonRepositoryProbe().inspect(repository)
    switching_collector = VisionEvidenceCollector(
        engine=engine,
        repository_probe=_SelectionChangingProbe(
            engine,
            project_id,
            None,
            observed,
        ),
    )

    with pytest.raises(VisionEvidenceCollectionError) as caught:
        switching_collector.collect(project_id)

    assert (
        caught.value.code
        is VisionEvidenceErrorCode.REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION
    )


def test_repository_attach_during_project_only_collection_fails_closed(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
) -> None:
    """Reject project-only evidence when a repository becomes active mid-collection."""
    project_id = _add_project(engine)
    binding_id = _bind_repository(
        engine,
        project_id=project_id,
        repository=repository,
        activate=False,
    )
    original = VisionEvidenceCollector._bounded_bundle

    def attach_then_bound(
        self: VisionEvidenceCollector,
        candidates: list[vision_evidence_module._CandidateEvidence],
        warnings: list[vision_evidence_module.VisionEvidenceWarning],
    ) -> vision_evidence_module.VisionEvidenceBundle:
        with Session(engine) as session:
            project = session.get(Project, project_id)
            assert project is not None
            project.active_repository_binding_id = binding_id
            session.add(project)
            session.commit()
        return original(self, candidates, warnings)

    monkeypatch.setattr(VisionEvidenceCollector, "_bounded_bundle", attach_then_bound)

    with pytest.raises(VisionEvidenceCollectionError) as caught:
        VisionEvidenceCollector(
            engine=engine,
            repository_probe=GitPythonRepositoryProbe(),
        ).collect(project_id)

    assert (
        caught.value.code
        is VisionEvidenceErrorCode.REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION
    )


def test_change_during_collection_discards_partial_evidence(
    engine: Engine,
    repository: Path,
) -> None:
    """Reject a repository whose provenance differs after evidence reads."""
    project_id = _add_project(engine)
    _bind_repository(engine, project_id=project_id, repository=repository)
    first = GitPythonRepositoryProbe().inspect(repository)
    second = first.model_copy(update={"remotes": ("https://example.test/changed.git",)})
    changing_collector = VisionEvidenceCollector(
        engine=engine,
        repository_probe=_ChangingProbe(first, second),
    )

    with pytest.raises(VisionEvidenceCollectionError) as caught:
        changing_collector.collect(project_id)

    assert (
        caught.value.code
        is VisionEvidenceErrorCode.REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION
    )


@pytest.mark.parametrize("operation", ["read", "stat"])
def test_post_open_races_fail_with_the_closed_repository_changed_error(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    repository: Path,
) -> None:
    """Translate post-open read and path replacement failures to the closed error."""
    readme = repository / "README.md"
    readme.write_text("inside\n", encoding="utf-8")
    project_id = _add_project(engine)
    _bind_repository(engine, project_id=project_id, repository=repository)
    observed = GitPythonRepositoryProbe().inspect(repository)
    collector = VisionEvidenceCollector(
        engine=engine,
        repository_probe=_ChangingProbe(observed, observed),
    )

    if operation == "read":

        def failed_read(descriptor: int, size: int) -> bytes:
            del descriptor, size
            raise OSError

        monkeypatch.setattr(vision_evidence_module.os, "read", failed_read)
    else:
        original_read = vision_evidence_module.os.read
        deleted = False

        def delete_after_read(descriptor: int, size: int) -> bytes:
            nonlocal deleted
            content = original_read(descriptor, size)
            if content and not deleted:
                readme.unlink()
                deleted = True
            return content

        monkeypatch.setattr(vision_evidence_module.os, "read", delete_after_read)

    with pytest.raises(VisionEvidenceCollectionError) as caught:
        collector.collect(project_id)

    assert (
        caught.value.code
        is VisionEvidenceErrorCode.REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION
    )
