# Domain Workflow Graph Hard-Break Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace AgileForge's session-backed scalar FSM with one framework-neutral hierarchical workflow graph whose position and guarded transitions are derived from typed durable Project facts, then prove the hard break on caRtola, ASA, and MyFinance.

**Architecture:** `WorkflowDomain.position(project_id)` is the only routing query and `WorkflowDomain.transition(request)` is the only workflow mutation entry point. Pure child graphs evaluate immutable fact snapshots; SQLModel repositories load and write named durable records; CLI, API, frontend, and ADK render or execute decisions without owning routing policy. The implementation remains isolated until one atomic cutover removes `orchestrator_agent`, FSM labels, product-workflow sessions, pre-Project context keys, and all compatibility scaffolding.

**Tech Stack:** Python 3.12, Pydantic 2, SQLModel/SQLAlchemy, SQLite, FastAPI, vanilla JavaScript frontend, Google ADK 2.2.0 graph workflows, OpenRouter, pytest, Ruff, `ty`, Bandit, GitPython.

## Global Constraints

- The approved design is `docs/superpowers/specs/2026-08-02-domain-workflow-graph-hard-break-design.md` and wins over this plan if wording diverges.
- Durable Project facts are authoritative. Workflow position, ADK sessions, rendered files, and transport state are derived.
- The domain imports no CLI, API, frontend, `google.adk`, provider SDK, session repository, or filesystem projection module.
- `WorkflowDomain.position()` is the only routing query. `WorkflowDomain.transition()` is the only workflow mutation entry point.
- Every normal positioned request carries `project_id`, graph version, fact fingerprint, decision fingerprint, idempotency key, actor, and optional correlation ID. Attempt continuations retain those origin guards for audit and are authorized by the durable attempt fingerprint instead of pretending their active node is still available.
- `OpenProjectShell` is the only mutation with neither position guards nor a durable attempt guard.
- Requests form a closed typed union. No public `action: str` plus dictionary escape hatch is allowed.
- The first implementation hashes the full canonical fact snapshot; do not add partitioned fingerprints before profiling.
- Graph rules are pure and receive an explicit evaluation time. They do not query a database or read a hidden clock.
- ADK is pinned exactly to `google-adk==2.2.0`; ADK executes eligible node recipes but never owns Project position or mutation safety.
- Production role default: `openrouter/openai/gpt-5.6-luna` with data collection denied and ZDR required.
- Test role default: `openrouter/openai/gpt-oss-20b:free`.
- `uv run --frozen pyrepo-check --all` and default pytest are offline and consume no provider credits.
- Live model evaluations are opt-in, budgeted, and excluded from the default quality gate.
- No migration or preservation of the current AgileForge database, workflow sessions, FSM states, context keys, command envelopes, or internal imports.
- Development commits may contain explicitly listed branch-only scaffolding, but no published runtime may consult both old and new routing authorities.
- The final tree contains no `orchestrator_agent` package, old FSM, session-derived routing source, or compatibility alias.
- Do not add `# type: ignore`, typing-related `# noqa`, checker exclusions, or `Any` only to satisfy a checker. Fix typing at the boundary.
- Only caRtola, ASA Deep Process Control Advisory System, and MyFinance are operator-led acceptance repositories.
- This implementation plan does not create branches, edit code, run project workflow mutations, or implement features in caRtola, ASA, or MyFinance.
- After cutover, the Operator runs AgileForge against those repositories. "Statement Streams and Coverage" is MyFinance test input, not an AgileForge implementation task.
- The MyFinance run must use synthetic evidence and an isolated test environment; the Operator owns that run and its external-repository changes.
- Every task ends with focused tests and a commit. Run `uv run --frozen pyrepo-check --all` after every task that changes a shared contract or production import boundary.

---

## Baseline And Execution Boundary

Start implementation from AgileForge commit `8f8d5ff`, which includes model-policy commit `cc83636`. At plan-writing time local `master` is two commits ahead of `origin/master`; verify that fact again before creating the implementation worktree.

Use `superpowers:using-git-worktrees` at execution time and create a branch named `dev/domain-workflow-graph-hard-break`. Do not implement directly on `master`. Keep one branch because the cutover is coupled, but review and commit each task independently.

Branch-only scaffolding is limited to these two items:

1. Existing `Product` persistence names may remain behind the new repository until Task 17 renames the fresh schema to `Project`/`project_id`.
2. `services/agent_workbench/fingerprints.py` may re-export the new canonical hash functions until Task 17 deletes the old application package.

Neither item may remain after Task 17. No production caller switches to `WorkflowDomain` before Task 16, so intermediate commits do not create a dual-routing runtime.

## Target File Map

### Domain

- `workflow/__init__.py` exports only `WorkflowDomain`, public contracts, and the closed request union.
- `workflow/contracts.py` owns categories, recommendations, decisions, positions, results, and transport-independent errors.
- `workflow/facts.py` owns immutable typed fact records and `WorkflowFactSnapshot`.
- `workflow/fingerprints.py` owns canonical normalization and full fact/decision fingerprints.
- `workflow/graph.py` owns pure node, child-graph, join, and terminal evaluation primitives.
- `workflow/clock.py` owns the explicit `Clock` protocol and system/fixed implementations.
- `workflow/requests/` owns command-specific Pydantic request types grouped by child graph.
- `workflow/definitions/` owns root and child graph definitions; each file contains rules for one business boundary.
- `workflow/handlers/` owns transactional command handlers grouped by child graph; routing checks do not live here.
- `workflow/domain.py` owns the two-method public service, guard revalidation, idempotency, transaction, dispatch, and post-mutation position.

### Persistence

- `models/workflow.py` owns new Project workflow records, constraints, attempts, outcomes, and transition receipts.
- `models/core.py`, `models/specs.py`, `models/events.py`, and remaining model modules become Project-named on the fresh schema in Task 17.
- `repositories/workflow.py` loads one full typed fact snapshot and exposes session-bound write helpers.
- `repositories/project.py` replaces `repositories/product.py` at cutover.

### Adapters

- `adapters/adk/runner.py` implements the durable attempt protocol around ADK execution.
- `adapters/adk/recipes.py` maps agentic node IDs to ADK execution recipes without reproducing domain prerequisites.
- `adapters/adk/agents/` contains retained leaf agent definitions only.
- `cli/workflow_commands.py` maps stable request kinds to command spelling and flags only.
- `services/application.py` provides read projections plus the injected `WorkflowDomain`; it contains no workflow conditions.
- `utils/api_schemas.py` carries graph/fact/decision guards on mutating API payloads.
- `frontend/app.js` and `frontend/project.js` render `WorkflowPosition` categories rather than FSM state labels.

### Verification

- `tests/workflow/` contains pure graph, fingerprint, domain, repository, and regression tests.
- `tests/adapters/` contains ADK and transport contract tests.
- `docs/testing/workflow-graph-acceptance-checklist.md` gives the Operator a manual fresh-database checklist and evidence template for caRtola, ASA, and MyFinance.

## Stable Node Catalog

Use these node IDs in facts, decisions, attempts, tests, command renderers, and acceptance evidence:

| Child graph | Stable node IDs |
|---|---|
| onboarding | `onboarding.greenfield.challenge`, `onboarding.greenfield.prd`, `onboarding.greenfield.prd_review`, `onboarding.greenfield.initial_spec`, `onboarding.greenfield.initial_spec_review`, `onboarding.brownfield.baseline`, `onboarding.brownfield.inventory`, `onboarding.brownfield.curation`, `onboarding.brownfield.initial_spec_review`, `onboarding.initial_scope_registration` |
| authority | `authority.compile`, `authority.review`, `authority.feedback`, `authority.repair` |
| vision | `vision.generate`, `vision.review` |
| backlog | `backlog.generate`, `backlog.review`, `backlog.reconcile` |
| planning | `planning.roadmap.generate`, `planning.roadmap.review`, `planning.story.generate`, `planning.story.review`, `planning.story_dependencies`, `planning.story_readiness`, `planning.sprint.plan`, `planning.sprint.review`, `planning.sprint.start` |
| execution | `execution.task.next`, `execution.task.complete`, `execution.story.close`, `execution.sprint.review`, `execution.sprint.close`, `execution.post_sprint_triage` |
| scope extension | `scope_extension.start`, `scope_extension.challenge`, `scope_extension.prd`, `scope_extension.prd_review`, `scope_extension.spec`, `scope_extension.spec_review`, `scope_extension.registration`, `scope_extension.authority`, `scope_extension.reconciliation` |

`scope_extension.start` is `optional_reentry` when current required work is terminal. Recovery nodes use `recovery`; every other unfinished node uses `required`.

## Task 1: Pin Runtime Dependencies And Enforce Offline Default Tests

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/test_runtime_dependency_contract.py`
- Create: `tests/test_default_network_guard.py`
- Create: `workflow/__init__.py`
- Create: `adapters/__init__.py`

**Interfaces:**
- Consumes: current model configuration in `config/models.yaml` and `config/models.test.yaml`.
- Produces: exact ADK version `2.2.0`, exact `pytest-socket` version `0.8.0`, package discovery for `workflow` and `adapters`, and a socket-disabled default pytest suite.

- [ ] **Step 1: Write dependency and network-contract tests**

```python
# tests/test_runtime_dependency_contract.py
from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path


def test_runtime_dependencies_are_exactly_pinned() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert "google-adk==2.2.0" in project["project"]["dependencies"]
    assert "pytest-socket==0.8.0" in project["project"]["dependencies"]
    assert importlib.metadata.version("google-adk") == "2.2.0"
    assert importlib.metadata.version("pytest-socket") == "0.8.0"


def test_domain_and_adapter_packages_are_discovered() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    included = set(project["tool"]["setuptools"]["packages"]["find"]["include"])
    assert {"workflow", "workflow.*", "adapters", "adapters.*"} <= included
```

```python
# tests/test_default_network_guard.py
from __future__ import annotations

import socket

import pytest
from pytest_socket import SocketBlockedError


def test_default_suite_blocks_external_network() -> None:
    with pytest.raises(SocketBlockedError):
        socket.create_connection(("example.com", 443), timeout=0.01)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run --frozen pytest tests/test_runtime_dependency_contract.py tests/test_default_network_guard.py -q`

Expected: FAIL because ADK is broadly ranged, `pytest-socket` and the packages are absent, and external network is not guarded.

- [ ] **Step 3: Add the exact dependency/package contract and offline fixture**

Set these exact dependency and package entries in `pyproject.toml`:

```toml
dependencies = [
    "google-adk==2.2.0",
    "pytest-socket==0.8.0",
]

[tool.setuptools.packages.find]
include = [
    "adapters",
    "adapters.*",
    "workflow",
    "workflow.*",
]
```

Keep every unrelated existing dependency and package entry. Add `workflow` and `adapters` to coverage sources; retain `orchestrator_agent` until Task 17.

Set pytest defaults in `pyproject.toml` to include `--disable-socket --allow-unix-socket`. Tests that intentionally use a local TCP server declare `@pytest.mark.allow_hosts(["127.0.0.1", "::1"])`. Tests that contact a provider or other external service require both `@pytest.mark.integration` and `@pytest.mark.enable_socket`; the default `pyrepo-check --all` selection excludes `integration`.

- [ ] **Step 4: Refresh the lock and verify GREEN**

Run: `uv lock`

Expected: `uv.lock` resolves `google-adk` at exactly `2.2.0`.

Run: `uv run --frozen pytest tests/test_runtime_dependency_contract.py tests/test_default_network_guard.py tests/test_model_config_env.py -q`

Expected: PASS with no external request.

- [ ] **Step 5: Run the full static/test gate**

Run: `uv run --frozen pyrepo-check --all`

Expected: Ruff, annotations, `ty`, Bandit, and default pytest all pass offline.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock workflow adapters tests/test_runtime_dependency_contract.py tests/test_default_network_guard.py
git commit -m "build: pin workflow graph runtime"
```

## Task 2: Add Immutable Public Contracts And Canonical Fingerprints

**Files:**
- Create: `workflow/contracts.py`
- Create: `workflow/facts.py`
- Create: `workflow/fingerprints.py`
- Create: `workflow/clock.py`
- Create: `workflow/requests/__init__.py`
- Create: `workflow/requests/base.py`
- Modify: `workflow/__init__.py`
- Modify: `services/agent_workbench/fingerprints.py`
- Create: `tests/workflow/test_contracts.py`
- Create: `tests/workflow/test_fingerprints.py`

**Interfaces:**
- Consumes: Python 3.12, Pydantic 2, and existing canonical hash behavior.
- Produces: `GRAPH_VERSION`, `WorkflowPosition`, `NodeDecision`, `TransitionResult`, `WorkflowError`, `PositionedRequest`, `WorkflowFactSnapshot`, `fact_fingerprint()`, and `decision_fingerprint()`.

- [ ] **Step 1: Write contract and fingerprint tests**

```python
# tests/workflow/test_fingerprints.py
from datetime import UTC, datetime

from workflow.facts import ProjectFact, WorkflowFactSnapshot
from workflow.fingerprints import fact_fingerprint


def test_fact_fingerprint_is_stable_for_equivalent_snapshots() -> None:
    created = datetime(2026, 8, 2, 12, tzinfo=UTC)
    first = WorkflowFactSnapshot(
        project=ProjectFact(
            project_id=3,
            name="MyFinance",
            origin="brownfield",
            created_at=created,
        )
    )
    second = first.model_copy(deep=True)
    assert fact_fingerprint(first) == fact_fingerprint(second)
    assert fact_fingerprint(first).startswith("sha256:")
```

```python
# tests/workflow/test_contracts.py
from typing import ClassVar, Literal

import pytest
from pydantic import ValidationError

from workflow.contracts import RecommendationKind, WorkflowErrorCode
from workflow.requests.base import PositionedRequest


class ExampleRequest(PositionedRequest):
    kind: Literal["test"] = "test"
    node_id: ClassVar[str] = "test.node"


def test_positioned_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ExampleRequest.model_validate(
            {
                "kind": "test",
                "project_id": 1,
                "graph_version": "agileforge.workflow.v1",
                "fact_fingerprint": "sha256:facts",
                "decision_fingerprint": "sha256:decision",
                "idempotency_key": "key-1",
                "actor": "test",
                "correlation_id": None,
                "unexpected": True,
            }
        )


def test_contract_enums_are_closed() -> None:
    assert {item.value for item in RecommendationKind} == {
        "required",
        "optional_reentry",
        "recovery",
    }
    assert {item.value for item in WorkflowErrorCode} == {
        "STALE_POSITION",
        "TRANSITION_NOT_AVAILABLE",
        "WORKFLOW_FACT_CONFLICT",
        "ATTEMPT_OBSOLETE",
        "EXTERNAL_EXECUTION_FAILED",
    }
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run --frozen pytest tests/workflow/test_contracts.py tests/workflow/test_fingerprints.py -q`

Expected: collection FAIL because the new modules do not exist.

- [ ] **Step 3: Implement the frozen public contracts**

Use this contract shape in `workflow/contracts.py`:

