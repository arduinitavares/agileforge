# Implementation Plan: Authority Validation Repair v2

## Overview

Implement issue #209 by restoring bounded validator-guided repair in the current
v2 ADK Authority recipe. The host first fixes opaque temporary-reference shape
mechanically. Remaining repairable output failures receive one distinct model
repair invocation and one final pass through the unchanged strict semantic
validator. Tasks are tracked by the checklist in issue #209 and this ordered
plan.

## Architecture Decisions

- Keep `normalize_compiler_output(...)` as the only success gate.
- Pre-normalize only opaque temporary references; infer no business semantics.
- Use a typed validation-repair input and a distinct retained leaf identity.
- Compose one initial and at most one repair leaf inside the existing graph
  recipe, following the Vision repair pattern.
- Disable generic retry for the outer Authority function node so cached invalid
  child output is not replayed as a fake retry.
- Keep persistence and durable replay outside the recipe unchanged.

## Task List

### Phase 1: Provider Boundary

- [x] Task 1: Add RED coverage for attempt-29 temporary references.
  - Acceptance: unique noncanonical temporary IDs and all exact references fail
    under current production behavior for the expected reason.
  - Verify: `uv run --frozen pytest tests/services/contracts/test_specification.py -q`
  - Files: normalizer contract tests and one captured fixture.

- [x] Task 2: Add deterministic temporary-reference preprocessing.
  - Acceptance: unique references normalize in one pass; ambiguous reuse,
    unknown references, and persisted invalid IDs remain closed.
  - Verify: focused normalizer tests pass.
  - Files: `services/contracts/specification_normalizer.py` and its tests.

### Checkpoint: Provider Boundary

- [x] Attempt-29's temporary-ID shape failure is removed locally; its later
  semantic source violation remains visible and enters bounded repair.
- [x] Existing attempt-27 ambiguous-identity regression remains closed.

### Phase 2: Bounded Validation Repair

- [x] Task 3: Add RED recipe tests for one feedback-informed repair pass.
  - Acceptance: current recipe fails because no distinct repair leaf is invoked;
    tests cover success, second failure, provider exception, compile, and
    post-human repair.
  - Verify: focused adapter tests fail for missing behavior, not fixture errors.
  - Files: `tests/adapters/test_adk_authority_normalization.py` and test helpers.

- [x] Task 4: Add the typed validation-repair contract and shared prompt mode.
  - Acceptance: repair input carries exact original input, bounded failure/output
    evidence, one ordinal, and stable fingerprint metadata; repair agent has a
    distinct name and the prompt/contract identity changes.
  - Verify: schema and prompt tests pass.
  - Files: `utils/spec_schemas.py`, `adapters/adk/agents/specification.py`,
    `adapters/adk/prompts/specification.txt`, and contract tests.

- [x] Task 5: Implement the one-pass recipe controller.
  - Acceptance: repairable failures invoke the distinct leaf once; terminal
    provider failures do not; second normalization failure includes both bounded
    findings; outer generic retry is disabled.
  - Verify: focused adapter tests pass.
  - Files: `adapters/adk/recipes.py`, `services/application.py`, and adapter tests.

### Checkpoint: Bounded Repair

- [x] One initial invalid plus one valid repair yields one recipe success.
- [x] Two invalid outputs yield one recipe failure and exactly two leaf calls.
- [x] Initial success yields one leaf call.
- [x] Initial provider exception yields no repair call.

### Phase 3: Persistence, Replay, And Regression

- [x] Task 6: Prove persistence and replay through the real workflow runner.
  - Acceptance: repair success creates one candidate; repair failure creates none;
    replay makes no calls and creates no duplicates.
  - Verify: runner-focused Authority tests pass.
  - Files: `tests/adapters/test_adk_authority_normalization.py` and only production
    files required by a proven gap.

- [ ] Task 7: Register captured regression evidence and run the complete gate.
  - Acceptance: attempts 25, 27, 28, and 29 are executable; issue #209 contract,
    documentation, and code agree; protected profile hashes do not change.
  - Verify: `uv run --frozen pyrepo-check --all`, `git diff --check`, SQLite
    integrity and foreign-key checks, and before/after database hashes.
  - Files: focused benchmark fixtures/tests and this documentation.

### Checkpoint: Complete

- [x] Focused RED/GREEN evidence recorded.
- [ ] Full repository gate passes from a committed checkout.
- [ ] Correctness, scope, and lean reviews have no unresolved findings.
- [ ] Worktree is clean and exactly one scoped commit is identified.
- [ ] No provider call, profile transfer, issue closure, push, or merge occurred.

## Risks And Mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Repair output hides a real source gap | High | Same strict normalizer runs after repair; human review remains required. |
| Generic ADK retry exceeds the semantic budget | High | Set the outer Authority node to one attempt and use one explicit repair leaf. |
| Invalid output bloats or injects the repair prompt | High | Carry it as bounded untrusted typed data with length, hash, and truncation metadata. |
| Temporary-ID preprocessing weakens stored IDs | High | Rebind only before parse; final persisted model and host IDs retain the strict pattern. |
| Repair creates duplicate candidates | High | Preserve existing durable attempt replay and single completion transaction. |
| Broad refactor regresses post-human repair | Medium | Share one recipe policy and test both `authority.compile` and `authority.repair`. |

## Open Questions

None.
