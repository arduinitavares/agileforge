# Project Scope Extension Design

**Date:** 2026-06-14
**Status:** In review
**Spec mode:** proposed_change
**Scope:** Project-agnostic scope extension after current executable scope is exhausted
**Builds on:**
- `docs/superpowers/specs/2026-06-10-post-sprint-learning-triage-design.md`
- `docs/superpowers/specs/2026-06-11-agentic-sprint-capacity-planning-design.md`
- `docs/superpowers/specs/2026-06-13-project-create-authority-compile-split-design.md`
- `docs/superpowers/specs/2026-06-14-authority-compiler-focused-repair-design.md`

## Revision History

- 2026-06-14: Initial design for a conservative, project-agnostic scope
  extension ritual that uses spec amendment and authority review before
  generating new execution work.

## Summary

AgileForge needs a first-class way to continue a mature project after the
current accepted roadmap/backlog execution scope is exhausted and the user has a
new product idea.

The user-facing ritual is **Project Scope Extension**. The internal mechanism is
**Spec Amendment + Authority Gate**.

In v1, scope extension is add-only at the structured source-item level. Existing
accepted source items, completed work, roadmap phases, Stories, Sprints, and
velocity history are preserved. New execution work can be generated only after
the amended spec passes additive validation, authority compilation, authority
review, and authority acceptance.

```text
SPRINT_COMPLETE / exhausted executable scope
-> scope extension proposal
-> additive spec amendment validation
-> new accepted authority
-> delta backlog generation
-> appended roadmap phase
-> Stories and Sprints from extension scope
```

## Problem

When a project finishes its original roadmap, AgileForge can correctly report no
active Sprint, no planned Sprint, and no refined Sprint candidates. That state
means the current execution scope is exhausted, not that the product can never
evolve.

Today, the user has no clean project-agnostic ritual for adding new scope to the
same project while preserving completed work and authority evidence. The risky
alternatives are:

- force backlog refinement even though there is no current backlog source to
  refine;
- edit specs and recompile authority through setup-oriented paths that look like
  initial project creation;
- create a new project and lose continuity of Sprint history, velocity, and
  completed evidence;
- generate backlog/roadmap/Stories from new prose without source-backed
  authority.

The last option would violate the authority chain that AgileForge depends on:

```text
Spec -> Authority -> Backlog -> Roadmap -> Stories -> Sprints
```

New scope must be proved through spec authority before AgileForge creates
execution artifacts.

## Goals

- Add a project-level ritual for extending a completed or exhausted project
  without forking it.
- Use spec amendment and authority compilation as the required internal gate for
  all new scope.
- Allow v1 amendments only when they add new structured source items.
- Block amendments that modify, remove, demote, promote, or rewrite previously
  accepted source items.
- Preserve existing completed work, Sprint history, metrics, and authority
  evidence.
- Generate backlog and roadmap work only for the newly accepted authority delta.
- Provide existing project context to generators only as read-only
  anti-duplication and dependency context.
- Append a new roadmap phase for extension scope in v1.
- Keep `workflow next` runnable-command parity: every advertised command must
  be executable or returned as blocked with a reason and remediation.

## Non-Goals

- Do not support scope removal or deprecation in v1.
- Do not support scope extension while a Sprint is active or planned.
- Do not rewrite, reorder, merge, or reinterpret existing roadmap phases in v1.
- Do not regenerate backlog for previously accepted source items in v1.
- Do not supersede completed Stories or Sprints.
- Do not introduce a project fork or project lineage model.
- Do not add a large family of new FSM states if existing spec/authority states
  plus extension metadata can represent the ritual safely.
- Do not make backlog/roadmap generators authoritative over amendment validity.
  Host validation owns the additive spec gate.

## Current State

Relevant current behavior:

- The FSM already has `SPRINT_COMPLETE`, `SPEC_UPDATE`, and `SPEC_COMPILE`
  states in `orchestrator_agent/fsm/states.py`.
- `SPRINT_COMPLETE` routing is already post-sprint triage aware.
- Authority compile/review/accept uses guarded `SETUP_REQUIRED` authority
  states and explicit spec-version/hash guards.
- Spec versions and compiled authority already exist as persisted authority
  boundaries.
- Backlog, roadmap, Story, and Sprint phases already reject work when authority
  is not accepted or compiled.
- The recent authority compiler repair keeps source-map validation fail-closed
  and adds better recovery for repairable compiler evidence failures.

Missing behavior:

- no command that says "extend this project's scope";
- no pre-compile additive amendment validator;
- no durable extension context that ties a new spec version to a base accepted
  spec version and a set of newly added source items;
