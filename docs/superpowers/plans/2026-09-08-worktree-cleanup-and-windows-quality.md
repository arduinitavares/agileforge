# Worktree Cleanup and Windows Quality Recovery Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to execute this plan task-by-task. The user's existing division of work takes precedence: Gemini implements approved batches; Codex plans and independently reviews them. Do not automatically dispatch implementation to a different model.

**Goal:** Retire obsolete worktrees without losing code or runtime evidence, deliver the reviewed #259 change, and make the Windows quality gate reliable while retaining Linux/macOS and filesystem-security coverage.

**Architecture:** Commit the abandoned worktrees' source changes to local recovery branches, export a self-contained Git bundle, and archive ignored runtime data separately. Verify restoration and retire the old worktrees; detailed comparison with master can wait until a saved change is needed. Deliver #259 independently, then make separately reviewed Windows repair commits on one branch from updated `master` and integrate them through one maintenance PR.

**Execution update:** Approved on 2026-09-08. The six obsolete/delivery worktrees and their local branches have been retired after archive verification. Issue #259 was delivered through PR #261 at `e7a7739e`; every configured check passed. Windows repairs now use `alex/windows-quality-recovery`. Each family retains focused validation and review; the full Windows and Linux gates run at the final integration checkpoint. This consolidates CI runs after the Linux gate measured 33m54s, while keeping #259 separate. The private archive retains the disposable restoration clone because automated approval review blocked its deletion.

**Tech Stack:** Git, PowerShell, uv 0.12.8, Python 3.13.15, pytest 8.4.2, pyrepo-check, Node/Playwright, GitHub Actions.

**Spec:** The user's cleanup and failure-repair request in this task; the required outcome and boundaries below are the execution specification.

## Required outcome and boundaries

- End with the main `master` checkout and, only while work is active, one named implementation worktree. No unexplained detached worktrees or obsolete issue branches remain.
- Preserve every unique source change, untracked file, and relevant ignored profile/database/log before removing its checkout. A clean Git status does not prove there is no runtime data.
- Account for all 28 diagnostic node IDs: 23 matching baseline failures, four failures not reproduced individually, and one paired runner timeout.
- Native Windows and Linux canonical gates pass. The existing macOS launcher and Windows security/ownership CI jobs continue to pass.
- Required Windows security tests execute. Do not obtain a green result through blanket Windows skips, weaker assertions, unchecked file opening, dereferencing reparse points, or removing retained-handle protections.
- Use only uv for Python execution. Use the selected checkout's `./agileforge-dev` for development runtime operations; read `info --profile <the selected profile> --json` before runtime mutations. Do not start, copy into, or reset another worktree's profile.
- Preserve the main checkout's active PID Extract profiles. This is AgileForge repository maintenance; it does not authorize PID Extract workflow/provider actions or issue #232/#260 product changes.
- Do not change global Git settings, enable Windows Developer Mode, elevate the user's session, alter branch protections, or waive required CI as a shortcut.
- The original planning stage performed no mutations. Execution was subsequently approved, as recorded above; the required outcome and preservation boundaries still apply.

## Verified starting state — 2026-09-08

| Checkout | Source state | Retained data | Planned disposition |
| --- | --- | --- | --- |
| Main `C:/Users/atavares/Projects/agileforge` | `master` at `844fc0d3`; two untracked #259 planning documents before this plan was added | Four profiles, six database files, and logs | Keep. Reconcile duplicate #259 documents after delivery. |
| `C:/Users/atavares/.codex/worktrees/1a96/agileforge` | Detached `fecc9c0a`; four modified tracked files and untracked `tests/windows/test_distribution_runtime_windows.py` | E2E profile and two root logs; about 1.84 MB | Commit to a local recovery branch, verify the archive, then retire. |
| `C:/Users/atavares/.codex/worktrees/a824/agileforge` | Detached `fecc9c0a`; verifier and smoke-test changes | Two XML evidence files, 7,410 bytes | Commit to a local recovery branch, verify the archive, then retire. |
| `C:/Users/atavares/.codex/worktrees/b6be/agileforge` | Detached `fecc9c0a`; verifier and smoke-test changes | No `.agileforge` or root logs found | Commit to a local recovery branch, verify the archive, then retire. |
| `.worktrees/alex-issue-251-story-readiness-content` | Clean `f6b93c2c`, ancestor of master; PR #253 merged | E2E profile and two root logs; about 1.86 MB | Archive evidence; remove worktree and merged local/remote branch. |
| `.worktrees/alex-issue-252-windows-secrets-file` | Clean `b220ebd8`, ancestor of master; PR #258 merged | 51 profile/audit/log files; about 3.94 MB | Archive evidence; remove worktree and merged local/remote branch. |
| `.worktrees/alex-issue-259-pending-sprint-review` | Clean `bf7340ea`; one unmerged commit; no remote branch or PR | Two root logs; about 0.50 MB | Keep through PR review and merge, then retire. |

