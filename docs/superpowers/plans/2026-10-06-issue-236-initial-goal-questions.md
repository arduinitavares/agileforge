# Initial Product Goal Questions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task after phase-2 approval. Use a fresh ultra implementer per task, spec and quality review per task, and a final whole-diff review. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make CLI and UI expose the same initial Product Goal questions and generated follow-ups, with explicit provenance and provider-free, read-only projections.

**Architecture:** Select effective questions in `DurableReadProjectionService.product_goal_status` from the existing validated Goal selection. Add one nullable projection field; CLI, standalone HTTP status, and the dashboard already forward this projection. Remove the UI's private starter array and render the new shared field.

**Tech Stack:** Python 3.13.15, SQLModel/SQLite, FastAPI, uv, pytest with pytest-socket, Ruff, browser JavaScript, and Node's existing test/VM harness.

**Spec:** [Issue #236 and triage comment](https://github.com/arduinitavares/agileforge/issues/236), the user's phase-1 acceptance criteria, and the contract/design below. This is a bounded change to an existing flow; no separate architectural spec is needed.

**Status:** Phase 2 complete and uncommitted. All external review changes and missing regressions were incorporated before execution. Both tasks passed independent spec/quality review; the fresh final Astra ultra whole-diff review approved with no actionable findings. Integrated verification: 48 focused Python tests and 269 frontend tests passed; Ruff check/format, syntax, and whitespace checks passed. Real desktop e2e was platform-skipped on macOS. Base: detached master `36efaff3bc17cf45e30888fe433bc8630df34e0d`. Ledger and exact evidence are retained outside the repository at `/private/tmp/agileforge-236-sdd.ZMDdAo`.

## Global Constraints

- Preserve `latest_questions`: questions from the last turn of the selected durable interview chain, or `[]` when that chain has no turns.
- No fabricated transcript turns, workflow-state writes, provider invocation, new persistence, or database migration during status reads.
- Use disposable in-memory/tmp databases only; never use home profiles/DBs or `agileforge-*` Docker resources.
- macOS: use `uv run --frozen pytest` with the repository's test guard bypass. Do not invoke native product/launcher processes or a bare/user-level `agileforge`.
- Git remains read-only: no commits, branches, stash, push, PR, or GitHub comment. Leave authorized changes for the orchestrator to commit.
- Surgical scope: no selector, graph, provider-output, interview-input, or unrelated UI refactors. Keep typing and path-banner conventions.
- Before dependency-behavior decisions, use `find-docs`; keep documentation queries free of repository code and private data.
- All phase-2 subagents must explicitly select reasoning effort `ultra`. Use `gpt-6.1-sol` for fresh implementers/spec checks and `gpt-6-astra` for independent quality/final review, with fresh scoped context. Disclose model, effort, context, and reason before each delegation. No descendants; the coordinator owns the four-agent ceiling and one-active-Astra limit. Inspect usage windows when exposed; missing windows remain unknown.

## Root Cause and Phase-1 Evidence

| Evidence on this master | Consequence |
| --- | --- |
| `frontend/project.js:902–937`, specifically `:916–923` | The interview panel uses persisted `latest_questions` when nonempty, otherwise supplies three local starter prompts. |
| `services/read_projections.py:2541–2641`, specifically `:2574–2578` | The shared status exposes only questions from the selected transcript's last turn. No turn means `latest_questions=[]`. |
| `cli/main.py:984–985` | `goal status` forwards the durable projection without adding prompts. |
| `api.py:1131–1135` and `:985–986` | Standalone Goal status and the dashboard Goal slot already use that same projection. |
| `workflow/definitions/product_goal.py:310–337`, `:414–468` | Revisions and next Goals select their own chains. An outcome's `GOAL_RESOLVED` status does not prevent the next interview. |

A throwaway reproduction lives outside the repository at `/private/tmp/agileforge-236.a3fGXX/test_issue236_reproduction.py`. It seeds an accepted Vision in a disposable in-memory database, injects real durable reads into `cli.main.main`, and feeds the captured CLI data into the actual frontend VM renderer. Production application construction and frontend fetch are forbidden; CLI runtime fencing/logging are replaced by test-only no-ops, and the repository conftest supplies the host guard bypass and database isolation. pytest-socket blocks Python network sockets; the Node renderer has no network path. This verifies adapter/renderer behavior, not a real Linux product process.

Observed output:

```text
INITIAL cli.latest_questions=[]
INITIAL ui.questions=["What valuable outcome should this Project achieve next?", "What observable result will prove success?", "What boundary keeps this Goal focused?"]
INITIAL effective_questions_field_present=False
FOLLOWUP cli.latest_questions=["Which boundary should the next outcome respect?"]
FOLLOWUP ui.questions=["Which boundary should the next outcome respect?"]
READS database_rows_unchanged=true; production_factory_forbidden=true; sockets_disabled_by_pytest=true
1 passed, 4 warnings in 5.52s
```

The reproduction's passing result confirms the current discrepancy, not a fix. It compares every database table before/after each read and repeats both states. The temp probe is optional evidence only; committed tests below are the gate of record and do not depend on this path. Loading the outside-repo test required `PYTHONPATH="$PWD"` for the explicit conftest plugin; this follows [pytest's documented import-path behavior](https://docs.pytest.org/en/stable/explanation/pythonpath.html).

Baseline checks executed with `UV_PROJECT_ENVIRONMENT=/private/tmp/agileforge-236.a3fGXX/venv`:

- `uv run --frozen pytest tests/services/test_durable_product_definition_projections.py -k 'goal or new_project_has_empty_durable_interview_reads' -q`: **10 passed, 95 deselected**.
- `node --test tests/test_product_goal_interview_ui.mjs`: **3 passed**.

The missing initial parity regression explains why the existing suites pass.

## Designs Considered

1. **Chosen: nullable `effective_questions` object with `questions` and `source`.** The projection chooses once; both consumers receive the same prompts and provenance. The object keeps its content and provenance together and avoids changing persisted-question semantics. Trade-off: it adds a nested object to the public success data, and explicit null semantics need documenting.
2. **Effective list plus sibling provenance field.** Also meets the acceptance criteria and is slightly flatter. It requires two coordinated fields; partial fixtures or consumers can separate the list from its source. No benefit here outweighs the clearer single object.
3. **Separate `initial_questions` with starter provenance, leaving generated questions in `latest_questions`.** Preserves both sources explicitly, but every consumer must repeat precedence and lifecycle selection. It makes future CLI/UI divergence easier and exposes two sources where one effective view is requested.

Overloading `latest_questions`, adding synthetic turns, persisting starter prompts, or generating questions on read are excluded by the requirements.

## Proposed Projection Contract

Every successful Product Goal status `data` includes the additive field:

```json
{
  "effective_questions": {
    "questions": ["What valuable outcome should this Project achieve next?"],
    "source": "builtin_starter"
  }
}
```

The example shows the shape; the full starter list is exactly, in order:

1. `What valuable outcome should this Project achieve next?`
2. `What observable result will prove success?`
3. `What boundary keeps this Goal focused?`

`source` is exactly `builtin_starter` or `generated`. `effective_questions=null` means there is no healthy Goal interview context in this projection; it does not change graph availability. The field is present, including as null, in both conflict success branches. Existing error envelopes such as `PROJECT_NOT_FOUND` remain unchanged.

| Selected state | `effective_questions` | Existing `latest_questions` |
| --- | --- | --- |
| Accepted Vision; no selected Goal turns | Full starter list, `builtin_starter` | `[]` |
| Eligible interview; selected latest questions nonempty | Exact ordered latest list, `generated` | Same persisted list |
| Feedback/rejected Goal; no revision answer yet | Starters, `builtin_starter` | `[]` for the selected new revision |
| Resolved Goal; next Goal has no answer yet | Starters, `builtin_starter` | `[]` |
| Eligible interview; latest questions empty | Starters, `builtin_starter`, preserving today's UI fallback | Unchanged empty list |
| No accepted Vision, active accepted Goal, pending candidate review, or conflicting facts | `null` | Unchanged existing selection |

Derive interview context from the already selected facts: a nonconflicting accepted Vision, no active Goal, and either no candidate or a candidate with feedback/rejected decision. Apply candidate validation before deriving effective questions. Do not call the workflow graph to compute this field, use `stale_reason` as an eligibility gate, or scan historical turns independently of the selector. These questions describe the selected interview context; existing graph-advertised actions continue to govern whether the UI displays a response form.

Valid incomplete provider outputs already require nonempty questions (`services/contracts/product_goal.py:94–106`). Therefore `builtin_starter` can coexist with a nonempty selected transcript only for invalid history; retaining that fallback preserves behavior and does not repair invalid history or change the provider contract.

## File Map and Existing Contracts

- `services/read_projections.py`: own the typed starter tuple and effective selection inside the existing `product_goal_status(*, project_id: int) -> JsonObject` method. No new module/service is needed.
- `frontend/project.js`: render `projection.effective_questions.questions` in `productGoalPanelMarkup`; missing/null/malformed question-list shape yields an empty list, without inventing starters or consulting `latest_questions`.
- `tests/services/test_durable_product_definition_projections.py`: lifecycle, ordering, isolation, and existing complete dictionary contracts at `:2463–2472`, `:2932–2945`, `:3749–3771`.
- `tests/adapters/test_api_workflow_domain.py`: HTTP forwarding and complete empty-Goal contract at `:224–233`.
- `tests/adapters/test_cli_workflow_domain.py`: real `main(..., application=...)` forwarding with injected durable reads; existing Goal coverage at `:149` is parser-only.
- `tests/adapters/test_api_dashboard_bundle.py`: ensure the new field survives snapshot-bound dashboard reads as well as standalone status.
- `tests/test_product_goal_interview_ui.mjs`: behavioral VM tests and updated projection fixtures at `:53–67`, `:84–90`, `:106–114`.
- `tests/fixtures/product_goal_starter_questions.json`: shared exact starter object (text, order, and source) asserted by the Python projection test and pinned by the Node test.
- `tests/e2e/test_single_project_lifecycle_ui.py`: update `_goal_projection` at `:896–924` for starter/generated/null semantics so removing the UI fallback does not erase stubbed questions.
- `.github/workflows/ci.yml` and `tests/test_ci_contract.py`: register the existing Goal Node suite; structurally assert its command-line inclusion. It is currently absent from the frontend job at `:89–98`.
- `docs/agent-cli-manual.md`: add the existing `goal status` command to the Product Goal catalog and a short subsection defining this field, its provenance/null states, and persisted-only `latest_questions`.

No separate Goal API response model, projection schema version, or field-enumerating documentation was found. API endpoints return a dictionary and retain their existing envelopes. Historical Superpowers specifications do not enumerate this field; do not rewrite them. `cli/main.py`, `api.py`, DB models, and provider contracts require no production changes.

## Review Focus

- Feedback/rejected revisions must use their new selected chain; test starters before revision answers and generated questions afterward in Task 1.
- Resolved Goals must permit next-Goal starters despite `GOAL_RESOLVED`, and must not revive old questions; test both stages in Task 1.
- Missing Vision, active Goals, pending review, and both conflict exits, including Vision-selection conflict, must expose null; test these in Task 1, including dashboard pending-review null.
- Reads must not construct providers, write durable rows, fabricate turns, or re-evaluate the graph just to select questions; pin provider guards, complete row comparisons, and repeated reads in Task 1.
- The UI must honor the new source even when `latest_questions` disagrees, handle null/missing data without local fallback, and escape question content; test these in Task 2, including a null projection with a respond action that still renders the form and “No open questions recorded.”

## Task 1: Shared Selection and Provider-Free Transport Contracts

**Files:** Create `tests/fixtures/product_goal_starter_questions.json`. Modify `services/read_projections.py`, `tests/services/test_durable_product_definition_projections.py`, `tests/adapters/test_cli_workflow_domain.py`, `tests/adapters/test_api_workflow_domain.py`, `tests/adapters/test_api_dashboard_bundle.py`, and `docs/agent-cli-manual.md`.

**Interfaces:** Consume `select_product_goal_interview_state(snapshot)` and the existing `latest_questions` local. Produce the nullable `data.effective_questions` contract above; the method signature and existing fields stay unchanged. CLI JSON, HTTP `data`, and the dashboard Goal slot forward this same contract without production adapter changes.

- [x] **RED:** Add `test_accepted_vision_without_goal_turns_exposes_starter_questions_read_only`. Reuse `_seed_vision_candidate(engine, decision="accepted")`; assert the exact starter object, empty transcript/latest list, no active/candidate, deterministic repeated results, and identical `durable_rows(engine)` before/after. Reuse the existing raw-row helper in `tests/adapters/sprint_retry_fixtures.py`.
- [x] Add the shared JSON fixture with the complete exact starter object above. The projection test must assert its real result against this fixture; Task 2's Node test must pin this same fixture's text/order/source and renderer output. Add `test_goal_starter_questions_are_a_fresh_copy`: mutate a returned starter list, re-read, and assert the original fixture remains intact.
- [x] Add `test_generated_goal_questions_take_precedence_over_starters`: seed a valid linked sequence with `_add_goal_turn` / `_GoalTurnSeed`, and assert the final selected questions' exact text/order and `generated` provenance, not an earlier turn or starters. Preserve exact transcript assertions in the existing incomplete-turn test.
- [x] Add `test_reopened_goal_interview_exposes_starters_until_generated_followups`, parameterized for feedback, rejection, and resolution. Assert starters before the selected new chain has a turn, then the new chain's generated list; reuse `_seed_goal_candidate` / `_resolve_goal`. Extend existing revision/next-Goal tests rather than duplicating their lineage assertions.
- [x] Add/extend `test_non_interview_goal_states_have_no_effective_questions` for missing Vision, active Goal, and pending review. Update the enumerated new-project, detached-conflict, and resolved-Goal dictionaries. Exercise Vision-selection conflict (`workflow/definitions/product_goal.py:419–420`), Goal-lineage conflict, and candidate projection validation failure with an otherwise valid selection. Use a narrowly patched candidate serializer for that second return if durable fixtures cannot isolate it. Keep not-found error contracts unchanged.
- [x] Add `test_goal_status_cli_preserves_effective_questions_projection` for initial and generated states with real durable reads and fixture seeds. Inject a reads-only application into `main(["goal", "status", "--project-id", ...], application=...)`; capture/parse JSON. Forbid `production_application`, bypass runtime fencing/logging only in the test, and retain conftest's platform bypass. Assert the explicit expected effective object and exact equality of CLI JSON `data` to `reads.product_goal_status(project_id=project_id)["data"]`, exit 0, and unchanged rows after repeated reads. CLI performs no selection.
- [x] Add `test_goal_status_endpoint_exposes_starter_and_generated_question_provenance` using `_StatusReadApplication`, patched `api_module._application`, and TestClient. Assert the explicit effective object and equality with the durable projection; verify unchanged rows. Update the existing empty-Goal HTTP contract with `effective_questions: None`.
- [x] Add `test_dashboard_goal_questions_match_standalone_status`, parameterized for initial, generated, and pending-review states. Assert `effective_questions is None` in the pending-review Goal slot. Reuse the dashboard `_application(engine)` harness; assert the effective object and complete Goal slot equality to standalone status, retaining the existing one-snapshot-load contract and no additional graph evaluation for questions.
- [x] Run these four focused commands before changing production code; require failures on the absent field, not fixture/setup errors:

```sh
uv run --frozen pytest tests/services/test_durable_product_definition_projections.py -k 'goal or new_project_has_empty_durable_interview_reads' -q
uv run --frozen pytest tests/adapters/test_cli_workflow_domain.py -k 'goal_status' -q
uv run --frozen pytest tests/adapters/test_api_workflow_domain.py -k 'goal_status or vision_and_goal_status' -q
uv run --frozen pytest tests/adapters/test_api_dashboard_bundle.py -k 'goal_questions or matches_standalone_reads' -q
```

- [x] **GREEN:** Add `_PRODUCT_GOAL_STARTER_QUESTIONS: tuple[str, ...]` near projection constants. Compute a freshly allocated `effective_questions: JsonObject | None` inside `product_goal_status` after validated selection/candidate handling. Add null to both conflict exits; do not modify how transcript or `latest_questions` are derived.
- [x] Write the focused manual subsection and add `goal status` to its existing catalog. Document the closed source set `{builtin_starter, generated}`, null behavior, distinction from persisted questions, and invalid-history-only coexistence of starters with a nonempty transcript. Do not test prose or documentation with regex.
- [x] Run the same four pytest commands, then `uv run --frozen ruff check services/read_projections.py tests/services/test_durable_product_definition_projections.py tests/adapters/test_cli_workflow_domain.py tests/adapters/test_api_workflow_domain.py tests/adapters/test_api_dashboard_bundle.py` and `uv run --frozen ruff format --check services/read_projections.py tests/services/test_durable_product_definition_projections.py tests/adapters/test_cli_workflow_domain.py tests/adapters/test_api_workflow_domain.py tests/adapters/test_api_dashboard_bundle.py`. Require successful exits and per-task spec/quality review before Task 2.

## Task 2: UI Uses Shared Questions and CI Runs Its Regression Suite

**Files:** Create `tests/test_product_goal_lifecycle_stub.py`. Modify `frontend/project.js`, `tests/test_product_goal_interview_ui.mjs`, `tests/e2e/test_single_project_lifecycle_ui.py`, `.github/workflows/ci.yml`, and `tests/test_ci_contract.py`. Consume Task 1's shared JSON fixture; do not create a second starter fixture.

**Interfaces:** Consume `projection.effective_questions?.questions` from Task 1. Preserve graph-advertised form/review/outcome gating and existing rendering/escaping.

- [x] **RED:** Update existing VM fixtures to include the new nullable/object field. Add cases for projected starters, generated follow-ups, null/missing/malformed list shape, and HTML-special-character questions. Intercept the existing `interviewFormMarkup` call to assert the exact questions passed; also assert rendered escaped output with the real function. Use deliberately different `latest_questions` values to prove the new field controls rendering. Assert absent effective data does not recreate the former starters. Avoid source regex checks, documentation matches, or arbitrary exact counts.
- [x] Load `tests/fixtures/product_goal_starter_questions.json` in the Node suite, pin its exact object including source/text/order, and assert it reaches the renderer unchanged. Add the null-effective-questions/respond-action case: “No open questions recorded.” is visible and the response form remains available.
- [x] Add browser-free behavioral tests in `tests/test_product_goal_lifecycle_stub.py`, importing `FakeLifecycle` without invoking the browser fixture: assert initial shared starters, generated questions with a seeded incomplete turn, and null for no Vision/pending review/active Goal. Run `uv run --frozen pytest tests/test_product_goal_lifecycle_stub.py -q` before changing the stub, then update e2e `_goal_projection` for these semantics and re-run. This separate module avoids the e2e file's macOS-wide skip marker.
- [x] Retain behavioral assertions that pending review, active Goal, and missing Vision show their existing panels and never produce a response form, using graph-consistent actions even if a fixture includes effective questions. Retain accepted Vision context and transcript rendering checks.
- [x] Extend `test_jobs_invoke_locked_repository_surfaces`: replace its focused frontend suite-list string assertion at `tests/test_ci_contract.py:250–259` with structural `shlex.split` checks for the YAML frontend command's `node --test` prefix and required existing suite paths plus the Goal suite. Assert inclusion without pinning list length, ordering, or prose; keep unrelated CI checks untouched.
- [x] Run `node --test tests/test_product_goal_interview_ui.mjs` and `uv run --frozen pytest tests/test_ci_contract.py -k 'jobs_invoke_locked_repository_surfaces' -q`. Confirm failure for the old UI selection and absent Goal suite.
- [x] **GREEN:** Replace only the question-selection block in `productGoalPanelMarkup` with a defensive array read from the shared object; remove the UI starter literals and `latest_questions` fallback. Keep normal rendering and action gating. Append `tests/test_product_goal_interview_ui.mjs` to the existing CI frontend command.
- [x] Run those same commands plus `node --check frontend/project.js`, `uv run --frozen ruff check tests/test_ci_contract.py tests/test_product_goal_lifecycle_stub.py tests/e2e/test_single_project_lifecycle_ui.py`, and `uv run --frozen ruff format --check tests/test_ci_contract.py tests/test_product_goal_lifecycle_stub.py tests/e2e/test_single_project_lifecycle_ui.py`. Target `uv run --frozen pytest tests/e2e/test_single_project_lifecycle_ui.py::test_desktop_human_single_lifecycle -q`; on macOS its explicit platform marker skips before any real process is started. Record this as skipped/not run, not browser verification. Obtain per-task spec/quality review.

## Phase-2 Final Acceptance and Handoff

- [x] In-repo tests are the acceptance gate of record: re-run each task's focused pytest/Node commands on the integrated result. The shared fixture links Python's actual projection assertions to Node's exact starter-object and renderer assertions; generated-state regressions enforce follow-up precedence independently. No acceptance command may depend on the temp reproduction script.
- [x] Run `uv run --frozen ruff check services/read_projections.py tests/services/test_durable_product_definition_projections.py tests/adapters/test_cli_workflow_domain.py tests/adapters/test_api_workflow_domain.py tests/adapters/test_api_dashboard_bundle.py tests/test_ci_contract.py tests/test_product_goal_lifecycle_stub.py tests/e2e/test_single_project_lifecycle_ui.py` and `uv run --frozen ruff format --check services/read_projections.py tests/services/test_durable_product_definition_projections.py tests/adapters/test_cli_workflow_domain.py tests/adapters/test_api_workflow_domain.py tests/adapters/test_api_dashboard_bundle.py tests/test_ci_contract.py tests/test_product_goal_lifecycle_stub.py tests/e2e/test_single_project_lifecycle_ui.py`. Avoid unrelated full-gate/native launcher/Docker work. Temp reproduction is optional supplemental evidence only.

- [x] Obtain a fresh independent Astra ultra whole-diff review of requirements, changed source, tests, and actual verification evidence. Address findings within scope and rerun only affected checks. Do not use implementation-team review as the independent quality gate.
- [x] Inspect `git diff --name-only`, path-specific diffs, and status. Confirm only the planned files changed; retain the plan and evidence, leave everything uncommitted, and report the implemented contract with tested scope.

## Behaviors Changed and Preserved

**Changes:** One additive nullable field in successful shared Goal status, CLI JSON, standalone Goal HTTP status, and dashboard Goal data; explicit starter/generated provenance; UI question sourcing from that field; focused contract documentation and CI coverage for the existing Goal Node suite.

**Preserved:** All existing fields and wrappers, `latest_questions` meaning, transcript contents, DB schema and stored bytes, provider-output rules, graph decisions and action availability, Goal numbering/revision lineage, review/outcome controls, accepted Vision context, status error behavior, and provider-free/no-write reads.

## Reviewer Decisions and Risks

- Approved: nested public object, closed source set, and null when the selected interview is unavailable (no Vision, active Goal, pending review, either conflict exit); not-found errors unchanged.
- Approved: starters for reopened revisions and next Goals until the newly selected chain has a turn; `GOAL_RESOLVED` must not suppress them.
- UI and API changes must ship together. Cached old frontend code still has its historical fallback; new frontend code receiving an old projection displays no questions. No compatibility fallback is planned because it would duplicate selection again.
- Scope includes registering the existing Goal Node suite in CI so the new regression runs continuously. No broader CI refactor is proposed.
- No blocking product/dependency questions remain. Phase-2 execution is authorized. Keep the SDD ledger outside the repository and paste it in the final report; no commits/branches/stash or GitHub actions.
