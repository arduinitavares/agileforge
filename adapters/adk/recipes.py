"""Execution-only ADK 2 recipes for domain graph nodes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from google.adk import Context, Workflow
from google.adk.workflow import START, JoinNode, RetryConfig, node
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from adapters.adk.errors import (
    AttemptRevalidationError,
    SpecificationAgenticExecutionError,
    VisionAgenticPreflightError,
)
from adapters.adk.preflight import revalidate_specification_attempt
from services.contracts.backlog import (
    BacklogAgentOutput,
    BacklogBuilderInput,
    BacklogOutput,
    canonicalize_backlog_items,
)
from services.contracts.product_goal import ProductGoalInterviewOutput
from services.contracts.roadmap import (
    RoadmapBuilderInput,
    RoadmapBuilderOutput,
    validate_roadmap_backlog_coverage,
)
from services.contracts.specification_authoring import (
    SpecificationStructuringInput,
    SpecificationStructuringOutput,
    specification_structuring_completion_payload,
)
from services.contracts.specification_references import (
    AcceptedSpecificationReference,
)
from services.contracts.sprint import (
    SprintPlannerInput,
    SprintPlannerOutput,
    validate_task_spec_references,
)
from services.contracts.story import (
    CanonicalStoryOutput,
    UserStoryAgentItem,
    UserStoryWriterInput,
    UserStoryWriterOutput,
    canonicalize_story_items,
)
from services.contracts.vision import (
    VisionAgentInput,
    VisionClarificationInput,
    VisionDraftOutput,
    VisionModelInput,
    VisionRepairInput,
)
from services.vision_output_validation import (
    VisionDraftValidationError,
    validate_vision_draft,
)
from utils.agileforge_spec_profile_v2 import SCHEMA_VERSION, SpecificationPayload
from workflow.contracts import JsonObject, WorkflowErrorCode
from workflow.fingerprints import canonical_hash
from workflow.requests import (
    CompleteSpecificationStructuring,
    GenerateVisionBootstrap,
    RecordBacklogDraft,
    RecordProductGoalInterviewTurn,
    RecordRoadmapDraft,
    RecordSprintPlan,
    RecordStoryDraft,
    RecordVisionInterviewTurn,
)
from workflow.requests.base import PositionedRequest

if TYPE_CHECKING:
    from google.adk.agents import BaseAgent

_JSON_OBJECT = TypeAdapter(JsonObject)
AGENTIC_NODE_IDS = (
    "vision.bootstrap",
    "vision.interview",
    "goal.interview",
    "specification.structure",
    "backlog.generate",
    "planning.roadmap.generate",
    "planning.story.generate",
    "planning.sprint.plan",
)


class RecipeInput(BaseModel):
    """Strict workflow envelope for normalized domain input."""

    model_config = ConfigDict(extra="forbid")
    payload: JsonObject


class RecipeOutput(BaseModel):
    """Strict workflow envelope for validated structured output."""

    model_config = ConfigDict(extra="forbid")
    payload: JsonObject


class _UnvalidatedRecipeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payload: object
    input_payload: JsonObject


class _JoinedBacklogValidations(BaseModel):
    model_config = ConfigDict(extra="forbid")
    validate_structure: RecipeOutput
    validate_round_trip: RecipeOutput


class _BacklogRecipePayload(BaseModel):
    """Provider input beside host-only Backlog persistence guards."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    builder_input: BacklogBuilderInput
    product_goal_artifact_id: int
    product_goal_fingerprint: str
    supersedes_backlog_artifact_id: int | None = None


class _RoadmapRecipePayload(BaseModel):
    """Provider input beside host-only Roadmap persistence guards."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    builder_input: RoadmapBuilderInput
    backlog_artifact_id: int
    backlog_artifact_fingerprint: str
    supersedes_roadmap_artifact_id: int | None = None


class _StoryCorrectionRecipeInput(BaseModel):
    """Closed host proof selecting one item from one accepted Story artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    story_id: int
    guidance: str
    source_story_artifact_id: int
    source_story_artifact_fingerprint: str
    source_story_item_id: str
    source_story_item_fingerprint: str


