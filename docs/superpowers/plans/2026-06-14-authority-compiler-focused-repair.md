# Authority Compiler Focused Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement #130 by adding per-request authority compiler model override, strict source-metadata subcodes, and focused structured-item repair for repairable source-map evidence failures.

**Architecture:** Add `compiler_model` to the existing authority compile/regenerate request path and include it in mutation hashes before touching compiler internals. Reuse the existing compiler agent by adding an override-capable builder, then add structured source-metadata issues and a focused retry path that recompiles only repairable structured source items. Preserve fail-closed behavior for invented evidence, non-hard over-promotion, example-only evidence, and unknown source items.

**Tech Stack:** Python 3.12, Pydantic v2, SQLModel, FastAPI, argparse CLI, Google ADK `Agent`, LiteLLM, pytest, `pyrepo-check`.

---

## Files And Responsibilities

- `cli/main.py`: add `--compiler-model` parser args and route them to the application facade.
- `api.py`: add optional `compiler_model` to dashboard authority compile request and pass it through.
- `services/agent_workbench/application.py`: add facade parameters and construct request models with `compiler_model`.
- `services/agent_workbench/project_setup.py`: persist `compiler_model` in `AuthorityCompileRequest`, request hash, dry-run metadata, compile invocation, and failure metadata.
- `services/agent_workbench/authority_regenerate.py`: persist `compiler_model` in `AuthorityRegenerateRequest`, inline ledger hash, dry-run metadata, compile invocation, and failure metadata.
- `services/agent_workbench/command_registry.py`: advertise optional `compiler_model` for authority compile/regenerate.
- `orchestrator_agent/agent_tools/spec_authority_compiler_agent/agent.py`: expose an override-capable compiler agent builder.
- `orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py`: add structured source-metadata issue subcodes while keeping existing string blocking gaps.
- `utils/spec_schemas.py`: add additive structured failure details to compiler failure output.
- `services/specs/compiler_service.py`: thread `compiler_model`, invoke override agents, detect focused repair candidates, build rich repair feedback, retry focused items, merge validated repairs, and expose diagnostics.
- Tests:
  - `tests/test_agent_workbench_cli.py`
  - `tests/test_agent_workbench_authority_decision_cli.py`
  - `tests/test_api_dashboard.py`
  - `tests/test_agent_workbench_application.py`
  - `tests/test_agent_workbench_project_setup.py`
  - `tests/test_agent_workbench_authority_regenerate.py`
  - `tests/test_spec_authority_compiler_normalizer.py`
  - `tests/test_specs_compiler_service.py`

## Task 1: Public Compiler Model Contract

**Files:**
- Modify: `cli/main.py`
- Modify: `api.py`
- Modify: `services/agent_workbench/application.py`
- Modify: `services/agent_workbench/project_setup.py`
- Modify: `services/agent_workbench/authority_regenerate.py`
- Modify: `services/agent_workbench/command_registry.py`
- Test: `tests/test_agent_workbench_cli.py`
- Test: `tests/test_agent_workbench_authority_decision_cli.py`
- Test: `tests/test_api_dashboard.py`
- Test: `tests/test_agent_workbench_application.py`
- Test: `tests/test_agent_workbench_project_setup.py`
- Test: `tests/test_agent_workbench_authority_regenerate.py`

- [ ] **Step 1: Write failing CLI routing tests**

Add to `tests/test_agent_workbench_cli.py` near `test_cli_routes_authority_compile_to_application`:

```python
def test_cli_routes_authority_compile_compiler_model_to_application(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify authority compile routes the per-request compiler model."""
    app = _CliApplication()

    rc = main(
        [
            "authority",
            "compile",
            "--project-id",
            "7",
            "--spec-version-id",
            "3",
            "--expected-spec-hash",
            "a" * 64,
            "--expected-state",
            "SETUP_REQUIRED",
            "--expected-setup-status",
            "authority_compile_required",
            "--compiler-model",
            "openrouter/openai/gpt-5.2",
            "--idempotency-key",
            "compile-model-cli-001",
        ],
        application=app,
    )

    payload = _json_payload(capsys)
    assert rc == 0
    assert _mapping(payload["meta"])["command"] == "agileforge authority compile"
    assert app.calls[-1] == (
        "authority_compile",
        {
            "project_id": 7,
            "spec_version_id": 3,
            "expected_spec_hash": "a" * 64,
            "expected_state": "SETUP_REQUIRED",
            "expected_setup_status": "authority_compile_required",
            "idempotency_key": "compile-model-cli-001",
            "dry_run": False,
            "dry_run_id": None,
            "correlation_id": None,
            "changed_by": "cli-agent",
            "compiler_model": "openrouter/openai/gpt-5.2",
        },
    )
```

Update `_CliApplication.authority_compile(...)` in `tests/test_agent_workbench_cli.py` to accept `compiler_model: str | None = None` and include it in the recorded call dictionary.

Add to `tests/test_agent_workbench_authority_decision_cli.py` near the regenerate tests:

```python
def test_authority_regenerate_cli_routes_compiler_model(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify authority regenerate routes the per-request compiler model."""
    app = _AuthorityDecisionCliApplication()

    rc = main(
        [
            "authority",
            "regenerate",
            "--project-id",
            str(PROJECT_ID),
            "--spec-version-id",
            "3",
            "--compiler-model",
            "openrouter/openai/gpt-5.2",
            "--idempotency-key",
            "regen-model-cli-001",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert payload["ok"] is True
    assert app.calls == [
        (
            "authority_regenerate",
            {
                "project_id": PROJECT_ID,
                "spec_version_id": 3,
                "idempotency_key": "regen-model-cli-001",
                "changed_by": "cli-agent",
                "dry_run": False,
                "compiler_model": "openrouter/openai/gpt-5.2",
            },
        )
    ]
```

Update `_AuthorityDecisionCliApplication.authority_regenerate(...)` to accept `compiler_model: str | None = None` and record it.