- no delta backlog generation mode;
- no roadmap extension mode that appends a new phase while preserving existing
  phases;
- no `workflow next` status for exhausted scope where extension is the correct
  project-level option.

## Design Principles

1. **User-facing extension, authority-backed mechanism.** The user extends the
   project. AgileForge proves the extension through spec authority.
2. **Add-only v1.** New scope is represented by new source item IDs. Existing
   accepted source items must remain byte-stable under canonical comparison.
3. **No hidden rewrite.** Extension must not silently rewrite old backlog,
   roadmap, Story, Sprint, or authority evidence.
4. **No execution while unresolved scope remains.** Extension is available only
   after the current executable scope is exhausted or explicitly made terminal.
5. **Delta generation with read-only context.** Generators work from new
   authority only, but receive existing project context to avoid duplication and
   dependency mistakes.
6. **Small FSM surface.** Prefer one bridge command plus extension metadata over
   a new family of lifecycle states.

## Proposed User Flow

```text
1. User completes or exhausts current execution scope.
2. `workflow next` reports no active/planned Sprint and no refined candidates.
3. AgileForge advertises project scope extension as a runnable project-level
   option.
4. User supplies an amended structured spec.
5. AgileForge validates the amendment against the latest accepted spec.
6. If validation passes, AgileForge records a new spec version and extension
   context.
7. AgileForge routes through authority compile, review, and accept.
8. After authority acceptance, AgileForge exposes delta backlog generation.
9. Saved delta backlog feeds an appended roadmap phase.
10. New Stories and Sprints proceed through the normal execution rituals.
```

## Preconditions

Scope extension v1 is available only when there is no unresolved executable
scope in the current accepted product plan.

Required conditions:

- no active Sprint;
- no planned Sprint;
- current workflow is `SPRINT_COMPLETE` or an equivalent no-active-execution
  project state;
- latest completed Sprint, if present, has post-sprint triage recorded;
- no refined Sprint candidates remain for the current accepted scope;
- no saveable Story draft, Story review, Story persistence, Sprint draft, or
  Sprint close workflow is pending;
- no active backlog/story item remains below a terminal state.

Terminal states include completed or intentionally closed work such as:

- `Done`
- `Merged`
- `Archived`
- `Deferred`
- `Superseded`

If the current schema does not yet have first-class `Deferred` or `Archived`
markers for all relevant objects, v1 implementation must add or reuse explicit
host-owned terminal markers before treating unresolved work as closed.

If executable scope remains, `workflow next` must not advertise scope extension
as runnable. It may show it as blocked with remediation such as:

```text
Finish, defer, or archive remaining executable scope before extending project scope.
```

## Command Contract

### Read-Only Inspection

`workflow next` should surface scope extension only from the exhausted-scope
state.

Suggested status:

```json
{
  "status": "project_scope_extension_available",
  "next_actions": [
    {
      "command": "agileforge scope extension validate --project-id PROJECT_ID --spec-file AMENDED_SPEC_FILE",
      "runnable": true,
      "reason": "Current executable scope is exhausted."
    },
    {
      "command": "agileforge scope extension start --project-id PROJECT_ID --spec-file AMENDED_SPEC_FILE --base-spec-version-id BASE_SPEC_VERSION_ID --expected-state SPRINT_COMPLETE --idempotency-key NEW_IDEMPOTENCY_KEY",
      "runnable": true,
      "reason": "Validate and register additive scope extension."
    }
  ]
}
```

`agileforge scope extension validate` is read-only. It parses the proposed spec,
compares it to the base accepted spec, and returns:

- base spec version id;
- proposed source item count;
- added source item IDs;
- blocked modified source item IDs;
- blocked removed source item IDs;
- blocked source-level changes;
- whether the amendment is additive-only;
- whether the project preconditions are satisfied.

### Mutating Start Command

`agileforge scope extension start` is the single v1 bridge from exhausted
execution scope into the existing spec/authority path.

Required inputs:

- `--project-id`
- `--spec-file`
- `--base-spec-version-id`
- `--expected-state`
- `--idempotency-key`

Optional inputs:

- `--changed-by`
- `--correlation-id`
- `--dry-run`

Required behavior:

- validate project preconditions;
- parse the amended spec;
- compare against `base_spec_version_id`;
- block non-additive changes before authority compilation;
- record extension metadata if validation passes;
- create a new spec version only for additive-valid amendments;
- route the project to the existing authority compile/review/accept path;
- preserve completed execution history.

