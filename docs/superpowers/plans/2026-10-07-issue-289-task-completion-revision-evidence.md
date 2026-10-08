# Task Completion Revision Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task after Phase 1 review and explicit implementation approval. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind new Task completions to a completion-time observation of the Project's bound target worktree, and make uncommitted delivery visible and intentional.

**Architecture:** Reuse `RepositoryProbe`/`GitPythonRepositoryProbe` to prepare repository evidence before the SQLite writer transaction, then verify HEAD and dirty state cheaply inside it. Store a canonical, versioned repository-evidence payload on original and retry completions, include it in new completion fingerprints, and expose it through existing read projections and the selected Task inspector.

**Tech Stack:** Python 3.13.15, uv, SQLModel/SQLAlchemy, SQLite, Pydantic, GitPython, argparse, FastAPI, existing JavaScript dashboard and Node test harnesses.

**Spec:** The DESIGN section of this file is the specification. External Claude Opus 5.5 review returned REQUEST CHANGES; the maintainer approved Phase 2 with the decisions incorporated below. The Phase 1 verification record remains historical evidence.

## DESIGN

### 1. Baseline, observed behavior, and root cause

Issue: [arduinitavares/agileforge#289](https://github.com/arduinitavares/agileforge/issues/289), P2 enhancement. Read with `gh issue view 289 --repo arduinitavares/agileforge --comments`, then JSON including comments because the first invocation returned no rendered output. The issue has no comments.

Actual checkout: clean detached HEAD `b3a4fb415de75e92fdfa5709986a6ba690eb6b51`. Requested baseline: `07654b8`. `git diff --stat 07654b8 HEAD` contains only `AGENTS.md` and three `docs/agents/` files; production code and tests are identical. Git state has not been changed.

Evidence map (line numbers refer to the actual checkout):

| Surface | Evidence | Consequence |
| --- | --- | --- |
| CLI | `cli/main.py:832-862`, `2406-2425` | Completion supplies outcome, artifact refs, acceptance, checklist, and transport identity; no repository acknowledgement. |
| API/application | `api.py:413-420`, `1932-1949`; `services/application.py:2166-2173`, `4070-4126` | Strict semantic request, replay before position/transition, no repository capture. |
| Validation/persistence | `services/task_execution_service.py:382-450`, `495-592` | Checks Sprint/Task ownership, open state, checklist, execution lineage and dependencies, then writes original or retry evidence without inspecting Git. |
| Stored evidence | `models/workflow.py:523-550`; `models/sprint_retry.py:179-224` | Neither evidence table stores target revision or dirty state. |
| Hash binding/read | `workflow/execution_integrity.py:637-686`; `repositories/workflow.py:3788-3845`; `repositories/sprint_retry.py:305-373` | Completion hash binds semantic execution evidence, but not completion-time repository state. |
| Task show | `services/read_projections.py:4325-4428`; `cli/main.py:1246-1253`, `1110-1112` | JSON-only show exposes completion Facts; no additional human-text CLI renderer exists. |
| Existing inspection | `services/repository_probe.py:87-104`; `adapters/git/repository_probe.py:103-147`, `184-202`, `305-351` | Probe already supplies normalized worktree/common directory, HEAD, branch/detached state, status entries/fingerprint, dirty flag and inspection time, with typed failures. |
| Existing provenance comparison | `services/specification_source_registration.py:558-590` | `_probe_matches_context` compares a live observation with an old binding. Completion must reuse the probe, but must not require the live HEAD/status to equal the attachment-time HEAD/status. Ordinary delivery changes those fields. |
| Dependencies/packets | `workflow/definitions/execution.py:329-410`, `510-548`; `workflow/execution_scope.py:51-69`; `services/packets/canonical.py:1233-1284`, `1345-1361`, `1416-1449` | Next Task selection uses status, Story prerequisite terminal states and minimum Task ID. There is no product revision checkout/starting-revision selector. The issue's operational consequence is an external harness/operator concern, not an existing checkout algorithm to modify. |

Provider-free reproduction: a throwaway script outside the repository seeded real accepted planning/execution facts, attached a clean disposable Git repository through persisted binding rows, then completed a Task through `WorkflowDomain.transition`. A second case changed one tracked path and added one untracked path after binding. Each case used its own disposable file-backed SQLite database.

```text
clean: completion_ok=true; status=Done; acceptance_result=fully_met;
       observed_dirty=false; dirty_path_count=0; head_recorded_on_completion=false
dirty: completion_ok=true; status=Done; acceptance_result=fully_met;
       bound_dirty=false; observed_dirty=true; dirty_path_count=2;
       head_recorded_on_completion=false
both: sprint_task_show.data.completion contains only existing semantic evidence;
      target HEAD remains unchanged
```

Local artifacts: `$TMPDIR/issue289-repro.hQZjoxzMEM/repro.py`, `repro.log`, and `run2/{clean,dirty}.db`. The first script run completed the clean Task but failed at a mistaken projection call; the corrected run uses `sprint_task_show` and produced both results above. These temporary artifacts are diagnostic evidence, not implementation or new repository tests.

### 2. Intent, alternatives, and selected policy

The operator needs to know which committed baseline was observed when delivery was declared, and whether additional work existed only in that bound worktree. Acceptance remains a semantic judgment; a clean repository alone does not prove that artifact refs or Task requirements were delivered.

| Design | Benefits | Trade-offs / disposition |
| --- | --- | --- |
| Record and warn for every dirty/detached completion | Lowest caller friction; purely additive observation | Dirty `fully_met` can still be submitted accidentally. Reject as the default for dirty full acceptance. |
| Refuse dirty or detached `fully_met` unconditionally | Forces a durable commit/branch convention | Blocks legitimate uncommitted and detached-worktree workflows, and pressures callers to commit. Reject: no automatic commits are allowed and semantic acceptance need not imply a branch. |
| **Record every new completion; require explicit acknowledgement for dirty `fully_met`; warn for clean detached HEAD** | Makes uncommitted full acceptance intentional without taking over Git; clean detached delivery records an exact existing commit | Adds a boolean to write contracts and requires careful replay compatibility. **Selected.** |
| Dedicated observation table or many nullable scalar columns | Relational queryability / more database constraints | A side table adds joins/ownership relationships; many columns increase migration surface and partial-state combinations. Prefer one typed canonical JSON column per existing evidence table for this bounded change. |

Policy applies equally to original and retry Tasks, at the shared completion-service boundary:

| Observed repository state | `fully_met` | `partially_met` | Recorded/read behavior |
| --- | --- | --- | --- |
| Clean, attached branch | Allow with default acknowledgement false | Allow | Exact snapshot, no repository warning. |
| Dirty, attached or detached | Refuse unless `uncommitted=true` | Allow without requiring acknowledgement | Snapshot always records dirty state/count. Accepted dirty completion warns that changes are outside HEAD; full acceptance records explicit acknowledgement. |
| Clean, detached HEAD | Allow without acknowledgement | Allow | `branch_name=null`, `detached_head=true`, exact HEAD, detached warning. A detached commit is still a valid revision; branch reachability is not inferred. |
| No active Repository Binding | Preserve existing ability to complete | Preserve existing ability to complete | Explicit new `not_bound` evidence and warning; do not invent a clean state or revision. Making attachment mandatory is a separate product decision. |
| Bound target cannot be inspected, including missing/unreachable path, unreadable Git metadata or unborn HEAD | Require `uncommitted=true` | Allow | Persist `unavailable` evidence with bounded summary and `REPOSITORY_UNAVAILABLE` warning. Never invent HEAD/clean state. |
| Explicit worktree belongs to a different common Git directory; missing/cross-project binding identity | Refuse even with acknowledgement | Refuse | Specific mismatch/ownership error; acknowledgement cannot bypass a real mismatch. |
| HEAD or dirty flag changes between outside capture and inside verification | Refuse, even with acknowledgement | Refuse | Name the revision-change rule; changed requests require a new idempotency key. Do not automatically resubmit. |

`uncommitted=true` on a clean or unbound completion is permitted and recorded as caller intent; it does not make that repository dirty and does not relax any other completion check. Detached means the probe's detached state, not a new search for branches that contain HEAD.

### 3. Evidence fields and capture boundary

Create frozen DTOs in `services/contracts/task_repository_evidence.py`, independent of workflow/model imports, with a discriminated `state` (`captured`, `not_bound`, `unavailable`), closed fields, strict boolean acknowledgement, and version `agileforge.task-repository-evidence.v1`:

| Field | Captured variant | Not-bound variant |
| --- | --- | --- |
| `version` | Fixed version above | Same version |
| `state` | `captured` | `not_bound` |
| `repository_binding_id` | Active Project-owned binding ID | Absent |
| `repository_binding_fingerprint` | Exact immutable binding fingerprint | Absent |
| `worktree_path`, `common_git_dir` | Normalized actual target identity | Absent |
| `head_sha` | Full probe-reported commit SHA | Absent |
| `branch_name` | Branch name or null when detached | Absent |
| `detached_head` | Boolean | Absent |
| `dirty` | Boolean | Absent |
| `dirty_path_count` | Nonnegative count of distinct `entry.path` values | Absent |
| `dirty_paths`, `dirty_paths_truncated` | Sorted first 50 distinct dirty paths, and whether the complete count exceeds 50 | Absent |
| `probed_path_matches_binding` | True only when normalized probed path equals bound checkout | Absent |
| `other_worktrees_present` | Observed only when no override was supplied | Absent |
| `status_fingerprint` | Existing probe fingerprint | Absent |
| `probe_version` | Existing probe version | Absent |
| `inspected_at` | Probe's server-generated UTC timestamp | Absent |
| `uncommitted_acknowledged` | Submitted strict boolean | Submitted strict boolean |

The not-bound variant intentionally has no observation timestamp: `completed_at` already records when the completion was stored. SQL NULL is reserved for historical evidence without a recorded revision; it is different from a new explicit not-bound observation.

The `unavailable` variant records binding ID/fingerprint, selected `worktree_path`, `probed_path_matches_binding`, acknowledgement, reason code `REPOSITORY_UNAVAILABLE`, typed probe failure code and a bounded safe summary (maximum 240 characters). It omits HEAD, branch, dirty state/count and dirty paths. Summaries use existing typed probe messages, never raw subprocess stderr or arbitrary exception text. An ordinary failed cheap verification after capture becomes unavailable under the same policy; an observed HEAD/dirty mismatch remains a refusal. A cheap verification timeout is a distinct retryable refusal that rolls back the writer transaction and receipt claim, even when uncommitted work was acknowledged.

`dirty_path_count = len({entry.path for entry in probe.status_entries})`: staged and unstaged changes to the same path count once; a rename counts its current path once; distinct untracked files count individually; ignored paths follow existing probe behavior. Do not use `len(status_entries)` or a CLI porcelain-line count.

Add `services/task_repository_evidence.py` to load the current Project-owned binding and use the injected existing probe. Optional CLI `--worktree PATH` / API `worktree_path` selects a worktree; the server probes it and requires its resolved absolute common Git directory to equal the bound repository's recorded resolved common directory. Run a completion-specific `inspect_common_git_dir` preflight before full capture, without requiring HEAD/status, using GitPython's Git metadata boundary equivalent to `git rev-parse --git-common-dir`. An observable foreign identity must refuse even when that repository has no commit or its full metadata parser fails; a genuinely unreadable identity or a same-repository metadata failure becomes unavailable. Normalize through existing probe APIs; never shell out ad hoc. An unrelated repository yields a specific `WORKTREE_REPOSITORY_MISMATCH` refusal. A supplied worktree with no binding yields `WORKTREE_REQUIRES_BINDING`. HEAD, branch and status may legitimately differ from attachment-time observations. Do not refresh the Repository Binding or mutate the Project/target repository.

When no override is supplied, probe the bound checkout and inspect topology through the GitPython adapter's worktree-list boundary. If another worktree exists, record `other_worktrees_present=true` and warning `OTHER_WORKTREES_PRESENT`, even when the bound checkout is clean. Do not inspect other worktrees' files automatically. With an override, record its actual path and `probed_path_matches_binding`; no topology warning is required. GitPython may invoke Git internally through its supported API; product/service code does not add subprocess capture.

Prepare identity/full status/topology evidence in a read-only Session **before `BEGIN IMMEDIATE`**. `WorkflowDomain.transition` first checks existing receipts read-only, prepares the evidence for new CompleteTask requests, then retains its existing transactional receipt/guard recheck. Thread immutable prepared evidence through local method arguments, never mutable coordinator state. Inside the write transaction, confirm the selected binding ID/fingerprint and perform one cheap `TaskRepositoryProbe.inspect_revision` call returning HEAD SHA and dirty boolean only. Add this completion-specific protocol as an extension of `RepositoryProbe`, leaving the existing one-method base protocol and unrelated test doubles unchanged. `GitPythonRepositoryProbe` implements the additional identity/cheap/topology methods. A changed HEAD/dirty flag or binding identity refuses with an actionable message naming the rule and need for a new idempotency key. Policy, fingerprint and business writes then use verified prepared evidence within the existing transaction.

Use a server-side `PreparedTaskRepositoryEvidence` and explicit probe dependency. `complete_task_in_session` receives prepared evidence from the domain; a bound direct-service caller must prepare it before beginning its writer transaction, and missing prepared evidence refuses rather than performing a full scan inside the lock. Unbound direct-service callers can form not-bound evidence without Git I/O. Both original and retry paths share capture/policy/verification helpers. The public guarded request contains only semantic acknowledgement and optional path, never the prepared evidence. Never accept a caller-reported snapshot, HEAD, branch, count or fingerprint.

A real mismatch found during outside preparation is carried as a typed server-owned refusal into the normal receipt/guard transaction. It must produce the existing durable conflict result rather than an uncaught preparation error or an early return that skips refusal idempotency.

This records an observation, not a file-content archive or atomic Git/SQLite transaction. The existing status fingerprint hashes normalized status metadata, not dirty file bytes. Cheap verification detects HEAD/dirty changes but cannot rule out changes that occur and revert, changed paths/bytes while dirty stays true, branch changes at the same HEAD, or changes after verification. State these limits; do not add source hashing, target locking, automatic commits or worktree preservation.

Keep probes read-only even for Git's optional index-stat refresh: use a scoped GitPython environment with `GIT_OPTIONAL_LOCKS=0` and probe-owned command-local `-c diff.autoRefreshIndex=false`, without changing process-global environment or target configuration. With refresh disabled, retain existing normalized full status entries and filter raw stat-only entries using Git's actual-content comparison with external/textconv helpers disabled. One NUL-delimited name-only diff and Python filtering stay outside the writer transaction; no per-path Git subprocesses are used. The cheap probe derives only a dirty boolean from GitPython porcelain status with normal untracked-directory reporting; it does not parse paths, build entries/fingerprints or calculate line-diff statistics inside the writer transaction. A temporary-repository test touches a tracked file's timestamp without changing content and verifies clean state plus unchanged index bytes/timestamp across full and cheap probes, and real modifications still report dirty. Git documents refresh in [git-status Background Refresh](https://git-scm.com/docs/git-status#_background_refresh) and [git-diff configuration](https://git-scm.com/docs/git-diff#_configuration).

Full inspection uses literal filename semantics and obtains NUL porcelain current paths, rename sources and explicit worktree/untracked area sets before unrestricted raw-diff calls. Filter complete NUL-framed raw records against tracked paths in Python before the pinned GitPython parser, skip raw diff for clean status, then check normalized current-path completeness. Changed leading-colon tracked paths retain safe typed unavailable handling; unchanged, embedded-colon and untracked-colon paths remain supported. Never expand changed paths into Git argv or rewrite the third-party normalization parser.

Submodule coverage: when a porcelain worktree path is absent from all normalized diff entries and its stage-zero index entry is a gitlink (mode `160000`), record one `worktree` / `modified` entry before the completeness check. Only on a coverage gap, read metadata with one unrestricted NUL-framed `git ls-files --stage -z` call and filter in Python; the pinned SDK's index decoder rejects Git-supported index format 4. Do not open submodule objects, pass paths on argv or add per-path Git commands. Every missing non-gitlink path still fails closed. This intentionally changes `b3a4fb4`'s clean result for untracked-only submodule content: recording dirty agrees with ordinary porcelain and the existing cheap verification probe, avoids a false repository-changed refusal during completion, and preserves the required acknowledgement for fully met work. Existing represented paths, tracked submodule edits, gitlink revision changes and configured submodule-ignore semantics retain their prior results; do not add a second worktree entry when an index entry already covers the path.

### 4. Schema, migration, and immutable hash compatibility

Add `repository_evidence_json: str | None = Field(default=None, sa_type=Text)` to both `TaskCompletionEvidence` and `SprintRetryTaskEvidence`. No new SQL uniqueness/foreign-key/check constraints, no new indexes, no changed identity or completion log columns. Typed decoding validates the JSON discriminator, closed fields, canonical encoding and internal consistency; the completion fingerprint binds the payload and its binding identity.

Downgrade: older binaries reject this schema; no reverse migration is provided. Recovery is restoring an operator-owned pre-upgrade backup into a separate state location and running the matching older binary, losing any later records unless separately exported. Never remove evidence columns or rewrite completion history automatically. This task changes no real user database or backup.

Production upgrade: the database migration below does not by itself provide an in-place production-profile upgrade. `load_production_state` checks the current schema before `ensure_business_db_ready` can apply these columns, and the existing profile manifest's `business_schema_sha256` is not republished by that path. Production upgrades depend on the general supported-prior-schema registry and profile-upgrade mechanism being built in PR #298 (#230). After #298 merges and this branch is rebased, register #289's migration with that mechanism. Do not implement a separate upgrade path or change production profile loading in this correction.

- [ ] **Pending: register migration with production upgrade registry after #298.**

Preserve independently frozen manifests for the exact pre-retry schema and the current pre-289 schema before extending `CURRENT_BUSINESS_SCHEMA_MANIFEST`. Add only the two nullable TEXT columns, with SQL NULL defaults, inside the existing `BEGIN IMMEDIATE` business-database initialization transaction in `models/db.py:2203-2239`; this is the database-layer migration chain, not a production-profile startup-upgrade guarantee:

1. Empty database: create the new complete schema.
2. Exact new schema: no-op.
3. Exact pre-289 schema: add the two columns, then verify the complete new manifest.
4. Exact supported pre-retry schema: perform the existing retry-table upgrade plus the two columns in the same transaction; newly created retry tables may already have the new column, so apply the fixed DDL only to columns missing on this exact known route.
5. Unknown/mixed/partially upgraded schema: preserve fail-closed behavior, with a diagnostic naming the supported upgrades accurately. Do not generalize to best-effort migrations.

Freeze actual pre-289 DDL in `tests/fixtures/issue_289/pre_revision_business_schema_07654b8.sql` and provider-free original/retry completed history in `tests/fixtures/issue_289/pre_revision_completed_history_07654b8.sql` before changing metadata or completion capture. The history fixture supplies durable legacy payloads/receipts/hashes, not values regenerated by the changed writer. Compare old values by explicitly enumerating old columns, not `SELECT *` after adding columns. Existing pre-retry migration tests must draw legacy completion history from this frozen source: creating new captured/not-bound completions and then dropping the new column would retain new hashes with missing evidence and create invalid synthetic legacy history. Verify atomic rollback and concurrent startup serialization using disposable file databases. Preserve all existing rows, primary keys, request/result JSON, completion/Story/Sprint hashes, logs and receipts. Do not populate old evidence from current Git state.

SQLite supports adding nullable columns without rewriting table content; the transaction still needs runtime rollback/concurrency tests. Primary references: [SQLite ALTER TABLE ADD COLUMN](https://www.sqlite.org/lang_altertable.html), [SQLAlchemy connections and transactions](https://docs.sqlalchemy.org/en/20/core/connections.html). The `find-docs` Context7 route requires an unsandboxed invocation unavailable in this session; official documentation was used as a fallback. No dependency version change is planned.

Extend `TaskEvidencePayload` and `TaskCompletionFact` with optional typed `repository_evidence`. For SQL NULL, omit that new key from canonical Fact dumps in both Python and JSON mode, including nested snapshot dumps. `task_evidence_fingerprint` adds the repository payload **only when non-null**. Old semantic hashes, downstream Story/Sprint closure hashes and business/decision fingerprints remain byte-for-byte unchanged. For new captured/not-bound rows, the full payload participates in the existing evidence hash. Readers recompute it for both original and retry rows and fail closed on malformed/noncanonical/tampered evidence. For captured evidence, also validate the recorded binding's Project ownership and immutable fingerprint against its retained historical row; never require it to still be the active binding or to have the captured live HEAD/status. A later binding refresh must not invalidate an immutable completion bound to its historical binding.

Add strict optional acknowledgement and optional `worktree_path` to guarded `CompleteTask`. Normalize absent/false acknowledgement to None for canonical request serialization; true remains true. Include each new key in the hash only when non-null, preserving original request bytes/hashes (`tests/workflow/test_execution_requests.py:36-68`). Application replay normalizes absent/None/false acknowledgement to false and compares both directions, and compares absent path to None and explicit path exactly. Changing acknowledgement or path requires a new idempotency key. Replay remains before repository probing, including after the worktree disappears. Old result JSON is replayed unchanged.

### 5. API / CLI / read / UI contract

**Writes:** Add strict boolean `uncommitted` defaulting to false and optional `worktree_path` to public API/application inputs; guarded CompleteTask canonicalizes the defaults as above. Forward acknowledgement to the service input and worktree selection to domain preparation; the service receives the selected path only through server-prepared evidence. Preserve every nonempty worktree path string exactly, including leading/trailing spaces, until server-side path resolution. Forward both semantics end-to-end. `POST /api/projects/{project_id}/sprint/task/complete` remains the route; existing decision headers, instance keys, checklist transports and actor/idempotency fields retain their meanings. CLI adds opt-in `--uncommitted` and `--worktree PATH`; neither is injected in generated default next-action argv. API rejects spoofed observation fields and nonboolean acknowledgement values.

Dirty or unavailable full acceptance without acknowledgement returns the existing conflict envelope naming the observed rule, `--uncommitted` / API `uncommitted=true`, and “A changed request requires a new idempotency key.” Mismatch and changed-revision refusals also name their rule and that key requirement. Preserve `WORKFLOW_FACT_CONFLICT` and existing nonzero/HTTP 409 behavior; do not automatically resubmit.

**Mutation outputs:** New successful original/retry completion results include `repository_evidence`, `repository_warnings` (stable codes: `UNCOMMITTED_WORKTREE`, `DETACHED_HEAD`, `REPOSITORY_NOT_BOUND`, `REPOSITORY_UNAVAILABLE`, `OTHER_WORKTREES_PRESENT`) and human-readable `repository_warning_messages`. Adopt both reviewer suggestions: bounded dirty paths and warning labels. Derive warnings/messages from persisted evidence and store them in new results for exact replay. Historical receipt output is unchanged.

**Reads:** In `sprint_task_show.data.completion`, `data.original_completion`, task-history equivalents, and each original/retry completion in Sprint history, add:

- `repository_evidence`: captured/not-bound/unavailable DTO as JSON, or null for old rows.
- `revision_recording`: `recorded`, `not_bound`, `unavailable`, or `not_recorded` respectively.
- `repository_warnings`: derived warning codes; legacy `not_recorded` must not be presented as clean.
- `repository_warning_messages`: human-readable labels for those codes.

Use a shared projection helper instead of altering canonical legacy Fact serialization for presentation. Reads never probe Git, recompute old evidence from live state, or write rows. No completion remains null, distinct from a completion with null historical repository evidence.

**Dashboard:** Render full HEAD, branch/detached label, selected path, bound-checkout match, dirty flag/count/bounded paths and acknowledgement in the selected Task's Checks panel, labelled “Repository at completion”. Legacy text is “Revision not recorded”; not-bound text is “Repository not bound”; unavailable text is “Repository unavailable” with bounded summary. Render human-readable warnings. Escape all strings. Keep selected-tab visibility and Task/retry identity controls. Add optional semantic worktree path input (`workspace-task-worktree-path`); it selects what the server inspects and never supplies machine evidence. Task board rows and packet schemas are unchanged.

Add unchecked semantic checkbox `workspace-task-delivery-ack`, label “I acknowledge uncommitted changes or unavailable repository evidence.” Submit `.checked` as `uncommitted`, and the exact nonempty worktree input as `worktree_path` (omit only an empty string; do not trim path whitespace). Never derive acknowledgement from stale state, check it automatically or silently resubmit. Preserve form evidence and existing refresh/locked-state handling.

**E2E audit is mandatory in the same change:** `tests/e2e/test_single_project_lifecycle_ui.py:3135-3147` forbids editable input IDs containing `commit`/`dirty`; the chosen semantic checkbox ID avoids these tokens without relaxing the prohibition on machine-owned fields. Assert its allowed type/label/default explicitly. Update the fake's exact completion POST field set at `2240-2251` to include `uncommitted`, with false/true assertions. Audit direct-fetch completion at `4500-4513` (omission remains valid). Scope duplicate “Revision not recorded”/acceptance text locators to the selected Task inspector, and select the Checks tab before asserting hidden-panel content. Run browser tests only in Linux CI.

### 6. Behaviors preserved, limits, and review decisions

Preserved: ownership/active Sprint/Task checks, checklist coverage and semantic evidence normalization, artifact rules, dependencies, accepted Specification lineage, original/retry separation, immutable completions, exact replay, read-only inspection, and the current next-Task selection algorithm. No commits, branches, target modifications, provider calls, new Task-start revision contract, packet version change or retroactive completion rewriting.

Maintainer decisions A/B and required changes 1-6 are now binding. Risks requiring review: conditional legacy serialization at every original/retry hash consumer; acknowledgement/path replay; common-directory validation for linked worktrees; probe ordering relative to the writer lock; metadata-only observations; migration preservation; E2E fake schemas/locators. No additional product decision is pending.

### Deferred follow-up

- Detached-HEAD reachability from a branch: retain detached warning, no reachability check.
- Uncommitted/unavailable indicators on Task boards or next-Task output: expose detail/history only.
- Linux container visibility of host Codex worktree paths: no mount/container changes. An unreachable selected path produces `unavailable` evidence under policy B.

## Global Constraints

- Phase 2 is approved with maintainer decisions recorded here; use fresh ultra implementers and ultra spec/quality reviewers, then fresh ultra final whole-diff review. No earlier model/provider routing restriction applies to this phase.
- Provider-free checks use disposable tmp Git repositories and tmp SQLite databases only; no real profiles, home DBs, provider/model calls, or `agileforge-*` Docker resources.
- macOS product runtime is Linux-only. Never invoke bare/user-level `agileforge`; native pytest is supported through `uv run --frozen pytest`. Do not run the launcher or product browser server natively.
- Git in this managed worktree is read-only: no commit, branch, stash, reset, push, PR or GitHub comment. All edits remain uncommitted; replace Superpowers commit steps with captured diffs/test evidence.
- Use existing repository inspection; no ad hoc Git subprocess capture and no target writes. Tmp fixture repositories may create local commits solely for provider-free tests.
- Keep source path banners first where present, important module values explicitly typed, and changes surgical.
- `api.py`, `cli/main.py`, `cli/workflow_commands.py`, `services/application.py`, and `services/read_projections.py` must not gain the retired words banned by `tests/adapters/test_production_read_surfaces.py`, including comments/docstrings.
- Tests exercise structure and behavior with fixture-known values, not documentation prose/regex or brittle total counts. Keep socket-disabled default pytest selection; do not enable integration/provider tests.

## Review Focus

1. Legacy completion Facts nested in Story/Sprint and retry history must retain original hashes after nullable columns appear (Tasks 1-2).
2. A receipt with acknowledgement true must not replay as false, and old receipts must replay with absent/false input even if the worktree vanished (Task 5).
3. Old attachment HEAD/status must not block delivery; linked worktree common-directory mismatches refuse, while unavailable targets follow acknowledgement policy (Tasks 3-4).
4. Staged plus unstaged entries on the same path, renames, and untracked paths must yield a distinct-path count rather than a status-entry count (Task 3).
5. Retry UI/detail/history must show retry evidence separately from original evidence and preserve selected identity/visibility after refresh (Tasks 6-7).

## TDD PLAN

Tasks are sequential because they share completion/model/transport files. Each task follows red → minimal green → focused verification → fresh task review; do not run concurrent mutations to these surfaces. Test names below are proposed additions, not claims that tests exist or have run.

### Task 1: Add nullable storage and exact-baseline atomic upgrades

**Files:** Modify `models/workflow.py`, `models/sprint_retry.py`, `models/db.py`, `tests/workflow/test_sprint_retry_schema.py`, `tests/workflow/test_workflow_models.py`; create `tests/fixtures/issue_289/pre_revision_business_schema_07654b8.sql`, `tests/fixtures/issue_289/pre_revision_completed_history_07654b8.sql`, `tests/workflow/test_task_completion_revision_schema.py`.

**Interfaces:** Both evidence models expose `repository_evidence_json: str | None`; `ensure_business_db_ready(engine_override: Engine | None = None) -> None` upgrades only the enumerated manifests in DESIGN 4.

- [x] Before changing metadata or writers, freeze the actual pre-289 database DDL and original/retry completed history with receipts and closure hashes using existing synthetic lifecycle helpers. Use current schema inspection and the independently captured pre-retry fixture. Load this frozen legacy history in existing upgrade-test helpers instead of generating new completion payloads and discarding their repository evidence.
- [x] Write `test_pre_revision_upgrade_preserves_existing_completion_history`, `test_pre_retry_upgrade_reaches_revision_schema`, `test_failed_second_column_upgrade_is_atomic`, `test_unknown_or_partial_schema_fails_closed`, and `test_concurrent_startup_upgrades_once`. Assert new nullable columns structurally, old columns/rows/JSON/hashes exactly preserved, legacy new fields SQL NULL, complete fresh/upgrade manifests equal, and rollback leaves the exact old schema. Extend the pre-retry test helper to copy its enumerated old columns after the new schema has additional fields.
- [x] RED: `uv run --frozen pytest tests/workflow/test_task_completion_revision_schema.py -q` must fail on missing fields/upgrade support, not test setup errors.
- [x] Add nullable TEXT fields and the exact transaction routes; update reviewed manifests and misleading unsupported-schema diagnostics without expanding accepted unknown schemas.
- [x] GREEN: `uv run --frozen pytest tests/workflow/test_task_completion_revision_schema.py tests/workflow/test_sprint_retry_schema.py tests/workflow/test_fresh_project_schema.py -q`.
- [x] Capture uncommitted diff and actual test evidence for task review; no Git mutation.

Representative assertions inside the migration test (fixture snapshots enumerate old columns):

```python
assert observed_manifest == CURRENT_BUSINESS_SCHEMA_MANIFEST
assert after_old_columns == before_old_columns
assert original_completion.repository_evidence_json is None
assert retry_completion.repository_evidence_json is None
```

### Task 2: Bind typed repository payloads without changing legacy hashes

**Files:** Create `services/contracts/task_repository_evidence.py`, `tests/workflow/test_task_repository_evidence_integrity.py`; modify `workflow/execution_integrity.py`, `workflow/facts.py`, `workflow/definitions/execution.py`, `repositories/workflow.py`, `repositories/sprint_retry.py`.

**Interfaces:** `CapturedTaskRepositoryEvidence`, `UnboundTaskRepositoryEvidence`, and discriminated `TaskRepositoryEvidence` define DESIGN 3. `canonical_task_evidence_payload(..., repository_evidence_json: str | None = None) -> TaskEvidencePayload` decodes the optional payload; `TaskCompletionFact.repository_evidence` is optional with canonical omission for None.

- [x] Write `test_legacy_completion_and_nested_snapshot_dumps_preserve_bytes`, `test_legacy_story_and_sprint_history_remains_valid`, `test_new_repository_payload_is_in_completion_hash`, and `test_tampered_original_and_retry_repository_payload_fails_closed`. Fixture-known captured values must round-trip; changing HEAD, branch, dirty/count, binding ID, acknowledgement or not-bound discriminator must invalidate the persisted hash. Assert malformed/extra-field/noncanonical JSON fails, cross-Project/missing historical bindings fail closed, a later active binding refresh preserves valid completion history, and null historical evidence does not acquire a new canonical key.
- [x] RED: `uv run --frozen pytest tests/workflow/test_task_repository_evidence_integrity.py -q`.
- [x] Implement the frozen DTO, conditional hashing/serialization, and propagate typed evidence through every original/retry decode/recompute site. Validate dirty/count and branch/detached consistency; preserve future reads after an active binding refresh using the recorded immutable binding.
- [x] GREEN: `uv run --frozen pytest tests/workflow/test_task_repository_evidence_integrity.py tests/workflow/test_execution_transitions.py tests/workflow/test_sprint_retry_execution.py -q`.
- [x] Capture diff/evidence for task review.

Representative legacy/new hash assertions:

```python
assert legacy_fact.model_dump(mode="json") == frozen_legacy_fact_payload
assert legacy_hash == frozen_legacy_hash
assert changed_head_hash != recorded_hash
```

### Task 3: Capture and verify live target evidence transactionally

**Files:** Create `services/task_repository_evidence.py`, `tests/services/test_task_completion_repository_evidence.py`; modify `services/repository_probe.py`, `adapters/git/repository_probe.py`, `services/task_execution_service.py`, `workflow/domain.py`, `workflow/handlers/execution.py`, `workflow/handlers/__init__.py`, `tests/services/test_repository_probe.py`, `tests/workflow/test_execution_recovery.py`.

**Interfaces:** `prepare_task_repository_evidence(session: Session, *, project_id: int, repository_probe: TaskRepositoryProbe, worktree_path: str | None, uncommitted: bool) -> PreparedTaskRepositoryEvidence`; verification receives the writer Session and prepared evidence, checks binding identity and uses `TaskRepositoryProbe.inspect_revision(path) -> RepositoryRevisionProbeResult` (HEAD+dirty only). `TaskRepositoryProbe.has_other_worktrees(path) -> bool` adds topology without scanning other worktrees' status. Preserve the old base RepositoryProbe protocol. The domain owns outside preparation; service owns cheap verification/persistence. The public request never accepts prepared evidence.

- [x] Write clean-current-commit, distinct/bounded-path count, not-bound, unavailable-partial, unrelated-worktree refusal, clean-main/dirty-linked topology, changed-HEAD/dirty refusal and retry-snapshot tests. Instrument `BEGIN IMMEDIATE` with an Engine event and a probe spy to prove full inspection happens before it and only cheap verification after it. Test full-capture failure and verification failure separately, binding changes, and >50 known dirty paths. Assert business-state preservation on actual mismatch refusals and target state unchanged.
- [x] RED: `uv run --frozen pytest tests/services/test_task_completion_repository_evidence.py -q`.
- [x] Implement outside preparation and explicit local threading through domain/handlers, cheap inside verification, related-worktree selection/topology warning, unavailable conversion, canonical persistence and result warnings/messages. Until Task 5 exposes semantic fields, service tests can pass explicit preparation inputs. Never full-scan inside `complete_task_in_session`; no global mutable prepared snapshot. Use an in-repo tmp runtime/log fixture in affected CLI suites, with all three diagnostic log paths; no out-of-tree plugin.
- [x] Keep Git filenames literal in per-path actual-content checks. Validate full normalized dirty-path completeness against the Git adapter's read-only NUL porcelain metadata outside the transaction. Limit existing raw-diff calls to those literal changed paths plus rename sources, skipping raw diff when status is clean; this avoids stale stat-only candidates without refreshing the index. A demonstrated third-party raw-diff parser omission (for example a colon-prefixed tracked path) must become a typed probe failure/unavailable observation, never a silently incomplete captured count; retain index byte/mtime preservation. This guard reuses GitPython and the existing normalized inspection rather than replacing its parser or adding shell commands.
- [x] GREEN: `uv run --frozen pytest tests/services/test_task_completion_repository_evidence.py tests/services/test_repository_probe.py tests/workflow/test_execution_recovery.py -q`.
- [x] Capture diff/evidence for task review.

Representative captured-value assertions (fixture has one staged+unstaged path and one untracked path):

```python
assert captured.head_sha == delivery_commit.hexsha
assert captured.head_sha != attachment_head
assert captured.dirty is True
assert captured.dirty_path_count == 2
assert target_after == target_before
```

### Task 4: Enforce dirty full-acceptance acknowledgement at the shared boundary

**Files:** Modify `services/task_execution_service.py`, `services/task_repository_evidence.py`, `tests/services/test_task_completion_repository_evidence.py`, `tests/workflow/test_sprint_retry_execution.py`; adjust `tests/workflow/execution_fixtures.py` and `tests/workflow/retry_execution_fixtures.py` only for ordinary execution cases that currently retain an unavailable synthetic active binding.

**Interfaces:** `TaskCompletionInput.uncommitted: bool = False`; `validate_task_repository_policy(evidence: TaskRepositoryEvidence, *, acceptance_result: Literal['partially_met', 'fully_met']) -> None` enforces DESIGN 2 and produces the existing service-conflict path.

- [x] Test dirty/unavailable `fully_met` requiring acknowledgement, acknowledged dirty/unavailable persistence, unavailable/dirty partial acceptance without acknowledgement, clean detached warning and existing-rule preservation. Cover original/retry and malformed direct-service bools; acknowledgement never bypasses common-directory or revision mismatches. Refusal messages state that a changed request requires a new idempotency key.
- [x] RED: `uv run --frozen pytest tests/services/test_task_completion_repository_evidence.py -k 'ack or partial or detached' -q`.
- [x] Forward the validated service boolean into captured evidence and apply policy before any business persistence; warnings derive from the saved observation. Direct service calls must enforce it too; do not use truthiness/coercion to grant acknowledgement.
- [x] Keep binding-focused cases on real disposable Git targets. Ordinary execution fixtures may explicitly become unbound after planning/start when repository binding is outside their test contract; clear only the recognized synthetic active-binding pointer and retain immutable binding rows and planning lineage. The original and retry execution start helpers may share this fixture-only adjustment. Do not blanket-acknowledge old test requests, rewrite frozen histories, or alter shared planning/lifecycle fixtures without concrete evidence.
- [x] GREEN: `uv run --frozen pytest tests/services/test_task_completion_repository_evidence.py tests/workflow/test_sprint_retry_execution.py tests/test_task_execution_service.py -q`.
- [x] Capture diff/evidence for task review.

Representative accepted dirty-completion assertions:

```python
assert result.ok is True
assert captured.dirty is True
assert captured.uncommitted_acknowledged is True
assert task.status is TaskStatus.DONE
```

### Task 5: Expose strict acknowledgement through API/CLI and preserve replay

**Files:** Modify `workflow/requests/execution.py`, `workflow/handlers/execution.py`, `workflow/domain.py`, `services/application.py`, `services/node_attempt_replay.py`, `api.py`, `cli/main.py`, `services/repository_probe.py`, `adapters/git/repository_probe.py`, `services/task_repository_evidence.py`, `tests/services/test_repository_probe.py`, `tests/services/test_task_completion_repository_evidence.py`, `tests/workflow/test_execution_requests.py`, `tests/adapters/test_cli_task_completion.py`, `tests/adapters/test_api_workflow_domain.py`; fix the two fixture engine-forwarding callers in `tests/workflow/test_superseded_story_dependencies.py`; create `tests/adapters/test_task_completion_repository_transport.py` and an in-repo shared CLI isolation fixture if needed.

**Interfaces:** Public semantic requests accept strict `uncommitted=False` and optional `worktree_path`; guarded serialization includes new keys only when non-null (false acknowledgement canonicalizes to None). CLI adds `--uncommitted` and `--worktree PATH`. Replay compares both acknowledgement directions and explicit/absent path, with new-key guidance on refusal. Preserve existing selectors/headers/checklist transports.

- [x] Test legacy hash/replay, true↔false/path-change identity, strict bool/spoof rejection, and CLI/API clean-main versus dirty-linked topology. No override yields clean + OTHER_WORKTREES_PRESENT; explicit linked path requires ack; unrelated repo refuses specifically. Test unavailable partial/full acknowledgement, old replay after tmp target disappears and actionable new-key refusals. Runtime/log isolation is an in-repo fixture, with no temporary plugin.
- [x] Verify server common-directory identity before full metadata capture, without requiring HEAD or status. A concrete reproduction showed an unrelated unborn repository becoming unavailable before its observable identity mismatch was checked; that must refuse `WORKTREE_REPOSITORY_MISMATCH` even with acknowledgement. Cover unrelated unborn/parser-failing repositories, same-repository metadata failure, genuinely unreachable paths, and identity failure. Add a completion-specific `TaskRepositoryProbe.inspect_common_git_dir` method using the existing Git adapter and resolved absolute common directory; retain the base one-method protocol. This preflight runs outside the writer and never runs for receipt replay.
- [x] RED: `uv run --frozen pytest tests/adapters/test_task_completion_repository_transport.py tests/workflow/test_execution_requests.py -q`.
- [x] Implement strict semantic fields/forwarding, non-null guarded serialization and narrow replay normalization. Keep default next-action argv without automatic acknowledgement/path. Add human-readable warning messages and specific refusal/new-key guidance without accepting client-reported Git state.
- [x] GREEN: `uv run --frozen pytest tests/adapters/test_task_completion_repository_transport.py tests/workflow/test_execution_requests.py tests/adapters/test_cli_task_completion.py tests/adapters/test_api_workflow_domain.py tests/adapters/test_cli_workflow_domain.py -q`.
- [x] Capture diff/evidence for task review.

Representative replay assertions:

```python
assert old_request.model_dump(mode="json") == frozen_request_payload
assert exact_replay.replayed is True
assert true_then_false.ok is False
assert false_then_true.ok is False
```

### Task 6: Add completion-time evidence to read projections and historical views

**Files:** Modify `services/read_projections.py`, `tests/services/test_sprint_status_projection.py`; create `tests/services/test_task_completion_repository_projection.py`; modify `tests/adapters/test_api_dashboard_bundle.py` only where structured history assertions need the new fields.

**Interfaces:** `_task_completion_projection(completion: TaskCompletionFact) -> JsonObject` adds DESIGN 5 presentation fields without changing canonical Fact dumps; use it for effective/original detail, history and original/retry Sprint-history lists.

- [x] Test completion-time versus live state, legacy/null versus not-bound versus unavailable, separate original/retry evidence, bounded paths and human-readable warnings in CLI/API/dashboard history. Commit/change only tmp Git after completion, assert fixed recorded values and no DB writes. No-completion remains null.
- [x] RED: `uv run --frozen pytest tests/services/test_task_completion_repository_projection.py -q`.
- [x] Add the presentation helper and use it consistently. The selected Task inspector reads the existing detail GET; the bundle receives fields through Sprint history, with no new task-detail bundle slot or Task-board status field.
- [x] GREEN: `uv run --frozen pytest tests/services/test_task_completion_repository_projection.py tests/services/test_sprint_status_projection.py tests/adapters/test_api_dashboard_bundle.py tests/adapters/test_production_read_surfaces.py -q`.
- [x] Capture diff/evidence for task review.

Representative projection assertions:

```python
assert legacy_completion["revision_recording"] == "not_recorded"
assert legacy_completion["repository_evidence"] is None
assert recorded_completion["repository_evidence"]["head_sha"] == completion_head
assert durable_rows_after_read == durable_rows_before_read
```

### Task 7: Render evidence and submit an explicit UI acknowledgement

**Files:** Modify `frontend/project.js`, `tests/test_sprint_retry_dashboard.mjs`, `tests/test_cockpit_action_synchronization.mjs`, `tests/test_lifecycle_workspace.mjs`, `tests/e2e/test_single_project_lifecycle_ui.py`. Modify `frontend/lifecycle-workspace.js` only if its validated merge needs to preserve new completion properties; no unrelated controller refactor.

**Interfaces:** Detail renderer displays captured/unavailable/not-bound/legacy evidence, bounded dirty paths and warning labels; form has unchecked acknowledgement plus optional worktree path; submit sends boolean and nonempty path only.

- [x] Write Node behavioral assertions for clean/dirty/detached/legacy/not-bound/unavailable evidence, escaped strings, full SHA, bounded dirty paths, warning labels, default false/explicit true and path/empty-path POST, and error-preserved form/identity. Exercise VM renderer/form harnesses, not source regexes.
- [x] RED: `node --test tests/test_sprint_retry_dashboard.mjs tests/test_cockpit_action_synchronization.mjs tests/test_lifecycle_workspace.mjs`.
- [x] Implement completion-time display, checkbox and optional semantic path submission; no automatic acknowledgement/resubmission or editable machine evidence. Preserve tabs/refresh.
- [x] Audit/update the E2E fake field set, allowed semantic checkbox/default, direct-fetch omission case, scoped duplicate-text locators, and Checks-tab visibility as specified in DESIGN 5. Add dirty rejection then explicit-ack flow to the provider-free fake. The semantic-checkbox assertion supplements the existing ban on machine-owned inputs.
- [x] GREEN: same Node command. Linux CI additionally runs `uv run --frozen pytest tests/e2e/test_single_project_lifecycle_ui.py -q`; do not claim a macOS skip proves browser behavior.
- [x] Capture diff/evidence for task review, including the explicit locally unexecuted Linux browser gate.

Representative assertions in the existing form/POST harness:

```javascript
assert.equal(ackCheckbox.checked, false);
assert.equal(defaultSubmission.uncommitted, false);
assert.equal(acknowledgedSubmission.uncommitted, true);
```

### Task 8: Verify integration and complete the prescribed independent review

**Files:** Only fix issue-289 findings in the task-owned files above. The independent dependency-Sprint helper in `tests/workflow/test_superseded_story_dependencies.py` must apply the existing exact-synthetic-binding cleanup after its successful StartSprint, retaining binding rows and source history. Its independent completion fingerprint reconstruction must include the retained optional repository evidence alongside the semantic fields. The full default run also identifies two related corrections: update the exact initializer AST contract in `tests/issue_210/test_authority_surface_removed.py` for the reviewed migration chain, retaining every unsafe-source rejection and adding evidence-migration mutation cases; make the existing actual-content filter in `adapters/git/repository_probe.py` retain dirty special-file entries without hashing or opening a FIFO, with behavioral coverage in `tests/services/test_repository_probe.py` and the existing source-registration suite.

Extend the in-repo temporary runtime/log isolation fixture to the six additional adapter modules that actually invoke the fenced CLI/lifespan: `test_api_sprint_retry.py`, `test_cli_sprint_retry.py`, `test_cli_sprint_triage.py`, `test_command_renderer.py`, `test_vision_bootstrap_api.py`, and `test_vision_bootstrap_cli.py`. Add the same narrowly selected fixture in `tests/services/conftest.py` for `test_story_sprint_selection.py` and `test_story_validation_application.py`. Keep the real fence and all three diagnostic log paths; do not introduce an external plugin or redirect any real profile/database. This corrects baseline test isolation rather than changing runtime policy. Execution ledger/review packets live in this plan's Superpowers workspace; retain actual commands and outputs.

- [x] Run targeted and affected Python suites below with logs outside the repo. Distinguish previously failing baseline tests from new failures; resolve changes caused by this implementation, and report unrelated baseline blockers rather than broadening scope silently.
- [x] Run repository-wide typing/lint, changed-Python formatting, whitespace, full Node CI list, default provider-free pytest and local E2E collection below. Actual browser/full product execution remains Linux CI only. No completion claim from a narrow test alone.
- [x] Fresh independent final Astra review receives DESIGN, final uncommitted diff, relevant source, task ledger and actual verification evidence, without implementer conversation. Preserve task review specification-compliance and quality verdicts and fix budgets described below. Confirm final required checks after accepted fixes; rerun broader checks only when new changes/failures justify it.
- [x] Final handoff reports what changed, exact tested scope, baseline failures if any, observation limits, review disposition and uncommitted status. No push/PR/GitHub actions.

### Task 9: Correct porcelain-only gitlink coverage after PR 300 review

**Files:** `adapters/git/repository_probe.py`, `tests/services/test_repository_probe.py`, and a narrow `tests/services/test_task_completion_repository_evidence.py` regression only if needed to prove capture/verification agreement. The coordinator owns this plan and the production-upgrade dependency note. Base: `0514d52a7188fc49a6afa0323e4cb7857a723bfe`; preserve the pre-existing historical timeout-section plan diff.

**Decision:** Follow the submodule coverage rule in DESIGN: untracked-only submodule content becomes a captured modified gitlink, deliberately differing from the clean full-probe result at `b3a4fb4`. Keep `inspect_revision` unchanged. This preserves its ordinary-porcelain dirty answer and the completion acknowledgement rule. Only missing paths absent from all entries qualify, and only confirmed stage-zero gitlinks with a porcelain worktree change; staged gitlinks already represented by index entries retain their existing fingerprints. Production upgrade work remains deferred to Task 10.

- [x] Reproduce untracked-only, tracked-modified and new-commit submodules against the actual `b3a4fb4` adapter in disposable repositories; retain source hashes, commands, outcomes and index-preservation evidence outside source Git state.
- [x] Observe RED before editing production: full and cheap probe agreement for the real submodule matrix, default/unset and configured ignore modes, Git index format 4, unchanged indexes and no SDK submodule traversal. Preserve strict rejection for non-gitlink metadata omissions and previously represented gitlink paths. Use structural/behavioral tests only.
- [x] Add the minimal Git-adapter fallback, then observe GREEN and run the full probe suite. Do not change timeouts, schema, API/CLI, persisted fields, projections, frontend, profiles or production upgrade code.
- [x] Fresh ultra specification-compliance and code-quality review, followed by final whole-new-diff review under the existing Superpowers task/final fix budgets. No descendants; preserve Git read-only and provider-free temporary state.
- [x] Pass repo-wide `uv run --frozen ty check` (**All checks passed!**), Ruff `.`, ANN `.`, formatting for changed Python, `git diff --check`, the full affected suites below and `uv run --frozen pytest tests/e2e --collect-only -q`. Include `tests/adapters/test_production_read_surfaces.py`. Actual Linux browser execution remains CI-only; frontend/E2E source is unchanged.

Task 9 verification: 503 passing tests across the 12 complete affected modules (149 probe, 277 attachment/lifecycle/completion/read, 77 spec/vision); all required quality gates passed, 66 E2E tests collected, and the additional exact CI Node command passed 329 tests. One test-only review fix isolated the existing SQLite status-timeout fixture from HEAD-wrapper startup through the existing deterministic reader seam; its real status process, deadlines, descendant cleanup, rollback, writer acquisition and replay checks remain. Fresh scoped rereview and final independent whole-new-diff review approved specification and code quality. All four files remain uncommitted; Task 10 is deferred.

Affected suite command (may be split into disjoint full-module batches):

```sh
uv run --frozen pytest tests/services/test_repository_probe.py tests/workflow/test_repository_attachment.py tests/workflow/test_repository_binding_model.py tests/services/test_project_lifecycle.py tests/services/test_task_completion_repository_evidence.py tests/services/test_task_completion_repository_projection.py tests/adapters/test_task_completion_repository_transport.py tests/services/test_specification_source_registration.py tests/services/test_vision_evidence.py tests/services/test_vision_evidence_reader.py tests/workflow/test_vision_evidence_persistence.py tests/adapters/test_production_read_surfaces.py -q --tb=short
```

### Task 10: Pending production upgrade registry registration after #298

**Deferred; do not implement in this correction.** After PR #298 (#230) merges and this branch is rebased, register #289's existing migration as one supported production upgrade registry entry. The database-layer migration is not an in-place production-profile upgrade; retain the downgrade and upgrade notes in DESIGN. No production profile or migration code changes are authorized here.

## Required verification commands and passing conditions

All Python commands use the checkout's uv project environment. Redirect verbose logs to a uniquely named `$TMPDIR` directory and print bounded summaries while preserving exit codes. New tests use pytest tmp paths and default socket blocking.

```sh
# Targeted new contracts, persistence, policy, transport and reads
uv run --frozen pytest tests/workflow/test_task_completion_revision_schema.py tests/workflow/test_task_repository_evidence_integrity.py tests/services/test_task_completion_repository_evidence.py tests/adapters/test_task_completion_repository_transport.py tests/services/test_task_completion_repository_projection.py -q

# Existing completion + directly affected suites
uv run --frozen pytest tests/test_task_execution_service.py tests/services/test_repository_probe.py tests/workflow/test_execution_requests.py tests/workflow/test_execution_transitions.py tests/workflow/test_execution_recovery.py tests/workflow/test_execution_graph.py tests/workflow/test_execution_scope.py tests/workflow/test_sprint_retry_execution.py tests/workflow/test_sprint_retry_schema.py tests/workflow/test_fresh_project_schema.py tests/workflow/test_workflow_models.py tests/workflow/test_sprint_retry_models.py tests/workflow/test_superseded_story_dependencies.py tests/services/test_sprint_status_projection.py tests/adapters/test_cli_task_completion.py tests/adapters/test_api_workflow_domain.py tests/adapters/test_cli_workflow_domain.py tests/adapters/test_api_dashboard_bundle.py tests/adapters/test_production_read_surfaces.py -q

# Additional affected migration/probe guards and isolated CLI/API consumers
uv run --frozen pytest tests/issue_210/test_authority_surface_removed.py tests/services/test_specification_source_registration.py tests/adapters/test_api_sprint_retry.py tests/adapters/test_cli_sprint_retry.py tests/adapters/test_cli_sprint_triage.py tests/adapters/test_command_renderer.py tests/adapters/test_vision_bootstrap_api.py tests/adapters/test_vision_bootstrap_cli.py tests/services/test_story_sprint_selection.py tests/services/test_story_validation_application.py -q

# Full default provider-free regression suite (integration excluded, sockets blocked)
uv run --frozen pytest -q

# Required repo-wide quality gates
uv run --frozen ty check
uv run --frozen ruff check .
uv run --frozen ruff check --select ANN .
git diff --check

# Frontend job's exact current suite list (.github/workflows/ci.yml)
node --test $(sed -n '91,100p' .github/workflows/ci.yml | grep -oE 'tests/[^ ]+\.mjs')

# Required macOS-safe E2E collection gate (execution remains Linux-only)
uv run --frozen pytest tests/e2e --collect-only -q

# Linux CI only; do not run product/server/E2E on macOS
uv run --frozen pytest tests/e2e/test_single_project_lifecycle_ui.py -q
```

Changed-file format command (filter to actual changed `.py`/`.pyi` files, including new untracked test files; Markdown/JavaScript are not Ruff input):

```sh
uv run --frozen ruff format --check adapters/git/repository_probe.py api.py cli/main.py models/db.py models/sprint_retry.py models/workflow.py repositories/sprint_retry.py repositories/workflow.py services/application.py services/contracts/task_repository_evidence.py services/node_attempt_replay.py services/read_projections.py services/repository_probe.py services/task_execution_service.py services/task_repository_evidence.py tests/adapters/conftest.py tests/adapters/test_api_dashboard_bundle.py tests/adapters/test_cli_task_completion.py tests/adapters/test_task_completion_repository_transport.py tests/e2e/test_single_project_lifecycle_ui.py tests/issue_210/test_authority_surface_removed.py tests/services/conftest.py tests/services/test_repository_probe.py tests/services/test_sprint_status_projection.py tests/services/test_task_completion_repository_evidence.py tests/services/test_task_completion_repository_projection.py tests/workflow/execution_fixtures.py tests/workflow/retry_execution_fixtures.py tests/workflow/test_execution_recovery.py tests/workflow/test_execution_requests.py tests/workflow/test_sprint_retry_schema.py tests/workflow/test_superseded_story_dependencies.py tests/workflow/test_task_completion_revision_schema.py tests/workflow/test_task_repository_evidence_integrity.py tests/workflow/test_workflow_models.py workflow/definitions/execution.py workflow/domain.py workflow/execution_integrity.py workflow/facts.py workflow/handlers/__init__.py workflow/handlers/execution.py workflow/requests/execution.py
```

Every required gate and every affected suite must exit 0; `ty check` must explicitly report **“All checks passed!”**. The extra full default regression run must be reported with its actual exit and counts. If unchanged host/sandbox failures remain, establish their cause and HEAD equality, preserve the failures, and report them separately; do not change unrelated production behavior, suppress tests, rewrite the source Git index, or run Linux product processes natively to manufacture a green result. `# type: ignore` does not satisfy ty. Prefer fixing types; use `# ty: ignore[rule]` only as a scoped last resort. Follow `tests/adapters/test_cli_workflow_domain.py:168-170` for `pytest.fail`: put the message on a separate line with the ignore attached to that message line. The selected production-read-surfaces test is mandatory because its retired-word checks include comments/docstrings.

```python
pytest.fail(
    "unexpected persistence"  # ty: ignore[invalid-argument-type]
)
```

Linux canonical CI also runs `./agileforge-dev check`, launcher smoke and container/production checks via the repository's established CI transport. Those remain CI responsibilities; this task does not authorize creating or touching user Docker resources. Any Phase 2 runtime/state CLI mutation requires that Linux checkout's `./agileforge-dev info --json` first.

## Phase 2 workflow and review ledger

Phase 2 is approved: use the four named Superpowers skills, fresh scoped native `gpt-6.1-sol` ultra implementers, fresh scoped `gpt-6-astra` ultra task reviewers with both verdicts, and a fresh Astra ultra whole-diff reviewer. User's Phase 2 ultra-only controls supersede earlier model/effort/provider routing. No descendants are needed; retain the client four-agent ceiling and serial mutations. Settings are explicit per dispatch; distinguish requested settings from independently exposed runtime confirmation.

Preserve Superpowers implementer/task-review/re-review templates, plan-specific workspace and progress ledger. Git is read-only; record per-task before/after source snapshots and diff packages instead of commits. Native agents use collaboration tools. External written-plan review has already occurred; no provider review/execution is needed under the latest Phase 2 controls. Reviewers are read-only and do not spawn descendants.

Each task review must return both specification-compliance and code-quality verdicts. Minor findings go in the ledger for final triage. Other findings use at most five fix rounds per task: rounds 1-3 resume the original implementer; rounds 4-5 require a fresh, permitted, more capable assignment. Re-review only the fixes after verifying their covering test evidence. Model/permission rules override automatic skill escalation; record a ruling if the permitted stronger route needs new authorization. Final review allows one fix dispatch and one scoped re-review, then adjudication of residual findings. Do not introduce a second review workflow or exceed these budgets.

Execution records are retained in `.superpowers/sdd/2026-10-07-issue-289-task-completion-revision-evidence/`. Tasks 1-7 passed fresh independent specification-compliance and code-quality review. Task 3 review identified a literal-filename issue in the new actual-content filter; the corrected literal-path filter and typed parser guard passed RED/GREEN verification while preserving target-index bytes and mtime. Task 8 corrected the exact migration AST guard and temporary-state test isolation, plus a demonstrated FIFO full-probe regression. Final review identified one Minor FIFO-test platform guard; its simulated missing-capability RED/GREEN and scoped rereview resolved the finding. All required verification is complete. Fresh independent final Astra ultra review returned **Task 8 specification compliance: APPROVED**, **Task 8 code quality: APPROVED**, and **whole-diff readiness: APPROVED for the requested handoff**. No findings remain open; Linux/full-default limits below remain explicit.

## Phase 2 verification record

| Required gate | Actual final result |
| --- | --- |
| `uv run --frozen ty check` | Exit 0; **All checks passed!** |
| `uv run --frozen ruff check .` | Exit 0; All checks passed! |
| `uv run --frozen ruff check --select ANN .` | Exit 0; All checks passed! |
| Exact changed-Python format command above | Exit 0; **42 files already formatted** |
| `git diff --check` | Exit 0; no output |
| Exact CI Node suite command above | Exit 0; **329 passed** |
| Five new Python contract suites | Exit 0; **208 passed**, 5 dependency warnings, 181.89 seconds |
| Nineteen existing affected Python suites, including production read surfaces | Exit 0; **1102 passed**, 5 dependency warnings, 535.60 seconds |
| Ten additional affected migration/probe/API/CLI suites | Exit 0; **260 passed**, 6 warnings, 111.55 seconds |
| `uv run --frozen pytest tests/e2e --collect-only -q` | Exit 0; **66 tests collected** |

The 34 unique affected Python modules total **1570 passed**. Required checks were run against the final production/test source; Node and E2E collection were retained after unrelated probe/test corrections because their inputs did not change. The six-warning expanded run includes the expected blocked-socket policy warning; the remaining warnings are dependency deprecations. Provider calls were not enabled.

The extra once-run full default suite returned **46 failed, 3929 passed, 80 skipped, 1 deselected**, exit 1. Its 41 corrected cases were subsequently verified through the entire affected modules: 37 temporary runtime/log isolation consumers, three strict migration/source guards, and one FIFO full-probe regression. All existing assertions remain enforced, with four additional unsafe-migration mutations. The full default suite was not rerun after the fixes; do not claim a green full default suite from the scoped reruns.

Five unchanged host/sandbox limitations remain separate: three `os.killpg` permission failures, one temporary filesystem that strips requested setuid mode bits, and one GitPython failure reading this checkout's Git index version 4. Nine relevant test/source paths were independently verified byte-equal to HEAD; a disposable mode probe observed 0755 after requesting 04755; the source index header was read without mutation. No skips were added to suppress these failures, no source index conversion occurred, and no Linux product processes were retried natively. The new FIFO test alone has the normal capability guard for platforms without `os.mkfifo`, verified to skip before changing its fixture.

Linux-only test file touched: `tests/e2e/test_single_project_lifecycle_ui.py`. Its completion fake accepts the optional semantic fields, preserves omission compatibility, and exercises explicit dirty refusal/manual acknowledgement. Visibility/default checks and scoped locators address hidden panels and strict-mode duplicates. Actual browser, launcher and container/product execution remain unverified locally and belong to Linux CI.

The uncommitted inventory contains **49 paths**: 23 production files, 25 tests/fixtures, and this plan. No commits, branches, stashes, pushes, PRs, GitHub comments, target-repository mutations, real profile/database access or user Docker-resource changes were made. Detailed commands, exits, RED/GREEN logs, complete file inventory and review disposition are retained in the local Task 8 report and handoff.

## Final external review corrections

Claude Opus 5.5 returned APPROVE WITH CHANGES with three required corrections. This starts a new, bounded review-correction cycle; earlier verification records remain historical. Keep all other behavior and source changes unchanged, retain uncommitted work, and observe RED before GREEN for each correction.

### DESIGN supplement

1. **Bulk status collection:** replace per-path content comparisons with one NUL-delimited `git diff --name-only -z --no-ext-diff --no-textconv` observation through GitPython, with literal path handling. Retain the `lstat` exception for missing/nonregular entries. Remove changed-path argv from raw index/worktree diffs; filter normalized records in Python against the tracked-path set. Preserve normalized metadata, completeness checks, target-index bytes/mtime and binding attach/refresh behavior relative to `b3a4fb4`. Full porcelain parsing retains explicit index/worktree/untracked area sets. Because read-only raw/name-only diff can report stat-only or index-only entries, worktree filtering also requires its actual porcelain worktree area; do not enable automatic index refresh or introduce copied-index machinery. Capture unrestricted NUL raw output through GitPython and discard irrelevant records before the pinned GitPython normalization parser: an untouched leading-colon filename otherwise corrupts that parser before normalized filtering can run. Keep the existing typed handling for changed leading-colon paths. This framing step preserves rename pairs and filename bytes; it does not replace normalized diff parsing.
2. **Bounded completion probes:** configure finite positive per-command limits (outside preparation default 10 seconds, inside verification default 2 seconds). Use GitPython `kill_after_timeout` on completion commands for the supported Linux runtime and macOS tests. Preserve healthy native Windows inspection tests with an internal SDK compatibility branch that omits the unsupported keyword there; validate configured limits everywhere. This creates no public opt-out, changes no product platform guard, and makes no Windows runtime/deadline support claim. Install `procps` in the Linux runtime-base stage so the pinned SDK watchdog has its required `ps` executable in production as well as development; keep other container behavior unchanged. Its index-diff helper uses `as_process=True`, where that option is ineffective; use effectively bounded capture/processing rather than adding an ignored keyword. The pinned Diff constructor also inspects real repository submodules and can start an unbounded object reader; supply a tiny immutable status-only parser context with no submodules, since only raw change/path/rename metadata is consumed. Preserve gitlink status behavior and reuse the existing parser without object-data access. The pinned watchdog also requires a process-enumeration command; this sandbox rejects its `ps` call. Exercise command/options and timeout mapping/rollback with controlled fakes locally, retain the actual failed native watchdog experiment, and add a disposable stalled-Git test for supported Linux CI. Do not claim unmocked native watchdog termination was verified or bypass the denied operation. Outside timeout becomes typed `PROBE_TIMED_OUT` unavailable evidence and follows Decision B. Inside timeout raises a distinct retryable verification failure that propagates to the transaction owner, rolls back the receipt/completion transaction promptly and returns the existing refusal envelope with an explicit retry rule. A retry of an unchanged request may reuse its key because the failed claim rolled back; a changed request still requires a new key. Do not silently turn an inside timeout into an acknowledged completion.
3. **Explicit-path privacy:** add typed `WORKTREE_PATH_UNUSABLE` with one generic summary for unusable explicit targets. Collapse missing/file/non-Git/unreadable or malformed explicit targets in stored evidence and public responses; retain detailed errors for the server-owned bound checkout, the distinct timeout behavior, and observable `WORKTREE_REPOSITORY_MISMATCH`. No client-supplied Git observations or new schema columns. Existing legacy error variants remain readable. The generic mapping covers `PATH_MISSING`, `PATH_NOT_DIRECTORY`, `NOT_GIT_WORKTREE`, `GIT_METADATA_UNREADABLE` and `MALFORMED_PATH`, including an explicitly selected bound checkout and post-capture failures. Retain `UNBORN_HEAD` only after bound-repository identity was validated: it describes already-validated bound Git state rather than an arbitrary-path type.

### TDD tasks

- [x] **Correction 1:** modify `adapters/git/repository_probe.py`; extend `tests/services/test_repository_probe.py` and `tests/workflow/test_repository_attachment.py`. First observe RED for thousands of modified/staged-renamed paths and subprocess growth/argv-size protection. GREEN must demonstrate a constant command budget, faithful staged/worktree/untracked and special-file evidence, and attach/refresh parity with the base probe on disposable repositories. Avoid wall-clock thresholds and brittle exact command counts. Capture any temporary base-probe scripts outside the repository.
- [x] **Correction 2:** modify the Git adapter/probe error contracts, `services/task_repository_evidence.py`, the `Dockerfile` runtime dependency, and only the service/domain exception forwarding needed for rollback. Add failing fake/stalled-Git tests to the probe/evidence/transport suites covering configured limits, real effective timeout handling, outside unavailable policy, inside retryable refusal/rollback, a promptly available independent SQLite writer against a disposable file database, and unchanged-key retry. Keep subprocess helpers and databases disposable.
- [x] **Correction 3:** modify `services/repository_probe.py` and `services/task_repository_evidence.py`; extend evidence/transport tests for missing paths, ordinary files, non-Git directories and unreadable metadata. RED must show distinguishable old codes; GREEN must show the same generic code/summary in persisted original/retry evidence and API responses, while unrelated Git repositories still refuse. Update user-facing labels only if necessary.
- [x] **Final review fix:** filesystem `exists`/`is_dir` preflight in both full inspection and completion helpers must run inside their typed error boundary. First observe RED with an inaccessible disposable parent or narrowly injected `PermissionError`, then verify typed adapter errors and generic explicit-path unavailable evidence/policy through persistence and transport. Restore every temporary permission change in `finally`. Use the one final fix dispatch, followed by one scoped rereview and checks justified by these changed sources.
- [x] Run specification-compliance and code-quality review between corrections, then final independent whole-change review. Use fresh ultra implementers/reviewers within the available collaboration slots; no descendants or concurrent edits to shared files. Record any tool-enforced delegation limitation rather than silently changing model/effort.
- [x] Re-run all explicit gates above against the final source, plus full `tests/workflow/test_repository_attachment.py` and `tests/workflow/test_repository_binding_model.py` suites. Preserve the prior extra default-suite/host-limit record; no unrelated changes or test suppression.

### Correction-cycle verification record

All fresh final gates exited 0: repository-wide Ty (**All checks passed!**), Ruff and ANN checks, formatting for all **43 changed Python files**, whitespace check, the exact CI Node command (**329 passed**), and E2E collection (**66 collected**). After the final permission fix, disjoint four-suite and 34-suite Python batches total **1726 passed across 38 unique affected modules** (304 + 1422), with one locally skipped supported-Linux stalled-Git case. This includes production read surfaces, repository attachment/binding/probe, migrations, services, workflow, API/CLI and container contract suites. The earlier full-default result remains historical and is not claimed green.

Disposable final-candidate comparisons against `b3a4fb4` confirm identical attach/refresh persisted records and both fingerprints for 3/1500 staged-renamed paths plus worktree changes. Separate 3/3000 modified/renamed probes retain a constant command budget, bounded argv and unchanged target index bytes/mtime. All three fresh independent task reviews approved specification compliance and code quality. The fresh whole-diff review identified one filesystem-permission boundary gap; its single allowed fix observed **40 RED failures**, then **40 focused GREEN passes**, and **304 passed / one Linux watchdog skip** in four full suites. Its scoped fresh Astra rereview approved specification compliance and code quality, with no residual finding. Actual permission denial ran in all four native fixtures and permissions were restored. The other 46 non-document source files remain byte-identical. Post-fix quality/Node/E2E collection and base parity passed; the disjoint remaining 34-module batch completed with **1422 passed**, exit 0. Final acceptance is complete. Actual commands, exits, RED/GREEN evidence, complete file/module inventories, portability limits and E2E locator audit are retained in `.superpowers/sdd/2026-10-07-issue-289-task-completion-revision-evidence/corrections-final-verification.md` and its referenced logs.

## PR 300 final low-finding correction

The orchestrator committed the issue implementation at `0a5b9875dcfad51ca1db4bb073e15494ae9ac487`. This follow-up is authorized implementation; new changes remain uncommitted. The earlier records are historical.

**DESIGN:** In-transaction `inspect_revision` receives one monotonic overall deadline, default two seconds, rather than three independent limits. Pass only its remaining budget to each Git call and reject exhausted budgets with the existing typed `PROBE_TIMED_OUT`. On supported POSIX hosts, run verification Git in a new session and kill its entire owned process group on timeout, then promptly reap/close bounded resources. Do not use GitPython's watchdog or unbounded pipe draining for these calls. Keep command execution in the shared Git adapter, preserving SDK command/environment integration, optional-lock suppression, HEAD race detection, unborn/revision error mapping, test reader hooks, and Windows healthy-inspection compatibility without claiming Windows runtime support. Outside inspection remains unchanged.

Verification commands disable `core.fsmonitor` and child-spawning configuration where this preserves the dirty answer. Do not use `--ignore-submodules=dirty` to hide actual tracked/untracked submodule changes; use configuration/process containment that preserves existing Git status semantics. Prove equivalence for clean/dirty standalone and submodule worktrees, including gitlink revision changes. No schema, API/CLI, policy, hash, projection or frontend changes.

- [x] Observe RED for child-spawning fake Git that holds pipes, a shared overall deadline across multiple calls, and fsmonitor suppression; retain real process cleanup evidence.
- [x] Implement the POSIX verification runner and monotonic budget in `adapters/git/repository_probe.py`; extend `tests/services/test_repository_probe.py`. Add narrow completion/transport integration coverage only if needed to prove writer release with the real runner. Retain unchanged behavior elsewhere.
- [x] Fresh independent specification-compliance and code-quality review, followed by final task-scoped whole-diff review/acceptance. Use fresh explicit ultra native agents, no descendants, existing Superpowers task/final fix budgets.
- [x] Pass repo-wide Ty (All checks passed!), Ruff/ANN, changed-Python formatting, whitespace, exact CI Node command, full affected probe/evidence/projection/transport/attachment suites, production read surfaces and E2E collection. Linux product/browser execution remains CI-only.

**Verification and acceptance:** Actual initial RED observed six failures against the committed adapter, with separate decreasing-budget and SQLite writer-release regressions also failing. Final focused coverage passes 40 tests. Real GitPython child/grandchild pipe-holder experiments return typed timeout in **0.754s / 0.753s** for a 0.75-second budget, with closed pipes and no executing recorded processes; the same final fixture stalls the captured baseline for **6.348s**. All 16 standalone/submodule dirty-answer comparisons pass without index changes. Adopted zombies await their host parent's reaping; process-group containment does not claim arbitrary daemon isolation or preemption of synchronous filesystem operations.

All required final gates exit 0: repo-wide Ty (**All checks passed!**), Ruff/ANN, three-file formatting, whitespace, exact CI Node (**329 passed**), **438 passed across eleven disjoint affected Python modules**, and E2E collection (**66 collected**). Fresh scoped review and final independent whole-new-diff review approve specification compliance and code quality after two test-only task fix rounds; production stayed frozen through those rounds. No frontend/E2E files changed; actual Linux browser execution remains CI-only. Commands, source hashes, retained RED/GREEN and unsuccessful intermediate runs, review verdicts and execution limits are recorded in `.superpowers/sdd/2026-10-07-issue-289-task-completion-revision-evidence/pr300-final-verification.md`. Four tracked files remain uncommitted against `0a5b9875dcfad51ca1db4bb073e15494ae9ac487`.

## Phase 1 verification record

- Confirmed clean source checkout and documentation-only difference from `07654b8` before writing this file.
- Confirmed original/dirty completion gap with real tmp repositories and databases, using production domain/service/projection paths and no providers.
- Baseline command: `uv run --frozen pytest tests/workflow/test_execution_transitions.py tests/workflow/test_execution_recovery.py tests/workflow/test_execution_requests.py tests/workflow/test_sprint_retry_execution.py tests/adapters/test_cli_task_completion.py tests/test_task_execution_service.py tests/services/test_repository_probe.py tests/adapters/test_production_read_surfaces.py -q`. Result: **150 passed, 39 failed, 5 warnings in 224.49s**. All failures were in `tests/adapters/test_cli_task_completion.py`, whose CLI runtime fencing resolves the source checkout under home to `/Users/aaat/.agileforge-runtime.lock` and rejects it as an unsafe path; representative stdout was `{"error": "runtime fence lock unsafe path: /Users/aaat/.agileforge-runtime.lock", "ok": false}`. Source: `utils/runtime_ownership.py:78-110`; this is a native sandbox/test-isolation limitation, not the issue-289 completion-service gap.
- A single-case diagnostic confirmed that exact refusal before the CLI reached completion (`cli-diagnostic.log`). A temporary pytest plugin outside the repo redirects runtime roots and diagnostic logs to each test's tmp directory, preserving the existing fence and following `tests/adapters/test_production_read_surfaces.py:29-44`. Its first CLI-only retry omitted `ERROR_LOG_PATH` and failed before completion with a missing checkout log path (**15 passed, 39 failed**); correcting all diagnostic log paths made the representative completion test pass (**1 passed**).
- Final isolated CLI command: `PYTHONPATH="$TMPDIR/issue289-repro.hQZjoxzMEM:$PWD" uv run --frozen pytest -p issue289_runtime_isolation tests/adapters/test_cli_task_completion.py -q`. Result: **54 passed, 4 warnings in 73.99s** (`isolated-cli-tests-final.log`). This reruns the 15 CLI passes plus the 39 previously refused cases; it is not 54 additional distinct tests. The other seven baseline suites passed without the temporary plugin. Phase 2 must preserve tmp runtime fencing and all diagnostic log paths in affected adapter tests rather than writing home locks or disabling the fence.
- `git diff --check` passed. Final source status contains only this untracked plan file. Repository-wide typing/lint, Node and Linux browser gates were listed for Phase 2, not run or claimed green in this documentation-only phase.
- No new production/tests were implemented, no repo Git state was mutated, and no external review/push/comment/provider execution was launched.
