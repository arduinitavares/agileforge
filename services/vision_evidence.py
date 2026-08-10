"""Collect deterministic, bounded repository evidence for Vision bootstrap."""

from __future__ import annotations

import json
import os
import re
import stat
import tomllib
from codecs import getincrementaldecoder
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast
from urllib.parse import urlsplit, urlunsplit

from sqlmodel import Session

from models.core import Project
from models.repository import RepositoryBinding
from services.contracts.vision_evidence import (
    MAX_EVIDENCE_ITEM_BYTES,
    MAX_EVIDENCE_ITEMS,
    MAX_EVIDENCE_TOTAL_BYTES,
    VisionEvidenceBundle,
    VisionEvidenceItem,
    VisionEvidenceKind,
    VisionEvidenceTrust,
    VisionEvidenceWarning,
)
from utils.agileforge_spec_profile import TechnicalSpecArtifact
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from services.repository_probe import RepositoryProbe, RepositoryProbeResult
    from workflow.contracts import JsonObject, JsonValue


_ALLOWED_PATHS: tuple[str, ...] = (
    "docs/spec/spec.json",
    "specs/spec.json",
    "CONTEXT.md",
    "README.md",
    "pyproject.toml",
    "docs/spec/spec.md",
    "specs/spec.md",
)
_SCP_REMOTE_RE: re.Pattern[str] = re.compile(
    r"^(?:[^@/:]+@)?(?P<host>[^/:]+):(?P<path>.+)$"
)
_VALID_JSON_SPEC_COUNT: Final[int] = 2


class VisionEvidenceErrorCode(StrEnum):
    """Closed failures produced while collecting Vision evidence."""

    PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
    REPOSITORY_BINDING_INVALID = "REPOSITORY_BINDING_INVALID"
    REPOSITORY_PROVENANCE_STALE = "REPOSITORY_PROVENANCE_STALE"
    REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION = (
        "REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION"
    )


class VisionEvidenceCollectionError(RuntimeError):
    """Typed closed failure raised when evidence cannot be collected safely."""

    def __init__(self, code: VisionEvidenceErrorCode, message: str) -> None:
        """Store the closed code and human-readable failure message."""
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class _CollectionContext:
    """Durable project state loaded before repository access."""

    project: Project
    binding: RepositoryBinding | None
    active_repository_binding_id: int | None


@dataclass(frozen=True)
class _CandidateEvidence:
    """One unbounded evidence candidate prepared from an approved source."""

    evidence_id: str
    kind: VisionEvidenceKind
    relative_path: str | None
    trust: VisionEvidenceTrust
    content: str | JsonObject
    truncated: bool = False


@dataclass(frozen=True)
class VisionEvidenceCollection:
    """One evidence bundle and the exact binding used to collect it."""

    bundle: VisionEvidenceBundle
    repository_binding_id: int | None