Remote `master` also points to `844fc0d3`. Its current CI run succeeded: https://github.com/arduinitavares/agileforge/actions/runs/34160128949 . The workflow runs the full canonical gate on Ubuntu; Windows currently runs two focused suites, not the complete gate. This explains why green existing CI does not establish a green native Windows full suite.

The three detached worktrees overlap distribution work already merged through PR #257. Their snapshot commits preserve that work without claiming it is redundant, correct, or ready to merge. Comparing every hunk with master is not a prerequisite for archiving and removing a fully recoverable checkout.

## Task 1: Preserve the recovery evidence

**Inputs:** The seven-worktree inventory, Git state, and the existing diagnostic artifacts.

**Output:** A private, restorable archive outside all checkouts and outside Temp. Proposed parent directory: `C:/Users/atavares/Documents/AgileForge-recovery/2026-09-08`; choose a new invocation subdirectory rather than overwrite an existing archive.

- [ ] Record each checkout's resolved absolute path, HEAD, branch, staged/unstaged/untracked state, and ancestry to the current remote master. Record the snapshot date and all commands.
- [ ] In each abandoned detached checkout, create its own local recovery branch: `alex/recovery-1a96-20260908`, `alex/recovery-a824-20260908`, and `alex/recovery-b6be-20260908`. If a name already exists, verify its contents before proceeding; never overwrite an existing recovery ref.
- [ ] Commit all inventoried tracked modifications/deletions and untracked source files as a clearly labelled WIP snapshot on the corresponding recovery branch. Stage the explicit file inventory, including the new Windows distribution test in `1a96`. This intentionally records the current combined working state rather than preserving partial-staging boundaries. Do not add secrets, ignored databases/logs, virtual environments, or caches to Git.
- [ ] Snapshot commits are archival, not implementation acceptance. They do not need passing tests to preserve the work, and they are not merged to master or pushed to GitHub.
- [ ] Create a self-contained Git bundle containing the three recovery branches and the existing master/#251/#252/#259 refs, including their required history. Preserve the main checkout's untracked planning files as ordinary files alongside the bundle; keep main on master. The two #259 documents will also be present in the #259 ref if their contents match that commit.
- [ ] Inventory ignored files beyond ordinary `.venv`, bytecode, and disposable caches. Preserve `.agileforge`, root `logs`, and any other audit or unique data; do not stage these into Git or publish them.
- [ ] Before copying a database, verify that no process is writing the profile. If active, use the matching checkout's supported lifecycle to quiesce that profile or obtain a consistent database backup. Do not copy only a live SQLite main file while ignoring its WAL, and do not stop unrelated main-checkout runtimes.
- [ ] Copy `C:/Users/atavares/AppData/Local/Temp/issue259-diagnostic` and `C:/Users/atavares/AppData/Local/Temp/codex-issue259-review-bf7340ea` into the archive. These include the preserved 28-node list, paired JSON evidence, and corrected review.
- [ ] Write a manifest containing original path, size, SHA-256, original HEAD, recovery branch and snapshot commit SHA, artifact role, and restoration instructions. Preserve raw copies of any unique source files whose bytes are transformed by Git filters, and record that distinction. Keep any credential-bearing runtime material private.
- [ ] Verify copied hashes and run `git bundle verify`. Clone the bundle into a disposable directory, restore each recovery branch, and compare its commit/tree and intended source contents with the original snapshot. Check separate raw/runtime files against the manifest. Remove only this disposable verification directory after the comparison.

