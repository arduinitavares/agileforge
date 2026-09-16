# Lossless Task Checklist Input Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development. Follow TDD and review the complete diff before handoff.

**Goal:** Submit exact checklist keys and values containing equals signs through semantic Task completion.

**Architecture:** Add `--checklist-file PATH`, containing only a nonempty JSON object of nonblank string keys and string values. Make it mutually exclusive with repeated `--checklist-item KEY=VALUE`; retain the latter's first-equals split. Keep completion validation, accepted requirements, request schemas, evidence, and idempotency unchanged.

**Tech Stack:** Python 3.13, argparse, stdlib JSON, existing Pydantic request validation, pytest/SQLModel.

**Spec:** https://github.com/arduinitavares/agileforge/issues/270 and its Agent Brief.

## Global Constraints

- Use uv and synthetic test databases; no provider calls or protected project state.
- Reject malformed, blank, duplicate, non-string, mixed, or incomplete mappings clearly.
- Detect duplicate JSON keys before dictionary construction, including collisions after the existing semantic whitespace normalization.
- Preserve values containing equals signs in legacy input and both keys and values in file input.
- Do not expose generic mutation payload files or change domain acceptance/idempotency rules.
- No pushes, merges, production completion, or issue closure in this task.

## Task 1: Lossless semantic CLI input

**Files:** `cli/main.py`, `cli/workflow_commands.py`, `tests/adapters/test_cli_workflow_domain.py`, `tests/adapters/test_command_renderer.py`, `docs/agent-cli-manual.md`.

**Interface:** `--checklist-file PATH` reads a nonempty JSON string-to-string object; only one checklist source is permitted. Workflow templates advertise `--checklist-file <checklist-file>`.

- [x] Add failing CLI boundary tests with `{"Run --ignore=tests/e2e": "exit=0", "Check A=B=C": "observed=A=B=C"}` and assert the exact `CompleteTaskRequest.checklist_result`.
- [x] Add rejection cases for `{}`, malformed JSON, arrays/scalars, blank keys/results, non-string results, duplicate keys (including escaped spellings and normalized collisions), missing/unreadable/invalid-UTF8 files, and mixed sources. Assert no application request is dispatched.
- [x] Confirm legacy `check=expected=actual` remains `{"check": "expected=actual"}`.
- [x] Run `uv run --frozen pytest tests/adapters/test_cli_workflow_domain.py tests/adapters/test_command_renderer.py -q --durations=5`; record expected RED.
- [x] Implement the smallest strict JSON loader, parser source selection, and request mapping. Preserve domain validation unchanged.
- [x] Update renderer/parser tests, CLI help, and operator guidance with file contents, command usage, coverage rules, and replay guidance.
- [x] Run focused tests and lint; review the diff.

## Task 2: Verify durable semantic completion

**Files:** new `tests/adapters/test_cli_task_completion.py`; extend `tests/workflow/execution_fixtures.py` and `tests/workflow/test_planning_transitions.py` with optional checklist input before accepting the synthetic plan.

**Interface:** Invoke real `cli.main.main` with a real `AgileForgeApplication`, synthetic SQLite state, and the new file option.

- [x] Reproduce the existing parser truncation on the baseline.
- [x] Seed accepted synthetic Task checklist text containing one/multiple equals signs through the planning fixture.
- [x] Assert CLI success records exact evidence, one execution log, and one completion receipt.
- [x] Replay the same idempotency key, including after reconnecting to file-backed SQLite; assert one durable completion.
- [x] Reject changed payload with the same key and altered/missing/extra checklist keys; compare workflow fingerprints and persisted state.
- [x] Preserve existing rejection receipts for domain validation failures and verify their replay. Parse errors must create no receipt; rejected domain requests must not change Task state or completion evidence.
- [ ] Run all affected suites, then `uv run --frozen pyrepo-check --all` with full output retained in a temporary log.
- [ ] Obtain independent correctness review; resolve confirmed findings and retain final provenance/evidence.