```python
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

GRAPH_VERSION: str = "agileforge.workflow.v1"
JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NodeCategory(StrEnum):
    AVAILABLE = "available"
    WAITING = "waiting"
    BLOCKED = "blocked"
    INVALID = "invalid"


class RecommendationKind(StrEnum):
    REQUIRED = "required"
    OPTIONAL_REENTRY = "optional_reentry"
    RECOVERY = "recovery"


class WorkflowErrorCode(StrEnum):
    STALE_POSITION = "STALE_POSITION"
    TRANSITION_NOT_AVAILABLE = "TRANSITION_NOT_AVAILABLE"
    WORKFLOW_FACT_CONFLICT = "WORKFLOW_FACT_CONFLICT"
    ATTEMPT_OBSOLETE = "ATTEMPT_OBSOLETE"
    EXTERNAL_EXECUTION_FAILED = "EXTERNAL_EXECUTION_FAILED"


class FactReference(FrozenModel):
    fact_type: str
    fact_id: str
    fingerprint: str


class Blocker(FrozenModel):
    code: str
    message: str
    fact_references: tuple[FactReference, ...] = ()


class InputField(FrozenModel):
    name: str
    value_type: Literal["string", "integer", "boolean", "object", "array"]
    required: bool = True


class NodeDecision(FrozenModel):
    node_id: str
    instance_key: str | None = None
    child_graph_id: str
    request_kind: str
    category: NodeCategory
    recommendation_kind: RecommendationKind
    reason_code: str
    required_inputs: tuple[InputField, ...] = ()
    fact_references: tuple[FactReference, ...] = ()
    blockers: tuple[Blocker, ...] = ()
    valid_until: datetime | None = None
    decision_fingerprint: str


class WorkflowPosition(FrozenModel):
    project_id: int
    graph_version: str
    fact_fingerprint: str
    evaluated_at: datetime
    available_nodes: tuple[str, ...]
    waiting_nodes: tuple[str, ...]
    blocked_nodes: tuple[str, ...]
    invalid_nodes: tuple[str, ...]
    terminal: bool
    decisions: tuple[NodeDecision, ...]


class WorkflowError(FrozenModel):
    code: WorkflowErrorCode
    message: str
    blockers: tuple[Blocker, ...] = ()


class TransitionResult(FrozenModel):
    ok: bool
    replayed: bool = False
    applied_node_id: str | None = None
    output: JsonObject = Field(default_factory=dict)
    position: WorkflowPosition | None = None
    error: WorkflowError | None = None
```

Define `GuardedRequest` in `workflow/requests/base.py` with `kind`, `project_id`, `graph_version`, `fact_fingerprint`, `decision_fingerprint`, `idempotency_key`, `actor`, and `correlation_id`. Define `PositionedRequest(GuardedRequest)` with `instance_key: str | None`, `attempt_id: int | None`, `attempt_fingerprint: str | None`, and a `ClassVar[str]` named `node_id`. A model validator requires `attempt_id` and `attempt_fingerprint` together. Human actions omit both; Task 15 uses them only for durable attempt completion. Both models use `ConfigDict(frozen=True, extra="forbid")`.

Expose these two exact lookup methods on `PositionedRequest` so the domain never introspects request fields ad hoc:

```python
def decision_node_id(self) -> str:
    return self.node_id

def decision_instance_key(self) -> str | None:
    return self.instance_key
```

Define `Clock.now() -> datetime`, `SystemClock`, and `FixedClock` in `workflow/clock.py`.

- [ ] **Step 4: Implement named immutable fact records**

`workflow/facts.py` must define named frozen models for these records and no generic key/value fact:

```python
class ProjectFact(FrozenModel):
    project_id: int
    name: str
    origin: Literal["greenfield", "brownfield"]
    created_at: datetime


class ProjectAbandonmentFact(FrozenModel):
    project_abandonment_id: int
    project_id: int
    reason: str
    abandoned_by: str
    abandoned_at: datetime


class DiscoveryRunFact(FrozenModel):
    discovery_run_id: int
    project_id: int
    purpose: Literal["initial", "extension"]
    ordinal: int
    created_at: datetime
    closed_at: datetime | None


class DiscoveryRunAbandonmentFact(FrozenModel):
    discovery_run_abandonment_id: int
    project_id: int
    discovery_run_id: int
    reason: str
    abandoned_by: str
    abandoned_at: datetime


class ChallengeArtifactFact(FrozenModel):
    challenge_artifact_id: int
    discovery_run_id: int
    content_fingerprint: str
    supersedes_id: int | None


class PrdVersionFact(FrozenModel):
    prd_version_id: int
    discovery_run_id: int
    content_fingerprint: str
    supersedes_id: int | None


class ReviewDecisionFact(FrozenModel):
    decision_id: int
    artifact_type: Literal["prd", "spec_draft", "authority", "vision", "backlog", "roadmap", "story", "sprint"]
    artifact_id: int
    artifact_fingerprint: str
    decision: Literal["accepted", "rejected", "feedback"]
    decided_at: datetime


class SpecDraftFact(FrozenModel):
    spec_draft_id: int
    discovery_run_id: int
    kind: Literal["initial", "amendment"]
    content_fingerprint: str
    base_spec_version_id: int | None
    base_spec_hash: str | None
    supersedes_id: int | None


class InitialScopeRegistrationFact(FrozenModel):
    registration_id: int
    discovery_run_id: int
    spec_draft_id: int
    spec_version_id: int
    spec_hash: str


class AuthorityFact(FrozenModel):
    authority_id: int
    spec_version_id: int
    authority_fingerprint: str
    status: Literal["pending_review", "accepted", "rejected", "stale"]
    decided_at: datetime | None


class PhaseArtifactFact(FrozenModel):
    artifact_type: Literal["vision", "backlog", "roadmap", "story_set", "sprint_plan"]
    artifact_id: str
    artifact_fingerprint: str
    status: Literal["draft", "pending_review", "accepted", "rejected", "superseded"]


class SprintFact(FrozenModel):
    sprint_id: int
    status: Literal["planned", "active", "completed"]
    completed_at: datetime | None


class StoryFact(FrozenModel):
    story_id: int
    status: str
    sprint_candidate: bool
    readiness_blockers: tuple[str, ...]


class TaskFact(FrozenModel):
    task_id: int
    sprint_id: int
    story_id: int
    status: str
    dependencies_satisfied: bool


class PostSprintTriageFact(FrozenModel):
    sprint_id: int
    impact: Literal["none", "backlog", "specification"]
    payload_fingerprint: str


class NodeAttemptFact(FrozenModel):
    attempt_id: int
    node_id: str
    instance_key: str | None
    graph_version: str
    input_fingerprint: str
    fact_fingerprint: str
    business_fact_fingerprint: str
    decision_fingerprint: str
    attempt_fingerprint: str
    model_id: str
    lease_expires_at: datetime
    outcome: Literal["success", "failure", "obsolete"] | None


class WorkflowFactSnapshot(FrozenModel):
    project: ProjectFact
    project_abandonments: tuple[ProjectAbandonmentFact, ...] = ()
    discovery_runs: tuple[DiscoveryRunFact, ...] = ()
    discovery_run_abandonments: tuple[DiscoveryRunAbandonmentFact, ...] = ()
    challenge_artifacts: tuple[ChallengeArtifactFact, ...] = ()
    prd_versions: tuple[PrdVersionFact, ...] = ()
    review_decisions: tuple[ReviewDecisionFact, ...] = ()
    spec_drafts: tuple[SpecDraftFact, ...] = ()
    initial_registrations: tuple[InitialScopeRegistrationFact, ...] = ()
    authorities: tuple[AuthorityFact, ...] = ()
    phase_artifacts: tuple[PhaseArtifactFact, ...] = ()
    sprints: tuple[SprintFact, ...] = ()
    stories: tuple[StoryFact, ...] = ()
    tasks: tuple[TaskFact, ...] = ()
    post_sprint_triage: tuple[PostSprintTriageFact, ...] = ()
    node_attempts: tuple[NodeAttemptFact, ...] = ()
```

Add repository-baseline and inventory fact models in Task 8 when their exact persisted contracts are introduced.

- [ ] **Step 5: Move canonical hashing behind the domain boundary**

Move the existing deterministic normalization behavior to `workflow/fingerprints.py`. Add:

```python
def fact_fingerprint(snapshot: WorkflowFactSnapshot) -> str:
    payload = {
        "graph_version": GRAPH_VERSION,
        "facts": snapshot.model_dump(mode="json"),
    }
    return canonical_hash(payload)


def decision_fingerprint(payload: Mapping[str, object]) -> str:
    return canonical_hash(payload)
```

Make `services/agent_workbench/fingerprints.py` a branch-only re-export of `canonical_hash`, `canonical_json`, and `normalize_for_hash`. Do not duplicate implementations.

- [ ] **Step 6: Verify GREEN and typing**

Run: `uv run --frozen pytest tests/workflow/test_contracts.py tests/workflow/test_fingerprints.py tests/test_agent_workbench_fingerprints.py -q`

Expected: PASS and old callers retain identical hashes through the temporary re-export.

Run: `uv run --frozen pyrepo-check --all`

Expected: all checks pass with no new suppressions.

- [ ] **Step 7: Commit**

```bash
git add workflow services/agent_workbench/fingerprints.py tests/workflow tests/test_agent_workbench_fingerprints.py
git commit -m "feat: add workflow graph contracts"
```

## Task 3: Implement The Pure Hierarchical Graph Kernel

**Files:**
- Create: `workflow/graph.py`
- Create: `workflow/definitions/__init__.py`
- Create: `workflow/definitions/root.py`
- Create: `tests/workflow/test_graph_kernel.py`
- Create: `tests/workflow/test_graph_properties.py`

**Interfaces:**
- Consumes: `WorkflowFactSnapshot`, `NodeDecision`, full fact fingerprint, and explicit evaluation time.
- Produces: `RuleEvaluation`, `NodeSpec`, `ChildGraphSpec`, `WorkflowGraph.evaluate(snapshot, evaluated_at) -> WorkflowPosition`.

- [ ] **Step 1: Write table-driven graph tests**

Cover available, waiting, blocked, invalid, parallel branches, all-of joins, optional re-entry terminal semantics, graph-version mismatch inputs, and time-sensitive lease expiry. Use `FixedClock(datetime(2026, 8, 2, 12, tzinfo=UTC))`; use no database, filesystem, ADK, network, or real clock.

```python
def test_optional_reentry_does_not_make_terminal_project_unfinished() -> None:
    graph = graph_with_optional_scope_extension()
    position = graph.evaluate(terminal_snapshot(), EVALUATED_AT)
    assert position.terminal is True
    assert position.available_nodes == ("scope_extension.start",)
    decision = position.decisions[0]
    assert decision.recommendation_kind == RecommendationKind.OPTIONAL_REENTRY
```

```python
def test_join_waits_for_every_required_branch() -> None:
    graph = graph_with_two_branch_join()
    position = graph.evaluate(snapshot_with_only_first_branch(), EVALUATED_AT)
    assert "test.join" in position.blocked_nodes
    assert position.terminal is False
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run --frozen pytest tests/workflow/test_graph_kernel.py tests/workflow/test_graph_properties.py -q`

Expected: collection FAIL because graph primitives do not exist.

- [ ] **Step 3: Implement pure graph primitives**

Use these exact responsibilities in `workflow/graph.py`:

```python
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from workflow.contracts import (
    Blocker,
    FactReference,
    InputField,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    WorkflowPosition,
)
from workflow.facts import WorkflowFactSnapshot
from workflow.fingerprints import decision_fingerprint, fact_fingerprint


class RuleCategory(StrEnum):
    SATISFIED = "satisfied"
    AVAILABLE = "available"
    WAITING = "waiting"
    BLOCKED = "blocked"
    INVALID = "invalid"


@dataclass(frozen=True)
class RuleEvaluation:
    category: RuleCategory
    reason_code: str
    instance_key: str | None = None
    fact_references: tuple[FactReference, ...] = ()
    blockers: tuple[Blocker, ...] = ()
    valid_until: datetime | None = None


NodeRule = Callable[[WorkflowFactSnapshot, datetime], tuple[RuleEvaluation, ...]]


@dataclass(frozen=True)
class NodeSpec:
    node_id: str
    child_graph_id: str
    request_kind: str
    recommendation_kind: RecommendationKind
    required_inputs: tuple[InputField, ...]
    evaluate_rule: NodeRule


@dataclass(frozen=True)
class ChildGraphSpec:
    child_graph_id: str
    nodes: tuple[NodeSpec, ...]
    children: tuple["ChildGraphSpec", ...] = ()

    def iter_nodes(self) -> Iterable[NodeSpec]:
        yield from self.nodes
        for child in self.children:
            yield from child.iter_nodes()


@dataclass(frozen=True)
class WorkflowGraph:
    graph_version: str
    root: ChildGraphSpec

    def evaluate(
        self,
        snapshot: WorkflowFactSnapshot,
        evaluated_at: datetime,
    ) -> WorkflowPosition:
        facts_hash = fact_fingerprint(snapshot)
        decisions = tuple(
            decision
            for node in self.root.iter_nodes()
            for evaluation in node.evaluate_rule(snapshot, evaluated_at)
            if (
                decision := self._decision(
                    node,
                    evaluation,
                    facts_hash,
                )
            )
            is not None
        )
        available = tuple(
            item.node_id for item in decisions if item.category is NodeCategory.AVAILABLE
        )
        waiting = tuple(
            item.node_id for item in decisions if item.category is NodeCategory.WAITING
        )
        blocked = tuple(
            item.node_id for item in decisions if item.category is NodeCategory.BLOCKED
        )
        invalid = tuple(
            item.node_id for item in decisions if item.category is NodeCategory.INVALID
        )
        required_or_recovery = tuple(
            item
            for item in decisions
            if item.recommendation_kind
            in {RecommendationKind.REQUIRED, RecommendationKind.RECOVERY}
        )
        return WorkflowPosition(
            project_id=snapshot.project.project_id,
            graph_version=self.graph_version,
            fact_fingerprint=facts_hash,
            evaluated_at=evaluated_at,
            available_nodes=available,
            waiting_nodes=waiting,
            blocked_nodes=blocked,
            invalid_nodes=invalid,
            terminal=not required_or_recovery,
            decisions=decisions,
        )
```

Every rule returns a tuple. A singleton node returns a one-element tuple; a repeated node returns one element per stable `instance_key`; a completed node returns one `RuleCategory.SATISFIED` element. An absent optional re-entry also returns `SATISFIED`. `_decision()` returns `None` for `SATISFIED`; otherwise it maps the internal category to the same-named public `NodeCategory`.

`_decision()` must fingerprint graph version, fact fingerprint, node ID, instance key, request kind, category, recommendation kind, reason code, required inputs, fact references, blockers, and `valid_until`. It must not hash `evaluated_at` for time-insensitive decisions; lease rules set `valid_until`, so a lease decision changes at its boundary without making every read unstable. Decision order is root child order, node order, then lexicographically sorted `instance_key`. Reject any rule output with duplicate instance keys for one node.

- [ ] **Step 4: Add the root hierarchy without business rules**

`workflow/definitions/root.py` must create seven named child graphs in this order: onboarding, authority, vision, backlog, planning, execution, scope extension. Each starts with an empty node tuple. Tests assert that duplicate node IDs and duplicate child graph IDs raise `ValueError` during `WorkflowGraph` construction.

- [ ] **Step 5: Verify GREEN and deterministic property cases**

Run: `uv run --frozen pytest tests/workflow/test_graph_kernel.py tests/workflow/test_graph_properties.py -q`

Expected: PASS for every table row and fixed-time lease boundary.

Run: `uv run --frozen pyrepo-check --all`

Expected: all checks pass.

- [ ] **Step 6: Commit**

```bash
git add workflow/graph.py workflow/definitions tests/workflow
git commit -m "feat: add hierarchical workflow graph kernel"
```

## Task 4: Add Named Durable Workflow Models And Database Constraints

**Files:**
- Create: `models/workflow.py`
- Modify: `models/core.py`
- Modify: `models/specs.py`
- Modify: `models/db.py`
- Modify: `models/__init__.py`
- Create: `tests/workflow/test_workflow_models.py`
- Create: `tests/workflow/test_workflow_constraints.py`

**Interfaces:**
- Consumes: current `Product` row as branch-only persistence scaffolding and existing `SpecRegistry`.
- Produces: named Project-owned workflow rows and database-enforced cardinality/provenance/idempotency constraints.

- [ ] **Step 1: Write fresh-schema model and constraint tests**

Create an in-memory SQLite engine with foreign keys enabled. Test all of these failures through actual commits:

1. two initial `DiscoveryRun` rows for one Project;
2. two unresolved extension runs for one Project;
3. a PRD linked to another Project's discovery run;
4. an initial draft with a base spec;
5. an amendment draft without base version/hash;
6. two initial registrations for one Project, run, draft, or spec;
7. two outcomes for one node attempt;
8. one idempotency key reused in the same request kind.

```python
def test_cross_project_prd_is_rejected(engine: Engine) -> None:
    with Session(engine) as session:
        first = seed_project_with_initial_run(session, name="first")
        second = seed_project_with_initial_run(session, name="second")
        session.add(
            PrdVersion(
                project_id=second.project_id,
                discovery_run_id=first.discovery_run_id,
                version_number=1,
                canonical_content_json="{}",
                content_fingerprint="sha256:prd",
                supersedes_prd_version_id=None,
                provenance_path=None,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
```