**Completion:** Every source/data item scheduled for deletion has a verified recovery path. Do not treat a stash, patch-only backup, or clean Git status as sufficient by itself.

## Task 2: Retire the merged #251 and #252 worktrees

**Inputs:** Verified archive from Task 1; current remote ancestry and PR merge state.

- [ ] Recheck that both worktrees remain clean and both branch tips are contained in current remote master. Recheck no active process uses either checkout.
- [ ] Resolve and validate the exact two target paths below, then remove them through Git, one at a time. These commands are for execution after the checks, not instructions to run during planning.

```powershell
git worktree remove 'C:/Users/atavares/Projects/agileforge/.worktrees/alex-issue-251-story-readiness-content'
git worktree remove 'C:/Users/atavares/Projects/agileforge/.worktrees/alex-issue-252-windows-secrets-file'
git branch -d alex/issue-251-story-readiness-content
git branch -d alex/issue-252-windows-secrets-file
```

- [ ] Delete only those two merged remote branches if still present. Do not use a wildcard or mirror push.
- [ ] Verify the remaining worktree list, branch list, archive manifest, and unchanged main profiles.

**Completion:** Two worktrees and their two obsolete branch pairs are retired, with evidence recoverable. If Git reports unexpected dirt, investigate the changed state rather than immediately using force.

## Task 3: Retire the three archived recovery worktrees

**Source inventory preserved in Task 1:** `.github/workflows/ci.yml`, `scripts/verify_distribution.py`, `tests/test_ci_contract.py`, `tests/test_distribution_smoke.py`, `tests/windows/test_distribution_runtime_windows.py`, and any additional exact paths in each fresh status report.

- [ ] Recheck each archive against the current recovery branch commit and require a clean tracked/untracked source state. Account explicitly for the new Windows distribution test in `1a96` and all separately archived ignored data.
- [ ] Verify no process still uses the checkout, then remove each exact worktree through ordinary `git worktree remove`. Snapshot commits should make force-removal unnecessary. If new dirt appears, update and reverify the snapshot rather than discard it.
- [ ] Once the bundle has been independently cloned and every recovery commit verified, delete only the three temporary local recovery branches by their exact names. These archival commits are intentionally unmerged, so normal `git branch -d` may refuse: any forced branch deletion must be limited to the recorded, verified bundle-backed commit. Do not force-delete the #259 delivery branch.
- [ ] Keep the bundle and manifest as the archive of unresolved old work. Compare a snapshot with current master later only if a repair batch needs it; do not merge the snapshot wholesale.
- [ ] Use native PowerShell path handling for any filesystem cleanup. Validate every resolved target against its explicitly named original worktree directory before recursive removal; do not enumerate in PowerShell and pass deletion commands to another shell.
- [ ] Verify only main and the #259 worktree remain. Recovery archives stay outside the repository; any live recovery branch remains named and explained until incorporated or retired after verification.

**Completion:** The three abandoned checkout directories and temporary recovery branches are gone. All their source changes are recoverable from verified snapshot commits in the private bundle, and their non-Git data is recoverable from the accompanying archive. No semantic reconciliation is required to complete cleanup.

## Task 4: Deliver #259 independently

**Files:** The existing reviewed five-file commit `bf7340ea`; no additional Windows repairs in this PR.

- [ ] Recheck HEAD and clean status, push `alex/issue-259-pending-sprint-review`, and open a PR against master using the corrected review's qualified verification wording.
- [ ] Include the current 139 frontend / 7 browser / static-check evidence and disclose that native Windows canonical validation failed. Explain the 23 matching baseline failures, four paired passes, and one incomplete paired timeout without inventing root causes.
- [ ] Read all existing PR comments and all CI job outcomes for the current head before responding or deciding to merge. Current configured jobs are Linux full gate, Node frontend, macOS launcher, Windows evidence security, and Windows runtime ownership.
- [ ] Merge when review and required CI are satisfied. Investigate newly observed failures; do not disable checks or claim this plan itself waives them. If CI is already green, the separate Windows repair effort need not be folded into #259.
- [ ] Update main by fast-forward. The two untracked #259 plan/design files in main may collide with incoming tracked files: compare them with the PR versions, archive any differences, and remove only verified duplicate copies before updating.
- [ ] Archive the #259 worktree's final evidence, then remove its worktree and merged local/remote branch using the same checks as Task 2.

