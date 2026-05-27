"""Stable enum definitions for the business data model."""

from __future__ import annotations

from enum import StrEnum


class TeamRole(StrEnum):
    """Roles for a member within a team."""

    DEVELOPER = "Developer"
    PRODUCT_OWNER = "Product Owner"
    DESIGNER = "Designer"
    QA = "QA"
    LEAD = "Lead"


class SprintStatus(StrEnum):
    """Status of a sprint."""

    PLANNED = "Planned"
    ACTIVE = "Active"
    COMPLETED = "Completed"


class StoryStatus(StrEnum):
    """Status of a user story."""

    TO_DO = "To Do"
    IN_PROGRESS = "In Progress"
    DONE = "Done"
    ACCEPTED = "Accepted"


class TaskStatus(StrEnum):
    """Status of a task."""

    TO_DO = "To Do"
    IN_PROGRESS = "In Progress"
    DONE = "Done"
    CANCELLED = "Cancelled"


class TaskAcceptanceResult(StrEnum):
    """Result of acceptance criteria check on a task."""

    NOT_CHECKED = "not_checked"
    PARTIALLY_MET = "partially_met"
    FULLY_MET = "fully_met"


class StoryResolution(StrEnum):
    """Resolution reason when story is marked DONE."""

    COMPLETED = "Completed"
    COMPLETED_WITH_CHANGES = "Completed with AC changes"
    PARTIAL = "Partial"
    WONT_DO = "Won't Do"


class WorkflowEventType(StrEnum):
    """Types of workflow events for metrics tracking."""

    VISION_SAVED = "vision_saved"
    SPEC_COMPILED = "spec_compiled"
    BACKLOG_SAVED = "backlog_saved"
    EVIDENCE_COLLECTED = "evidence_collected"
    ROADMAP_SAVED = "roadmap_saved"
    STORIES_SAVED = "stories_saved"
    STORY_DEPENDENCIES_PROPOSED = "story_dependencies_proposed"
    STORY_DEPENDENCIES_APPLIED = "story_dependencies_applied"
    SPRINT_PLAN_DRAFT = "sprint_plan_draft"
    SPRINT_PLAN_REVIEW = "sprint_plan_review"
    SPRINT_PLAN_SAVED = "sprint_plan_saved"
    SPRINT_STARTED = "sprint_started"
    SPRINT_COMPLETED = "sprint_completed"
    FSM_STATE_DWELL = "fsm_state_dwell"
    TLX_PROMPT_TRIGGERED = "tlx_prompt_triggered"


class TimeFrame(StrEnum):
    """Roadmap time frames for prioritization."""

    NOW = "Now"
    NEXT = "Next"
    LATER = "Later"


class SpecAuthorityStatus(StrEnum):
    """Status of compiled spec authority for a product."""

    CURRENT = "current"
    STALE = "stale"
    NOT_COMPILED = "not_compiled"
    PENDING_REVIEW = "pending_review"
