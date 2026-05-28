# As-Built Batched Assessment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `agileforge as-built assess` produce a concrete As-Built artifact for large brownfield repos by splitting the assessor work into bounded batches and merging the results into one cacheable assessment.

**Architecture:** Keep the host-side evidence pack as the request fingerprint source of truth, but invoke the As-Built ADK agent with smaller target slices. Validate every batch against its batch evidence pack, merge capability assessments back in full authority-target order, and write workflow state only after all batches succeed.

**Tech Stack:** Python 3.12, Pydantic v2, SQLModel, Google ADK, anyio, argparse CLI, pytest, existing AgileForge workbench envelopes/fingerprints/runtime config.

---

## Problem Statement

The current `agileforge as-built assess` command is installed and returns bounded JSON errors, but the caRtola smoke still fails:

```text
As-Built assessor timed out after 120 seconds.
```

That is an AgileForge implementation problem, not an action for the caRtola test agent. The current runner sends every extracted authority target and all bounded repo evidence to one model call. caRtola has enough accepted authority targets that this single call can exceed the assessor window before producing any artifact.

The fix is to preserve one logical As-Built assessment while executing the model work in deterministic batches.

## Non-Goals

- Do not ask the caRtola agent to run another smoke until this AgileForge change is merged.
- Do not run backlog generation as part of this fix.
- Do not add database tables.
- Do not cache partial batch results in Phase 1.
- Do not add streaming progress output; the CLI must continue to return one JSON envelope.
- Do not weaken idempotency or identity validation.
- Do not treat timed-out batches as successful `unclear` findings.

## Files

- Modify: `utils/runtime_config.py`
  - Add `get_as_built_assessor_batch_size()`.
- Modify: `tests/test_runtime_config.py`
  - Add default, override, and invalid batch-size tests.
- Modify: `services/agent_workbench/as_built_assessment.py`
  - Add evidence-pack batch slicing.
  - Add batch input construction.
  - Add per-batch invocation and diagnostics.
  - Add final assessment merge.
- Modify: `tests/test_as_built_assessment.py`
  - Add batching, merge, failure, and pack-slicing tests.
- No CLI parser changes are required.

## Design Contract

`agileforge as-built assess` still performs one logical mutation:

1. Build the full `EvidencePack`.
2. Compute the full `evidence_pack_fingerprint`.
3. Compute the request fingerprint from the full pack plus `AS_BUILT_ASSESSOR_BATCH_SIZE`.
4. Split the full pack into batch packs.
5. Invoke the ADK assessor once per batch.
6. Validate each batch assessment against the corresponding batch pack.
7. Merge all batch assessments into one `AsBuiltAssessment` whose `evidence_pack_fingerprint` is the full pack fingerprint.
8. Cache only the merged assessment and record one `AS_BUILT_ASSESSED` workflow event.

Batching is not visible as separate workflow state. It is an execution detail.

## Runtime Behavior

Default batch size:

```text
AS_BUILT_ASSESSOR_BATCH_SIZE=10
```

Valid range:

```text
1 <= AS_BUILT_ASSESSOR_BATCH_SIZE <= 50
```

Invalid batch-size configuration must return the standard `MUTATION_FAILED` envelope from `assess()` rather than crashing the CLI.

Successful response data must add these inspectability fields:

```json
{
  "authority_target_count": 115,
  "batch_count": 12,
  "batch_size": 10
}
```

Failure response details for an assessor failure must include:

```json
{
  "project_id": 2,
  "detail": "As-Built assessor timed out after 120 seconds.",
  "authority_target_count": 115,
  "batch_count": 12,
  "batch_size": 10,
  "completed_batches": 3,
  "failed_batch_index": 4,
  "evidence_pack_fingerprint": "sha256:..."
}
```

## Task 1: Add Runtime Batch-Size Configuration

**Files:**
- Modify: `utils/runtime_config.py`
- Modify: `tests/test_runtime_config.py`

- [ ] **Step 1: Write failing runtime config tests**

Add these tests near the existing As-Built timeout tests in `tests/test_runtime_config.py`:

```python
DEFAULT_AS_BUILT_BATCH_SIZE = 10
CUSTOM_AS_BUILT_BATCH_SIZE = 7


def test_as_built_batch_size_defaults_to_bounded_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As-built assessor batches should be bounded by default."""
    monkeypatch.delenv("AS_BUILT_ASSESSOR_BATCH_SIZE", raising=False)

    assert get_as_built_assessor_batch_size() == DEFAULT_AS_BUILT_BATCH_SIZE


def test_as_built_batch_size_honors_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """As-built assessor batch size can be tuned for smoke tests."""
    monkeypatch.setenv(
        "AS_BUILT_ASSESSOR_BATCH_SIZE",
        str(CUSTOM_AS_BUILT_BATCH_SIZE),
    )

    assert get_as_built_assessor_batch_size() == CUSTOM_AS_BUILT_BATCH_SIZE


def test_as_built_batch_size_rejects_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Batch size must be positive."""
    monkeypatch.setenv("AS_BUILT_ASSESSOR_BATCH_SIZE", "0")

    with pytest.raises(RuntimeConfigError, match="at least 1"):
        get_as_built_assessor_batch_size()


def test_as_built_batch_size_rejects_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batch size must stay under the host-side safety cap."""
    monkeypatch.setenv("AS_BUILT_ASSESSOR_BATCH_SIZE", "51")

    with pytest.raises(RuntimeConfigError, match="at most 50"):
        get_as_built_assessor_batch_size()
```