- [ ] **Step 2: Run model tests and verify RED**

Run: `uv run --frozen pytest tests/workflow/test_workflow_models.py tests/workflow/test_workflow_constraints.py -q`

Expected: collection FAIL because `models.workflow` does not exist.

- [ ] **Step 3: Add shell provenance to the current aggregate**

Add `origin: str` with a `greenfield`/`brownfield` check constraint to the existing top-level row. Do not add an executable lifecycle state. Add `ProjectAbandonment` as a separate typed record in `models/workflow.py`.

- [ ] **Step 4: Implement named workflow tables**

`models/workflow.py` must contain these final class names and keys:

```python
class DiscoveryRun(SQLModel, table=True):
    __tablename__ = "discovery_runs"
    discovery_run_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="products.product_id", index=True)
    purpose: str = Field(index=True)
    ordinal: int
    created_at: datetime = Field(default_factory=utc_now, nullable=False)
    closed_at: datetime | None = Field(default=None)


class ChallengeArtifact(SQLModel, table=True):
    __tablename__ = "challenge_artifacts"
    challenge_artifact_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    discovery_run_id: int = Field(index=True)
    version_number: int
    canonical_content_json: str = Field(sa_type=Text)
    content_fingerprint: str = Field(index=True)
    supersedes_challenge_artifact_id: int | None = Field(default=None)
    provenance_path: str | None = Field(default=None, sa_type=Text)
    created_at: datetime = Field(default_factory=utc_now, nullable=False)


class PrdVersion(SQLModel, table=True):
    __tablename__ = "prd_versions"
    prd_version_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    discovery_run_id: int = Field(index=True)
    version_number: int
    canonical_content_json: str = Field(sa_type=Text)
    content_fingerprint: str = Field(index=True)
    supersedes_prd_version_id: int | None = Field(default=None)
    provenance_path: str | None = Field(default=None, sa_type=Text)
    created_at: datetime = Field(default_factory=utc_now, nullable=False)


class PrdDecision(SQLModel, table=True):
    __tablename__ = "prd_decisions"
    prd_decision_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    discovery_run_id: int = Field(index=True)
    prd_version_id: int = Field(index=True)
    artifact_fingerprint: str = Field(index=True)
    decision: str = Field(index=True)
    reviewer: str = Field(index=True)
    notes: str = Field(sa_type=Text)
    idempotency_key: str = Field(index=True)
    decided_at: datetime = Field(default_factory=utc_now, nullable=False)


class SpecDraft(SQLModel, table=True):
    __tablename__ = "spec_drafts"
    spec_draft_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    discovery_run_id: int = Field(index=True)
    kind: str = Field(index=True)
    version_number: int
    canonical_content_json: str = Field(sa_type=Text)
    content_fingerprint: str = Field(index=True)
    base_spec_version_id: int | None = Field(default=None, index=True)
    base_spec_hash: str | None = Field(default=None, index=True)
    supersedes_spec_draft_id: int | None = Field(default=None)
    provenance_path: str | None = Field(default=None, sa_type=Text)
    created_at: datetime = Field(default_factory=utc_now, nullable=False)


class SpecDraftDecision(SQLModel, table=True):
    __tablename__ = "spec_draft_decisions"
    spec_draft_decision_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    discovery_run_id: int = Field(index=True)
    spec_draft_id: int = Field(index=True)
    artifact_fingerprint: str = Field(index=True)
    decision: str = Field(index=True)
    reviewer: str = Field(index=True)
    notes: str = Field(sa_type=Text)
    idempotency_key: str = Field(index=True)
    decided_at: datetime = Field(default_factory=utc_now, nullable=False)


class InitialScopeRegistration(SQLModel, table=True):
    __tablename__ = "initial_scope_registrations"
    initial_scope_registration_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    discovery_run_id: int = Field(index=True)
    spec_draft_id: int = Field(index=True)
    spec_version_id: int = Field(index=True)
    spec_hash: str = Field(index=True)
    registered_by: str = Field(index=True)
    registered_at: datetime = Field(default_factory=utc_now, nullable=False)
```

Also add these exact persisted fields; use `Text` for every JSON/payload/error string:

```python
class ProjectAbandonment(SQLModel, table=True):
    project_abandonment_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    reason: str = Field(sa_type=Text)
    abandoned_by: str = Field(index=True)
    abandoned_at: datetime = Field(default_factory=utc_now, nullable=False)


class DiscoveryRunAbandonment(SQLModel, table=True):
    discovery_run_abandonment_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    discovery_run_id: int = Field(index=True)
    reason: str = Field(sa_type=Text)
    abandoned_by: str = Field(index=True)
    abandoned_at: datetime = Field(default_factory=utc_now, nullable=False)


class RepositoryBaseline(SQLModel, table=True):
    repository_baseline_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    repository_path: str = Field(sa_type=Text)
    git_commit: str | None = Field(default=None, index=True)
    dirty: bool
    content_fingerprint: str = Field(index=True)
    version_number: int
    recorded_at: datetime = Field(default_factory=utc_now, nullable=False)


class RepositoryInventory(SQLModel, table=True):
    repository_inventory_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    repository_baseline_id: int = Field(index=True)
    canonical_inventory_json: str = Field(sa_type=Text)
    selected_for_model_json: str = Field(sa_type=Text)
    content_fingerprint: str = Field(index=True)
    version_number: int
    file_count: int
    total_bytes: int
    recorded_at: datetime = Field(default_factory=utc_now, nullable=False)


class WorkflowNodeAttempt(SQLModel, table=True):
    workflow_node_attempt_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    node_id: str = Field(index=True)
    instance_key: str | None = Field(default=None, index=True)
    graph_version: str
    fact_fingerprint: str
    business_fact_fingerprint: str
    decision_fingerprint: str
    normalized_input_json: str = Field(sa_type=Text)
    input_fingerprint: str
    model_id: str
    execution_settings_json: str = Field(sa_type=Text)
    idempotency_key: str = Field(index=True)
    actor: str
    correlation_id: str | None = Field(default=None)
    started_at: datetime
    lease_expires_at: datetime
    attempt_fingerprint: str = Field(index=True)


class WorkflowNodeAttemptOutcome(SQLModel, table=True):
    workflow_node_attempt_outcome_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    workflow_node_attempt_id: int = Field(index=True)
    status: str = Field(index=True)
    output_fingerprint: str | None = Field(default=None)
    output_json: str | None = Field(default=None, sa_type=Text)
    failure_code: str | None = Field(default=None)
    failure_message: str | None = Field(default=None, sa_type=Text)
    recorded_at: datetime


class WorkflowTransitionReceipt(SQLModel, table=True):
    workflow_transition_receipt_id: int | None = Field(default=None, primary_key=True)
    request_kind: str = Field(index=True)
    idempotency_key: str = Field(index=True)
    request_fingerprint: str
    request_json: str = Field(sa_type=Text)
    result_json: str | None = Field(default=None, sa_type=Text)
    started_at: datetime
    completed_at: datetime | None = Field(default=None)
```

Use `CheckConstraint`, `UniqueConstraint`, `ForeignKeyConstraint`, and partial SQLite `Index` declarations to enforce every test from Step 1. `WorkflowTransitionReceipt` stores request/result JSON for idempotency but is not a Workflow Fact and is never parsed by graph rules.

Implement these named database invariants, rather than relying on handler checks:

| Constraint/index | Exact invariant |
|---|---|
| `uq_discovery_project_id` | `DiscoveryRun(project_id, discovery_run_id)` is a composite parent key. |
| `uq_discovery_purpose_ordinal` | `(project_id, purpose, ordinal)` is unique. |
| `uq_initial_discovery_per_project` | Partial unique index on `project_id` where `purpose = 'initial'`. |
| `uq_open_extension_per_project` | Partial unique index on `project_id` where `purpose = 'extension' AND closed_at IS NULL`. Abandonment writes its typed row and closes the run atomically. |
| `ck_discovery_purpose` | `purpose` is `initial` or `extension`. |
| Artifact parent FKs | Every challenge, PRD, draft, and review row uses a composite `(project_id, discovery_run_id)` FK to its discovery run. |
| Artifact version keys | `(project_id, discovery_run_id, version_number)` and `(project_id, discovery_run_id, content_fingerprint)` are unique in each versioned artifact table. |
| Review parent FKs | PRD and draft decisions use composite FKs including `project_id`, `discovery_run_id`, and the reviewed artifact ID. |
| One decision per version | `(project_id, prd_version_id)` and `(project_id, spec_draft_id)` are unique in their decision tables. |
| `ck_spec_draft_base` | `initial` requires both base fields null; `amendment` requires both non-null. No other kind is valid. |
| Initial registration identity | `project_id`, `discovery_run_id`, `spec_draft_id`, and `spec_version_id` are each independently unique in `InitialScopeRegistration`, with composite FKs to the Project-owned run, draft, and spec. |
| One abandonment | Each Project and discovery run has at most one abandonment row. |
| Baseline/inventory versions | `(project_id, version_number)` and `(project_id, content_fingerprint)` are unique for each repository artifact type; inventory references a same-Project baseline through a composite FK. |
| One attempt outcome | `WorkflowNodeAttemptOutcome.workflow_node_attempt_id` is unique and references its same-Project attempt. |
| Attempt/outcome shape | Lease expiry is after start; outcome status is `success`, `failure`, or `obsolete`; success requires output fields; failure requires failure fields; obsolete stores neither accepted output nor failure payload. |
| One live attempt per decision | The `BEGIN IMMEDIATE` guarded start handler rejects a second outcome-free attempt for `(project_id, node_id, instance_key)` unless the prior lease expired and is marked obsolete in the same transaction. |
| One transition receipt | `(request_kind, idempotency_key)` is unique. The row also stores canonical `request_fingerprint`, serialized result, and completion timestamp. |

Add every composite parent key as a `UniqueConstraint` before referencing it. Enable `PRAGMA foreign_keys=ON` in production and test engine creation; tests must fail if this pragma is absent.

Use `products.product_id` only as the declared branch-only FK target. Task 17 changes it to `projects.project_id` and removes the old name.

- [ ] **Step 5: Register models and simplify fresh bootstrap behavior**

Import `models.workflow` in `models/db.py` so `SQLModel.metadata.create_all()` sees the tables. Do not add an old-database interpretation migration. Keep existing migrations callable until Task 17 so intermediate tests remain green.

- [ ] **Step 6: Verify constraints and full gate**

Run: `uv run --frozen pytest tests/workflow/test_workflow_models.py tests/workflow/test_workflow_constraints.py tests/test_business_db_bootstrap.py -q`

Expected: PASS, including database-enforced cross-Project and cardinality failures.

Run: `uv run --frozen pyrepo-check --all`

Expected: all checks pass.

- [ ] **Step 7: Commit**

```bash
git add models tests/workflow tests/test_business_db_bootstrap.py
git commit -m "feat: persist typed workflow facts"
```

## Task 5: Load One Full Typed Fact Snapshot

**Files:**
- Create: `repositories/workflow.py`
- Modify: `repositories/__init__.py`
- Create: `tests/workflow/test_workflow_repository.py`
- Create: `tests/workflow/test_snapshot_restart.py`

**Interfaces:**
- Consumes: a caller-owned `sqlmodel.Session` and all named fact tables.
- Produces: `WorkflowFactRepository.load(project_id: int) -> WorkflowFactSnapshot` and session-bound write helpers that never commit independently.

- [ ] **Step 1: Write repository mapping and restart tests**

Seed a complete Project with discovery, accepted initial spec, accepted authority, accepted backlog, one completed sprint, triage, and one extension run. Assert exact typed tuples, deterministic ordering, and identical fact fingerprints before and after closing every SQLModel session and recreating `WorkflowDomain` dependencies.

```python
def test_snapshot_is_reproducible_after_repository_restart(tmp_path: Path) -> None:
    engine = sqlite_engine(tmp_path / "workflow.db")
    project_id = seed_complete_project(engine)
    with Session(engine) as first_session:
        first = WorkflowFactRepository(first_session).load(project_id)
    with Session(engine) as second_session:
        second = WorkflowFactRepository(second_session).load(project_id)
    assert first == second
    assert fact_fingerprint(first) == fact_fingerprint(second)
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run --frozen pytest tests/workflow/test_workflow_repository.py tests/workflow/test_snapshot_restart.py -q`

Expected: collection FAIL because the repository does not exist.

- [ ] **Step 3: Implement explicit typed loaders**

`WorkflowFactRepository` must accept an existing session and never call `commit()`, `rollback()`, or `close()`.

```python
class WorkflowFactRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def load(self, project_id: int) -> WorkflowFactSnapshot:
        project = self._project(project_id)
        return WorkflowFactSnapshot(
            project=project,
            discovery_runs=self._discovery_runs(project_id),
            challenge_artifacts=self._challenge_artifacts(project_id),
            prd_versions=self._prd_versions(project_id),
            review_decisions=self._review_decisions(project_id),
            spec_drafts=self._spec_drafts(project_id),
            initial_registrations=self._initial_registrations(project_id),
            authorities=self._authorities(project_id),
            phase_artifacts=self._phase_artifacts(project_id),
            sprints=self._sprints(project_id),
            stories=self._stories(project_id),
            tasks=self._tasks(project_id),
            post_sprint_triage=self._post_sprint_triage(project_id),
            node_attempts=self._node_attempts(project_id),
        )
```

Each private loader issues explicit ordered selects and maps SQLModel rows to named frozen facts. It may parse domain-specific canonical artifact JSON only to validate stored content; graph selectors consume typed fields, not arbitrary session or event JSON. The mutation ledger is not a source in `load()`.

- [ ] **Step 4: Add database-content drift tests**

Record an accepted draft with canonical JSON plus a provenance path. Change or delete the file. Reload and assert the snapshot and fingerprint do not change. Add a cross-Project fixture and prove the repository raises `WorkflowFactLoadError` if corruption is forced with foreign keys disabled.

- [ ] **Step 5: Verify GREEN and query ownership**

Run: `uv run --frozen pytest tests/workflow/test_workflow_repository.py tests/workflow/test_snapshot_restart.py -q`

Expected: PASS.

Run: `rg -n "session_reader|get_project_state|fsm_state" repositories/workflow.py workflow/facts.py`

Expected: no output.

Run: `uv run --frozen pyrepo-check --all`

Expected: all checks pass.

- [ ] **Step 6: Commit**

```bash
git add repositories/workflow.py repositories/__init__.py tests/workflow
git commit -m "feat: load canonical workflow snapshots"
```

## Task 6: Implement Guarded Transaction And Idempotency Core

**Files:**
- Create: `workflow/domain.py`
- Create: `workflow/handlers/__init__.py`
- Create: `workflow/handlers/project_shell.py`
- Create: `workflow/requests/project_shell.py`
- Modify: `workflow/requests/__init__.py`
- Modify: `workflow/definitions/root.py`
- Modify: `workflow/__init__.py`
- Create: `tests/workflow/test_workflow_domain.py`
- Create: `tests/workflow/test_transition_idempotency.py`
- Create: `tests/workflow/test_transition_concurrency.py`

**Interfaces:**
- Consumes: `WorkflowGraph`, `WorkflowFactRepository`, `WorkflowTransitionReceipt`, and injected `Clock`.
- Produces: `WorkflowDomain.position(project_id)` and `WorkflowDomain.transition(request)` with `OpenProjectShell` and `AbandonProjectShell` as the first executable variants.

- [ ] **Step 1: Write public-interface and stale-guard tests**

Test shell creation, name uniqueness, idempotent replay, same-key/different-request conflict, stale graph version, stale fact fingerprint, stale decision fingerprint, expired decision, unavailable node, rollback after handler exception, and two concurrent identical requests. The concurrency test uses two independent sessions and a barrier immediately before receipt claim; assert one handler invocation, one completed receipt, and one replayed result.

