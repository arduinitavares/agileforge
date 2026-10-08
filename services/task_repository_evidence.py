# services/task_repository_evidence.py
"""Prepare live Task repository evidence before its writer transaction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from models.core import Project
from models.repository import RepositoryBinding, repository_binding_fingerprint
from services.contracts.task_repository_evidence import (
    CapturedTaskRepositoryEvidence,
    UnavailableTaskRepositoryEvidence,
    UnboundTaskRepositoryEvidence,
)
from services.repository_probe import RepositoryProbeError, RepositoryProbeErrorCode
from workflow.execution_integrity import (
    ExecutionIntegrityError,
    validate_task_repository_evidence_binding,
)

if TYPE_CHECKING:
    from sqlmodel import Session

    from services.contracts.task_repository_evidence import TaskRepositoryEvidence
    from services.repository_probe import TaskRepositoryProbe

_VERSION: Literal["agileforge.task-repository-evidence.v1"] = (
    "agileforge.task-repository-evidence.v1"
)
_NEW_KEY: str = "A changed request requires a new idempotency key."
_MAX_DIRTY_PATHS: int = 50
_UNUSABLE_EXPLICIT_PATH_ERRORS: frozenset[RepositoryProbeErrorCode] = frozenset(
    {
        RepositoryProbeErrorCode.PATH_MISSING,
        RepositoryProbeErrorCode.PATH_NOT_DIRECTORY,
        RepositoryProbeErrorCode.NOT_GIT_WORKTREE,
        RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE,
        RepositoryProbeErrorCode.MALFORMED_PATH,
    }
)
type TaskRepositoryRefusalRule = Literal[
    "REPOSITORY_BINDING_CHANGED",
    "WORKTREE_REQUIRES_BINDING",
    "WORKTREE_REPOSITORY_MISMATCH",
    "REPOSITORY_REVISION_CHANGED",
]


class TaskRepositoryEvidenceError(RuntimeError):
    """A repository identity, revision, or acceptance-policy conflict."""


class TaskRepositoryVerificationTimeout(TaskRepositoryEvidenceError):  # noqa: N818
    """Retryable verification failure requiring the transaction owner to roll back."""

    retryable: bool = True

    def __init__(self) -> None:
        """Keep retry guidance compatible with the existing refusal envelope."""
        super().__init__(
            "REPOSITORY_VERIFICATION_TIMEOUT: Repository verification timed out. "
            "Retry the unchanged request with the same idempotency key. "
            f"{_NEW_KEY}"
        )


@dataclass(frozen=True)
class TaskRepositoryRefusal:
    """Closed rule and safe detail deferred until the receipt is claimed."""

    rule: TaskRepositoryRefusalRule
    detail: str

    @property
    def message(self) -> str:
        """Explain the observed conflict and changed-request replay boundary."""
        return f"{self.rule}: {self.detail} {_NEW_KEY}"


@dataclass(frozen=True)
class PreparedTaskRepositoryEvidence:
    """Server-owned successful observation or deferred transactional refusal."""

    project_id: int
    evidence: TaskRepositoryEvidence | None
    refusal: TaskRepositoryRefusal | None = None
    explicit_worktree: bool = False


def _refusal(
    project_id: int, rule: TaskRepositoryRefusalRule, detail: str
) -> PreparedTaskRepositoryEvidence:
    """Carry a preparation conflict into the normal durable receipt path."""
    return PreparedTaskRepositoryEvidence(
        project_id=project_id,
        evidence=None,
        refusal=TaskRepositoryRefusal(rule=rule, detail=detail),
    )


def _binding(session: Session, project_id: int) -> RepositoryBinding | None:
    """Load only the Project's current immutable binding and check ownership."""
    project = session.get(Project, project_id)
    if project is None:
        message = "Project repository identity is missing."
        raise TaskRepositoryEvidenceError(message)
    binding_id = project.active_repository_binding_id
    if binding_id is None:
        return None
    binding = session.get(RepositoryBinding, binding_id)
    if binding is None or binding.project_id != project_id:
        message = "Active repository binding identity is invalid."
        raise TaskRepositoryEvidenceError(message)
    return binding