The command should not compile authority itself unless it reuses the existing
guarded authority compile command path. A safe v1 default is to record the
validated amendment and then advertise `agileforge authority compile`.

## Amendment Validation

The additive validation gate runs before authority compilation.

Base source:

- latest accepted spec version for the project, unless the caller provides an
  explicit `base_spec_version_id`;
- explicit base must match the current accepted authority lineage.

Proposed source:

- amended `agileforge.spec.v1` structured spec file;
- must parse successfully before comparison.

For every source item ID that exists in the base accepted spec:

- the item must still exist in the proposed spec;
- canonical item fingerprint must match;
- source level must match;
- type must match;
- normative text must match;
- acceptance criteria must match;
- verification contract must match;
- existing relationships among existing items must match.

Allowed v1 changes:

- new source item IDs;
- new source item text, acceptance criteria, verification, and source levels;
- new relationships from new source items to existing source items, if they do
  not mutate existing source item definitions;
- new contextual non-normative items, if they do not alter existing accepted
  source items.

Blocked v1 changes:

- missing existing source item ID;
- changed existing source item title, description, text, level, acceptance, or
  verification;
- changed existing source item type;
- changed relationship among existing source items;
- changed source item ID for old content;
- removal or deprecation of existing accepted scope;
- attempt to turn existing proposed/draft scope into a rewrite of accepted
  scope without a new source item ID.

Failure codes should be stable and machine-readable:

- `SCOPE_EXTENSION_NOT_AVAILABLE`
- `SCOPE_EXTENSION_UNRESOLVED_SCOPE`
- `SCOPE_EXTENSION_NON_ADDITIVE`
- `SCOPE_EXTENSION_REMOVED_SOURCE_ITEM`
- `SCOPE_EXTENSION_MODIFIED_SOURCE_ITEM`
- `SCOPE_EXTENSION_SOURCE_LEVEL_CHANGED`
- `SCOPE_EXTENSION_BASE_SPEC_STALE`
- `SCOPE_EXTENSION_SPEC_INVALID`

## SpecRegistry Lifecycle

Invalid amendments must not look like normal compileable spec versions.

Preferred v1 rule:

```text
parse proposed spec
-> validate additive-only amendment
-> create new SpecRegistry row only when validation passes
```

If implementation requires audit records for failed amendment attempts, store
them separately or mark them with an explicit rejected-amendment status that
authority compile and "latest spec" lookups ignore.

An accepted extension context should include:

```json
{
  "schema_version": "agileforge.scope_extension.v1",
  "mode": "scope_extension",
  "policy": "additive_source_items_only",
  "project_id": 3,
  "base_spec_version_id": 7,
  "amended_spec_version_id": 8,
  "added_source_item_ids": ["REQ.operational-learning-report"],
  "base_spec_fingerprint": "sha256:...",
  "amended_spec_fingerprint": "sha256:...",
  "extension_fingerprint": "sha256:...",
  "created_at": "2026-06-14T00:00:00Z",
  "created_by": "cli-agent"
}
```

The extension context is routing and provenance metadata. It does not itself
approve authority, backlog, roadmap, Stories, or Sprints.

## Authority Gate

After extension start, the new spec version follows the existing authority
flow:

```text
authority compile
-> authority review
-> authority accept
```

Rules:

- backlog generation for extension scope is blocked until the new authority is
  accepted;
- authority compile uses the full amended spec so the compiler can validate the
  whole authority artifact;
- delta extraction happens after authority acceptance;
- source-map validation remains fail-closed;
- the extension context must preserve the added source item IDs so delta
  generation can select only new authority.

## Delta Authority

Delta authority is the subset of the newly accepted compiled authority derived
from newly added source item IDs.

The delta extractor should include:

- invariants whose `source_item_id` is in `added_source_item_ids`;
- source map entries for those invariants;
- assumptions or gaps tied to the new source items;
- rejected features tied to the new source items;
- scope themes only when they are supported by new source items.

The delta extractor must not include:

- invariants derived only from existing source items;
- completed Story/Sprint evidence;
- old backlog or roadmap prose as if it were new authority;
- source-map entries whose source item is outside the extension.

If accepted authority contains no delta authority for the added source items,
backlog generation must block with a clear reason.

Suggested error:

```text
SCOPE_EXTENSION_DELTA_AUTHORITY_EMPTY
```

## Delta Generator Context

Backlog and roadmap generation for scope extension need dual context.

### Delta Authority Context

This is the only source of new execution scope.

It contains:

- newly added source item IDs;
- compiled invariants for those source items;
- source map excerpts;
- accepted assumptions and gaps tied to the new source items.