```python
def test_stale_fact_fingerprint_returns_new_position_without_mutation(domain: WorkflowDomain) -> None:
    project_id = open_greenfield_shell(domain)
    offered = domain.position(project_id)
    mutate_fact_outside_domain(project_id)
    result = domain.transition(abandon_request(offered, fact_fingerprint="sha256:old"))
    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.STALE_POSITION
    assert result.position is not None
    assert result.position.fact_fingerprint != "sha256:old"
    assert project_is_not_abandoned(project_id)
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run --frozen pytest tests/workflow/test_workflow_domain.py tests/workflow/test_transition_idempotency.py tests/workflow/test_transition_concurrency.py -q`

Expected: collection FAIL because `WorkflowDomain` and requests do not exist.

- [ ] **Step 3: Define the first closed request union**

```python
class OpenProjectShell(FrozenModel):
    kind: Literal["open_project_shell"] = "open_project_shell"
    name: str = Field(min_length=1, max_length=200)
    origin: Literal["greenfield", "brownfield"]
    idempotency_key: str = Field(min_length=1, max_length=200)
    actor: str = Field(min_length=1, max_length=200)
    correlation_id: str | None = None


class AbandonProjectShell(PositionedRequest):
    kind: Literal["abandon_project_shell"] = "abandon_project_shell"
    node_id: ClassVar[str] = "onboarding.abandon_shell"
    reason: str = Field(min_length=1)


TransitionRequest: TypeAlias = OpenProjectShell | AbandonProjectShell
```

Expand this alias in subsequent tasks. It must remain importable from `workflow.requests` and `workflow`.

- [ ] **Step 4: Implement the two-method domain service**

```python
class WorkflowDomain:
    def __init__(self, *, engine: Engine, graph: WorkflowGraph, clock: Clock) -> None:
        self._engine = engine
        self._graph = graph
        self._clock = clock

    def position(self, project_id: int) -> WorkflowPosition:
        evaluated_at = self._clock.now()
        with Session(self._engine) as session:
            snapshot = WorkflowFactRepository(session).load(project_id)
            return self._graph.evaluate(snapshot, evaluated_at)

    def transition(self, request: TransitionRequest) -> TransitionResult:
        evaluated_at = self._clock.now()
        with Session(self._engine) as session:
            self._begin_write(session)
            try:
                receipt = self._claim_receipt(session, request, evaluated_at)
                if receipt.replayed_result is not None:
                    result = receipt.replayed_result.model_copy(update={"replayed": True})
                    session.commit()
                    return result
                if isinstance(request, OpenProjectShell):
                    result = execute_open_project_shell(
                        session,
                        request,
                        self._graph,
                        evaluated_at,
                    )
                else:
                    before = self._position_in_session(
                        session,
                        request.project_id,
                        evaluated_at,
                    )
                    failure = self._guard_failure(request, before, evaluated_at)
                    if failure is not None:
                        result = failure
                    else:
                        decision = self._available_decision(before, request)
                        result = dispatch_transition(
                            session,
                            request,
                            decision,
                            evaluated_at,
                        )
                        result = result.model_copy(
                            update={
                                "position": self._position_in_session(
                                    session,
                                    request.project_id,
                                    evaluated_at,
                                )
                            }
                        )
                self._complete_receipt(session, receipt, result, evaluated_at)
                session.commit()
                return result
            except Exception:
                session.rollback()
                raise
```

`_begin_write()` executes `BEGIN IMMEDIATE` as the first database statement on SQLite; for any later supported dialect it calls `session.begin()`. This serializes receipt claim and fact writes before either concurrent writer can observe a missing receipt. Set a finite SQLite busy timeout and convert timeout exhaustion into `WORKFLOW_FACT_CONFLICT`; never retry a non-idempotent handler outside the receipt transaction.

`_claim_receipt()` uses the unique `(request_kind, idempotency_key)` constraint and compares a canonical request fingerprint. Replays return the recorded result. Reuse with changed input returns a conflict without invoking a handler. Fact writes, audit attribution, and receipt completion use the same SQLModel session and transaction. A replay path still completes the local transaction before returning.

`_guard_failure()` checks graph version, fact fingerprint, exact node decision fingerprint, and `valid_until` before dispatch. It returns the newly derived position for `STALE_POSITION`. `_available_decision()` matches the exact `(request.decision_node_id(), request.decision_instance_key())` pair and rejects waiting, blocked, invalid, satisfied, and absent nodes as `TRANSITION_NOT_AVAILABLE` or `WORKFLOW_FACT_CONFLICT`.

- [ ] **Step 5: Make shell creation and abandonment factual**

`OpenProjectShell` inserts the top-level Project row plus exactly one initial `DiscoveryRun` in one transaction. It does not create authority, backlog, stories, sprints, or a session. `AbandonProjectShell` inserts `ProjectAbandonment`; hard delete remains allowed only before accepted authority.

- [ ] **Step 6: Verify concurrency and rollback GREEN**

Run: `uv run --frozen pytest tests/workflow/test_workflow_domain.py tests/workflow/test_transition_idempotency.py tests/workflow/test_transition_concurrency.py -q`

Expected: PASS; concurrency produces one fact mutation and one replayed result.

Run: `uv run --frozen pyrepo-check --all`

Expected: all checks pass with typed request narrowing and no suppression.

- [ ] **Step 7: Commit**

```bash
git add workflow tests/workflow
git commit -m "feat: guard workflow transitions transactionally"
```

## Task 7: Implement Greenfield Project-Shell Onboarding And Initial Registration

**Files:**
- Create: `workflow/requests/onboarding.py`
- Create: `workflow/definitions/onboarding.py`
- Create: `workflow/handlers/onboarding.py`
- Modify: `workflow/requests/__init__.py`
- Modify: `workflow/handlers/__init__.py`
- Modify: `workflow/definitions/root.py`
- Modify: `services/specs/lifecycle_service.py`
- Create: `tests/workflow/test_greenfield_onboarding_graph.py`
- Create: `tests/workflow/test_greenfield_onboarding_transitions.py`
- Create: `tests/workflow/test_initial_scope_registration.py`
- Create: `tests/workflow/test_greenfield_probe_regressions.py`

**Interfaces:**
- Consumes: Project Shell, initial `DiscoveryRun`, canonical artifact storage, and `SpecRegistry`.
- Produces: greenfield request variants, pure onboarding rules, append-only decisions, and one-shot `RegisterInitialScope`.

- [ ] **Step 1: Write the greenfield graph matrix**

Create one table row for every greenfield stage: no challenge, challenge recorded, PRD draft, pending PRD review, rejected PRD, accepted PRD, initial spec draft, pending spec review, rejected spec, accepted spec, registered scope, contradictory double acceptance, and abandoned shell.

```python
def test_greenfield_shell_requires_challenge_artifact() -> None:
    position = greenfield_graph().evaluate(greenfield_shell_snapshot(), EVALUATED_AT)
    assert position.available_nodes == ("onboarding.greenfield.challenge",)
    assert decision(position, "onboarding.greenfield.challenge").request_kind == (
        "record_challenge_artifact"
    )
```

```python
def test_accepted_initial_spec_exposes_registration_not_executable_work() -> None:
    position = greenfield_graph().evaluate(accepted_initial_spec_snapshot(), EVALUATED_AT)
    assert "onboarding.initial_scope_registration" in position.available_nodes
    assert all(not node.startswith("backlog.") for node in position.available_nodes)
```

- [ ] **Step 2: Run graph tests and verify RED**

Run: `uv run --frozen pytest tests/workflow/test_greenfield_onboarding_graph.py -q`

Expected: collection FAIL because onboarding definitions do not exist.

- [ ] **Step 3: Define exact greenfield request variants**

Add these classes to `workflow/requests/onboarding.py`, each inheriting `PositionedRequest` and fixing `kind` plus `node_id`:

```python
class RecordChallengeArtifact(PositionedRequest):
    kind: Literal["record_challenge_artifact"] = "record_challenge_artifact"
    node_id: ClassVar[str] = "onboarding.greenfield.challenge"
    canonical_content: JsonObject
    provenance_path: str | None = None


class RecordPrdVersion(PositionedRequest):
    kind: Literal["record_prd_version"] = "record_prd_version"
    node_id: ClassVar[str] = "onboarding.greenfield.prd"
    challenge_artifact_id: int
    canonical_content: JsonObject
    supersedes_prd_version_id: int | None = None
    provenance_path: str | None = None


class DecidePrd(PositionedRequest):
    kind: Literal["decide_prd"] = "decide_prd"
    node_id: ClassVar[str] = "onboarding.greenfield.prd_review"
    prd_version_id: int
    artifact_fingerprint: str
    decision: Literal["accepted", "rejected", "feedback"]
    notes: str


class RecordInitialSpecDraft(PositionedRequest):
    kind: Literal["record_initial_spec_draft"] = "record_initial_spec_draft"
    node_id: ClassVar[str] = "onboarding.greenfield.initial_spec"
    prd_version_id: int
    canonical_content: JsonObject
    supersedes_spec_draft_id: int | None = None
    provenance_path: str | None = None


class DecideInitialSpecDraft(PositionedRequest):
    kind: Literal["decide_initial_spec_draft"] = "decide_initial_spec_draft"
    node_id: ClassVar[str] = "onboarding.greenfield.initial_spec_review"
    spec_draft_id: int
    artifact_fingerprint: str
    decision: Literal["accepted", "rejected", "feedback"]
    notes: str


class RegisterInitialScope(PositionedRequest):
    kind: Literal["register_initial_scope"] = "register_initial_scope"
    node_id: ClassVar[str] = "onboarding.initial_scope_registration"
    spec_draft_id: int
```

Add all six variants to the closed `TransitionRequest` union.

- [ ] **Step 4: Implement pure greenfield rules**

Rules select facts by the Project's initial `DiscoveryRun`, exact artifact fingerprint, and append-only terminal decisions. Rejection exposes the corresponding new-version node. Multiple terminal decisions for one exact artifact yield `invalid` with `WORKFLOW_FACT_CONFLICT`; they do not select the newest mutable status.

The first required flow is exactly:

```text
onboarding.greenfield.challenge
onboarding.greenfield.prd
onboarding.greenfield.prd_review
onboarding.greenfield.initial_spec
onboarding.greenfield.initial_spec_review
onboarding.initial_scope_registration
authority.compile
authority.review
```

Do not add a raw-spec bypass.

- [ ] **Step 5: Implement immutable writes and append-only decisions**

Handlers canonicalize `canonical_content`, compute its hash, and insert a new version. They never reopen a provenance file to reconstruct accepted content. `DecidePrd` and `DecideInitialSpecDraft` verify the exact stored fingerprint and append decision rows; they never update draft rows.

`RecordInitialSpecDraft` always writes `kind="initial"`, `base_spec_version_id=None`, and `base_spec_hash=None`. Database and handler validation both reject amendment fields.

- [ ] **Step 6: Implement one-shot Initial Scope Registration**

Refactor `services/specs/lifecycle_service.py` so the low-level registration function accepts a caller-owned session and canonical stored JSON. The onboarding handler must perform these writes in one domain transaction:

```python
canonical_content = accepted_draft.canonical_content_json
spec_hash = canonical_hash(json.loads(canonical_content))
spec = SpecRegistry(
    product_id=request.project_id,
    spec_hash=spec_hash,
    content=canonical_content,
    content_ref=accepted_draft.provenance_path,
    status="approved",
    approved_at=evaluated_at,
    approved_by=request.actor,
    approval_notes="Initial scope registration",
)
session.add(spec)
session.flush()
session.add(
    InitialScopeRegistration(
        project_id=request.project_id,
        discovery_run_id=accepted_draft.discovery_run_id,
        spec_draft_id=accepted_draft.spec_draft_id,
        spec_version_id=require_id(spec.spec_version_id),
        spec_hash=spec_hash,
        registered_by=request.actor,
        registered_at=evaluated_at,
    )
)
```

The handler returns the new spec version ID/hash. It does not compile authority or unlock executable work.

- [ ] **Step 7: Add the pre-Project failure regressions**

Prove all three previously observed failures are impossible:

1. mutate/delete the source file after review and register the stored content unchanged;
2. replay shell/opening and registration keys without creating a second Project or spec;
3. fail after Project creation, replay, and retain discovery/artifact provenance because those rows already belong to the Project Shell.

Run: `uv run --frozen pytest tests/workflow/test_greenfield_onboarding_graph.py tests/workflow/test_greenfield_onboarding_transitions.py tests/workflow/test_initial_scope_registration.py tests/workflow/test_greenfield_probe_regressions.py -q`

Expected: PASS.

- [ ] **Step 8: Run the full gate and commit**

Run: `uv run --frozen pyrepo-check --all`

Expected: all checks pass.

```bash
git add workflow services/specs/lifecycle_service.py tests/workflow
git commit -m "feat: onboard greenfield project shells"
```

## Task 8: Implement Git-Aware Brownfield Inventory And Onboarding

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `services/agent_workbench/repository_inventory.py`
- Modify: `services/agent_workbench/brownfield_curation.py`
- Modify: `workflow/facts.py`
- Modify: `workflow/requests/onboarding.py`
- Modify: `workflow/requests/__init__.py`
- Modify: `workflow/definitions/onboarding.py`
- Modify: `workflow/handlers/onboarding.py`
- Create: `tests/workflow/test_repository_inventory.py`
- Create: `tests/workflow/test_brownfield_onboarding_graph.py`
- Create: `tests/workflow/test_brownfield_onboarding_transitions.py`

**Interfaces:**
- Consumes: a brownfield Project Shell and a repository path supplied as operator input.
- Produces: `RepositoryInventoryService.inventory(root) -> RepositoryInventoryResult`, deterministic bounded model selection, brownfield baseline/inventory/curation requests, and reviewed initial registration convergence.

- [ ] **Step 1: Write Git inventory tests before adding GitPython**

Create temporary Git repositories containing tracked files, non-ignored untracked files, repository-ignore entries, `.git/info/exclude` entries, a configured global ignore file, filenames containing spaces and newlines, symlinks, a secret-shaped file, and an oversized file.

Assert:

- tracked and non-ignored untracked paths appear in deterministic byte-order;
- all three Git ignore sources are honored;
- secret and oversized paths remain represented in the complete inventory with content/hash suppression metadata;
- model selection excludes them;
- hitting an inventory bound raises `RepositoryInventoryLimitError` with count, byte total, limit, and remediation;
- hitting the model-context budget does not truncate the complete inventory.

```python
def test_model_budget_does_not_truncate_complete_inventory(git_repo: Path) -> None:
    create_many_text_files(git_repo, count=1_200)
    result = RepositoryInventoryService(
        limits=InventoryLimits(
            max_files=50_000,
            max_total_bytes=2_000_000_000,
            max_hash_bytes_per_file=10_000_000,
            max_model_files=500,
            max_model_bytes=2_000_000,
        )
    ).inventory(git_repo)
    assert len(result.files) == 1_200
    assert len(result.selected_for_model) == 500
    assert result.truncated is False
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run --frozen pytest tests/workflow/test_repository_inventory.py -q`

Expected: collection FAIL because the inventory service does not exist.

- [ ] **Step 3: Add the tested Git dependency**

Pin `GitPython==3.1.57` in `pyproject.toml` and refresh `uv.lock`. GitPython is justified here because the implementation must use Git's tracked/non-ignored view without introducing subprocess-security suppressions. The inventory service is short-lived and explicitly closes repository resources after each scan.

- [ ] **Step 4: Implement complete inventory separately from model selection**

Use these exact contracts:

```python
@dataclass(frozen=True)
class InventoryLimits:
    max_files: int = 50_000
    max_total_bytes: int = 2_000_000_000
    max_hash_bytes_per_file: int = 10_000_000
    max_model_files: int = 500
    max_model_bytes: int = 2_000_000


@dataclass(frozen=True)
class InventoryFile:
    path: str
    size_bytes: int
    sha256: str | None
    content_status: Literal["hashable", "secret", "oversized", "symlink"]


@dataclass(frozen=True)
class RepositoryInventoryResult:
    root: Path
    git_available: bool
    commit: str | None
    dirty: bool
    files: tuple[InventoryFile, ...]
    selected_for_model: tuple[str, ...]
    total_bytes: int
    inventory_fingerprint: str
    truncated: Literal[False] = False
```