def _unavailable(
    evidence: CapturedTaskRepositoryEvidence | UnavailableTaskRepositoryEvidence,
    error: RepositoryProbeError,
    *,
    explicit_worktree: bool = False,
) -> UnavailableTaskRepositoryEvidence:
    """Retain target identity with a closed safe message and no invented state."""
    code = (
        RepositoryProbeErrorCode.WORKTREE_PATH_UNUSABLE
        if explicit_worktree and error.code in _UNUSABLE_EXPLICIT_PATH_ERRORS
        else error.code
    )
    return UnavailableTaskRepositoryEvidence(
        version=_VERSION,
        state="unavailable",
        repository_binding_id=evidence.repository_binding_id,
        repository_binding_fingerprint=evidence.repository_binding_fingerprint,
        worktree_path=evidence.worktree_path,
        probed_path_matches_binding=evidence.probed_path_matches_binding,
        uncommitted_acknowledged=evidence.uncommitted_acknowledged,
        reason_code="REPOSITORY_UNAVAILABLE",
        probe_error_code=code,
        error_summary=str(RepositoryProbeError(code, ""))[:240],
    )


def prepare_task_repository_evidence(
    session: Session,
    *,
    project_id: int,
    repository_probe: TaskRepositoryProbe,
    worktree_path: str | None,
    uncommitted: bool,
) -> PreparedTaskRepositoryEvidence:
    """Capture full status and topology outside the workflow writer lock."""
    try:
        binding = _binding(session, project_id)
    except TaskRepositoryEvidenceError as error:
        return _refusal(project_id, "REPOSITORY_BINDING_CHANGED", str(error))
    if binding is None:
        if worktree_path is not None:
            return _refusal(
                project_id,
                "WORKTREE_REQUIRES_BINDING",
                "An explicit worktree requires an active Repository Binding.",
            )
        return PreparedTaskRepositoryEvidence(
            project_id=project_id,
            evidence=UnboundTaskRepositoryEvidence(
                version=_VERSION,
                state="not_bound",
                uncommitted_acknowledged=uncommitted,
            ),
        )
    return _prepare_bound_evidence(
        binding,
        repository_probe=repository_probe,
        worktree_path=worktree_path,
        uncommitted=uncommitted,
    )


