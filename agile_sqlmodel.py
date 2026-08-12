"""Public SQLModel schema facade and database bootstrap entry point."""

from importlib import import_module
from types import ModuleType

from models.authority_curation import (
    AuthorityCurationAttempt,
    AuthorityFeedbackAttempt,
)
from models.core import (
    Epic,
    Feature,
    Project,
    ProjectPersona,
    ProjectTeam,
    Sprint,
    SprintStory,
    Task,
    Team,
    TeamMember,
    TeamMembership,
    Theme,
    UserStory,
    UserStoryDependency,
)
from models.enums import (
    SpecAuthorityStatus,
    SprintStatus,
    StoryResolution,
    StoryStatus,
    TaskAcceptanceResult,
    TaskStatus,
    TeamRole,
    TimeFrame,
    WorkflowEventType,
)
from models.events import (
    StoryCompletionLog,
    TaskExecutionLog,
    WorkflowEvent,
)
from models.product_definition import (
    ProductGoalArtifact,
    ProductGoalArtifactDecision,
    ProductGoalInterviewTurn,
    ProductGoalOutcome,
    SpecificationCandidate,
    SpecificationDecision,
    SpecificationSource,
    VisionEvidenceSnapshot,
    VisionInterviewTurn,
    VisionRevisionIntent,
)
from models.repository import RepositoryBinding
from models.specs import (
    CompiledSpecAuthority,
    SpecAuthorityAcceptance,
    SpecRegistry,
)

__all__ = [
    "AuthorityCurationAttempt",
    "AuthorityFeedbackAttempt",
    "CompiledSpecAuthority",
    "Epic",
    "Feature",
    "ProductGoalArtifact",
    "ProductGoalArtifactDecision",
    "ProductGoalInterviewTurn",
    "ProductGoalOutcome",
    "Project",
    "ProjectPersona",
    "ProjectTeam",
    "RepositoryBinding",
    "SpecAuthorityAcceptance",
    "SpecAuthorityStatus",
    "SpecRegistry",
    "SpecificationCandidate",
    "SpecificationDecision",
    "SpecificationSource",
    "Sprint",
    "SprintStatus",
    "SprintStory",
    "StoryCompletionLog",
    "StoryResolution",
    "StoryStatus",
    "Task",
    "TaskAcceptanceResult",
    "TaskExecutionLog",
    "TaskStatus",
    "Team",
    "TeamMember",
    "TeamMembership",
    "TeamRole",
    "Theme",
    "TimeFrame",
    "UserStory",
    "UserStoryDependency",
    "VisionEvidenceSnapshot",
    "VisionInterviewTurn",
    "VisionRevisionIntent",
    "WorkflowEvent",
    "WorkflowEventType",
]


def _db_module() -> ModuleType:
    """Load models.db lazily so model imports stay DB-config agnostic."""
    return import_module("models.db")


def __getattr__(name: str):
    """Lazily expose database globals without resolving configuration eagerly."""
    if name in {
        "DB_URL",
        "engine",
        "get_database_url",
        "get_engine",
        "create_db_and_tables",
        "ensure_business_db_ready",
    }:
        value = getattr(_db_module(), name)
        globals()[name] = value
        return value
    message = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(message)


if __name__ == "__main__":
    _db_module().create_db_and_tables()
