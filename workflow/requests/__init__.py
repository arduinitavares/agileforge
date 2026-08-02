"""Guarded request contracts for workflow graph transitions."""

from workflow.requests.authority import (
    CompileAuthority,
    DecideAuthority,
    RecordAuthorityFeedback,
    RepairAuthority,
)
from workflow.requests.onboarding import (
    DecideBrownfieldInitialSpec,
    DecideInitialSpecDraft,
    DecidePrd,
    RecordBrownfieldSpecDraft,
    RecordChallengeArtifact,
    RecordInitialSpecDraft,
    RecordPrdVersion,
    RecordRepositoryBaseline,
    RecordRepositoryInventory,
    RegisterInitialScope,
)
from workflow.requests.product_definition import (
    DecideBacklog,
    DecideVision,
    ReconcileBacklog,
    RecordBacklogDraft,
    RecordVisionDraft,
)
from workflow.requests.project_shell import AbandonProjectShell, OpenProjectShell

type TransitionRequest = (
    OpenProjectShell
    | AbandonProjectShell
    | RecordChallengeArtifact
    | RecordRepositoryBaseline
    | RecordRepositoryInventory
    | RecordBrownfieldSpecDraft
    | DecideBrownfieldInitialSpec
    | RecordPrdVersion
    | DecidePrd
    | RecordInitialSpecDraft
    | DecideInitialSpecDraft
    | RegisterInitialScope
    | CompileAuthority
    | DecideAuthority
    | RecordAuthorityFeedback
    | RepairAuthority
    | RecordVisionDraft
    | DecideVision
    | RecordBacklogDraft
    | DecideBacklog
    | ReconcileBacklog
)

__all__ = [
    "AbandonProjectShell",
    "CompileAuthority",
    "DecideAuthority",
    "DecideBacklog",
    "DecideBrownfieldInitialSpec",
    "DecideInitialSpecDraft",
    "DecidePrd",
    "DecideVision",
    "OpenProjectShell",
    "ReconcileBacklog",
    "RecordAuthorityFeedback",
    "RecordBacklogDraft",
    "RecordBrownfieldSpecDraft",
    "RecordChallengeArtifact",
    "RecordInitialSpecDraft",
    "RecordPrdVersion",
    "RecordRepositoryBaseline",
    "RecordRepositoryInventory",
    "RecordVisionDraft",
    "RegisterInitialScope",
    "RepairAuthority",
    "TransitionRequest",
]