- [ ] **Step 2: Run CLI tests and verify they fail for missing parser/facade support**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_cli.py -q -k "authority_compile and compiler_model"
uv run --frozen pytest tests/test_agent_workbench_authority_decision_cli.py -q -k "compiler_model"
```

Expected: both fail because `--compiler-model` is not accepted or is not routed.

- [ ] **Step 3: Write failing API and application tests**

In `tests/test_api_dashboard.py`, update `test_authority_compile_api_routes_to_workbench_application` request JSON:

```python
"compiler_model": "openrouter/openai/gpt-5.2",
```

Update the expected `fake_app.calls[-1]` dictionary:

```python
"compiler_model": "openrouter/openai/gpt-5.2",
```

Add to `tests/test_api_dashboard.py` near `test_authority_compile_api_forbids_extra_fields`:

```python
def test_authority_compile_api_rejects_misspelled_compiler_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compiler model must use the exact public field name."""
    client, repo, _workflow = _build_client(monkeypatch)
    repo.products.append(DummyProduct(product_id=10, name="API Project"))

    response = client.post(
        "/api/projects/10/authority/compile",
        json={
            "spec_version_id": 3,
            "expected_spec_hash": "a" * 64,
            "expected_state": "SETUP_REQUIRED",
            "expected_setup_status": "authority_compile_required",
            "idempotency_key": "authority-compile-api-001",
            "compilerModel": "openrouter/openai/gpt-5.2",
        },
    )

    assert response.status_code == HTTP_UNPROCESSABLE
```

In `tests/test_agent_workbench_application.py`, update `test_application_routes_authority_compile_to_setup_runner`:

```python
result = app.authority_compile(
    project_id=PROJECT_ID,
    spec_version_id=SPEC_VERSION_ID,
    expected_spec_hash="a" * 64,
    expected_state="SETUP_REQUIRED",
    expected_setup_status="authority_compile_required",
    idempotency_key="authority-compile-cli-001",
    dry_run=False,
    dry_run_id=None,
    correlation_id="corr-1",
    changed_by="test-agent",
    compiler_model="openrouter/openai/gpt-5.2",
)
assert result["ok"] is True
assert runner.calls[0][0] == "compile_authority"
request = cast("AuthorityCompileRequest", runner.calls[0][1])
assert request.compiler_model == "openrouter/openai/gpt-5.2"
```

Update `test_application_authority_regenerate_delegates_to_runner`:

```python
result = app.authority_regenerate(
    project_id=PROJECT_ID,
    spec_version_id=SPEC_VERSION_ID,
    idempotency_key="regen-app-001",
    changed_by="test",
    dry_run=True,
    compiler_model="openrouter/openai/gpt-5.2",
)

assert result["ok"] is True
assert runner.calls == [
    AuthorityRegenerateRequest(
        project_id=PROJECT_ID,
        spec_version_id=SPEC_VERSION_ID,
        idempotency_key="regen-app-001",
        changed_by="test",
        dry_run=True,
        compiler_model="openrouter/openai/gpt-5.2",
    )
]
```

In `tests/test_agent_workbench_project_setup.py`, add a request-hash assertion near `test_authority_compile_request_validation_rules`:

```python
def test_authority_compile_request_hash_includes_compiler_model() -> None:
    """Compiler model changes must produce a different mutation request hash."""
    base = AuthorityCompileRequest(
        project_id=7,
        spec_version_id=3,
        expected_spec_hash="a" * 64,
        expected_state="SETUP_REQUIRED",
        expected_setup_status="authority_compile_required",
        idempotency_key="compile-hash-001",
        compiler_model="openrouter/openai/gpt-5.2",
    )
    changed = base.model_copy(update={"compiler_model": "openrouter/openai/gpt-5.3"})

    assert base.normalized_request_hash() != changed.normalized_request_hash()
```

In `tests/test_agent_workbench_authority_regenerate.py`, add:

```python
def test_authority_regenerate_ledger_hash_includes_compiler_model(
    authority_regenerate_runner: AuthorityRegenerateRunner,
    approved_spec_version_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reusing a key with a different compiler model must conflict."""

    def fake_compile(**kwargs: object) -> dict[str, object]:
        return {
            "success": True,
            "authority_id": 44,
            "spec_version_id": approved_spec_version_id,
            "compiler_version": "2.0.0",
            "prompt_hash": "a" * 64,
            "cached": False,
        }

    monkeypatch.setattr(
        authority_regenerate_mod,
        "compile_spec_authority_for_version_with_engine",
        fake_compile,
    )

    first = authority_regenerate_runner.regenerate(
        AuthorityRegenerateRequest(
            project_id=1,
            spec_version_id=approved_spec_version_id,
            idempotency_key="regen-model-hash-001",
            compiler_model="openrouter/openai/gpt-5.2",
        )
    )
    second = authority_regenerate_runner.regenerate(
        AuthorityRegenerateRequest(
            project_id=1,
            spec_version_id=approved_spec_version_id,
            idempotency_key="regen-model-hash-001",
            compiler_model="openrouter/openai/gpt-5.3",
        )
    )

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["errors"][0]["code"] == "IDEMPOTENCY_KEY_REUSED"
```

If this test needs the real `product_id` fixture instead of literal `1`, use the fixture and pass it into the test signature.

- [ ] **Step 4: Run API/application/hash tests and verify they fail**

Run:

```bash
uv run --frozen pytest tests/test_api_dashboard.py -q -k "authority_compile_api"
uv run --frozen pytest tests/test_agent_workbench_application.py -q -k "authority_compile or authority_regenerate"
uv run --frozen pytest tests/test_agent_workbench_project_setup.py -q -k "authority_compile_request_hash_includes_compiler_model"
uv run --frozen pytest tests/test_agent_workbench_authority_regenerate.py -q -k "compiler_model"
```

Expected: failures show missing `compiler_model` fields or unchanged hashes.

- [ ] **Step 5: Implement public contract plumbing**

In `services/agent_workbench/project_setup.py`, update `AuthorityCompileRequest`:

```python
class AuthorityCompileRequest(BaseModel):
    """Validated request for `agileforge authority compile`."""

    project_id: int
    spec_version_id: int
    expected_spec_hash: str = Field(min_length=1)
    expected_state: str = Field(min_length=1)
    expected_setup_status: str = Field(min_length=1)
    compiler_model: str | None = Field(default=None, min_length=1)
    idempotency_key: str | None = None
    dry_run: bool = False
    dry_run_id: str | None = None
    correlation_id: str | None = None
    changed_by: str = "cli-agent"
```

Add `compiler_model` to its hash payload:

```python
"compiler_model": self.compiler_model,
```

In `services/agent_workbench/authority_regenerate.py`, update `AuthorityRegenerateRequest`:

```python
class AuthorityRegenerateRequest(BaseModel):
    """CLI request for authority regeneration."""

    project_id: int
    spec_version_id: int
    compiler_model: str | None = Field(default=None, min_length=1)
    idempotency_key: str | None = None
    changed_by: str = "cli-agent"
    dry_run: bool = False
