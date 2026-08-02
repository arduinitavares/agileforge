# Task 9 Report: Derive Authority Workflow From Facts

## Outcome

- Status: complete; requested review fixes complete
- Base commit: `4584849b8a4f908de6ccec149beeaef23757df13`
- Verified implementation tree SHA before this report: `70c20fcddccb575946b4946a2bc654466286873b`
- Commit message: `feat: derive authority workflow from facts`
- Provider calls: none; tests used compiler fakes and remained offline

The root workflow now composes one authority graph for initial and extension specs. Four closed request variants drive compile, review decision, factual feedback, and repair. Position is derived from registered spec versions, compiled authorities, append-only terminal decisions, feedback attempts, and node attempts. Accepted authority for the current approved spec alone exposes the next `vision.generate` boundary.

Compiler, review, and decision low-level functions now use a caller-owned `Session`. `WorkflowDomain` owns decision, fact, expiry, commit, rollback, receipt, and mutation-audit boundaries. The new path has no setup/FSM/session source of truth or expected-state compatibility guards.

## RED Evidence

`uv run --frozen pytest tests/workflow/test_authority_graph.py tests/workflow/test_authority_transitions.py tests/workflow/test_authority_restart.py -q`

- Result: collection failed with 3 errors.
- `workflow.definitions.authority` did not exist for graph and restart tests.
- `build_authority_review_snapshot_in_session` did not exist for transition tests.

## GREEN Evidence

- Focused authority suite: `19 passed, 5 warnings` in `1.40s`.
- Brief-required compiler/review/decision invariant suite: `215 passed, 5 warnings` in `6.76s`.
- `uv run --frozen pyrepo-check --all`:
  - Ruff: pass.
  - Annotation lint: pass.
  - `ty`: pass.
  - Bandit: no issues.
  - Pytest: `3381 passed, 2 skipped, 13 deselected, 9 warnings` in `118.15s`.
- A scan of the new authority definition, handlers, and requests found no `expected_state`, `expected_setup_status`, `setup_status`, `fsm_state`, `Any`, typing suppression, or `noqa` usage.

## Behavior Covered

- Registered current spec exposes compile with exact spec ID/hash binding and default model `openrouter/openai/gpt-5.6-luna`.
- Active attempt waits; expired or failed compile exposes recovery; compile failure is atomic.
- Persisted pending authority derives review waiting and survives deletion of ADK/session storage.
- Decisions bind exact authority and review fingerprints; conflicting terminal decisions fail closed with `WORKFLOW_FACT_CONFLICT`.
- Rejection routes through durable typed feedback and repair, then returns to review.
- A newer approved spec makes historical accepted authority stale and re-exposes compile while historical acceptance continues to block abandonment.
- Compiler artifact, canonical fingerprint, prompt/compiler provenance, review completeness, append-only acceptance, receipt, and audit invariants remain covered.

## Changed Files

- `.superpowers/sdd/task-9-report.md`
- `repositories/workflow.py`
- `services/agent_workbench/authority_decision.py`
- `services/agent_workbench/authority_review.py`
- `services/specs/compiler_service.py`
- `tests/test_specs_compiler_service.py`
- `tests/workflow/test_authority_graph.py`
- `tests/workflow/test_authority_restart.py`
- `tests/workflow/test_authority_transitions.py`
- `tests/workflow/test_graph_kernel.py`
- `tests/workflow/test_graph_properties.py`
- `tests/workflow/test_initial_scope_registration.py`
- `workflow/__init__.py`
- `workflow/definitions/authority.py`
- `workflow/definitions/root.py`
- `workflow/domain.py`
- `workflow/facts.py`
- `workflow/graph.py`
- `workflow/handlers/__init__.py`
- `workflow/handlers/authority.py`
- `workflow/requests/__init__.py`
- `workflow/requests/authority.py`

## Concerns

- Only the next vision child boundary is exposed; vision/backlog transitions remain intentionally unimplemented.
- The full suite retains existing deprecation, socket-guard, experimental ADK, and SQLAlchemy warnings; the gate reported no failures.
- The implementation tree SHA excludes this report because a Git commit cannot contain its own SHA. The final commit SHA is recorded in the task handoff.

