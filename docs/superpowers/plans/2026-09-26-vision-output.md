# Vision output reliability implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Resolve #283 with sufficient Vision generation capacity and precise, recoverable output failures.

**Architecture:** Follow the existing Specification pre-schema callback and diagnostic-event pattern with a focused Vision validator. Reuse the existing workflow persistence and transport contracts, extending their closed error-code sets. Keep one source for Vision's effective execution settings and use it for both recipe construction and attempt provenance.

**Tech Stack:** Python 3.13, uv, Google ADK 2.2.0, LiteLLM 1.78.3, Pydantic, SQLite, Docker Compose on Linux.

**Spec:** ../specs/2026-09-26-vision-output.md

## Global constraints
- Implement only in the isolated alex/issue-283-vision-output branch based on d6b771dd.
- Budget 128000; total workflow timeout 600 seconds; lease 660 seconds. Preserve configured Luna/Sol roles and reasoning=max.
- Run product/tests with uv inside the task's Linux development container. The controller owns transport; no Windows source bind mount, production volumes or credentials in development.
- No worker may run provider calls, modify production, push, merge, or message GitHub. Parent handles review, release and the one authorized live bootstrap.
- Preserve existing override behavior and other node settings. Never rewrite history or accept partial JSON.

## Review focus
- A complete JSON object with MAX_TOKENS must still fail as incomplete; absent metadata must never be invented.
- Malformed output from the semantic-repair leaf must be classified and tagged repair, without another call.
- Failure persistence and repeated idempotency keys must preserve the precise error and avoid duplicate facts.
- A long-running valid Vision must not expire under the previous 300-second lease; unrelated nodes retain their deadlines.
- Diagnostic fields reject arbitrary text and invalid numeric metadata; generated secrets must not leak through exception chaining or event messages.

## Task 1: Implement and test Vision capacity and output failures

**Inventory findings:** Product Goal currently calls `get_vision_interviewer_max_tokens()` too. Isolate its existing 4096 default when raising Vision's default; do not accidentally raise Product Goal. Capture effective Vision generation settings from the composed agent at application construction, following Specification's captured generation configuration, rather than re-reading changing environment values when an attempt starts. Production and development launchers currently do not forward the Vision token environment variable, so do not claim a new configurable runtime override works without testing that transport. The new built-in default must work in the installed production image without an override.

**Active file ownership:** The separate native display worker owns services/read_projections.py, frontend/project.js, tests/test_vision_interview_ui.mjs and tests/services/test_vision_failure_projection.py. The native backend worker owns backend response/configuration/tests only and must not edit those four files. Parent serializes all Linux checks and owns release/scratch helpers.

**Files:** Modify utils/runtime_controls.py and utils/runtime_config.py for defaults/accessors; adapters/adk/agents/vision.py for provider configuration and callbacks; create adapters/adk/vision_output.py; modify adapters/adk/errors.py, adapters/adk/runner.py, adapters/adk/recipes.py, services/application.py, workflow/contracts.py and workflow/domain.py as required. Extend focused tests under tests/adapters, tests/services and tests/workflow, plus the existing dashboard test location if its error handling needs changes. Document effective settings in docs/linux-containers.md. Avoid unrelated restructuring.

**Interfaces:** Add a pure validate_vision_response(response_text, *, finish_reason, usage, stage) -> VisionDraftOutput and an adapter-owned VisionOutputValidationError carrying a closed error code, safe message and diagnostic dictionary. Add pre-schema callbacks for primary and repair that supply stage and available metadata. Extend the runner's diagnostic persistence to Vision, with a stable vision_output_diagnostic state key and correct invocation identity. Add optional Vision-specific registry settings rather than changing settings for all recipes. New Vision attempts record generation_config.max_output_tokens, timeout_seconds, max_attempts=1, max_semantic_repairs=1 and existing reasoning.

- [x] Write regression tests first and run them to observe failure on current behavior. Use fake BaseLlm through real Agent and workflow runner, not only a fake leaf. Include EOF with MAX_TOKENS, STOP and no metadata; empty response; malformed non-EOF JSON; invalid schema; valid control; genuine provider exception; malformed repair output; repeated idempotency and later successful explicit retry. Assert no partial Vision facts/approval and safe bounded diagnostic persistence.
- [x] Add tests for default and overridden budgets, request-body token/reasoning/timeout settings, matching application provenance and registry deadline, and 660-second Vision lease with unchanged other nodes.
- [x] Implement minimal code preserving strict schema and existing bounded semantic repair. When raw EOF has no MAX_TOKENS, report incomplete output with unknown cause. Keep available finish reason and prompt/completion/reasoning/total token counts where provided by the installed adapter.
- [x] Add CLI/API/dashboard regression coverage proving safe code/message propagation and replay. Do not change generated payloads merely to satisfy a snapshot.
- [x] Run focused tests and style/type checks. Record exact red/green commands and counts. Parent runs the full canonical gate on the integrated branch once.
- [x] Review the diff and commit the verified implementation.

## Task 2: Independent verification and release (parent only)

- [ ] Run the canonical Linux ./agileforge-dev check gate and inspect every failure; confirm frontend and packaging checks required by CI.
- [x] Obtain independent fresh-context correctness review, fix material findings with regression evidence and rerun affected checks.
- [ ] Build an immutable candidate from clean committed source; rehearse installed-runtime defaults and safe fake-provider failure behavior without production data or secrets.
- [ ] Confirm no active production writer, stop only production, make and verify a fresh supported backup, retain old image identity, recreate only production with the existing secret mount and volumes. Compare project/business state before/after image cutover.
- [ ] Check info, workflow position and workflow next for project2; run exactly one bootstrap command with a new idempotency key. Never blindly retry an uncertain result. Verify durable result, recorded capacity/model/reasoning, trace metadata, dashboard and next workflow step. Stop at human review.
- [ ] Report live result and branch/CI status honestly. Preserve rollback and diagnostic evidence; do not mark the issue resolved if generation still fails.

Validation so far: the initial actual-ADK regression failed with the old generic code. Final focused set: 83 passed; frontend Vision tests: 19 passed. Ruff, annotations and ty pass. Independent Sol review found no remaining blockers after the known-empty diagnostic correction. Canonical gate and release validation remain pending below.
