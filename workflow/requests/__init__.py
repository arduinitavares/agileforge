"""Guarded request contracts for workflow graph transitions."""

from workflow.requests.onboarding import (
    DecideInitialSpecDraft,
    DecidePrd,
    RecordChallengeArtifact,
    RecordInitialSpecDraft,
    RecordPrdVersion,
    RegisterInitialScope,
)
from workflow.requests.project_shell import AbandonProjectShell, OpenProjectShell

type TransitionRequest = (
    OpenProjectShell
    | AbandonProjectShell
    | RecordChallengeArtifact
    | RecordPrdVersion
    | DecidePrd
    | RecordInitialSpecDraft
    | DecideInitialSpecDraft
    | RegisterInitialScope
)

__all__ = [
    "AbandonProjectShell",
    "DecideInitialSpecDraft",
    "DecidePrd",
    "OpenProjectShell",
    "RecordChallengeArtifact",
    "RecordInitialSpecDraft",
    "RecordPrdVersion",
    "RegisterInitialScope",
    "TransitionRequest",
]
