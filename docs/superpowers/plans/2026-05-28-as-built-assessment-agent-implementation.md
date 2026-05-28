# As-Built Assessment Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a guarded, inspectable As-Built Assessment phase that compares accepted authority against bounded repository evidence before backlog generation, then passes the structured assessment into the Backlog Primer as advisory brownfield context.
**Architecture:** A host-side `AsBuiltAssessmentRunner` builds a deterministic evidence pack, invokes a Google ADK `as_built_assessor` agent with strict Pydantic schemas, caches the resulting `agileforge.as_built_assessment.v1` JSON in workflow state, records an idempotent workflow event, and exposes the cache through a first-class `as_built_assessment` Backlog Primer input field.
**Tech Stack:** Python 3.12, Pydantic v2, SQLModel, Google ADK, LiteLLM/OpenRouter, argparse CLI, pytest, existing AgileForge workbench envelopes and fingerprints.

---

## Non-Negotiable Scope

- Do not add database tables.
- Do not change Sprint Planning, Sprint execution, story close, or sprint close.
- Do not make OpenSpec a workflow dependency.
- Do not execute `/opsx:*` commands.
- Do not add agent-side shell, CodeGraph, or file-system tools in Phase 1.
- Do not infer that absent evidence means missing implementation.
- Do not remove or rewrite the existing `agileforge evidence collect` command; it remains compatibility evidence.
- Do not allow backlog generation to silently ignore a fresh `as_built_assessment_cached` value.

## Command Contract

Implement this command:

```sh
agileforge as-built assess \
  --project-id 2 \
  --repo-path /Users/aaat/projects/caRtola \
  --spec-mode unknown \
  --idempotency-key as-built-cartola-smoke-001
```

Optional flags:

```sh
--spec-file /Users/aaat/projects/caRtola/specs/spec.json
--user-input "Assess caRtola before regenerating backlog."
```

`--spec-mode` accepts exactly:

- `current_state`
- `desired_state`
- `proposed_change`
- `unknown`

Default `--spec-mode` is `unknown`.

The command mutates only workflow state and the workflow event log. It must not create, save, delete, reorder, or supersede Product Backlog rows.

Successful envelope data shape:

```json
{
  "project_id": 2,
  "assessment_fingerprint": "sha256:assessment",
  "evidence_pack_fingerprint": "sha256:pack",
  "stored_state_key": "as_built_assessment_cached",
  "stored_meta_key": "as_built_assessment_cache_meta",
  "idempotent_replay": false,
  "assessment": {}
}
```

## Workflow State Contract

Store these workflow state keys:

```json
{
  "as_built_assessment_cached": "{canonical JSON string}",
  "as_built_assessment_cache_meta": {
    "schema_version": "agileforge.as_built_assessment.v1",
    "agent_version": "agileforge.as_built_assessor.v1",
    "evidence_pack_builder_version": "agileforge.as_built_pack_builder.v1",
    "authority_fingerprint": "sha256:authority",
    "repo_git_commit": "gitsha-or-null",
    "repo_dirty": false,
    "evidence_pack_fingerprint": "sha256:pack",
    "assessment_fingerprint": "sha256:assessment",
    "generated_at": "2026-05-28T12:00:00Z"
  }
}
```

Freshness rules for backlog input:

- If `as_built_assessment_cached` is absent, pass `"NO_AS_BUILT_ASSESSMENT"`.
- If `as_built_assessment_cache_meta` is absent, pass `"NO_AS_BUILT_ASSESSMENT"`.
- If the cached assessment fails schema validation, pass `"NO_AS_BUILT_ASSESSMENT"`.
- If assessment metadata and cache metadata disagree on `agent_version`, `evidence_pack_builder_version`, `authority_fingerprint`, `repo_git_commit`, `repo_dirty`, or `evidence_pack_fingerprint`, pass `"NO_AS_BUILT_ASSESSMENT"`.
- If cache metadata `assessment_fingerprint` does not match the canonical hash of `as_built_assessment_cached`, pass `"NO_AS_BUILT_ASSESSMENT"`.
- If `evidence_pack_builder_version` differs from the current constant, pass `"NO_AS_BUILT_ASSESSMENT"`.

The backlog runtime cannot prove the live repository has not changed unless an assessment command is rerun. It must still prevent internally inconsistent or version-stale caches from reaching the Backlog Primer.

## Backlog Adapter Mapping

Add a Backlog Primer input field:

```python
as_built_assessment: str
```

The value is raw canonical `agileforge.as_built_assessment.v1` JSON, or the literal `"NO_AS_BUILT_ASSESSMENT"`.

Status mapping:

