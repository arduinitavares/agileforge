# SDD ledger — plan: docs/superpowers/plans/2026-08-25-story-readiness-sprint-candidacy.md

## Preflight interface scan

| Tasks | Producer and consumer | Result |
| --- | --- | --- |
| 1 → 2 | Task 1 produces canonical v3 evidence; Task 2 reconciles it | Consistent: reconciliation uses the same in-session evaluator and skips byte-current v3 evidence. |
| 1 → 3 | Task 1 produces exact current eligibility; Task 3 gates Select on it | Consistent: Remove and Defer do not require current evidence. |
| 2 → 3 | Task 2 defines hard-renamed API/CLI surfaces; Task 3 adds selection surfaces beside them | Consistent: no legacy validation alias becomes a second authority. |
| 3 → 4 | Task 3 projects append-only selection facts; Task 4 consumes them for selected scope and candidacy | Consistent: latest canonical event supplies state and fingerprint. |
| 4 → 5 | Task 4 produces strict readiness/candidate/dependency projections; Task 5 renders and mutates them | Consistent: browser inputs use exact server fingerprints and fail closed. |
| 5 → 6 | Task 5 completes cross-surface behavior; Task 6 documents and verifies it | Consistent: documentation follows proven behavior. |
| 1 | v3 tests match v3 schema and acceptance hook | Internally consistent after correcting the exact in-session evaluator signature. |
| 2 | reconciliation tests match hard rename and receipt contract | Internally consistent. |
| 3 | state tests match event metadata and transition table | Internally consistent after the Global Constraints transition table was added. |
| 4 | candidate/dependency tests match selected-scope fingerprint | Internally consistent. |
| 5 | UI tests match missing/stale recheck boundary | Internally consistent after excluding current deterministic rule failures from recheck copy. |
| 6 | docs, gates, reviews, commit, and optional profile preparation are ordered | Internally consistent. |

Ruling: A current deterministic rule failure does not render a Re-run control; only missing or stale evidence does — repeating unchanged inputs cannot change eligibility and would recreate the approval-like click #223 removes — if wrong, operators would need a recheck button after every current failure.

Ruling: Selection state is reconstructed from canonical `workflow_events`, not a new table — this preserves the exact #222 SQLite-backup transfer while retaining append-only audit history — if wrong, a future query/index migration may be preferable.

Ruling: A selected-scope fingerprint includes the canonical v3 evidence fingerprint and latest selection-event fingerprint — this prevents restored evidence or re-selection from reviving an old dependency decision — if wrong, dependency confirmation will be more conservative and require another explicit review.

Task 1: dispatched `/root/task1_v3_acceptance` at base `3143f63cd052da2668f80fb6eab91ced5797bd7f` using `gpt-5.6-terra` high, standard context.

Task 1: Ruling: v3 is a durable schema replacement, so Task 1 may update every directly necessary v2 evidence consumer and test even when the plan file list omitted it; Task 2 API/CLI renames and Task 3 selection behavior remain excluded — this keeps the incremental branch green — if wrong, Task 1's diff will be broader than its initial file list.

Task 1: initial implementation commit `b0a798e`; focused GREEN `69 passed`, Ruff clean.

Task 1: review needs fixes — Critical: evaluator `ValueError` could commit partial acceptance through the handler conflict path. Important: winner proof did not compare exact v3 identities/version/diagnostics, and rollback/replay/failed-evidence regressions were missing. Fix round 1 dispatched to `/root/task1_v3_acceptance` from `b0a798e`.

Task 1: fix round 1/5 (2 addressed, 1 open — persisted failed evidence, persistence rollback, advancing-clock replay, and identity/reference/diagnostic tamper coverage incomplete; commits `33968ac`..`330ae40`).

Task 1: fix round 2 dispatched to `/root/task1_v3_acceptance` from `330ae40` for the remaining executable regressions.

Task 1: fix round 2/5 (1 addressed, 0 open — all acceptance-time failed-evidence, persistence rollback, advancing-clock replay, and tamper regressions verified; commit `e7a29db`).

Task 1: complete (commits `3143f63`..`e7a29db`, review clean).

Task 2: dispatched `/root/task2_reconcile` at base `e7a29dbbd2f787309c8929f2f45c2d295931b6bc` using `gpt-5.6-terra` high, standard context.

Task 2: initial implementation commit `dbee2d2`; focused GREEN `18 passed`, changed-file ruff/annotations/ty clean.

Task 2: review needs fixes — recomputed evidence was not re-proven current before receipting; multi-Story prevalidation/partial rollback, CLI all-target omission, and concurrency were not established. Earlier v3 hybrid test expectations and Task 1 test-helper ty diagnostics are carried into this fix to restore incremental branch gates.

Task 2: Ruling: update the stale hybrid expectations now while retaining the temporary internal `ready_for_sprint` compatibility result until Task 4 removes projection terminology — this keeps the branch green without expanding Task 2 into candidacy projections — if wrong, one internal result key remains misleading for two more slices.