**Completion:** #259 is merged and main is current; no issue worktree remains. Until the merge happens, keep the reviewed worktree available.

## Repair workflow for Tasks 5–9

- All Windows repair families use the single named `alex/windows-quality-recovery` branch created from updated master after #259. Commit and review each family separately, then merge and retire the worktree after final integration validation. Do not accumulate one worktree per test.
- Gemini implements the bounded batch. Codex reviews behavior, actual diffs, and evidence; use a fresh security review for changes to filesystem protections. Honor the user's explicit model-selection policy for all Codex delegates.
- Start with the existing failing tests. Add a regression only when it verifies a newly diagnosed case or guards a meaningful contract; avoid duplicate tests that merely mirror implementation.
- Use the smallest supported `pyrepo-check --python 3.13.15 --format json pytest <exact-node-or-file>` selection during development. Confirm CLI help before using extra selectors. Retain complete envelopes outside the checkout.
- For unresolved hangs/order-dependent behavior, capture pytest phase/stack evidence under a bounded watchdog and ensure the whole owned test process tree exits. A timeout record must preserve partial output and stacks, not just an invented exit code.
- Run targeted static/security checks for each family, and all existing PR CI plus the new Windows full gate on the final maintenance PR. A family can be accepted with the remaining known Windows baseline failures explicitly recorded; that is not a passing Windows full gate. Run the complete native Windows canonical gate at the final integration checkpoint, or earlier when new changes introduce a specific unresolved integration concern. Use the checkout's launcher through `sh ./agileforge-dev check`; retain its full terminal output and real exit code. JSON tail summaries alone are insufficient failure evidence.
- Do not rerun the hour-long gate repeatedly on unchanged source to chase a known failure. Use focused diagnostics until a change or new evidence justifies the next full run.

## Task 5: Repair exact-byte fixtures — four failures

**Files:** New `.gitattributes`; `tests/issue_210/test_authority_surface_removed.py`; `tests/test_specification_structuring_quality_benchmark.py`; `tests/services/test_specification_authoring_input.py`; the hash-pinned fixture paths referenced by those tests.

- [ ] Preserve all expected source hashes and intentional BOM/CRLF cases. Compare Git blob bytes, checked-out bytes, and test-generated bytes separately.
- [ ] Add narrow repository attributes for byte-pinned fixture paths so checkout conversion cannot alter them. `-text` preserves exact blob bytes; use explicit `text eol=lf` for shell entrypoints where LF is the intended contract. Avoid a repository-wide renormalization or changing global `core.autocrlf`.
- [ ] Fix the separate generated-fixture problem in `registered_source`: CONTEXT and ADR fixtures use `write_text` with the Windows default newline conversion. Write their intended LF bytes explicitly. The primary SPECIFICATION fixture deliberately contains BOM/CRLF and must retain those bytes.

```python
(repository / "CONTEXT.md").write_bytes(b"Context only from registration.\n")
(repository / ADR_PATH).write_bytes(b"# Exact source decision\n")
```

- [ ] Run the four exact failing nodes, adjacent exact-source tests, and the fixture membership/hash checks in a fresh Windows checkout. Require the existing hashes to pass unchanged.
- [ ] Review and commit this family; retain Linux byte-integrity coverage at the maintenance PR's integration gate.

**Completion:** The four failures pass without normalizing product input or rewriting golden hashes to accommodate platform transformations.

## Task 6: Repair path and distribution portability — three failures

**Files:** `adapters/git/repository_probe.py`; `tests/services/test_repository_probe.py`; `scripts/verify_distribution.py`; `tests/test_distribution_smoke.py`; relevant retained detached-worktree patches. Keep probe and distribution fixes in separate commits because their diagnoses are independent.

