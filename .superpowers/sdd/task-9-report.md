# Task 9 Report: Derive Authority Workflow From Facts

## Outcome

- Status: complete
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