For Git repositories use `with Repo(root) as repo:` and call `repo.git.ls_files("-co", "--exclude-standard", "-z")`, split on NUL, and sort encoded relative paths deterministically. Capture `HEAD` plus the porcelain-status fingerprint before and after hashing; raise `RepositoryChangedDuringInventoryError` if either changes, so a mixed snapshot is never persisted. Record commit and dirty state from that verified snapshot. For non-Git directories use a fallback walker with an explicit fixed ignore policy and `git_available=False`.

The deterministic model selector ranks safe files by recognized source/config/doc type, path depth, bounded size, and stable lexical tie-break. It returns a bounded subset only; it never changes `files`.

- [ ] **Step 5: Replace the old 1,000-file early return**

Delete `MAX_SCAN_MANIFEST_FILES` and `_file_manifest()` from `services/agent_workbench/brownfield_curation.py`. Adapt the existing curation service to persist `RepositoryInventoryResult.files` and `selected_for_model` separately. No warning may describe a successful inventory as truncated.

- [ ] **Step 6: Add brownfield request variants and rules**

Add these positioned variants:

- `RecordRepositoryBaseline` for `onboarding.brownfield.baseline` with repository path, commit, dirty flag, and baseline fingerprint;
- `RecordRepositoryInventory` for `onboarding.brownfield.inventory` with baseline ID and complete inventory fingerprint;
- `RecordBrownfieldSpecDraft` for `onboarding.brownfield.curation` with inventory ID, canonical initial spec content, provenance, and the base-class attempt binding when generated through ADK;
- `DecideBrownfieldInitialSpec` for `onboarding.brownfield.initial_spec_review` with exact draft fingerprint and terminal decision.

The brownfield join reuses `RegisterInitialScope`; both origins converge on the same registration and authority graph. A repository is evidence, not Project identity or accepted authority.

- [ ] **Step 7: Verify actual acceptance-repository capacity without model calls**

Run:

```bash
uv run --frozen python -m services.agent_workbench.repository_inventory /Users/aaat/projects/caRtola --summary
uv run --frozen python -m services.agent_workbench.repository_inventory /Users/aaat/projects/asa-deep-process-control-experiments --summary
uv run --frozen python -m services.agent_workbench.repository_inventory /Users/aaat/myfinance --summary
```

Expected: all three complete with `truncated=false`; current Git-visible counts are approximately 881, 326, and 392 respectively, but the test asserts only complete deterministic output because repository counts can legitimately change.

- [ ] **Step 8: Verify tests, full gate, and commit**

Run: `uv run --frozen pytest tests/workflow/test_repository_inventory.py tests/workflow/test_brownfield_onboarding_graph.py tests/workflow/test_brownfield_onboarding_transitions.py tests/test_agent_workbench_brownfield_curation.py -q`

Expected: PASS.

Run: `uv run --frozen pyrepo-check --all`

Expected: all checks pass.

```bash
git add pyproject.toml uv.lock services/agent_workbench workflow tests/workflow tests/test_agent_workbench_brownfield_curation.py
git commit -m "feat: add complete brownfield inventory"
```

## Task 9: Converge Initial And Extension Authority On One Graph

**Files:**
- Create: `workflow/requests/authority.py`
- Create: `workflow/definitions/authority.py`
- Create: `workflow/handlers/authority.py`
- Modify: `workflow/requests/__init__.py`
- Modify: `workflow/handlers/__init__.py`
- Modify: `workflow/definitions/root.py`
- Modify: `services/specs/compiler_service.py`
- Modify: `services/agent_workbench/authority_decision.py`
- Modify: `services/agent_workbench/authority_review.py`
- Create: `tests/workflow/test_authority_graph.py`
- Create: `tests/workflow/test_authority_transitions.py`
- Create: `tests/workflow/test_authority_restart.py`

**Interfaces:**
- Consumes: approved `SpecRegistry` version plus existing compiler/authority persistence semantics.
- Produces: `CompileAuthority`, `DecideAuthority`, `RecordAuthorityFeedback`, and `RepairAuthority` transitions shared by initial and scope-extension flows.

- [ ] **Step 1: Write authority-state graph tests**

Cover registered spec without compile, active node attempt, compile failure, pending review, accepted authority, rejected authority, feedback, stale authority after a new accepted spec, and conflicting terminal decisions.

```python
def test_accepted_authority_unlocks_downstream_graphs() -> None:
    position = authority_graph().evaluate(accepted_authority_snapshot(), EVALUATED_AT)
    assert "authority.compile" not in position.available_nodes
    assert "authority.review" not in position.available_nodes
    assert "vision.generate" in position.available_nodes
```

```python
def test_pending_review_survives_restart_without_session() -> None:
    first = domain_from_database().position(PROJECT_ID)
    delete_all_adk_sessions_if_present()
    second = domain_from_database().position(PROJECT_ID)
    assert first.fact_fingerprint == second.fact_fingerprint
    assert second.waiting_nodes == ("authority.review",)
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run --frozen pytest tests/workflow/test_authority_graph.py tests/workflow/test_authority_transitions.py tests/workflow/test_authority_restart.py -q`

Expected: collection FAIL because authority graph modules do not exist.

- [ ] **Step 3: Define typed authority requests**

```python
class CompileAuthority(PositionedRequest):
    kind: Literal["compile_authority"] = "compile_authority"
    node_id: ClassVar[str] = "authority.compile"
    spec_version_id: int
    expected_spec_hash: str
    compiler_model: str = "openrouter/openai/gpt-5.6-luna"


class DecideAuthority(PositionedRequest):
    kind: Literal["decide_authority"] = "decide_authority"
    node_id: ClassVar[str] = "authority.review"
    pending_authority_id: int
    authority_fingerprint: str
    review_fingerprint: str
    decision: Literal["accepted", "rejected"]
    rationale: str


class RecordAuthorityFeedback(PositionedRequest):
    kind: Literal["record_authority_feedback"] = "record_authority_feedback"
    node_id: ClassVar[str] = "authority.feedback"
    pending_authority_id: int
    authority_fingerprint: str
    feedback: JsonObject


class RepairAuthority(PositionedRequest):
    kind: Literal["repair_authority"] = "repair_authority"
    node_id: ClassVar[str] = "authority.repair"
    source_authority_id: int
    source_authority_fingerprint: str
```

Add all variants to the closed union.

- [ ] **Step 4: Separate compiler mutation from old session guards**

Refactor compiler and authority decision services into caller-session functions. Preserve compiler invariants, pending-authority fingerprints, review packet completeness, append-only `SpecAuthorityAcceptance`, and mutation audit. Remove `expected_state` and `expected_setup_status` from those low-level functions; `WorkflowDomain` supplies graph/fact/decision guards before calling them.

`CompileAuthority` must verify the spec ID/hash selected by the node decision and persist the pending result only through the caller transaction. `DecideAuthority` binds to the exact pending authority and review fingerprint. Accepted Authority over the current registered spec is the only executable-work gate.

- [ ] **Step 5: Make review waiting factual**

The graph derives pending review from a persisted pending authority with no terminal decision. It does not inspect `setup_status`, `fsm_state`, ADK events, or a session. Deleting a session database must be a no-op for `position()`.

- [ ] **Step 6: Verify GREEN and existing invariant coverage**

Run: `uv run --frozen pytest tests/workflow/test_authority_graph.py tests/workflow/test_authority_transitions.py tests/workflow/test_authority_restart.py tests/test_specs_compiler_service.py tests/test_agent_workbench_authority_review.py tests/test_agent_workbench_authority_decision.py -q`

Expected: PASS; old tests exercise retained compiler/review semantics through caller-session functions.

Run: `uv run --frozen pyrepo-check --all`

Expected: all checks pass.

- [ ] **Step 7: Commit**

```bash
git add workflow services/specs services/agent_workbench/authority_decision.py services/agent_workbench/authority_review.py tests/workflow tests/test_specs_compiler_service.py tests/test_agent_workbench_authority_review.py tests/test_agent_workbench_authority_decision.py
git commit -m "feat: derive authority workflow from facts"
```

## Task 10: Move Vision And Backlog Decisions Behind The Domain Graph

**Files:**
- Create: `workflow/requests/product_definition.py`
- Create: `workflow/definitions/vision.py`
- Create: `workflow/definitions/backlog.py`
- Create: `workflow/handlers/product_definition.py`
- Modify: `workflow/requests/__init__.py`
- Modify: `workflow/handlers/__init__.py`
- Modify: `workflow/definitions/root.py`
- Modify: `services/agent_workbench/vision_phase.py`
- Modify: `services/agent_workbench/backlog_phase.py`
- Modify: `services/agent_workbench/backlog_active_reset.py`
- Modify: `services/agent_workbench/backlog_reconciliation.py`
- Create: `tests/workflow/test_vision_backlog_graph.py`
- Create: `tests/workflow/test_vision_backlog_transitions.py`

**Interfaces:**
- Consumes: accepted current authority and existing durable vision/backlog attempt/artifact rows.
- Produces: draft/decision/reconciliation requests and factual parallel availability where the graph permits it.

- [ ] **Step 1: Write vision/backlog graph tests**

Cover no accepted authority, accepted authority, active generation attempt, draft waiting review, rejection, accepted vision, accepted backlog, superseded backlog, stale accepted artifacts after authority replacement, and explicit join into planning.

```python
def test_shell_with_no_accepted_authority_cannot_generate_backlog() -> None:
    position = project_graph().evaluate(shell_without_authority(), EVALUATED_AT)
    assert "backlog.generate" in position.blocked_nodes
    assert "backlog.generate" not in position.available_nodes
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run --frozen pytest tests/workflow/test_vision_backlog_graph.py tests/workflow/test_vision_backlog_transitions.py -q`

Expected: FAIL because requests/rules are absent.

- [ ] **Step 3: Define exact request families**

Add:

- `RecordVisionDraft` for `vision.generate`, carrying canonical content and authority fingerprint; the base-class attempt binding is populated by the ADK adapter;
- `DecideVision` for `vision.review`, carrying exact draft fingerprint and accepted/rejected/feedback decision;
- `RecordBacklogDraft` for `backlog.generate`, carrying canonical content and accepted authority fingerprint; the base-class attempt binding is populated by the ADK adapter;
- `DecideBacklog` for `backlog.review`, carrying exact artifact fingerprint and terminal decision;
- `ReconcileBacklog` for `backlog.reconcile`, carrying the accepted replacement authority and the exact affected artifact IDs.

Each record remains immutable. Corrections create superseding versions and append decisions.

- [ ] **Step 4: Extract session-free durable mutations**

Refactor the four existing services so generation/output validation and durable writes can be invoked with a caller-owned session. Delete routing checks from the extracted functions. Preserve host-side validation, approval fingerprints, active-backlog replacement safety, progressed-story guards, and reconciliation audit.

The graph rules own whether a command is available. The handlers own only request-specific invariant validation and writes.

- [ ] **Step 5: Verify graph and old business invariants**

Run: `uv run --frozen pytest tests/workflow/test_vision_backlog_graph.py tests/workflow/test_vision_backlog_transitions.py tests/test_agent_workbench_vision_phase.py tests/test_agent_workbench_backlog_phase.py tests/test_backlog_active_reset.py -q`

Expected: PASS.

Run: `uv run --frozen pyrepo-check --all`

Expected: all checks pass.

- [ ] **Step 6: Commit**

```bash
git add workflow services/agent_workbench tests/workflow tests/test_agent_workbench_vision_phase.py tests/test_agent_workbench_backlog_phase.py tests/test_backlog_active_reset.py
git commit -m "feat: route vision and backlog through graph"
```

## Task 11: Move Roadmap, Story, And Sprint Planning Behind The Graph

**Files:**
- Create: `workflow/requests/planning.py`
- Create: `workflow/definitions/planning.py`
- Create: `workflow/handlers/planning.py`
- Modify: `workflow/requests/__init__.py`
- Modify: `workflow/handlers/__init__.py`
- Modify: `workflow/definitions/root.py`
- Modify: `services/agent_workbench/roadmap_phase.py`
- Modify: `services/agent_workbench/story_phase.py`
- Modify: `services/agent_workbench/sprint_phase.py`
- Modify: `services/story_dependencies.py`
- Modify: `services/sprint_input.py`
- Create: `tests/workflow/test_planning_graph.py`
- Create: `tests/workflow/test_planning_transitions.py`
- Create: `tests/workflow/test_planning_joins.py`

**Interfaces:**
- Consumes: accepted current authority, accepted backlog, normalized roadmap/story/sprint records, and existing dependency/readiness semantics.
- Produces: typed roadmap, story, dependency, readiness, sprint-plan, sprint-review, and sprint-start transitions.

- [ ] **Step 1: Write a complete planning matrix**

Table-drive these conditions: no backlog, roadmap draft/review/acceptance, multiple uncovered requirements, parallel story-generation availability, unresolved dependency cycle, missing points/rank, no sprint candidates, sprint-plan draft/review, stale plan after story change, and reviewed plan ready to start.

```python
def test_story_nodes_can_be_available_in_parallel() -> None:
    position = project_graph().evaluate(
        snapshot_with_two_uncovered_requirements(),
        EVALUATED_AT,
    )
    assert position.available_nodes.count("planning.story.generate") == 2
```

Use the repeated-node support built into Tasks 2 and 3. The planning rule returns one `RuleEvaluation` per uncovered requirement, sorted by stable key, with `instance_key="requirement:<requirement_id>"`. `available_nodes` may contain the same stable node ID more than once, while decisions and requests remain uniquely addressable by `(node_id, instance_key)`. Add a property case proving that reversing repository row order does not change decision order or fingerprints.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run --frozen pytest tests/workflow/test_planning_graph.py tests/workflow/test_planning_transitions.py tests/workflow/test_planning_joins.py -q`

Expected: FAIL because planning requests/rules and decision instances are absent.

- [ ] **Step 3: Define planning requests**

Add these exact classes and fixed node IDs:

- `RecordRoadmapDraft` -> `planning.roadmap.generate`;
- `DecideRoadmap` -> `planning.roadmap.review`;
- `RecordStoryDraft` -> `planning.story.generate`, including `requirement_id` as the decision instance;
- `DecideStory` -> `planning.story.review`, binding exact story artifact fingerprint;
- `ApplyStoryDependencies` -> `planning.story_dependencies`, carrying reviewed dependency edges and source fingerprint;
- `RepairStoryReadiness` -> `planning.story_readiness`, carrying exact story IDs and expected readiness fingerprint;
- `RecordSprintPlan` -> `planning.sprint.plan`, carrying selected story IDs and canonical task plan; the base-class attempt binding is populated by the ADK adapter;
- `DecideSprintPlan` -> `planning.sprint.review`, binding the exact plan fingerprint;
- `StartSprint` -> `planning.sprint.start`, binding reviewed plan and candidate-set fingerprint.

All requests inherit the common guard. No request accepts `expected_state`.

- [ ] **Step 4: Implement child rules and explicit joins**

Roadmap becomes available after accepted backlog. Story instances become available for each uncovered accepted requirement after roadmap acceptance. The dependency/readiness join is satisfied only when every selected story has accepted content, valid semantic dependencies, points, and rank. Sprint planning becomes available only after that join and at least one candidate. Sprint start requires an accepted plan whose candidate-set fingerprint still matches.

No scalar `SPRINT_SETUP`, `SPRINT_DRAFT`, or `SPRINT_PERSISTENCE` value appears in a rule.

- [ ] **Step 5: Extract session-free planning mutations**

Retain existing deterministic business rules for story linkage, dependency cycles, readiness repair, plan validation, task metadata, and selected-story conflict checks. Move routing guards to graph tests and make write functions accept the domain's session. A handler exception must roll back its business rows and transition receipt together.

- [ ] **Step 6: Verify focused and retained business tests**

Run:

```bash
uv run --frozen pytest tests/workflow/test_planning_graph.py tests/workflow/test_planning_transitions.py tests/workflow/test_planning_joins.py tests/test_agent_workbench_roadmap_phase.py tests/test_agent_workbench_story_phase.py tests/test_sprint_planner_tools.py -q
```

Expected: PASS.

Run: `uv run --frozen pyrepo-check --all`

Expected: all checks pass.

- [ ] **Step 7: Commit**

```bash
git add workflow services/agent_workbench services/story_dependencies.py services/sprint_input.py tests/workflow tests/test_agent_workbench_roadmap_phase.py tests/test_agent_workbench_story_phase.py tests/test_sprint_planner_tools.py
git commit -m "feat: derive planning workflow from facts"
```

## Task 12: Move Sprint Execution And Post-Sprint Triage Behind The Graph

**Files:**
- Create: `workflow/requests/execution.py`
- Create: `workflow/definitions/execution.py`
- Create: `workflow/handlers/execution.py`
- Modify: `workflow/requests/__init__.py`
- Modify: `workflow/handlers/__init__.py`
- Modify: `workflow/definitions/root.py`
- Modify: `services/agent_workbench/sprint_phase.py`
- Modify: `services/task_execution_service.py`
- Modify: `services/story_close_service.py`
- Modify: `services/agent_workbench/post_sprint_triage.py`
- Create: `tests/workflow/test_execution_graph.py`
- Create: `tests/workflow/test_execution_transitions.py`
- Create: `tests/workflow/test_execution_recovery.py`

**Interfaces:**
- Consumes: active sprint, task dependencies/status/evidence, story completion, sprint review, and durable triage rows.
- Produces: `CompleteTask`, `CloseStory`, `ReviewSprint`, `CloseSprint`, and `RecordPostSprintTriage` transitions plus derived next-task decisions.

- [ ] **Step 1: Write execution graph tests**

Cover no active sprint, next dependency-safe task, in-progress task precedence, blocked dependency, evidence-incomplete completion, story close, all stories terminal, sprint review, sprint close, triage missing, triage recorded with impacts `none`, `backlog`, and `specification`, and interrupted mutation recovery.

```python
def test_next_task_is_derived_from_durable_dependencies() -> None:
    position = project_graph().evaluate(active_sprint_snapshot(), EVALUATED_AT)
    item = decision(position, "execution.task.complete", "task:42")
    assert item.category is NodeCategory.AVAILABLE
    assert item.fact_references[0].fact_id == "42"
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run --frozen pytest tests/workflow/test_execution_graph.py tests/workflow/test_execution_transitions.py tests/workflow/test_execution_recovery.py -q`

Expected: FAIL because execution graph modules are absent.

- [ ] **Step 3: Define exact execution requests**

```python
class CompleteTask(PositionedRequest):
    kind: Literal["complete_task"] = "complete_task"
    node_id: ClassVar[str] = "execution.task.complete"
    instance_key: str
    task_id: int
    outcome_summary: str
    artifact_refs: tuple[str, ...]
    acceptance_result: Literal["partially_met", "fully_met"]
    checklist_result: JsonObject


