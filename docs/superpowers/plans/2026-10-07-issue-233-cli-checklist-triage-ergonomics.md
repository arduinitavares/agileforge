# Issue #233 CLI Checklist and Triage Ergonomics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. External Opus 5.5 review returned APPROVE WITH CHANGES; the user approved phase two after incorporating the changes recorded below. Steps use checkbox (`- [x]`) syntax for tracking. No commits are authorized.

**Goal:** Accept explicit 1-based checklist results and inline no-impact triage summaries without changing completion validation, persisted semantic payloads, or replay identity.

**Architecture:** Normalize convenience inputs in the CLI before constructing the existing application requests. Resolve checklist indexes against ordered canonical Task metadata through the existing read projection; the completion service still enforces exact coverage. Inline triage constructs the same JSON object as the equivalent file, then uses the existing application, workflow, persistence, and receipt path.

**Tech Stack:** Python 3.13.15, argparse, Pydantic, SQLModel/SQLite, pytest, uv, ty, Ruff. No new dependencies.

**Spec:** [Issue #233 and its triage comment](https://github.com/arduinitavares/agileforge/issues/233), plus the user's phase-one task requirements. The design decisions below serve as the requested non-interactive design artifact; no separate spec or implementation is requested in this phase.

## Global constraints and phase boundary

- Phase two is now authorized after applying the external plan-review changes below. Do not commit, branch, stash, push, open a PR, or comment on GitHub.
- Preserve the requested subagent-driven execution path: fresh ultra implementer per task, specification and quality review per task, and final independent whole-diff review.
- Use only uv for Python project commands. Native macOS pytest is supported by `tests/conftest.py`; the real launcher/runtime is Linux-only. Never invoke a bare/user-level `agileforge` or use real home profiles/databases or `agileforge-*` Docker resources.
- Verification must be provider-free, with disposable databases, test model configuration, and sockets disabled by the repository pytest configuration. Runtime fence roots and diagnostic files must also stay in temporary directories.
- Every Codex child launched in this phase must explicitly use `reasoning_effort="ultra"`. Phase-two implementers are fresh ultra agents per task as requested. Apply the current user model-selection policy, explicit disclosures, descendant controls, one-Astra limit, and four-workstream ceiling; do not launch nested Ultra coordinators or silently substitute settings.
- Preserve important explicit annotations and any existing first-line repository-relative path banners. Do not add unrelated formatting, refactors, dependencies, schema migrations, or UI changes.
- Tests assert typed parser structure, captured semantic requests, observable errors, persisted records, and replay behavior. Do not add documentation/prose regex tests or brittle fixed catalog/suite counts.

## Current behavior and root cause at `8c9e693a8820784f509c7c12f9f3942850392be0`

| Boundary | Evidence | Consequence |
| --- | --- | --- |
| Checklist lexical parser | `cli/main.py:382-390` partitions on the first `=` and trims key/result. | `1=met` becomes literal `("1", "met")`; missing `=` fails before Task context is loaded. Values containing further `=` remain supported. |
| Checklist source selection | `cli/main.py:441-455`, `719-740` select exactly one item/file source and reject duplicate keys/repeated checklist files. | No task-aware index mapping exists. JSON files preserve keys containing `=`. |
| Semantic completion submission | `cli/main.py:2259-2278`; `services/application.py:2166-2173`, `4070-4126`. | The CLI sends an exact-text dictionary into the existing nonblank request schema. |
| Authoritative coverage check | `services/task_execution_service.py:338-355`. | Input keys must equal the executable checklist keys. The current error gives no valid-item list. |
| Ordered checklist source | `utils/task_metadata.py:57`, `78-85`; `services/read_projections.py:4272-4296`, `4350-4356`. | Canonical metadata retains order. The existing Task read includes historical `snapshot.tasks`, not just open tasks, and carries `metadata_json`. |
| Triage parser/dispatch | `cli/main.py:759-766`, `2328-2341`. | `--file` is mandatory for every impact; `--summary` is unavailable. |
| Current triage payload boundary | `services/application.py:2198-2203`, `4273-4327`; `workflow/requests/execution.py:76-90`; `services/agent_workbench/post_sprint_triage.py:202-212`, `246-288`. | The current workflow accepts a JSON object, closed impact choices (`none`, `backlog`, `specification`), and exact completed original/retry scope with a closure. It does not impose a closed set of payload keys. |
| Canonical persistence | `services/agent_workbench/post_sprint_triage.py:348-386`, `397-412`; `workflow/execution_integrity.py:843-848`. | Payload JSON and impact are canonicalized/hashed; original and retry persistence and events already exist. |
| Semantic replay/receipt identity | `services/application.py:4074-4089`, `4278-4289`; `services/node_attempt_replay.py:142-179`; `workflow/domain.py:438-451`. | Replay compares semantic fields plus project/actor/correlation identity before current decision selection. File path and argv syntax are absent from the receipt. |
| Command catalog | `cli/workflow_commands.py:164-173`, `219-224`; `tests/adapters/test_command_renderer.py:690-742`. | Completion advertises a lossless checklist file; triage advertises impact plus a file. Both remain useful for all supported inputs. |

The same triage module also contains an older structured `build_triage_payload` helper (`:112-199`) with different impacts and fields. The active CLI calls `record_post_sprint_triage_in_session` through the execution handler; it does not call that helper. Do not route inline input through it, relax its checks, or invent a schema migration to reconcile them.

### Provider-free reproduction and baseline

Throwaway harness and probes live outside the repository at `/private/tmp/issue233-probes/`; no product code or repository tests were edited. They use injected synthetic applications, temporary SQLite files, the existing pytest platform bypass, and an isolation plugin that redirects actual runtime fences and diagnostics to `tmp_path`.

Observed output on this master:

```text
parser: checklist_items=[("1", "met")]
index 1=met,2=met: exit 1; WORKFLOW_FACT_CONFLICT;
  Checklist result must cover every executable checklist item.
unknown exact-text item: exit 1; same message, no listed items
missing '=': exit 2; --checklist-item must be a nonblank KEY=VALUE pair.
triage --impact none --summary TEXT: exit 2; required: --file
triage --impact none --file containing {"summary": TEXT}: exit 0;
  captured canonical_payload={"summary": TEXT}
```

The two throwaway probes passed. The existing `tests/adapters/test_cli_task_completion.py` and `tests/adapters/test_cli_workflow_domain.py` baseline passed **184 tests, 4 deprecation warnings** after isolation. The first run had 64 failures caused by a home runtime fence lock, not issue behavior; its results do not establish a product regression. Logs: `/private/tmp/issue233-repro.log`, `/private/tmp/issue233-isolated-baseline.log`, and `/private/tmp/issue233-baseline.log`.

## Designs considered and selected

1. **CLI normalization with per-item indexes — selected.** Translate indexes to exact text before constructing `CompleteTaskRequest`, and translate inline triage to the existing payload. This keeps transport syntax out of request hashes and persistence and retains existing domain validation. It requires a bounded metadata read for index resolution and for malformed-input diagnostics.
2. **Add ordinal/all-met fields to application and workflow request models.** This could normalize inside a transaction, but would expand public semantic contracts, replay handling, API consumers, and possibly receipt compatibility for a CLI ergonomics issue. Reject for this scope.
3. **Add only all-met, or add it alongside indexes.** All-met is explicitly optional in the triage scope, while the issue acceptance criteria require indexes. Defer it: per-item results meet the criteria and make each resolution explicit. No `--all-checklist-met` flag is planned.

Do not replace advertised command templates with the shortcuts. Triage templates cannot assume `impact=none`, and checklist files remain the general lossless form. Update help and the manual; strengthen structural catalog tests while preserving rendered templates and projection fields.

## Chosen input contracts

### Checklist items

- Sources stay mutually exclusive: repeat `--checklist-item KEY=VALUE` or pass one `--checklist-file PATH`. Files always contain exact-text keys; never reinterpret numeric file keys as indexes.
- Preserve the first-`=` split and existing key/result trimming. Results remain arbitrary nonblank strings, including `met`, `failed`, and `expected=actual`; do not introduce a `met` enum or infer Task acceptance from a result word.
- Retain context-free lexical checking at parse time so malformed entries fail before runtime access/application creation. Enrich a checklist syntax error only through a safely available injected application/read, and only if that read succeeds; otherwise preserve the original syntax error, exit 2, and no item list. Diagnostic enrichment must never invoke the production application or replace syntax failure with a read/runtime failure. Valid entries retain parsed key/result tuples.
- Load checklist context using `parse_execution_instance_key` (must be Task kind) and `application.reads.sprint_task_show(project_id=..., task_id=...)`, then strictly parse `data.task.metadata_json` with `parse_task_metadata`. Resolve order exactly as stored; never sort checklist text, query a database from the CLI, call a provider, or infer availability from this read.
- Ordinary nonnumeric exact-text entries and checklist files retain their current direct submission path. Fetch Task context when any supplied key is numeric-looking, or when enriching an item syntax/duplicate error. Here numeric-looking means a whole key shaped as a signed/unsigned integer or decimal, including Unicode decimal digits; this detects malformed ordinal candidates without interpreting ordinary prose as numbers. Authoritative exact-text/file coverage errors are enriched in the completion service.
- For a source containing any numeric-looking key, first test **complete literal-key coverage** against canonical checklist text, including the other supplied text keys. Also calculate a full valid index reading when possible: if both readings fully cover but produce different dictionaries, reject as ambiguous and recommend the file form; if dictionaries are identical, accept. Literal-first precedence otherwise preserves complete legacy literal semantics. When literal coverage is incomplete, require every key to be a canonical ASCII positive decimal index (`1`, `2`, ...), within bounds, and without a collision where a numeric literal checklist name denotes a different indexed item. Reject mixed text/index inputs or ambiguous collisions.
- A text-only source stays exact-text even if it is incomplete/unknown: the existing service rejects missing or extra keys. In an index source, resolve supplied valid entries only; never fill omissions. The existing service must still reject incomplete coverage.
- Reject zero, negative/signed, leading-zero, decimal, non-ASCII-digit, out-of-range, duplicate, and mixed ordinal inputs. Literal exact-key backward compatibility takes precedence when these strings are genuine checklist names and the whole payload covers them exactly; files remain the unambiguous escape hatch.
- Diagnostics use one shared capped formatter in the CLI and service: show at most 20 items in stored order, at most 160 displayed characters of each item text (159 characters plus `…` when truncated), then `… and N more` for omitted items. Prefix each shown item with its 1-based index. Preserve the service's existing coverage-error prefix, conflict code, exit 1, and durable rejection receipt semantics. Lexical/index/source errors return exit 2 and do not write receipts, evidence, logs of task execution, or Task state. Test both caps at the formatter and transport/service boundary.
- If Task identity/metadata cannot be safely read, report that failure without fabricating item names or falling back to another project/Task. The existing application remains responsible for original/retry scope, availability, concurrency, and completion validation.

Example with checklist `("Run focused tests", "Confirm A=B=C")`:

```text
--checklist-item 1=met --checklist-item 2=observed=A=B=C
=> {"Run focused tests": "met", "Confirm A=B=C": "observed=A=B=C"}
```

For checklist `("2", "1")`, `1=a,2=b` is rejected because its complete literal and indexed readings differ; equal values may produce identical mappings and are accepted. Checklist `("1", "2")` has identical readings and is accepted. For `("Run tests", "1")`, `1=met,2=met` is ambiguous and rejected; use a file. Complete unambiguous literal coverage retains legacy semantics.

### Triage payload

- Require exactly one source: one `--file PATH` or one `--summary TEXT`. Neither, both, or repeated occurrences of either are argument errors (exit 2), with no application mutation.
- `--summary` is permitted only with `--impact none`; backlog/specification still require `--file`. This is a CLI-only restriction, not a new domain/schema rule.
- Reject an empty or whitespace-only inline summary. For accepted summaries, preserve the original string, including surrounding whitespace, Unicode, quotes, newlines, and `=`. Use `.strip()` only to detect blank input.
- Inline input produces exactly `{"summary": TEXT}`: no schema version, provenance marker, argv, file path, impact field, default fields, or extra metadata in the payload. Project, instance, impact, actor, correlation ID, and idempotency key remain existing request fields.
- Preserve the current file JSON-object loader and all file payload contents for every impact. Do not add summary requirements or stricter validation to file input.
- Explicitly rejecting repeated `--file` tightens today's argparse last-value-wins behavior. This is intentional and must be documented and reviewed.

### Idempotency decision

Keep the existing caller-provided keys, semantic replay comparison, canonical request JSON, and hash algorithms unchanged. Normalize before calling the application. Exact text, indexed items, and checklist-file input with the same resolved mapping must replay under the same key; file and inline triage with the same object must do likewise. Different results, summary text, payload fields, impact, instance, project, actor, or correlation ID remain conflicts under a reused key.

A pre-upgrade rejected literal request such as `{"1": "met"}` is not semantically equal to its newly resolved index mapping. Pin the existing same-key conflict (not replay) with a durable receipt test. Manual guidance: use a fresh idempotency key after a pre-upgrade index attempt was rejected.

Do not trim accepted triage summaries or add input-source fields: either would make equivalent payloads differ. Mapping requires retained canonical metadata even after completion; prove restart and later-scope replay. If that existing projection cannot safely supply historical metadata, stop and revise this design rather than silently disabling replay checks.

## File map

| File | Responsibility |
| --- | --- |
| `cli/main.py` | Parse-time checklist syntax checks, optional capped error enrichment, Task-context lookup, exact/index normalization, triage source exclusivity, semantic request construction, CLI help. |
| `utils/task_metadata.py` | Small shared indexed checklist formatter; no metadata model/schema change. |
| `services/task_execution_service.py` | Enrich exact coverage mismatch diagnostics while retaining the exact-set check. |
| `tests/test_task_metadata.py` | Formatter behavior without parsing documentation prose. |
| `tests/test_task_execution_service.py` | Direct coverage-error behavior and unchanged metadata validation. |
| `tests/adapters/test_cli_workflow_domain.py` | Parser structure, semantic transport captures, malformed input and source errors; replace the affected help-prose assertions with structural assertions. |
| `tests/adapters/test_cli_task_completion.py` | Real completion persistence, rejection invariants, numeric edge cases, and cross-form restart replay. Inject `DurableReadProjectionService` into its synthetic application for index reads. |
| **New** `tests/adapters/test_cli_sprint_triage.py` | Temporary SQLite triage equivalence, receipt identity, restart replay, invalid-input invariants, and retry-bound coverage. |
| `tests/adapters/test_command_renderer.py` | Verify generated file templates still parse/dispatch and retain their semantic required inputs, with no fixed catalog count assertions added. |
| `docs/agent-cli-manual.md` | Checklist/index rules, diagnostics, inline/file examples, byte-preserving equivalence, and replay guidance. |

No change is planned to `cli/workflow_commands.py`, `services/application.py`, workflow request classes, database models, repositories, schemas, routing, or frontend files. Read these as integration evidence, not edit targets.

## Review focus

1. Numeric literal checklist names: preserve complete legacy exact-key payloads and reject genuinely ambiguous ordinal interpretation (Task 2).
2. Item text/results containing `=` and Unicode: keep file transport lossless and indexed results equivalent (Tasks 1–2).
3. Completion/retry replay after state moves: resolve retained Task metadata without selecting a different attempt or requiring an open Task (Task 2).
4. Summary whitespace/Unicode/newlines: compare exact payload and receipt semantics, not just displayed output (Task 3).
5. Invalid input and scope: distinguish argument failures with no writes from durable service rejection receipts; never expose another project's checklist (Tasks 1–3).

## Task 1: Add task-aware checklist diagnostics without relaxing validation

**Files:** Modify `cli/main.py:382-455,719-740,2259-2278`, `utils/task_metadata.py`, `services/task_execution_service.py:338-355`, `docs/agent-cli-manual.md:194-230`; test `tests/test_task_metadata.py`, `tests/test_task_execution_service.py`, `tests/adapters/test_cli_workflow_domain.py`, `tests/adapters/test_cli_task_completion.py`.

**Interfaces:** Add `format_checklist_items(checklist_items: tuple[str, ...]) -> str` in `utils/task_metadata.py` using the exact caps above. In `cli/main.py`, add `_task_checklist_items(args: argparse.Namespace, application: _Application) -> tuple[str, ...]`; change `_task_checklist_result(args: argparse.Namespace, application: _Application) -> dict[str, str]`. Keep `_parse_checklist_item(value: str) -> tuple[str, str]` as the context-free parse-time checker and file helpers' existing semantics. The context read produces no mutation or new public fields.

- [x] **Red:** Add `test_completion_coverage_error_lists_valid_items` for missing/extra/mismatched keys and internal-spacing mismatches against real valid metadata. Assert service exit 1, unchanged coverage prefix, and stored-order index/text pairs; assert completion/evidence/business rows remain unchanged except the existing rejection receipt. Add formatter cases with Unicode and `=`, both item-count and per-item-character caps, and boundary coverage of capped service/CLI messages.
- [x] **Run:** `uv run --frozen pytest tests/test_task_metadata.py tests/test_task_execution_service.py tests/adapters/test_cli_task_completion.py -q` with the isolation setup below. New diagnostic assertions must fail because master omits the list.
- [x] **Green:** Use the formatter in the existing exact-set mismatch branch; retain `set(checklist_result) != set(metadata.checklist_items)` and every preceding validation.
- [x] **Red → green:** Add `test_cli_malformed_checklist_item_lists_valid_items` for omitted `=`, blank key/result, and duplicate normalized keys. Preserve parse-time syntax validation and enrich only through a safely available injected read that succeeds. Add `test_cli_malformed_checklist_syntax_wins_when_application_or_read_unavailable`: syntax error wins, exit 2, no list, no runtime access or production application creation. Add a real wrong-project read test asserting known checklist text never appears in the output. Preserve direct exact-text/file paths and existing nonblank/duplicate file checks. Give capture applications a narrowly typed fake read projection only when needed, and inject the real `DurableReadProjectionService` into the synthetic completion application.
- [x] **Verify:** `uv run --frozen pytest tests/test_task_metadata.py tests/test_task_execution_service.py tests/adapters/test_cli_task_completion.py tests/adapters/test_cli_workflow_domain.py -q`. Update the manual's diagnostics explanation in this task.
- [x] **Task review:** Obtain specification-compliance and code-quality verdicts under the selected phase-two workflow. Leave changes uncommitted while the read-only Git restriction applies.

## Task 2: Normalize explicit 1-based checklist items and prove replay compatibility

**Files:** Modify `cli/main.py`, `tests/adapters/test_cli_workflow_domain.py`, `tests/adapters/test_cli_task_completion.py`, `tests/adapters/test_command_renderer.py`, `docs/agent-cli-manual.md`.

**Interfaces:** Add `_resolve_checklist_entries(entries: tuple[tuple[str, str], ...], checklist_items: tuple[str, ...]) -> dict[str, str]` in `cli/main.py`. Task 1 supplies metadata and error formatting. Return only exact-text keys; reject ambiguous mapping with `ValueError`. The existing `CompleteTaskRequest`, application, and workflow request remain unchanged.

- [x] **Red:** Add `test_cli_index_completion_persists_exact_checklist` using the existing file-backed fixture's two equals-bearing checklist items. Submit `1=met` and `2=observed=A=B=C`; assert stored evidence is the exact-text dictionary and the receipt request contains only that dictionary, not ordinal/source fields.
- [x] **Run:** `uv run --frozen pytest tests/adapters/test_cli_task_completion.py -k index_completion -q`. On master the request must fail exact checklist coverage, not an unrelated fixture error.
- [x] **Green:** Implement uniform index resolution under the chosen literal-coverage precedence; pass its mapping through `_task_complete`. Do not populate missing items, coerce result values, or alter service checks.
- [x] **Red → green:** Add `test_cli_index_checklist_rejects_invalid_or_ambiguous_entries` parameterized over zero, signed/negative, leading-zero, decimal, Unicode-digit, out-of-range, duplicate, text/index mix, and numeric-literal collision. Assert exit 2, ordered valid-item diagnostics, and unchanged raw durable rows. Separately assert valid but incomplete ordinal coverage still gets the existing durable service rejection (exit 1).
- [x] **Red → green:** Add `test_cli_numeric_literal_checklist_preserves_exact_form` and `test_cli_checklist_file_never_interprets_numeric_keys`. Reject the `("2", "1")`, `1=a,2=b` differing complete mappings; accept identical mappings for `("1", "2")` and equal values. Prove unambiguous literal-first coverage remains supported and the `("Run tests", "1")` shorthand collision rejects. Keep first-equals result and lossless file tests.
- [x] **Red → green:** Add `test_cli_completion_cross_form_replay_survives_restart`, covering index → file/exact text and file/exact text → index with equal resolved values, same metadata, actor, correlation ID, and key. Recreate the application after disposing the engine; compare returned output and raw durable rows. Changed values must not replay as success; Task metadata is immutable, so do not add a metadata-mutation test unless explicitly labeled simulated corruption. Include retry-bound Task and replay after completion/closed Sprint/later active Sprint using retained source metadata. Add `test_cli_pre_upgrade_index_rejection_conflicts_after_normalization`: seed a durable literal `{"1": "met"}` rejection through the unchanged application, retry indexed input with the same key, assert conflict/not replay and unchanged durable rows. Document fresh-key guidance.
- [x] **Contract verification:** Replace the affected checklist help-prose assertions with parser action/destination/source-group assertions. Extend rendered-command tests by substituting real file placeholders and checking typed parsed fields/captured request semantics. Keep both advertised file templates unchanged; add no fixed command count assertions.
- [x] **Verify:** `uv run --frozen pytest tests/adapters/test_cli_task_completion.py tests/adapters/test_cli_workflow_domain.py tests/adapters/test_command_renderer.py tests/workflow/test_sprint_retry_execution.py -q`. Update manual/help with examples, collisions, exclusivity, omission rejection, and cross-form replay.
- [x] **Task review:** Require both verdicts and disposition of findings. Leave changes uncommitted.

## Task 3: Add inline no-impact triage with identical persistence and receipts

**Files:** Modify `cli/main.py:759-766,2328-2341`, `tests/adapters/test_cli_workflow_domain.py`, `tests/adapters/test_command_renderer.py`, `docs/agent-cli-manual.md`; create `tests/adapters/test_cli_sprint_triage.py`.

**Interfaces:** Add `_triage_payload(args: argparse.Namespace) -> JsonObject` in `cli/main.py`. A required mutually exclusive argparse group collects `triage_files: list[str] | None` and `triage_summaries: list[str] | None` via `append` so repeats can be rejected. Return the unchanged file loader's object or exactly `{"summary": original_summary}`; submit the existing `PostSprintTriageRequest`.

- [x] **Red:** Add `test_post_sprint_triage_cli_summary_and_file_construct_identical_requests` using the capture application and all request metadata, plus `test_post_sprint_triage_cli_summary_preserves_exact_text` with surrounding whitespace, Unicode, quotes, newlines, and `=`. Compare typed request dumps, not only their summary fields.
- [x] **Run:** `uv run --frozen pytest tests/adapters/test_cli_workflow_domain.py -k post_sprint_triage -q`. On master the inline case must fail on required `--file`.
- [x] **Green:** Add the exclusive source group and `_triage_payload`. Restrict inline use to `none`, reject blank/repeated input before dispatch, preserve accepted summary bytes, and keep file payload handling unchanged.
- [x] **Red → green:** Add `test_post_sprint_triage_cli_requires_exactly_one_payload_source`, `test_post_sprint_triage_cli_rejects_repeated_payload_source`, `test_post_sprint_triage_cli_rejects_summary_for_downstream_impact`, `test_post_sprint_triage_cli_rejects_blank_summary`, and `test_post_sprint_triage_cli_file_preserves_payload_for_every_impact`. Cover both argument orders, no source, repeated identical/different values, malformed/unreadable/non-object files, and backlog/specification JSON objects with nested fields. Assert exit 2 and no captured mutation for invalid inputs.
- [x] **Red → green:** In the new durable suite, seed a closed, untriaged Sprint using `tests/workflow/execution_retry_support.py:_complete_execution_sprint` and existing raw-row helpers. Add `test_cli_triage_persists_identical_payload_and_receipt_for_both_sources`: clone the same disposable seed into two databases, use identical keys/metadata and a fixed clock, then compare canonical payload bytes, payload fingerprint, full receipt request JSON/fingerprint, result JSON/output, events, and raw durable rows. Do not compare uncontrolled timestamps or unrelated database IDs from independent seeds.
- [x] **Red → green:** Add `test_cli_triage_cross_source_replay_survives_restart` in both directions; replay after disposal/reopen must return the stored output with `replayed=True` and change no raw durable rows. Add `test_cli_triage_changed_summary_with_same_key_preserves_durable_rows` and separate invalid-inline no-write checks. Preserve existing same-payload/new-key correction rejection behavior; do not confuse it with same-key replay.
- [x] **Red → green:** Add `test_cli_retry_triage_summary_preserves_retry_binding`, using the established exact retry closure fixture pattern in `tests/workflow/test_sprint_retry_execution.py:1573`. Compare file/inline payload and retry receipts without rewriting the original triage. Include schema/scope negative cases (wrong impact, wrong project/instance, missing closure) to confirm existing validation remains effective.
- [x] **Verify:** `uv run --frozen pytest tests/adapters/test_cli_sprint_triage.py tests/adapters/test_cli_workflow_domain.py tests/adapters/test_command_renderer.py tests/workflow/test_execution_requests.py tests/workflow/test_execution_transitions.py tests/workflow/test_sprint_retry_execution.py -q`. Update CLI help/manual with equivalent inline/file examples and the intentional repeated-file rejection; assert help options structurally and command templates behaviorally.
- [x] **Task review:** Require specification-compliance and code-quality verdicts. Leave changes uncommitted.

## Verification setup and required gates

For native macOS verification, recreate a temporary pytest isolation plugin rather than modifying repository-wide runtime behavior. This setup is test-only; it does not launch the product, change `HOME`, or override real provider settings. Use a fresh task directory on a resumed run:

```sh
issue233_tmp="$(mktemp -d "${TMPDIR:-/private/tmp}/issue233.XXXXXX")"
export UV_PROJECT_ENVIRONMENT="$issue233_tmp/venv"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$issue233_tmp:$PWD${PYTHONPATH:+:$PYTHONPATH}"
cat > "$issue233_tmp/issue233_test_isolation.py" <<'PY'
"""Confine synthetic CLI test runtime fences and diagnostics to tmp_path."""
from pathlib import Path
import pytest

@pytest.fixture(autouse=True)
def isolate_cli_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from utils import runtime_ownership, logging_config, failure_artifacts
    monkeypatch.setattr(runtime_ownership, "runtime_roots", lambda: (tmp_path,))
    monkeypatch.setattr(logging_config, "APP_LOG_PATH", tmp_path / "app.log")
    monkeypatch.setattr(logging_config, "ERROR_LOG_PATH", tmp_path / "error.log")
    monkeypatch.setattr(failure_artifacts, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(failure_artifacts, "FAILURES_DIR", tmp_path / "failures")
PY
export PYTEST_PLUGINS=issue233_test_isolation
```

Do not use this plugin to bypass platform/ownership acceptance tests: it is for the provider-free affected suites below. Keep CI's real canonical Linux runtime gate unchanged. Redirect each gate's full output to a separate file under the temporary directory, retain its exit status, and print a bounded summary. Do not substitute a narrow type check for the repo-wide gate.

- [x] Targeted and affected suites:

```sh
uv run --frozen pytest tests/test_task_metadata.py tests/test_task_execution_service.py tests/adapters/test_cli_task_completion.py tests/adapters/test_cli_sprint_triage.py tests/adapters/test_cli_workflow_domain.py tests/adapters/test_command_renderer.py -q
uv run --frozen pytest tests/workflow/test_execution_requests.py tests/workflow/test_execution_transitions.py tests/workflow/test_execution_recovery.py tests/workflow/test_execution_scope.py tests/workflow/test_sprint_retry_execution.py tests/workflow/test_sprint_retry_acceptance.py -q
```

- [x] **Repo-wide typing:** `uv run --frozen ty check`. Require exit 0 and **`All checks passed!`**. `# type: ignore` does not suppress ty; use `# ty: ignore[rule]` only as a justified last resort. Follow the existing `pytest.fail` convention when needed:

```python
pytest.fail(
    "message"  # ty: ignore[invalid-argument-type]
)
```

- [x] **Repo-wide lint:** `uv run --frozen ruff check .`.
- [x] **Repo-wide annotation lint:** `uv run --frozen ruff check --select ANN .`.
- [x] **Format changed Python files:** `uv run --frozen ruff format --check cli/main.py utils/task_metadata.py services/task_execution_service.py tests/test_task_metadata.py tests/test_task_execution_service.py tests/adapters/test_cli_workflow_domain.py tests/adapters/test_cli_task_completion.py tests/adapters/test_cli_sprint_triage.py tests/adapters/test_command_renderer.py`. Refine the explicit list only if approved implementation actually changes a different file; do not give Ruff Markdown files.
- [x] **Whitespace:** `git diff --check` (and inspect the untracked plan separately until it can be included in a reviewed diff).
- [x] **Frontend:** Not applicable: no frontend files changed. If reviewer-approved scope touches frontend, run the exact CI Node suite: `node --test tests/test_workflow_position_display.mjs tests/test_lifecycle_workspace.mjs tests/test_sprint_retry_dashboard.mjs tests/test_dashboard_review_safety.mjs tests/test_dashboard_bundle.mjs tests/test_cockpit_action_synchronization.mjs tests/test_create_project_modal_required_fields.mjs tests/test_vision_interview_ui.mjs tests/test_product_goal_interview_ui.mjs`.
- [x] Preserve CI's canonical full gate and Linux launcher/container acceptance unchanged (not executed on this macOS host). Do not run a native product launcher or touch existing Docker resources to reproduce them here. Report any unrun environment-dependent gate separately.

## Phase-two review and execution handoff

Phase-one plan review is complete and phase two is approved. The resumed execution has incorporated the external review above and refreshed checkout state/instructions before implementation. Preserve the ledger, per-task reviews, and final independent review.

Use `subagent-driven-development`, `test-driven-development`, `requesting-code-review`, and `verification-before-completion`. Preserve the installed workflow's implementer/task-reviewer/re-review/final-review templates and its workspace/ledger conventions. Each task gets a fresh scoped ultra implementer and a fresh task review with both specification and quality verdicts. Use the active policy's Orca-controlled Opus route for its bounded reviewer seats when authorized/available; no routine duplicate native reviewer for the same seat. Final substantive whole-change review is a separate fresh native Astra assignment, with the effort and descendant controls explicitly resolved under the resumed user's policy (never assume this phase authorizes an unselectable configuration).

Preserve the workflow budget: at most five per-task fix rounds (rounds 1–3 continue the same bounded implementer, rounds 4–5 require a fresh, appropriately stronger permitted assignment); use scoped re-review and record rulings/findings. Final review permits one fix dispatch and one scoped re-review, followed by disposition of residual findings. Do not add extra review loops or ignore load-bearing findings to declare completion. If required model/control settings cannot be maintained, disclose the precise gap and resolve it under user policy before delegation.

No commits or Git mutations while this sandbox restriction applies, despite generic workflow commit steps. Do not create another worktree; the user already supplied this detached, managed worktree. Required gates and fresh final whole-diff review must pass before phase-two completion can be claimed. Repeat/broaden checks only for changed code, failures, or unresolved concerns.

## Contract changes and preserved contracts

**Will change:** CLI `--checklist-item` accepts indexed keys while preserving parse-time syntax-error precedence. Checklist mismatch/syntax messages can list capped ordered index/text pairs, including shared service errors consumed by other transports. Triage accepts a mutually exclusive `--summary` for `none` as a CLI-only convenience, rejects blank inline summaries and repeated payload flags, and changes the parser Namespace fields for triage file/summary collection. CLI help, manual, and structural/behavioral contract tests change.

**Will not change:** application/workflow request fields, HTTP/API/projection fields, impact enums, metadata/database schemas, UI, provider calls, command catalog templates, checklist exact-set coverage, result-value vocabulary, file JSON-object validation, original/retry binding and closure requirements, routing authority, correction lineage, persisted payload/receipt formats, idempotency-key ownership, replay comparison, or fingerprint algorithms. Shared error message text improves; envelope shape, error codes, and exit-code classes remain the same. Existing exact-text and file input remain supported, including equals-bearing keys/results.

## Open reviewer questions and risks

1. Literal-first precedence is approved with one ambiguity exception: reject complete competing index/literal mappings that differ; accept identical mappings.
2. Preserve context-free parse-time validation and enrich only if an injected read succeeds. Unavailable runtime/application/read must not displace the syntax error.
3. Repeated `--file` rejection, nonblank-only inline summaries, `none`-only CLI restriction, deferred all-met, and leaving the legacy helper untouched are approved.
4. The current triage workflow has an open JSON-object payload and an older structured helper alongside it. This plan preserves the active schema/scope checks. If the reviewer expects the older multi-layer schema to become authoritative, that is a separate product/schema decision and is outside this issue.
5. Extra Task reads for shorthand can fail on corrupt/unavailable retained projections where an already stored receipt could otherwise replay. Historical/retry/later-scope tests must establish supported replay behavior; do not expand this issue into a new metadata/receipt recovery mechanism without review.
6. Phase-two routing uses the explicitly requested fresh native Sol ultra implementers. The installed Copilot controls do not establish enforceable built-in read denial for the required restricted review entry; no external reviewer was launched. Use the policy's bounded native Astra alternative, ultra as explicitly requested for every child, for task reviews and a separate fresh final whole-diff review. No descendants are authorized; quota windows are unavailable, not assumed exhausted.

## Phase-one self-review

- [x] Requirements mapped to tasks: parser and contextual diagnostics (1–2), explicit resolution and compatibility/replay (2), summary exclusivity and exact file equivalence (3), docs/help/catalog contracts (1–3), all named gates above.
- [x] Alternatives and deferred all-met option documented; no fuzzy matching, auto-filling, schema relaxation, or UI work introduced.
- [x] Named helper signatures agree across tasks; existing service/request/replay authority remains intact.
- [x] Five review-focus cases have owning behavioral tests; verification separates argument failures from durable rejected requests.
- [x] External Opus 5.5 review changes incorporated before implementation: bounded shared diagnostics (20 items, 160 displayed characters), syntax precedence, legacy-receipt conflict/fresh-key guidance, differing-complete-mapping rejection, immutable-metadata test scope, internal-spacing mismatch, and real wrong-project non-disclosure assertion.
