"""Input and output schemas for the Sprint Planner agent."""

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from utils.task_metadata import StructuredTaskSpec


def _normalize_comparison_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower().strip())


_BROAD_STORY_COMPLETION_PHRASES = {
    _normalize_comparison_text("complete the story"),
    _normalize_comparison_text("finish the story"),
    _normalize_comparison_text("implement the story"),
    _normalize_comparison_text("satisfy all acceptance criteria"),
    _normalize_comparison_text("satisfy acceptance criteria"),
    _normalize_comparison_text("all acceptance criteria met"),
    _normalize_comparison_text("story complete"),
    _normalize_comparison_text("story is complete"),
}


class SprintPlannerStory(BaseModel):
    """A candidate user story for sprint planning.

    NOTE: No strict config or validation constraints here.
    ADK automatic function-calling cannot parse ge/le/min_length
    on input schemas. Constraints belong on output schemas only.
    """

    story_id: Annotated[int, Field(description="User story ID from the database.")]
    story_title: Annotated[
        str,
        Field(description="Short user story title (at least 3 chars)."),
    ]
    priority: Annotated[
        int,
        Field(description="Priority rank (1 is highest, must be >= 1)."),
    ]
    story_points: Annotated[
        int | None,
        Field(
            default=None,
            description="Optional story points estimate (>= 1 when provided).",
        ),
    ]
    parent_group: Annotated[
        int | None,
        Field(
            default=None,
            description="Optional readiness group derived from sprint input.",
        ),
    ]
    group_slot: Annotated[
        int | None,
        Field(
            default=None,
            description="Optional position within the readiness group derived from sprint input.",
        ),
    ]
    story_description: Annotated[
        str,
        Field(description="Detailed user story description."),
    ]
    acceptance_criteria_items: Annotated[
        list[str],
        Field(default_factory=list, description="List of acceptance criteria items."),
    ]
    persona: Annotated[
        str | None,
        Field(default=None, description="Target persona for the story."),
    ]
    source_requirement: Annotated[
        str | None,
        Field(default=None, description="Original source requirement or reference."),
    ]
    prerequisite_story_ids: Annotated[
        list[int],
        Field(
            default_factory=list,
            description="Active prerequisite story IDs that must precede this story.",
        ),
    ]
    blocked_by_story_ids: Annotated[
        list[int],
        Field(
            default_factory=list,
            description="Prerequisite story IDs that are still unresolved candidates.",
        ),
    ]
    dependency_status: Annotated[
        Literal["ready", "blocked"],
        Field(
            default="ready",
            description="Dependency readiness for this story in sprint planning.",
        ),
    ]
    evaluated_invariant_ids: Annotated[
        list[str],
        Field(
            default_factory=list,
            description="Invariant IDs already evaluated for this story and allowed for task binding.",
        ),
    ]
    story_compliance_boundary_summaries: Annotated[
        list[str],
        Field(
            default_factory=list,
            description="Summaries of compliance boundaries or architectural constraints applicable to this story.",
        ),
    ]


class SprintPlannerInput(BaseModel):
    """Schema for Sprint Planner input.

    NOTE: No ge/le/min_length constraints here.
    ADK automatic function-calling cannot parse Annotated constraints
    on input schemas. Validation constraints belong on output schemas.
    """

    available_stories: Annotated[
        list[SprintPlannerStory],
        Field(description="Prioritized list of available stories for this sprint."),
    ]
    team_velocity_assumption: Annotated[
        Literal["Low", "Medium", "High"],
        Field(description="Velocity band for capacity planning."),
    ]
    sprint_duration_days: Annotated[
        int,
        Field(
            description="Sprint duration in days (1-31).",
        ),
    ]
    user_context: Annotated[
        str | None,
        Field(
            default=None,
            description="Optional user context or focus for the sprint.",
        ),
    ]
    max_story_points: Annotated[
        int | None,
        Field(
            default=None,
            description="Optional story points cap (>= 1) for capacity planning.",
        ),
    ]
    include_task_decomposition: Annotated[
        bool,
        Field(
            default=True,
            description="Whether to generate task decomposition per story.",
        ),
    ]