Update the import block in `tests/test_runtime_config.py`:

```python
from utils.runtime_config import (
    RuntimeConfigError,
    clear_runtime_config_cache,
    get_as_built_assessor_batch_size,
    get_as_built_assessor_timeout_seconds,
    get_business_db_target,
    get_database_echo,
    get_session_db_target,
    is_spec_compiler_schema_disabled,
    resolve_database_target,
)
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
uv run pytest tests/test_runtime_config.py \
  -k "as_built_batch_size" -q
```

Expected: tests fail because `get_as_built_assessor_batch_size` does not exist.

- [ ] **Step 3: Implement the runtime config helper**

Add this function after `get_as_built_assessor_timeout_seconds()` in `utils/runtime_config.py`:

```python
def get_as_built_assessor_batch_size(default: int = 10) -> int:
    """Return the maximum authority targets per as-built assessor batch."""
    value = get_int_env("AS_BUILT_ASSESSOR_BATCH_SIZE", default)
    if value < 1:
        msg = "AS_BUILT_ASSESSOR_BATCH_SIZE must be at least 1."
        raise RuntimeConfigError(msg)
    if value > 50:
        msg = "AS_BUILT_ASSESSOR_BATCH_SIZE must be at most 50."
        raise RuntimeConfigError(msg)
    return value
```

- [ ] **Step 4: Run the runtime config tests**

Run:

```bash
uv run pytest tests/test_runtime_config.py \
  -k "as_built_batch_size or as_built_timeout" -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add utils/runtime_config.py tests/test_runtime_config.py
git commit -m "feat: add as-built assessor batch size config"
```

## Task 2: Add Evidence-Pack Batch Slicing

**Files:**
- Modify: `services/agent_workbench/as_built_assessment.py`
- Modify: `tests/test_as_built_assessment.py`

- [ ] **Step 1: Write failing batch-slicing tests**

Add this import in `tests/test_as_built_assessment.py`:

```python
from services.agent_workbench.as_built_assessment import (
    AS_BUILT_ASSESSMENT_META_STATE_KEY,
    AS_BUILT_ASSESSMENT_STATE_KEY,
    MAX_AUTHORITY_TARGETS,
    MAX_FILE_MANIFEST_ENTRIES,
    MAX_SNIPPETS_PER_TARGET,
    AsBuiltAssessmentRunner,
    build_authority_targets,
    build_evidence_pack,
    split_evidence_pack_for_assessment,
)
```

Add these tests after `test_build_evidence_pack_includes_search_observation_paths`:

```python
def test_split_evidence_pack_batches_targets_in_order(tmp_path: Path) -> None:
    """Batch packs should preserve authority-target order and fingerprints."""
    repo = tmp_path / "repo"
    repo.mkdir()
    authority = {
        "invariants": [
            {
                "id": f"INV-batch-{index:04d}",
                "type": "DATA_CONTRACT",
                "parameters": {
                    "source_item_id": f"REQ.batch-{index}",
                    "rule": f"rule {index}",
                },
            }
            for index in range(5)
        ]
    }
    for index in range(5):
        (repo / f"file_{index}.py").write_text(
            f"# INV-batch-{index:04d}\n",
            encoding="utf-8",
        )
    full_pack = build_evidence_pack(
        project_id=2,
        authority_fingerprint="sha256:authority",
        compiled_authority=authority,
        repo_path=repo,
        spec_mode="unknown",
        spec_file=None,
    )

    batches = split_evidence_pack_for_assessment(full_pack, batch_size=2)

    assert [len(batch.authority_targets) for batch in batches] == [2, 2, 1]
    assert [
        target.authority_ref
        for batch in batches
        for target in batch.authority_targets
    ] == [target.authority_ref for target in full_pack.authority_targets]
    assert all(batch.evidence_pack_fingerprint.startswith("sha256:") for batch in batches)
    assert {batch.evidence_pack_fingerprint for batch in batches} != {
        full_pack.evidence_pack_fingerprint
    }


def test_split_evidence_pack_filters_snippets_to_batch_paths(
    tmp_path: Path,
) -> None:
    """Batch packs should not carry snippets unrelated to the selected targets."""
    repo = tmp_path / "repo"
    repo.mkdir()
    authority = {
        "invariants": [
            {
                "id": "INV-batch-a",
                "type": "DATA_CONTRACT",
                "parameters": {"source_item_id": "REQ.batch-a"},
            },
            {
                "id": "INV-batch-b",
                "type": "DATA_CONTRACT",
                "parameters": {"source_item_id": "REQ.batch-b"},
            },
        ]
    }
    (repo / "a.py").write_text("# INV-batch-a\n", encoding="utf-8")
    (repo / "b.py").write_text("# INV-batch-b\n", encoding="utf-8")
    full_pack = build_evidence_pack(
        project_id=2,
        authority_fingerprint="sha256:authority",
        compiled_authority=authority,
        repo_path=repo,
        spec_mode="unknown",
        spec_file=None,
    )

    first_batch = split_evidence_pack_for_assessment(full_pack, batch_size=1)[0]

    assert [target.authority_ref for target in first_batch.authority_targets] == [
        "REQ.batch-a"
    ]
    assert [snippet.path for snippet in first_batch.source_snippets] == ["a.py"]
    assert [observation.query for observation in first_batch.search_observations] == [
        "REQ.batch-a"
    ]
```

