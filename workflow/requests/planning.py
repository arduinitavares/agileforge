"""Typed requests for Roadmap, Story, dependency, and Sprint planning facts."""

from __future__ import annotations

from typing import ClassVar, Literal, Self

from pydantic import Field, model_validator

from workflow.contracts import FrozenModel, JsonObject
from workflow.facts import StoryDependencyReviewEdgeFact
from workflow.requests.base import PositionedRequest, ReviewRationale

ReviewedDependencyEdge = StoryDependencyReviewEdgeFact


class StoryReadinessUpdate(FrozenModel):
    """Exact planning metadata repair for one Story."""

    story_id: int
    story_points: int = Field(ge=1)
    rank: str = Field(min_length=1)


class RecordRoadmapDraft(PositionedRequest):
    """Record immutable Roadmap content for the accepted current Backlog."""

    kind: Literal["record_roadmap_draft"] = "record_roadmap_draft"
    node_id: ClassVar[str] = "planning.roadmap.generate"
    backlog_artifact_id: int
    backlog_artifact_fingerprint: str = Field(min_length=1)
    canonical_content: JsonObject
    content_fingerprint: str = Field(min_length=1)
    supersedes_roadmap_artifact_id: int | None = None


class DecideRoadmap(PositionedRequest):
    """Append a decision for one exact immutable Roadmap artifact."""

    kind: Literal["decide_roadmap"] = "decide_roadmap"
    node_id: ClassVar[str] = "planning.roadmap.review"
    roadmap_artifact_id: int
    artifact_fingerprint: str = Field(min_length=1)
    decision: Literal["accepted", "rejected", "feedback"]
    rationale: ReviewRationale


class _RequirementPositionedRequest(PositionedRequest):
    _is_request_scaffold: ClassVar[bool] = True
    requirement_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_requirement_instance(self) -> Self:
        expected = f"requirement:{self.requirement_id}"
        if self.instance_key is not None and self.instance_key != expected:
            message = f"instance_key must be exactly {expected!r}."
            raise ValueError(message)
        return self

    def decision_instance_key(self) -> str:
        return f"requirement:{self.requirement_id}"


class RecordStoryDraft(_RequirementPositionedRequest):
    """Record immutable Story-set content for one accepted requirement."""

    kind: Literal["record_story_draft"] = "record_story_draft"
    node_id: ClassVar[str] = "planning.story.generate"
    roadmap_artifact_id: int
    roadmap_artifact_fingerprint: str = Field(min_length=1)
    canonical_content: JsonObject
    content_fingerprint: str = Field(min_length=1)
    supersedes_story_artifact_id: int | None = None


class DecideStory(_RequirementPositionedRequest):
    """Append a decision bound to one exact immutable Story artifact."""

    kind: Literal["decide_story"] = "decide_story"
    node_id: ClassVar[str] = "planning.story.review"
    story_artifact_id: int
    artifact_fingerprint: str = Field(min_length=1)
    decision: Literal["accepted", "rejected", "feedback"]
    rationale: ReviewRationale


class ApplyStoryDependencies(PositionedRequest):
    """Apply one exact reviewed semantic dependency set."""

    kind: Literal["apply_story_dependencies"] = "apply_story_dependencies"
    node_id: ClassVar[str] = "planning.story_dependencies"
    selected_story_ids: tuple[int, ...] = Field(min_length=1)
    reviewed_edges: tuple[ReviewedDependencyEdge, ...]
    source_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_canonical_dependency_set(self) -> Self:
        """Require a sorted, unique, closed dependency set."""
        if self.selected_story_ids != tuple(sorted(set(self.selected_story_ids))):
            message = "selected_story_ids must be sorted and unique."
            raise ValueError(message)
        pairs = tuple(
            (edge.dependent_story_id, edge.prerequisite_story_id)
            for edge in self.reviewed_edges
        )
        if pairs != tuple(sorted(set(pairs))):
            message = "reviewed_edges must be sorted and unique."
            raise ValueError(message)
        selected = set(self.selected_story_ids)
        if any(left not in selected or right not in selected for left, right in pairs):
            message = "reviewed_edges must remain inside selected_story_ids."
            raise ValueError(message)
        return self


class RepairStoryReadiness(PositionedRequest):
    """Repair exact Story planning metadata against a current fingerprint."""

    kind: Literal["repair_story_readiness"] = "repair_story_readiness"
    node_id: ClassVar[str] = "planning.story_readiness"
    story_ids: tuple[int, ...] = Field(min_length=1)
    repairs: tuple[StoryReadinessUpdate, ...] = Field(min_length=1)
    expected_readiness_fingerprint: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_exact_repair_set(self) -> Self:
        """Require one ordered repair for every guarded Story."""
        if self.story_ids != tuple(sorted(set(self.story_ids))):
            message = "story_ids must be sorted and unique."
            raise ValueError(message)
        repair_ids = tuple(item.story_id for item in self.repairs)
        if repair_ids != self.story_ids:
            message = "repairs must cover story_ids exactly in sorted order."
            raise ValueError(message)
        return self


class RecordSprintPlan(PositionedRequest):
    """Record one exact canonical Sprint plan for the current candidates."""

    kind: Literal["record_sprint_plan"] = "record_sprint_plan"
    node_id: ClassVar[str] = "planning.sprint.plan"
    team_name: str = Field(min_length=1)
    selected_story_ids: tuple[int, ...] = Field(min_length=1)
    canonical_task_plan: JsonObject
    plan_fingerprint: str = Field(min_length=1)
    candidate_set_fingerprint: str = Field(min_length=1)
    supersedes_sprint_plan_artifact_id: int | None = None
    include_task_decomposition: bool = True

    @model_validator(mode="after")
    def validate_selected_story_ids(self) -> Self:
        """Require stable sorted selected Story identities."""
        if self.selected_story_ids != tuple(sorted(set(self.selected_story_ids))):
            message = "selected_story_ids must be sorted and unique."
            raise ValueError(message)
        return self


class DecideSprintPlan(PositionedRequest):
    """Append a decision for one exact immutable Sprint plan."""

    kind: Literal["decide_sprint_plan"] = "decide_sprint_plan"
    node_id: ClassVar[str] = "planning.sprint.review"
    sprint_plan_artifact_id: int
    plan_fingerprint: str = Field(min_length=1)
    decision: Literal["accepted", "rejected", "feedback"]
    rationale: ReviewRationale


class StartSprint(PositionedRequest):
    """Start the exact accepted Sprint plan while its candidates remain current."""

    kind: Literal["start_sprint"] = "start_sprint"
    node_id: ClassVar[str] = "planning.sprint.start"
    sprint_plan_artifact_id: int
    sprint_id: int
    plan_fingerprint: str = Field(min_length=1)
    candidate_set_fingerprint: str = Field(min_length=1)


__all__ = [
    "ApplyStoryDependencies",
    "DecideRoadmap",
    "DecideSprintPlan",
    "DecideStory",
    "RecordRoadmapDraft",
    "RecordSprintPlan",
    "RecordStoryDraft",
    "RepairStoryReadiness",
    "ReviewedDependencyEdge",
    "StartSprint",
    "StoryReadinessUpdate",
]