@dataclass(frozen=True)
class VisionEvidenceCollector:
    """Read only the approved, stable evidence surface for one project."""

    engine: Engine
    repository_probe: RepositoryProbe

    def collect(self, project_id: int) -> VisionEvidenceBundle:
        """Collect one deterministic bundle or fail closed on provenance drift."""
        return self.collect_with_provenance(project_id).bundle

    def collect_with_provenance(self, project_id: int) -> VisionEvidenceCollection:
        """Collect evidence and return binding identity from the same context."""
        context = self._load_context(project_id)
        candidates = [self._project_candidate(context.project)]
        warnings: list[VisionEvidenceWarning] = []
        if context.binding is not None:
            observed = self._verify_binding(context.binding)
            candidates.extend(
                self._repository_candidates(
                    binding=context.binding,
                    probe=observed,
                    warnings=warnings,
                )
            )
            self._verify_unchanged(context.binding, observed)
        binding_id = context.active_repository_binding_id
        if context.binding is not None and binding_id is None:
            raise VisionEvidenceCollectionError(
                VisionEvidenceErrorCode.REPOSITORY_BINDING_INVALID,
                "Active repository binding has no durable identity.",
            )
        bundle = self._bounded_bundle(candidates, warnings)
        self._verify_active_binding(project_id, binding_id)
        return VisionEvidenceCollection(
            bundle=bundle,
            repository_binding_id=binding_id,
        )

    def _load_context(self, project_id: int) -> _CollectionContext:
        """Load the requested project and its exact active binding, if selected."""
        with Session(self.engine) as session:
            project = session.get(Project, project_id)
            if project is None:
                raise VisionEvidenceCollectionError(
                    VisionEvidenceErrorCode.PROJECT_NOT_FOUND,
                    "Project does not exist.",
                )
            binding_id = project.active_repository_binding_id
            if binding_id is None:
                return _CollectionContext(
                    project=project,
                    binding=None,
                    active_repository_binding_id=None,
                )
            binding = session.get(RepositoryBinding, binding_id)
            if binding is None or binding.project_id != project_id:
                raise VisionEvidenceCollectionError(
                    VisionEvidenceErrorCode.REPOSITORY_BINDING_INVALID,
                    (
                        "Active repository binding is missing or belongs to "
                        "another project."
                    ),
                )
            return _CollectionContext(
                project=project,
                binding=binding,
                active_repository_binding_id=binding_id,
            )

    def _verify_active_binding(
        self,
        project_id: int,
        expected_binding_id: int | None,
    ) -> None:
        """Require repository selection to match the identity captured at entry."""
        with Session(self.engine) as session:
            project = session.get(Project, project_id)
            if project is None:
                raise VisionEvidenceCollectionError(
                    VisionEvidenceErrorCode.PROJECT_NOT_FOUND,
                    "Project was removed during evidence collection.",
                )
            if project.active_repository_binding_id != expected_binding_id:
                raise VisionEvidenceCollectionError(
                    VisionEvidenceErrorCode.REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION,
                    "Active repository selection changed during evidence collection.",
                )

    def _verify_binding(self, binding: RepositoryBinding) -> RepositoryProbeResult:
        """Probe the selected worktree and require an exact durable match."""
        try:
            observed = self.repository_probe.inspect(binding.worktree_path)
        except Exception as exc:
            raise VisionEvidenceCollectionError(
                VisionEvidenceErrorCode.REPOSITORY_PROVENANCE_STALE,
                "Repository provenance could not be refreshed.",
            ) from exc
        if not self._matches_binding(binding, observed):
            raise VisionEvidenceCollectionError(
                VisionEvidenceErrorCode.REPOSITORY_PROVENANCE_STALE,
                "Repository provenance differs from the active binding.",
            )
        return observed

    def _verify_unchanged(
        self,
        binding: RepositoryBinding,
        before: RepositoryProbeResult,
    ) -> None:
        """Re-probe after all descriptor reads and reject any changed provenance."""
        try:
            after = self.repository_probe.inspect(binding.worktree_path)
        except Exception as exc:
            raise VisionEvidenceCollectionError(
                VisionEvidenceErrorCode.REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION,
                "Repository provenance could not be rechecked after evidence reads.",
            ) from exc
        if self._provenance_fields(after) != self._provenance_fields(before):
            raise VisionEvidenceCollectionError(
                VisionEvidenceErrorCode.REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION,
                "Repository provenance changed during evidence collection.",
            )

    def _matches_binding(
        self,
        binding: RepositoryBinding,
        observed: RepositoryProbeResult,
    ) -> bool:
        """Compare all stable provenance fields stored in the immutable binding."""
        try:
            stored_remotes = json.loads(binding.remotes_json)
        except json.JSONDecodeError:
            return False
        if not isinstance(stored_remotes, list) or not all(
            isinstance(remote, str) for remote in stored_remotes
        ):
            return False
        expected = (
            binding.worktree_path,
            binding.common_git_dir,
            binding.head_sha,
            binding.branch_name,
            binding.detached_head,
            binding.dirty,
            binding.status_fingerprint,
            canonical_json(stored_remotes),
        )
        return expected == self._provenance_fields(observed)

    @staticmethod
    def _provenance_fields(probe: RepositoryProbeResult) -> tuple[object, ...]:
        """Return exactly the mutable fields that identify repository provenance."""
        return (
            probe.worktree_path,
            probe.common_git_dir,
            probe.head_sha,
            probe.branch_name,
            probe.detached_head,
            probe.dirty,
            probe.status_fingerprint,
            canonical_json(list(probe.remotes)),
        )

    @staticmethod
    def _project_candidate(project: Project) -> _CandidateEvidence:
        """Prepare operator-provided project metadata without persistence details."""
        content: JsonObject = {
            "name": project.name,
            "description": project.description,
        }
        return _CandidateEvidence(
            evidence_id="project:metadata",
            kind="project_metadata",
            relative_path=None,
            trust="operator_provided",
            content=content,
        )

    def _repository_candidates(
        self,
        *,
        binding: RepositoryBinding,
        probe: RepositoryProbeResult,
        warnings: list[VisionEvidenceWarning],
    ) -> list[_CandidateEvidence]:
        """Read only the allowlisted repository files in declared priority order."""
        candidates = [self._repository_candidate(probe, warnings)]
        try:
            worktree = Path(binding.worktree_path).resolve(strict=True)
        except OSError as exc:
            raise VisionEvidenceCollectionError(
                VisionEvidenceErrorCode.REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION,
                "Repository worktree changed before evidence files were read.",
            ) from exc
        root_descriptor = self._open_worktree_descriptor(str(worktree))
        try:
            valid_specs = self._structured_spec_candidates(
                root_descriptor,
                worktree,
                warnings,
            )
            candidates.extend(candidate for _path, candidate in valid_specs)
            candidates.extend(
                self._supplemental_candidates(
                    root_descriptor,
                    worktree,
                    valid_specs,
                    warnings,
                )
            )
        finally:
            os.close(root_descriptor)
        return candidates

    @staticmethod
    def _open_worktree_descriptor(worktree_path: str) -> int:
        """Retain the verified worktree directory as the traversal anchor."""
        no_follow = getattr(os, "O_NOFOLLOW", None)
        directory = getattr(os, "O_DIRECTORY", None)
        if not isinstance(no_follow, int) or not isinstance(directory, int):
            raise VisionEvidenceCollectionError(
                VisionEvidenceErrorCode.REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION,
                "Platform cannot safely open the repository worktree.",
            )
        try:
            return os.open(worktree_path, os.O_RDONLY | no_follow | directory)
        except OSError as exc:
            raise VisionEvidenceCollectionError(
                VisionEvidenceErrorCode.REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION,
                "Repository worktree changed before evidence files were read.",
            ) from exc

    def _repository_candidate(
        self,
        probe: RepositoryProbeResult,
        warnings: list[VisionEvidenceWarning],
    ) -> _CandidateEvidence:
        """Prepare observed repository provenance without sensitive local detail."""
        remotes, remote_omitted = self._sanitized_remotes(probe.remotes)
        if remote_omitted:
            warnings.append(
                self._warning(
                    code="REMOTE_OMITTED",
                    source="repository",
                    message="Local or invalid repository remotes were omitted.",
                )
            )
        remote_values: list[JsonValue] = list(remotes)
        content: JsonObject = {
            "head_sha": probe.head_sha,
            "branch_name": probe.branch_name,
            "detached_head": probe.detached_head,
            "dirty": probe.dirty,
            "remotes": remote_values,
        }
        return _CandidateEvidence(
            evidence_id="repository:provenance",
            kind="repository_provenance",
            relative_path=None,
            trust="observed_provenance",
            content=content,
        )

    def _structured_spec_candidates(
        self,
        root_descriptor: int,
        worktree: Path,
        warnings: list[VisionEvidenceWarning],
    ) -> list[tuple[str, _CandidateEvidence]]:
        """Collect valid JSON specs and emit their conflict warning when needed."""
        valid_specs: list[tuple[str, _CandidateEvidence]] = []
        for relative_path in _ALLOWED_PATHS[:2]:
            candidate = self._json_spec_candidate(
                root_descriptor,
                worktree,
                relative_path,
                warnings,
            )
            if candidate is not None:
                valid_specs.append((relative_path, candidate))
        if self._specifications_conflict(valid_specs):
            warnings.append(
                self._warning(
                    code="CONFLICTING_SPECIFICATIONS",
                    source="repository",
                    message="Valid structured specifications have different content.",
                )
            )
        return valid_specs

    @staticmethod
    def _specifications_conflict(
        valid_specs: list[tuple[str, _CandidateEvidence]],
    ) -> bool:
        """Return whether both valid JSON specs have different canonical content."""
        if len(valid_specs) != _VALID_JSON_SPEC_COUNT:
            return False
        return canonical_hash(valid_specs[0][1].content) != canonical_hash(
            valid_specs[1][1].content
        )

    def _supplemental_candidates(
        self,
        root_descriptor: int,
        worktree: Path,
        valid_specs: list[tuple[str, _CandidateEvidence]],
        warnings: list[VisionEvidenceWarning],
    ) -> list[_CandidateEvidence]:
        """Collect context, package metadata, and a fallback Markdown spec."""
        candidates: list[_CandidateEvidence] = []
        for relative_path, kind in (
            ("CONTEXT.md", "context"),
            ("README.md", "readme"),
        ):
            candidate = self._text_candidate(
                root_descriptor,
                worktree,
                relative_path,
                kind,
                warnings,
            )
            if candidate is not None:
                candidates.append(candidate)
        package = self._package_candidate(root_descriptor, worktree, warnings)
        if package is not None:
            candidates.append(package)
        if not valid_specs:
            for relative_path in _ALLOWED_PATHS[-2:]:
                candidate = self._text_candidate(
                    root_descriptor,
                    worktree,
                    relative_path,
                    "technical_specification",
                    warnings,
                )
                if candidate is not None:
                    candidates.append(candidate)
                    break
        return candidates

    def _json_spec_candidate(
        self,
        root_descriptor: int,
        worktree: Path,
        relative_path: str,
        warnings: list[VisionEvidenceWarning],
    ) -> _CandidateEvidence | None:
        """Validate one optional structured specification file."""
        source = self._read_text(
            root_descriptor,
            worktree,
            relative_path,
            warnings,
            structured=True,
        )
        if source is None:
            return None
        text, _truncated = source
        try:
            artifact = TechnicalSpecArtifact.model_validate_json(text)
        except ValueError:
            warnings.append(
                self._warning(
                    code="INVALID_SPECIFICATION",
                    source=relative_path,
                    message=(
                        "Structured specification did not match the AgileForge "
                        "profile."
                    ),
                )
            )
            return None
        return _CandidateEvidence(
            evidence_id=f"file:{relative_path}",
            kind="technical_specification",
            relative_path=relative_path,
            trust="unreviewed_repository_evidence",
            content=cast("JsonObject", artifact.model_dump(mode="json", by_alias=True)),
        )

    def _package_candidate(
        self,
        root_descriptor: int,
        worktree: Path,
        warnings: list[VisionEvidenceWarning],
    ) -> _CandidateEvidence | None:
        """Extract selected package metadata without exposing the full TOML source."""
        relative_path = "pyproject.toml"
        source = self._read_text(
            root_descriptor,
            worktree,
            relative_path,
            warnings,
            structured=True,
        )
        if source is None:
            return None
        text, _truncated = source
        try:
            parsed = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            warnings.append(
                self._warning(
                    code="INVALID_TOML",
                    source=relative_path,
                    message="Package metadata is not valid TOML.",
                )
            )
            return None
        project_table = parsed.get("project")
        if not isinstance(project_table, dict):
            return None
        content: JsonObject = {}
        for key in ("name", "description"):
            value = project_table.get(key)
            if isinstance(value, str):
                content[key] = value
        keywords = project_table.get("keywords")
        if isinstance(keywords, list) and all(
            isinstance(item, str) for item in keywords
        ):
            content["keywords"] = sorted(keywords)
        scripts = project_table.get("scripts")
        if isinstance(scripts, dict) and all(
            isinstance(name, str) and isinstance(value, str)
            for name, value in scripts.items()
        ):
            content["scripts"] = {name: scripts[name] for name in sorted(scripts)}
        if not content:
            return None
        return _CandidateEvidence(
            evidence_id=f"file:{relative_path}",
            kind="package_metadata",
            relative_path=relative_path,
            trust="unreviewed_repository_evidence",
            content=content,
        )

    def _text_candidate(
        self,
        root_descriptor: int,
        worktree: Path,
        relative_path: str,
        kind: VisionEvidenceKind,
        warnings: list[VisionEvidenceWarning],
    ) -> _CandidateEvidence | None:
        """Read one optional UTF-8 text file through a verified descriptor."""
        source = self._read_text(
            root_descriptor,
            worktree,
            relative_path,
            warnings,
            structured=False,
        )
        if source is None:
            return None
        text, truncated = source
        if not text.strip():
            return None
        return _CandidateEvidence(
            evidence_id=f"file:{relative_path}",
            kind=kind,
            relative_path=relative_path,
            trust="unreviewed_repository_evidence",
            content=text.strip(),
            truncated=truncated,
        )

    def _read_text(
        self,
        root_descriptor: int,
        worktree: Path,
        relative_path: str,
        warnings: list[VisionEvidenceWarning],
        *,
        structured: bool,
    ) -> tuple[str, bool] | None:
        """Read one optional source safely and reject changed or escaping files."""
        resolved_path = self._resolve_internal_path(
            worktree,
            relative_path,
            warnings,
        )
        if resolved_path is None:
            return None
        opened = self._open_descriptor(
            root_descriptor,
            resolved_path,
            relative_path,
            warnings,
        )
        if opened is None:
            return None
        descriptor, parent_descriptor, leaf_name = opened
        try:
            try:
                before = os.fstat(descriptor)
                source_limit = MAX_EVIDENCE_ITEM_BYTES + 1
                content = bytearray()
                while len(content) < source_limit:
                    chunk = os.read(descriptor, source_limit - len(content))
                    if not chunk:
                        break
                    content.extend(chunk)
                after = os.fstat(descriptor)
                current = os.stat(
                    leaf_name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise VisionEvidenceCollectionError(
                    VisionEvidenceErrorCode.REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION,
                    "Approved evidence file changed while it was read.",
                ) from exc
        finally:
            os.close(descriptor)
            os.close(parent_descriptor)
        if not (
            self._file_identity(before) == self._file_identity(after)
            and self._file_identity(before) == self._file_identity(current)
        ):
            raise VisionEvidenceCollectionError(
                VisionEvidenceErrorCode.REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION,
                "Approved evidence file changed while it was read.",
            )
        truncated = len(content) > MAX_EVIDENCE_ITEM_BYTES
        if truncated and structured:
            warnings.append(
                self._warning(
                    code="STRUCTURED_EVIDENCE_TOO_LARGE",
                    source=relative_path,
                    message="Structured evidence exceeded the per-item byte limit.",
                )
            )
            return None
        bounded_content = bytes(content[:MAX_EVIDENCE_ITEM_BYTES])
        try:
            decoder = getincrementaldecoder("utf-8")()
            text = decoder.decode(bounded_content, final=not truncated)
        except UnicodeDecodeError:
            warnings.append(
                self._warning(
                    code="INVALID_UTF8",
                    source=relative_path,
                    message="Approved evidence file is not valid UTF-8.",
                )
            )
            return None
        if truncated:
            warnings.append(
                self._warning(
                    code="TEXT_EVIDENCE_TRUNCATED",
                    source=relative_path,
                    message="Text evidence exceeded the configured byte limit.",
                )
            )
        return text, truncated

    @staticmethod
    def _resolve_internal_path(
        worktree: Path,
        relative_path: str,
        warnings: list[VisionEvidenceWarning],
    ) -> str | None:
        """Resolve one allowlisted source and retain only internal targets."""
        try:
            resolved = (worktree / relative_path).resolve(strict=True)
        except FileNotFoundError:
            return None
        except OSError:
            warnings.append(
                VisionEvidenceCollector._warning(
                    code="EVIDENCE_UNREADABLE",
                    source=relative_path,
                    message="Approved evidence path could not be resolved.",
                )
            )
            return None
        try:
            return resolved.relative_to(worktree).as_posix()
        except ValueError:
            warnings.append(
                VisionEvidenceCollector._warning(
                    code="SYMLINK_ESCAPE",
                    source=relative_path,
                    message="Approved evidence path resolves outside the worktree.",
                )
            )
            return None

    def _open_descriptor(
        self,
        root_descriptor: int,
        resolved_path: str,
        source_path: str,
        warnings: list[VisionEvidenceWarning],
    ) -> tuple[int, int, str] | None:
        """Open every relative path component without following symbolic links."""
        no_follow = getattr(os, "O_NOFOLLOW", None)
        directory = getattr(os, "O_DIRECTORY", None)
        if not isinstance(no_follow, int) or not isinstance(directory, int):
            warnings.append(
                self._warning(
                    code="EVIDENCE_UNREADABLE",
                    source=source_path,
                    message="Platform cannot safely open approved evidence files.",
                )
            )
            return None
        relative = Path(resolved_path)
        parts = relative.parts
        if relative.is_absolute() or not parts or any(
            part in {"", ".", ".."} for part in parts
        ):
            warnings.append(
                self._warning(
                    code="EVIDENCE_UNREADABLE",
                    source=source_path,
                    message="Approved evidence path is not repository-relative.",
                )
            )
            return None
        parent_descriptor = self._open_parent_descriptor(
            root_descriptor,
            parts[:-1],
            source_path,
            no_follow | directory,
            warnings,
        )
        if parent_descriptor is None:
            return None
        leaf_name = parts[-1]
        if not self._component_is_safe(
            parent_descriptor,
            leaf_name,
            source_path,
            warnings,
        ):
            os.close(parent_descriptor)
            return None
        descriptor = self._open_regular_leaf(
            parent_descriptor,
            leaf_name,
            source_path,
            no_follow,
            warnings,
        )
        if descriptor is None:
            os.close(parent_descriptor)
            return None
        return descriptor, parent_descriptor, leaf_name

    def _open_regular_leaf(
        self,
        parent_descriptor: int,
        leaf_name: str,
        source_path: str,
        no_follow: int,
        warnings: list[VisionEvidenceWarning],
    ) -> int | None:
        """Open one nonblocking leaf and retain only regular files."""
        nonblock = getattr(os, "O_NONBLOCK", 0)
        if not isinstance(nonblock, int):
            nonblock = 0
        try:
            descriptor = os.open(
                leaf_name,
                os.O_RDONLY | no_follow | nonblock,
                dir_fd=parent_descriptor,
            )
        except OSError:
            warnings.append(
                self._warning(
                    code="EVIDENCE_UNREADABLE",
                    source=source_path,
                    message="Approved evidence file could not be opened.",
                )
            )
            return None
        try:
            opened_stat = os.fstat(descriptor)
        except OSError:
            os.close(descriptor)
            warnings.append(
                self._warning(
                    code="EVIDENCE_UNREADABLE",
                    source=source_path,
                    message="Approved evidence file could not be inspected.",
                )
            )
            return None
        if not stat.S_ISREG(opened_stat.st_mode):
            os.close(descriptor)
            warnings.append(
                self._warning(
                    code="EVIDENCE_UNREADABLE",
                    source=source_path,
                    message="Approved evidence source is not a regular file.",
                )
            )
            return None
        return descriptor

    def _open_parent_descriptor(
        self,
        root_descriptor: int,
        components: tuple[str, ...],
        relative_path: str,
        directory_flags: int,
        warnings: list[VisionEvidenceWarning],
    ) -> int | None:
        """Traverse intermediate directories relative to the retained root."""
        try:
            parent_descriptor = os.dup(root_descriptor)
        except OSError:
            warnings.append(
                self._warning(
                    code="EVIDENCE_UNREADABLE",
                    source=relative_path,
                    message="Approved evidence traversal could not be started.",
                )
            )
            return None
        for component in components:
            if not self._component_is_safe(
                parent_descriptor,
                component,
                relative_path,
                warnings,
            ):
                os.close(parent_descriptor)
                return None
            try:
                next_descriptor = os.open(
                    component,
                    os.O_RDONLY | directory_flags,
                    dir_fd=parent_descriptor,
                )
            except OSError:
                os.close(parent_descriptor)
                warnings.append(
                    self._warning(
                        code="EVIDENCE_UNREADABLE",
                        source=relative_path,
                        message="Approved evidence directory could not be opened.",
                    )
                )
                return None
            os.close(parent_descriptor)
            parent_descriptor = next_descriptor
        return parent_descriptor

    def _component_is_safe(
        self,
        parent_descriptor: int,
        component: str,
        relative_path: str,
        warnings: list[VisionEvidenceWarning],
    ) -> bool:
        """Reject missing, unreadable, and symbolic-link path components."""
        try:
            component_stat = os.stat(
                component,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        except OSError:
            warnings.append(
                self._warning(
                    code="EVIDENCE_UNREADABLE",
                    source=relative_path,
                    message="Approved evidence path could not be inspected.",
                )
            )
            return False
        if not stat.S_ISLNK(component_stat.st_mode):
            return True
        warnings.append(
            self._warning(
                code="SYMLINK_ESCAPE",
                source=relative_path,
                message="Approved evidence path contains a symbolic link.",
            )
        )
        return False

    @staticmethod
    def _file_identity(stat_result: os.stat_result) -> tuple[int, int, int, int]:
        """Return the stable metadata used to detect replacement or modification."""
        return (
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_size,
            stat_result.st_mtime_ns,
        )

    @staticmethod
    def _sanitized_remotes(remotes: tuple[str, ...]) -> tuple[list[str], bool]:
        """Return sorted network remotes without credentials or local paths."""
        sanitized: list[str] = []
        omitted = False
        for remote in remotes:
            normalized = VisionEvidenceCollector._sanitize_remote(remote)
            if normalized is None:
                omitted = True
            else:
                sanitized.append(normalized)
        return sorted(set(sanitized)), omitted

    @staticmethod
    def _sanitize_remote(remote: str) -> str | None:
        """Normalize one network remote and omit local, malformed, or file URLs."""
        scp_match = _SCP_REMOTE_RE.fullmatch(remote)
        if scp_match is not None and "://" not in remote:
            remote = f"ssh://{scp_match.group('host')}/{scp_match.group('path')}"
        parsed = urlsplit(remote)
        if parsed.scheme == "file" or not parsed.scheme or parsed.hostname is None:
            return None
        try:
            port = parsed.port
        except ValueError:
            return None
        host = parsed.hostname
        netloc = host if port is None else f"{host}:{port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))

    def _bounded_bundle(
        self,
        candidates: list[_CandidateEvidence],
        warnings: list[VisionEvidenceWarning],
    ) -> VisionEvidenceBundle:
        """Apply the fixed item and byte budgets before building the contract bundle."""
        items: list[VisionEvidenceItem] = []
        total_bytes = 0
        for candidate in candidates:
            if len(items) >= MAX_EVIDENCE_ITEMS:
                warnings.append(
                    self._warning(
                        code="EVIDENCE_TOTAL_LIMIT_REACHED",
                        source="bundle",
                        message="Evidence item limit reached.",
                    )
                )
                break
            remaining = MAX_EVIDENCE_TOTAL_BYTES - total_bytes
            content = candidate.content
            truncated = candidate.truncated
            size = self._content_size(content)
            if isinstance(content, str):
                target = min(MAX_EVIDENCE_ITEM_BYTES, remaining)
                if target <= 0:
                    warnings.append(
                        self._warning(
                            code="EVIDENCE_TOTAL_LIMIT_REACHED",
                            source="bundle",
                            message="Evidence byte limit reached.",
                        )
                    )
                    break
                if size > target:
                    content = self._truncate_utf8(content, target)
                    was_truncated = truncated
                    truncated = True
                    size = self._content_size(content)
                    if not was_truncated:
                        warnings.append(
                            self._warning(
                                code="TEXT_EVIDENCE_TRUNCATED",
                                source=candidate.relative_path or "bundle",
                                message=(
                                    "Text evidence exceeded the configured byte limit."
                                ),
                            )
                        )
            elif size > MAX_EVIDENCE_ITEM_BYTES:
                warnings.append(
                    self._warning(
                        code="STRUCTURED_EVIDENCE_TOO_LARGE",
                        source=candidate.relative_path or "bundle",
                        message="Structured evidence exceeded the per-item byte limit.",
                    )
                )
                continue
            if size > remaining:
                warnings.append(
                    self._warning(
                        code="EVIDENCE_TOTAL_LIMIT_REACHED",
                        source="bundle",
                        message="Evidence byte limit reached.",
                    )
                )
                continue
            item = VisionEvidenceItem(
                evidence_id=candidate.evidence_id,
                kind=candidate.kind,
                relative_path=candidate.relative_path,
                content_fingerprint=canonical_hash(content),
                trust=candidate.trust,
                content=content,
                truncated=truncated,
            )
            items.append(item)
            total_bytes += size
        ordered_warnings = tuple(sorted(warnings, key=self._warning_sort_key))
        payload = {
            "schema_version": "agileforge.vision-evidence.v1",
            "items": [item.model_dump(mode="json") for item in items],
            "warnings": [
                warning.model_dump(mode="json") for warning in ordered_warnings
            ],
        }
        return VisionEvidenceBundle(
            schema_version="agileforge.vision-evidence.v1",
            items=tuple(items),
            warnings=ordered_warnings,
            evidence_fingerprint=canonical_hash(payload),
        )

    @staticmethod
    def _content_size(content: str | JsonObject) -> int:
        """Measure model-facing content exactly as the evidence contract does."""
        serialized = content if isinstance(content, str) else canonical_json(content)
        return len(serialized.encode("utf-8"))

    @staticmethod
    def _truncate_utf8(content: str, byte_limit: int) -> str:
        """Trim a string to a valid UTF-8 boundary without splitting a character."""
        return content.encode("utf-8")[:byte_limit].decode("utf-8", errors="ignore")

    @staticmethod
    def _warning(code: str, source: str, message: str) -> VisionEvidenceWarning:
        """Build one warning constrained to stable public values."""
        return VisionEvidenceWarning(code=code, source=source, message=message)

    @staticmethod
    def _warning_sort_key(warning: VisionEvidenceWarning) -> tuple[str, str, str]:
        """Sort warning fields according to the public deterministic contract."""
        return warning.source, warning.code, warning.message


__all__ = [
    "VisionEvidenceCollection",
    "VisionEvidenceCollectionError",
    "VisionEvidenceCollector",
    "VisionEvidenceErrorCode",
]