- [ ] For embedded NUL input, enforce the `MALFORMED_PATH` contract before filesystem existence checks; retain missing-path versus malformed-path distinctions and public error redaction. Run the existing exact invalid-path test on Windows and Linux.
- [ ] Fix the surrogate filename test setup: its current failure happens in `os.fsdecode(b"surrogate-\xff.txt")`, before the probe runs. Separate POSIX raw-byte filename coverage from Windows Unicode/path coverage without dropping deterministic normalization or fingerprint assertions.
- [ ] For the snapshot failure, capture exact source/destination paths and lengths at the failing copy. Reproduce under both the observed nested path and a short temporary root to determine whether this is path length, missing directories, or another Windows copy failure. Do not assert a root cause from WinError 3 alone.
- [ ] Incorporate only relevant, reviewed recovery changes. Fix the confirmed snapshot cause and retain the test's existing guarantees: tracked working-tree edits appear in the artifacts, ignored stale build files do not, and the checkout remains unchanged.
- [ ] Run the probe suite, exact snapshot regression, wheel/sdist verification, and real Windows distribution runtime test. Preserve existing isolation, no-credential-leak, and path safety behavior.

**Completion:** The three failures pass; malformed input is classified correctly and distribution snapshots work under the supported nested Windows checkout layout.

## Task 7: Repair filesystem-security test execution — thirteen failures and four unresolved cases

**Files:** `tests/services/test_vision_evidence.py`; `tests/windows/test_vision_evidence_windows.py`; `tests/services/test_vision_evidence_reader.py`; `tests/test_socket_marker_contract.py`; `tests/conftest.py`; adapter files only when evidence proves a product defect.

- [ ] For the nine privilege failures, distinguish tests whose behavior actually requires a symlink from tests using a symlink merely as setup. The two socket-marker tests can use copied conftest content while still exercising their collection contract.
- [ ] For actual symlink security tests, retain capability-aware local behavior and require execution in a Windows CI environment with the needed capability. Missing capability must be explicit and must fail a CI job that claims to verify that protection; do not turn every security failure into a skip.
- [ ] Correct the four race tests that patch POSIX `os.read`/`os.open` while the Windows adapter uses native calls. Keep POSIX-specific tests on POSIX, and require equivalent Windows race injections against `_read_handle`, `_open_relative`, or a narrow supported test boundary. Each test must prove its injection actually executed.
- [ ] Investigate the four Windows nodes that pass alone by running the relevant modules together, then their preceding full-suite neighborhood while retaining order, temporary paths, handles, and teardown evidence. Minimize a failing sequence when reproduced; repair the demonstrated shared-state/cleanup cause. Do not label it an ordering bug before reproduction.
- [ ] If those four cases remain unreproduced after the bounded combined run, retain that status and require them to execute successfully in the subsequent complete Windows gate; do not claim a specific fix occurred.
- [ ] Run real regular-file, reparse/symlink, traversal, replacement, growth, deletion, parent-handle retention, and secret-redaction cases on their applicable platforms. Require fresh independent review for any production reader change.

**Completion:** All thirteen deterministic failures are either genuinely repaired or correctly platform-scoped with executing native equivalents. The four previously intermittent tests execute in the final combined/full suite. Security guarantees and negative-case coverage remain intact.

## Task 8: Repair launcher-test portability — three failures

**Files:** `scripts/ci_launcher_smoke.py`; `tests/test_ci_launcher_smoke.py`; Windows runtime/launcher tests under `tests/windows` and `tests/dev_runtime`.

- [ ] Identify which process-group contracts are intentionally POSIX-specific. Run `os.killpg` tests only where that API exists, and verify equivalent Windows process ownership/termination guarantees in the native suite.
- [ ] Correct script invocation at the boundary that currently passes a script directly to Windows process creation. Use an explicit interpreter/shell argv appropriate to that entrypoint; preserve exact checkout and profile binding.
- [ ] Exercise complete startup, read verification, shutdown, pre-identity failure cleanup, and absence of surviving owned child processes. Do not weaken serving-PID identity checks or substitute descendant PID acceptance.
- [ ] Run Linux/macOS launcher smoke and the native Windows ownership suites, including the real installed-distribution regression. Keep required-execution assertions in CI.

**Completion:** Three launcher failures are accounted for with the intended platform contracts covered; no process or profile leak is accepted as a test workaround.

## Task 9: Diagnose and repair the ADK workflow timeout — one incomplete case

**Files:** `tests/workflow/test_product_definition_facts.py`; fixture owners in that module/`tests/conftest.py`; actual ADK/runtime lifecycle code only if the captured stack identifies a product ownership bug.

