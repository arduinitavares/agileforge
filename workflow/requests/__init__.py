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
from workflow.requests.planning import (
    ApplyStoryDependencies,
    DecideRoadmap,
    DecideSprintPlan,
    DecideStory,
    RecordRoadmapDraft,
    RecordSprintPlan,
    RecordStoryDraft,
    RepairStoryReadiness,
    StartSprint,
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
    | RecordRoadmapDraft
    | DecideRoadmap
    | RecordStoryDraft
    | DecideStory
    | ApplyStoryDependencies
    | RepairStoryReadiness
    | RecordSprintPlan
    | DecideSprintPlan
    | StartSprint
)

__all__ = [
    "AbandonProjectShell",
    "ApplyStoryDependencies",
    "CompileAuthority",
    "DecideAuthority",
    "DecideBacklog",
    "DecideBrownfieldInitialSpec",
    "DecideInitialSpecDraft",
    "DecidePrd",
    "DecideRoadmap",
    "DecideSprintPlan",
    "DecideStory",
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
    "RecordRoadmapDraft",
    "RecordSprintPlan",
    "RecordStoryDraft",
    "RecordVisionDraft",
    "RegisterInitialScope",
    "RepairAuthority",
    "RepairStoryReadiness",
    "StartSprint",
    "TransitionRequest",
]
