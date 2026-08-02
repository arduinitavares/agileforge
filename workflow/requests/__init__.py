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
)

__all__ = [
    "AbandonProjectShell",
    "CompileAuthority",
    "DecideAuthority",
    "DecideBrownfieldInitialSpec",
    "DecideInitialSpecDraft",
    "DecidePrd",
    "OpenProjectShell",
    "RecordAuthorityFeedback",
    "RecordBrownfieldSpecDraft",
    "RecordChallengeArtifact",
    "RecordInitialSpecDraft",
    "RecordPrdVersion",
    "RecordRepositoryBaseline",
    "RecordRepositoryInventory",
    "RegisterInitialScope",
    "RepairAuthority",
    "TransitionRequest",
]
