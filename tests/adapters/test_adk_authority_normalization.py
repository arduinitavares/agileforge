# tests/adapters/test_adk_authority_normalization.py
"""Provider-free regression coverage for the Authority compiler boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from google.adk import Workflow
from google.adk.agents import BaseAgent, InvocationContext
from google.adk.apps import App, ResumabilityConfig
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import START, node
from google.genai import types
from sqlmodel import Session, select

from adapters.adk.prompts.specification import (
    SPEC_AUTHORITY_COMPILER_PROMPT_HASH,
    SPEC_AUTHORITY_COMPILER_VERSION,
)
from adapters.adk.recipes import (
    AgenticRecipeNodes,
    RecipeInput,
    RecipeOutput,
    build_agentic_recipe_registry,
)
from adapters.adk.runner import (
    AdkExecutionConfig,
    AdkRunGuards,
    AdkWorkflowRunner,
)
from models.core import Project
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance
from models.workflow import WorkflowNodeAttempt, WorkflowNodeAttemptOutcome
from services.application import AuthorityRepairInputService
from services.authority_compilation_input import AuthorityCompilationInputService
from services.authority_review_projection import (
    AuthorityReviewSnapshot,
    build_authority_review_snapshot_in_session,
)
from services.contracts.authority_input_v2 import AuthorityInputV2, AuthorityItemV2
from services.contracts.specification_normalizer import normalize_compiler_output
from tests.workflow.lifecycle_fixtures import seed_accepted_specification
from utils.spec_schemas import (
    SpecAuthorityCompilationFailure,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerEnvelope,
    SpecAuthorityCompilerInput,
    SpecAuthorityCompilerOutput,
    SpecAuthorityValidationRepairInput,
)
from workflow.clock import FixedClock
from workflow.definitions.authority import authority_graph
from workflow.domain import WorkflowDomain
from workflow.requests import DecideAuthority, RecordAuthorityFeedback

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from typing import Any

    from sqlalchemy.engine import Engine

    from adapters.adk.recipes import AdkRecipeRegistry
    from workflow.contracts import (
        JsonObject,
        NodeDecision,
        TransitionResult,
        WorkflowPosition,
    )

EVALUATED_AT = datetime(2026, 8, 14, 12, tzinfo=UTC)
EXECUTION_SETTINGS: JsonObject = {"timeout_seconds": 5.0, "max_attempts": 2}
LEASE_SECONDS = 60
EXPECTED_REPAIRED_AUTHORITY_COUNT = 2
INVALID_OUTPUT_EVIDENCE_LIMIT = 131_072
SOURCE_ID = "REQ.issue-205.authority-boundary"
SOURCE_STATEMENT = "Authority output MUST preserve typed requirements."
NON_FINITE_SOURCE = "The score MUST be at most NaN."
ISSUE_208_FIXTURE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "benchmarks"
    / "authority-quality"
    / "string-calculator-tooling-constraint"
)
ISSUE_209_FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "authority"
    / "issue_209_attempt_29"
)


@dataclass(frozen=True)
class _RecipeRunFixture:
    """Optional provider-free inputs for one direct recipe execution."""

    compiler_input: SpecAuthorityCompilerInput | None = None
    validation_repair_response: object | None = None
    validation_repair_observations: list[SpecAuthorityValidationRepairInput] | None = (
        None
    )
    initial_leaf: BaseAgent | Workflow | None = None


@dataclass(frozen=True)
class _AuthorityRunnerResponses:
    """Provider-free initial and validation-repair responses for both paths."""

    compile: object
    repair: object
    compile_validation_repair: object | None = None
    repair_validation_repair: object | None = None


class CountingAuthorityLeaf(BaseAgent):
    """Return one deterministic provider-free value and record executions."""

    response: object
    calls: list[str]

    async def _run_async_impl(
        self,
        ctx: InvocationContext,
    ) -> AsyncGenerator[Event, None]:
        del ctx
        self.calls.append(self.name)
        yield Event(author=self.name, output=self.response)


def _unused_leaf(name: str) -> CountingAuthorityLeaf:
    return CountingAuthorityLeaf(name=name, response={}, calls=[])


def _counting_leaf(
    *,
    name: str,
    response: object,
    calls: list[str],
) -> CountingAuthorityLeaf:
    """Keep the caller-owned observation list instead of Pydantic's copy."""
    leaf = CountingAuthorityLeaf(name=name, response=response, calls=calls)
    leaf.calls = calls
    return leaf


def _validation_repair_leaf(
    *,
    response: object,
    calls: list[str],
    observations: list[SpecAuthorityValidationRepairInput],
) -> Workflow:
    """Return one typed provider-free repair leaf and capture its exact input."""

    @node(name="record_authority_validation_repair", rerun_on_resume=True)
    async def record_authority_validation_repair(
        node_input: SpecAuthorityValidationRepairInput,
    ) -> object:
        calls.append("authority_validation_repair")
        observations.append(node_input)
        return response

    return Workflow(
        name="authority_validation_repair_provider",
        input_schema=SpecAuthorityValidationRepairInput,
        edges=[(START, record_authority_validation_repair)],
    )


def _failing_authority_leaf(*, name: str, calls: list[str]) -> Workflow:
    """Raise one provider-like exception before any compiler output exists."""

    @node(name=f"raise_{name}", rerun_on_resume=True)
    async def raise_provider_failure(
        node_input: SpecAuthorityCompilerInput,
    ) -> object:
        del node_input
        calls.append(name)
        message = "provider transport failed before output"
        raise RuntimeError(message)

    return Workflow(
        name=f"{name}_workflow",
        input_schema=SpecAuthorityCompilerInput,
        edges=[(START, raise_provider_failure)],
    )


def _authority_input() -> AuthorityInputV2:
    item = AuthorityItemV2(
        id=SOURCE_ID,
        type="REQ",
        statement=SOURCE_STATEMENT,
        level="MUST",
        acceptance=(
            "Host normalization remains authoritative.",
            "Authority output MUST include first_field.",
            "Authority output MUST include second_field.",
            NON_FINITE_SOURCE,
        ),
    )
    return AuthorityInputV2(
        artifact_id="SPEC.issue-205",
        normative_items=(item,),
        normative_relations=(),
        eligible_item_ids=(SOURCE_ID,),
        authority_input_fingerprint="sha256:" + ("a" * 64),
    )


def _compiler_input() -> SpecAuthorityCompilerInput:
    return SpecAuthorityCompilerInput(
        authority_input=_authority_input(),
        project_id=1,
        spec_version_id=2,
        specification_fingerprint="sha256:" + ("b" * 64),
    )


def _issue_208_authority_input() -> AuthorityInputV2:
    return AuthorityInputV2.model_validate_json(
        (ISSUE_208_FIXTURE_ROOT / "source/authority-input.json").read_text(
            encoding="utf-8"
        )
    )


def _issue_208_compiler_input() -> SpecAuthorityCompilerInput:
    return SpecAuthorityCompilerInput(
        authority_input=_issue_208_authority_input(),
        project_id=1,
        spec_version_id=2,
        specification_fingerprint="sha256:" + ("2" * 64),
    )