| Assessment status | Backlog behavior |
| --- | --- |
| `observed` | Do not create duplicate implementation work unless user input explicitly asks for replacement or redesign. |
| `observed_with_missing_evidence` | Scope work to tests, validation, docs, or hardening. |
| `contradicted` | Create a PO-visible conflict/remediation candidate. |
| `not_observed` | Create product work only when accepted authority requires the capability and limitations are visible. |
| `unclear` | Create discovery or PO-review work, not guessed implementation. |

If both `as_built_assessment` and `implementation_evidence` are present, `as_built_assessment` is the primary brownfield advisory input. `implementation_evidence` is supporting compatibility context only.

## Implementation Tasks

- [ ] 1. Add failing schema tests for the As-Built agent contracts.
- [ ] 2. Implement `orchestrator_agent/agent_tools/as_built_assessor` schemas, instructions, theory document, and ADK agent wiring.
- [ ] 3. Add host-side evidence pack builder tests for caRtola-shaped authority and bounded snippet collection.
- [ ] 4. Implement host-side evidence pack builder and cache/fingerprint helpers.
- [ ] 5. Add failing runner, CLI, command schema, and idempotency tests.
- [ ] 6. Implement `agileforge as-built assess` runner, facade method, CLI parser, command registry metadata, and workflow event recording.
- [ ] 7. Add failing Backlog Primer adapter tests.
- [ ] 8. Implement Backlog Primer input schema, runtime cache adapter, and instruction updates.
- [ ] 9. Run targeted tests, then full test suite.
- [ ] 10. Run caRtola smoke command and inspect the artifact without running backlog generation automatically.

---

## Task 1: Add Failing Schema Tests

Use `superpowers:test-driven-development` for this task.

Create `/Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/tests/test_as_built_assessor_schemas.py`.

The test file must validate:

- strict output schema rejects extra fields
- all assessment statuses are accepted
- `is_complete` can be true with low-confidence `unclear`
- cache metadata includes `agent_version` and `evidence_pack_builder_version`
- empty `authority_targets` requires an explicit no-targets limitation

Add this concrete test content:

```python
"""Tests for As-Built Assessment agent schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator_agent.agent_tools.as_built_assessor.schemes import (
    AGENT_VERSION,
    ASSESSMENT_SCHEMA_VERSION,
    EVIDENCE_PACK_BUILDER_VERSION,
    AsBuiltAssessment,
    AsBuiltAssessmentCacheMeta,
    AsBuiltAssessorInput,
    AuthorityTarget,
    CapabilityAssessment,
    EvidencePack,
    EvidenceSnippet,
    OpenSpecContext,
    OriginalSpecContext,
    RepoSnapshot,
)


def _repo_snapshot() -> RepoSnapshot:
    return RepoSnapshot(path="/repo", git_commit="abc123", dirty=False)


def _evidence_pack(authority_targets: list[AuthorityTarget]) -> EvidencePack:
    return EvidencePack(
        schema_version="agileforge.as_built_evidence_pack.v1",
        builder_version=EVIDENCE_PACK_BUILDER_VERSION,
        authority_fingerprint="sha256:authority",
        evidence_pack_fingerprint="sha256:pack",
        generated_at="2026-05-28T12:00:00Z",
        repo_snapshot=_repo_snapshot(),
        warnings=[],
        file_manifest_summary={"total_files": 3},
        authority_targets=authority_targets,
        source_snippets=[],
        test_snippets=[],
        doc_snippets=[],
        cli_observations=[],
        search_observations=[],
        limitations=[],
    )


def test_output_schema_accepts_all_statuses() -> None:
    statuses = [
        "observed",
        "observed_with_missing_evidence",
        "contradicted",
        "not_observed",
        "unclear",
    ]

    assessment = AsBuiltAssessment(
        schema_version=ASSESSMENT_SCHEMA_VERSION,
        project_id=2,
        assessment_id="as-built-2-abc",
        agent_version=AGENT_VERSION,
        evidence_pack_builder_version=EVIDENCE_PACK_BUILDER_VERSION,
        authority_fingerprint="sha256:authority",
        evidence_pack_fingerprint="sha256:pack",
        generated_at="2026-05-28T12:05:00Z",
        assessment_summary="Assessment completed.",
        repo_snapshot=_repo_snapshot(),
        capability_assessments=[
            CapabilityAssessment(
                authority_ref=f"REQ.status-{index}",
                invariant_refs=[f"INV-{index:04d}"],
                capability_title=f"Capability {index}",
                status=status,
                confidence="low",
                evidence=[],
                limitations=["Tests were not executed."],
                recommended_backlog_treatment="po_review_required",
                reasoning="Bounded evidence supports only a conservative assessment.",
            )
            for index, status in enumerate(statuses)
        ],
        cross_cutting_findings=[],
        open_questions=[],
        is_complete=True,
        clarifying_questions=[],
    )

    assert assessment.is_complete is True
    assert [item.status for item in assessment.capability_assessments] == statuses


def test_output_schema_rejects_extra_fields() -> None:
    payload = {
        "schema_version": ASSESSMENT_SCHEMA_VERSION,
        "project_id": 2,
        "assessment_id": "as-built-2-abc",
        "agent_version": AGENT_VERSION,
        "evidence_pack_builder_version": EVIDENCE_PACK_BUILDER_VERSION,
        "authority_fingerprint": "sha256:authority",
        "evidence_pack_fingerprint": "sha256:pack",
        "generated_at": "2026-05-28T12:05:00Z",
        "assessment_summary": "Assessment completed.",
        "repo_snapshot": _repo_snapshot().model_dump(mode="json"),
        "capability_assessments": [],
        "cross_cutting_findings": [],
        "open_questions": [],
        "is_complete": True,
        "clarifying_questions": [],
        "unexpected": "blocked",
    }

    with pytest.raises(ValidationError):
        AsBuiltAssessment.model_validate(payload)


def test_input_schema_accepts_unknown_spec_mode_and_no_history() -> None:
    target = AuthorityTarget(
        authority_ref="REQ.live-squad-recommendation",
        invariant_refs=["INV-a4b296c058e88663"],
        title="Live squad recommendation",
        invariant_type="STATE_TRANSITION",
        source_requirement_id="REQ.live-squad-recommendation",
        terms=["live squad recommendation", "market is open"],
        parameters={"state": "live recommendation run"},
    )

    parsed = AsBuiltAssessorInput(
        project_id=2,
        assessment_id="as-built-2-abc",
        compiled_authority='{"invariants":[]}',
        original_spec=OriginalSpecContext(
            spec_mode="unknown",
            json="{}",
            markdown="",
        ),
        repo_evidence_pack=_evidence_pack([target]),
        openspec_context=OpenSpecContext(
            present=False,
            spec_summaries=[],
            change_summaries=[],
        ),
        prior_as_built_assessment="NO_HISTORY",
        user_input="",
    )

    assert parsed.original_spec.spec_mode == "unknown"
    assert parsed.repo_evidence_pack.authority_targets[0].authority_ref == (
        "REQ.live-squad-recommendation"
    )


def test_empty_authority_targets_requires_explicit_limitation() -> None:
    pack = _evidence_pack([])
    assert pack.has_no_targets_limitation() is False

    pack_with_limitation = pack.model_copy(
        update={"limitations": ["No authority targets were extracted."]}
    )
    assert pack_with_limitation.has_no_targets_limitation() is True


def test_cache_meta_contains_required_freshness_fields() -> None:
    meta = AsBuiltAssessmentCacheMeta(
        schema_version=ASSESSMENT_SCHEMA_VERSION,
        agent_version=AGENT_VERSION,
        evidence_pack_builder_version=EVIDENCE_PACK_BUILDER_VERSION,
        authority_fingerprint="sha256:authority",
        repo_git_commit="abc123",
        repo_dirty=False,
        evidence_pack_fingerprint="sha256:pack",
        assessment_fingerprint="sha256:assessment",
        generated_at="2026-05-28T12:05:00Z",
    )

    assert meta.agent_version == AGENT_VERSION
    assert meta.evidence_pack_builder_version == EVIDENCE_PACK_BUILDER_VERSION
```

Run:

```sh
uv run pytest tests/test_as_built_assessor_schemas.py -q
```

Expected first result: import failure because `as_built_assessor` does not exist.

## Task 2: Implement Agent Schemas And ADK Wiring

Use `superpowers:test-driven-development` for this task. Implement only enough to pass Task 1 plus agent import checks.

Create directory:

```text
/Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/orchestrator_agent/agent_tools/as_built_assessor/
```

Create `/Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/orchestrator_agent/agent_tools/as_built_assessor/__init__.py` as an empty package marker.

Create `/Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/orchestrator_agent/agent_tools/as_built_assessor/schemes.py` with these model groups:

- constants:
  - `ASSESSMENT_SCHEMA_VERSION = "agileforge.as_built_assessment.v1"`
  - `EVIDENCE_PACK_SCHEMA_VERSION = "agileforge.as_built_evidence_pack.v1"`
  - `AGENT_VERSION = "agileforge.as_built_assessor.v1"`
  - `EVIDENCE_PACK_BUILDER_VERSION = "agileforge.as_built_pack_builder.v1"`