class SprintPlannerSelectedStory(BaseModel):
    """A selected story with justification and tasks."""

    model_config = ConfigDict(extra="forbid")

    story_id: Annotated[
        int, Field(description="User story ID selected for the sprint.")
    ]
    story_title: Annotated[str, Field(min_length=3, description="Story title.")]
    tasks: Annotated[
        list[StructuredTaskSpec],
        Field(
            default_factory=list,
            description="Optional structured task list for the story.",
        ),
    ]
    reason_for_selection: Annotated[
        str,
        Field(min_length=3, description="Reason this story supports the sprint goal."),
    ]


class SprintPlannerDeselectedStory(BaseModel):
    """A story not selected for the sprint."""

    model_config = ConfigDict(extra="forbid")

    story_id: Annotated[int, Field(description="User story ID not selected.")]
    reason: Annotated[
        str,
        Field(min_length=3, description="Reason this story was not selected."),
    ]


class SprintPlannerCapacityAnalysis(BaseModel):
    """Capacity analysis summary for sprint planning."""

    model_config = ConfigDict(extra="forbid")

    velocity_assumption: Annotated[
        Literal["Low", "Medium", "High"],
        Field(description="Velocity band used in the plan."),
    ]
    capacity_band: Annotated[
        str,
        Field(description="Human-readable capacity band, e.g., '2-3 stories'."),
    ]
    selected_count: Annotated[
        int,
        Field(ge=0, description="Number of stories selected."),
    ]
    story_points_used: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            description="Total story points selected when available.",
        ),
    ]
    max_story_points: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description="Optional story points cap used for planning.",
        ),
    ]
    commitment_note: Annotated[
        str,
        Field(
            min_length=3,
            description="Commitment check asking if scope is achievable.",
        ),
    ]
    reasoning: Annotated[
        str,
        Field(
            min_length=3,
            description="Explanation for why the sprint scope is feasible.",
        ),
    ]


class SprintPlannerOutput(BaseModel):
    """Schema for Sprint Planner output."""

    model_config = ConfigDict(extra="forbid")

    sprint_goal: Annotated[
        str,
        Field(min_length=3, description="Single sprint objective statement."),
    ]
    sprint_number: Annotated[
        int, Field(ge=1, description="Sprint number for this product.")
    ]
    duration_days: Annotated[
        int,
        Field(ge=1, le=31, description="Sprint duration in days."),
    ]
    selected_stories: Annotated[
        list[SprintPlannerSelectedStory],
        Field(description="Stories selected for the sprint backlog."),
    ]
    deselected_stories: Annotated[
        list[SprintPlannerDeselectedStory],
        Field(description="Stories not selected and why."),
    ]
    capacity_analysis: Annotated[
        SprintPlannerCapacityAnalysis,
        Field(description="Capacity reasoning and commitment check."),
    ]


def validate_task_invariant_bindings(
    output: "SprintPlannerOutput",
    *,
    allowed_invariant_ids_by_story: dict[int, list[str]],
) -> list[str]:
    """Validate that each task only binds invariants allowed for its parent story."""
    errors: list[str] = []
    for story in output.selected_stories:
        allowed_ids = set(allowed_invariant_ids_by_story.get(story.story_id, []))
        for task in story.tasks:
            invalid_ids = [
                invariant_id
                for invariant_id in task.relevant_invariant_ids
                if invariant_id not in allowed_ids
            ]
            if invalid_ids:
                errors.append(
                    "Story "
                    f"{story.story_id} task '{task.description}' referenced invalid invariant IDs: "
                    + ", ".join(invalid_ids)
                )
    return errors