Task 2: fix round 1 dispatched to `/root/task2_reconcile` from `dbee2d2`.

Task 2: fix round 1/5 (5 addressed, 0 open — current-evidence postcondition, batch safety, concurrency, stale-success, and v3 gate regressions verified; commit `d451978`).

Task 2: complete (commits `e7a29db`..`d451978`, review clean).

Task 3: Ruling: expose one intent-based API route and explicit CLI verbs over the same mutation; selection history is exact-version-bound, receipt-backed, and schema-preserving in `workflow_events` — this keeps one durable authority across adapters — if wrong, separate API routes would be clearer but behavior-equivalent.

Task 3: dispatched `/root/task3_selection` at base `84e7038` using `gpt-5.6-sol` high, standard context.

Task 3: initial implementation complete at `d0ba1a7`; focused GREEN `123 passed`, directly affected fixtures `200 passed`, changed-file ruff/annotations/ty clean.

Task 3: review needs fixes — Important: canonical persisted `select` history accepts a null observed eligibility-evidence fingerprint (and audit text weaker than the writer contract), allowing tampered history to project selected/candidate. Minor: repository Story projection rebuilds the full project selection history once per Story. Fix round 1 dispatched to `/root/task3_selection` from `d0ba1a7`.

Task 3: fix round 1/5 (2 addressed, 1 new open — stricter domain actor validation lets whitespace-only HTTP actor escape the selection route as 500; commits `0d98176`..`6421bff`). Fix round 2 dispatched to `/root/task3_selection` from `6421bff` for a transport-level 422 contract.

Task 3: fix round 2/5 (1 addressed, 0 open — selection-specific whitespace audit metadata returns HTTP 422 before application invocation; commits `bfaac0f`..`5aeacc0`).

Task 3: complete (commits `84e7038`..`5aeacc0`, review clean).

Task 4: dispatched `/root/task4_selected_scope` at base `5aeacc0` using `gpt-5.6-sol` high, standard context.

Task 4: Ruling: reuse `StoryDependencyReview.source_fingerprint` as the canonical selected-scope fingerprint and add no schema column; existing review evidence that does not equal the new current scope fingerprint is stale and fails closed, with no compatibility inference — this follows the repository hard-break/fresh-evidence policy — if wrong, a dedicated redundant column would require a schema break without adding authority.

Task 4: initial implementation complete at `ccbfcce`; focused GREEN `308 passed`, changed-file ruff/annotations/ty clean.

Task 4: review needs fixes — Important: proposed selected-scope edges block the dependency-review action that must resolve them; candidate projection misses cycles through preserved external-dependent rows; handler compares rank-ordered IDs against the canonical Story-ID-ordered request. Fix round 1 dispatched to `/root/task4_selected_scope` from `ccbfcce`.

Task 4: Ruling: one dependency review is immutable per exact selected-scope fingerprint; exact retry replays and a changed duplicate conflicts, while a new review requires a new selection/evidence scope fingerprint — the existing unique durable key and public transition guards enforce that contract — if wrong, revisable same-scope reviews require a separate explicit append-only versioning design beyond #223.

Task 4: fix round 1/5 (3 addressed, 1 new open — candidate closure now includes transitive external rows, but pending-plan and execution fingerprints still cover only selected-owned rows; commits `86219e1`..`ee7a2f3`). Fix round 2 dispatched to `/root/task4_selected_scope` from `ee7a2f3`.

Task 4: fix round 2/5 (1 addressed, 0 open — repository candidacy, pending-plan freshness, and execution now share the canonical selected-reachable active dependency closure; commits `7dc2a20`..`a816813`).

Task 4: complete (commits `5aeacc0`..`a816813`, review clean).

Task 5: dispatched `/root/task5_frontend` at base `a816813` using `gpt-5.6-terra` high, standard context.

Task 5: initial implementation complete at `4bc3dc8`; Node GREEN `58 passed`, progressive Playwright `2 passed`.

Task 5: review needs fixes — Critical: external prerequisites are dropped from review payload; missing/malformed candidacy does not gate the Sprint-generation form. Important: mutation rejection re-enables ineligible Select; dependency/scope parsing accepts absent/duplicate/conflicting data; readiness parsing accepts contradictory/current-failure-without-diagnostics and mislabels missing as stale. Minor: successful rerender loses focus. Fix round 1 dispatched to `/root/task5_frontend` from `4bc3dc8`.

Task 5: fix round 1/5 (4 addressed, 2 partially open, 3 new defects — current candidate scope is not cross-checked against dependency scope; successful dependency POST plus reload failure unlocks stale controls; conflicting unselected Story scope fingerprints pass; rejected/self edges are treated as reviewable; commits `44b09c2`..`44aa160`). Fix round 2 dispatched to `/root/task5_frontend` from `44aa160`.