- [ ] **Step 2: Run the failing batch-slicing tests**

Run:

```bash
uv run pytest tests/test_as_built_assessment.py \
  -k "split_evidence_pack" -q
```

Expected: tests fail because `split_evidence_pack_for_assessment` does not exist.

- [ ] **Step 3: Implement batch slicing**

In `services/agent_workbench/as_built_assessment.py`, add this public helper after `build_evidence_pack()`:

```python
def split_evidence_pack_for_assessment(
    pack: EvidencePack,
    *,
    batch_size: int,
) -> list[EvidencePack]:
    """Split one full evidence pack into deterministic assessor batch packs."""
    if batch_size < 1:
        msg = "batch_size must be at least 1"
        raise ValueError(msg)
    if not pack.authority_targets or len(pack.authority_targets) <= batch_size:
        return [pack]

    batches: list[EvidencePack] = []
    for start in range(0, len(pack.authority_targets), batch_size):
        end = start + batch_size
        batches.append(_slice_evidence_pack(pack=pack, start=start, end=end))
    return batches
```

Add this private helper below it:

```python
def _slice_evidence_pack(
    *,
    pack: EvidencePack,
    start: int,
    end: int,
) -> EvidencePack:
    selected_observations = pack.search_observations[start:end]
    referenced_paths = {
        path
        for observation in selected_observations
        for path in observation.paths
    }
    sliced = pack.model_copy(
        update={
            "evidence_pack_fingerprint": "sha256:pending",
            "authority_targets": pack.authority_targets[start:end],
            "source_snippets": [
                snippet
                for snippet in pack.source_snippets
                if snippet.path in referenced_paths
            ],
            "test_snippets": [
                snippet
                for snippet in pack.test_snippets
                if snippet.path in referenced_paths
            ],
            "doc_snippets": [
                snippet
                for snippet in pack.doc_snippets
                if snippet.path in referenced_paths
            ],
            "search_observations": selected_observations,
        }
    )
    return _finalize_pack(sliced)
```

- [ ] **Step 4: Run the batch-slicing tests**

Run:

```bash
uv run pytest tests/test_as_built_assessment.py \
  -k "split_evidence_pack" -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add services/agent_workbench/as_built_assessment.py tests/test_as_built_assessment.py
git commit -m "feat: split as-built evidence packs into batches"
```

## Task 3: Add Batch Merge Helpers

**Files:**
- Modify: `services/agent_workbench/as_built_assessment.py`
- Modify: `tests/test_as_built_assessment.py`

- [ ] **Step 1: Update the fake assessment helper to cover every pack target**

Replace the existing `capability_assessments=[...]` block inside `_fake_assessment()` in `tests/test_as_built_assessment.py` with this list comprehension:

```python
        capability_assessments=[
            CapabilityAssessment(
                authority_ref=item.authority_ref,
                invariant_refs=item.invariant_refs,
                capability_title=item.title,
                status="observed",
                confidence="medium",
                evidence=[],
                limitations=["Tests were not executed."],
                recommended_backlog_treatment="skip_new_implementation",
                reasoning="Repo evidence supports the capability.",
            )
            for item in pack.authority_targets
        ],
```

This keeps existing runner tests honest: a schema-valid fake assessment now covers the full input pack instead of accidentally hiding omitted target coverage.

- [ ] **Step 2: Write failing merge tests**

Add these tests after the batch-slicing tests:

```python
def test_merge_batch_assessments_restores_full_pack_identity(
    tmp_path: Path,
) -> None:
    """Merged assessment should use the full pack identity, not a batch identity."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "live.py").write_text(
        "# INV-a4b296c058e88663\n# INV-ffe2e17832c41874\n",
        encoding="utf-8",
    )
    full_pack = build_evidence_pack(
        project_id=2,
        authority_fingerprint="sha256:authority",
        compiled_authority=CARTOLA_AUTHORITY,
        repo_path=repo,
        spec_mode="unknown",
        spec_file=None,
    )
    batches = split_evidence_pack_for_assessment(full_pack, batch_size=1)
    batch_assessments = [
        _fake_assessment(
            _input_payload_for_pack(batch).model_copy(
                update={"assessment_id": f"batch-{index}"}
            )
        )
        for index, batch in enumerate(batches, start=1)
    ]

    merged = as_built_module.merge_batch_assessments(
        project_id=2,
        assessment_id="as-built-2-full",
        full_pack=full_pack,
        batch_assessments=batch_assessments,
    )

    assert merged.assessment_id == "as-built-2-full"
    assert merged.evidence_pack_fingerprint == full_pack.evidence_pack_fingerprint
    assert len(merged.capability_assessments) == len(full_pack.authority_targets)
    assert [
        item.authority_ref for item in merged.capability_assessments
    ] == [target.authority_ref for target in full_pack.authority_targets]
    assert merged.is_complete is True


def test_merge_batch_assessments_rejects_missing_capability(
    tmp_path: Path,
) -> None:
    """Batch merge must fail if an agent omits an authority target."""
    repo = tmp_path / "repo"
    repo.mkdir()
    full_pack = build_evidence_pack(
        project_id=2,
        authority_fingerprint="sha256:authority",
        compiled_authority=CARTOLA_AUTHORITY,
        repo_path=repo,
        spec_mode="unknown",
        spec_file=None,
    )
    empty_batch = _fake_assessment(_input_payload_for_pack(full_pack)).model_copy(
        update={"capability_assessments": []}
    )

    with pytest.raises(ValueError, match="coverage did not match authority targets"):
        as_built_module.merge_batch_assessments(
            project_id=2,
            assessment_id="as-built-2-full",
            full_pack=full_pack,
            batch_assessments=[empty_batch],
        )
```

- [ ] **Step 3: Run the failing merge tests**

Run:

```bash
uv run pytest tests/test_as_built_assessment.py \
  -k "merge_batch_assessments" -q
```

Expected: tests fail because `merge_batch_assessments` does not exist.

- [ ] **Step 4: Implement merge helpers**

Add this function after `split_evidence_pack_for_assessment()`:

```python
def merge_batch_assessments(
    *,
    project_id: int,
    assessment_id: str,
    full_pack: EvidencePack,
    batch_assessments: list[AsBuiltAssessment],
) -> AsBuiltAssessment:
    """Merge validated batch assessments into one full-pack assessment."""
    expected_keys = [_target_key(target) for target in full_pack.authority_targets]
    capabilities: dict[tuple[str, tuple[str, ...]], CapabilityAssessment] = {}
    cross_cutting_findings: list[str] = []
    open_questions: list[str] = []
    clarifying_questions: list[str] = []
    for index, assessment in enumerate(batch_assessments, start=1):
        for capability in assessment.capability_assessments:
            capabilities[_capability_key(capability)] = capability
        cross_cutting_findings.extend(
            f"[batch {index}] {item}" for item in assessment.cross_cutting_findings
        )
        open_questions.extend(
            f"[batch {index}] {item}" for item in assessment.open_questions
        )
        clarifying_questions.extend(
            f"[batch {index}] {item}" for item in assessment.clarifying_questions
        )

    missing = [key for key in expected_keys if key not in capabilities]
    extra = [key for key in capabilities if key not in expected_keys]
    if missing or extra:
        msg = (
            "Batch assessment coverage did not match authority targets: "
            f"missing={len(missing)} extra={len(extra)}"
        )
        raise ValueError(msg)

    ordered_capabilities = [capabilities[key] for key in expected_keys]
    batch_count = len(batch_assessments)
    return AsBuiltAssessment(
        schema_version=ASSESSMENT_SCHEMA_VERSION,
        project_id=project_id,
        assessment_id=assessment_id,
        agent_version=AGENT_VERSION,
        evidence_pack_builder_version=EVIDENCE_PACK_BUILDER_VERSION,
        authority_fingerprint=full_pack.authority_fingerprint,
        evidence_pack_fingerprint=full_pack.evidence_pack_fingerprint,
        generated_at=utc_now_iso(),
        assessment_summary=(
            f"Merged {batch_count} As-Built assessment batch(es) covering "
            f"{len(ordered_capabilities)} authority target(s)."
        ),
        repo_snapshot=full_pack.repo_snapshot,
        capability_assessments=ordered_capabilities,
        cross_cutting_findings=cross_cutting_findings,
        open_questions=open_questions,
        is_complete=all(assessment.is_complete for assessment in batch_assessments),
        clarifying_questions=clarifying_questions,
    )
```

Add these helpers below it:

```python
def _target_key(target: AuthorityTarget) -> tuple[str, tuple[str, ...]]:
    return (target.authority_ref, tuple(target.invariant_refs))


def _capability_key(
    capability: CapabilityAssessment,
) -> tuple[str, tuple[str, ...]]:
    return (capability.authority_ref, tuple(capability.invariant_refs))
```