class _StoryRecipePayload(BaseModel):
    """Provider input beside exact immutable Story parent guards."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    writer_input: UserStoryWriterInput
    source_backlog_artifact_id: int
    source_backlog_artifact_fingerprint: str
    roadmap_artifact_id: int
    roadmap_artifact_fingerprint: str
    supersedes_story_artifact_id: int | None = None
    correction: _StoryCorrectionRecipeInput | None = None
    correction_source: CanonicalStoryOutput | None = None


class _SprintRecipePayload(BaseModel):
    """Trusted host evidence persisted before the Sprint planner runs."""

    model_config = ConfigDict(extra="forbid")
    planner_input: SprintPlannerInput
    capacity_points: int
    capacity_source: Literal["user_override", "project_metrics"]
    capacity_basis: str
    requested_max_story_points: int | None = None
    requested_story_ids: list[int]
    locked_story_ids: list[int]
    team_name: str
    guidance: str | None = None
    candidate_set_fingerprint: str


@dataclass(frozen=True)
class AttemptCompletionContext:
    """Exact durable attempt guards supplied to one output adapter."""

    project_id: int
    graph_version: str
    fact_fingerprint: str
    decision_fingerprint: str
    instance_key: str | None
    attempt_id: int
    attempt_fingerprint: str
    idempotency_key: str
    actor: str
    correlation_id: str | None
    normalized_input: JsonObject


type OutputAdapter = Callable[
    [object, AttemptCompletionContext],
    PositionedRequest,
]


@dataclass(frozen=True)
class AdkRecipe:
    """Execution recipe for one stable domain node ID."""

    node_id: str
    workflow: Workflow
    output_adapter: OutputAdapter


@dataclass(frozen=True)
class AgenticRecipeNodes:
    """Injected retained execution nodes used to compose the complete registry."""

    vision_interview: BaseAgent | Workflow
    vision_repair: BaseAgent | Workflow
    product_goal: BaseAgent | Workflow
    specification_structurer: BaseAgent | Workflow
    backlog_generation: BaseAgent | Workflow
    roadmap_generation: BaseAgent | Workflow
    story_generation: BaseAgent | Workflow
    sprint_planning: BaseAgent | Workflow
    story_correction: BaseAgent | Workflow | None = None


class UnknownAdkRecipeError(LookupError):
    """Raised when no execution recipe exists for a stable graph node."""

    def __init__(self, node_id: str) -> None:
        """Retain the missing stable node ID in the error message."""
        super().__init__(f"No ADK recipe is registered for node {node_id!r}.")


class AdkRecipeRegistry:
    """Map stable domain node IDs to execution-only ADK recipes."""

    def __init__(
        self,
        recipes: tuple[AdkRecipe, ...],
        *,
        required_node_ids: tuple[str, ...] | None = None,
    ) -> None:
        """Index recipes while rejecting ambiguous duplicate node IDs."""
        self._recipes = {recipe.node_id: recipe for recipe in recipes}
        if len(self._recipes) != len(recipes):
            message = "ADK recipe node IDs must be unique"
            raise ValueError(message)
        if required_node_ids is not None and tuple(self._recipes) != required_node_ids:
            message = "ADK recipes must exactly match the domain agentic catalog"
            raise ValueError(message)

    @property
    def node_ids(self) -> tuple[str, ...]:
        """Return registered stable node IDs in deterministic declaration order."""
        return tuple(self._recipes)

    def require(self, node_id: str) -> AdkRecipe:
        """Return the recipe for a stable node or fail closed."""
        try:
            return self._recipes[node_id]
        except KeyError as exc:
            raise UnknownAdkRecipeError(node_id) from exc


def _execution_limits(execution_settings: JsonObject) -> tuple[float, int]:
    timeout_value = execution_settings.get("timeout_seconds")
    attempts_value = execution_settings.get("max_attempts")
    if (
        isinstance(timeout_value, bool)
        or not isinstance(timeout_value, int | float)
        or timeout_value <= 0
    ):
        msg = "execution_settings.timeout_seconds must be a positive number."
        raise ValueError(msg)
    if (
        isinstance(attempts_value, bool)
        or not isinstance(attempts_value, int)
        or attempts_value < 1
    ):
        msg = "execution_settings.max_attempts must be a positive integer."
        raise ValueError(msg)
    return float(timeout_value), attempts_value


def validate_structured_output(output: object) -> JsonObject:
    """Validate a leaf output as one JSON object at the adapter boundary."""
    return _JSON_OBJECT.validate_python(output)


def _request_output_adapter(
    request_type: type[PositionedRequest],
) -> OutputAdapter:
    """Build one schema-only adapter from recipe output to a guarded request."""

    def adapt(
        output: object,
        context: AttemptCompletionContext,
    ) -> PositionedRequest:
        payload = dict(RecipeOutput.model_validate(output).payload)
        payload.update(
            {
                "project_id": context.project_id,
                "graph_version": context.graph_version,
                "fact_fingerprint": context.fact_fingerprint,
                "decision_fingerprint": context.decision_fingerprint,
                "instance_key": context.instance_key,
                "attempt_id": context.attempt_id,
                "attempt_fingerprint": context.attempt_fingerprint,
                "idempotency_key": context.idempotency_key,
                "actor": context.actor,
                "correlation_id": context.correlation_id,
            }
        )
        return request_type.model_validate(payload)

    return adapt


def _vision_interview_output_adapter(
    output: object,
    context: AttemptCompletionContext,
) -> GenerateVisionBootstrap | RecordVisionInterviewTurn:
    """Bind strict Vision output to trusted host input captured at attempt start."""
    parsed_input = VisionAgentInput.model_validate(context.normalized_input)
    request = parsed_input.request
    recipe_output = RecipeOutput.model_validate(output)
    parsed = VisionDraftOutput.model_validate(recipe_output.payload)
    if isinstance(request, VisionClarificationInput):
        return RecordVisionInterviewTurn(
            project_id=context.project_id,
            graph_version=context.graph_version,
            fact_fingerprint=context.fact_fingerprint,
            decision_fingerprint=context.decision_fingerprint,
            instance_key=context.instance_key,
            idempotency_key=context.idempotency_key,
            actor=context.actor,
            correlation_id=context.correlation_id,
            vision_evidence_snapshot_id=request.vision_evidence_snapshot_id,
            evidence_fingerprint=request.evidence.evidence_fingerprint,
            user_text=request.human_response,
            addressed_question_ids=request.addressed_question_ids,
            updated_components=_JSON_OBJECT.validate_python(
                parsed.components.model_dump(mode="json")
            ),
            project_vision_statement=parsed.draft_statement,
            is_complete=parsed.is_complete,
            clarifying_questions=tuple(
                _JSON_OBJECT.validate_python(item.model_dump(mode="json"))
                for item in parsed.clarifying_questions
            ),
            component_basis=tuple(
                _JSON_OBJECT.validate_python(item.model_dump(mode="json"))
                for item in parsed.component_basis
            ),
            assumptions=tuple(
                _JSON_OBJECT.validate_python(item.model_dump(mode="json"))
                for item in parsed.assumptions
            ),
            conflicts=tuple(
                _JSON_OBJECT.validate_python(item.model_dump(mode="json"))
                for item in parsed.conflicts
            ),
            attempt_id=context.attempt_id,
            attempt_fingerprint=context.attempt_fingerprint,
        )
    return GenerateVisionBootstrap(
        project_id=context.project_id,
        graph_version=context.graph_version,
        fact_fingerprint=context.fact_fingerprint,
        decision_fingerprint=context.decision_fingerprint,
        instance_key=context.instance_key,
        idempotency_key=context.idempotency_key,
        actor=context.actor,
        correlation_id=context.correlation_id,
        operation=request.operation,
        evidence=_JSON_OBJECT.validate_python(request.evidence.model_dump(mode="json")),
        evidence_fingerprint=request.evidence.evidence_fingerprint,
        evidence_warnings=tuple(
            _JSON_OBJECT.validate_python(item.model_dump(mode="json"))
            for item in request.evidence.warnings
        ),
        repository_binding_id=parsed_input.host.repository_binding_id,
        supersedes_vision_evidence_snapshot_id=(
            parsed_input.host.supersedes_vision_evidence_snapshot_id
        ),
        updated_components=_JSON_OBJECT.validate_python(
            parsed.components.model_dump(mode="json")
        ),
        project_vision_statement=parsed.draft_statement,
        is_complete=parsed.is_complete,
        clarifying_questions=tuple(
            _JSON_OBJECT.validate_python(item.model_dump(mode="json"))
            for item in parsed.clarifying_questions
        ),
        component_basis=tuple(
            _JSON_OBJECT.validate_python(item.model_dump(mode="json"))
            for item in parsed.component_basis
        ),
        assumptions=tuple(
            _JSON_OBJECT.validate_python(item.model_dump(mode="json"))
            for item in parsed.assumptions
        ),
        conflicts=tuple(
            _JSON_OBJECT.validate_python(item.model_dump(mode="json"))
            for item in parsed.conflicts
        ),
        attempt_id=context.attempt_id,
        attempt_fingerprint=context.attempt_fingerprint,
    )


def _product_goal_interview_output_adapter(
    output: object,
    context: AttemptCompletionContext,
) -> RecordProductGoalInterviewTurn:
    """Bind validated Goal output to the persisted human response."""
    parsed = ProductGoalInterviewOutput.model_validate(
        RecipeOutput.model_validate(output).payload
    )
    user_text = context.normalized_input.get("user_response")
    if not isinstance(user_text, str):
        message = "Product Goal attempt lacks the captured user response."
        raise TypeError(message)
    return RecordProductGoalInterviewTurn(
        project_id=context.project_id,
        graph_version=context.graph_version,
        fact_fingerprint=context.fact_fingerprint,
        decision_fingerprint=context.decision_fingerprint,
        instance_key=context.instance_key,
        idempotency_key=context.idempotency_key,
        actor=context.actor,
        correlation_id=context.correlation_id,
        user_text=user_text,
        updated_components=parsed.updated_components.model_dump(mode="json"),
        product_goal_statement=parsed.product_goal_statement,
        is_complete=parsed.is_complete,
        clarifying_questions=tuple(parsed.clarifying_questions),
        attempt_id=context.attempt_id,
        attempt_fingerprint=context.attempt_fingerprint,
    )


def _specification_reference(
    *,
    version_id: int,
    spec_hash: str,
    canonical_json: str,
) -> AcceptedSpecificationReference:
    return AcceptedSpecificationReference(
        spec_version_id=version_id,
        spec_hash=spec_hash,
        canonical_specification_json=canonical_json,
        payload=SpecificationPayload.model_validate_json(canonical_json),
    )


def _backlog_output_adapter(
    output: object,
    context: AttemptCompletionContext,
) -> RecordBacklogDraft:
    envelope = _BacklogRecipePayload.model_validate(context.normalized_input)
    content = BacklogOutput.model_validate(RecipeOutput.model_validate(output).payload)
    canonical_content = _JSON_OBJECT.validate_python(content.model_dump(mode="json"))
    return RecordBacklogDraft(
        project_id=context.project_id,
        graph_version=context.graph_version,
        fact_fingerprint=context.fact_fingerprint,
        decision_fingerprint=context.decision_fingerprint,
        instance_key=context.instance_key,
        attempt_id=context.attempt_id,
        attempt_fingerprint=context.attempt_fingerprint,
        idempotency_key=context.idempotency_key,
        actor=context.actor,
        correlation_id=context.correlation_id,
        spec_version_id=envelope.builder_input.accepted_specification_version_id,
        spec_hash=envelope.builder_input.accepted_specification_hash,
        product_goal_artifact_id=envelope.product_goal_artifact_id,
        product_goal_fingerprint=envelope.product_goal_fingerprint,
        canonical_content=canonical_content,
        content_fingerprint=canonical_hash(canonical_content),
        supersedes_backlog_artifact_id=envelope.supersedes_backlog_artifact_id,
    )


def _roadmap_output_adapter(
    output: object,
    context: AttemptCompletionContext,
) -> RecordRoadmapDraft:
    envelope = _RoadmapRecipePayload.model_validate(context.normalized_input)
    content = RoadmapBuilderOutput.model_validate(
        RecipeOutput.model_validate(output).payload
    )
    validate_roadmap_backlog_coverage(
        content,
        (item.backlog_item_id for item in envelope.builder_input.backlog_items),
    )
    canonical_content = _JSON_OBJECT.validate_python(content.model_dump(mode="json"))
    return RecordRoadmapDraft(
        project_id=context.project_id,
        graph_version=context.graph_version,
        fact_fingerprint=context.fact_fingerprint,
        decision_fingerprint=context.decision_fingerprint,
        instance_key=context.instance_key,
        attempt_id=context.attempt_id,
        attempt_fingerprint=context.attempt_fingerprint,
        idempotency_key=context.idempotency_key,
        actor=context.actor,
        correlation_id=context.correlation_id,
        backlog_artifact_id=envelope.backlog_artifact_id,
        backlog_artifact_fingerprint=envelope.backlog_artifact_fingerprint,
        canonical_content=canonical_content,
        content_fingerprint=canonical_hash(canonical_content),
        supersedes_roadmap_artifact_id=envelope.supersedes_roadmap_artifact_id,
    )


def _story_output_adapter(
    output: object,
    context: AttemptCompletionContext,
) -> RecordStoryDraft:
    envelope = _StoryRecipePayload.model_validate(context.normalized_input)
    content = CanonicalStoryOutput.model_validate(
        RecipeOutput.model_validate(output).payload
    )
    canonical_content = _JSON_OBJECT.validate_python(content.model_dump(mode="json"))
    return RecordStoryDraft(
        project_id=context.project_id,
        graph_version=context.graph_version,
        fact_fingerprint=context.fact_fingerprint,
        decision_fingerprint=context.decision_fingerprint,
        instance_key=context.instance_key,
        attempt_id=context.attempt_id,
        attempt_fingerprint=context.attempt_fingerprint,
        idempotency_key=context.idempotency_key,
        actor=context.actor,
        correlation_id=context.correlation_id,
        backlog_item_id=envelope.writer_input.parent_backlog_item_id,
        source_backlog_artifact_id=envelope.source_backlog_artifact_id,
        source_backlog_artifact_fingerprint=(
            envelope.source_backlog_artifact_fingerprint
        ),
        roadmap_artifact_id=envelope.roadmap_artifact_id,
        roadmap_artifact_fingerprint=envelope.roadmap_artifact_fingerprint,
        canonical_content=canonical_content,
        content_fingerprint=canonical_hash(canonical_content),
        supersedes_story_artifact_id=envelope.supersedes_story_artifact_id,
    )


def _sprint_output_adapter(
    output: object,
    context: AttemptCompletionContext,
) -> RecordSprintPlan:
    """Validate exact locked Sprint scope and bind only host-owned evidence."""
    envelope = _SprintRecipePayload.model_validate(context.normalized_input)
    planner_input = envelope.planner_input
    locked_ids = tuple(envelope.locked_story_ids)
    planner_ids = tuple(item.story_id for item in planner_input.available_stories)
    if planner_ids != locked_ids or len(set(locked_ids)) != len(locked_ids):
        message = "Sprint attempt input does not contain one exact locked cohort."
        raise ValueError(message)
    if (
        envelope.capacity_points != planner_input.capacity_points
        or envelope.capacity_source != planner_input.capacity_source
        or envelope.capacity_basis != planner_input.capacity_basis
        or envelope.guidance != planner_input.user_context
    ):
        message = "Sprint attempt envelope does not match its typed planner input."
        raise ValueError(message)
    if (
        envelope.capacity_source == "user_override"
        and envelope.requested_max_story_points != envelope.capacity_points
    ) or (
        envelope.capacity_source == "project_metrics"
        and envelope.requested_max_story_points is not None
    ):
        message = "Sprint attempt capacity source does not match caller semantics."
        raise ValueError(message)

    parsed = SprintPlannerOutput.model_validate(
        RecipeOutput.model_validate(output).payload
    )
    selected_ids = tuple(item.story_id for item in parsed.selected_stories)
    if selected_ids != locked_ids:
        message = "Sprint planner changed the exact locked Story cohort or order."
        raise ValueError(message)
    story_by_id = {item.story_id: item for item in planner_input.available_stories}
    if any(
        selected.story_item_id != story_by_id[selected.story_id].story_item_id
        for selected in parsed.selected_stories
    ):
        message = "Sprint planner changed a locked Story item identity."
        raise ValueError(message)
    specification = _specification_reference(
        version_id=planner_input.accepted_specification_version_id,
        spec_hash=planner_input.accepted_specification_hash,
        canonical_json=planner_input.accepted_specification_json,
    )
    for selected in parsed.selected_stories:
        parent = story_by_id[selected.story_id]
        for task in selected.tasks:
            validate_task_spec_references(
                specification,
                task,
                parent_story_spec_item_ids=parent.spec_item_ids,
            )

    return RecordSprintPlan(
        project_id=context.project_id,
        graph_version=context.graph_version,
        fact_fingerprint=context.fact_fingerprint,
        decision_fingerprint=context.decision_fingerprint,
        instance_key=context.instance_key,
        attempt_id=context.attempt_id,
        attempt_fingerprint=context.attempt_fingerprint,
        idempotency_key=context.idempotency_key,
        actor=context.actor,
        correlation_id=context.correlation_id,
        spec_version_id=planner_input.accepted_specification_version_id,
        spec_hash=planner_input.accepted_specification_hash,
        team_name=envelope.team_name,
        planner_output=parsed.model_dump(mode="json"),
    )


def _build_single_leaf_workflow(
    *,
    workflow_name: str,
    execution_node_name: str,
    leaf_agent: BaseAgent | Workflow,
    execution_settings: JsonObject,
) -> Workflow:
    """Build one bounded single-leaf recipe without domain routing policy."""
    timeout_seconds, max_attempts = _execution_limits(execution_settings)
    retry_config = RetryConfig(max_attempts=max_attempts)

    @node(
        name=execution_node_name,
        rerun_on_resume=True,
        retry_config=retry_config,
        timeout=timeout_seconds,
    )
    async def execute_leaf(
        context: Context,
        node_input: RecipeInput,
    ) -> RecipeOutput:
        generated = await context.run_node(
            leaf_agent,
            node_input=node_input.payload,
        )
        return RecipeOutput(payload=validate_structured_output(generated))

    return Workflow(
        name=workflow_name,
        retry_config=retry_config,
        timeout=timeout_seconds,
        input_schema=RecipeInput,
        output_schema=RecipeOutput,
        edges=[(START, execute_leaf)],
    )


def _build_sprint_workflow(
    *,
    leaf_agent: BaseAgent | Workflow,
    execution_settings: JsonObject,
) -> Workflow:
    """Pass only typed SprintPlannerInput to one bounded planner leaf."""
    timeout_seconds, max_attempts = _execution_limits(execution_settings)
    retry_config = RetryConfig(max_attempts=max_attempts)

    @node(
        name="execute_sprint_planner",
        rerun_on_resume=True,
        retry_config=retry_config,
        timeout=timeout_seconds,
    )
    async def execute_sprint_planner(
        context: Context,
        node_input: RecipeInput,
    ) -> RecipeOutput:
        envelope = _SprintRecipePayload.model_validate(node_input.payload)
        generated = await context.run_node(
            leaf_agent,
            node_input=envelope.planner_input.model_dump(mode="json"),
        )
        output = SprintPlannerOutput.model_validate(generated)
        return RecipeOutput(payload=output.model_dump(mode="json"))

    return Workflow(
        name="sprint_planning",
        retry_config=retry_config,
        timeout=timeout_seconds,
        input_schema=RecipeInput,
        output_schema=RecipeOutput,
        edges=[(START, execute_sprint_planner)],
    )


def _build_roadmap_workflow(
    *,
    leaf_agent: BaseAgent | Workflow,
    execution_settings: JsonObject,
) -> Workflow:
    """Pass one exact typed Roadmap root and validate parent coverage."""
    timeout_seconds, max_attempts = _execution_limits(execution_settings)
    retry_config = RetryConfig(max_attempts=max_attempts)

    @node(
        name="execute_roadmap_generator",
        rerun_on_resume=True,
        retry_config=retry_config,
        timeout=timeout_seconds,
    )
    async def execute_roadmap_generator(
        context: Context,
        node_input: RecipeInput,
    ) -> RecipeOutput:
        envelope = _RoadmapRecipePayload.model_validate(node_input.payload)
        generated = await context.run_node(
            leaf_agent,
            node_input=envelope.builder_input.model_dump(mode="json"),
        )
        output = RoadmapBuilderOutput.model_validate(generated)
        validate_roadmap_backlog_coverage(
            output,
            (item.backlog_item_id for item in envelope.builder_input.backlog_items),
        )
        return RecipeOutput(payload=output.model_dump(mode="json"))

    return Workflow(
        name="roadmap_generation",
        retry_config=retry_config,
        timeout=timeout_seconds,
        input_schema=RecipeInput,
        output_schema=RecipeOutput,
        edges=[(START, execute_roadmap_generator)],
    )


def _build_story_workflow(
    *,
    leaf_agent: BaseAgent | Workflow,
    correction_leaf_agent: BaseAgent | Workflow | None,
    execution_settings: JsonObject,
) -> Workflow:
    """Pass one exact typed Story root and host-mint its immutable items."""
    timeout_seconds, max_attempts = _execution_limits(execution_settings)
    retry_config = RetryConfig(max_attempts=max_attempts)

    @node(
        name="execute_story_generator",
        rerun_on_resume=True,
        retry_config=retry_config,
        timeout=timeout_seconds,
    )
    async def execute_story_generator(
        context: Context,
        node_input: RecipeInput,
    ) -> RecipeOutput:
        envelope = _StoryRecipePayload.model_validate(node_input.payload)
        if envelope.correction is not None:
            if correction_leaf_agent is None:
                message = "Story correction requires its injected patch leaf."
                raise ValueError(message)
            leaf = correction_leaf_agent
        else:
            leaf = leaf_agent
        generated = await context.run_node(
            leaf,
            node_input=envelope.writer_input.model_dump(mode="json"),
        )
        output = UserStoryWriterOutput.model_validate(generated)
        writer_input = envelope.writer_input
        specification = _specification_reference(
            version_id=writer_input.accepted_specification_version_id,
            spec_hash=writer_input.accepted_specification_hash,
            canonical_json=writer_input.accepted_specification_json,
        )
        agent_items = output.user_stories
        if envelope.correction is not None:
            source = envelope.correction_source
            correction = envelope.correction
            if (
                source is None
                or envelope.supersedes_story_artifact_id
                != correction.source_story_artifact_id
                or canonical_hash(source.model_dump(mode="json"))
                != correction.source_story_artifact_fingerprint
                or not source.is_complete
                or source.clarifying_questions
                or not output.is_complete
                or output.clarifying_questions
                or len(output.user_stories) != 1
            ):
                message = "Story correction requires one complete replacement item."
                raise ValueError(message)
            matching = tuple(
                index
                for index, item in enumerate(source.story_items)
                if item.item.story_item_id == correction.source_story_item_id
                and item.item_fingerprint == correction.source_story_item_fingerprint
            )
            if len(matching) != 1:
                message = "Story correction source item identity is invalid."
                raise ValueError(message)
            source_items = [
                UserStoryAgentItem.model_validate(
                    item.item.model_dump(
                        mode="json",
                        exclude={"story_item_id", "persona"},
                    )
                )
                for item in source.story_items
            ]
            source_items[matching[0]] = output.user_stories[0]
            agent_items = tuple(source_items)
        canonical = CanonicalStoryOutput(
            story_items=canonicalize_story_items(
                specification,
                parent_backlog_spec_item_ids=(
                    writer_input.parent_backlog_spec_item_ids
                ),
                agent_items=agent_items,
            ),
            is_complete=output.is_complete,
            clarifying_questions=output.clarifying_questions,
        )
        return RecipeOutput(payload=canonical.model_dump(mode="json"))

    return Workflow(
        name="story_generation",
        retry_config=retry_config,
        timeout=timeout_seconds,
        input_schema=RecipeInput,
        output_schema=RecipeOutput,
        edges=[(START, execute_story_generator)],
    )


def build_specification_structuring_workflow(
    *,
    leaf_agent: BaseAgent | Workflow,
    execution_settings: JsonObject,
) -> Workflow:
    """Validate exact host input and typed output around one structuring leaf."""
    timeout_seconds, max_attempts = _execution_limits(execution_settings)
    retry_config = RetryConfig(max_attempts=max_attempts)

    @node(
        name="execute_specification_structurer",
        rerun_on_resume=True,
        retry_config=retry_config,
        timeout=timeout_seconds,
    )
    async def execute_specification_structurer(
        context: Context,
        node_input: RecipeInput,
    ) -> RecipeOutput:
        structuring_input = SpecificationStructuringInput.model_validate(
            node_input.payload
        )
        revalidated = revalidate_specification_attempt("before_provider")
        if revalidated is not None and (not revalidated.ok or revalidated.replayed):
            raise AttemptRevalidationError(revalidated)
        generated = await context.run_node(
            leaf_agent,
            node_input=structuring_input.model_dump(mode="json"),
        )
        revalidated = revalidate_specification_attempt("after_provider")
        if revalidated is not None and (not revalidated.ok or revalidated.replayed):
            raise AttemptRevalidationError(revalidated)
        try:
            output = SpecificationStructuringOutput.model_validate(generated)
        except ValidationError as error:
            payload = generated.get("payload") if isinstance(generated, dict) else None
            schema_version = (
                payload.get("schema_version") if isinstance(payload, dict) else None
            )
            if schema_version is not None and schema_version != SCHEMA_VERSION:
                code = WorkflowErrorCode.UNSUPPORTED_SPECIFICATION_SCHEMA
                message = "Specification structurer returned an unsupported schema."
            else:
                code = WorkflowErrorCode.INVALID_SPECIFICATION_PAYLOAD
                message = "Specification structurer returned an invalid v2 payload."
            raise SpecificationAgenticExecutionError(
                code=code,
                message=message,
            ) from error
        return RecipeOutput(
            payload=specification_structuring_completion_payload(output)
        )

    return Workflow(
        name="specification_structuring",
        retry_config=retry_config,
        timeout=timeout_seconds,
        input_schema=RecipeInput,
        output_schema=RecipeOutput,
        edges=[(START, execute_specification_structurer)],
    )


def build_vision_workflow(
    *,
    primary_leaf: BaseAgent | Workflow,
    repair_leaf: BaseAgent | Workflow | None = None,
    execution_settings: JsonObject,
) -> Workflow:
    """Run Vision once, with at most one semantic repair call."""
    timeout_seconds, _max_attempts = _execution_limits(execution_settings)
    retry_config = RetryConfig(max_attempts=1)

    @node(
        name="generate_vision_draft",
        rerun_on_resume=True,
        retry_config=retry_config,
        timeout=timeout_seconds,
    )
    async def generate_vision_draft(
        context: Context,
        node_input: RecipeInput,
    ) -> RecipeOutput:
        envelope = VisionAgentInput.model_validate(node_input.payload)
        if (
            envelope.preflight is not None
            and envelope.preflight.expected_evidence_fingerprint
            != envelope.preflight.observed_evidence.evidence_fingerprint
        ):
            raise VisionAgenticPreflightError(
                code=WorkflowErrorCode.VISION_EVIDENCE_STALE,
                message="Vision evidence changed before provider invocation.",
            )
        generated = await context.run_node(
            primary_leaf,
            node_input=VisionModelInput(request=envelope.request).model_dump(
                mode="json"
            ),
        )
        draft = VisionDraftOutput.model_validate(validate_structured_output(generated))
        try:
            validate_vision_draft(draft, envelope.request)
        except VisionDraftValidationError as error:
            if repair_leaf is None:
                raise
            repair_input = VisionRepairInput(
                schema_version="agileforge.vision-repair.v1",
                operation="repair",
                validation_findings=error.findings,
                invalid_output=draft,
                allowed_evidence_ids=tuple(
                    item.evidence_id for item in envelope.request.evidence.items
                ),
                human_input_available=isinstance(
                    envelope.request,
                    VisionClarificationInput,
                )
                or envelope.request.operation == "revision",
            )
            repaired = await context.run_node(
                repair_leaf,
                node_input=repair_input.model_dump(mode="json"),
            )
            draft = VisionDraftOutput.model_validate(
                validate_structured_output(repaired)
            )
            validate_vision_draft(draft, envelope.request)
        return RecipeOutput(payload=draft.model_dump(mode="json"))

    return Workflow(
        name="vision_generation",
        retry_config=retry_config,
        timeout=timeout_seconds,
        input_schema=RecipeInput,
        output_schema=RecipeOutput,
        edges=[(START, generate_vision_draft)],
    )


def build_backlog_generation_workflow(
    *,
    leaf_agent: BaseAgent | Workflow,
    execution_settings: JsonObject,
) -> Workflow:
    """Generate once, validate in parallel, and join one Backlog artifact."""
    timeout_seconds, max_attempts = _execution_limits(execution_settings)
    retry_config = RetryConfig(max_attempts=max_attempts)

    def canonical_backlog(node_input: _UnvalidatedRecipeOutput) -> RecipeOutput:
        envelope = _BacklogRecipePayload.model_validate(node_input.input_payload)
        output = BacklogAgentOutput.model_validate(node_input.payload)
        specification = _specification_reference(
            version_id=envelope.builder_input.accepted_specification_version_id,
            spec_hash=envelope.builder_input.accepted_specification_hash,
            canonical_json=envelope.builder_input.accepted_specification_json,
        )
        canonical = BacklogOutput(
            backlog_items=canonicalize_backlog_items(
                specification,
                output.backlog_items,
            ),
            is_complete=output.is_complete,
            clarifying_questions=output.clarifying_questions,
        )
        return RecipeOutput(payload=canonical.model_dump(mode="json"))

    @node(
        name="generate_backlog",
        rerun_on_resume=True,
        retry_config=retry_config,
        timeout=timeout_seconds,
    )
    async def generate_backlog(
        context: Context,
        node_input: RecipeInput,
    ) -> _UnvalidatedRecipeOutput:
        envelope = _BacklogRecipePayload.model_validate(node_input.payload)
        generated = await context.run_node(
            leaf_agent,
            node_input=envelope.builder_input.model_dump(mode="json"),
        )
        return _UnvalidatedRecipeOutput(
            payload=generated,
            input_payload=node_input.payload,
        )

    @node(
        name="validate_structure",
        rerun_on_resume=True,
        retry_config=retry_config,
        timeout=timeout_seconds,
    )
    async def validate_structure(
        node_input: _UnvalidatedRecipeOutput,
    ) -> RecipeOutput:
        return canonical_backlog(node_input)

    @node(
        name="validate_round_trip",
        rerun_on_resume=True,
        retry_config=retry_config,
        timeout=timeout_seconds,
    )
    async def validate_round_trip(
        node_input: _UnvalidatedRecipeOutput,
    ) -> RecipeOutput:
        validated = canonical_backlog(node_input)
        return RecipeOutput.model_validate_json(validated.model_dump_json())

    join_validations = JoinNode(
        name="join_backlog_validations",
        rerun_on_resume=True,
        retry_config=retry_config,
        timeout=timeout_seconds,
        output_schema=_JoinedBacklogValidations,
    )

    @node(
        name="emit_validated_backlog",
        rerun_on_resume=True,
        retry_config=retry_config,
        timeout=timeout_seconds,
    )
    async def emit_validated_backlog(
        node_input: _JoinedBacklogValidations,
    ) -> RecipeOutput:
        if node_input.validate_structure != node_input.validate_round_trip:
            msg = "Parallel Backlog validations produced different output."
            raise ValueError(msg)
        return node_input.validate_structure

    return Workflow(
        name="backlog_generation",
        retry_config=retry_config,
        timeout=timeout_seconds,
        input_schema=RecipeInput,
        output_schema=RecipeOutput,
        edges=[
            (
                START,
                generate_backlog,
                (validate_structure, validate_round_trip),
                join_validations,
                emit_validated_backlog,
            )
        ],
    )


def build_agentic_recipe_registry(
    *,
    nodes: AgenticRecipeNodes,
    execution_settings: JsonObject,
) -> AdkRecipeRegistry:
    """Compose exactly one execution-only recipe for every agentic domain node."""
    return AdkRecipeRegistry(
        (
            AdkRecipe(
                node_id="vision.bootstrap",
                workflow=build_vision_workflow(
                    primary_leaf=nodes.vision_interview,
                    repair_leaf=nodes.vision_repair,
                    execution_settings=execution_settings,
                ),
                output_adapter=_vision_interview_output_adapter,
            ),
            AdkRecipe(
                node_id="vision.interview",
                workflow=build_vision_workflow(
                    primary_leaf=nodes.vision_interview,
                    repair_leaf=nodes.vision_repair,
                    execution_settings=execution_settings,
                ),
                output_adapter=_vision_interview_output_adapter,
            ),
            AdkRecipe(
                node_id="goal.interview",
                workflow=_build_single_leaf_workflow(
                    workflow_name="product_goal_interview",
                    execution_node_name="execute_product_goal_interviewer",
                    leaf_agent=nodes.product_goal,
                    execution_settings=execution_settings,
                ),
                output_adapter=_product_goal_interview_output_adapter,
            ),
            AdkRecipe(
                node_id="specification.structure",
                workflow=build_specification_structuring_workflow(
                    leaf_agent=nodes.specification_structurer,
                    execution_settings=execution_settings,
                ),
                output_adapter=_request_output_adapter(
                    CompleteSpecificationStructuring
                ),
            ),
            AdkRecipe(
                node_id="backlog.generate",
                workflow=build_backlog_generation_workflow(
                    leaf_agent=nodes.backlog_generation,
                    execution_settings=execution_settings,
                ),
                output_adapter=_backlog_output_adapter,
            ),
            AdkRecipe(
                node_id="planning.roadmap.generate",
                workflow=_build_roadmap_workflow(
                    leaf_agent=nodes.roadmap_generation,
                    execution_settings=execution_settings,
                ),
                output_adapter=_roadmap_output_adapter,
            ),
            AdkRecipe(
                node_id="planning.story.generate",
                workflow=_build_story_workflow(
                    leaf_agent=nodes.story_generation,
                    correction_leaf_agent=nodes.story_correction,
                    execution_settings=execution_settings,
                ),
                output_adapter=_story_output_adapter,
            ),
            AdkRecipe(
                node_id="planning.sprint.plan",
                workflow=_build_sprint_workflow(
                    leaf_agent=nodes.sprint_planning,
                    execution_settings=execution_settings,
                ),
                output_adapter=_sprint_output_adapter,
            ),
        ),
        required_node_ids=AGENTIC_NODE_IDS,
    )


__all__ = [
    "AGENTIC_NODE_IDS",
    "AdkRecipe",
    "AdkRecipeRegistry",
    "AgenticRecipeNodes",
    "AttemptCompletionContext",
    "OutputAdapter",
    "RecipeInput",
    "RecipeOutput",
    "UnknownAdkRecipeError",
    "build_agentic_recipe_registry",
    "build_backlog_generation_workflow",
    "build_specification_structuring_workflow",
    "build_vision_workflow",
    "validate_structured_output",
]