- enum literals:
  - `SpecMode = Literal["current_state", "desired_state", "proposed_change", "unknown"]`
  - `AssessmentStatus = Literal["observed", "observed_with_missing_evidence", "contradicted", "not_observed", "unclear"]`
  - `AssessmentConfidence = Literal["high", "medium", "low"]`
  - `BacklogTreatment = Literal["skip_new_implementation", "create_verification_item", "create_hardening_item", "create_authority_conflict_item", "create_discovery_item", "create_product_item", "po_review_required"]`
  - `EvidenceKind = Literal["source", "test", "doc", "config", "cli", "search"]`
- input models:
  - `RepoSnapshot`
  - `EvidenceWarning`
  - `AuthorityTarget`
  - `EvidenceSnippet`
  - `CliObservation`
  - `SearchObservation`
  - `OriginalSpecContext`
  - `OpenSpecContext`
  - `EvidencePack`
  - `AsBuiltAssessorInput`
- output/cache models:
  - `CapabilityEvidence`
  - `CapabilityAssessment`
  - `AsBuiltAssessment`
  - `AsBuiltAssessmentCacheMeta`

Use `ConfigDict(extra="forbid")` on output/cache models and on bounded pack models. Use `ConfigDict(extra="ignore")` only on `AsBuiltAssessorInput`.

Required helpers in `schemes.py`:

```python
def has_no_targets_limitation(self) -> bool:
    return any(
        "no authority targets" in limitation.lower()
        for limitation in self.limitations
    )
```

Attach that method to `EvidencePack`.

Create `/Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/orchestrator_agent/agent_tools/as_built_assessor/instructions.txt`.

Instruction content must include these exact rule sentences:

```text
Repo evidence is required for observed claims.
Missing repo evidence is not proof of missing implementation.
A spec describing current behavior is not backlog scope.
A spec describing desired behavior is not proof that work is missing.
Do not recommend duplicate implementation work for status observed.
Return JSON only.
```

Create `/Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/orchestrator_agent/agent_tools/as_built_assessor/scrum_theory_as_built_assessor.md` explaining:

- the assessment is advisory input before Product Backlog generation
- the Product Owner still owns backlog decisions
- a Sprint Backlog is not created by this agent
- `observed` suppresses duplicate implementation candidates
- `unclear` routes to discovery or PO review

Create `/Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/orchestrator_agent/agent_tools/as_built_assessor/agent.py` by following the existing `roadmap_builder/agent.py` style, with:

- model key: `as_built_assessor`
- runner identity added in Task 6
- max token function added in this task:
  - `utils/runtime_config.py`: `get_as_built_assessor_max_tokens(default: int = 8192) -> int`
- model config added in this task:
  - `/Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/config/models.yaml`
  - `as_built_assessor: "openrouter/deepseek/deepseek-v4-pro"`

Run:

```sh
uv run pytest tests/test_as_built_assessor_schemas.py -q
```

Expected result: all tests in that file pass.

## Task 3: Add Failing Evidence Pack Builder Tests

Use `superpowers:test-driven-development` for this task.

Create `/Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/tests/test_as_built_assessment.py`.

Add tests for:

- caRtola-shaped `compiled_authority.invariants[]` with no `items[]` produces non-empty `authority_targets`
- target includes `target id = INV-*`, `source_requirement_id = parameters.source_item_id`, `invariant_type`, and flattened behavioral terms
- exact boundary search does not match substrings
- skip `.codegraph`, sockets, lock files, databases, binary/image files, and files above 500 KiB
- pack budgets emit warnings instead of failing
- empty target extraction emits a no-targets limitation and a warning

Use this caRtola-shaped authority fixture:

```python
CARTOLA_AUTHORITY = {
    "invariants": [
        {
            "id": "INV-a4b296c058e88663",
            "type": "STATE_TRANSITION",
            "parameters": {
                "source_item_id": "REQ.live-squad-recommendation",
                "source_level": "MUST",
                "state": "live recommendation run",
                "trigger": "market is open",
                "outcome": "exactly one operator-facing recommended squad",
            },
        },
        {
            "id": "INV-ffe2e17832c41874",
            "type": "DATA_CONTRACT",
            "parameters": {
                "source_item_id": "REQ.legal-roster",
                "subject": "selected live squad",
                "fields": ["roster_size_12", "one_tecnico", "eleven_non_tecnico"],
                "rule": "must satisfy Cartola roster rules",
            },
        },
    ],
    "source_map": [
        {
            "source_item_id": "REQ.live-squad-recommendation",
            "excerpt": "Recommend a live squad while the market is open.",
        }
    ],
    "requirement_candidates": [],
    "authority_mappings": [],
}
```

Run:

```sh
uv run pytest tests/test_as_built_assessment.py -q
```

Expected first result: import failure because `services.agent_workbench.as_built_assessment` does not exist.