Update the schema import block in `services/agent_workbench/as_built_assessment.py`:

```python
from orchestrator_agent.agent_tools.as_built_assessor.schemes import (
    AGENT_VERSION,
    ASSESSMENT_SCHEMA_VERSION,
    EVIDENCE_PACK_BUILDER_VERSION,
    EVIDENCE_PACK_SCHEMA_VERSION,
    AsBuiltAssessment,
    AsBuiltAssessmentCacheMeta,
    AsBuiltAssessorInput,
    AuthorityTarget,
    CapabilityAssessment,
    EvidenceKind,
    EvidencePack,
    EvidenceSnippet,
    EvidenceWarning,
    OpenSpecContext,
    OriginalSpecContext,
    RepoSnapshot,
    SearchObservation,
    SpecMode,
)
```

- [ ] **Step 5: Run the merge tests**

Run:

```bash
uv run pytest tests/test_as_built_assessment.py \
  -k "merge_batch_assessments" -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add services/agent_workbench/as_built_assessment.py tests/test_as_built_assessment.py
git commit -m "feat: merge as-built assessment batches"
```

## Task 4: Invoke the Assessor in Batches

**Files:**
- Modify: `services/agent_workbench/as_built_assessment.py`
- Modify: `tests/test_as_built_assessment.py`

- [ ] **Step 1: Write failing runner batching tests**

Add this fake invoker near `_fake_assessment()`:

```python
class _RecordingBatchInvoker:
    """Fake invoker that records every batch payload."""

    def __init__(self) -> None:
        self.payloads: list[AsBuiltAssessorInput] = []

    def __call__(self, payload: AsBuiltAssessorInput) -> AsBuiltAssessment:
        self.payloads.append(payload)
        return _fake_assessment(payload)
```

Add this test near `test_runner_stores_assessment_cache_and_event`:

```python
def test_runner_invokes_assessor_in_batches_and_merges_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Large target sets should invoke the agent per batch and cache one result."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    _seed_authority(engine)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "live.py").write_text(
        "# INV-a4b296c058e88663\n# INV-ffe2e17832c41874\n",
        encoding="utf-8",
    )
    workflow = _WorkflowStub()
    invoker = _RecordingBatchInvoker()
    monkeypatch.setattr(as_built_module, "get_as_built_assessor_batch_size", lambda: 1)
    runner = AsBuiltAssessmentRunner(
        engine=engine,
        product_repo=_ProductRepoStub(),
        workflow_service=workflow,
        invoke_agent=invoker,
    )

    result = runner.assess(
        project_id=1,
        repo_path=str(repo),
        spec_file=None,
        spec_mode="unknown",
        user_input=None,
        idempotency_key="batched-key",
    )

    assert result["ok"] is True
    assert len(invoker.payloads) == EXPECTED_CARTOLA_TARGET_COUNT
    assert result["data"]["batch_count"] == EXPECTED_CARTOLA_TARGET_COUNT
    assert result["data"]["batch_size"] == 1
    assert result["data"]["authority_target_count"] == EXPECTED_CARTOLA_TARGET_COUNT
    cached = AsBuiltAssessment.model_validate_json(
        workflow.state[AS_BUILT_ASSESSMENT_STATE_KEY]
    )
    assert cached.evidence_pack_fingerprint == result["data"]["evidence_pack_fingerprint"]
    assert len(cached.capability_assessments) == EXPECTED_CARTOLA_TARGET_COUNT
```

- [ ] **Step 2: Run the failing runner batching test**

Run:

```bash
uv run pytest tests/test_as_built_assessment.py \
  -k "runner_invokes_assessor_in_batches" -q
```

Expected: test fails because the runner still invokes the assessor once.

- [ ] **Step 3: Add batch input construction**

Add this helper near `_input_payload_for_pack` equivalent code in `services/agent_workbench/as_built_assessment.py`:

```python
def _assessor_input_for_pack(
    *,
    project_id: int,
    assessment_id: str,
    compiled: dict[str, Any],
    spec_file: Path | None,
    spec_mode: SpecMode,
    pack: EvidencePack,
    user_input: str | None,
) -> AsBuiltAssessorInput:
    return AsBuiltAssessorInput(
        project_id=project_id,
        assessment_id=assessment_id,
        compiled_authority=canonical_json(compiled),
        original_spec=_original_spec_context(
            spec_file=spec_file,
            spec_mode=spec_mode,
        ),
        repo_evidence_pack=pack,
        openspec_context=OpenSpecContext(
            present=False,
            spec_summaries=[],
            change_summaries=[],
        ),
        prior_as_built_assessment="NO_HISTORY",
        user_input=user_input or "",
    )
```

Update `_assessment_id()` to support batch IDs:

```python
def _assessment_id(
    project_id: int,
    evidence_pack_fingerprint: str,
    *,
    batch_index: int | None = None,
    batch_count: int | None = None,
) -> str:
    suffix = evidence_pack_fingerprint.replace("sha256:", "")[:12]
    base = f"as-built-{project_id}-{suffix}"
    if batch_index is None or batch_count is None:
        return base
    return f"{base}-batch-{batch_index:03d}-of-{batch_count:03d}"
```

- [ ] **Step 4: Replace single invocation with batch invocation**

In `AsBuiltAssessmentRunner.assess()`, read the batch size before computing the request fingerprint:

```python
        try:
            batch_size = get_as_built_assessor_batch_size()
        except (RuntimeConfigError, ValueError) as exc:
            return error_envelope(
                command=AS_BUILT_ASSESS_COMMAND,
                error=_mutation_failed(
                    "As-built assessment configuration is invalid.",
                    {"project_id": project_id, "detail": str(exc)},
                ),
            )
```

Add this field to the request fingerprint payload:

```python
                "assessor_batch_size": batch_size,
```

Then replace the current `input_payload = ... self._invoke_agent(input_payload)` block with:

```python
        batch_packs = split_evidence_pack_for_assessment(
            pack,
            batch_size=batch_size,
        )
        assessment_id = _assessment_id(project_id, pack.evidence_pack_fingerprint)
        try:
            assessment = self._invoke_agent_batches(
                project_id=project_id,
                assessment_id=assessment_id,
                compiled=compiled,
                spec_file=Path(spec_file) if spec_file else None,
                spec_mode=_normalize_spec_mode(spec_mode),
                full_pack=pack,
                batch_packs=batch_packs,
                batch_size=batch_size,
                user_input=user_input,
            )
        except (RuntimeError, ValidationError, ValueError) as exc:
            return error_envelope(
                command=AS_BUILT_ASSESS_COMMAND,
                error=_mutation_failed(
                    "As-built assessment agent failed.",
                    _batch_failure_details(
                        project_id=project_id,
                        detail=str(exc),
                        full_pack=pack,
                        batch_count=len(batch_packs),
                        batch_size=batch_size,
                    ),
                ),
            )
```

Import `RuntimeConfigError` and `get_as_built_assessor_batch_size`:

```python
from utils.runtime_config import (
    AS_BUILT_RUNNER_IDENTITY,
    RuntimeConfigError,
    get_as_built_assessor_batch_size,
    get_as_built_assessor_timeout_seconds,
)
```

Update the final identity-validation block after `_invoke_agent_batches()` to validate the merged full assessment:

```python
        assessment_identity_error = _validate_assessment_identity(
            assessment=assessment,
            pack=pack,
            project_id=project_id,
            assessment_id=assessment_id,
        )
```

- [ ] **Step 5: Implement `_invoke_agent_batches`**

Add this method to `AsBuiltAssessmentRunner` below `_record_event()`:

```python
    def _invoke_agent_batches(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        assessment_id: str,
        compiled: dict[str, Any],
        spec_file: Path | None,
        spec_mode: SpecMode,
        full_pack: EvidencePack,
        batch_packs: list[EvidencePack],
        batch_size: int,
        user_input: str | None,
    ) -> AsBuiltAssessment:
        batch_assessments: list[AsBuiltAssessment] = []
        batch_count = len(batch_packs)
        for index, batch_pack in enumerate(batch_packs, start=1):
            batch_input = _assessor_input_for_pack(
                project_id=project_id,
                assessment_id=_assessment_id(
                    project_id,
                    batch_pack.evidence_pack_fingerprint,
                    batch_index=index,
                    batch_count=batch_count,
                ),
                compiled=compiled,
                spec_file=spec_file,
                spec_mode=spec_mode,
                pack=batch_pack,
                user_input=_batch_user_input(
                    user_input=user_input,
                    index=index,
                    batch_count=batch_count,
                    batch_size=batch_size,
                ),
            )
            try:
                batch_assessment = self._invoke_agent(batch_input)
            except (RuntimeError, ValidationError, ValueError) as exc:
                msg = (
                    f"Batch {index}/{batch_count} failed after "
                    f"{len(batch_assessments)} completed batch(es): {exc}"
                )
                raise RuntimeError(msg) from exc
            identity_error = _validate_assessment_identity(
                assessment=batch_assessment,
                pack=batch_pack,
                project_id=project_id,
                assessment_id=batch_input.assessment_id,
            )
            if identity_error is not None:
                msg = (
                    f"Batch {index}/{batch_count} failed identity validation: "
                    f"{identity_error.message}"
                )
                raise ValueError(msg)
            batch_assessments.append(batch_assessment)
        return merge_batch_assessments(
            project_id=project_id,
            assessment_id=assessment_id,
            full_pack=full_pack,
            batch_assessments=batch_assessments,
        )
```

Add these module-level helpers:

```python
def _batch_user_input(
    *,
    user_input: str | None,
    index: int,
    batch_count: int,
    batch_size: int,
) -> str:
    prefix = (
        f"Assess only the authority_targets in this evidence pack. "
        f"This is batch {index} of {batch_count}; configured batch size is "
        f"{batch_size}."
    )
    if user_input and user_input.strip():
        return f"{prefix}\n\nUser input: {user_input.strip()}"
    return prefix


def _batch_failure_details(
    *,
    project_id: int,
    detail: str,
    full_pack: EvidencePack,
    batch_count: int,
    batch_size: int,
) -> dict[str, Any]:
    failed_batch_index = _extract_failed_batch_index(detail)
    completed_batches = max(failed_batch_index - 1, 0) if failed_batch_index else 0
    return {
        "project_id": project_id,
        "detail": detail,
        "authority_target_count": len(full_pack.authority_targets),
        "batch_count": batch_count,
        "batch_size": batch_size,
        "completed_batches": completed_batches,
        "failed_batch_index": failed_batch_index,
        "evidence_pack_fingerprint": full_pack.evidence_pack_fingerprint,
    }


def _extract_failed_batch_index(detail: str) -> int | None:
    marker = "Batch "
    if marker not in detail:
        return None
    suffix = detail.split(marker, 1)[1]
    raw_index = suffix.split("/", 1)[0]
    try:
        return int(raw_index)
    except ValueError:
        return None
```

- [ ] **Step 6: Add response metadata**

In the successful envelope `data` dict in `assess()`, add:

```python
                "authority_target_count": len(pack.authority_targets),
                "batch_count": len(batch_packs),
                "batch_size": batch_size,
```

- [ ] **Step 7: Run the runner batching test**

Run:

```bash
uv run pytest tests/test_as_built_assessment.py \
  -k "runner_invokes_assessor_in_batches" -q
```

Expected: selected test passes.

- [ ] **Step 8: Commit**

```bash
git add services/agent_workbench/as_built_assessment.py tests/test_as_built_assessment.py
git commit -m "feat: invoke as-built assessor in batches"
```

## Task 5: Fail Closed on Batch Failure

**Files:**
- Modify: `tests/test_as_built_assessment.py`
- Modify: `services/agent_workbench/as_built_assessment.py`

- [ ] **Step 1: Write the failing batch-failure test**

Add this fake invoker near `_RecordingBatchInvoker`:

```python
class _SecondBatchTimeoutInvoker:
    """Fake invoker that fails on the second batch."""

    def __init__(self) -> None:
        self.call_count = 0

    def __call__(self, payload: AsBuiltAssessorInput) -> AsBuiltAssessment:
        self.call_count += 1
        if self.call_count == 2:
            msg = "As-Built assessor timed out after 120 seconds."
            raise RuntimeError(msg)
        return _fake_assessment(payload)
```

Add this test near the runner failure tests:

```python
def test_runner_batch_failure_does_not_cache_or_record_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any batch failure should fail the whole command without partial cache."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    _seed_authority(engine)
    repo = tmp_path / "repo"
    repo.mkdir()
    workflow = _WorkflowStub()
    invoker = _SecondBatchTimeoutInvoker()
    monkeypatch.setattr(as_built_module, "get_as_built_assessor_batch_size", lambda: 1)
    runner = AsBuiltAssessmentRunner(
        engine=engine,
        product_repo=_ProductRepoStub(),
        workflow_service=workflow,
        invoke_agent=invoker,
    )

    result = runner.assess(
        project_id=1,
        repo_path=str(repo),
        spec_file=None,
        spec_mode="unknown",
        user_input=None,
        idempotency_key="batch-failure",
    )

    assert result["ok"] is False
    details = result["errors"][0]["details"]
    assert result["errors"][0]["code"] == "MUTATION_FAILED"
    assert details["batch_count"] == EXPECTED_CARTOLA_TARGET_COUNT
    assert details["batch_size"] == 1
    assert details["failed_batch_index"] == 2
    assert details["completed_batches"] == 1
    assert AS_BUILT_ASSESSMENT_STATE_KEY not in workflow.state
    assert AS_BUILT_ASSESSMENT_META_STATE_KEY not in workflow.state
    with Session(engine) as session:
        assert session.exec(select(WorkflowEvent)).all() == []
```

- [ ] **Step 2: Run the failing batch-failure test**

Run:

```bash
uv run pytest tests/test_as_built_assessment.py \
  -k "runner_batch_failure" -q
```

Expected: test fails until failure diagnostics and no-cache behavior are wired correctly.

- [ ] **Step 3: Fix failure diagnostics**

Make `_invoke_agent_batches()` raise errors with the `Batch {index}/{batch_count}` prefix exactly as shown in Task 4. Make sure `assess()` catches the error before any workflow state update or event recording happens.

- [ ] **Step 4: Run runner failure tests**

Run:

```bash
uv run pytest tests/test_as_built_assessment.py \
  -k "runner_batch_failure or timeout_failure or identity_mismatch" -q
```