```

Add the import if missing:

```python
from pydantic import BaseModel, Field
```

Update the inline regenerate ledger hash:

```python
request_hash=canonical_hash(
    {
        "command": AUTHORITY_REGENERATE_COMMAND,
        "project_id": request.project_id,
        "spec_version_id": request.spec_version_id,
        "compiler_model": request.compiler_model,
    }
),
```

In `services/agent_workbench/application.py`, add `compiler_model` to both facade methods and request construction:

```python
compiler_model: str | None = None,
```

and:

```python
compiler_model=compiler_model,
```

In `cli/main.py`, add parser args:

```python
authority_compile.add_argument("--compiler-model")
authority_regenerate.add_argument("--compiler-model")
```

and pass through:

```python
compiler_model=args.compiler_model,
```

In `api.py`, update `AuthorityCompileApiRequest`:

```python
compiler_model: str | None = Field(default=None, min_length=1)
```

and pass:

```python
compiler_model=req.compiler_model,
```

In `services/agent_workbench/command_registry.py`, add `"compiler_model"` to `input_optional` for `agileforge authority compile` and `agileforge authority regenerate`.

- [ ] **Step 6: Run task 1 tests and commit**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_cli.py -q -k "authority_compile"
uv run --frozen pytest tests/test_agent_workbench_authority_decision_cli.py -q -k "authority_regenerate"
uv run --frozen pytest tests/test_api_dashboard.py -q -k "authority_compile_api"
uv run --frozen pytest tests/test_agent_workbench_application.py -q -k "authority_compile or authority_regenerate"
uv run --frozen pytest tests/test_agent_workbench_project_setup.py -q -k "authority_compile_request"
uv run --frozen pytest tests/test_agent_workbench_authority_regenerate.py -q -k "compiler_model or regenerate"
```

Expected: selected tests pass.

Commit:

```bash
git add cli/main.py api.py services/agent_workbench/application.py services/agent_workbench/project_setup.py services/agent_workbench/authority_regenerate.py services/agent_workbench/command_registry.py tests/test_agent_workbench_cli.py tests/test_agent_workbench_authority_decision_cli.py tests/test_api_dashboard.py tests/test_agent_workbench_application.py tests/test_agent_workbench_project_setup.py tests/test_agent_workbench_authority_regenerate.py
git commit -m "feat(authority): expose compiler model override"
```

## Task 2: Compiler Agent Override Injection

**Files:**
- Modify: `orchestrator_agent/agent_tools/spec_authority_compiler_agent/agent.py`
- Modify: `services/specs/compiler_service.py`
- Modify: `services/agent_workbench/project_setup.py`
- Modify: `services/agent_workbench/authority_regenerate.py`
- Test: `tests/test_specs_compiler_service.py`
- Test: `tests/test_agent_workbench_project_setup.py`
- Test: `tests/test_agent_workbench_authority_regenerate.py`

- [ ] **Step 1: Write failing compiler-service override tests**

Add to `tests/test_specs_compiler_service.py` near the default compiler invocation tests:

```python
def test_default_compiler_invocation_passes_compiler_model_to_async_invoker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compiler model override should reach the async agent invocation seam."""
    from services.specs import compiler_service  # noqa: PLC0415

    captured: list[str | None] = []

    async def fake_invoke(
        payload: SpecAuthorityCompilerInput,
        *,
        compiler_model: str | None = None,
    ) -> str:
        del payload
        captured.append(compiler_model)
        return _success_payload_json()

    monkeypatch.setattr(
        "services.specs.compiler_service._invoke_spec_authority_compiler_async",
        fake_invoke,
    )

    compiler_service._default_invoke_spec_authority_compiler(
        spec_content=json.dumps(_agileforge_spec_profile_payload()),
        content_ref=None,
        product_id=4,
        spec_version_id=9,
        compiler_model="openrouter/openai/gpt-5.2",
    )

    assert captured == ["openrouter/openai/gpt-5.2"]
```

Add a direct agent construction test:

```python
def test_compiler_agent_override_rechecks_schema_disable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Override agent construction should observe the current schema-disable flag."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent import agent

    monkeypatch.setattr(agent, "is_spec_compiler_schema_disabled", lambda: True)

    built = agent.build_spec_authority_compiler_agent(
        compiler_model="openrouter/openai/gpt-5.2"
    )

    assert getattr(built, "output_schema", None) is None
```

- [ ] **Step 2: Run override tests and verify they fail**

Run:

```bash
uv run --frozen pytest tests/test_specs_compiler_service.py -q -k "compiler_model_to_async_invoker or override_rechecks"
```

Expected: fails because the builder and function signatures do not exist yet.

- [ ] **Step 3: Implement override-capable agent builder**

In `orchestrator_agent/agent_tools/spec_authority_compiler_agent/agent.py`, replace module-level construction with this shape:

```python
def _compiler_model(model_id: str) -> LiteLlm:
    """Build the LiteLLM wrapper for one compiler model id."""
    return LiteLlm(
        model=model_id,
        api_key=get_openrouter_api_key(),
        drop_params=True,
        extra_body=get_openrouter_extra_body(),
    )


def build_spec_authority_compiler_agent(
    *,
    compiler_model: str | None = None,
) -> Agent:
    """Build a spec authority compiler agent for one invocation."""
    disable_schema = is_spec_compiler_schema_disabled()
    output_schema = None if disable_schema else SpecAuthorityCompilerEnvelope
    return Agent(
        name="spec_authority_compiler_agent",
        description="Compiler-style agent that extracts spec authority in strict JSON.",
        model=_compiler_model(
            compiler_model or get_model_id("spec_authority_compiler")
        ),
        input_schema=SpecAuthorityCompilerInput,
        output_schema=output_schema,
        instruction=SPEC_AUTHORITY_COMPILER_INSTRUCTIONS,
        output_key="spec_authority_compilation",
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


root_agent = build_spec_authority_compiler_agent()
```

In `services/specs/compiler_service.py`, update `_spec_authority_compiler_agent`:

```python
def _spec_authority_compiler_agent(
    *,
    compiler_model: str | None = None,
) -> object:
    """Load or build the ADK compiler agent only for compile-time execution paths."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.agent import (  # noqa: PLC0415
        build_spec_authority_compiler_agent,
        root_agent,
    )

    if compiler_model:
        return build_spec_authority_compiler_agent(compiler_model=compiler_model)
    return root_agent
```

Update `_invoke_spec_authority_compiler_async`:

```python
async def _invoke_spec_authority_compiler_async(
    input_payload: SpecAuthorityCompilerInput,
    *,
    compiler_model: str | None = None,
) -> str:
    """Invoke the spec authority compiler agent and return raw JSON text."""
    return await invoke_agent_to_text(
        agent=_spec_authority_compiler_agent(compiler_model=compiler_model),
        runner_identity=SPEC_AUTHORITY_COMPILER_IDENTITY,
        payload_json=input_payload.model_dump_json(),
        no_text_error="Compiler agent returned no text response",
    )
```

Thread `compiler_model: str | None = None` through:

- `_default_invoke_spec_authority_compiler`
- `_invoke_spec_authority_compiler`
- `_invoke_and_normalize_spec_authority`
- `_compile_spec_authority_output`
- `_invoke_focused_structured_item_authority`
- `_run_compiler_attempt`
- `_invoke_compiler_for_version`
- `compile_spec_authority_for_version_with_engine`
- `compile_spec_authority_for_version`

At each call site, pass `compiler_model=compiler_model`.

- [ ] **Step 4: Pass compiler model from mutation runners**

In `services/agent_workbench/project_setup.py`, update the call to `compile_pending_authority_for_project(...)` by passing the compiler through `engine_bound_compiler`:

```python
def engine_bound_compiler(**kwargs: Any) -> dict[str, Any]:
    return compile_spec_authority_for_version_with_engine(
        engine=self._engine,
        compiler_model=request.compiler_model,
        **kwargs,
    )
```

In `services/agent_workbench/authority_regenerate.py`, update `_compile_authority(...)`:

```python
return compile_spec_authority_for_version_with_engine(
    engine=self.engine,
    spec_version_id=request.spec_version_id,
    force_recompile=True,
    compiler_model=request.compiler_model,
    lease_guard=lease_guard,
    record_progress=record_progress,
)
```

- [ ] **Step 5: Add runner plumbing assertions**

In `tests/test_agent_workbench_project_setup.py`, extend the fake compile callback used by `test_authority_compile_succeeds_from_compile_required` to assert:

```python
assert kwargs["compiler_model"] == "openrouter/openai/gpt-5.2"
```

Then pass `compiler_model="openrouter/openai/gpt-5.2"` in that request.

In `tests/test_agent_workbench_authority_regenerate.py`, update the fake compile in a new test:

```python
def test_authority_regenerate_passes_compiler_model_to_compile_service(
    authority_regenerate_runner: AuthorityRegenerateRunner,
    product_id: int,
    approved_spec_version_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regenerate should pass the selected compiler model into compiler service."""
    captured: list[str | None] = []

    def fake_compile(**kwargs: object) -> dict[str, object]:
        captured.append(cast("str | None", kwargs.get("compiler_model")))
        return _persist_compiled_authority(
            engine=authority_regenerate_runner.engine,
            product_id=product_id,
            prompt_hash="b" * 64,
            spec_version_id=approved_spec_version_id,
        )

    monkeypatch.setattr(
        authority_regenerate_mod,
        "compile_spec_authority_for_version_with_engine",
        fake_compile,
    )

    result = authority_regenerate_runner.regenerate(
        AuthorityRegenerateRequest(
            project_id=product_id,
            spec_version_id=approved_spec_version_id,
            idempotency_key="regen-model-pass-001",
            compiler_model="openrouter/openai/gpt-5.2",
        )
    )

    assert result["ok"] is True
    assert captured == ["openrouter/openai/gpt-5.2"]
```

- [ ] **Step 6: Run task 2 tests and commit**

Run:

```bash
uv run --frozen pytest tests/test_specs_compiler_service.py -q -k "compiler_model or override_rechecks"
uv run --frozen pytest tests/test_agent_workbench_project_setup.py -q -k "authority_compile_succeeds"
uv run --frozen pytest tests/test_agent_workbench_authority_regenerate.py -q -k "compiler_model or pass"
```

Expected: selected tests pass.

Commit:

```bash
git add orchestrator_agent/agent_tools/spec_authority_compiler_agent/agent.py services/specs/compiler_service.py services/agent_workbench/project_setup.py services/agent_workbench/authority_regenerate.py tests/test_specs_compiler_service.py tests/test_agent_workbench_project_setup.py tests/test_agent_workbench_authority_regenerate.py
git commit -m "feat(authority): pass compiler model to compiler agent"
```

## Task 3: Structured Source Metadata Subcodes

**Files:**
- Modify: `utils/spec_schemas.py`
- Modify: `orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py`
- Test: `tests/test_spec_authority_compiler_normalizer.py`

- [ ] **Step 1: Write failing subcode tests**

Add to `tests/test_spec_authority_compiler_normalizer.py` near existing `SOURCE_METADATA_MISMATCH` tests:

```python
def test_structured_profile_source_metadata_failure_includes_repairable_subcode() -> None:
    """Unsupported behavioral source evidence should expose a repairable subcode."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    raw = _legacy_success_payload()
    raw["invariants"] = [
        {
            "id": "INV-3333333333333333",
            "type": "USER_INTERACTION",
            "source_item_id": "CONSTRAINT.uv-managed",
            "source_level": "MUST",
            "parameters": {
                "trigger": "running documented setup commands",
                "target": "project environment",
                "expected_response": "auto-approve all recommendations without review",
            },
        }
    ]
    raw["source_map"] = [
        {
            "invariant_id": "INV-3333333333333333",
            "excerpt": "uv-managed Python project.",
            "location": "CONSTRAINT.uv-managed.title",
        }
    ]

    normalized = normalize_compiler_output(
        json.dumps(raw),
        source_text=_asa_like_structured_spec_source(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    assert normalized.root.reason == "SOURCE_METADATA_MISMATCH"
    details = normalized.root.source_metadata_issues
    assert details is not None
    assert details[0]["subcode"] == "BEHAVIORAL_SOURCE_EVIDENCE_UNSUPPORTED"
    assert details[0]["repairable"] is True
    assert details[0]["source_item_id"] == "CONSTRAINT.uv-managed"
```

Add over-promotion and example-only tests:

```python
def test_structured_profile_over_promotion_subcode_is_not_repairable() -> None:
    """Hard-ban over-promotion should remain fail-closed without focused repair."""
    from orchestrator_agent.agent_tools.spec_authority_compiler_agent.normalizer import (  # noqa: E501, PLC0415
        normalize_compiler_output,
    )

    payload = _legacy_success_payload()
    payload["invariants"] = [
        {
            "id": "INV-aaaaaaaaaaaaaaaa",
            "type": "FORBIDDEN_CAPABILITY",
            "parameters": {"capability": "Sass"},
        }
    ]
    payload["source_map"] = [
        {
            "invariant_id": "INV-aaaaaaaaaaaaaaaa",
            "excerpt": (
                "The implementation avoids Sass, CoffeeScript, or other "
                "preprocessors unless a reviewer records a framework-specific "
                "reason."
            ),
            "location": "CONSTRAINT.html-css-js-style.acceptance[0]",
        }
    ]

    normalized = normalize_compiler_output(
        json.dumps(payload),
        source_text=_structured_behavior_spec_source(),
        source_format="agileforge.spec.v1",
    )

    assert isinstance(normalized.root, SpecAuthorityCompilationFailure)
    details = normalized.root.source_metadata_issues
    assert details is not None
    assert details[0]["subcode"] == "LEGACY_MODALITY_PROMOTION"
    assert details[0]["repairable"] is False
```

- [ ] **Step 2: Run subcode tests and verify they fail**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority_compiler_normalizer.py -q -k "subcode"
```

Expected: fails because `source_metadata_issues` does not exist.

- [ ] **Step 3: Add additive failure detail schema**

In `utils/spec_schemas.py`, update `SpecAuthorityCompilationFailure`:

```python
    source_metadata_issues: Annotated[
        list[dict[str, object]] | None,
        Field(
            default=None,
            description="Structured source metadata validation issues.",
        ),
    ] = None
```

Keep `extra="forbid"` unchanged.

- [ ] **Step 4: Implement structured issue generation**

In `orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py`, add constants near source metadata helpers:

```python
BEHAVIORAL_SOURCE_EVIDENCE_UNSUPPORTED = "BEHAVIORAL_SOURCE_EVIDENCE_UNSUPPORTED"
LEGACY_MODALITY_PROMOTION = "LEGACY_MODALITY_PROMOTION"
EXAMPLE_ONLY_SOURCE_EVIDENCE = "EXAMPLE_ONLY_SOURCE_EVIDENCE"
UNKNOWN_SOURCE_ITEM = "UNKNOWN_SOURCE_ITEM"
MISSING_SOURCE_ITEM_ID = "MISSING_SOURCE_ITEM_ID"
SOURCE_LEVEL_MISMATCH = "SOURCE_LEVEL_MISMATCH"
```

Add a helper returning dictionaries:

```python
def _source_metadata_issue(
    *,
    subcode: str,
    message: str,
    invariant_id: str,
    source_item_id: str | None = None,
    expected_source_level: str | None = None,
    observed_source_level: str | None = None,
    repairable: bool = False,
) -> dict[str, object]:
    """Build a structured source metadata issue for compiler failure details."""
    issue: dict[str, object] = {
        "subcode": subcode,
        "message": message,
        "invariant_id": invariant_id,
        "repairable": repairable,
    }
    if source_item_id:
        issue["source_item_id"] = source_item_id
    if expected_source_level:
        issue["expected_source_level"] = expected_source_level
    if observed_source_level:
        issue["observed_source_level"] = observed_source_level
    return issue
```

Add `_structured_authority_metadata_issues(...)` and have `_structured_authority_metadata_errors(...)` derive strings from the structured issues:

```python
def _structured_authority_metadata_issues(
    success: SpecAuthorityCompilationSuccess,
    *,
    source_text: str,
) -> list[dict[str, object]]:
    """Validate model-emitted authority metadata against structured source."""
    source_items = _structured_profile_items_by_id(source_text)
    if not source_items:
        return []

    issues: list[dict[str, object]] = []
    source_map_ids = _source_map_item_ids_by_invariant(success)
    source_map_entries = _source_map_entries_by_invariant(success.source_map)
    for invariant in success.invariants:
        issues.extend(
            _behavioral_source_metadata_issues(
                invariant,
                source_items=source_items,
                source_entries=source_map_entries.get(invariant.id, []),
            )
        )
        issues.extend(
            _legacy_modality_promotion_issues(
                invariant,
                source_items=source_items,
                source_item_ids=source_map_ids.get(invariant.id, set()),
            )
        )
        issues.extend(
            _example_only_source_issues(
                invariant,
                source_items=source_items,
                source_item_ids=source_map_ids.get(invariant.id, set()),
            )
        )
    return issues


def _structured_authority_metadata_errors(
    success: SpecAuthorityCompilationSuccess,
    *,
    source_text: str,
) -> list[str]:
    """Return legacy string source metadata errors."""
    return [
        str(issue["message"])
        for issue in _structured_authority_metadata_issues(
            success,
            source_text=source_text,
        )
    ]
```

Rename the three existing sub-validator functions or keep their names and add issue-returning versions. Preserve all existing error message text so existing tests remain stable.

When building the failure in `normalize_compiler_output`, pass structured details:

```python
metadata_issues = _structured_authority_metadata_issues(
    success,
    source_text=source_text,
)
if metadata_issues:
    return _failure(
        reason="SOURCE_METADATA_MISMATCH",
        blocking_gaps=[str(issue["message"]) for issue in metadata_issues],
        source_metadata_issues=metadata_issues,
    )
```

Update `_failure(...)` signature to accept `source_metadata_issues: list[dict[str, object]] | None = None` and set it on `SpecAuthorityCompilationFailure`.

- [ ] **Step 5: Run subcode tests and existing strictness tests**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority_compiler_normalizer.py -q -k "SOURCE_METADATA_MISMATCH or source_metadata or fake_excerpt or over_promotion or example"
```

Expected: selected tests pass.

Commit:

```bash
git add utils/spec_schemas.py orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py tests/test_spec_authority_compiler_normalizer.py
git commit -m "feat(authority): classify source metadata failures"
```

## Task 4: Focused Repair Retry And Merge

**Files:**
- Modify: `services/specs/compiler_service.py`
- Test: `tests/test_specs_compiler_service.py`

- [ ] **Step 1: Write failing focused repair success test**

Add to `tests/test_specs_compiler_service.py`:

```python
def test_compile_spec_authority_repairs_one_behavioral_source_item(
    session: Session,
    sample_product: Product,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repairable source metadata failure should retry only the failing item."""
    from services.specs import compiler_service  # noqa: PLC0415

    spec_version_id = _approved_spec_version(
        session,
        sample_product.product_id,
        content=json.dumps(_agileforge_spec_profile_payload()),
    )
    calls: list[dict[str, str | None]] = []

    def fake_invoke(
        *,
        spec_content: str,
        content_ref: str | None,
        product_id: int | None,
        spec_version_id: int | None,
        domain_hint: str | None = None,
        compiler_model: str | None = None,
    ) -> str:
        del content_ref, product_id, spec_version_id
        calls.append(
            {
                "spec_content": spec_content,
                "domain_hint": domain_hint,
                "compiler_model": compiler_model,
            }
        )
        if domain_hint is None:
            return _source_metadata_failure_json(
                source_item_id="REQ.payments.email",
                invariant_id="INV-badbadbadbadbad1",
            )
        return _compiled_success_json_for_source_item("REQ.payments.email")

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_invoke,
    )

    result = compiler_service.compile_spec_authority_for_version_with_engine(
        engine=session.get_bind(),
        spec_version_id=spec_version_id,
        force_recompile=True,
        compiler_model="openrouter/openai/gpt-5.2",
    )

    assert result["success"] is True
    assert len(calls) == 2
    assert "REQ.payments.email" in calls[1]["spec_content"]
    assert "source_item_id: REQ.payments.email" in str(calls[1]["domain_hint"])
    assert calls[1]["compiler_model"] == "openrouter/openai/gpt-5.2"
```

Add helper functions in the test file:

```python
def _approved_spec_version(
    session: Session,
    product_id: int,
    *,
    content: str,
) -> int:
    """Persist one approved spec version and return its id."""
    spec = SpecRegistry(
        product_id=product_id,
        spec_hash="sha256:focused-repair",
        content=content,
        content_ref="specs/focused-repair.json",
        status="approved",
        approved_at=datetime.now(UTC),
        approved_by="test",
        approval_notes="Approved for focused repair tests.",
    )
    session.add(spec)
    session.commit()
    session.refresh(spec)
    return require_id(spec.spec_version_id, "spec_version_id")


def _source_metadata_failure_json(
    *,
    source_item_id: str,
    invariant_id: str,
) -> str:
    failure = SpecAuthorityCompilationFailure(
        error="SPEC_COMPILATION_FAILED",
        reason="SOURCE_METADATA_MISMATCH",
        blocking_gaps=[
            f"{invariant_id} source_item_id {source_item_id} lacks supporting real source_map evidence."
        ],
        source_metadata_issues=[
            {
                "subcode": "BEHAVIORAL_SOURCE_EVIDENCE_UNSUPPORTED",
                "message": (
                    f"{invariant_id} source_item_id {source_item_id} "
                    "lacks supporting real source_map evidence."
                ),
                "invariant_id": invariant_id,
                "source_item_id": source_item_id,
                "expected_source_level": "MUST",
                "repairable": True,
            }
        ],
    )
    return SpecAuthorityCompilerOutput(root=failure).model_dump_json()


def _compiled_success_json_for_source_item(source_item_id: str) -> str:
    success = SpecAuthorityCompilationSuccess(
        scope_themes=["Payments"],
        domain=None,
        invariants=[
            Invariant(
                id="INV-1111111111111111",
                type=InvariantType.REQUIRED_FIELD,
                source_item_id=source_item_id,
                source_level=SpecAuthoritySourceLevel.MUST,
                parameters=RequiredFieldParams(field_name="email"),
            )
        ],
        eligible_feature_rules=[],
        rejected_features=[],
        gaps=[],
        assumptions=[],
        source_map=[
            SourceMapEntry(
                invariant_id="INV-1111111111111111",
                excerpt="The system must collect customer email.",
                location=f"{source_item_id}.acceptance[0]",
            )
        ],
        compiler_version="2.0.0",
        prompt_hash="a" * 64,
    )
    return SpecAuthorityCompilerOutput(root=success).model_dump_json()
```

- [ ] **Step 2: Write failing no-repair tests**

Add:

```python
def test_compile_spec_authority_does_not_repair_over_promotion(
    session: Session,
    sample_product: Product,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-repairable source metadata failures should not trigger focused retry."""
    from services.specs import compiler_service  # noqa: PLC0415

    spec_version_id = _approved_spec_version(
        session,
        sample_product.product_id,
        content=json.dumps(_agileforge_spec_profile_payload()),
    )
    calls = 0

    def fake_invoke(**kwargs: object) -> str:
        nonlocal calls
        calls += 1
        failure = SpecAuthorityCompilationFailure(
            error="SPEC_COMPILATION_FAILED",
            reason="SOURCE_METADATA_MISMATCH",
            blocking_gaps=[
                "INV-hard FORBIDDEN_CAPABILITY over-promotes DECISION.choice source level None."
            ],
            source_metadata_issues=[
                {
                    "subcode": "LEGACY_MODALITY_PROMOTION",
                    "message": (
                        "INV-hard FORBIDDEN_CAPABILITY over-promotes "
                        "DECISION.choice source level None."
                    ),
                    "invariant_id": "INV-hard",
                    "source_item_id": "DECISION.choice",
                    "repairable": False,
                }
            ],
        )
        return SpecAuthorityCompilerOutput(root=failure).model_dump_json()

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_invoke,
    )

    result = compiler_service.compile_spec_authority_for_version_with_engine(
        engine=session.get_bind(),
        spec_version_id=spec_version_id,
        force_recompile=True,
    )

    assert result["success"] is False
    assert calls == 1
    assert result["details"]["repair_attempted"] is False
```

- [ ] **Step 3: Run focused repair tests and verify they fail**

Run:

```bash
uv run --frozen pytest tests/test_specs_compiler_service.py -q -k "repairs_one_behavioral or does_not_repair_over_promotion"
```

Expected: failure because focused repair helpers and diagnostics do not exist.

- [ ] **Step 4: Implement repair candidate extraction and hint builder**

In `services/specs/compiler_service.py`, add dataclasses:

```python
@dataclass(frozen=True)
class _FocusedRepairCandidate:
    """One structured source item eligible for focused compiler repair."""

    item_id: str
    invariant_id: str
    expected_source_level: str | None
    observed_source_level: str | None
    reason: str
    source_excerpt: str | None = None
```

Add helpers:

```python
def _repairable_source_metadata_candidates(
    failure: SpecAuthorityCompilationFailure,
    artifact: TechnicalSpecArtifact,
) -> list[_FocusedRepairCandidate]:
    """Return repairable source metadata failures with known structured items."""
    item_ids = {item.id for item in artifact.items}
    raw_issues = failure.source_metadata_issues or []
    candidates: list[_FocusedRepairCandidate] = []
    for issue in raw_issues:
        if issue.get("subcode") != "BEHAVIORAL_SOURCE_EVIDENCE_UNSUPPORTED":
            continue
        if issue.get("repairable") is not True:
            continue
        source_item_id = issue.get("source_item_id")
        invariant_id = issue.get("invariant_id")
        if not isinstance(source_item_id, str) or source_item_id not in item_ids:
            continue
        if not isinstance(invariant_id, str):
            continue
        candidates.append(
            _FocusedRepairCandidate(
                item_id=source_item_id,
                invariant_id=invariant_id,
                expected_source_level=(
                    str(issue["expected_source_level"])
                    if issue.get("expected_source_level") is not None
                    else None
                ),
                observed_source_level=(
                    str(issue["observed_source_level"])
                    if issue.get("observed_source_level") is not None
                    else None
                ),
                reason=str(issue.get("message") or failure.reason),
                source_excerpt=(
                    str(issue["source_excerpt"])
                    if issue.get("source_excerpt") is not None
                    else None
                ),
            )
        )
    return candidates
```

Add hint builder:

```python
def _focused_repair_domain_hint(candidate: _FocusedRepairCandidate) -> str:
    """Build rich validator feedback for one focused authority repair attempt."""
    lines = [
        "Your previous authority output failed source metadata validation.",
        "",
        "Repair target:",
        f"- source_item_id: {candidate.item_id}",
        f"- source_level: {candidate.expected_source_level or 'unknown'}",
        f"- failing invariant_id: {candidate.invariant_id}",
        f"- failure reason: {candidate.reason}",
    ]
    if candidate.observed_source_level:
        lines.append(f"- observed_source_level: {candidate.observed_source_level}")
    if candidate.source_excerpt:
        lines.append(f"- invalid source excerpt: {candidate.source_excerpt}")
    lines.extend(
        [
            "",
            "Retry only this source item.",
            "Use only source_map excerpts that appear verbatim in the source item text.",
            "Do not invent source references or source levels.",
            "If the source item cannot support an invariant, omit that invariant or return a blocking gap.",
            "Return only valid compiled authority JSON.",
        ]
    )
    return "\n".join(lines)
```

- [ ] **Step 5: Implement focused retry in `_invoke_compiler_for_version`**

Add helper:

```python
def _focused_repair_successes(
    artifact: TechnicalSpecArtifact,
    *,
    candidates: list[_FocusedRepairCandidate],
    spec_version: SpecRegistry,
    compiler_model: str | None,
    lease_guard: Callable[[str], bool] | None,
    heartbeat_interval_seconds: float,
    timeout_seconds: float,
) -> list[SpecAuthorityCompilationSuccess] | _CompilerInvocationResult:
    """Run focused repair attempts and return successes or a failure result."""
    successes: list[SpecAuthorityCompilationSuccess] = []
    for candidate in candidates:
        focused_content = _focused_structured_spec_content(
            artifact,
            item_id=candidate.item_id,
        )
        invocation = _run_compiler_attempt(
            spec_version,
            spec_content=focused_content,
            lease_guard=lease_guard,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            timeout_seconds=timeout_seconds,
            domain_hint=_focused_repair_domain_hint(candidate),
            compiler_model=compiler_model,
        )
        if isinstance(invocation, dict):
            failure = dict(invocation)
            failure.setdefault("details", {})
            details = failure["details"]
            if isinstance(details, dict):
                details["repair_attempted"] = True
                details["repair_item_ids"] = [candidate.item_id]
                details["repair_result"] = "failed"
            return _CompilerInvocationResult(failure=failure)
        if isinstance(invocation.output.root, SpecAuthorityCompilationFailure):
            failure = _normalized_failure_result(
                spec_version,
                raw_json=invocation.raw_json,
                failure=invocation.output.root,
            )
            details = failure.setdefault("details", {})
            if isinstance(details, dict):
                details["repair_attempted"] = True
                details["repair_item_ids"] = [candidate.item_id]
                details["repair_result"] = "failed"
            return _CompilerInvocationResult(failure=failure)
        successes.append(cast("SpecAuthorityCompilationSuccess", invocation.output.root))
    return successes
```

Then in `_invoke_compiler_for_version(...)`, after the first normalized failure with `SOURCE_METADATA_MISMATCH`, parse the structured artifact and candidates. If candidates exist, run repair, merge successes, and return success:

```python
    if retry_reason == "SOURCE_METADATA_MISMATCH":
        artifact = _structured_spec_artifact_or_none(spec_content)
        if artifact is not None:
            candidates = _repairable_source_metadata_candidates(
                normalized.root,
                artifact,
            )
            if candidates:
                repaired = _focused_repair_successes(
                    artifact,
                    candidates=candidates,
                    spec_version=spec_version,
                    compiler_model=compiler_model,
                    lease_guard=lease_guard,
                    heartbeat_interval_seconds=heartbeat_interval_seconds,
                    timeout_seconds=timeout_seconds,
                )
                if isinstance(repaired, _CompilerInvocationResult):
                    return repaired
                merged = _merge_compilation_successes(repaired)
                return _CompilerInvocationResult(success=merged)
```

Keep schema retry behavior unchanged for `INVALID_JSON` and `JSON_VALIDATION_FAILED`.

- [ ] **Step 6: Add no-partial-persist assertion**

Extend the failed repair test to assert no `CompiledSpecAuthority` row exists after failure:

```python
with Session(session.get_bind()) as verify_session:
    rows = verify_session.exec(select(CompiledSpecAuthority)).all()
assert rows == []
```

Import `select` and `CompiledSpecAuthority` if not already available in the test file.

- [ ] **Step 7: Run task 4 tests and commit**

Run:

```bash
uv run --frozen pytest tests/test_specs_compiler_service.py -q -k "repair or SOURCE_METADATA_MISMATCH or schema_retry"
```

Expected: selected tests pass.

Commit:

```bash
git add services/specs/compiler_service.py tests/test_specs_compiler_service.py
git commit -m "feat(authority): retry repairable source metadata failures"
```

## Task 5: Diagnostics, Docs, And Final Verification

**Files:**
- Modify: `services/specs/compiler_service.py`
- Modify: `docs/feedback/asa-milestone1-agileforge-feedback.md`
- Test: `tests/test_specs_compiler_service.py`
- Test: `tests/test_agent_workbench_application.py`

- [ ] **Step 1: Write failing diagnostics test**

Add to `tests/test_specs_compiler_service.py`:

```python
def test_source_metadata_failure_details_include_repair_guidance(
    session: Session,
    sample_product: Product,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unrepaired source metadata failures should include actionable guidance."""
    from services.specs import compiler_service  # noqa: PLC0415

    spec_version_id = _approved_spec_version(
        session,
        sample_product.product_id,
        content=json.dumps(_agileforge_spec_profile_payload()),
    )

    def fake_invoke(**kwargs: object) -> str:
        return _source_metadata_failure_json(
            source_item_id="REQ.payments.email",
            invariant_id="INV-badbadbadbadbad1",
        )

    monkeypatch.setattr(
        compiler_service,
        "_invoke_spec_authority_compiler",
        fake_invoke,
    )

    result = compiler_service.compile_spec_authority_for_version_with_engine(
        engine=session.get_bind(),
        spec_version_id=spec_version_id,
        force_recompile=True,
    )

    assert result["success"] is False
    details = result["details"]
    assert details["source_metadata_subcode"] == "BEHAVIORAL_SOURCE_EVIDENCE_UNSUPPORTED"
    assert details["source_item_id"] == "REQ.payments.email"
    assert details["invalid_invariant_id"] == "INV-badbadbadbadbad1"
    assert details["repair_attempted"] is True
    assert any("--compiler-model" in command for command in details["suggested_commands"])
```

- [ ] **Step 2: Run diagnostics test and verify it fails**

Run:

```bash
uv run --frozen pytest tests/test_specs_compiler_service.py -q -k "repair_guidance"
```

Expected: fails because diagnostics are not projected yet.

- [ ] **Step 3: Implement bounded diagnostics projection**

In `services/specs/compiler_service.py`, add helper:

```python
def _source_metadata_failure_detail_fields(
    failure: SpecAuthorityCompilationFailure,
    *,
    repair_attempted: bool,
    repair_item_ids: list[str],
    repair_result: str,
) -> dict[str, object]:
    """Return bounded actionable diagnostics for source metadata failures."""
    issue = next(
        (
            raw
            for raw in failure.source_metadata_issues or []
            if isinstance(raw, dict)
        ),
        None,
    )
    details: dict[str, object] = {
        "repair_attempted": repair_attempted,
        "repair_item_ids": repair_item_ids,
        "repair_result": repair_result,
    }
    if issue is not None:
        if issue.get("subcode") is not None:
            details["source_metadata_subcode"] = str(issue["subcode"])
        if issue.get("source_item_id") is not None:
            details["source_item_id"] = str(issue["source_item_id"])
        if issue.get("invariant_id") is not None:
            details["invalid_invariant_id"] = str(issue["invariant_id"])
        if issue.get("expected_source_level") is not None:
            details["source_level"] = str(issue["expected_source_level"])
        if issue.get("source_excerpt") is not None:
            details["source_excerpt"] = str(issue["source_excerpt"])[:500]
    details["suggested_commands"] = [
        (
            "agileforge authority compile --project-id 7 --spec-version-id 3 "
            "--expected-spec-hash refresh-from-workflow-next "
            "--expected-state SETUP_REQUIRED "
            "--expected-setup-status authority_compile_failed "
            "--compiler-model openrouter/openai/gpt-5.2 "
            "--idempotency-key authority-compile-retry-20260614-001"
        ),
        (
            "agileforge authority regenerate --project-id 7 --spec-version-id 3 "
            "--compiler-model openrouter/openai/gpt-5.2 "
            "--idempotency-key authority-regenerate-retry-20260614-001"
        ),
    ]
    return details
```

When producing `_normalized_failure_result(...)` for `SOURCE_METADATA_MISMATCH`, merge these fields into `details`.

- [ ] **Step 4: Update feedback doc**

Append a short entry to `docs/feedback/asa-milestone1-agileforge-feedback.md` under the issue tracking area:

```markdown
### Authority compiler source-map repair (#130)

Status: Accepted for implementation.

Decision:
- Keep source-map validation fail-closed.
- Add per-run `--compiler-model` support for authority compile/regenerate.
- Add focused repair only for structured behavioral source-evidence failures.
- Do not auto-repair legacy modality promotion or example-only evidence.

Reason:
ASA exposed the operational need for a stronger compiler retry, but the product
must not accept invented or unsupported authority evidence.
```

- [ ] **Step 5: Run focused and full gates**

Run focused suites:

```bash
uv run --frozen pytest tests/test_agent_workbench_cli.py -q -k "authority_compile"
uv run --frozen pytest tests/test_agent_workbench_authority_decision_cli.py -q -k "authority_regenerate"
uv run --frozen pytest tests/test_api_dashboard.py -q -k "authority_compile_api"
uv run --frozen pytest tests/test_agent_workbench_application.py -q -k "authority_compile or authority_regenerate"
uv run --frozen pytest tests/test_agent_workbench_project_setup.py -q -k "authority_compile"
uv run --frozen pytest tests/test_agent_workbench_authority_regenerate.py -q
uv run --frozen pytest tests/test_spec_authority_compiler_normalizer.py -q -k "SOURCE_METADATA_MISMATCH or source_metadata or fake_excerpt or over_promotion or example"
uv run --frozen pytest tests/test_specs_compiler_service.py -q -k "repair or compiler_model or schema_retry or SOURCE_METADATA_MISMATCH"
```

Run final gate:

```bash
pyrepo-check --all
```

Expected: all pass.

- [ ] **Step 6: Commit final diagnostics/docs**

Commit:

```bash
git add services/specs/compiler_service.py tests/test_specs_compiler_service.py docs/feedback/asa-milestone1-agileforge-feedback.md
git commit -m "docs(feedback): record authority compiler repair decision"
```

## Final Branch Verification

After all tasks are committed, run:

```bash
git status --short --branch
git log --oneline -6
pyrepo-check --all
```

Expected:

- worktree clean;
- branch contains the design commit plus implementation commits;
- `pyrepo-check --all` passes.

## Plan Self-Review

- Spec coverage: public CLI/API contract is Task 1; compiler override injection is Task 2; source metadata subcodes are Task 3; focused repair trigger, rich feedback, merge, and no-repair cases are Task 4; diagnostics and feedback update are Task 5.
- Marker scan: no unresolved task markers are intentionally left in this plan.
- Type consistency: the plan uses `compiler_model`, `source_metadata_issues`, `BEHAVIORAL_SOURCE_EVIDENCE_UNSUPPORTED`, `expected_source_level`, and `observed_source_level` consistently across schema, normalizer, compiler service, and tests.
- Scope check: the plan does not implement brownfield setup, scope extension, backlog, roadmap, story, sprint, authority acceptance, or model default changes.
