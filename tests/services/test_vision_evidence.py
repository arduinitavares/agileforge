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
    MAX_EVIDENCE_ITEMS,
    MAX_EVIDENCE_TOTAL_BYTES,
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


def _bind_repository(engine: Engine, *, project_id: int, repository: Path) -> None:
    """Persist the immutable binding that selects the temporary repository."""
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
        project = session.get(Project, project_id)
        assert project is not None
        project.active_repository_binding_id = binding.repository_binding_id
        session.add(project)
        session.commit()


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

    def swap_then_open(path: Path | str, flags: int) -> int:
        if str(path) == str(readme):
            readme.unlink()
            readme.symlink_to(outside)
        return original_open(path, flags)

    monkeypatch.setattr(vision_evidence_module.os, "open", swap_then_open)

    bundle = collector.collect(project_id)

    assert "outside" not in bundle.model_dump_json()
    assert [warning.code for warning in bundle.warnings] == ["EVIDENCE_UNREADABLE"]


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

    assert sum(
        len(
            (
                item.content
                if isinstance(item.content, str)
                else canonical_json(item.content)
            ).encode("utf-8")
        )
        for item in bundle.items
    ) == MAX_EVIDENCE_TOTAL_BYTES
    assert bundle.items[-1].evidence_id == "file:docs/spec/spec.md"
    assert bundle.items[-1].truncated is True
    assert [warning.code for warning in bundle.warnings] == ["TEXT_EVIDENCE_TRUNCATED"]


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