def _prepare_bound_evidence(
    binding: RepositoryBinding,
    *,
    repository_probe: TaskRepositoryProbe,
    worktree_path: str | None,
    uncommitted: bool,
) -> PreparedTaskRepositoryEvidence:
    """Capture one selected member of a verified Project-owned binding."""
    project_id = binding.project_id
    binding_id = binding.repository_binding_id
    if binding_id is None:
        return _refusal(
            project_id, "REPOSITORY_BINDING_CHANGED", "Binding has no durable identity."
        )
    target = binding.worktree_path if worktree_path is None else worktree_path
    explicit_worktree = worktree_path is not None
    bound_target = binding.worktree_path
    normalization_error: RepositoryProbeError | None = None
    try:
        target = str(Path(target).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        target = "<malformed-path>"
        normalization_error = RepositoryProbeError(
            RepositoryProbeErrorCode.MALFORMED_PATH, target
        )
    identity = UnavailableTaskRepositoryEvidence(
        version=_VERSION,
        state="unavailable",
        repository_binding_id=binding_id,
        repository_binding_fingerprint=repository_binding_fingerprint(binding),
        worktree_path=target,
        probed_path_matches_binding=target == bound_target,
        uncommitted_acknowledged=uncommitted,
        reason_code="REPOSITORY_UNAVAILABLE",
        probe_error_code=RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE,
        error_summary="Git metadata could not be read.",
    )
    if normalization_error is not None:
        return PreparedTaskRepositoryEvidence(
            project_id=project_id,
            evidence=_unavailable(
                identity, normalization_error, explicit_worktree=explicit_worktree
            ),
            explicit_worktree=explicit_worktree,
        )
    try:
        common_git_dir = repository_probe.inspect_common_git_dir(target)
        common_git_dir_matches = (
            str(Path(common_git_dir).resolve()) == binding.common_git_dir
        )
        observed = repository_probe.inspect(target) if common_git_dir_matches else None
        if (
            observed is None
            or str(Path(observed.common_git_dir).resolve()) != binding.common_git_dir
        ):
            return _refusal(
                project_id,
                "WORKTREE_REPOSITORY_MISMATCH",
                "Selected worktree does not belong to the bound repository.",
            )
        other_worktrees = (
            repository_probe.has_other_worktrees(observed.worktree_path)
            if worktree_path is None
            else False
        )
    except RepositoryProbeError as error:
        if error.code is RepositoryProbeErrorCode.REPOSITORY_CHANGED_DURING_PROBE:
            return _refusal(
                project_id,
                "REPOSITORY_REVISION_CHANGED",
                "Repository HEAD changed during preparation.",
            )
        return PreparedTaskRepositoryEvidence(
            project_id=project_id,
            evidence=_unavailable(identity, error, explicit_worktree=explicit_worktree),
            explicit_worktree=explicit_worktree,
        )
    paths = tuple(sorted({entry.path for entry in observed.status_entries}))
    evidence = CapturedTaskRepositoryEvidence(
        **identity.model_dump(
            exclude={
                "state",
                "reason_code",
                "probe_error_code",
                "error_summary",
                "worktree_path",
                "probed_path_matches_binding",
            }
        ),
        state="captured",
        worktree_path=observed.worktree_path,
        probed_path_matches_binding=observed.worktree_path == bound_target,
        common_git_dir=observed.common_git_dir,
        head_sha=observed.head_sha,
        branch_name=observed.branch_name,
        detached_head=observed.detached_head,
        dirty=observed.dirty,
        dirty_path_count=len(paths),
        dirty_paths=paths[:_MAX_DIRTY_PATHS],
        dirty_paths_truncated=len(paths) > _MAX_DIRTY_PATHS,
        other_worktrees_present=other_worktrees,
        status_fingerprint=observed.status_fingerprint,
        probe_version=observed.probe_version,
        inspected_at=observed.inspected_at,
    )
    return PreparedTaskRepositoryEvidence(
        project_id=project_id,
        evidence=evidence,
        explicit_worktree=explicit_worktree,
    )


def verify_task_repository_evidence(
    session: Session,
    *,
    project_id: int,
    prepared: PreparedTaskRepositoryEvidence | None,
    repository_probe: TaskRepositoryProbe,
    uncommitted: bool = False,
) -> TaskRepositoryEvidence:
    """Verify active binding and HEAD/dirty only inside the writer transaction."""
    evidence = _verify_prepared_binding(
        session, project_id=project_id, prepared=prepared, uncommitted=uncommitted
    )
    if evidence.uncommitted_acknowledged is not uncommitted:
        message = (
            "UNCOMMITTED_ACKNOWLEDGEMENT_MISMATCH: Prepared acknowledgement "
            f"does not agree with the completion command. {_NEW_KEY}"
        )
        raise TaskRepositoryEvidenceError(message)
    if not isinstance(evidence, CapturedTaskRepositoryEvidence):
        return evidence
    try:
        revision = repository_probe.inspect_revision(evidence.worktree_path)
    except RepositoryProbeError as error:
        if error.code is RepositoryProbeErrorCode.PROBE_TIMED_OUT:
            raise TaskRepositoryVerificationTimeout() from error
        if error.code is not RepositoryProbeErrorCode.REPOSITORY_CHANGED_DURING_PROBE:
            return _unavailable(
                evidence,
                error,
                explicit_worktree=prepared is not None and prepared.explicit_worktree,
            )
        message = (
            "REPOSITORY_REVISION_CHANGED: Repository HEAD changed during verification. "
            f"{_NEW_KEY}"
        )
        raise TaskRepositoryEvidenceError(message) from error
    if revision.head_sha != evidence.head_sha or revision.dirty != evidence.dirty:
        message = (
            "REPOSITORY_REVISION_CHANGED: Captured HEAD or dirty state changed. "
            f"{_NEW_KEY}"
        )
        raise TaskRepositoryEvidenceError(message)
    return evidence


def _verify_prepared_binding(
    session: Session,
    *,
    project_id: int,
    prepared: PreparedTaskRepositoryEvidence | None,
    uncommitted: bool,
) -> TaskRepositoryEvidence:
    """Verify the captured binding without resolving or probing filesystem paths."""
    if prepared is not None and prepared.refusal is not None:
        raise TaskRepositoryEvidenceError(prepared.refusal.message)
    try:
        binding = _binding(session, project_id)
    except TaskRepositoryEvidenceError as error:
        message = f"REPOSITORY_BINDING_CHANGED: {error} {_NEW_KEY}"
        raise TaskRepositoryEvidenceError(message) from error
    if prepared is None:
        if binding is not None:
            message = (
                "REPOSITORY_EVIDENCE_PREPARATION_REQUIRED: Bound completion "
                f"requires outside preparation. {_NEW_KEY}"
            )
            raise TaskRepositoryEvidenceError(message)
        return UnboundTaskRepositoryEvidence(
            version=_VERSION, state="not_bound", uncommitted_acknowledged=uncommitted
        )
    try:
        return _validate_prepared_binding(
            prepared, project_id=project_id, binding=binding
        )
    except ExecutionIntegrityError as error:
        message = f"REPOSITORY_BINDING_CHANGED: {error} {_NEW_KEY}"
        raise TaskRepositoryEvidenceError(message) from error


def _validate_prepared_binding(
    prepared: PreparedTaskRepositoryEvidence,
    *,
    project_id: int,
    binding: RepositoryBinding | None,
) -> TaskRepositoryEvidence:
    """Reject any difference in prepared Project or immutable binding identity."""
    evidence = prepared.evidence
    if prepared.project_id != project_id or evidence is None:
        message = "Prepared Project repository identity is invalid."
        raise ExecutionIntegrityError(message)
    if isinstance(evidence, UnboundTaskRepositoryEvidence):
        if binding is not None:
            message = "An active Repository Binding was added."
            raise ExecutionIntegrityError(message)
    else:
        validate_task_repository_evidence_binding(
            evidence, project_id=project_id, binding=binding
        )
    return evidence


def validate_task_repository_policy(
    evidence: TaskRepositoryEvidence,
    *,
    acceptance_result: Literal["partially_met", "fully_met"],
) -> None:
    """Require explicit intent for full acceptance beyond a known clean revision."""
    if acceptance_result != "fully_met" or evidence.uncommitted_acknowledged:
        return
    if isinstance(evidence, CapturedTaskRepositoryEvidence) and evidence.dirty:
        message = (
            "UNCOMMITTED_ACKNOWLEDGEMENT_REQUIRED: Full acceptance of a dirty "
            f"worktree requires --uncommitted / API uncommitted=true. {_NEW_KEY}"
        )
        raise TaskRepositoryEvidenceError(message)
    if isinstance(evidence, UnavailableTaskRepositoryEvidence):
        message = (
            "REPOSITORY_UNAVAILABLE_ACKNOWLEDGEMENT_REQUIRED: Full acceptance "
            "with unavailable repository evidence requires --uncommitted / "
            "API uncommitted=true. "
            f"{_NEW_KEY}"
        )
        raise TaskRepositoryEvidenceError(message)


def task_repository_warnings(
    evidence: TaskRepositoryEvidence,
) -> tuple[list[str], list[str]]:
    """Return presentation codes and safe explanations outside canonical facts."""
    if isinstance(evidence, UnboundTaskRepositoryEvidence):
        return ["REPOSITORY_NOT_BOUND"], ["No repository was bound at Task completion."]
    if isinstance(evidence, UnavailableTaskRepositoryEvidence):
        return ["REPOSITORY_UNAVAILABLE"], [
            "Repository evidence was unavailable at Task completion: "
            + evidence.error_summary
        ]
    codes: list[str] = []
    messages: list[str] = []
    if evidence.dirty:
        codes.append("UNCOMMITTED_WORKTREE")
        messages.append("Worktree changes are outside the recorded HEAD commit.")
    if evidence.detached_head:
        codes.append("DETACHED_HEAD")
        messages.append("Completion observed a detached HEAD commit.")
    if evidence.other_worktrees_present:
        codes.append("OTHER_WORKTREES_PRESENT")
        messages.append("Other worktrees exist; only the bound checkout was inspected.")
    return codes, messages