def validate_task_decomposition_quality(
    output: "SprintPlannerOutput",
    *,
    include_task_decomposition: bool,
    has_acceptance_criteria_by_story: dict[int, bool] | None = None,
    acceptance_criteria_items_by_story: dict[int, list[str]] | None = None,
) -> list[str]:
    """Validate deterministic quality gates for sprint task decomposition."""
    errors: list[str] = []
    has_acceptance_criteria_by_story = has_acceptance_criteria_by_story or {}
    acceptance_criteria_items_by_story = acceptance_criteria_items_by_story or {}
    file_extension_pattern = re.compile(r"\.[a-zA-Z0-9]+$")
    path_pattern = re.compile(r"[/\\]")

    for story in output.selected_stories:
        if include_task_decomposition and not story.tasks:
            errors.append(f"Story {story.story_id}: Missing task decomposition.")
            continue

        normalized_descriptions = set()
        all_same_workstreams = True
        all_same_targets = True
        first_workstreams = None
        first_targets = None
        story_acceptance_items = acceptance_criteria_items_by_story.get(
            story.story_id, []
        )
        normalized_story_acceptance_items = {
            _normalize_comparison_text(item)
            for item in story_acceptance_items
            if isinstance(item, str) and item.strip()
        }
        has_story_acceptance_criteria = bool(normalized_story_acceptance_items) or bool(
            has_acceptance_criteria_by_story.get(story.story_id, False)
        )

        for task in story.tasks:
            desc = task.description.strip()
            if not desc:
                errors.append(
                    f"Story {story.story_id}: Found task with empty description."
                )

            if task.task_kind == "other":
                errors.append(
                    f"Story {story.story_id} task '{desc}': 'task_kind' cannot be 'other'. Please categorize properly."
                )

            if not task.artifact_targets:
                errors.append(
                    f"Story {story.story_id} task '{desc}': Must specify at least one artifact_target."
                )

            if not task.workstream_tags:
                errors.append(
                    f"Story {story.story_id} task '{desc}': Must specify at least one workstream_tag."
                )

            for target in task.artifact_targets:
                if file_extension_pattern.search(target) or path_pattern.search(target):
                    errors.append(
                        f"Story {story.story_id} task '{desc}': artifact_target '{target}' looks like an exact file path. Use component/module names instead."
                    )

            if not task.checklist_items:
                errors.append(
                    f"Story {story.story_id} task '{desc}': Must specify at least one checklist item."
                )

            norm_desc = _normalize_comparison_text(desc)
            if norm_desc:
                if norm_desc in normalized_descriptions:
                    errors.append(
                        f"Story {story.story_id}: Duplicate or identical task description found: '{desc}'."
                    )
                normalized_descriptions.add(norm_desc)

            norm_targets = [
                _normalize_comparison_text(t) for t in task.artifact_targets
            ]
            if len(norm_targets) != len(set(norm_targets)):
                errors.append(
                    f"Story {story.story_id} task '{desc}': Contains duplicate artifact_targets."
                )

            norm_tags = [_normalize_comparison_text(t) for t in task.workstream_tags]
            if len(norm_tags) != len(set(norm_tags)):
                errors.append(
                    f"Story {story.story_id} task '{desc}': Contains duplicate workstream_tags."
                )

            for checklist_item in task.checklist_items:
                checklist_text = checklist_item.strip()
                checklist_norm = _normalize_comparison_text(checklist_text)
                if not checklist_norm:
                    errors.append(
                        f"Story {story.story_id} task '{desc}': checklist item must not be empty."
                    )
                    continue
                if checklist_norm in normalized_story_acceptance_items:
                    errors.append(
                        f"Story {story.story_id} task '{desc}': checklist item '{checklist_text}' duplicates story acceptance criteria."
                    )
                elif checklist_norm in _BROAD_STORY_COMPLETION_PHRASES:
                    errors.append(
                        f"Story {story.story_id} task '{desc}': checklist item '{checklist_text}' is too story-level; use task-local completion criteria instead."
                    )

            task_tags_set = frozenset(norm_tags)
            task_targets_set = frozenset(norm_targets)

            if first_workstreams is None:
                first_workstreams = task_tags_set
                first_targets = task_targets_set
            else:
                if task_tags_set != first_workstreams:
                    all_same_workstreams = False
                if task_targets_set != first_targets:
                    all_same_targets = False

        if (
            has_story_acceptance_criteria
            and len(story.tasks) > 1
            and all_same_workstreams
            and all_same_targets
        ):
            errors.append(
                f"Story {story.story_id}: Tasks are under-decomposed. All {len(story.tasks)} tasks carry the exact same workstreams and artifact targets."
            )

    return errors


__all__ = [
    "SprintPlannerCapacityAnalysis",
    "SprintPlannerDeselectedStory",
    "SprintPlannerInput",
    "SprintPlannerOutput",
    "SprintPlannerSelectedStory",
    "SprintPlannerStory",
    "validate_task_decomposition_quality",
    "validate_task_invariant_bindings",
]
