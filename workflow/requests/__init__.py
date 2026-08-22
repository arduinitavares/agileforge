"""Guarded request contracts for workflow graph transitions."""

from workflow.requests.attempts import (
    FailNodeAttempt,
    ObsoleteNodeAttempt,
    RevalidateNodeAttempt,
    StartNodeAttempt,
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
    CompleteSpecificationStructuring,
    DecideSpecification,
    RegisterSpecificationSource,
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
    | GenerateVisionBootstrap
    | RecordVisionInterviewTurn
    | DecideVisionReview
    | BeginVisionRevision
    | RecordProductGoalInterviewTurn
    | DecideProductGoalReview
    | FulfillProductGoal
    | AbandonProductGoal
    | RegisterSpecificationSource
    | CompleteSpecificationStructuring
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
    | RevalidateNodeAttempt
    | ObsoleteNodeAttempt
    | FailNodeAttempt
)

__all__ = [
    "AbandonProductGoal",
    "ApplyStoryDependencies",
    "BeginVisionRevision",
    "CloseSprint",
    "CloseStory",
    "CompleteSpecificationStructuring",
    "CompleteTask",
    "CreateProject",
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
    "ObsoleteNodeAttempt",
    "RecordBacklogDraft",
    "RecordPostSprintTriage",
    "RecordProductGoalInterviewTurn",
    "RecordRepositoryBinding",
    "RecordRoadmapDraft",
    "RecordSprintPlan",
    "RecordStoryDraft",
    "RecordVisionInterviewTurn",
    "RegisterSpecificationSource",
    "RepairStoryReadiness",
    "RepositoryBindingInput",
    "RevalidateNodeAttempt",
    "ReviewSprint",
    "StartNodeAttempt",
    "StartSprint",
    "TransitionRequest",
]