def _issue_208_response(*, gold: bool) -> str:
    candidate_dir = "gold-authority" if gold else "generated-authority"
    return (
        ISSUE_208_FIXTURE_ROOT / f"agileforge/{candidate_dir}/compiled-authority.json"
    ).read_text(encoding="utf-8")


def _issue_208_response_with_field_name(field_name: str) -> str:
    payload = json.loads(_issue_208_response(gold=True))
    payload["invariants"][0]["parameters"]["field_name"] = field_name
    return json.dumps(payload)


def _issue_209_authority_input() -> AuthorityInputV2:
    return AuthorityInputV2.model_validate_json(
        (ISSUE_209_FIXTURE_ROOT / "authority-input.json").read_text(encoding="utf-8")
    )


def _issue_209_repair_payload(authority_input: AuthorityInputV2) -> dict[str, Any]:
    payload = _success_payload(label="attempt-29-repaired")
    payload["gaps"] = [
        f"{item_id}: provider-free validation-repair fixture."
        for item_id in authority_input.eligible_item_ids
    ]
    return payload


def _success_payload(*, label: str = "compile") -> dict[str, Any]:
    return {
        "schema_version": "agileforge.compiled_authority.v3",
        "scope_themes": [f"Issue 205 {label}"],
        "domain": "authority compiler boundary",
        "invariants": [],
        "eligible_feature_rules": [],
        "rejected_features": [],
        "gaps": [f"{SOURCE_ID}: represented by a provider-free gap."],
        "assumptions": [],
        "source_map": [],
        "compiler_version": "provider-placeholder",
        "prompt_hash": "0" * 64,
    }


def _ambiguous_payload() -> dict[str, Any]:
    payload = _success_payload(label="ambiguous")
    payload["gaps"] = []
    payload["invariants"] = [
        {
            "id": "INV-0000000000000000",
            "type": "REQUIRED_FIELD",
            "source_item_id": SOURCE_ID,
            "source_level": "MUST",
            "parameters": {"field_name": "first_field"},
        },
        {
            "id": "INV-0000000000000000",
            "type": "REQUIRED_FIELD",
            "source_item_id": SOURCE_ID,
            "source_level": "MUST",
            "parameters": {"field_name": "second_field"},
        },
    ]
    payload["source_map"] = [
        {
            "invariant_id": "INV-0000000000000000",
            "excerpt": SOURCE_STATEMENT,
            "location": SOURCE_ID,
        },
        {
            "invariant_id": "INV-0000000000000000",
            "excerpt": "Host normalization remains authoritative.",
            "location": SOURCE_ID,
        },
    ]
    return payload


def _distinct_multi_invariant_payload(
    *,
    label: str = "distinct-references",
) -> dict[str, Any]:
    payload = _success_payload(label=label)
    payload["gaps"] = []
    payload["invariants"] = [
        {
            "id": "INV-0000000000000000",
            "type": "REQUIRED_FIELD",
            "source_item_id": SOURCE_ID,
            "source_level": "MUST",
            "parameters": {"field_name": "first_field"},
        },
        {
            "id": "INV-0000000000000001",
            "type": "REQUIRED_FIELD",
            "source_item_id": SOURCE_ID,
            "source_level": "MUST",
            "parameters": {"field_name": "second_field"},
        },
    ]
    payload["source_map"] = [
        {
            "invariant_id": "INV-0000000000000000",
            "excerpt": "Authority output MUST include first_field.",
            "location": SOURCE_ID,
        },
        {
            "invariant_id": "INV-0000000000000001",
            "excerpt": "Authority output MUST include second_field.",
            "location": SOURCE_ID,
        },
    ]
    payload.update(
        {
            "ir_schema_version": "provider.ir.v1",
            "ir_provenance": "model_emitted",
            "authority_mappings": [
                {
                    "candidate_id": "CAND-first-field",
                    "authority_item_id": "INV-0000000000000000",
                    "authority_target_kind": "invariant",
                    "mapping_status": "covered",
                    "mapping_rationale": "Maps the first field requirement.",
                    "mapping_provenance": "model_quote",
                },
                {
                    "candidate_id": "CAND-second-field",
                    "authority_item_id": "INV-0000000000000001",
                    "authority_target_kind": "invariant",
                    "mapping_status": "covered",
                    "mapping_rationale": "Maps the second field requirement.",
                    "mapping_provenance": "model_quote",
                },
            ],
        }
    )
    return payload


def _assert_distinct_multi_invariant_lineage(
    compiled: SpecAuthorityCompilationSuccess,
) -> None:
    invariant_ids_by_field = {
        item.parameters.model_dump(mode="json")["field_name"]: item.id
        for item in compiled.invariants
    }
    expected_invariant_count = 2
    assert len(invariant_ids_by_field) == expected_invariant_count
    assert "INV-0000000000000000" not in invariant_ids_by_field.values()
    assert "INV-0000000000000001" not in invariant_ids_by_field.values()
    assert {entry.excerpt: entry.invariant_id for entry in compiled.source_map} == {
        "Authority output MUST include first_field.": invariant_ids_by_field[
            "first_field"
        ],
        "Authority output MUST include second_field.": invariant_ids_by_field[
            "second_field"
        ],
    }
    assert {
        mapping.candidate_id: mapping.authority_item_id
        for mapping in compiled.authority_mappings
    } == {
        "CAND-first-field": invariant_ids_by_field["first_field"],
        "CAND-second-field": invariant_ids_by_field["second_field"],
    }


def _typed_source_violation_payload() -> dict[str, Any]:
    payload = _success_payload(label="typed-source")
    payload["gaps"] = []
    payload["invariants"] = [
        {
            "id": "INV-0000000000000001",
            "type": "REQUIRED_FIELD",
            "source_item_id": "REQ.outside-authority-input",
            "source_level": "MUST",
            "parameters": {"field_name": "outside_field"},
        }
    ]
    payload["source_map"] = [
        {
            "invariant_id": "INV-0000000000000001",
            "excerpt": "An outside requirement MUST include outside_field.",
            "location": "REQ.outside-authority-input",
        }
    ]
    return payload


def _coverage_gap_payload() -> dict[str, Any]:
    payload = _success_payload(label="coverage")
    payload["gaps"] = []
    return payload


def _non_finite_payload() -> dict[str, Any]:
    payload = _success_payload(label="non-finite")
    payload["gaps"] = []
    payload["invariants"] = [
        {
            "id": "INV-0000000000000002",
            "type": "MAX_VALUE",
            "source_item_id": SOURCE_ID,
            "source_level": "MUST",
            "parameters": {"field_name": "score", "max_value": float("nan")},
        }
    ]
    payload["source_map"] = [
        {
            "invariant_id": "INV-0000000000000002",
            "excerpt": NON_FINITE_SOURCE,
            "location": SOURCE_ID,
        }
    ]
    return payload


