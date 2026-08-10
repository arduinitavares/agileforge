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
from workflow.requests.product_definition import DecideBacklog, RecordBacklogDraft
from workflow.requests.product_discovery import (
    DecideSpecification,
    RecordDiscoveryArtifact,
    RecordSpecificationCandidate,
)
from workflow.requests.product_goal import (
    AbandonProductGoal,
    DecideProductGoalReview,
    FulfillProductGoal,
    RecordProductGoalInterviewTurn,
)
from workflow.requests.project import (
    CreateProject,
    RecordRepositoryBinding,
    RepositoryBindingInput,
)
from workflow.requests.vision import (
    BeginVisionRevision,
    DecideVisionReview,
    GenerateVisionBootstrap,
    RecordVisionInterviewTurn,
)

type TransitionRequest = (
    CreateProject
    | RecordRepositoryBinding
    | CompileAuthority
    | DecideAuthority
    | RecordAuthorityFeedback
    | RepairAuthority
    | GenerateVisionBootstrap
    | RecordVisionInterviewTurn
    | DecideVisionReview
    | BeginVisionRevision
    | RecordProductGoalInterviewTurn
    | DecideProductGoalReview
    | FulfillProductGoal
    | AbandonProductGoal
    | RecordDiscoveryArtifact
    | RecordSpecificationCandidate
    | DecideSpecification
    | RecordBacklogDraft
    | DecideBacklog
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
    | StartNodeAttempt
    | FailNodeAttempt
)

__all__ = [
    "AbandonProductGoal",
    "ApplyStoryDependencies",
    "BeginVisionRevision",
    "CloseSprint",
    "CloseStory",
    "CompileAuthority",
    "CompleteTask",
    "CreateProject",
    "DecideAuthority",
    "DecideBacklog",
    "DecideProductGoalReview",
    "DecideRoadmap",
    "DecideSpecification",
    "DecideSprintPlan",
    "DecideStory",
    "DecideVisionReview",
    "FailNodeAttempt",
    "FulfillProductGoal",
    "GenerateVisionBootstrap",
    "RecordAuthorityFeedback",
    "RecordBacklogDraft",
    "RecordDiscoveryArtifact",
    "RecordPostSprintTriage",
    "RecordProductGoalInterviewTurn",
    "RecordRepositoryBinding",
    "RecordRoadmapDraft",
    "RecordSpecificationCandidate",
    "RecordSprintPlan",
    "RecordStoryDraft",
    "RecordVisionInterviewTurn",
    "RepairAuthority",
    "RepairStoryReadiness",
    "RepositoryBindingInput",
    "ReviewSprint",
    "StartNodeAttempt",
    "StartSprint",
    "TransitionRequest",
]
