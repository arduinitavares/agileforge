"""Safely capture exact repository bytes for Specification source registration."""

from __future__ import annotations

import base64
import hashlib
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, Self

from pydantic import Field, field_validator, model_validator
from sqlmodel import Session

from models.core import Project
from models.repository import RepositoryBinding, repository_binding_fingerprint
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services.contracts.specification_source import (
    SPECIFICATION_SOURCE_CONTEXT_ID,
    SPECIFICATION_SOURCE_MAX_BUNDLE_BYTES,
    SPECIFICATION_SOURCE_MAX_DOCUMENT_BYTES,
    SPECIFICATION_SOURCE_PRIMARY_ID,
    SpecificationContextCapture,
    SpecificationRepositoryRevision,
    SpecificationSourceBundle,
    SpecificationSourceDocument,
    source_bundle_fingerprint,
    specification_source_adr_id,
)
from services.repository_probe import RepositoryProbeError
from services.specification_source_windows import (
    UnsafeWindowsSourceError,
    WindowsSpecificationSourceWorktree,
    open_windows_source_worktree,
)
from services.vision_evidence_reader import (
    RepositoryEvidenceCapability,
    RepositoryEvidenceCapabilityError,
    RepositoryEvidenceChangedError,
)
from workflow.contracts import FrozenModel
from workflow.definitions.product_goal import (
    accepted_current_goal,
    accepted_current_vision,
)
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine

    from services.repository_probe import RepositoryProbe, RepositoryProbeResult
    from workflow.facts import ProductGoalArtifactFact, VisionArtifactFact

MAX_SPECIFICATION_SOURCE_DOCUMENT_BYTES: int = SPECIFICATION_SOURCE_MAX_DOCUMENT_BYTES
MAX_SPECIFICATION_SOURCE_TOTAL_BYTES: int = SPECIFICATION_SOURCE_MAX_BUNDLE_BYTES
_CONTEXT_PATH = "CONTEXT.md"


class SpecificationSourceRegistrationErrorCode(StrEnum):
    """Closed failures emitted by exact-byte source preparation."""

    PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
    ACCEPTED_LINEAGE_REQUIRED = "ACCEPTED_LINEAGE_REQUIRED"
    REPOSITORY_BINDING_REQUIRED = "REPOSITORY_BINDING_REQUIRED"
    REPOSITORY_PROVENANCE_STALE = "REPOSITORY_PROVENANCE_STALE"
    REPOSITORY_CHANGED_DURING_CAPTURE = "REPOSITORY_CHANGED_DURING_CAPTURE"
    SOURCE_LINEAGE_CHANGED = "SOURCE_LINEAGE_CHANGED"
    CAPABILITY_UNAVAILABLE = "REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE"
    SOURCE_MISSING = "SOURCE_MISSING"
    UNSAFE_FILE = "UNSAFE_FILE"
    INVALID_UTF8 = "INVALID_UTF8"
    SOURCE_TOO_LARGE = "SOURCE_TOO_LARGE"
    SOURCE_CHANGED_DURING_CAPTURE = "SOURCE_CHANGED_DURING_CAPTURE"
    DUPLICATE_SOURCE = "DUPLICATE_SOURCE"


class SpecificationSourceRegistrationError(RuntimeError):
    """One typed failure that prevents a source bundle from being registered."""

    def __init__(
        self,
        code: SpecificationSourceRegistrationErrorCode,
        message: str,
    ) -> None:
        """Retain the stable closed code with one bounded diagnostic."""
        self.code = code
        super().__init__(message)


def _canonical_relative_path(value: str) -> str:
    """Require an exact repository-relative POSIX path without aliases."""
    if not value or "\x00" in value or "\\" in value:
        message = "Source paths must be non-empty repository-relative POSIX paths."
        raise ValueError(message)
    candidate = PurePosixPath(value)
    if (
        candidate.is_absolute()
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or candidate.as_posix() != value
    ):
        message = "Source paths must be canonical and may not traverse the repository."
        raise ValueError(message)
    return value


