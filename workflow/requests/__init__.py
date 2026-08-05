"""Guarded request contracts for workflow graph transitions."""

from workflow.requests.attempts import FailNodeAttempt, StartNodeAttempt
from workflow.requests.authority import (
    CompileAuthority,
    DecideAuthority,
    RecordAuthorityFeedback,
    RepairAuthority,
)
from workflow.requests.execution import (
    CloseSprint,
    CloseStory,
    CompleteTask,
    RecordPostSprintTriage,
    ReviewSprint,
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
from workflow.requests.scope_extension import (
    AbandonScopeExtension,
    DecideAmendmentSpecDraft,
    DecideExtensionPrd,
    ReconcileScopeExtension,
    RecordAmendmentSpecDraft,
    RecordExtensionChallenge,
    RecordExtensionPrd,
    RegisterScopeExtension,
    ScopeExtensionArtifactReference,
    StartScopeExtension,
)
from workflow.requests.vision import (
    BeginVisionRevision,
    DecideVisionReview,
    RecordVisionInterviewTurn,
)

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
    | RecordVisionInterviewTurn
    | DecideVisionReview
    | BeginVisionRevision
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
    | CompleteTask
    | CloseStory
    | ReviewSprint
    | CloseSprint
    | RecordPostSprintTriage
    | StartScopeExtension
    | RecordExtensionChallenge
    | RecordExtensionPrd
    | DecideExtensionPrd
    | RecordAmendmentSpecDraft
    | DecideAmendmentSpecDraft
    | RegisterScopeExtension
    | ReconcileScopeExtension
    | AbandonScopeExtension
    | StartNodeAttempt
    | FailNodeAttempt
)

__all__ = [
    "AbandonProjectShell",
    "AbandonScopeExtension",
    "ApplyStoryDependencies",
    "BeginVisionRevision",
    "CloseSprint",
    "CloseStory",
    "CompileAuthority",
    "CompleteTask",
    "DecideAmendmentSpecDraft",
    "DecideAuthority",
    "DecideBacklog",
    "DecideBrownfieldInitialSpec",
    "DecideExtensionPrd",
    "DecideInitialSpecDraft",
    "DecidePrd",
    "DecideRoadmap",
    "DecideSprintPlan",
    "DecideStory",
    "DecideVision",
    "DecideVisionReview",
    "FailNodeAttempt",
    "OpenProjectShell",
    "ReconcileBacklog",
    "ReconcileScopeExtension",
    "RecordAmendmentSpecDraft",
    "RecordAuthorityFeedback",
    "RecordBacklogDraft",
    "RecordBrownfieldSpecDraft",
    "RecordChallengeArtifact",
    "RecordExtensionChallenge",
    "RecordExtensionPrd",
    "RecordInitialSpecDraft",
    "RecordPostSprintTriage",
    "RecordPrdVersion",
    "RecordRepositoryBaseline",
    "RecordRepositoryInventory",
    "RecordRoadmapDraft",
    "RecordSprintPlan",
    "RecordStoryDraft",
    "RecordVisionDraft",
    "RecordVisionInterviewTurn",
    "RegisterInitialScope",
    "RegisterScopeExtension",
    "RepairAuthority",
    "RepairStoryReadiness",
    "ReviewSprint",
    "ScopeExtensionArtifactReference",
    "StartNodeAttempt",
    "StartScopeExtension",
    "StartSprint",
    "TransitionRequest",
]
