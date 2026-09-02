# Specification Validation and Recovery Investigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reproduce issue #245 without real model calls, correct classification and diagnostics for invalid Specification output, clarify the primary prompt, and establish the evidence needed to decide whether automatic recovery should follow.

**Architecture:** Keep the real leaf output schema and classify generated output in its after-model callback. Preserve ADK 2.2.0's existing propagation of typed child errors; append a sanitized diagnostic through the runner's session service after a known output failure. Implement this first phase independently of any automatic repair subsystem.

**Tech Stack:** Python >=3.13.15,<3.14; uv; installed Google ADK 2.2.0; Pydantic; SQLModel; pytest with pytest-socket; existing Node frontend tests.

**Spec:** [Issue #245](https://github.com/arduinitavares/agileforge/issues/245), the user's approved investigation direction, and the September 2 request to evaluate the independent review and revise this plan. The decisions and acceptance criteria below are the execution specification.

**Revision:** 2, September 2, 2026. Replaces the previous immediate additive-repair design. Automatic recovery remains an explicit follow-on investigation, not an implemented or abandoned requirement.

## Global Constraints

- Current authorization covers this plan only. Do not execute its implementation steps, tests, or experiments during planning.
- Preserve every approved requirement, historical item, and relation. Never remove requirements to obtain a passing result.
- Do not edit registered P&ID sources, reconstruction notes, ADRs, external skills, production configuration, issues, or runtime databases.
- Keep the closed `agileforge.spec.v2` schema and graph invariants. No invalid candidate or automatic acceptance.
- No new dependencies, ADK upgrades, business schema migrations, service restarts, or Roadmap/Backlog behavior changes.
- Subsequently authorized automated verification uses synthetic data, fake `BaseLlm` responses, disposable test repositories/databases, and disabled network sockets.
- Inspect runtime SQLite databases only using URI `mode=ro` and `PRAGMA query_only=ON`. Keep business and ADK trace databases separate.
- New diagnostics must contain no raw failed response, full private source, Pydantic `input`/`ctx`, credentials, or raw exception traceback text.
- Retain byte-exact source registration. Fixture newline handling must not normalize production inputs.
- Code implementation, commits, branches, publishing, live retries, and runtime changes require authorization beyond this planning request. Disposable Git commits already used inside authorized test fixtures are test setup, not project history changes.
- Follow checkout `AGENTS.md`; use uv. For development runtime operations use that checkout's `./agileforge-dev`, with `info --json` before mutations. Never use the user-level shim.

## Verified baseline

AgileForge revision: `db43b680fd6d15a72e841ebd1d98a09bf8d75fa1`.

Project 1, attempt 6, source registration 2: `specification.structure` failed with `SPECIFICATION_PRODUCER_FAILED`; no candidate was persisted. ADK session `sha256:677e888e10ce5d19a0244480b58f5ae43afbb115bc1881ad6e86af0b27577046`, invocation `e-2dc36d32-2c2b-4676-865e-a493b1b2d55f`, error event `2f55fc35-1e97-4c8a-914d-959a44f972ee`.

The surviving error is `SpecificationStructuringOutput.payload: unknown relation endpoint: DATA.review-state-current`. The full failed response, usage, and finish reason were not found in that session. This proves the exact endpoint ID was missing from the generated item set; it does not establish omission versus rename, model reasoning, or source organization as the cause.

The source defines the ID and has 50 unique items and 24 closed relations. Primary source revision `d6251d38810902ee1abe4664f57094612842fe6a`, SHA-256 `adb898fa66316d2f7193624a16ca4a2fd1ab51954a593622ebc8f28e73ff85ea`. No source-contract violation has been demonstrated. `RequirementLevel.INFORMATIVE` supports descriptive historical items; mandatory fields and normative preservation obligations still apply.

The actual successful Calculator campaign used the same relevant code, prompt, schema, model, and limits. Its candidate had 37 items and 26 closed relations. Compare like-for-like normalized input: Calculator 15,980 bytes; P&ID 142,300 bytes. The issue-200 fixture is a separate regression fixture, not the complete evidence for that live campaign. The P&ID attempt lasted about 108 seconds with a configured total timeout of 120 seconds, 32,768 output tokens, and `max_attempts=2`.

### Review dispositions

| Review finding | Verified conclusion and plan change |
| --- | --- |
| Real-Agent reproduction and callback classification are sound | Retained. The fake model must run inside the actual leaf schema boundary. |
| Catching only the output-error subtype necessarily regresses incomplete-output classification | Not established; the installed framework disproves the necessary propagation claim. `Context.run_node` wraps, but `NodeRunner` unwraps before Workflow/Runner propagation. Remove unnecessary manual conversion and test all typed codes through the actual path. |
| More general manual unwrapping is automatically safer | Not necessarily. Raising a direct exception inside the function can enter local retry logic that a propagated dynamic failure skips. Preserve the dynamic failure path and prohibit unintended retries of output validation failures. |
| Repair cannot fit after a 108-second primary under the same 120-second budget | A serious budget risk, not proof every future repair must time out. Remove automatic repair from the first phase; do not silently increase limits. |
| Regex eligibility violates the ADR's accepted-authority boundary | No direct violation was demonstrated: the earlier check gated repair before human acceptance. It did add formatting dependence. Remove that parser from this phase. |
| Additive repair can produce duplicate or split meaning | Valid limitation. Exact graph/item preservation cannot establish semantic completeness. Recovery needs a separate design and evaluation. |
| A clearer prompt prevents this problem, so one successful run makes recovery redundant | Unsupported. Instructions reduce ambiguity but do not guarantee compliance; one success does not establish a failure rate or remove the need to evaluate resilience. |
| Assigning the decorated node's schema is necessarily an unsupported monkeypatch | `output_schema` is a real field, but the generator conversion is avoidable. Use public session-service event persistence in the runner instead. No invented `context.emit` API. |
| Windows fixture lengths differ due to CRLF | Confirmed: source 8,881 on disk versus 8,726 LF bytes; Context 1,334 versus 1,297. Normalize only controlled fixture construction, then test production preservation of both LF and CRLF bytes. |
| Phase 1 alone is an airtight production fix for all of #245 | Too strong. Phase 1 addresses the confirmed classification/diagnostic defects. P&ID candidate generation and any recovery behavior remain separately unverified. |

### Code evidence to read before execution

- `adapters/adk/agents/specification_author.py:52-107`: extraction, incomplete-output callback, production schema/output key.
- `adapters/adk/recipes.py:1049-1109`: leaf invocation precedes the post-return schema catch.
- `adapters/adk/runner.py:414-490,549-599`: error mapping and session-service lifetime.
- Installed `google/adk/agents/context.py:510-517`: `DynamicNodeFailError(error=child_ctx.error)`.
- Installed `google/adk/workflow/_node_runner.py:133-155`: unwraps dynamic errors and returns before ordinary retry handling.
- Installed `google/adk/workflow/_workflow.py:370-383` and `google/adk/runners.py:579-580`: pass and raise the original child error.
- Installed `google/adk/workflow/utils/_retry_utils.py:29-48`: direct exceptions can be retried by generic configured attempts.
- Installed `google/adk/runners.py:523-529,958-977`: `yield_user_message=True` exposes the real invocation ID before the leaf runs.
- Installed `google/adk/sessions/database_session_service.py:647-766`: public `append_event`, with stale-session checks requiring a fresh session snapshot.
- `utils/agileforge_spec_profile_v2.py:45-52,191-210,254-281`: levels, required item fields, endpoint validation.
- `frontend/project.js:4851`: terminal wording currently recognizes only producer failure.
- `docs/adr/0005-use-accepted-specification-as-delivery-contract.md`: accepted Specification is delivery authority; source registration/structuring remain unchanged.

## Scope and file map

Phase 1 contains four independently reviewable tasks. Phase 2 is a design/evaluation gate, with no automatic implementation attached.

| File | Phase 1 responsibility |
| --- | --- |
| Modify `adapters/adk/errors.py` | Typed output failure carrying sanitized diagnostics only. |
| Create `adapters/adk/specification_output.py` | Pure response classification and safe diagnostic extraction. |
| Modify `adapters/adk/agents/specification_author.py` | Validate before ADK conversion while retaining schema and incomplete detection. |
| Modify `adapters/adk/recipes.py` | Always revalidate after a dispatched leaf; keep coroutine and post-return validation. |
| Modify `adapters/adk/runner.py` | Append sanitized diagnostic events through the existing session service; preserve business error mapping. |
| Modify `adapters/adk/prompts/specification_author.txt` | Explicit ID, relation, and historical-item guidance. |
| Modify `services/contracts/specification_authoring.py` | Producer/prompt constants for changed primary instructions. |
| Modify `frontend/project.js` | Known terminal failure wording. |
| Modify `tests/adapters/test_adk_specification_authoring_failures.py` | Real-leaf reproduction, existing-code preservation, drift, diagnostics, fixture portability. |
| Create `tests/adapters/test_specification_output.py` | Classifier and redaction tests. |
| Modify `tests/adapters/test_specification_author_agent.py` | Production callback, schema, configuration and existing single-prompt hash assertions. |
| Modify `tests/adapters/test_api_workflow_domain.py`, `tests/adapters/test_cli_workflow_domain.py` | Corrected code/message transport. |
| Modify `tests/test_workflow_position_display.mjs` | UI terminal versus uncertain outcomes. |
| Create `docs/testing/SPECIFICATION-OUTPUT-RECOVERY.md` | Operator interpretation, safe diagnostics, limits and future evaluation. |

Do not create a repair agent, Markdown ID parser, repair DTO/module, repair prompt, prompt bundle, or new application composition wiring in Phase 1. Do not modify the canonical v2 validator or frozen input fingerprint contract.

## Task 1: Reproduce the real failure and preserve output error classifications

**Interfaces:**

- Consume existing `SpecificationStructuringInput`, `SpecificationStructuringOutput`, `SpecificationAgenticExecutionError`, and `reject_incomplete_specification_output`.
- Produce `validate_specification_response(response_text: str | None, *, finish_reason: str | None, usage: JsonObject) -> SpecificationStructuringOutput` in `adapters/adk/specification_output.py`.
- Produce `build_specification_output_diagnostic(response_text: str | None, *, finish_reason: str | None, usage: JsonObject, code: str) -> JsonObject` in that module.
- Produce callback `validate_specification_output(callback_context: CallbackContext, llm_response: LlmResponse) -> None` in the agent module.
- Produce `SpecificationOutputValidationError(SpecificationAgenticExecutionError)` with `diagnostic: JsonObject`, excluded from repr. No raw response or parsed output field is retained on the exception.

**Files:** errors, new classifier, agent, recipe, ADK failure tests, new classifier tests; paths from the file map.

- [ ] Add `json`/`deepcopy` imports and the following synthetic response helper to the ADK failure tests. It deliberately includes a relation whose endpoint has no item.

```python
def _dangling_output() -> JsonObject:
    output = deepcopy(_valid_output())
    payload = cast("JsonObject", output["payload"])
    payload["relations"] = [
        {"from": "REQ.attempt-boundary", "type": "tracks", "to": "RISK.missing-source"}
    ]
    return output


def test_real_leaf_dangling_endpoint_is_invalid_payload(engine: Engine, tmp_path: Path) -> None:
    model = _SpecificationResponseLlm(
        model="fake/issue-245",
        response_text=json.dumps(_dangling_output()),
        finish_reason=types.FinishReason.STOP,
    )
    leaf = Agent(
        name="issue_245_structurer",
        model=model,
        input_schema=SpecificationStructuringInput,
        output_schema=SpecificationStructuringOutput,
        instruction="Return the supplied synthetic response.",
        mode="single_turn",
        output_key="specification_candidate",
        after_model_callback=reject_incomplete_specification_output,
    )
    runner, _, project_id, decision, frozen, guards = _system(engine, tmp_path, leaf)
    result = runner.run(decision, frozen, guards=guards)
    assert not result.ok
    assert result.error is not None
    assert result.error.code.value == "INVALID_SPECIFICATION_PAYLOAD"
    assert model.calls == ["provider"]
    with Session(engine) as session:
        outcome = _latest_outcome(session, project_id=project_id)
        assert outcome.status == "failure"
        assert outcome.failure_code == "INVALID_SPECIFICATION_PAYLOAD"
        assert not session.exec(select(SpecificationCandidate)).all()
```

- [ ] Run `uv run --no-sync --offline pytest tests/adapters/test_adk_specification_authoring_failures.py::test_real_leaf_dangling_endpoint_is_invalid_payload -q`. Record the assertion failure: actual `SPECIFICATION_PRODUCER_FAILED`, expected `INVALID_SPECIFICATION_PAYLOAD`. A fixture/startup failure does not establish reproduction.
- [ ] Add the typed error and classifier. The classifier parses with `json.loads`, requires an object root, distinguishes explicit non-v2 `payload.schema_version`, then invokes `SpecificationStructuringOutput.model_validate`. Convert parsing/schema errors to fixed messages with suppressed exception chaining; never retain/format raw Pydantic errors. Build diagnostics from bounded safe fields as specified in Task 2.
- [ ] Implement the new callback using this ordering. Existing incomplete detection remains first and its code/message remain unchanged; it is enriched into the diagnostic subtype without changing classification.

```python
def validate_specification_output(callback_context: CallbackContext, llm_response: LlmResponse) -> None:
    text = _response_text(llm_response)
    reason = llm_response.finish_reason
    finish_reason = None if reason is None else reason.value
    usage: JsonObject = {
        "prompt_token_count": getattr(llm_response.usage_metadata, "prompt_token_count", None),
        "candidates_token_count": getattr(llm_response.usage_metadata, "candidates_token_count", None),
    }
    try:
        reject_incomplete_specification_output(callback_context, llm_response)
    except SpecificationAgenticExecutionError as error:
        raise SpecificationOutputValidationError(
            code=error.code,
            message=error.message,
            diagnostic=build_specification_output_diagnostic(
                text, finish_reason=finish_reason, usage=usage, code=error.code,
            ),
        ) from None
    validate_specification_response(text, finish_reason=finish_reason, usage=usage)
```

- [ ] Replace the production root and the new regression test callback with `validate_specification_output`. Preserve the exact input/output schema, `mode="single_turn"`, `output_key`, model and generation settings.
- [ ] Keep `execute_specification_structurer` a coroutine returning `RecipeOutput`. Keep ADK's `DynamicNodeFailError` propagation; do not convert it into a fresh direct exception in the function. Move post-call revalidation into `finally`, retaining the existing pre-call check:

```python
try:
    generated = await context.run_node(
        leaf_agent, node_input=structuring_input.model_dump(mode="json"),
    )
finally:
    revalidated = revalidate_specification_attempt("after_provider")
    if revalidated is not None and (not revalidated.ok or revalidated.replayed):
        raise AttemptRevalidationError(revalidated)
```

Keep the existing post-return output check for injected leaves. Suppress raw exception chaining there. Explicitly use `RetryConfig(max_attempts=1)` for both Specification wrapper levels, retaining the configured timeout/token limits. This prevents direct post-return validation and revalidation errors from accidentally rerunning the provider. Document this effective fail-closed policy through producer version `1.0.2`; do not alter past attempt settings or claim a crash-proof global call cap.

- [ ] Add real-Agent, real-runner parameterized cases. For incomplete-output rows, run both the old incomplete callback and the new callback: the old case proves existing ADK propagation, and the new case proves preservation. Use the new callback for the other output-validation rows. All leaves include `output_key`. Assert result and durable outcome match, exactly one model dispatch, and zero candidates:

| Response | Finish reason | Expected code |
| --- | --- | --- |
| Complete dangling graph | STOP | INVALID_SPECIFICATION_PAYLOAD |
| Complete v2 object missing required fields | STOP | INVALID_SPECIFICATION_PAYLOAD |
| Non-EOF malformed JSON: `{"payload": invalid}` | STOP | INVALID_SPECIFICATION_PAYLOAD |
| Explicit v1 schema | STOP | UNSUPPORTED_SPECIFICATION_SCHEMA |
| Cut-off JSON: `{"payload":` | STOP | SPECIFICATION_OUTPUT_INCOMPLETE |
| Complete valid JSON | MAX_TOKENS | SPECIFICATION_OUTPUT_INCOMPLETE |
| Existing fake provider exception | none | SPECIFICATION_PRODUCER_FAILED |

Also assert that a fake leaf raising an unallowlisted typed code retains the runner's producer-failure fallback, and that host/input/revalidation exceptions retain their distinct behavior. Do not widen the business allowlist or globally relabel arbitrary `ValidationError` instances.

- [ ] Add a settings case with `max_attempts=2`: a terminal generated-output failure still makes one model dispatch. Add source drift after invalid output: post-call revalidation runs and its obsolete result wins; no candidate is written. ADK's original safe leaf-error event may remain while the business outcome is obsolete.
- [ ] Address the verified fixture portability issue only in test preparation. Use this helper for the controlled issue-200 Markdown/Context fixture bytes before `_system` writes the temporary registered source:

```python
def _issue_200_fixture_bytes(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")
```

Keep the existing canonical LF byte lengths and hashes asserted against these constructed fixture bytes. Add a separate `source_bytes` parameterized real-runner test with explicit LF and CRLF bytes (`b"# Source\nDescription.\n"` and `b"# Source\r\nDescription.\r\n"`). After registration/input assembly, assert `SpecificationStructuringInput.model_validate(frozen).registered_source.source.text.encode("utf-8") == source_bytes`. Production loading remains byte-exact. This is not permission to normalize user documents or weaken equality checks.

- [ ] Retain the unshortened issue-200 complete-output test and add the production output key/callback. It must still produce the same semantic canonical payload with one model dispatch. Its new provenance envelope will intentionally differ after Task 4's version change.
- [ ] Run `uv run --no-sync --offline pytest tests/adapters/test_adk_specification_authoring_failures.py tests/adapters/test_specification_output.py tests/adapters/test_specification_author_agent.py -q`. This task is independently useful without recovery.

## Task 2: Retain safe, correlated diagnostics without changing recipe shape

**Interfaces:** Consume Task 1's `SpecificationOutputValidationError.diagnostic`. Add runner helper `_append_specification_output_diagnostic(self, *, session_service: BaseSessionService, session_id: str, invocation_id: str, diagnostic: JsonObject) -> None`. It is async. No new business schema or public transport fields.

**Files:** classifier, runner, classifier tests and real-runner tests.

- [ ] Use only these diagnostic fields: `schema_version="agileforge.specification-output-diagnostic.v1"`, `stage="primary"`, `code`, `response_sha256`, `response_bytes`, `finish_reason`, `prompt_token_count`, `candidates_token_count`, `item_count`, `relation_count`, `missing_item_count`, `item_ids`, `missing_item_ids`, and `ids_truncated`.
- [ ] SHA-256 and byte count describe extracted non-thought response text encoded as UTF-8; use null when text is absent. Only nonnegative integer usage counts and known finish-reason values are admitted. Include IDs only after validating the v2 syntax; cap each array at 100 and report full counts/truncation. Compute missing endpoints from independently valid items/relations; unknown counts are null. No titles, statements, rationale, source notes, raw response, or Pydantic error entries.
- [ ] Make public messages fixed except for up to five syntactically verified missing endpoint IDs plus a remaining count. Example: `Specification structurer returned an invalid v2 payload. Unknown relation endpoint: RISK.missing-source.` The runner must preserve the existing allowlisted code/message.
- [ ] Add the redaction test before wiring persistence:

```python
def test_invalid_output_diagnostics_do_not_echo_response_prose() -> None:
    sentinel = "PRIVATE_RESPONSE_SENTINEL_245"
    raw = json.dumps({"payload": {"schema_version": "agileforge.spec.v2", "items": [{"statement": sentinel}]}})
    with pytest.raises(SpecificationOutputValidationError) as raised:
        validate_specification_response(raw, finish_reason="STOP", usage={})
    error = raised.value
    assert sentinel not in str(error)
    assert sentinel not in repr(error)
    assert sentinel not in json.dumps(error.diagnostic)
    assert error.diagnostic["response_bytes"] == len(raw.encode("utf-8"))
    assert error.diagnostic["response_sha256"] == "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

- [ ] In `_run_recipe`, use the public `yield_user_message` option for the Specification recipe and capture the nonempty invocation ID from the first yielded user event. Other recipes retain current behavior. The event has `output=None` and must not affect result selection. Do not supply an invented `invocation_id` to `run_async`: ADK uses that argument for resumption.
- [ ] Catch `SpecificationOutputValidationError` around the asynchronous runner loop, after ADK has propagated the original typed error. Append the safe diagnostic before the owned session service is closed, then bare-raise the same error. Leave `recipes.py` a coroutine and do not assign decorated node schema properties. Replace only the existing loop, preserving its surrounding session setup, final `RecipeOutput` validation and service cleanup:

```python
is_specification = recipe.node_id == "specification.structure"
invocation_id = ""
output: object | None = None
try:
    async for event in runner.run_async(
        user_id=self._config.identity.user_id,
        session_id=session_id,
        new_message=message,
        yield_user_message=is_specification,
    ):
        if is_specification and event.author == "user" and not invocation_id:
            invocation_id = event.invocation_id
        if event.output is not None:
            output = event.output
except SpecificationOutputValidationError as error:
    if is_specification:
        await self._append_specification_output_diagnostic(
            session_service=session_service,
            session_id=session_id,
            invocation_id=invocation_id,
            diagnostic=error.diagnostic,
        )
    raise
```
- [ ] Implement the helper using public session APIs. Reload the session to avoid stale storage revisions, preserve the captured invocation ID, and emit a host-authored event with no output:

```python
session = await session_service.get_session(
    app_name=self._config.identity.app_name,
    user_id=self._config.identity.user_id,
    session_id=session_id,
)
if session is None or not invocation_id:
    logger.warning("Specification output diagnostic could not be appended.")
    return
await session_service.append_event(
    session=session,
    event=Event(
        author="specification_output_validator",
        invocation_id=invocation_id,
        state={"specification_output_diagnostic": diagnostic},
    ),
)
```

Use module-level `logger: logging.Logger = logging.getLogger(__name__)`. Optional diagnostic enrichment failures must emit that fixed warning plus safe attempt identity, without `str(error)` or `exc_info`, and preserve the original typed business failure. Do not catch cancellation. Existing infrastructure failures before a known output failure retain their current classification. Do not claim successful diagnostic persistence when append fails.

- [ ] Extend `_system` with optional injected `session_service: BaseSessionService | None = None`, defaulting to its existing in-memory service. Inspect diagnostic events from the same attempt fingerprint. Assert a nonempty invocation ID equals the original failing leaf event, diagnostic event `output is None`, no candidate, correct business failure, and sentinel absence from messages/diagnostic/log output.
- [ ] Add an async session-service test double that fails only when appending `specification_output_diagnostic`. The real runner must still return/persist `INVALID_SPECIFICATION_PAYLOAD`, make no extra model call, and issue the fixed safe warning. Original user/error events can still persist.
- [ ] Exercise persistence against a disposable ADK SQLite trace database under `tmp_path` as well as in-memory sessions. Never point this test at runtime databases. Read the final event back to verify correlation and serialization, not just a mocked append call.
- [ ] Test and document the precedence limit: when post-call source/authority revalidation overrides a generated-output error with obsolete/replayed status, that status wins. This helper enriches typed output failures that reach the runner; it does not promise an enrichment event for every cancellation or superseding host failure. Existing safe ADK leaf errors remain available when persisted.
- [ ] Run `uv run --no-sync --offline pytest tests/adapters/test_adk_specification_authoring_failures.py tests/adapters/test_specification_output.py -q`.

## Task 3: Make API, CLI and UI distinguish terminal failure from uncertain outcome

**Interfaces:** Existing `TransitionResult`, existing error allowlist, existing `specificationStructuringFailureMessage(binding, error, refreshed)`. No new codes or routes.

**Files:** frontend and three transport/presentation test files in the map. API/CLI implementation changes are unnecessary unless tests identify a concrete serialization defect.

- [ ] Expand the UI's definitive terminal set without changing the existing network-uncertainty branch:

```javascript
const terminalCodes = new Set([
    'SPECIFICATION_PRODUCER_FAILED',
    'INVALID_SPECIFICATION_PAYLOAD',
    'UNSUPPORTED_SPECIFICATION_SCHEMA',
    'SPECIFICATION_OUTPUT_INCOMPLETE',
]);
const terminalFailure = error?.status === 409 && terminalCodes.has(error?.code);
```

Keep the prior-candidate versus registered-source wording driven by `binding.mode`. Known terminal failures say no new candidate was produced; unknown/network outcomes still instruct the user to verify current state.

- [ ] Add this test with the existing frontend VM harness and parameterize the terminal codes above; separately test a 503/network error remains uncertain:

```javascript
test('known invalid Specification output has a definitive failure message', () => {
    const context = loadFrontend();
    const message = context.specificationStructuringFailureMessage(
        { mode: 'initial' },
        { status: 409, code: 'INVALID_SPECIFICATION_PAYLOAD', message: 'Unknown relation endpoint: RISK.missing-source.' },
        true,
    );
    assert.match(message, /No new candidate was produced/);
    assert.match(message, /RISK\.missing-source/);
    assert.doesNotMatch(message, /outcome is uncertain/);
});
```

- [ ] Extend the existing Specification API/CLI stubs to return `INVALID_SPECIFICATION_PAYLOAD` with `safe_message = "Specification structurer returned an invalid v2 payload."`. Keep their existing route arguments and CLI setup. Assert the exact existing envelopes:

```python
assert response.status_code == 409
assert response.json()["detail"]["error"]["code"] == "INVALID_SPECIFICATION_PAYLOAD"
assert response.json()["detail"]["error"]["message"] == safe_message
assert exit_code != 0
cli_payload = json.loads(capsys.readouterr().out)
assert cli_payload["error"]["code"] == "INVALID_SPECIFICATION_PAYLOAD"
assert cli_payload["error"]["message"] == safe_message
```

- [ ] Run `node --test tests/test_workflow_position_display.mjs`, then `uv run --no-sync --offline pytest tests/adapters/test_api_workflow_domain.py tests/adapters/test_cli_workflow_domain.py -k 'specification or structuring' -q`. Persistence evidence comes from the real-runner tests, not these transport stubs.

## Task 4: Clarify the primary prompt, bind provenance, and document the verified scope

**Interfaces:** Keep the existing single-prompt loader/hash convention and all frozen input/candidate contracts. Producer version becomes `1.0.2`; prompt version becomes `agileforge.specification-structurer.prompt.v3`.

**Files:** primary prompt, contract constants, agent tests, new operator document.

- [ ] Add these paragraphs after the current completeness paragraph in `specification_author.txt`:

```text
When the registered source explicitly defines typed item IDs, preserve those IDs and their item types. Every relation endpoint must name an item included in the returned payload. Preserve explicit source relations; do not remove a relation to conceal a missing item.

Keep historical implementation facts distinct from target requirements. Preserve a historical item's identity and supported information or behavior when the source requires it. Use INFORMATIVE for a purely descriptive historical fact, and preserve any normative transition or preservation obligation with its actual force. Historical storage does not become a target storage mandate. Supply the verification and acceptance fields required by the schema without inventing new product obligations. Do not silently replace a historical ID with a target ID.
```

Keep the existing amendment/removal declarations and full-result instructions. Explain in the operator document that these paragraphs reduce ambiguity and are not a guarantee of successful or semantically complete generation.

- [ ] Update the two version constants. Compute `SPECIFICATION_STRUCTURER_PROMPT_HASH` from the actual modified primary file with the existing `compute_specification_structurer_prompt_hash` algorithm (lowercase, collapse whitespace, UTF-8 SHA-256, `sha256:` prefix). Keep `adapters/adk/prompts/specification_author.py`'s existing single-prompt hash guard; no bundle mechanism.
- [ ] Update prompt provenance assertions to the computed digest/version. Keep historical recorded fixture envelopes unchanged. Validate that changing primary instructions changes the hash and that production advertises the same nested output schema and generation limits. A prompt-string assertion proves instruction presence only; fake-model tests cannot prove the prompt improves generation quality.
- [ ] Write `docs/testing/SPECIFICATION-OUTPUT-RECOVERY.md` with the four error meanings, safe diagnostic fields, attempt/session/invocation lookup, source-byte guarantees, and one-dispatch fail-closed behavior for classified output errors. A timeout is an execution failure, not evidence that the provider was offline; retain current timeout classification in Phase 1 and do not misdescribe it as a proven network outage.
- [ ] Include the architecture: external `to-spec` preparation -> registered source -> internal structurer -> human-accepted v2 Specification -> Backlog -> Roadmap. No synchronization of the external Markdown format into Roadmap, mandatory typed headings, or new human source-registration review.
- [ ] Run the focused Python tests from Tasks 1-2 plus `tests/adapters/test_adk_workflow_runner.py`, the Task 3 transport tests, and the frontend Node test. Apply repository-required lint/type gates to changed files. No live end-to-end campaign or provider-backed test belongs to this phase.
- [ ] Review the diff for no source edits, no validator relaxation, no raw-response persistence, no hidden retries/budget increases, no automatic acceptance, and no Roadmap changes. Record reproduction failure-before and passing-after evidence, new prompt identity, and the outstanding live-verification status.

## Phase 2: Recovery design and evaluation gate

Do not implement the earlier Markdown-heading parser or additive repair subsystem as part of Phase 1. Its limitations require an explicit design choice. Preserve the original goal of handling invalid output deliberately; accurate error messages alone do not provide automatic recovery.

The smallest next evidence step after Phase 1 is an authorized attempt against the unchanged registered P&ID source, with the improved diagnostics. This is an operational check, not a causal experiment establishing why attempt 6 failed.

If automatic recovery is pursued, compare these approaches before selecting one:

| Approach | Required evidence / unresolved tradeoff |
| --- | --- |
| Keep classified failure and explicit operator retry | Lowest implementation scope; operator still bears recovery, and a fresh response can differ semantically. |
| One whole-candidate correction using frozen source plus validation findings | Allows global consistency review, but can drop or weaken previously generated requirements. Needs preservation checks, full-source traceability review, and a bounded call/budget policy. |
| One additive missing-item patch | Mechanically preserves existing items/edges, but can duplicate a renamed concept or split obligations; format-dependent eligibility is not a semantic proof. |

Any recovery design must specify:

1. An explicit total wall-clock/token/call budget, cancellation behavior and retry semantics. About 12 seconds remaining in the observed P&ID attempt is not an adequate assumed repair budget. A larger or separate budget must be authorized and recorded, not silently reset inside a wrapper.
2. Immutable frozen input and exact lineage, pre/post-call source/authority checks, no intermediate candidate, and fail-closed final validation.
3. Preservation of approved requirements, historical facts, normative force and relations. Closed graphs and matching IDs alone do not prove this; include human semantic review.
4. Diagnostic retention for primary, correction and timeout outcomes without leaking raw private content. A correction timeout must not hide the previously observed validation failure from investigation.
5. Offline tests for second-invalid output, dropped requirements, duplicates/renames, source drift, cancellation, crash replay, and a strict per-invocation dispatch bound.
6. Representative authorized evaluation inputs before claims of reliability. A single successful P&ID retry neither proves prompt-only prevention nor establishes that recovery is unnecessary.

Phase 2 requires a concrete reviewed design and separate implementation authorization. Do not declare a preferred architecture proven from the missing historical response.

## Separately authorized P&ID verification

1. Resolve the intended checkout/runtime/profile and record commit, producer version, prompt hash, model, output-token limit, timeout, and effective retry policy. Do not conflate the production `uvicorn api:app` process with a development launcher profile. Obtain authorization before runtime changes or provider calls.
2. Reconfirm source registration and frozen bundle identities without edits. Create only a newly authorized workflow attempt; never rewrite attempt 6.
3. Make one authorized Structure Specification request. Correlate its business attempt, trace session/invocation, dispatch count, diagnostic event and final candidate/outcome.
4. If a candidate exists, review all 50 explicit source IDs, 24 relations, 35 user stories and their transition/preservation obligations. Resolve omissions, semantic duplicates, weakened requirements and accidental historical-to-target mandates before acceptance. Do not automatically edit the source or accept the candidate.
5. If it fails, use classified evidence to decide the next experiment; do not keep retrying. Any source-layout ablation must preserve every approved requirement and be separately authorized and compared against unchanged input. One success is a sample, not a root-cause proof.

## Completion criteria and handoff

- The real-leaf synthetic test demonstrates the original misclassification before the change.
- Invalid, unsupported and incomplete output receive the correct durable/transport classifications through the actual ADK path; genuine execution and host failures remain distinct.
- Existing incomplete handling is tested through both old and new callbacks, without assuming a regression that the code trace does not support.
- Sanitized diagnostics persist with the real attempt/session/invocation identity when the known output failure reaches the runner; enrichment failure cannot mask it.
- Known terminal UI failures are definitive; uncertain network outcomes remain uncertain.
- Existing valid output stays one call and byte-exact source registration handles LF and CRLF inputs.
- Primary prompt provenance is current; no claim that prompt wording guarantees graph validity or semantic completeness.
- Automatic recovery remains explicitly deferred for design; P&ID remains unverified until the authorized run and semantic review.

Recommended execution is `superpowers:subagent-driven-development`, with a review after each task; `superpowers:executing-plans` in the current task is also supported. Neither starts from this planning deliverable. No commits or branches until the user's restriction is separately changed.