class CloseStory(PositionedRequest):
    kind: Literal["close_story"] = "close_story"
    node_id: ClassVar[str] = "execution.story.close"
    instance_key: str
    story_id: int
    resolution: str
    delivered: str
    evidence: str
    known_gaps: str


class ReviewSprint(PositionedRequest):
    kind: Literal["review_sprint"] = "review_sprint"
    node_id: ClassVar[str] = "execution.sprint.review"
    sprint_id: int
    review_fingerprint: str


class CloseSprint(PositionedRequest):
    kind: Literal["close_sprint"] = "close_sprint"
    node_id: ClassVar[str] = "execution.sprint.close"
    sprint_id: int
    review_fingerprint: str


class RecordPostSprintTriage(PositionedRequest):
    kind: Literal["record_post_sprint_triage"] = "record_post_sprint_triage"
    node_id: ClassVar[str] = "execution.post_sprint_triage"
    sprint_id: int
    impact: Literal["none", "backlog", "specification"]
    canonical_payload: JsonObject
```

- [ ] **Step 4: Implement pure execution ordering**

The graph selects active work from normalized Sprint/Story/Task facts. An in-progress eligible task remains the required instance before a new to-do task. Dependency cycles or missing prerequisites yield `invalid` or `blocked`, never a guessed next task. Closing a sprint requires every attached story terminal and a persisted review fact. Post-sprint triage is required exactly once for the completed sprint.

- [ ] **Step 5: Refactor writes into caller transactions**

Preserve existing task evidence validation, checklist rules, dependency guards, story-close fingerprinting, sprint-close fingerprinting, and append-only triage corrections. Remove calls that update `fsm_state`, `active_sprint_id`, `latest_completed_sprint_id`, or session JSON. Those values are derived from normalized rows in the next snapshot.

- [ ] **Step 6: Verify focused and existing execution tests**

Run:

```bash
uv run --frozen pytest tests/workflow/test_execution_graph.py tests/workflow/test_execution_transitions.py tests/workflow/test_execution_recovery.py tests/test_agent_workbench_sprint_phase.py tests/test_task_execution_service.py tests/test_story_close_service.py tests/test_post_sprint_triage.py -q
```

Expected: PASS.

Run: `uv run --frozen pyrepo-check --all`

Expected: all checks pass.

- [ ] **Step 7: Commit**

```bash
git add workflow services tests/workflow tests/test_agent_workbench_sprint_phase.py tests/test_task_execution_service.py tests/test_story_close_service.py tests/test_post_sprint_triage.py
git commit -m "feat: derive execution workflow from facts"
```

## Task 13: Implement Scope Extension And The Issue 193 Regression

**Files:**
- Create: `workflow/requests/scope_extension.py`
- Create: `workflow/definitions/scope_extension.py`
- Create: `workflow/handlers/scope_extension.py`
- Modify: `workflow/requests/__init__.py`
- Modify: `workflow/handlers/__init__.py`
- Modify: `workflow/definitions/root.py`
- Modify: `services/agent_workbench/scope_discovery.py`
- Modify: `services/agent_workbench/scope_extension.py`
- Create: `tests/workflow/test_scope_extension_graph.py`
- Create: `tests/workflow/test_scope_extension_transitions.py`
- Create: `tests/workflow/test_issue_193_regression.py`
- Create: `tests/workflow/test_scope_extension_restart.py`

**Interfaces:**
- Consumes: terminal current scope, completed triage, accepted base spec/authority, and one unresolved-extension cardinality.
- Produces: optional new-run start, amendment discovery/review/registration, authority convergence, reconciliation, and stale applied-draft suppression by construction.

- [ ] **Step 1: Write the complete scope-extension matrix**

Cover terminal Project with optional re-entry, active sprint, remaining candidates, missing triage, one unresolved extension, extension challenge/PRD/spec review, rejected versions, accepted amendment, pending registration, pending/accepted extension authority, downstream reconciliation, completed run, and abandoned run.

```python
def test_terminal_project_exposes_optional_new_extension_only() -> None:
    position = project_graph().evaluate(completed_project_snapshot(), EVALUATED_AT)
    start = decision(position, "scope_extension.start", None)
    assert position.terminal is True
    assert start.category is NodeCategory.AVAILABLE
    assert start.recommendation_kind is RecommendationKind.OPTIONAL_REENTRY
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run --frozen pytest tests/workflow/test_scope_extension_graph.py tests/workflow/test_scope_extension_transitions.py tests/workflow/test_issue_193_regression.py -q`

Expected: FAIL because scope-extension graph modules are absent.

- [ ] **Step 3: Define extension-specific requests**

Add:

- `StartScopeExtension` -> `scope_extension.start`, with accepted base spec ID/hash;
- `RecordExtensionChallenge` -> `scope_extension.challenge`;
- `RecordExtensionPrd` -> `scope_extension.prd`;
- `DecideExtensionPrd` -> `scope_extension.prd_review`;
- `RecordAmendmentSpecDraft` -> `scope_extension.spec`, requiring base spec ID/hash;
- `DecideAmendmentSpecDraft` -> `scope_extension.spec_review`;
- `RegisterScopeExtension` -> `scope_extension.registration`;
- `ReconcileScopeExtension` -> `scope_extension.reconciliation`;
- `AbandonScopeExtension` as a recovery request for an unresolved run before accepted replacement authority.

The extension artifact classes are distinct from initial-spec classes even where fields overlap.

- [ ] **Step 4: Implement extension facts and authority convergence**

`StartScopeExtension` inserts a new extension `DiscoveryRun` only after the optional decision is revalidated. The amendment draft pins the accepted base spec ID/hash. Registration reads accepted canonical JSON, creates one new `SpecRegistry` version, and then reuses the authority graph from Task 9. Reconciliation records how accepted backlog/roadmap/story facts relate to replacement authority before the run closes.

The previous extension run's nodes become satisfied when its registered spec has accepted authority and reconciliation is complete. A new run receives a new `DiscoveryRun` ID and cannot reuse an applied draft.

- [ ] **Step 5: Encode issue 193 as a domain regression**

The test must perform these actual transitions:

1. accept one extension draft;
2. register its amended spec;
3. compile and accept replacement authority;
4. reconcile and close the extension run;
5. call `position()` and assert the completed run's start/registration decisions are satisfied and absent from required/recovery next actions;
6. replay the old advertised registration/start request and assert `STALE_POSITION` with no new spec or run;
7. assert a fresh `scope_extension.start` exists only as a new `optional_reentry` decision with a different decision fingerprint.

This regression must not special-case matching hashes in a command renderer. It passes because both query and mutation use the same complete snapshot and graph.

- [ ] **Step 6: Verify restart and old scope business invariants**

Run:

```bash
uv run --frozen pytest tests/workflow/test_scope_extension_graph.py tests/workflow/test_scope_extension_transitions.py tests/workflow/test_issue_193_regression.py tests/workflow/test_scope_extension_restart.py tests/test_agent_workbench_scope_extension.py tests/test_agent_workbench_scope_discovery.py -q
```

Expected: PASS.

Run: `uv run --frozen pyrepo-check --all`

Expected: all checks pass.

- [ ] **Step 7: Commit**

```bash
git add workflow services/agent_workbench/scope_discovery.py services/agent_workbench/scope_extension.py tests/workflow tests/test_agent_workbench_scope_extension.py tests/test_agent_workbench_scope_discovery.py
git commit -m "feat: model scope extension as a graph"
```

## Task 14: Relocate Useful Agent Contracts And Delete Root-Orchestrator Behavior

**Files:**
- Create: `services/contracts/__init__.py`
- Create: `services/contracts/as_built.py`
- Create: `services/contracts/authority.py`
- Create: `services/contracts/backlog.py`
- Create: `services/contracts/vision.py`
- Create: `services/contracts/roadmap.py`
- Create: `services/contracts/specification.py`
- Create: `services/contracts/sprint.py`
- Create: `services/contracts/story.py`
- Create: `adapters/adk/agents/__init__.py`
- Create: `adapters/adk/agents/as_built.py`
- Create: `adapters/adk/agents/authority.py`
- Create: `adapters/adk/agents/backlog.py`
- Create: `adapters/adk/agents/vision.py`
- Create: `adapters/adk/agents/roadmap.py`
- Create: `adapters/adk/agents/specification.py`
- Create: `adapters/adk/agents/sprint.py`
- Create: `adapters/adk/agents/story.py`
- Create: `adapters/adk/prompts/__init__.py`
- Create: `adapters/adk/prompts/specification.py`
- Create: `services/story_linkage.py`
- Modify: all live imports of `orchestrator_agent.agent_tools`
- Move/modify: retained tests named below
- Delete in this task: `orchestrator_agent/agent.py`
- Delete in this task: `orchestrator_agent/agent_tools/utils/resilience.py`

**Interfaces:**
- Consumes: useful leaf definitions and deterministic Pydantic validation from `orchestrator_agent/agent_tools/`.
- Produces: adapter-owned leaf agents, service-owned validation contracts, and no root agent or legacy retry wrapper.

- [ ] **Step 1: Write import-boundary tests before moving files**

Create `tests/adapters/test_agent_contract_boundaries.py` and update existing model/runtime import-boundary tests. Assert:

- `services.contracts` imports no `google.adk`, `litellm`, `orchestrator_agent`, repository, or workflow adapter;
- `adapters.adk.agents` may import `services.contracts`, model config, prompts, and ADK;
- no retained leaf agent imports SQLModel models or repositories;
- no runtime import loads `orchestrator_agent.agent`.

```python
def test_service_contracts_do_not_import_adk() -> None:
    imports = imported_modules_under(Path("services/contracts"))
    assert not {name for name in imports if name.startswith("google.adk")}
```

- [ ] **Step 2: Run boundary tests and verify RED**

Run: `uv run --frozen pytest tests/adapters/test_agent_contract_boundaries.py tests/test_agent_tool_runtime_import_boundary.py tests/test_model_package_boundary.py -q`

Expected: FAIL because the target packages do not exist and live imports still point at `orchestrator_agent`.

- [ ] **Step 3: Classify and move each legacy file by responsibility**

Use this exact mapping; do not move a mixed `tools.py` intact:

| Legacy source | Retained destination |
|---|---|
| `agent_tools/as_built_assessor/agent.py` | `adapters/adk/agents/as_built.py` |
| `agent_tools/as_built_assessor/schemes.py` | `services/contracts/as_built.py` |
| `agent_tools/authority_curation/agent.py` | `adapters/adk/agents/authority.py` |
| `agent_tools/authority_curation/schemes.py` | `services/contracts/authority.py` |
| `agent_tools/backlog_primer/agent.py` | `adapters/adk/agents/backlog.py` |
| `agent_tools/backlog_primer/schemes.py` | `services/contracts/backlog.py` |
| `agent_tools/backlog_primer/tools.py` | validated output helpers to `services/contracts/backlog.py`; writes already moved to `workflow/handlers/product_definition.py` |
| `agent_tools/product_vision_tool/agent.py` | `adapters/adk/agents/vision.py` |
| `agent_tools/product_vision_tool/schemes.py` | `services/contracts/vision.py` |
| `agent_tools/product_vision_tool/tools.py` | validation to `services/contracts/vision.py`; writes already moved to handlers |
| `agent_tools/roadmap_builder/agent.py` | `adapters/adk/agents/roadmap.py` |
| `agent_tools/roadmap_builder/schemes.py` | `services/contracts/roadmap.py` |
| `agent_tools/roadmap_builder/tools.py` | validation to `services/contracts/roadmap.py`; writes already moved to handlers |
| `agent_tools/spec_authority_compiler_agent/agent.py` | `adapters/adk/agents/specification.py` |
| `agent_tools/spec_authority_compiler_agent/compiler_contract.py` | `services/contracts/specification.py` |
| `agent_tools/spec_authority_compiler_agent/instructions_source.py` | `adapters/adk/prompts/specification.py` |
| `agent_tools/spec_authority_compiler_agent/normalizer.py` | `services/contracts/specification.py` |
| `agent_tools/spec_validator_agent/schemes.py` | `services/contracts/specification.py` |
| `agent_tools/spec_validator_agent/tools.py` | deterministic validation to `services/specs/lifecycle_service.py` |
| `agent_tools/sprint_planner_tool/agent.py` | `adapters/adk/agents/sprint.py` |
| `agent_tools/sprint_planner_tool/schemes.py` | `services/contracts/sprint.py` |
| `agent_tools/sprint_planner_tool/tools.py` | validation to `services/contracts/sprint.py`; writes already moved to `workflow/handlers/planning.py` |
| `agent_tools/user_story_writer_tool/agent.py` | `adapters/adk/agents/story.py` |
| `agent_tools/user_story_writer_tool/schemes.py` | `services/contracts/story.py` |
| `agent_tools/user_story_writer_tool/tools.py` | validation to `services/contracts/story.py`; writes already moved to `workflow/handlers/planning.py` |
| `agent_tools/story_linkage.py` | `services/story_linkage.py` |

Where two source files target one destination, preserve exported type names only when they still represent one coherent contract. Split the destination if its McCabe complexity exceeds 10; name the split by domain concept, not by legacy package.

- [ ] **Step 4: Remove root orchestration and legacy resilience**

Delete `orchestrator_agent/agent.py`; no replacement root agent exists. Delete the legacy resilience wrapper instead of wrapping ADK 2 graph execution with it. Task 15 adds explicit recipe-level retry/timeouts and the durable attempt lease.

- [ ] **Step 5: Move retained tests to their owning packages**

Move and update:

- schema tests to `tests/services/contracts/`;
- leaf-agent prompt/registration tests to `tests/adapters/`;
- compiler normalizer tests to `tests/services/contracts/test_specification.py`;
- story linkage tests to `tests/test_story_linkage.py`.

Delete only tests that assert the deleted root agent composition or legacy resilience behavior. Do not delete business validation coverage.

- [ ] **Step 6: Verify all live imports use the new boundaries**

Run: `rg -n "orchestrator_agent\.agent_tools|orchestrator_agent\.agent" --glob '*.py' --glob '!docs/**' .`

Expected: no output.

Run: `uv run --frozen pytest tests/adapters tests/services/contracts tests/test_agent_tool_runtime_import_boundary.py tests/test_model_package_boundary.py -q`

Expected: PASS.

Run: `uv run --frozen pyrepo-check --all`

Expected: all checks pass without carrying the legacy per-file Ruff suppressions onto new files.

- [ ] **Step 7: Commit**

```bash
git add adapters services workflow tests orchestrator_agent
git commit -m "refactor: relocate workflow leaf agents"
```

## Task 15: Add Durable Node Attempts And The ADK 2 Graph Adapter

**Files:**
- Create: `workflow/requests/attempts.py`
- Modify: `workflow/requests/__init__.py`
- Modify: `workflow/requests/base.py`
- Modify: `workflow/domain.py`
- Modify: `workflow/graph.py`
- Modify: `workflow/facts.py`
- Modify: `workflow/fingerprints.py`
- Modify: `workflow/handlers/__init__.py`
- Create: `workflow/handlers/attempts.py`
- Create: `adapters/adk/recipes.py`
- Create: `adapters/adk/runner.py`
- Modify: `utils/runtime_config.py`
- Modify: `tests/workflow/test_fingerprints.py`
- Create: `tests/workflow/test_node_attempts.py`
- Create: `tests/adapters/test_adk_graph_recipes.py`
- Create: `tests/adapters/test_adk_workflow_runner.py`
- Create: `tests/adapters/test_adk_session_independence.py`

**Interfaces:**
- Consumes: available agentic `NodeDecision`, exact ADK 2.2.0 `Workflow`/`Context`/`node` APIs, and retained leaf agents.
- Produces: `StartNodeAttempt`, `FailNodeAttempt`, `AttemptCompletionContext`, `AdkRecipeRegistry`, and `AdkWorkflowRunner.run(decision, input_payload) -> TransitionResult`.

- [ ] **Step 1: Write attempt lifecycle tests**

Cover durable start receipt, duplicate start replay, active lease waiting, expiry recovery, success with downstream fact in one transaction, provider failure, output-validation failure, process crash before outcome, late result after fact change, obsolete outcome, and ADK session deletion.

```python
def test_late_model_result_is_recorded_obsolete_without_authority_fact() -> None:
    attempt = start_attempt(domain, node_id="authority.compile")
    mutate_project_facts(domain, PROJECT_ID)
    result = complete_authority_attempt(domain, attempt)
    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.ATTEMPT_OBSOLETE
    assert stored_attempt_outcome(attempt.attempt_id) == "obsolete"
    assert pending_authority_count(PROJECT_ID) == 0
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run --frozen pytest tests/workflow/test_node_attempts.py tests/adapters/test_adk_graph_recipes.py tests/adapters/test_adk_workflow_runner.py tests/adapters/test_adk_session_independence.py -q`

Expected: collection FAIL because attempt requests and the adapter do not exist.

- [ ] **Step 3: Define attempt requests and lease rules**

```python
class StartNodeAttempt(GuardedRequest):
    kind: Literal["start_node_attempt"] = "start_node_attempt"
    target_node_id: str
    target_instance_key: str | None = None
    normalized_input: JsonObject
    model_id: str
    execution_settings: JsonObject
    lease_seconds: int = Field(ge=30, le=3_600)