## Review Fix Outcome

- Review-fix base commit: `22a516a1fa8f7da3fdaa8133584572d98c0baa5e`
- Verified review-fix code/test tree SHA before this report update: `4d151f6a1ad15e2883a6fc1be1800e5a2704a709`
- Review-fix commit message: `fix: bind authority transitions to exact facts`
- Provider calls: none; compiler execution remained faked and the default socket guard remained active

Compile decisions now use the stable instance key `spec:<spec_version_id>:<spec_hash>`. Only a persisted `authority.compile` attempt with that exact instance can produce active, success, failure, expiry, or recovery semantics for the current spec.

Authority review now exposes one canonical full `review_fingerprint`. The schema-qualified `review_token`, domain request validation, low-level decision write, and persisted acceptance provenance all use the same function. The fingerprint includes project/spec source context, exact pending authority provenance and artifact, compiler invariants and classifications, coverage and IR evidence, review findings, and complete Scope Discovery provenance. A fresh `DecideAuthority` preflights this fingerprint before claiming its receipt; an existing successful receipt still replays.

Compiler, review, and decision lifecycle tests reject replacement `Session` construction and any commit, rollback, or close of the caller session. Post-flush exception probes cover both authority compilation and acceptance: business writes and receipts roll back together, the identical retry writes once, and the next identical call replays once.

## Review Fix RED Evidence

1. `uv run --frozen pytest tests/workflow/test_authority_graph.py tests/workflow/test_authority_transitions.py tests/workflow/test_authority_restart.py -q`
   - `12 failed, 17 passed, 5 warnings` in `1.74s`.
   - Three persisted old-attempt probes failed because compile had no spec-scoped instance key.
   - Review tests failed because `AuthorityReviewSnapshot` had no canonical full `review_fingerprint`.
2. `uv run --frozen pytest tests/test_agent_workbench_authority_decision.py::test_accept_with_review_token_promotes_authority_and_advances_to_vision -q`
   - `1 failed, 5 warnings` in `0.82s`.
   - Legacy acceptance still persisted the coverage digest instead of the canonical full review fingerprint.

## Review Fix GREEN Evidence

- Focused Task 9 suite: `29 passed, 5 warnings` in `1.72s`.
- Workflow transaction/idempotency/concurrency slice: `36 passed, 5 warnings` in `4.44s`.
- Task 9 plus retained compiler/review/decision invariants: `225 passed, 5 warnings` in `7.24s`.
- `uv run --frozen pyrepo-check --all`:
  - Ruff: pass.
  - Annotation lint: pass.
  - `ty`: pass.
  - Bandit: no issues.
  - Pytest: `3391 passed, 2 skipped, 13 deselected, 9 warnings` in `119.90s`.
- Source scan found no `Any`, typing suppression, or compatibility/session-state routing terms introduced in the authority domain path.

## Review Fix Files

- `.superpowers/sdd/task-9-report.md`
- `services/agent_workbench/authority_decision.py`
- `services/agent_workbench/authority_review.py`
- `tests/test_agent_workbench_authority_decision.py`
- `tests/test_agent_workbench_authority_review.py`
- `tests/workflow/test_authority_graph.py`
- `tests/workflow/test_authority_transitions.py`
- `workflow/definitions/authority.py`
- `workflow/domain.py`
- `workflow/handlers/__init__.py`
- `workflow/handlers/authority.py`

## Review Fix Concerns

- Review-token payload semantics intentionally changed. Tokens issued before this fix fail closed and must be regenerated from the current packet.
- A fresh domain authority decision rebuilds the deterministic review snapshot during preflight and again immediately before mutation in the same transaction. This keeps receipt ordering and mutation-time validation explicit at the cost of one extra local review computation.
- Existing deprecation, socket-guard, experimental ADK, and SQLAlchemy warnings remain unchanged; the full gate reported no failures.
