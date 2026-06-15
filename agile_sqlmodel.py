# agile_sqlmodel.py

"""
Defines the Agile project management schema using SQLModel.

This script creates all 12 tables, including link models for
many-to-many relationships, and sets up a SQLite database.

This version fixes the 'utcnow' deprecation warning and the
'func.now' runtime error.
"""

import sys
from importlib import import_module
from types import ModuleType

# When this compatibility shim is executed as a script, models.core may import
# it again by module name while exports are still being populated. Register the
# current module under the canonical name up front so that path resolves to this
# in-flight module instead of executing the file twice.
if __name__ == "__main__":
    sys.modules.setdefault("agile_sqlmodel", sys.modules[__name__])

# Re-export model symbols from their new package locations and ensure SQLModel
# metadata is populated when this compatibility shim is imported or executed.
from models.brownfield import (
    BrownfieldScanAttempt,
    BrownfieldSourceArtifact,
    BrownfieldSpecApproval,
    BrownfieldSpecDraftAttempt,
)
from models.core import (
    Epic,
    Feature,
    Product,
    ProductPersona,
    ProductTeam,
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
from models.specs import (
    CompiledSpecAuthority,
    SpecAuthorityAcceptance,
    SpecRegistry,
)

__all__ = [
    "BrownfieldScanAttempt",
    "BrownfieldSourceArtifact",
    "BrownfieldSpecApproval",
    "BrownfieldSpecDraftAttempt",
    "CompiledSpecAuthority",
    "Epic",
    "Feature",
    "Product",
    "ProductPersona",
    "ProductTeam",
    "SpecAuthorityAcceptance",
    "SpecAuthorityStatus",
    "SpecRegistry",
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
    "WorkflowEvent",
    "WorkflowEventType",
]


# --- 1. Enums for Status Fields ---


def _db_module() -> ModuleType:
    """Load models.db lazily so model imports stay DB-config agnostic."""
    return import_module("models.db")


def __getattr__(name: str):
    """Lazily expose DB globals so importing this shim does not require DB env."""
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
    # This makes the script runnable with explicit environment configuration.
    _db_module().create_db_and_tables()