- [ ] Run the exact `test_loader_keeps_interview_turn_after_configured_adk_trace_database_is_deleted` node with retained phase markers and timed stack dumps around session-service construction, `_persist_trace_session`, event-loop completion, file deletion, fact loading, and teardown.
- [ ] Capture where execution blocks before choosing a fix. Both previous JSON/stdout files were empty and exit 124 was assigned by the diagnostic runner; the SQLite-lock explanation is not established.
- [ ] Inspect the installed ADK resource lifecycle and retrieve its current official documentation before changing library-specific cleanup. Close/dispose resources through their supported owner and event loop when that is the proven cause; do not add arbitrary sleeps or catch-and-ignore deletion errors.
- [ ] Preserve the test's actual contract: deleting the trace database must not remove or change the business interview fact. Keep business and trace databases separate, and leave no owned connections/processes behind.
- [ ] Verify the focused case completes on Windows and Linux, then run the surrounding module to detect teardown interactions.

**Completion:** A diagnosed cause, tested repair, completed pytest evidence, and bounded clean teardown replace the timeout-only record.

## Task 10: Add lasting CI coverage and finish cleanup

**Files:** `.github/workflows/ci.yml`; `tests/test_ci_contract.py`; `tests/dev_runtime/test_dev_checks.py` only if launcher output behavior is changed; concise developer validation documentation as needed.

- [ ] Add a native Windows full canonical gate after the repair batches. Install the same pinned uv/Python/controller and browser prerequisites as required by the actual suite. Size its job timeout from the measured Windows run with headroom; the observed 53-minute local run will not fit the existing Linux 45-minute job limit.
- [ ] Retain complete failure output/JUnit or structured results as CI artifacts on failure. Preserve the real command exit code when capturing output. Keep the existing security/ownership jobs and their required-test execution checks.
- [ ] Update and run the repository's CI-contract tests against the final job definitions and required execution behavior.
- [ ] Run the final combined Windows gate from a frozen clean revision and retain before/after HEAD and Git state. Require exit 0 for all canonical stages; review intended platform/capability skips explicitly. Linux canonical and macOS launcher CI must also pass.
- [ ] Reconcile all 28 original node IDs against current outcomes; record renamed or platform-split replacements instead of silently dropping entries. Resolve any newly exposed failures before calling Windows support complete.
- [ ] Merge the final maintenance PR, fast-forward main, archive final evidence, and remove the merged repair worktree and branch. Retire recovery refs only after their contents are incorporated or verified in the external archive.
- [ ] Include this approved plan in a maintenance PR so it becomes tracked documentation. When that PR reaches main, reconcile its original untracked copy by content, just as for the #259 documents, rather than leaving another unexplained local file.
- [ ] Verify `git worktree list` contains only `C:/Users/atavares/Projects/agileforge`; `git branch` contains only master unless a specifically named active task exists; `git status` has no unexplained source changes; main runtime profiles remain available.

**Completion:** Obsolete worktrees/branches are gone, evidence is recoverable, #259 is delivered, and the full Windows gate is covered by CI rather than relying on an occasional manual diagnostic pass.

## Immediate execution order

1. Commit WIP source snapshots, bundle them, archive non-Git data, and verify restoration; retire #251/#252 and the three former detached worktrees.
2. Push/open the #259 PR, review CI, merge, and retire that worktree. PR CI can run while archive verification continues.
3. Use one implementation worktree for the five repair families, with separate commits and reviews where causes are independent.
4. Add the Windows full CI gate and finish with master as the sole idle checkout.

No new GitHub issues or PRs were created while preparing this plan. If tracking is desired during execution, create one Windows-quality umbrella issue with the five bounded repair families above and link their concrete acceptance checks; cleanup itself uses the private recovery manifest.

## Documentation checked

- Git worktree removal and safeguards: https://github.com/git/htmldocs/blob/gh-pages/git-worktree.adoc
- Git attributes and newline conversion: https://github.com/git/htmldocs/blob/gh-pages/gitattributes.adoc
- Self-contained bundles and clone-based restoration: https://github.com/git/htmldocs/blob/gh-pages/git-bundle.adoc
- Existing CI definition: `.github/workflows/ci.yml` at `844fc0d3`.