## Task 4: Implement Host Evidence Pack Builder

Use `superpowers:test-driven-development` for this task.

Create `/Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/services/agent_workbench/as_built_assessment.py`.

Public constants:

```python
AS_BUILT_ASSESS_COMMAND = "agileforge as-built assess"
AS_BUILT_ASSESSMENT_STATE_KEY = "as_built_assessment_cached"
AS_BUILT_ASSESSMENT_META_STATE_KEY = "as_built_assessment_cache_meta"
MAX_SCAN_BYTES = 500 * 1024
MAX_AUTHORITY_TARGETS = 150
MAX_SNIPPETS_PER_TARGET = 5
MAX_SNIPPET_LINES = 40
MAX_SNIPPET_BYTES = 8 * 1024
MAX_PACK_BYTES = 750 * 1024
MAX_FILE_MANIFEST_ENTRIES = 300
```

Implement these public functions:

```python
def utc_now_iso() -> str
def assessment_fingerprint(assessment: AsBuiltAssessment) -> str
def cache_meta_for_assessment(assessment: AsBuiltAssessment) -> AsBuiltAssessmentCacheMeta
def cached_assessment_for_backlog(state: dict[str, Any]) -> str
def build_authority_targets(compiled: dict[str, Any]) -> tuple[list[AuthorityTarget], list[EvidenceWarning], list[str]]
def build_evidence_pack(
    *,
    project_id: int,
    authority_fingerprint: str,
    compiled_authority: dict[str, Any],
    repo_path: Path,
    spec_mode: SpecMode,
    spec_file: Path | None,
) -> EvidencePack
```

Implementation rules:

- `build_authority_targets` must inspect `invariants[]` first.
- For each invariant, use:
  - `authority_ref = parameters.source_item_id` when present, otherwise invariant `id`
  - `invariant_refs = [id]`
  - `invariant_type = type`
  - `source_requirement_id = parameters.source_item_id`
  - `terms` from invariant id, source requirement id, invariant type, source map excerpts, and normalized string/list values from `parameters`
- If `invariants[]` is empty, inspect structured `items[]` only as a secondary compatibility path.
- If no targets are extracted, return an evidence pack with `limitations=["No authority targets were extracted."]` and a warning code `AS_BUILT_NO_AUTHORITY_TARGETS`.
- Do not classify implementation state in the builder.
- Search must use exact token boundary matching for IDs and safe lowercase substring matching for normalized domain terms.
- Snippets must include only bounded text around matches.
- Pack size above `MAX_PACK_BYTES` must truncate lower-priority snippets and add warning code `AS_BUILT_PACK_TRUNCATED`.
- Dirty git repo must add warning code `AS_BUILT_REPO_DIRTY`.
- Unsupported files must be skipped with warning counts, not hard failures.

Reuse ideas from `services/agent_workbench/evidence_collect.py`, but do not make the new builder depend on the old `ReconciliationReport` schema.

Run:

```sh
uv run pytest tests/test_as_built_assessment.py tests/test_as_built_assessor_schemas.py -q
```

Expected result: both files pass.

## Task 5: Add Failing Runner, CLI, Registry, And Idempotency Tests

Use `superpowers:test-driven-development` for this task.

Extend these files:

- `/Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/tests/test_as_built_assessment.py`
- `/Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/tests/test_agent_workbench_command_schema.py`
- `/Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/tests/test_agent_workbench_cli.py`
- `/Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/tests/test_agent_workbench_phase1_integration.py`
- `/Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/tests/test_agent_workbench_application.py`

Required test assertions:

- command registry includes `agileforge as-built assess`
- command schema marks it as `mutates: true`
- command schema has required inputs `["project_id", "repo_path", "idempotency_key"]`
- command schema has optional inputs `["spec_file", "spec_mode", "user_input"]`
- command schema exposes errors:
  - `PROJECT_NOT_FOUND`
  - `INVALID_COMMAND`
  - `AUTHORITY_NOT_ACCEPTED`
  - `AUTHORITY_NOT_COMPILED`
  - `MUTATION_FAILED`
  - `IDEMPOTENCY_KEY_REUSED`
- CLI parses `as-built assess`
- application facade has `as_built_assess`
- runner caches `as_built_assessment_cached`
- runner caches `as_built_assessment_cache_meta`
- runner records `WorkflowEventType.AS_BUILT_ASSESSED`
- idempotent replay with same request fingerprint returns `idempotent_replay: true`
- idempotency key reuse with different repo/spec/source fingerprint returns `IDEMPOTENCY_KEY_REUSED`
- request fingerprint includes the evidence pack fingerprint, spec mode, spec file content hash when provided, authority fingerprint, repo path, repo git commit, repo dirty flag, `AGENT_VERSION`, and `EVIDENCE_PACK_BUILDER_VERSION`