### Existing Project Context

This is read-only guidance used to avoid duplication and dependency mistakes.

It contains:

- current accepted spec version id;
- current accepted authority version id;
- existing backlog items;
- existing roadmap phases;
- completed and terminal Stories;
- completed Sprints and relevant velocity/metrics summary;
- deferred, archived, or superseded items;
- active dependency graph if one exists.

Prompt contract:

```text
Generate execution work only for delta authority.
Use existing project context only to avoid duplication, conflicts, and orphaned dependencies.
Do not alter, rename, reorder, or reinterpret existing completed or planned work.
```

## Backlog Extension

Backlog extension is additive.

The backlog generator should run in an explicit extension mode that receives
delta authority and existing project context.

Required behavior:

- produce backlog candidates only for new authority;
- mark generated items with extension provenance;
- reference the amended spec version and extension fingerprint;
- reject duplicate or overlapping backlog items when they conflict with
  existing active/terminal work;
- preserve existing active and terminal backlog rows;
- avoid active backlog reset in v1 extension mode.

Suggested provenance fields:

```json
{
  "origin": "scope_extension",
  "scope_extension_fingerprint": "sha256:...",
  "base_spec_version_id": 7,
  "accepted_spec_version_id": 8,
  "source_item_ids": ["REQ.operational-learning-report"]
}
```

Saving extension backlog appends new rows. It must not replace the active
backlog baseline.

## Roadmap Extension

Roadmap v1 should append a new phase after existing phases.

Required behavior:

- preserve all existing roadmap phases and requirement entries;
- create one new roadmap phase for extension scope;
- include only requirements derived from extension backlog items;
- keep extension provenance on the new phase and requirements;
- do not reorder, merge, split, or rename existing phases;
- do not blend new requirements into old phases in v1.

The new phase title may be generated by the roadmap agent, but it must be marked
as extension-derived and linked to the extension fingerprint.

If the roadmap generator determines the new scope cannot stand as a new phase
without modifying existing phases, v1 must block and report that smarter
roadmap reconciliation is out of scope.

Suggested error:

```text
ROADMAP_EXTENSION_RECONCILIATION_REQUIRED
```

## Story And Sprint Continuation

After extension roadmap save:

- Story generation runs only for requirements in the appended extension phase.
- Saved Stories reference the amended spec version and extension fingerprint.
- Sprint candidates come only from saved/refined extension Stories unless other
  explicitly terminal-safe continuation work exists.
- Sprint metrics remain project-wide and include historical completed Sprints,
  but planner capacity should not force unrelated old scope into the extension.

Completed old Stories and Sprints remain visible as history and metrics. They
are not regenerated or superseded by extension flow.

## Workflow Next Contract

`workflow next` must distinguish at least these statuses:

- `project_scope_extension_available`
- `project_scope_extension_blocked_by_active_sprint`
- `project_scope_extension_blocked_by_planned_sprint`
- `project_scope_extension_blocked_by_unresolved_scope`
- `project_scope_extension_validation_failed`
- `project_scope_extension_authority_required`
- `project_scope_extension_authority_review_required`
- `project_scope_extension_backlog_available`
- `project_scope_extension_roadmap_available`
- `project_scope_extension_story_available`
- `project_scope_extension_sprint_available`

Every advertised runnable command must be executable against the same workflow
snapshot. Blocked commands must include:

- reason code;
- human-readable message;
- remediation command or next inspection command;
- relevant counts or item IDs when available.

## API And UI Projection

The dashboard should not show scope extension as another setup reset.

When scope extension is available, the project home should show:

- current product scope exhausted;
- latest completed Sprint;
- zero remaining refined candidates;
- current accepted spec version;
- "Extend Project Scope" action;
- explanation that extension requires authority review before backlog/roadmap
  generation.

During an active extension, the UI should show:

- base spec version;
- amended spec version;
- added source item count;
- blocked modified/removed source item IDs, if validation failed;
- authority compile/review/accept status;
- delta backlog status;
- appended roadmap phase status.

The UI must avoid wording that implies completed work was reset.

## Error Handling

| Case | Required Behavior | User/System Impact |
| --- | --- | --- |
| Active Sprint exists | Block extension start | User must close or cancel Sprint through existing rituals |
| Planned Sprint exists | Block extension start | User must start/close/cancel planned Sprint or discard it through existing rituals |
| Refined candidates remain | Block extension start | User must execute, defer, or archive current scope |
| Existing source item removed | Block before SpecRegistry compileable row | Prevents hidden scope deletion |
| Existing source item modified | Block before authority compile | Preserves completed authority evidence |
| Added source items compile to no authority | Block delta backlog generation | Prevents source-less backlog |
| Delta backlog duplicates existing item | Return save/generation blocker | Prevents duplicate execution scope |
| Roadmap extension requires rewrite | Block v1 roadmap save | Prevents silent roadmap mutation |