Expected: selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add services/agent_workbench/as_built_assessment.py tests/test_as_built_assessment.py
git commit -m "fix: fail closed on as-built batch failure"
```

## Task 6: Preserve Idempotency and Cache Semantics

**Files:**
- Modify: `tests/test_as_built_assessment.py`
- Modify: `services/agent_workbench/as_built_assessment.py`

- [ ] **Step 1: Write idempotency regression test for changed batch size**

Add this test near existing idempotency tests:

```python
def test_runner_rejects_reused_idempotency_key_with_changed_batch_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batch size affects assessor execution and must guard idempotency replay."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    _seed_authority(engine)
    repo = tmp_path / "repo"
    repo.mkdir()
    workflow = _WorkflowStub()
    runner = AsBuiltAssessmentRunner(
        engine=engine,
        product_repo=_ProductRepoStub(),
        workflow_service=workflow,
        invoke_agent=_fake_assessment,
    )
    monkeypatch.setattr(as_built_module, "get_as_built_assessor_batch_size", lambda: 2)

    assert runner.assess(
        project_id=1,
        repo_path=str(repo),
        spec_file=None,
        spec_mode="unknown",
        user_input=None,
        idempotency_key="batch-size-key",
    )["ok"] is True
    monkeypatch.setattr(as_built_module, "get_as_built_assessor_batch_size", lambda: 1)
    second = runner.assess(
        project_id=1,
        repo_path=str(repo),
        spec_file=None,
        spec_mode="unknown",
        user_input=None,
        idempotency_key="batch-size-key",
    )

    assert second["ok"] is False
    assert second["errors"][0]["code"] == "IDEMPOTENCY_KEY_REUSED"
```

- [ ] **Step 2: Run the idempotency regression test**

Run:

```bash
uv run pytest tests/test_as_built_assessment.py \
  -k "changed_batch_size" -q
```

Expected: test passes once `assessor_batch_size` is included in the request fingerprint.

- [ ] **Step 3: Run the full As-Built test file**

Run:

```bash
uv run pytest tests/test_as_built_assessment.py -q
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add services/agent_workbench/as_built_assessment.py tests/test_as_built_assessment.py
git commit -m "test: guard as-built idempotency by batch size"
```

## Task 7: Static Checks and Full Verification

**Files:**
- No source edits expected.

- [ ] **Step 1: Run targeted checks**

Run:

```bash
uv run pytest tests/test_runtime_config.py tests/test_as_built_assessment.py -q
uv run ruff check utils/runtime_config.py services/agent_workbench/as_built_assessment.py tests/test_runtime_config.py tests/test_as_built_assessment.py
```

Expected: tests and Ruff pass.

- [ ] **Step 2: Run repository verification**

Run:

```bash
pyrepo-check --all
```

Expected: all configured checks pass.

- [ ] **Step 3: Commit verification-only changes if any files changed**

If formatting or generated updates changed files:

```bash
git add .
git commit -m "chore: verify as-built batch assessment"
```

If no files changed, do not create an empty commit.

## Task 8: Merge and Then Run One caRtola Smoke

**Files:**
- No source edits expected.

- [ ] **Step 1: Merge the feature branch into master**

Use the normal branch completion workflow. The intended branch name is:

```text
dev/as-built-batched-assessment
```

- [ ] **Step 2: Run the controlled smoke after merge**

Run this from `/Users/aaat/projects/agileforge` after master contains the fix:

```bash
agileforge as-built assess \
  --project-id 2 \
  --repo-path /Users/aaat/projects/caRtola \
  --spec-mode unknown \
  --idempotency-key "cartola-as-built-batched-$(date +%Y%m%d%H%M%S)" \
  > /Users/aaat/projects/caRtola/as-built-assess-batched.json
```

Expected successful artifact:

```json
{
  "ok": true,
  "data": {
    "stored_state_key": "as_built_assessment_cached",
    "stored_meta_key": "as_built_assessment_cache_meta",
    "authority_target_count": 115,
    "batch_count": 12,
    "batch_size": 10,
    "assessment": {
      "schema_version": "agileforge.as_built_assessment.v1",
      "capability_assessments": []
    }
  }
}
```

The exact `authority_target_count` may differ if caRtola authority changes before the smoke. The important pass criteria are:

- `ok` is `true`
- cache keys are present
- `batch_count` is greater than `1` for caRtola-sized authority
- `assessment.capability_assessments` count equals `authority_target_count`

- [ ] **Step 3: Do not run backlog generation automatically**

Stop after inspecting the As-Built artifact. Backlog generation is the next experimental decision after this artifact exists.

## Self-Review Checklist

- The plan fixes the actual failure: one oversized model call timing out.
- The caRtola agent has no action until AgileForge is fixed and merged.
- The full evidence pack remains the logical fingerprint and cache identity.
- Batch packs get their own fingerprints for identity validation.
- Workflow state writes happen only after every batch validates and the merge succeeds.
- Idempotency covers batch size.
- Tests prove batching, merging, fail-closed behavior, and cache semantics.