def _recipe_payload(
    node_id: str,
    *,
    compiler_input: SpecAuthorityCompilerInput | None = None,
) -> JsonObject:
    serialized_compiler_input = (compiler_input or _compiler_input()).model_dump(
        mode="json"
    )
    if node_id == "authority.compile":
        return {
            "project_id": 1,
            "spec_version_id": 2,
            "expected_spec_hash": "sha256:" + ("b" * 64),
            "compiler_model": "fake/compiler",
            "compiler_input": serialized_compiler_input,
        }
    return {
        "source_authority_id": 7,
        "source_authority_fingerprint": "sha256:" + ("c" * 64),
        "compiler_input": serialized_compiler_input,
    }


def _registry(
    *,
    compile_leaf: BaseAgent | Workflow,
    repair_leaf: BaseAgent | Workflow,
    compile_validation_repair_leaf: BaseAgent | Workflow,
    repair_validation_repair_leaf: BaseAgent | Workflow,
) -> AdkRecipeRegistry:
    return build_agentic_recipe_registry(
        nodes=AgenticRecipeNodes(
            authority_compile=compile_leaf,
            authority_repair=repair_leaf,
            authority_compile_validation_repair=compile_validation_repair_leaf,
            authority_repair_validation_repair=repair_validation_repair_leaf,
            vision_interview=_unused_leaf("unused_vision_interview"),
            vision_repair=_unused_leaf("unused_vision_repair"),
            product_goal=_unused_leaf("unused_product_goal"),
            specification_structurer=_unused_leaf("unused_specification_structurer"),
            backlog_generation=_unused_leaf("unused_backlog"),
            roadmap_generation=_unused_leaf("unused_roadmap"),
            story_generation=_unused_leaf("unused_story"),
            sprint_planning=_unused_leaf("unused_sprint"),
        ),
        execution_settings=EXECUTION_SETTINGS,
    )


async def _run_recipe_async(
    node_id: str,
    *,
    response: object,
    calls: list[str],
    fixture: _RecipeRunFixture | None = None,
) -> RecipeOutput:
    options = fixture or _RecipeRunFixture()
    target_leaf = options.initial_leaf or _counting_leaf(
        name=node_id.replace(".", "_"), response=response, calls=calls
    )
    observations = (
        options.validation_repair_observations
        if options.validation_repair_observations is not None
        else []
    )
    validation_repair_leaf = _validation_repair_leaf(
        response=(
            response
            if options.validation_repair_response is None
            else options.validation_repair_response
        ),
        calls=calls,
        observations=observations,
    )
    registry = _registry(
        compile_leaf=(
            target_leaf if node_id == "authority.compile" else _unused_leaf("compile")
        ),
        repair_leaf=(
            target_leaf if node_id == "authority.repair" else _unused_leaf("repair")
        ),
        compile_validation_repair_leaf=(
            validation_repair_leaf
            if node_id == "authority.compile"
            else _unused_leaf("unused_compile_validation_repair")
        ),
        repair_validation_repair_leaf=(
            validation_repair_leaf
            if node_id == "authority.repair"
            else _unused_leaf("unused_repair_validation_repair")
        ),
    )
    recipe = registry.require(node_id)
    session_service = InMemorySessionService()
    session_id = node_id.replace(".", "-")
    await session_service.create_session(
        app_name="authority_normalization_recipe_test",
        user_id="authority_normalization_recipe_user",
        session_id=session_id,
    )
    app = App(
        name="authority_normalization_recipe_test",
        root_agent=recipe.workflow,
        resumability_config=ResumabilityConfig(is_resumable=True),
    )
    runner = Runner(app=app, session_service=session_service)
    message = types.Content(
        role="user",
        parts=[
            types.Part(
                text=RecipeInput(
                    payload=_recipe_payload(
                        node_id,
                        compiler_input=options.compiler_input,
                    )
                ).model_dump_json()
            )
        ],
    )
    output: object | None = None
    async for event in runner.run_async(
        user_id="authority_normalization_recipe_user",
        session_id=session_id,
        new_message=message,
    ):
        if event.output is not None:
            output = event.output
    if output is None:
        message = "Authority recipe produced no output."
        raise AssertionError(message)
    return RecipeOutput.model_validate(output)


def _run_recipe(
    node_id: str,
    *,
    response: object,
    calls: list[str],
    fixture: _RecipeRunFixture | None = None,
) -> RecipeOutput:
    return asyncio.run(
        _run_recipe_async(
            node_id,
            response=response,
            calls=calls,
            fixture=fixture,
        )
    )


def _success_response(kind: str) -> object:
    payload = _success_payload()
    if kind == "direct_json":
        return json.dumps(payload)
    if kind == "envelope_json":
        return json.dumps({"result": payload})
    if kind == "decoded_envelope":
        return {"result": payload}
    if kind == "model_envelope":
        return SpecAuthorityCompilerEnvelope(
            result=SpecAuthorityCompilationSuccess.model_validate(payload)
        )
    raise AssertionError(kind)


@pytest.mark.parametrize("node_id", ["authority.compile", "authority.repair"])
@pytest.mark.parametrize(
    "response_kind",
    ["direct_json", "envelope_json", "decoded_envelope", "model_envelope"],
)
def test_authority_recipes_normalize_supported_compiler_output_shapes(
    node_id: str,
    response_kind: str,
) -> None:
    """Compile and repair share one raw and structured normalization boundary."""
    calls: list[str] = []

    output = _run_recipe(
        node_id,
        response=_success_response(response_kind),
        calls=calls,
    )

    compiled = output.payload["compiled_authority"]
    assert isinstance(compiled, dict)
    assert compiled["compiler_version"] == SPEC_AUTHORITY_COMPILER_VERSION
    assert compiled["prompt_hash"] == SPEC_AUTHORITY_COMPILER_PROMPT_HASH
    assert calls == [node_id.replace(".", "_")]


@pytest.mark.parametrize("node_id", ["authority.compile", "authority.repair"])
def test_authority_recipes_preserve_distinct_multi_invariant_references(
    node_id: str,
) -> None:
    """Compile and repair rewrite only exact temporary provider identities."""
    calls: list[str] = []

    output = _run_recipe(
        node_id,
        response=json.dumps(_distinct_multi_invariant_payload()),
        calls=calls,
    )

    compiled = SpecAuthorityCompilationSuccess.model_validate(
        output.payload["compiled_authority"]
    )
    _assert_distinct_multi_invariant_lineage(compiled)
    assert calls == [node_id.replace(".", "_")]


@pytest.mark.parametrize("node_id", ["authority.compile", "authority.repair"])
def test_attempt_28_tooling_constraint_fails_closed_in_compile_and_repair(
    node_id: str,
) -> None:
    """The captured paraphrase/misclassification never crosses the host boundary."""
    calls: list[str] = []

    with pytest.raises(RuntimeError) as captured:
        _run_recipe(
            node_id,
            response=_issue_208_response(gold=False),
            calls=calls,
            fixture=_RecipeRunFixture(compiler_input=_issue_208_compiler_input()),
        )

    error = captured.value
    code = getattr(error, "code", None)
    code_value = getattr(code, "value", code)
    assert type(error).__name__ == "AuthorityAgenticExecutionError"
    assert code_value == "AUTHORITY_COMPILATION_FAILED"
    assert "INELIGIBLE_INVARIANT_SOURCE" in str(error)
    assert "CONSTRAINT.001 semantics" in str(error)
    assert calls == [node_id.replace(".", "_"), "authority_validation_repair"]