## Quality Attributes

### Security And Privacy

Scope extension uses local spec files and existing project authority data. It
must not expose full spec text in unbounded API/UI payloads. Failure diagnostics
should include bounded excerpts and stable IDs, not entire specs unless an
existing explicit include-spec option is used.

### Reliability And Idempotency

The mutating extension start command must be idempotent. Its normalized request
hash should include:

- project id;
- base spec version id;
- canonical amended spec fingerprint;
- expected workflow state;
- extension policy;
- changed-by/correlation metadata only if existing mutation rules include it.

Replays with the same idempotency key and same request return the same result.
Replays with the same key and different spec/base/policy return the existing
idempotency conflict behavior.

### Observability

Extension events should be recorded as workflow events with:

- base spec version id;
- amended spec version id;
- added source item count;
- validation status;
- extension fingerprint;
- authority status;
- delta backlog/roadmap status when those phases complete.

### Performance

Additive validation compares structured source item fingerprints and should be
linear in source item count. Full authority compilation remains bounded by the
existing authority compile timeout and heartbeat behavior.

## Alternatives Considered

| Option | Pros | Cons | Decision |
| --- | --- | --- | --- |
| Directly append accepted product scope | Simple UX | Bypasses authority and creates source-map/evidence gaps | Rejected |
| Spec amendment with authority gate | Preserves source-backed chain and project continuity | Requires additive validation and delta generation | Chosen |
| New project fork | Clean separation | Loses Sprint history, velocity, and completed evidence continuity | Rejected for default path |
| Add many new FSM states | Explicit lifecycle | Larger FSM surface and more transition bugs | Rejected for v1 |
| Roadmap smart reconciliation | More flexible planning | Can rewrite or reorder old roadmap unintentionally | Deferred to v2 |
| Add-and-deprecate | Supports scope removal | Requires lifecycle for pending backlog, roadmap, Stories, and Sprints | Deferred to v2 |

## Migration And Compatibility

Existing projects remain unchanged.

For projects with exhausted scope, `workflow next` gains a new project-level
option. Existing backlog refinement, authority regenerate, roadmap, Story, and
Sprint commands keep their current contracts unless invoked through the new
extension mode.

No completed Sprint, Story, roadmap, backlog, or authority row should be
rewritten by this feature.

## Success Metrics

| Metric | Target | Measurement Source |
| --- | --- | --- |
| Extension validation safety | 100% of modified/removed existing source items block before authority compile | Unit/integration tests |
| Runnable command parity | 100% of scope-extension commands advertised by `workflow next` execute or return blocked reasons from same snapshot | Workflow tests |
| History preservation | 0 completed Stories/Sprints mutated by extension flow | Persistence tests |
| Delta generation precision | 100% of extension backlog items reference new source item IDs and amended spec version | Backlog/roadmap tests |
| Final quality gate | `pyrepo-check --all` passes | CI/local gate |

## Open Questions

| Question | Impact | Recommendation |
| --- | --- | --- |
| Exact command group name: `scope extension` vs `project extend-scope` | CLI ergonomics and command registry naming | Use `scope extension` for a ritual-level group unless command registry conventions push toward `project` |
| Whether rejected amendment attempts need durable rows | Auditability vs schema complexity | Use mutation/failure artifacts first; add rejected-attempt table only if needed |
| Whether v2 should support deprecation/removal | Scope lifecycle after v1 | Defer until add-only flow is stable |
| Whether future roadmap reconciliation may merge into planned phases | Planning intelligence | Defer; v1 appends a new phase |

## Acceptance Criteria

- `workflow next` exposes scope extension only when preconditions are satisfied.
- Scope extension start blocks active/planned Sprint states.
- Scope extension start blocks unresolved executable scope.
- Additive validation blocks changed or removed existing source items before
  authority compilation.
- Valid additive amendments produce a new spec version and extension context.
- New authority must be compiled, reviewed, and accepted before delta backlog
  generation.
- Delta backlog generation uses only new accepted source items as execution
  scope.
- Existing project context is available to generators only as read-only
  anti-duplication context.
- Roadmap extension appends a new phase without mutating existing phases.
- Completed Stories and Sprints remain unchanged.
- All mutating commands are guarded and idempotent.
- `pyrepo-check --all` passes after implementation.