Monkeypatch the agent invocation in runner tests so tests do not call a provider. The fake invocation must return a schema-valid `AsBuiltAssessment`.

Run:

```sh
uv run pytest \
  tests/test_as_built_assessment.py \
  tests/test_agent_workbench_command_schema.py \
  tests/test_agent_workbench_cli.py \
  tests/test_agent_workbench_phase1_integration.py \
  tests/test_agent_workbench_application.py \
  -q
```

Expected first result: failures because command/facade/event type still need implementation.

## Task 6: Implement Runner, Facade, CLI, Registry, And Event Type

Use `superpowers:test-driven-development` for this task.

Modify `/Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/models/enums.py`:

```python
AS_BUILT_ASSESSED = "as_built_assessed"
```

Modify `/Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/utils/runtime_config.py`:

```python
AS_BUILT_RUNNER_IDENTITY = RunnerIdentity(
    app_name="as_built_assessor",
    user_id="dashboard_as_built",
)


def get_as_built_assessor_max_tokens(default: int = 8192) -> int:
    """Return the max token budget for the as-built assessor."""
    return get_int_env("AS_BUILT_ASSESSOR_MAX_TOKENS", default)
```

In `services/agent_workbench/as_built_assessment.py`, add class `AsBuiltAssessmentRunner` with this constructor and public method signature:

```python
class AsBuiltAssessmentRunner:
    def __init__(
        self,
        *,
        product_repo: _ProductRepository | None = None,
        workflow_service: _WorkflowService | None = None,
        engine: Engine | None = None,
        invoke_agent: _AgentInvoker | None = None,
    ) -> None:
        """Initialize runner dependencies."""

    def assess(
        self,
        *,
        project_id: int,
        repo_path: str,
        spec_file: str | None,
        spec_mode: str,
        user_input: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Assess current implementation state and cache the assessment."""
```

Runner behavior:

1. fail closed when project is missing
2. fail closed when authority is not accepted or compiled
3. fail closed when `repo_path` is absent or not a directory
4. normalize `spec_mode`, defaulting to `unknown`
5. build evidence pack
6. compute request fingerprint
7. perform idempotent replay check against `WorkflowEventType.AS_BUILT_ASSESSED`
8. invoke `as_built_assessor` with `AsBuiltAssessorInput`
9. schema-validate `AsBuiltAssessment`
10. write canonical assessment JSON and cache meta to workflow state
11. record workflow event metadata containing idempotency key, request fingerprint, assessment fingerprint, evidence pack fingerprint, and assessment JSON
12. return standard `{ok, data, warnings, errors}` envelope

Add application protocol and facade method in `/Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/services/agent_workbench/application.py`:

```python
class _AsBuiltAssessmentRunner(Protocol):
    def assess(
        self,
        *,
        project_id: int,
        repo_path: str,
        spec_file: str | None,
        spec_mode: str,
        user_input: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Assess current implementation state and cache it in workflow state."""
```

Add:

```python
def as_built_assess(
    self,
    *,
    project_id: int,
    repo_path: str,
    spec_file: str | None,
    spec_mode: str,
    user_input: str | None,
    idempotency_key: str,
) -> dict[str, Any]:
    return self._get_as_built_runner().assess(
        project_id=project_id,
        repo_path=repo_path,
        spec_file=spec_file,
        spec_mode=spec_mode,
        user_input=user_input,
        idempotency_key=idempotency_key,
    )
```

Add lazy getter `_get_as_built_runner`.

Modify `/Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/services/agent_workbench/command_registry.py` with command metadata:

```python
CommandMetadata(
    name="agileforge as-built assess",
    mutates=True,
    phase="phase_2d",
    requires_idempotency_key=True,
    input_required=("project_id", "repo_path", "idempotency_key"),
    input_optional=("spec_file", "spec_mode", "user_input"),
    errors=(
        ErrorCode.PROJECT_NOT_FOUND.value,
        ErrorCode.INVALID_COMMAND.value,
        ErrorCode.AUTHORITY_NOT_ACCEPTED.value,
        ErrorCode.AUTHORITY_NOT_COMPILED.value,
        ErrorCode.MUTATION_FAILED.value,
        ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
    ),
)
```

Modify `/Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/cli/main.py`:

- add usage examples for `agileforge as-built assess`
- add top-level parser `as-built`
- add subparser `assess`
- add flags:
  - `--project-id` required int
  - `--repo-path` required str
  - `--spec-file` optional str
  - `--spec-mode` optional choices
  - `--user-input` optional str
  - `--idempotency-key` required str
- route to `_as_built_assess`

Run:

```sh
uv run pytest \
  tests/test_as_built_assessment.py \
  tests/test_agent_workbench_command_schema.py \
  tests/test_agent_workbench_cli.py \
  tests/test_agent_workbench_phase1_integration.py \
  tests/test_agent_workbench_application.py \
  -q
```

Expected result: all targeted command/facade/runner tests pass.

## Task 7: Add Failing Backlog Adapter Tests

Use `superpowers:test-driven-development` for this task.

Extend:

- `/Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/tests/test_backlog_primer_agent.py`
- `/Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/tests/test_agent_workbench_backlog_phase.py`

Required assertions:

- Backlog Primer `InputSchema` accepts `as_built_assessment`
- `build_backlog_input_context` passes fresh cached `as_built_assessment_cached`
- missing cache becomes `"NO_AS_BUILT_ASSESSMENT"`
- metadata mismatch becomes `"NO_AS_BUILT_ASSESSMENT"`
- evidence pack fingerprint mismatch becomes `"NO_AS_BUILT_ASSESSMENT"`
- builder version mismatch becomes `"NO_AS_BUILT_ASSESSMENT"`
- `implementation_evidence` still works and defaults to `"NO_EVIDENCE"`
- backlog instructions mention all five statuses:
  - `observed`
  - `observed_with_missing_evidence`
  - `contradicted`
  - `not_observed`
  - `unclear`
- backlog instructions state that `as_built_assessment` supersedes `implementation_evidence` for brownfield backlog generation

Add a test where repo commit and dirty flag are unchanged but `evidence_pack_fingerprint` differs between cache metadata and assessment JSON. That test must assert:

```python
assert context["as_built_assessment"] == "NO_AS_BUILT_ASSESSMENT"
```

Run:

```sh
uv run pytest \
  tests/test_backlog_primer_agent.py \
  tests/test_agent_workbench_backlog_phase.py \
  -q
```

Expected first result: failures because Backlog Primer has no `as_built_assessment` field and runtime has no cache adapter.

## Task 8: Implement Backlog Adapter And Instructions

Use `superpowers:test-driven-development` for this task.

Modify `/Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/orchestrator_agent/agent_tools/backlog_primer/schemes.py`:

```python
as_built_assessment: Annotated[
    str,
    Field(
        description=(
            "Raw agileforge.as_built_assessment.v1 JSON from "
            "as_built_assessment_cached or 'NO_AS_BUILT_ASSESSMENT' when no "
            "fresh assessment cache is available."
        ),
    ),
]
```

Keep `implementation_evidence` unchanged for compatibility.

Modify `/Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/services/backlog_runtime.py`:

- import `cached_assessment_for_backlog` from `services.agent_workbench.as_built_assessment`
- call it in `build_backlog_input_context`
- add key `"as_built_assessment": cached_assessment_for_backlog(state)`

The returned context must include both fields:

```python
{
    "as_built_assessment": cached_assessment_for_backlog(state),
    "implementation_evidence": implementation_evidence or "NO_EVIDENCE",
}
```

Modify `/Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/orchestrator_agent/agent_tools/backlog_primer/instructions.txt`:

- update input schema text to include:
  - `"as_built_assessment": "NO_AS_BUILT_ASSESSMENT | raw agileforge.as_built_assessment.v1 JSON object"`
- add a new section before `IMPLEMENTATION EVIDENCE`:

```text
**AS-BUILT ASSESSMENT:**
* The input field `as_built_assessment` is either `NO_AS_BUILT_ASSESSMENT` or a raw `agileforge.as_built_assessment.v1` JSON object.
* Treat `as_built_assessment` as the primary brownfield advisory source when present.
* It is not Product Backlog authority. It is evidence-aware guidance for Product Owner review.
* For `status=observed`, do not create duplicate implementation work unless user_input explicitly asks for replacement or redesign.
* For `status=observed_with_missing_evidence`, scope work to verification, hardening, tests, docs, or validation.
* For `status=contradicted`, create a PO-visible conflict/remediation candidate.
* For `status=not_observed`, create product work only when accepted authority requires the capability and preserve evidence limitations in `technical_note`.
* For `status=unclear`, create discovery or PO-review work, not guessed implementation.
* If both `as_built_assessment` and `implementation_evidence` are present, `as_built_assessment` supersedes `implementation_evidence` for brownfield backlog generation.
```

Run:

```sh
uv run pytest \
  tests/test_backlog_primer_agent.py \
  tests/test_agent_workbench_backlog_phase.py \
  -q
```

Expected result: targeted backlog tests pass.

## Task 9: Full Verification

Use `superpowers:verification-before-completion` before reporting completion.

Run targeted tests first:

```sh
uv run pytest \
  tests/test_as_built_assessor_schemas.py \
  tests/test_as_built_assessment.py \
  tests/test_backlog_primer_agent.py \
  tests/test_agent_workbench_backlog_phase.py \
  tests/test_agent_workbench_command_schema.py \
  tests/test_agent_workbench_cli.py \
  tests/test_agent_workbench_phase1_integration.py \
  tests/test_agent_workbench_application.py \
  -q
```

Expected result: all selected tests pass.

Run full tests:

```sh
uv run pytest -q
```

Expected result: all repository tests pass, allowing existing warning count to remain unchanged unless a new warning is introduced by this work.

Run static unfinished-marker scan on changed files:

```sh
uv run python - <<'PY'
from pathlib import Path

markers = ["TO" + "DO", "T" + "BD", "stub" + "bed"]
paths = [
    Path("orchestrator_agent/agent_tools/as_built_assessor"),
    Path("services/agent_workbench/as_built_assessment.py"),
    Path("services/backlog_runtime.py"),
    Path("orchestrator_agent/agent_tools/backlog_primer"),
    Path("cli/main.py"),
    Path("services/agent_workbench/command_registry.py"),
    Path("utils/runtime_config.py"),
    Path("config/models.yaml"),
    Path("models/enums.py"),
    Path("tests/test_as_built_assessment.py"),
    Path("tests/test_as_built_assessor_schemas.py"),
    Path("tests/test_agent_workbench_backlog_phase.py"),
    Path("tests/test_backlog_primer_agent.py"),
    Path("tests/test_agent_workbench_command_schema.py"),
    Path("tests/test_agent_workbench_cli.py"),
    Path("tests/test_agent_workbench_phase1_integration.py"),
    Path("tests/test_agent_workbench_application.py"),
]

matches: list[str] = []
for path in paths:
    candidates = path.rglob("*") if path.is_dir() else [path]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        text = candidate.read_text(encoding="utf-8")
        for marker in markers:
            if marker in text:
                matches.append(f"{candidate}:{marker}")

if matches:
    print("\n".join(matches))
    raise SystemExit(1)
PY
```

Expected result: no matches.

## Task 10: caRtola Smoke Check

Do not run backlog generation automatically in this task.

Run from the worktree with explicit DB environment values that point at the intended AgileForge database:

```sh
AGILEFORGE_DB_URL="$AGILEFORGE_DB_URL" \
AGILEFORGE_SESSION_DB_URL="$AGILEFORGE_SESSION_DB_URL" \
uv run --project /Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent \
  python -m cli.main as-built assess \
  --project-id 2 \
  --repo-path /Users/aaat/projects/caRtola \
  --spec-mode unknown \
  --idempotency-key as-built-cartola-smoke-001 \
  > /Users/aaat/projects/agileforge/.worktrees/as-built-assessment-agent/as-built-cartola-smoke.json
```

Expected result:

- top-level `ok: true`, or a provider/runtime failure that does not mutate backlog rows
- `data.stored_state_key == "as_built_assessment_cached"` when successful
- `data.assessment.capability_assessments` is non-empty when authority targets are present
- warnings may include dirty repo and skipped file diagnostics
- no Product Backlog rows are created by this command

Inspect summary:

```sh
uv run python - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("as-built-cartola-smoke.json").read_text())
print(payload["ok"])
data = payload.get("data") or {}
assessment = data.get("assessment") or {}
print(data.get("stored_state_key"))
print(len(assessment.get("capability_assessments", [])))
print([warning.get("code") for warning in payload.get("warnings", [])[:5]])
PY
```

Expected result on successful provider invocation:

```text
True
as_built_assessment_cached
<a positive integer>
<zero or more warning codes>
```

If the provider is unavailable, report the exact error envelope and do not claim the smoke passed.

## Review Checkpoints

After Task 4, use `superpowers:requesting-code-review` for the evidence pack builder because authority extraction is the highest-risk implementation surface.

After Task 8, use `superpowers:requesting-code-review` for the backlog adapter because silent no-op integration is the main product risk.

After Task 10, use `superpowers:finishing-a-development-branch` to choose merge, PR, keep, or discard.

## Plan Self-Review Checklist

- The plan adds a first-class `as_built_assessment` Backlog Primer field.
- The plan tests that cached assessment reaches `build_backlog_input_context`.
- The plan prevents the old `compiled_authority.items[]` assumption by requiring `invariants[]` extraction.
- The plan includes `evidence_pack_fingerprint` in cache metadata and stale-cache tests.
- The plan includes a command/API contract.
- The plan includes idempotency replay and key-reuse guard behavior.
- The plan keeps OpenSpec optional and advisory.
- The plan does not add database tables.
- The plan does not mutate Product Backlog rows during assessment.