Task 5: fix round 2/5 (3 addressed, 1 partially open — ordinary false/throw reload stays locked, but the HTTP 409 stale-state rerender creates a new native-enabled inert Confirm button; commits `b9c8362`..`706a350`). Fix round 3 dispatched to `/root/task5_frontend` from `706a350`.

Task 5: fix round 3/5 (serialized post-success 409 replacement addressed, 1 new Critical open — a successful refresh begun while the POST is pending clears the sentinel and permits a second concurrent mutation/new key; commits `b98945e`..`d729026`). Fix round 4 requires a fresh higher-tier implementer from `d729026`.

Task 5: fix round 4/5 (1 addressed, 0 open — exact token-bound `submitting`/`awaiting_authority` phases prevent refresh races and second payloads; commits `b07e31a`..`cd63f0e`).

Task 5: complete (commits `a816813`..`cd63f0e`, review clean).

Task 6: dispatched `/root/task6_delivery` at base `cd63f0e` using `gpt-5.6-terra` high, standard context. Controller retains independent final reviews, protected rehash, optional profile preparation, and finishing handoff.

Task 6 review fix round 1: `9dc8970` locks dependency review across accepted-unstarted, active, and completed-untriaged Sprint lifecycle states in both graph evaluation and the in-transaction handler; it preserves the existing post-triage API reopening proof for a new selected future scope. The v3 packet evidence and narrow #210 persisted-evidence supersession docs are current. REDs proved graph availability and handler mutation in all three forbidden states. GREEN: focused lifecycle `8 passed`, planning `136 passed`, execution `63 passed`, API `225 passed`, packet/docs `88 passed`, CLI `129 passed`, exact #223 matrix `777 passed`, Node `67 passed`, Playwright `7 passed`; full `pyrepo-check --all` passed (`2480 passed, 1 skipped, 1 deselected`). No protected/manual/provider action occurred.

Task 6 final whole-branch review fix: RED `2d949f4` captured next-Sprint scope authority, canonical selection responses, required receipt anchors, 409 recovery locking, direct Story API/CLI hard break, and exact structural proof/non-proof disclosure. GREEN `52c13d4` uses selected-scope projection authority end-to-end; binds each selection event to an exact completed transition receipt; renders canonical StoryFact results; locks stale mutation controls through authoritative 409 recovery; and centralizes the exact v3 provider-free disclosure. Dead global DependencyGraph/load/inspect/assert and canonicalCandidateDependencies helper paths were deleted only after `rg` found no callers; auditable SelectedScopeStory lineage and concurrency proof remain. GREEN: focused Python `489 passed`, Node `67 passed`, test-owned Playwright `9 passed`; clean-head `pyrepo-check --all`: `2488 passed, 1 skipped, 1 deselected`, with ruff/annotations/ty/bandit clean. The initial dirty full-gate CI-launcher-only failures were acceptance-checkout-cleanliness policy (`acceptance checkout must be clean` before profile creation); the clean-head rerun passed them. No protected/manual/provider action occurred.

Task 6 independent review fix round 2: RED `41d25a2` captures operator-observed selected-scope fingerprint authority, exact browser Sprint candidate IDs, closed immutable selection receipt replay, immutable Sprint-start dependency rows, and removal of the unreachable automatic selector. GREEN `5a42093` requires and transactionally rechecks the observed fingerprint across API/CLI/browser/application; submits exact projected candidates; reloads mutable selection truth instead of replaying it; verifies historical execution against canonical Sprint-start row snapshots; and rejects empty/manual-less Sprint selection. GREEN: authority matrix `448 passed`, historical planning/execution `286 passed`, Node `69 passed`, authorized test-owned Playwright `9 passed`, changed-file ruff/ty and diff check clean. No protected/manual/provider action occurred.

Task 6 completion-gate/correctness fixes: the clean aggregate exposed one CLI renderer migration RED (`2491 passed, 1 failed`) because `workflow next` omitted the now-required observed selected-scope fingerprint; `6f33fee` adds the exact placeholder and reconciles the CLI manual (`39` renderer, `4` focused CLI, `16` docs tests green). Independent correctness then proved rank-ordered candidate facts could send `[103,101]` against backend `[101,103]`, and a torn same-fingerprint subset was locally enabled. Both Node REDs failed before `6faedb7`, which canonicalizes only transport IDs and cross-checks the dependency-projected candidacy vector. GREEN: targeted `2 passed`, Node `70 passed`, authorized Playwright `9 passed`, diff check clean. No protected/manual/provider action occurred.

Task 6 final independent verdicts: specification APPROVED, correctness APPROVED after the candidate-vector fix, and lean scope APPROVED; no remaining Important findings. Reviewers independently checked the full branch plus bounded `6f33fee` and `6faedb7` deltas and made no repository or external-state changes.