class FailNodeAttempt(FrozenModel):
    kind: Literal["fail_node_attempt"] = "fail_node_attempt"
    project_id: int
    attempt_id: int
    attempt_fingerprint: str
    failure_code: str
    failure_message: str
    idempotency_key: str
    actor: str
    correlation_id: str | None = None
```

`StartNodeAttempt` is not a generic business-action escape hatch. The domain accepts it only for a currently available decision whose stable node ID exists in `AdkRecipeRegistry`. Its target decision fingerprint remains the request's `decision_fingerprint`.

Add `business_fact_fingerprint(snapshot)` in `workflow/fingerprints.py`. It hashes the same canonical Project snapshot as `fact_fingerprint()` but excludes `node_attempts`; it does not exclude any business artifact, review, authority, planning, execution, or triage fact. Tests prove that starting an attempt changes the full fact fingerprint but not this business fingerprint, while changing any business fact changes both.

Starting an attempt stores the target node/instance, offered graph/fact/decision fingerprints, current business fact fingerprint, normalized input fingerprint, immutable execution settings, lease, and a canonical attempt fingerprint. `TransitionResult.output` returns only `attempt_id`, `attempt_fingerprint`, and `lease_expires_at`.

An active attempt changes its target node to `waiting`, with `valid_until=lease_expires_at`. At that instant, the same target becomes an available `recovery` decision referencing the expired attempt. Starting recovery atomically records the expired attempt as `obsolete` and creates the replacement attempt.

A generated `PositionedRequest` carrying both `attempt_id` and `attempt_fingerprint` is an attempt continuation. The domain does not require that target to remain publicly available, because the active attempt intentionally made it waiting. Instead it verifies: no outcome exists; the lease is live; the attempt fingerprint matches; request node and instance match the attempt; and current `business_fact_fingerprint` equals the start value. It then runs the same request-specific invariant handler and writes the downstream business fact plus `WorkflowNodeAttemptOutcome(status="success")` in one transaction. Any mismatch writes `obsolete`, writes no business fact, and returns `ATTEMPT_OBSOLETE`. `FailNodeAttempt` performs the same attempt checks and writes only `failure`, or `obsolete` if the continuation is late or stale.

- [ ] **Step 4: Implement one recipe registry with no business prerequisites**

```python
@dataclass(frozen=True)
class AttemptCompletionContext:
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


OutputAdapter: TypeAlias = Callable[
    [object, AttemptCompletionContext],
    PositionedRequest,
]


@dataclass(frozen=True)
class AdkRecipe:
    node_id: str
    workflow: Workflow
    output_adapter: OutputAdapter


class AdkRecipeRegistry:
    def __init__(self, recipes: tuple[AdkRecipe, ...]) -> None:
        self._recipes = {recipe.node_id: recipe for recipe in recipes}
        if len(self._recipes) != len(recipes):
            raise ValueError("ADK recipe node IDs must be unique")

    def require(self, node_id: str) -> AdkRecipe:
        try:
            return self._recipes[node_id]
        except KeyError as exc:
            raise UnknownAdkRecipeError(node_id) from exc
```

Recipes map only node ID to execution. They contain no prerequisite, completion, terminal, or next-command condition.

- [ ] **Step 5: Build ADK graph workflows using the pinned API**

Use ADK 2.2.0 imports verified by the local environment:

```python
from google.adk import Context, Workflow
from google.adk.apps import App, ResumabilityConfig
from google.adk.runners import Runner
from google.adk.workflow import node
```

For each agentic node, create a recipe workflow with `@node` functions and `Context.run_node`. Use ADK parallel or join edges only inside one artifact-generation recipe. Do not reproduce the root Project graph.

```python
class RecipeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payload: JsonObject


class RecipeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payload: JsonObject


def build_backlog_generation_workflow(*, leaf_agent: BaseAgent) -> Workflow:
    @node(name="generate_and_validate", rerun_on_resume=True)
    async def generate_and_validate(
        context: Context,
        node_input: RecipeInput,
    ) -> RecipeOutput:
        generated = await context.run_node(
            leaf_agent,
            node_input=node_input.payload,
        )
        return RecipeOutput(payload=validate_structured_output(generated))

    return Workflow(
        name="backlog_generation",
        input_schema=RecipeInput,
        output_schema=RecipeOutput,
        edges=[("START", generate_and_validate)],
    )
```

Import `BaseAgent` from `google.adk.agents`. Give every recipe explicit timeout and retry settings from its `execution_settings`; tests use fake `BaseAgent` implementations and deterministic schemas.

- [ ] **Step 6: Implement the domain-bounded runner**

`AdkWorkflowRunner` performs exactly this sequence:

1. receive a `NodeDecision` already returned by `position()`;
2. submit `StartNodeAttempt` through `WorkflowDomain.transition()`;
3. create an ADK session keyed by attempt ID, not Project ID;
4. run the recipe outside the domain transaction;
5. validate structured output at the adapter boundary;
6. build `AttemptCompletionContext` from the stored attempt and call the node-specific output adapter;
7. submit it through `WorkflowDomain.transition()`;
8. on external failure, submit `FailNodeAttempt` and return `EXTERNAL_EXECUTION_FAILED`.

Extend `WorkflowDomain.transition()` with three explicit branches before normal positioned dispatch: `StartNodeAttempt` validates a currently available registry-backed decision; `FailNodeAttempt` validates the stored attempt; and any `PositionedRequest` with an attempt binding follows the continuation checks above. A normal human `PositionedRequest` still follows `_guard_failure()` and current availability. No other request can bypass graph availability.

Rename product-workflow session configuration to ADK execution-trace configuration. No domain/repository reader may load it. Deleting that database before a `position()` call must not change the result.

- [ ] **Step 7: Verify adapter boundaries and resume behavior**

Run:

```bash
uv run --frozen pytest tests/workflow/test_node_attempts.py tests/adapters/test_adk_graph_recipes.py tests/adapters/test_adk_workflow_runner.py tests/adapters/test_adk_session_independence.py -q
```

Expected: PASS with fake leaf agents and no provider call.

Run: `rg -n "from (models|repositories)" adapters/adk`

Expected: no output.

Run: `uv run --frozen pyrepo-check --all`

Expected: all checks pass offline.

- [ ] **Step 8: Commit**

```bash
git add workflow adapters/adk utils/runtime_config.py tests/workflow tests/adapters
git commit -m "feat: execute graph nodes through ADK"
```

## Task 16: Cut CLI, API, And Frontend Over To WorkflowDomain

**Files:**
- Create: `services/application.py`
- Create: `cli/workflow_commands.py`
- Modify: `cli/main.py`
- Modify: `services/agent_workbench/command_registry.py`
- Modify: `services/agent_workbench/command_schema.py`
- Modify: `services/agent_workbench/read_projection.py`
- Modify: `utils/api_schemas.py`
- Modify: `api.py`
- Modify: `routers/sprint.py`
- Modify: `frontend/app.js`
- Modify: `frontend/project.js`
- Create: `tests/adapters/test_command_renderer.py`
- Create: `tests/adapters/test_cli_workflow_domain.py`
- Create: `tests/adapters/test_api_workflow_domain.py`
- Create: `tests/test_workflow_position_display.mjs`
- Modify: affected frontend `.mjs` tests

**Interfaces:**
- Consumes: `WorkflowDomain`, closed requests, `WorkflowPosition`, and existing non-routing read projections.
- Produces: task-specific transport handlers, `/api/projects/{project_id}/position`, and one condition-free command renderer registry.

- [ ] **Step 1: Write shared position-fixture adapter tests**

Use the same serialized fixtures for CLI, API, and frontend. Prove:

- every available `required`/`recovery` decision has one rendered action;
- `optional_reentry`, blocked, waiting, invalid, and satisfied decisions are not advertised by `workflow next`;
- zero commands include terminal/waiting/invalid explanation;
- each mutation handler builds the exact request type and copies all guards;
- no adapter imports repositories or checks an FSM/setup status.

```python
def test_workflow_next_renders_required_and_recovery_only() -> None:
    payload = render_workflow_next(position_fixture())
    assert [item["node_id"] for item in payload["commands"]] == [
        "authority.compile",
        "authority.repair",
    ]
```

- [ ] **Step 2: Run adapter tests and verify RED**

Run: `uv run --frozen pytest tests/adapters/test_command_renderer.py tests/adapters/test_cli_workflow_domain.py tests/adapters/test_api_workflow_domain.py -q`

Expected: FAIL because transports still call the old application/session routing.

- [ ] **Step 3: Implement a condition-free command renderer**

```python
@dataclass(frozen=True)
class CommandRenderer:
    request_kind: str
    render: Callable[[WorkflowPosition, NodeDecision], tuple[str, ...]]


class CommandRendererRegistry:
    def __init__(self, renderers: tuple[CommandRenderer, ...]) -> None:
        self._renderers = {item.request_kind: item for item in renderers}

    def command_for(
        self,
        position: WorkflowPosition,
        decision: NodeDecision,
    ) -> tuple[str, ...]:
        return self._renderers[decision.request_kind].render(position, decision)
```

Each renderer owns spelling, flags, help placeholders, and fact-reference extraction only. Every mutating command includes:

```text
--graph-version
--expected-fact-fingerprint
--expected-decision-fingerprint
--idempotency-key
--changed-by
```

Remove `--expected-state` and `--expected-setup-status` from the new command contracts.

- [ ] **Step 4: Replace the huge application routing facade**

`services/application.py` injects one `WorkflowDomain` and existing non-routing read services. Its workflow methods are:

```python
class AgileForgeApplication:
    def position(self, *, project_id: int) -> WorkflowPosition:
        return self._workflow_domain.position(project_id)

    def transition(self, request: TransitionRequest) -> TransitionResult:
        return self._workflow_domain.transition(request)
```

Task-specific CLI/API methods may construct request classes and call `transition()`. They must not query a lower-level helper to decide availability.

- [ ] **Step 5: Cut CLI commands over atomically**

Make `agileforge workflow next --project-id` call `position()` once and render every available required/recovery decision. Add `agileforge workflow position --project-id --include-optional` for full typed orientation. Keep task-specific command names; do not add `workflow transition --action`.

`project create` now means `OpenProjectShell` and requires `--origin greenfield|brownfield`; it no longer accepts a raw spec or greenfield context key. Every later mutating CLI handler constructs a positioned request from explicit guards.

- [ ] **Step 6: Cut API and frontend over in the same task**

Replace `/api/projects/{project_id}/state` with `/api/projects/{project_id}/position`. API mutation schemas carry graph/fact/decision guards. The frontend stores the current decision fingerprint with each action and sends it back unchanged.

Delete frontend phase arrays keyed by `SETUP_REQUIRED`, `VISION_*`, `BACKLOG_*`, `SPRINT_*`, and `SPRINT_COMPLETE`. Render child graph IDs plus available/waiting/blocked/invalid categories. A terminal Project may show an explicit "Start scope extension" control sourced from the optional decision, but it is not shown as unfinished work.

- [ ] **Step 7: Prove runtime imports only the new authority**

Run:

```bash
rg -n "WorkflowService|ReadOnlySessionReader|FSMController|OrchestratorState|fsm_state|setup_status" cli api.py routers frontend services/application.py cli/workflow_commands.py
```

Expected: no production routing reference. Historical response-field compatibility is not retained.

Run:

```bash
uv run --frozen pytest tests/adapters/test_command_renderer.py tests/adapters/test_cli_workflow_domain.py tests/adapters/test_api_workflow_domain.py tests/test_agent_workbench_cli.py tests/test_api_dashboard.py -q
node --test tests/test_workflow_position_display.mjs
```

Expected: PASS.

Run: `uv run --frozen pyrepo-check --all`

Expected: all checks pass.

- [ ] **Step 8: Commit the unpublished runtime cutover**

```bash
git add cli services api.py routers utils/api_schemas.py frontend tests
git commit -m "feat: cut transports over to workflow graph"
```

Do not publish or merge at this point. Task 17 immediately removes the dead runtime and branch-only scaffolding.

## Task 17: Rename The Fresh Schema And Delete Every Legacy Routing Surface

**Files:**
- Rename: `repositories/product.py` -> `repositories/project.py`
- Modify: all live model/repository/service/tool/test imports and Project foreign keys
- Modify: `models/db.py`
- Modify: `models/core.py`
- Modify: `models/specs.py`
- Modify: `models/events.py`
- Modify: `models/agent_workbench.py`
- Modify: `models/authority_curation.py`
- Modify: `models/brownfield.py`
- Modify: `agile_sqlmodel.py`
- Modify: `pyproject.toml`
- Modify: `config/models.test.yaml`
- Modify: `tests/test_model_config_env.py`
- Modify: `README.md`
- Modify: `docs/agent-cli-manual.md`
- Delete: `orchestrator_agent/`
- Delete: `services/workflow.py`
- Delete: `repositories/session.py`
- Delete: `services/agent_workbench/session_reader.py`
- Delete: `services/agent_workbench/application.py`
- Delete: `services/orchestrator_context_service.py`
- Delete: `services/orchestrator_query_service.py`
- Delete: `services/phases/workflow_state.py`
- Delete: `tools/orchestrator_tools.py`
- Delete: `db/migrations.py`
- Delete: session/FSM/orchestrator-only scripts and tests listed below
- Create: `tests/workflow/test_legacy_runtime_absent.py`
- Create: `tests/workflow/test_fresh_project_schema.py`

**Interfaces:**
- Consumes: production callers already switched to `WorkflowDomain` in Task 16.
- Produces: final Project-named fresh schema and a tree with no old routing authority or compatibility package.

- [ ] **Step 1: Write hard-break absence tests**

```python
def test_legacy_runtime_modules_are_absent() -> None:
    assert importlib.util.find_spec("orchestrator_agent") is None
    assert importlib.util.find_spec("services.workflow") is None
    assert importlib.util.find_spec("repositories.session") is None