def test_attempt_29_rebinds_temporary_ids_then_repairs_semantic_failure() -> None:
    """The exact Manual Test output crosses both bounded recovery boundaries."""
    authority_input = _issue_209_authority_input()
    raw_output = (ISSUE_209_FIXTURE_ROOT / "compiler-output.json").read_text(
        encoding="utf-8"
    )
    manifest = json.loads(
        (ISSUE_209_FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8")
    )
    assert isinstance(manifest, dict)
    assert (
        hashlib.sha256(
            (ISSUE_209_FIXTURE_ROOT / "authority-input.json").read_bytes()
        ).hexdigest()
        == manifest["authority_input_sha256"]
    )
    assert (
        hashlib.sha256(
            (ISSUE_209_FIXTURE_ROOT / "compiler-output.json").read_bytes()
        ).hexdigest()
        == manifest["compiler_output_sha256"]
    )
    assert [item["id"] for item in json.loads(raw_output)["invariants"][-4:]] == [
        "INV-0000000000010",
        "INV-0000000000011",
        "INV-0000000000012",
        "INV-0000000000013",
    ]

    initial = normalize_compiler_output(
        raw_output,
        authority_input=authority_input,
    ).root
    assert isinstance(initial, SpecAuthorityCompilationFailure)
    assert initial.reason == manifest["expected_initial_host_failure"]
    assert "parameters are not authorized" in initial.blocking_gaps[0]
    assert "match pattern" not in initial.blocking_gaps[0]

    calls: list[str] = []
    observations: list[SpecAuthorityValidationRepairInput] = []
    compiler_input = SpecAuthorityCompilerInput(
        authority_input=authority_input,
        project_id=1,
        spec_version_id=2,
        specification_fingerprint="sha256:" + ("d" * 64),
    )
    output = _run_recipe(
        "authority.compile",
        response=raw_output,
        calls=calls,
        fixture=_RecipeRunFixture(
            compiler_input=compiler_input,
            validation_repair_response=json.dumps(
                _issue_209_repair_payload(authority_input)
            ),
            validation_repair_observations=observations,
        ),
    )

    compiled = SpecAuthorityCompilationSuccess.model_validate(
        output.payload["compiled_authority"]
    )
    assert len(compiled.gaps) == len(authority_input.eligible_item_ids)
    assert calls == ["authority_compile", "authority_validation_repair"]
    assert len(observations) == 1
    assert observations[0].validation_failure == initial
    assert observations[0].invalid_output_excerpt == raw_output


@pytest.mark.parametrize("node_id", ["authority.compile", "authority.repair"])
@pytest.mark.parametrize("field_name", ["request-limit", "request_lim"])
def test_paraphrased_parameter_fails_closed_in_compile_and_repair(
    node_id: str,
    field_name: str,
) -> None:
    """Supported invariant parameters still must be copied verbatim."""
    calls: list[str] = []

    with pytest.raises(RuntimeError) as captured:
        _run_recipe(
            node_id,
            response=_issue_208_response_with_field_name(field_name),
            calls=calls,
            fixture=_RecipeRunFixture(compiler_input=_issue_208_compiler_input()),
        )

    error = captured.value
    code = getattr(error, "code", None)
    code_value = getattr(code, "value", code)
    assert type(error).__name__ == "AuthorityAgenticExecutionError"
    assert code_value == "AUTHORITY_COMPILATION_FAILED"
    assert "INELIGIBLE_INVARIANT_SOURCE" in str(error)
    assert "CONSTRAINT.002 semantics" in str(error)
    assert calls == [node_id.replace(".", "_"), "authority_validation_repair"]


@pytest.mark.parametrize("node_id", ["authority.compile", "authority.repair"])
def test_identifier_normalization_remains_supported_in_compile_and_repair(
    node_id: str,
) -> None:
    """The sole documented snake_case normalization remains valid."""
    calls: list[str] = []

    output = _run_recipe(
        node_id,
        response=_issue_208_response_with_field_name("request_limit"),
        calls=calls,
        fixture=_RecipeRunFixture(compiler_input=_issue_208_compiler_input()),
    )

    compiled = SpecAuthorityCompilationSuccess.model_validate(
        output.payload["compiled_authority"]
    )
    assert (
        compiled.invariants[0].parameters.model_dump(mode="json")["field_name"]
        == "request_limit"
    )
    assert calls == [node_id.replace(".", "_")]


@pytest.mark.parametrize("node_id", ["authority.compile", "authority.repair"])
def test_tooling_gap_and_measurable_constraint_normalize_in_compile_and_repair(
    node_id: str,
) -> None:
    """Both compiler paths accept the exact gap and supported MAX_VALUE mapping."""
    calls: list[str] = []

    output = _run_recipe(
        node_id,
        response=_issue_208_response(gold=True),
        calls=calls,
        fixture=_RecipeRunFixture(compiler_input=_issue_208_compiler_input()),
    )

    compiled = SpecAuthorityCompilationSuccess.model_validate(
        output.payload["compiled_authority"]
    )
    assert compiled.gaps == [
        "CONSTRAINT.001: unsupported tooling requirement; enforce outside "
        "compiled Authority."
    ]
    assert len(compiled.invariants) == 1
    assert compiled.invariants[0].source_item_id == "CONSTRAINT.002"
    assert compiled.invariants[0].parameters.model_dump(mode="json") == {
        "field_name": "request limit",
        "max_value": 100,
    }
    assert calls == [node_id.replace(".", "_")]


@pytest.mark.parametrize("node_id", ["authority.compile", "authority.repair"])
def test_authority_recipes_run_one_feedback_informed_validation_repair(
    node_id: str,
) -> None:
    """A repairable host failure gets one distinct, typed correction attempt."""
    calls: list[str] = []
    observations: list[SpecAuthorityValidationRepairInput] = []
    compiler_input = _compiler_input()
    invalid_output = json.dumps(_typed_source_violation_payload())

    output = _run_recipe(
        node_id,
        response=invalid_output,
        calls=calls,
        fixture=_RecipeRunFixture(
            compiler_input=compiler_input,
            validation_repair_response=json.dumps(_success_payload(label="repaired")),
            validation_repair_observations=observations,
        ),
    )

    compiled = SpecAuthorityCompilationSuccess.model_validate(
        output.payload["compiled_authority"]
    )
    assert compiled.gaps == [f"{SOURCE_ID}: represented by a provider-free gap."]
    assert calls == [node_id.replace(".", "_"), "authority_validation_repair"]
    assert len(observations) == 1
    repair_input = observations[0]
    assert repair_input.compiler_input == compiler_input
    assert repair_input.validation_failure.reason == "INELIGIBLE_INVARIANT_SOURCE"
    assert (
        "outside-authority-input" in (repair_input.validation_failure.blocking_gaps[0])
    )
    assert repair_input.invalid_output_excerpt == invalid_output
    assert repair_input.invalid_output_fingerprint == (
        "sha256:" + hashlib.sha256(invalid_output.encode("utf-8")).hexdigest()
    )
    assert repair_input.invalid_output_length == len(invalid_output)
    assert repair_input.invalid_output_truncated is False
    assert repair_input.repair_ordinal == 1


def test_authority_validation_repair_evidence_is_bounded() -> None:
    """Large invalid output remains diagnostic data with a stable size and hash."""
    calls: list[str] = []
    observations: list[SpecAuthorityValidationRepairInput] = []
    invalid_output = "x" * 140_000

    _run_recipe(
        "authority.compile",
        response=invalid_output,
        calls=calls,
        fixture=_RecipeRunFixture(
            validation_repair_response=json.dumps(_success_payload(label="repaired")),
            validation_repair_observations=observations,
        ),
    )

    assert calls == ["authority_compile", "authority_validation_repair"]
    assert len(observations) == 1
    repair_input = observations[0]
    assert len(repair_input.invalid_output_excerpt) == INVALID_OUTPUT_EVIDENCE_LIMIT
    assert "<authority-compiler-output-truncated>" in (
        repair_input.invalid_output_excerpt
    )
    assert repair_input.invalid_output_excerpt.startswith("x")
    assert repair_input.invalid_output_excerpt.endswith("x")
    assert repair_input.invalid_output_length == len(invalid_output)
    assert repair_input.invalid_output_truncated is True
    assert repair_input.invalid_output_fingerprint == (
        "sha256:" + hashlib.sha256(invalid_output.encode("utf-8")).hexdigest()
    )


@pytest.mark.parametrize("node_id", ["authority.compile", "authority.repair"])
def test_authority_validation_repair_never_recurses(node_id: str) -> None:
    """A second invalid output ends the action after exactly two model calls."""
    calls: list[str] = []

    with pytest.raises(RuntimeError) as captured:
        _run_recipe(
            node_id,
            response="not initial JSON",
            calls=calls,
            fixture=_RecipeRunFixture(validation_repair_response="not repaired JSON"),
        )

    assert "Bounded Authority validation repair failed" in str(captured.value)
    assert "Initial:" in str(captured.value)
    assert "Final:" in str(captured.value)
    assert calls == [node_id.replace(".", "_"), "authority_validation_repair"]


@pytest.mark.parametrize("node_id", ["authority.compile", "authority.repair"])
def test_model_declared_terminal_failure_does_not_run_validation_repair(
    node_id: str,
) -> None:
    """A structured non-repairable provider failure remains terminal."""
    calls: list[str] = []
    failure = json.dumps(
        {
            "schema_version": "agileforge.compiled_authority.v3",
            "error": "SPEC_COMPILATION_FAILED",
            "reason": "MODEL_BLOCKED",
            "blocking_gaps": ["The provider refused this request."],
        }
    )

    with pytest.raises(RuntimeError, match="MODEL_BLOCKED"):
        _run_recipe(node_id, response=failure, calls=calls)

    assert calls == [node_id.replace(".", "_")]


@pytest.mark.parametrize("node_id", ["authority.compile", "authority.repair"])
def test_provider_exception_does_not_run_validation_repair(node_id: str) -> None:
    """No correction call occurs when the initial leaf produced no output."""
    calls: list[str] = []
    failing_leaf = _failing_authority_leaf(
        name=node_id.replace(".", "_"),
        calls=calls,
    )

    with pytest.raises(RuntimeError, match="provider transport failed"):
        _run_recipe(
            node_id,
            response={},
            calls=calls,
            fixture=_RecipeRunFixture(initial_leaf=failing_leaf),
        )

    assert calls == [node_id.replace(".", "_")]


@pytest.mark.parametrize("node_id", ["authority.compile", "authority.repair"])
@pytest.mark.parametrize(
    ("response", "reason", "detail"),
    [
        ("not JSON", "INVALID_JSON", "not valid JSON"),
        (
            json.dumps(
                {
                    "schema_version": "agileforge.compiled_authority.v3",
                    "unexpected": True,
                }
            ),
            "JSON_VALIDATION_FAILED",
            "schema error",
        ),
        (
            json.dumps(_typed_source_violation_payload()),
            "INELIGIBLE_INVARIANT_SOURCE",
            "outside-authority-input",
        ),
        (
            json.dumps(_coverage_gap_payload()),
            "INCOMPLETE_NORMATIVE_COVERAGE",
            SOURCE_ID,
        ),
        (
            json.dumps(_ambiguous_payload()),
            "JSON_VALIDATION_FAILED",
            "ambiguous repeated invariant identity",
        ),
        pytest.param(
            json.dumps(_non_finite_payload()),
            "INVALID_JSON",
            "not valid JSON",
            id="raw-non-finite-number",
        ),
        pytest.param(
            _non_finite_payload(),
            "INVALID_JSON",
            "not valid JSON",
            id="structured-non-finite-number",
        ),
    ],
)
def test_authority_recipes_raise_stable_domain_failures_after_normalization(
    node_id: str,
    response: object,
    reason: str,
    detail: str,
) -> None:
    """Every closed normalizer failure stays actionable in compile and repair."""
    calls: list[str] = []

    with pytest.raises(RuntimeError) as captured:
        _run_recipe(node_id, response=response, calls=calls)

    error = captured.value
    code = getattr(error, "code", None)
    code_value = getattr(code, "value", code)
    assert type(error).__name__ == "AuthorityAgenticExecutionError"
    assert code_value == "AUTHORITY_COMPILATION_FAILED"
    assert reason in str(error)
    assert detail in str(error)
    assert "validation error for SpecAuthorityCompilerEnvelope" not in str(error)
    assert calls == [node_id.replace(".", "_"), "authority_validation_repair"]


def _seed_compile_target(engine: Engine) -> tuple[int, int, str]:
    content = json.dumps(
        {
            "schema_version": "agileforge.spec.v2",
            "artifact_id": "SPEC.issue-205-runner",
            "title": "Issue 205 runner boundary",
            "summary": "Normalize compiler output before Authority persistence.",
            "problem_statement": "Raw compiler JSON must reach host normalization.",
            "items": [
                {
                    "id": SOURCE_ID,
                    "type": "REQ",
                    "title": "Normalize Authority output",
                    "statement": SOURCE_STATEMENT,
                    "level": "MUST",
                    "verification": "integration-test",
                    "acceptance": [
                        "Host normalization remains authoritative.",
                        "Authority output MUST include first_field.",
                        "Authority output MUST include second_field.",
                    ],
                }
            ],
            "relations": [],
            "controlled_terms": [],
            "external_references": [],
        }
    )
    with Session(engine) as session:
        project = Project(name="Issue 205 Authority boundary")
        session.add(project)
        session.flush()
        assert project.project_id is not None
        lineage = seed_accepted_specification(
            session,
            project_id=project.project_id,
            content=content,
            recorded_at=EVALUATED_AT - timedelta(minutes=1),
        )
        assert lineage.spec.spec_version_id is not None
        return (
            project.project_id,
            lineage.spec.spec_version_id,
            lineage.spec.spec_hash,
        )


def _seed_issue_208_compile_target(engine: Engine) -> tuple[int, int, str]:
    authority_input = _issue_208_authority_input()
    content = json.dumps(
        {
            "schema_version": "agileforge.spec.v2",
            "artifact_id": authority_input.artifact_id,
            "title": "Issue 208 tooling constraint classification",
            "summary": "Classify only faithfully representable Authority invariants.",
            "problem_statement": (
                "Tooling requirements must remain outside unsupported invariants."
            ),
            "items": [
                {
                    **item.model_dump(mode="json"),
                    "title": f"Authority source {item.id}",
                    "verification": "manual-review",
                }
                for item in authority_input.normative_items
            ],
            "relations": [],
            "controlled_terms": [],
            "external_references": [],
        }
    )
    with Session(engine) as session:
        project = Project(name="Issue 208 Authority classification")
        session.add(project)
        session.flush()
        assert project.project_id is not None
        lineage = seed_accepted_specification(
            session,
            project_id=project.project_id,
            content=content,
            recorded_at=EVALUATED_AT - timedelta(minutes=1),
        )
        assert lineage.spec.spec_version_id is not None
        return (
            project.project_id,
            lineage.spec.spec_version_id,
            lineage.spec.spec_hash,
        )


def _build_runner(
    engine: Engine,
    *,
    project_id: int,
    responses: _AuthorityRunnerResponses,
    calls: list[str],
) -> tuple[AdkWorkflowRunner, WorkflowDomain]:
    compile_repair_observations: list[SpecAuthorityValidationRepairInput] = []
    repair_repair_observations: list[SpecAuthorityValidationRepairInput] = []
    registry = _registry(
        compile_leaf=_counting_leaf(
            name="compile_provider",
            response=responses.compile,
            calls=calls,
        ),
        repair_leaf=_counting_leaf(
            name="repair_provider",
            response=responses.repair,
            calls=calls,
        ),
        compile_validation_repair_leaf=_validation_repair_leaf(
            response=(
                responses.compile
                if responses.compile_validation_repair is None
                else responses.compile_validation_repair
            ),
            calls=calls,
            observations=compile_repair_observations,
        ),
        repair_validation_repair_leaf=_validation_repair_leaf(
            response=(
                responses.repair
                if responses.repair_validation_repair is None
                else responses.repair_validation_repair
            ),
            calls=calls,
            observations=repair_repair_observations,
        ),
    )
    domain = WorkflowDomain(
        engine=engine,
        graph=authority_graph(),
        clock=FixedClock(now_value=EVALUATED_AT),
        adk_recipe_registry=registry,
    )
    runner = AdkWorkflowRunner(
        domain=domain,
        registry=registry,
        session_service=InMemorySessionService(),
        config=AdkExecutionConfig(
            project_id=project_id,
            model_id="fake/compiler",
            execution_settings=EXECUTION_SETTINGS,
            lease_seconds=LEASE_SECONDS,
            actor="operator@example.com",
        ),
    )
    return runner, domain


def _decision(position: WorkflowPosition, node_id: str) -> NodeDecision:
    return next(item for item in position.decisions if item.node_id == node_id)


def _run_compile(
    engine: Engine,
    *,
    runner: AdkWorkflowRunner,
    domain: WorkflowDomain,
    project_id: int,
    idempotency_key: str,
) -> tuple[TransitionResult, NodeDecision, JsonObject, AdkRunGuards]:
    position = domain.position(project_id)
    decision = _decision(position, "authority.compile")
    compiler_input = AuthorityCompilationInputService(engine=engine).build(
        project_id=project_id,
        decision=decision,
        compiler_model="fake/compiler",
    )
    guards = AdkRunGuards(
        position=position,
        idempotency_key=idempotency_key,
        actor="operator@example.com",
        correlation_id="issue-205",
    )
    return (
        runner.run(decision, compiler_input, guards=guards),
        decision,
        compiler_input,
        guards,
    )


def _reject_and_record_feedback(
    engine: Engine,
    *,
    domain: WorkflowDomain,
    project_id: int,
) -> NodeDecision:
    review_position = domain.position(project_id)
    with Session(engine) as session:
        review = build_authority_review_snapshot_in_session(
            session,
            project_id=project_id,
        )
    assert isinstance(review, AuthorityReviewSnapshot)
    assert review.pending_authority_id is not None
    assert review.authority_fingerprint is not None
    review_decision = _decision(review_position, "authority.review")
    rejected = domain.transition(
        DecideAuthority(
            project_id=project_id,
            graph_version=review_position.graph_version,
            fact_fingerprint=review_position.fact_fingerprint,
            decision_fingerprint=review_decision.decision_fingerprint,
            instance_key=review_decision.instance_key,
            idempotency_key="issue-205-reject",
            actor="operator@example.com",
            correlation_id="issue-205",
            pending_authority_id=review.pending_authority_id,
            authority_fingerprint=review.authority_fingerprint,
            review_fingerprint=review.review_fingerprint,
            decision="rejected",
            rationale="Exercise the repair compiler boundary.",
        )
    )
    assert rejected.ok is True
    assert rejected.position is not None
    feedback_decision = _decision(rejected.position, "authority.feedback")
    feedback = domain.transition(
        RecordAuthorityFeedback(
            project_id=project_id,
            graph_version=rejected.position.graph_version,
            fact_fingerprint=rejected.position.fact_fingerprint,
            decision_fingerprint=feedback_decision.decision_fingerprint,
            instance_key=feedback_decision.instance_key,
            idempotency_key="issue-205-feedback",
            actor="operator@example.com",
            correlation_id="issue-205",
            pending_authority_id=review.pending_authority_id,
            authority_fingerprint=review.authority_fingerprint,
            feedback={"summary": "Recompile through the same host boundary."},
        )
    )
    assert feedback.ok is True
    assert feedback.position is not None
    return _decision(feedback.position, "authority.repair")


def _run_repair(
    engine: Engine,
    *,
    runner: AdkWorkflowRunner,
    domain: WorkflowDomain,
    project_id: int,
    decision: NodeDecision,
) -> tuple[TransitionResult, JsonObject, AdkRunGuards]:
    position = domain.position(project_id)
    payload = AuthorityRepairInputService(engine=engine).build(
        project_id=project_id,
        decision=decision,
    )
    assert payload is not None
    guards = AdkRunGuards(
        position=position,
        idempotency_key="issue-205-repair-attempt",
        actor="operator@example.com",
        correlation_id="issue-205",
    )
    return runner.run(decision, payload, guards=guards), payload, guards


def _assert_authority_failure(result: TransitionResult) -> None:
    assert result.ok is False
    assert result.error is not None
    assert result.error.code.value == "AUTHORITY_COMPILATION_FAILED"
    assert "JSON_VALIDATION_FAILED" in result.error.message
    assert "ambiguous repeated invariant identity" in result.error.message
    assert "validation error for SpecAuthorityCompilerEnvelope" not in (
        result.error.message
    )


def test_raw_json_compile_and_repair_persist_one_pending_authority_and_replay(
    engine: Engine,
) -> None:
    """Valid raw JSON persists one current review target per lifecycle step."""
    project_id, _spec_version_id, _spec_hash = _seed_compile_target(engine)
    calls: list[str] = []
    runner, domain = _build_runner(
        engine,
        project_id=project_id,
        responses=_AuthorityRunnerResponses(
            compile=json.dumps(_distinct_multi_invariant_payload(label="compile")),
            repair=json.dumps(_distinct_multi_invariant_payload(label="repair")),
        ),
        calls=calls,
    )

    compile_result, compile_decision, compile_input, compile_guards = _run_compile(
        engine,
        runner=runner,
        domain=domain,
        project_id=project_id,
        idempotency_key="issue-205-compile",
    )
    compile_replay = runner.run(
        compile_decision,
        compile_input,
        guards=compile_guards,
    )

    assert compile_result.ok is True
    assert compile_replay == compile_result.model_copy(update={"replayed": True})
    with Session(engine) as session:
        assert len(session.exec(select(CompiledSpecAuthority)).all()) == 1
        assert session.exec(select(SpecAuthorityAcceptance)).all() == []

    repair_decision = _reject_and_record_feedback(
        engine,
        domain=domain,
        project_id=project_id,
    )
    repair_result, repair_input, repair_guards = _run_repair(
        engine,
        runner=runner,
        domain=domain,
        project_id=project_id,
        decision=repair_decision,
    )
    repair_replay = runner.run(
        repair_decision,
        repair_input,
        guards=repair_guards,
    )

    assert repair_result.ok is True
    assert repair_replay == repair_result.model_copy(update={"replayed": True})
    assert calls == ["compile_provider", "repair_provider"]
    with Session(engine) as session:
        authorities = session.exec(select(CompiledSpecAuthority)).all()
        decisions = session.exec(select(SpecAuthorityAcceptance)).all()
        review = build_authority_review_snapshot_in_session(
            session,
            project_id=project_id,
        )
        assert len(authorities) == EXPECTED_REPAIRED_AUTHORITY_COUNT
        assert len(decisions) == 1
        assert decisions[0].status == "rejected"
        for authority in authorities:
            assert authority.compiled_artifact_json is not None
            compiled = SpecAuthorityCompilerOutput.model_validate_json(
                authority.compiled_artifact_json
            ).root
            assert isinstance(compiled, SpecAuthorityCompilationSuccess)
            _assert_distinct_multi_invariant_lineage(compiled)
        assert isinstance(review, AuthorityReviewSnapshot)
        assert review.pending_authority_id == authorities[-1].authority_id


def test_validation_repair_success_persists_once_for_both_lifecycle_paths(
    engine: Engine,
) -> None:
    """Compile and post-human repair persist only their strictly repaired result."""
    project_id, _spec_version_id, _spec_hash = _seed_issue_208_compile_target(engine)
    calls: list[str] = []
    runner, domain = _build_runner(
        engine,
        project_id=project_id,
        responses=_AuthorityRunnerResponses(
            compile=_issue_208_response(gold=False),
            repair=_issue_208_response(gold=False),
            compile_validation_repair=_issue_208_response(gold=True),
            repair_validation_repair=_issue_208_response(gold=True),
        ),
        calls=calls,
    )

    compile_result, compile_decision, compile_input, compile_guards = _run_compile(
        engine,
        runner=runner,
        domain=domain,
        project_id=project_id,
        idempotency_key="issue-209-repaired-compile",
    )
    compile_replay = runner.run(
        compile_decision,
        compile_input,
        guards=compile_guards,
    )
    assert compile_result.ok is True
    assert compile_replay == compile_result.model_copy(update={"replayed": True})

    repair_decision = _reject_and_record_feedback(
        engine,
        domain=domain,
        project_id=project_id,
    )
    repair_result, repair_input, repair_guards = _run_repair(
        engine,
        runner=runner,
        domain=domain,
        project_id=project_id,
        decision=repair_decision,
    )
    repair_replay = runner.run(
        repair_decision,
        repair_input,
        guards=repair_guards,
    )

    assert repair_result.ok is True
    assert repair_replay == repair_result.model_copy(update={"replayed": True})
    assert calls == [
        "compile_provider",
        "authority_validation_repair",
        "repair_provider",
        "authority_validation_repair",
    ]
    with Session(engine) as session:
        authorities = session.exec(select(CompiledSpecAuthority)).all()
        decisions = session.exec(select(SpecAuthorityAcceptance)).all()
        assert len(authorities) == EXPECTED_REPAIRED_AUTHORITY_COUNT
        assert len(decisions) == 1
        assert decisions[0].status == "rejected"
        for authority in authorities:
            assert authority.compiled_artifact_json is not None
            compiled = SpecAuthorityCompilerOutput.model_validate_json(
                authority.compiled_artifact_json
            ).root
            assert isinstance(compiled, SpecAuthorityCompilationSuccess)
            assert compiled.gaps == [
                "CONSTRAINT.001: unsupported tooling requirement; enforce outside "
                "compiled Authority."
            ]


def test_attempt_28_failure_persists_no_partial_authority_and_replays(
    engine: Engine,
) -> None:
    """Captured tooling misclassification records one durable closed failure."""
    project_id, _spec_version_id, _spec_hash = _seed_issue_208_compile_target(engine)
    calls: list[str] = []
    runner, domain = _build_runner(
        engine,
        project_id=project_id,
        responses=_AuthorityRunnerResponses(
            compile=_issue_208_response(gold=False),
            repair=_issue_208_response(gold=True),
        ),
        calls=calls,
    )

    result, decision, compiler_input, guards = _run_compile(
        engine,
        runner=runner,
        domain=domain,
        project_id=project_id,
        idempotency_key="issue-208-invalid-compile",
    )
    replay = runner.run(decision, compiler_input, guards=guards)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code.value == "AUTHORITY_COMPILATION_FAILED"
    assert "INELIGIBLE_INVARIANT_SOURCE" in result.error.message
    assert "CONSTRAINT.001 semantics" in result.error.message
    assert replay == result.model_copy(update={"replayed": True})
    assert calls == ["compile_provider", "authority_validation_repair"]
    with Session(engine) as session:
        assert session.exec(select(CompiledSpecAuthority)).all() == []
        assert session.exec(select(SpecAuthorityAcceptance)).all() == []
        attempts = session.exec(
            select(WorkflowNodeAttempt).where(
                WorkflowNodeAttempt.node_id == "authority.compile"
            )
        ).all()
        attempt_ids = {
            attempt.workflow_node_attempt_id
            for attempt in attempts
            if attempt.workflow_node_attempt_id is not None
        }
        outcomes = [
            outcome
            for outcome in session.exec(select(WorkflowNodeAttemptOutcome)).all()
            if outcome.workflow_node_attempt_id in attempt_ids
        ]
        assert len(attempts) == 1
        assert len(outcomes) == 1
        assert outcomes[0].failure_code == "AUTHORITY_COMPILATION_FAILED"
        assert "CONSTRAINT.001 semantics" in (outcomes[0].failure_message or "")


def test_tooling_gap_compile_and_repair_persist_once_and_replay(
    engine: Engine,
) -> None:
    """Gold classification yields one pending Authority per lifecycle step."""
    project_id, _spec_version_id, _spec_hash = _seed_issue_208_compile_target(engine)
    calls: list[str] = []
    runner, domain = _build_runner(
        engine,
        project_id=project_id,
        responses=_AuthorityRunnerResponses(
            compile=_issue_208_response(gold=True),
            repair=_issue_208_response(gold=True),
        ),
        calls=calls,
    )

    compile_result, compile_decision, compile_input, compile_guards = _run_compile(
        engine,
        runner=runner,
        domain=domain,
        project_id=project_id,
        idempotency_key="issue-208-valid-compile",
    )
    compile_replay = runner.run(
        compile_decision,
        compile_input,
        guards=compile_guards,
    )
    assert compile_result.ok is True
    assert compile_replay == compile_result.model_copy(update={"replayed": True})

    repair_decision = _reject_and_record_feedback(
        engine,
        domain=domain,
        project_id=project_id,
    )
    repair_result, repair_input, repair_guards = _run_repair(
        engine,
        runner=runner,
        domain=domain,
        project_id=project_id,
        decision=repair_decision,
    )
    repair_replay = runner.run(
        repair_decision,
        repair_input,
        guards=repair_guards,
    )

    assert repair_result.ok is True
    assert repair_replay == repair_result.model_copy(update={"replayed": True})
    assert calls == ["compile_provider", "repair_provider"]
    with Session(engine) as session:
        authorities = session.exec(select(CompiledSpecAuthority)).all()
        decisions = session.exec(select(SpecAuthorityAcceptance)).all()
        review = build_authority_review_snapshot_in_session(
            session,
            project_id=project_id,
        )
        assert len(authorities) == EXPECTED_REPAIRED_AUTHORITY_COUNT
        assert len(decisions) == 1
        assert decisions[0].status == "rejected"
        for authority in authorities:
            assert authority.compiled_artifact_json is not None
            compiled = SpecAuthorityCompilerOutput.model_validate_json(
                authority.compiled_artifact_json
            ).root
            assert isinstance(compiled, SpecAuthorityCompilationSuccess)
            assert compiled.gaps == [
                "CONSTRAINT.001: unsupported tooling requirement; enforce outside "
                "compiled Authority."
            ]
            assert len(compiled.invariants) == 1
            assert compiled.invariants[0].source_item_id == "CONSTRAINT.002"
        assert isinstance(review, AuthorityReviewSnapshot)
        assert review.pending_authority_id == authorities[-1].authority_id


def test_ambiguous_raw_compile_records_actionable_failure_without_authority(
    engine: Engine,
) -> None:
    """Captured-like direct JSON fails after normalization without partial state."""
    project_id, _spec_version_id, _spec_hash = _seed_compile_target(engine)
    calls: list[str] = []
    runner, domain = _build_runner(
        engine,
        project_id=project_id,
        responses=_AuthorityRunnerResponses(
            compile=json.dumps(_ambiguous_payload()),
            repair=json.dumps(_success_payload(label="unused-repair")),
        ),
        calls=calls,
    )

    result, decision, compiler_input, guards = _run_compile(
        engine,
        runner=runner,
        domain=domain,
        project_id=project_id,
        idempotency_key="issue-205-ambiguous-compile",
    )
    replay = runner.run(decision, compiler_input, guards=guards)

    _assert_authority_failure(result)
    assert replay == result.model_copy(update={"replayed": True})
    assert calls == ["compile_provider", "authority_validation_repair"]
    with Session(engine) as session:
        assert session.exec(select(CompiledSpecAuthority)).all() == []
        assert session.exec(select(SpecAuthorityAcceptance)).all() == []
        attempts = session.exec(
            select(WorkflowNodeAttempt).where(
                WorkflowNodeAttempt.node_id == "authority.compile"
            )
        ).all()
        attempt_ids = {
            attempt.workflow_node_attempt_id
            for attempt in attempts
            if attempt.workflow_node_attempt_id is not None
        }
        outcomes = [
            outcome
            for outcome in session.exec(select(WorkflowNodeAttemptOutcome)).all()
            if outcome.workflow_node_attempt_id in attempt_ids
        ]
        assert len(attempts) == 1
        assert len(outcomes) == 1
        assert outcomes[0].failure_code == "AUTHORITY_COMPILATION_FAILED"
        assert "ambiguous repeated invariant identity" in (
            outcomes[0].failure_message or ""
        )


def test_ambiguous_raw_repair_records_actionable_failure_without_replacement(
    engine: Engine,
) -> None:
    """Repair uses the same boundary and never persists a partial replacement."""
    project_id, _spec_version_id, _spec_hash = _seed_compile_target(engine)
    calls: list[str] = []
    structured_compile = SpecAuthorityCompilerEnvelope(
        result=SpecAuthorityCompilationSuccess.model_validate(
            _success_payload(label="structured-setup")
        )
    )
    runner, domain = _build_runner(
        engine,
        project_id=project_id,
        responses=_AuthorityRunnerResponses(
            compile=structured_compile,
            repair=json.dumps(_ambiguous_payload()),
        ),
        calls=calls,
    )
    compile_result, _decision_value, _input, _guards_value = _run_compile(
        engine,
        runner=runner,
        domain=domain,
        project_id=project_id,
        idempotency_key="issue-205-repair-setup",
    )
    assert compile_result.ok is True
    repair_decision = _reject_and_record_feedback(
        engine,
        domain=domain,
        project_id=project_id,
    )

    result, repair_input, guards = _run_repair(
        engine,
        runner=runner,
        domain=domain,
        project_id=project_id,
        decision=repair_decision,
    )
    replay = runner.run(repair_decision, repair_input, guards=guards)

    _assert_authority_failure(result)
    assert replay == result.model_copy(update={"replayed": True})
    assert calls == [
        "compile_provider",
        "repair_provider",
        "authority_validation_repair",
    ]
    with Session(engine) as session:
        authorities = session.exec(select(CompiledSpecAuthority)).all()
        decisions = session.exec(select(SpecAuthorityAcceptance)).all()
        repair_attempts = session.exec(
            select(WorkflowNodeAttempt).where(
                WorkflowNodeAttempt.node_id == "authority.repair"
            )
        ).all()
        repair_outcomes = [
            outcome
            for outcome in session.exec(select(WorkflowNodeAttemptOutcome)).all()
            if outcome.workflow_node_attempt_id
            in {attempt.workflow_node_attempt_id for attempt in repair_attempts}
        ]
        assert len(authorities) == 1
        assert len(decisions) == 1
        assert decisions[0].status == "rejected"
        assert len(repair_attempts) == 1
        assert len(repair_outcomes) == 1
        assert repair_outcomes[0].failure_code == "AUTHORITY_COMPILATION_FAILED"