class SpecificationSourceRegistrationRequest(FrozenModel):
    """Caller-owned source selection without host-derived identity or bytes."""

    project_id: int = Field(gt=0)
    source_path: str = Field(min_length=1)
    preparation_capability: Literal["grill-with-docs"]
    adr_paths: tuple[str, ...] = ()
    expected_decision_fingerprint: str | None = Field(
        default=None,
        min_length=1,
        exclude=True,
        repr=False,
    )
    idempotency_key: str = Field(min_length=1, max_length=200)
    actor: str = Field(min_length=1, max_length=200)
    correlation_id: str | None = Field(default=None, min_length=1)

    @field_validator("source_path")
    @classmethod
    def validate_source_path(cls, value: str) -> str:
        """Reject paths whose spelling changes their repository identity."""
        return _canonical_relative_path(value)

    @field_validator("adr_paths")
    @classmethod
    def canonicalize_adr_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Validate and sort the set-like applicable ADR selection."""
        paths = tuple(_canonical_relative_path(item) for item in value)
        if any(
            not path.startswith("docs/adr/") or not path.endswith(".md")
            for path in paths
        ):
            message = "Applicable ADRs must be Markdown files under docs/adr/."
            raise ValueError(message)
        return tuple(sorted(paths))

    @model_validator(mode="after")
    def validate_unique_roles(self) -> Self:
        """Prevent one lexical path from receiving multiple source roles."""
        paths = (self.source_path, _CONTEXT_PATH, *self.adr_paths)
        if len(paths) != len(set(paths)):
            message = "Source, Context, and ADR paths must be distinct."
            raise ValueError(message)
        return self

    def semantic_fingerprint(self) -> str:
        """Hash only stable caller semantics, excluding captured repository state."""
        return canonical_hash(
            {
                "kind": "register_specification_source",
                "project_id": self.project_id,
                "source_path": self.source_path,
                "preparation_capability": self.preparation_capability,
                "adr_paths": self.adr_paths,
                "actor": self.actor,
                "correlation_id": self.correlation_id,
            }
        )


@dataclass(frozen=True)
class PreparedSpecificationSourceRegistration:
    """Host-derived exact bundle plus durable identities required for persistence."""

    project_id: int
    accepted_vision_artifact_id: int
    accepted_product_goal_artifact_id: int
    repository_binding_id: int
    repository_binding_fingerprint: str
    request_fingerprint: str
    source_fingerprint: str
    bundle: SpecificationSourceBundle


@dataclass(frozen=True)
class _DurableSourceContext:
    """Exact current lineage and repository selection loaded in one DB snapshot."""

    project_id: int
    vision_artifact_id: int
    vision_fingerprint: str
    product_goal_artifact_id: int
    product_goal_fingerprint: str
    repository_binding_id: int
    repository_binding_fingerprint: str
    worktree_path: str
    common_git_dir: str
    head_sha: str
    branch_name: str | None
    detached_head: bool
    dirty: bool
    status_fingerprint: str
    remotes_json: str


@dataclass(frozen=True)
class _CapturedDocument:
    """Exact bytes and open-file identity for one selected repository file."""

    document: SpecificationSourceDocument
    device: int
    inode: int

    @property
    def physical_identity(self) -> tuple[int, int]:
        return self.device, self.inode


@dataclass(frozen=True)
class SpecificationSourceRegistrationService:
    """Prepare one canonical source bundle from current durable Project facts."""

    engine: Engine
    repository_probe: RepositoryProbe

    def capability(self, project_id: int) -> RepositoryEvidenceCapability:
        """Probe the source root without reading documents or hiding stale bindings."""
        context = self._load_context(project_id)
        self._verify_provenance(context)
        try:
            with _source_root(context.worktree_path):
                pass
        except SpecificationSourceRegistrationError as error:
            if error.code is not (
                SpecificationSourceRegistrationErrorCode.CAPABILITY_UNAVAILABLE
            ):
                raise
            self._verify_provenance(context)
            return RepositoryEvidenceCapability(
                available=False,
                code="REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE",
                message=str(error),
            )
        return RepositoryEvidenceCapability(available=True)

    def _verify_provenance(
        self, context: _DurableSourceContext
    ) -> RepositoryProbeResult:
        try:
            observed = self.repository_probe.inspect(context.worktree_path)
        except RepositoryProbeError as error:
            raise SpecificationSourceRegistrationError(
                SpecificationSourceRegistrationErrorCode.REPOSITORY_PROVENANCE_STALE,
                "Repository provenance cannot be inspected for source capture.",
            ) from error
        if not _probe_matches_context(observed, context):
            raise SpecificationSourceRegistrationError(
                SpecificationSourceRegistrationErrorCode.REPOSITORY_PROVENANCE_STALE,
                "Repository provenance differs from the active binding.",
            )
        return observed

    def verify_prepared(
        self,
        prepared: PreparedSpecificationSourceRegistration,
        /,
    ) -> SpecificationSourceRegistrationError | None:
        """Re-capture a prepared selection at the final write boundary."""
        bundle = prepared.bundle
        try:
            current = self.prepare(
                SpecificationSourceRegistrationRequest(
                    project_id=prepared.project_id,
                    source_path=bundle.source.relative_path,
                    preparation_capability=bundle.preparation_capability,
                    adr_paths=tuple(item.relative_path for item in bundle.adrs),
                    idempotency_key="specification-source-write-verification",
                    actor="specification-source-write-verification",
                    correlation_id=None,
                )
            )
        except SpecificationSourceRegistrationError as error:
            return error
        if (
            current.project_id,
            current.accepted_vision_artifact_id,
            current.accepted_product_goal_artifact_id,
            current.repository_binding_id,
            current.repository_binding_fingerprint,
            current.source_fingerprint,
            current.bundle,
        ) != (
            prepared.project_id,
            prepared.accepted_vision_artifact_id,
            prepared.accepted_product_goal_artifact_id,
            prepared.repository_binding_id,
            prepared.repository_binding_fingerprint,
            prepared.source_fingerprint,
            prepared.bundle,
        ):
            return SpecificationSourceRegistrationError(
                SpecificationSourceRegistrationErrorCode.SOURCE_CHANGED_DURING_CAPTURE,
                "Prepared Specification source changed before persistence.",
            )
        return None

    def prepare(
        self,
        request: SpecificationSourceRegistrationRequest,
    ) -> PreparedSpecificationSourceRegistration:
        """Capture exact bytes between stable probes and recheck durable lineage."""
        context = self._load_context(request.project_id)
        before = self._verify_provenance(context)

        source, context_capture, adrs = _capture_selected_documents(
            worktree_path=context.worktree_path,
            source_path=request.source_path,
            adr_paths=request.adr_paths,
        )
        try:
            middle = self.repository_probe.inspect(context.worktree_path)
        except RepositoryProbeError as error:
            raise SpecificationSourceRegistrationError(
                SpecificationSourceRegistrationErrorCode.REPOSITORY_CHANGED_DURING_CAPTURE,
                "Repository provenance changed while source bytes were captured.",
            ) from error
        if _probe_identity(middle) != _probe_identity(before):
            raise SpecificationSourceRegistrationError(
                SpecificationSourceRegistrationErrorCode.REPOSITORY_CHANGED_DURING_CAPTURE,
                "Repository provenance changed while source bytes were captured.",
            )
        verified_source, verified_context, verified_adrs = _capture_selected_documents(
            worktree_path=context.worktree_path,
            source_path=request.source_path,
            adr_paths=request.adr_paths,
        )
        try:
            after = self.repository_probe.inspect(context.worktree_path)
        except RepositoryProbeError as error:
            raise SpecificationSourceRegistrationError(
                SpecificationSourceRegistrationErrorCode.REPOSITORY_CHANGED_DURING_CAPTURE,
                "Repository provenance changed while source bytes were verified.",
            ) from error
        if _probe_identity(after) != _probe_identity(before):
            raise SpecificationSourceRegistrationError(
                SpecificationSourceRegistrationErrorCode.REPOSITORY_CHANGED_DURING_CAPTURE,
                "Repository provenance changed while source bytes were verified.",
            )
        if not _probe_matches_context(after, context):
            raise SpecificationSourceRegistrationError(
                SpecificationSourceRegistrationErrorCode.REPOSITORY_CHANGED_DURING_CAPTURE,
                "Repository provenance no longer matches the active binding.",
            )
        if (source, context_capture, adrs) != (
            verified_source,
            verified_context,
            verified_adrs,
        ):
            raise SpecificationSourceRegistrationError(
                SpecificationSourceRegistrationErrorCode.SOURCE_CHANGED_DURING_CAPTURE,
                "Selected source, Context, or ADR bytes changed during capture.",
            )
        if self._load_context(request.project_id) != context:
            raise SpecificationSourceRegistrationError(
                SpecificationSourceRegistrationErrorCode.SOURCE_LINEAGE_CHANGED,
                "Vision, Product Goal, or repository selection changed during capture.",
            )

        bundle = SpecificationSourceBundle(
            producer_capability="to-spec",
            preparation_capability=request.preparation_capability,
            source=verified_source,
            context=verified_context,
            adrs=verified_adrs,
            repository_revision=SpecificationRepositoryRevision(
                head_sha=before.head_sha,
                branch_name=before.branch_name,
                detached_head=before.detached_head,
                dirty=before.dirty,
                status_entries=before.status_entries,
                status_fingerprint=before.status_fingerprint,
                remotes=before.remotes,
                probe_version=before.probe_version,
                warnings=before.warnings,
            ),
            accepted_vision_fingerprint=context.vision_fingerprint,
            accepted_product_goal_fingerprint=context.product_goal_fingerprint,
        )
        return PreparedSpecificationSourceRegistration(
            project_id=request.project_id,
            accepted_vision_artifact_id=context.vision_artifact_id,
            accepted_product_goal_artifact_id=context.product_goal_artifact_id,
            repository_binding_id=context.repository_binding_id,
            repository_binding_fingerprint=context.repository_binding_fingerprint,
            request_fingerprint=request.semantic_fingerprint(),
            source_fingerprint=source_bundle_fingerprint(bundle),
            bundle=bundle,
        )

    def _load_context(self, project_id: int) -> _DurableSourceContext:
        try:
            with Session(self.engine) as session:
                return _load_context_in_session(session, project_id)
        except WorkflowFactLoadError as error:
            raise SpecificationSourceRegistrationError(
                SpecificationSourceRegistrationErrorCode.ACCEPTED_LINEAGE_REQUIRED,
                str(error),
            ) from error


def _load_context_in_session(
    session: Session,
    project_id: int,
) -> _DurableSourceContext:
    """Load exact current lineage and repository selection in a caller transaction."""
    project = session.get(Project, project_id)
    if project is None:
        raise SpecificationSourceRegistrationError(
            SpecificationSourceRegistrationErrorCode.PROJECT_NOT_FOUND,
            "Project does not exist.",
        )
    try:
        snapshot = WorkflowFactRepository(session).load(project_id)
    except WorkflowFactLoadError as error:
        raise SpecificationSourceRegistrationError(
            SpecificationSourceRegistrationErrorCode.ACCEPTED_LINEAGE_REQUIRED,
            str(error),
        ) from error
    vision = accepted_current_vision(snapshot)
    goal = accepted_current_goal(snapshot)
    if vision is None or goal is None:
        raise SpecificationSourceRegistrationError(
            SpecificationSourceRegistrationErrorCode.ACCEPTED_LINEAGE_REQUIRED,
            "Source registration requires an accepted Vision and active Goal.",
        )
    binding_id = project.active_repository_binding_id
    if binding_id is None:
        raise SpecificationSourceRegistrationError(
            SpecificationSourceRegistrationErrorCode.REPOSITORY_BINDING_REQUIRED,
            "Source registration requires an active repository binding.",
        )
    binding = session.get(RepositoryBinding, binding_id)
    if binding is None or binding.project_id != project_id:
        raise SpecificationSourceRegistrationError(
            SpecificationSourceRegistrationErrorCode.REPOSITORY_BINDING_REQUIRED,
            "The active repository binding is unavailable.",
        )
    return _durable_context(project_id, vision, goal, binding)


def _durable_context(
    project_id: int,
    vision: VisionArtifactFact,
    goal: ProductGoalArtifactFact,
    binding: RepositoryBinding,
) -> _DurableSourceContext:
    """Copy ORM and fact values before the read transaction closes."""
    binding_id = binding.repository_binding_id
    if binding_id is None:
        raise SpecificationSourceRegistrationError(
            SpecificationSourceRegistrationErrorCode.ACCEPTED_LINEAGE_REQUIRED,
            "Source registration lineage is incomplete.",
        )
    return _DurableSourceContext(
        project_id=project_id,
        vision_artifact_id=vision.vision_artifact_id,
        vision_fingerprint=vision.content_fingerprint,
        product_goal_artifact_id=goal.product_goal_artifact_id,
        product_goal_fingerprint=goal.content_fingerprint,
        repository_binding_id=binding_id,
        repository_binding_fingerprint=repository_binding_fingerprint(binding),
        worktree_path=binding.worktree_path,
        common_git_dir=binding.common_git_dir,
        head_sha=binding.head_sha,
        branch_name=binding.branch_name,
        detached_head=binding.detached_head,
        dirty=binding.dirty,
        status_fingerprint=binding.status_fingerprint,
        remotes_json=binding.remotes_json,
    )


def _probe_matches_context(
    probe: RepositoryProbeResult,
    context: _DurableSourceContext,
) -> bool:
    """Compare one live probe to every stable field in the active binding."""
    expected = (
        context.worktree_path,
        context.common_git_dir,
        context.head_sha,
        context.branch_name,
        context.detached_head,
        context.dirty,
        context.status_fingerprint,
        context.remotes_json,
    )
    return expected == _probe_identity(probe)


def _probe_identity(probe: RepositoryProbeResult) -> tuple[object, ...]:
    """Exclude volatile inspection time while retaining semantic repository state."""
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


def _capture_selected_documents(
    *,
    worktree_path: str,
    source_path: str,
    adr_paths: tuple[str, ...],
) -> tuple[
    SpecificationSourceDocument,
    SpecificationContextCapture,
    tuple[SpecificationSourceDocument, ...],
]:
    """Capture the selected source set from one non-following worktree anchor."""
    captures: list[_CapturedDocument] = []
    total_bytes = 0
    with _source_root(worktree_path) as root_descriptor:
        source_capture = _capture_document(
            root_descriptor,
            relative_path=source_path,
            source_id=SPECIFICATION_SOURCE_PRIMARY_ID,
            required=True,
        )
        source_capture = _require_capture(source_capture)
        captures.append(source_capture)
        total_bytes += source_capture.document.byte_length

        context_capture = _capture_document(
            root_descriptor,
            relative_path=_CONTEXT_PATH,
            source_id=SPECIFICATION_SOURCE_CONTEXT_ID,
            required=False,
        )
        if context_capture is None:
            context = SpecificationContextCapture(state="absent")
        else:
            captures.append(context_capture)
            total_bytes += context_capture.document.byte_length
            context = SpecificationContextCapture(
                state="present",
                document=context_capture.document,
            )

        adr_captures: list[_CapturedDocument] = []
        for relative_path in adr_paths:
            captured = _capture_document(
                root_descriptor,
                relative_path=relative_path,
                source_id=specification_source_adr_id(relative_path),
                required=True,
            )
            captured = _require_capture(captured)
            captures.append(captured)
            adr_captures.append(captured)
            total_bytes += captured.document.byte_length

    if total_bytes > MAX_SPECIFICATION_SOURCE_TOTAL_BYTES:
        raise SpecificationSourceRegistrationError(
            SpecificationSourceRegistrationErrorCode.SOURCE_TOO_LARGE,
            "Registered source bytes exceed the aggregate capture limit.",
        )
    identities = [capture.physical_identity for capture in captures]
    if len(identities) != len(set(identities)):
        raise SpecificationSourceRegistrationError(
            SpecificationSourceRegistrationErrorCode.DUPLICATE_SOURCE,
            "Selected paths resolve to the same physical source file.",
        )
    return (
        source_capture.document,
        context,
        tuple(capture.document for capture in adr_captures),
    )


@contextmanager
def _source_root(
    worktree_path: str,
) -> Iterator[int | WindowsSpecificationSourceWorktree]:
    if os.name == "nt":
        try:
            with open_windows_source_worktree(Path(worktree_path)) as worktree:
                yield worktree
        except UnsafeWindowsSourceError as error:
            raise SpecificationSourceRegistrationError(
                SpecificationSourceRegistrationErrorCode.UNSAFE_FILE,
                str(error),
            ) from error
        except RepositoryEvidenceCapabilityError as error:
            raise SpecificationSourceRegistrationError(
                SpecificationSourceRegistrationErrorCode.CAPABILITY_UNAVAILABLE,
                "Specification capture needs native 64-bit Windows and a local "
                "NTFS/ReFS worktree with safe handle support. "
                "This runtime or filesystem cannot provide that support.",
            ) from error
        except RepositoryEvidenceChangedError as error:
            raise SpecificationSourceRegistrationError(
                SpecificationSourceRegistrationErrorCode.SOURCE_CHANGED_DURING_CAPTURE,
                str(error),
            ) from error
        return
    descriptor = _open_root(worktree_path)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _open_root(worktree_path: str) -> int:
    flags = _required_open_flags(directory=True)
    try:
        return os.open(worktree_path, flags)
    except OSError as error:
        raise SpecificationSourceRegistrationError(
            SpecificationSourceRegistrationErrorCode.UNSAFE_FILE,
            "The repository worktree cannot be opened safely.",
        ) from error


def _capture_document(
    root_descriptor: int | WindowsSpecificationSourceWorktree,
    *,
    relative_path: str,
    source_id: str,
    required: bool,
) -> _CapturedDocument | None:
    if isinstance(root_descriptor, WindowsSpecificationSourceWorktree):
        captured = root_descriptor.capture(
            relative_path, MAX_SPECIFICATION_SOURCE_DOCUMENT_BYTES
        )
        if captured is None:
            if not required:
                return None
            raise SpecificationSourceRegistrationError(
                SpecificationSourceRegistrationErrorCode.SOURCE_MISSING,
                f"Selected source path is missing: {relative_path}",
            )
        return _document_from_bytes(
            captured.content,
            relative_path=relative_path,
            source_id=source_id,
            device=captured.volume,
            inode=captured.file_id,
        )
    return _capture_posix_document(
        root_descriptor,
        relative_path=relative_path,
        source_id=source_id,
        required=required,
    )


def _capture_posix_document(
    root_descriptor: int, *, relative_path: str, source_id: str, required: bool
) -> _CapturedDocument | None:
    parent_descriptor = os.dup(root_descriptor)
    parts = PurePosixPath(relative_path).parts
    try:
        for component in parts[:-1]:
            parent_descriptor = _descend_directory(parent_descriptor, component)
        leaf_name = parts[-1]
        if not _exact_directory_entry(parent_descriptor, leaf_name):
            if not required:
                return None
            raise SpecificationSourceRegistrationError(
                SpecificationSourceRegistrationErrorCode.SOURCE_MISSING,
                f"Selected source path is missing: {relative_path}",
            )
        try:
            current = os.stat(
                leaf_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            if not required:
                return None
            raise SpecificationSourceRegistrationError(
                SpecificationSourceRegistrationErrorCode.SOURCE_MISSING,
                f"Selected source path is missing: {relative_path}",
            ) from None
        except OSError as error:
            raise SpecificationSourceRegistrationError(
                SpecificationSourceRegistrationErrorCode.UNSAFE_FILE,
                f"Selected source path cannot be inspected: {relative_path}",
            ) from error
        if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
            raise SpecificationSourceRegistrationError(
                SpecificationSourceRegistrationErrorCode.UNSAFE_FILE,
                (
                    "Selected source path is not a regular non-symlink file: "
                    f"{relative_path}"
                ),
            )
        if current.st_size > MAX_SPECIFICATION_SOURCE_DOCUMENT_BYTES:
            raise SpecificationSourceRegistrationError(
                SpecificationSourceRegistrationErrorCode.SOURCE_TOO_LARGE,
                f"Selected source exceeds the per-document limit: {relative_path}",
            )
        return _read_open_document(
            parent_descriptor,
            leaf_name=leaf_name,
            relative_path=relative_path,
            source_id=source_id,
            expected=current,
        )
    finally:
        os.close(parent_descriptor)


def _descend_directory(parent_descriptor: int, component: str) -> int:
    """Open one directory component, closing the prior descriptor on success."""
    if not _exact_directory_entry(parent_descriptor, component):
        raise SpecificationSourceRegistrationError(
            SpecificationSourceRegistrationErrorCode.UNSAFE_FILE,
            "Selected source contains a missing directory component.",
        )
    try:
        component_stat = os.stat(
            component,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        raise SpecificationSourceRegistrationError(
            SpecificationSourceRegistrationErrorCode.UNSAFE_FILE,
            "Selected source contains an unsafe directory component.",
        ) from error
    if stat.S_ISLNK(component_stat.st_mode) or not stat.S_ISDIR(component_stat.st_mode):
        raise SpecificationSourceRegistrationError(
            SpecificationSourceRegistrationErrorCode.UNSAFE_FILE,
            "Selected source contains an unsafe directory component.",
        )
    try:
        child = os.open(
            component,
            _required_open_flags(directory=True),
            dir_fd=parent_descriptor,
        )
    except OSError as error:
        raise SpecificationSourceRegistrationError(
            SpecificationSourceRegistrationErrorCode.UNSAFE_FILE,
            "Selected source contains an unsafe directory component.",
        ) from error
    os.close(parent_descriptor)
    return child


def _exact_directory_entry(parent_descriptor: int, component: str) -> bool:
    """Reject filesystem aliases whose spelling differs from the selected path."""
    try:
        if component in os.listdir(parent_descriptor):
            return True
        os.stat(component, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise SpecificationSourceRegistrationError(
            SpecificationSourceRegistrationErrorCode.UNSAFE_FILE,
            "Selected source path cannot be resolved exactly.",
        ) from error
    raise SpecificationSourceRegistrationError(
        SpecificationSourceRegistrationErrorCode.UNSAFE_FILE,
        "Selected source path spelling aliases a different directory entry.",
    )


def _require_capture(value: _CapturedDocument | None) -> _CapturedDocument:
    """Narrow a required capture without an assertion in production code."""
    if value is None:
        raise SpecificationSourceRegistrationError(
            SpecificationSourceRegistrationErrorCode.SOURCE_MISSING,
            "A required selected source is missing.",
        )
    return value


def _read_open_document(
    parent_descriptor: int,
    *,
    leaf_name: str,
    relative_path: str,
    source_id: str,
    expected: os.stat_result,
) -> _CapturedDocument:
    try:
        descriptor = os.open(
            leaf_name,
            _required_open_flags(directory=False),
            dir_fd=parent_descriptor,
        )
    except OSError as error:
        raise SpecificationSourceRegistrationError(
            SpecificationSourceRegistrationErrorCode.UNSAFE_FILE,
            f"Selected source cannot be opened safely: {relative_path}",
        ) from error
    try:
        before = os.fstat(descriptor)
        content = bytearray()
        limit = MAX_SPECIFICATION_SOURCE_DOCUMENT_BYTES + 1
        while len(content) < limit:
            chunk = os.read(descriptor, limit - len(content))
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        current = os.stat(
            leaf_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        raise SpecificationSourceRegistrationError(
            SpecificationSourceRegistrationErrorCode.SOURCE_CHANGED_DURING_CAPTURE,
            f"Selected source changed while it was read: {relative_path}",
        ) from error
    finally:
        os.close(descriptor)
    if not (
        _file_identity(expected) == _file_identity(before)
        and _file_identity(before) == _file_identity(after)
        and _file_identity(before) == _file_identity(current)
        and stat.S_ISREG(before.st_mode)
        and stat.S_ISREG(current.st_mode)
    ):
        raise SpecificationSourceRegistrationError(
            SpecificationSourceRegistrationErrorCode.SOURCE_CHANGED_DURING_CAPTURE,
            f"Selected source changed while it was read: {relative_path}",
        )
    return _document_from_bytes(
        bytes(content),
        relative_path=relative_path,
        source_id=source_id,
        device=before.st_dev,
        inode=before.st_ino,
    )


def _document_from_bytes(
    raw: bytes, *, relative_path: str, source_id: str, device: int, inode: int
) -> _CapturedDocument:
    """Apply the same strict byte contract after either platform's safe read."""
    if len(raw) > MAX_SPECIFICATION_SOURCE_DOCUMENT_BYTES:
        raise SpecificationSourceRegistrationError(
            SpecificationSourceRegistrationErrorCode.SOURCE_TOO_LARGE,
            f"Selected source exceeds the per-document limit: {relative_path}",
        )
    try:
        raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise SpecificationSourceRegistrationError(
            SpecificationSourceRegistrationErrorCode.INVALID_UTF8,
            f"Selected source is not valid UTF-8: {relative_path}",
        ) from error
    fingerprint = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    document = SpecificationSourceDocument(
        source_id=source_id,
        relative_path=relative_path,
        content_base64=base64.b64encode(raw).decode("ascii"),
        byte_length=len(raw),
        content_fingerprint=fingerprint,
    )
    return _CapturedDocument(
        document=document,
        device=device,
        inode=inode,
    )


def _required_open_flags(*, directory: bool) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    nonblock = getattr(os, "O_NONBLOCK", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if not isinstance(no_follow, int) or not isinstance(nonblock, int):
        raise SpecificationSourceRegistrationError(
            SpecificationSourceRegistrationErrorCode.CAPABILITY_UNAVAILABLE,
            "Specification capture requires non-following, non-blocking file opens "
            "on this platform.",
        )
    if directory and not isinstance(directory_flag, int):
        raise SpecificationSourceRegistrationError(
            SpecificationSourceRegistrationErrorCode.CAPABILITY_UNAVAILABLE,
            "Specification capture requires safe directory-descriptor traversal "
            "on this platform.",
        )
    flags = os.O_RDONLY | no_follow | nonblock
    return flags | directory_flag if directory else flags


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
    )


__all__ = [
    "MAX_SPECIFICATION_SOURCE_DOCUMENT_BYTES",
    "MAX_SPECIFICATION_SOURCE_TOTAL_BYTES",
    "PreparedSpecificationSourceRegistration",
    "SpecificationSourceRegistrationError",
    "SpecificationSourceRegistrationErrorCode",
    "SpecificationSourceRegistrationRequest",
    "SpecificationSourceRegistrationService",
]