def test_fresh_schema_uses_project_names(engine: Engine) -> None:
    names = set(inspect(engine).get_table_names())
    assert "projects" in names
    assert "products" not in names
    assert "sessions" not in names
```

Add AST import tests proving domain and transports contain no deleted import. Add literal scans over executable code/config/current docs for `orchestrator_agent`, `FSMController`, `STATE_REGISTRY`, `fsm_state`, `AGILEFORGE_SESSION_DB_URL`, `GreenfieldDiscoveryContext`, and `context_key`.

- [ ] **Step 2: Run absence tests and verify RED**

Run: `uv run --frozen pytest tests/workflow/test_legacy_runtime_absent.py tests/workflow/test_fresh_project_schema.py -q`

Expected: FAIL because old modules/tables still exist.

- [ ] **Step 3: Rename the canonical aggregate on the fresh schema**

Apply this exact naming cut across remaining live code:

| Old | Final |
|---|---|
| `Product` | `Project` |
| `products` | `projects` |
| `product_id` | `project_id` |
| `ProductRepository` | `ProjectRepository` |
| `ProductTeam` | `ProjectTeam` |
| `ProductPersona` | `ProjectPersona` |
| `repositories/product.py` | `repositories/project.py` |

Rename SQL table columns and every foreign-key target because the database is recreated. Preserve domain terms such as product vision only where they describe the artifact, not the aggregate identity. Remove the temporary `products.product_id` target from `models/workflow.py`.

- [ ] **Step 4: Remove old database migration interpretation**

Delete `db/migrations.py` and migration-only tests. `models/db.py` must create the current SQLModel metadata and validate foreign keys; it must not inspect or mutate old workflow/session/FSM schema. Keep backup tooling only if it operates on the fresh current database and has non-migration tests.

- [ ] **Step 5: Delete obsolete runtime modules and pre-Project tables**

Delete the complete `orchestrator_agent/` tree, old workflow/session services, old application facade, FSM/session state projection, orchestrator context/query services, and root orchestrator tools.

Delete these pre-Project models and APIs from the remaining agent-workbench modules:

- `GreenfieldDiscoveryContext` and every greenfield `context_key` artifact/decision table;
- setup status/session fields used only for routing;
- setup retry code that reconstructs routing state;
- product-workflow session doctor/schema requirements.

Delete scripts whose only purpose is session/FSM operation: `scripts/delete_session_stories.py`, `scripts/inspect_session_stories.py`, and `scripts/verify_fsm_tools.py`.

- [ ] **Step 6: Delete obsolete tests but preserve moved business coverage**

Delete tests for the deleted runtime:

- `tests/test_fsm_controller.py`;
- `tests/test_fsm_story_transitions.py`;
- `tests/test_workflow_session_bootstrap.py`;
- `tests/test_agent_workbench_session_reader.py`;
- `tests/test_orchestrator_context_service.py`;
- `tests/test_orchestrator_query_service.py`;
- `tests/test_orchestrator_runtime_import_boundary.py`;
- `tests/test_orchestrator_tools.py`;
- `tests/test_orchestrator_tools_unittest.py`;
- `tests/test_phase_workflow_state.py`;
- `tests/test_select_project_hydration.py` where assertions are session-only;
- `tests/test_state_reconstruction.py` where assertions reconstruct session state.

If one of those files contains durable business assertions, move that assertion to the relevant `tests/workflow/` or service-contract test before deleting the file.

- [ ] **Step 7: Clean configuration, packaging, and current documentation**

Remove `orchestrator_agent` from setuptools packages, coverage sources, Ruff per-file ignores, config roles, and current documentation. Remove the test `orchestrator` model role because no such agent remains. Add focused production roles only for retained leaf recipes.

Update `README.md` and `docs/agent-cli-manual.md` to describe Project Shell, `workflow position`, guarded task-specific commands, and operator-led repository onboarding. Historical specs/plans may retain old names as history.

- [ ] **Step 8: Run hard-break scans**

Run:

```bash
rg -n "orchestrator_agent|FSMController|STATE_REGISTRY|fsm_state|AGILEFORGE_SESSION_DB_URL|GreenfieldDiscoveryContext|context_key" --glob '!docs/superpowers/specs/**' --glob '!docs/superpowers/plans/**' --glob '!artifacts/**' .
```

Expected: no executable code, configuration, test, current operator guide, or API document match.

Run:

```bash
rg -n "\bProduct\b|\bproduct_id\b|products\.product_id|repositories\.product" models repositories workflow adapters services cli api.py routers tools utils tests
```

Expected: no match for the old aggregate identity.

- [ ] **Step 9: Verify fresh schema and full gate**

Run: `uv run --frozen pytest tests/workflow/test_legacy_runtime_absent.py tests/workflow/test_fresh_project_schema.py -q`

Expected: PASS.

Run: `uv run --frozen pyrepo-check --all`

Expected: Ruff, annotations, `ty`, Bandit, and the rewritten default suite all pass offline.

- [ ] **Step 10: Commit the hard break**

```bash
git add -A
git commit -m "refactor: remove legacy workflow runtime"
```

## Task 18: Prepare The Operator-Led Three-Repository Acceptance Checklist

**Files:**
- Create: `docs/testing/workflow-graph-acceptance-checklist.md`
- Create: `tests/test_workflow_acceptance_document.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: final CLI contracts and fresh-schema behavior after Task 17.
- Produces: a manual checklist and evidence template for the Operator's caRtola, ASA, and MyFinance runs. It does not execute or modify those repositories.

- [ ] **Step 1: Write a documentation-contract test**

```python
from pathlib import Path


def test_acceptance_checklist_has_only_the_selected_repositories() -> None:
    text = Path("docs/testing/workflow-graph-acceptance-checklist.md").read_text(
        encoding="utf-8"
    )
    assert "/Users/aaat/projects/caRtola" in text
    assert "/Users/aaat/projects/asa-deep-process-control-experiments" in text
    assert "/Users/aaat/myfinance" in text
    assert "Statement Streams and Coverage" in text
    assert "Operator runs every command" in text
    assert "synthetic evidence only" in text
```

- [ ] **Step 2: Run the documentation test and verify RED**

Run: `uv run --frozen pytest tests/test_workflow_acceptance_document.py -q`

Expected: FAIL because the checklist does not exist.

- [ ] **Step 3: Write the fresh-database preflight**

The checklist must instruct the Operator to:

1. use a new disposable `AGILEFORGE_DB_URL` and run current schema creation;
2. leave the prior AgileForge database untouched rather than migrating it;
3. record AgileForge commit SHA, graph version, model configuration, repository commit/dirty state, and start time;
4. call `agileforge project create --origin brownfield` for one Project Shell;
5. call `agileforge workflow next --project-id <id>` before every mutation;
6. run exactly the task-specific command returned by that position;
7. record the before/after fact and decision fingerprints;
8. stop and report any advertised command that fails its unchanged guards.

Use literal `<id>` and `<returned-command>` markers only in the operator document; the implementation plan itself has already defined every command contract.

- [ ] **Step 4: Add caRtola and ASA acceptance sections**

For each repository, the Operator runs:

```text
Project Shell
repository baseline
complete Git-aware inventory
initial spec curation
human initial-spec decision
initial scope registration
authority compile
authority review
human authority decision
position capture
process restart
position recapture
```

The checklist requires evidence that inventory was complete and not silently truncated, accepted authority ID/hash remained the same after restart, and position/fact fingerprint remained reproducible. It does not ask Codex to create branches or edit either repository.

- [ ] **Step 5: Add the MyFinance real-feature section**

State the boundary verbatim:

```text
"Statement Streams and Coverage" is the real feature supplied by the Operator to test AgileForge. AgileForge must guide the work through accepted authority, backlog, roadmap/story, sprint planning, task execution, review, sprint close, and post-sprint triage. Operator runs every command and owns all MyFinance changes.
```

Require synthetic evidence only and an isolated MyFinance test environment. The checklist records whether AgileForge:

- discovered the current MyFinance repository and approved product context;
- produced a backlog consistent with the approved Statement Stream/Coverage language;
- created reviewable roadmap, story, sprint, and task facts;
- rejected stale commands after any Project fact changed;
- preserved position across AgileForge process and ADK-session deletion;
- reached post-sprint triage without a session/FSM repair command.

Do not prescribe MyFinance code changes, create a MyFinance worktree, or dispatch subagents. The feature is operator-run acceptance input.

- [ ] **Step 6: Add the evidence template and stop boundary**

The evidence template contains:

```yaml
repository_name: ""
repository_path: ""
repository_commit: ""
repository_dirty: false
agileforge_commit: ""
project_id: 0
graph_versions: []
commands: []
fact_fingerprints: []
decision_fingerprints: []
authority_ids: []
authority_hashes: []
model_ids: []
verification_commands: []
verification_results: []
final_position: {}
observed_failures: []
```

The implementation worker stops after handing this checklist to the Operator. They do not claim the three-repository acceptance passed. Execution resumes only after the Operator returns results or a concrete failure.

- [ ] **Step 7: Verify and commit**

Run: `uv run --frozen pytest tests/test_workflow_acceptance_document.py -q`

Expected: PASS.

Run: `uv run --frozen pyrepo-check --all`

Expected: all checks pass.

```bash
git add docs/testing/workflow-graph-acceptance-checklist.md README.md tests/test_workflow_acceptance_document.py
git commit -m "docs: add workflow graph acceptance checklist"
```

## Task 19: Close Operator Findings And Run Final Verification

**Files:**
- Create: `tests/workflow/test_end_to_end_lifecycle.py`
- Modify: files implicated by concrete Operator acceptance failures only
- Modify: `CONTEXT.md`
- Modify: `docs/superpowers/specs/2026-08-02-domain-workflow-graph-hard-break-design.md`
- Create: `docs/testing/results/workflow-graph-acceptance.md`

**Interfaces:**
- Consumes: Operator evidence from Task 18 and the complete final runtime.
- Produces: fixed regressions for every confirmed AgileForge failure, one offline full-lifecycle test, final verification evidence, and design status update.

- [ ] **Step 1: Triage returned evidence without inferring missing results**

For every `observed_failures` entry, require the exact repository, before position, command, request guards, error, after position, and database facts. Use `superpowers:systematic-debugging`; add one failing regression that reproduces the concrete AgileForge behavior before changing production code. Do not change code for a suspected issue that the evidence does not reproduce.

- [ ] **Step 2: Add one complete offline lifecycle regression**

Use in-repository synthetic artifacts and fake ADK recipes. The test must traverse:

```text
brownfield Project Shell
baseline and inventory
reviewed initial spec
initial registration
accepted authority
vision and backlog
roadmap and stories
sprint plan and start
task and story completion
sprint review and close
post-sprint triage
terminal position with optional scope extension
extension discovery and reviewed amendment
replacement accepted authority and reconciliation
terminal position with a new optional extension
```

At three checkpoints, close every session/repository object and construct a new `WorkflowDomain` from the same fresh database. Assert stable fact fingerprints when facts do not change. Delete the ADK trace database before one checkpoint. Make no provider call.

- [ ] **Step 3: Run focused regression tests**

Run: `uv run --frozen pytest tests/workflow/test_end_to_end_lifecycle.py tests/workflow/test_issue_193_regression.py -q`

Expected: PASS.

- [ ] **Step 4: Run final architecture scans**

Run:

```bash
rg -n "orchestrator_agent|FSMController|STATE_REGISTRY|fsm_state|AGILEFORGE_SESSION_DB_URL|GreenfieldDiscoveryContext|context_key" --glob '!docs/superpowers/specs/**' --glob '!docs/superpowers/plans/**' --glob '!docs/testing/results/**' --glob '!artifacts/**' .
```

Expected: no active-runtime/current-guide match.

Run:

```bash
rg -n "from (cli|api|adapters)|import (cli|api|adapters)" workflow
```

Expected: no output.

Run:

```bash
rg -n "from (models|repositories)|import (models|repositories)" adapters cli frontend
```

Expected: no adapter repository bypass. Python-based CLI may import public workflow contracts and `services.application` only.

- [ ] **Step 5: Run the complete verification gate**

Run: `git diff --check`

Expected: no whitespace errors.

Run: `uv run --frozen pyrepo-check --all`

Expected: Ruff, annotations, `ty`, Bandit, and default offline pytest all pass.

Run explicit frontend tests if they are not included by `pyrepo-check`:

```bash
node --test tests/*.mjs
```

Expected: all frontend contract tests pass.

- [ ] **Step 6: Record only confirmed acceptance results**

Create `docs/testing/results/workflow-graph-acceptance.md` from the Operator's evidence and record the evidence timestamp inside the document. Mark caRtola, ASA, and MyFinance independently as `passed`, `failed`, or `not_run`. Do not translate missing evidence into success.

Update `CONTEXT.md` and the design status only after all required implementation checks pass. If any repository remains `failed` or `not_run`, state that clearly and keep final acceptance open.

- [ ] **Step 7: Request code review and close the branch**

Use `superpowers:requesting-code-review`. Resolve factual findings with new failing tests. Re-run Step 5 after the last change. Then use `superpowers:finishing-a-development-branch` to present merge/push options; do not publish without the Operator's choice.

- [ ] **Step 8: Commit final verified state**

```bash
git add workflow adapters cli services models repositories routers utils frontend tests CONTEXT.md docs
git commit -m "test: verify domain workflow graph cutover"
```

## Spec Coverage Trace

| Approved requirement | Implemented by |
|---|---|
| Two-method deep interface | Tasks 2, 6 |
| Full fact and decision fingerprints | Tasks 2, 3, 6 |
| Hierarchical graph, parallel nodes, joins | Tasks 3, 11 |
| Typed durable Project facts | Tasks 4, 5 |
| Project Shell and one discovery per Project | Tasks 6, 7, 8 |
| Canonical initial/amendment content | Tasks 7, 13 |
| One-shot initial registration | Task 7 |
| Accepted Authority unlock | Task 9 |
| Vision/backlog/planning/execution | Tasks 10, 11, 12 |
| Optional scope re-entry and issue 193 | Task 13 |
| Durable ADK attempts and at-least-once boundary | Task 15 |
| CLI/API/frontend adapters only | Task 16 |
| Fresh schema and Project naming | Task 17 |
| Delete orchestrator/FSM/session routing | Tasks 14, 17 |
| Git-aware complete inventory | Task 8 |
| Production/test model policy and offline tests | Tasks 1, 15, 17 |
| caRtola, ASA, MyFinance proof | Tasks 18, 19, Operator-run |
| No typing suppressions; full quality gate | Every task; final Task 19 |

## Execution Handoff

Use one of these approaches after the plan is approved:

1. **Subagent-Driven (recommended):** use `superpowers:subagent-driven-development` to implement AgileForge tasks with a fresh worker and two-stage review per task. This does not dispatch agents into caRtola, ASA, or MyFinance.
2. **Inline Execution:** use `superpowers:executing-plans` in this task, execute AgileForge tasks in checkpoints, and stop at Task 18 for the Operator's repository runs.
