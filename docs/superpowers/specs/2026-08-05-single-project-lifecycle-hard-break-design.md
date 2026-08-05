# Single Project Lifecycle Hard-Break Design

**Date:** 2026-08-05
**Status:** Approved for amended written-spec review
**Supersedes:** `2026-08-02-domain-workflow-graph-hard-break-design.md`
**Scope:** Project creation, initial Vision and Product Goal, optional repository
attachment, deterministic local repository inspection, Authority sequencing,
workflow convergence, and removal of the former dual-origin setup architecture

## Summary

AgileForge will have one Project lifecycle. A Project is created with a name. A
local Git repository may be attached during creation or later, but repository
attachment does not select a different workflow. The first product step is an
interactive Project Vision interview. After a human accepts Vision, a separate
Product Goal interview defines the objective that scopes discovery.

The existing product-definition and delivery sequence remains intact. Vision
and Product Goal use separate focused interviews and separate review decisions.
Expanded, the lifecycle is:

```text
Create Project (Name)
-> Optional Repository Attachment (during creation or later)
-> Project Vision Interview
-> Human Review And Acceptance Of Project Vision
-> Product Goal Interview
-> Human Review And Acceptance Of Product Goal
-> Grill Me With Docs
-> To Spec
-> Human Specification Review And Acceptance
-> Specification Authority Compile, Review, And Acceptance
-> Product Backlog Extract/Generate, Refine, Review, And Acceptance
-> Roadmap Generate, Refine, Review, And Acceptance
-> User Stories Generate, Refine, Dependency Review, And Acceptance
-> Sprint Candidate Readiness
-> Sprint Planning, Human Review, And Start
-> Task Execution And Evidence
-> Story Closure
-> Sprint Review And Closure
-> Post-Sprint Triage
```

The Project Vision and Product Goal are related but distinct. The Vision states
the product's enduring direction. The Product Goal states one valuable future
state pursued within that direction. The accepted Vision is trusted context for
the Goal interview, so the operator does not repeat it. One accepted Product
Goal remains active across as many Sprints as needed until a human records it as
fulfilled or abandoned. Weekly features normally contribute to that Goal; they
do not create new Product Goals. The expanded sequence above documents the
workflow that this hard break must preserve; this design does not authorize
deleting or collapsing downstream artifacts and human decisions.

Repository attachment is an orthogonal capability. It records a small,
deterministic snapshot without reading repository content, building a complete
inventory, calling a model, using a network service, or blocking discovery.

This is a hard break. The former origin field, specialized setup paths, global
repository reconstruction machinery, compatibility routes, persistence models,
tests, prompts, and active documentation are deleted rather than deprecated.
No data migration or compatibility mode is provided.

## Problem

The dual-origin architecture turned a narrow requirement -- avoid planning work
that the repository already satisfies -- into a mandatory project-wide setup
pipeline. That pipeline introduced full inventory, model-backed curation,
specification review, authority compilation, and current-state reconstruction
before an operator could start normal product discovery.

This produced the wrong coupling:

- repository presence determined the Project lifecycle;
- setup paid for broad technical analysis before a feature was known;
- unrelated repository changes invalidated project-wide evidence;
- the human UI exposed internal workflow machinery;
- provider failures and semantic-repair loops blocked basic Project creation;
- the same Project could not naturally move from no repository to an active
  repository without changing conceptual type.

Repository presence is evidence availability, not product identity. Every
Project must use the same product-development flow.

## Goals

- Provide one Project lifecycle and one workflow graph.
- Make an interview-based Project Vision the first product step.
- Capture and human-accept Project Vision before starting a separate Product
  Goal interview.
- Permit exactly one active accepted Product Goal until it is fulfilled or
  abandoned.
- Allow one local Git repository to be attached during creation or later.
- Inspect repository identity and working-tree state without provider calls.
- Preserve `grill-me-with-docs`, `to spec`, specification and authority review,
  Product Backlog extraction and refinement, Roadmap, User Story
  generation and refinement, dependency review, Sprint planning and review,
  execution, closure, and post-Sprint triage behavior.
- Keep repository inspection behind a small replaceable interface.
- Support Git worktrees and detached HEADs.
- Allow dirty repositories while making their state explicit and fingerprinted.
- Remove all executable and active-documentation traces of the retired
  dual-origin architecture.
- Recreate development databases under the new schema instead of migrating old
  Project records.

## Non-Goals

- No feature-level current-state or gap assessment in this slice.
- No complete repository inventory.
- No repository-content hashing or source-file ingestion.
- No model-backed repository interpretation.
- No CodeGraph dependency during Project setup.
- No GitHub API or GitHub CLI dependency during Project setup.
- No remote repository cloning.
- No multi-repository Project support in this slice.
- No import or migration of existing AgileForge Project records.
- No deletion or collapse of Project Vision, Product Goal, Product Backlog,
  Roadmap, User Story refinement, Sprint review, or execution stages.
- No redesign of the established discovery, specification, extraction,
  planning, or execution stages beyond making Vision the first product step,
  placing Authority after accepted specification, and reconnecting every stage
  to one lifecycle.

Feature-level implementation assessment is a separate follow-up module. It will
compare one accepted desired outcome with targeted repository evidence after
`to spec` and before remaining work is admitted to the Product Backlog.

## Domain Model

### Project

`Project` has no origin or setup-mode field. Its setup identity contains:

- `project_id`;
- `name`;
- optional description;
- creation and update timestamps; and
- the existing product artifacts accumulated by the common workflow.

Project creation does not require a Product Goal or generated product content.
It creates the durable identity needed to conduct and review the Vision
interview.

### Project Vision

The first product action is a guided Vision interview. It gathers the
established Vision components and asks follow-up questions until it can produce
one immutable Project Vision draft. Incomplete turns are resumable draft state,
not accepted product facts. A human accepts, rejects, or provides feedback
against the exact Vision fingerprint.

The Vision interview may use the attached repository's identity as provenance,
but it does not infer product intent from source code. Human answers remain the
highest-authority source for Vision.

The full Vision interview is required only while the Project has no accepted
Vision. An intentional Vision revision reopens the Vision interview. An active
Product Goal must be fulfilled or explicitly abandoned before a revised Vision
can be accepted, preventing the revision from silently invalidating committed
work.

### Product Goal

After Vision acceptance, a separate guided Product Goal interview receives the
accepted Vision as read-only context. It asks focused questions about the next
valuable future state, beneficiary, value, observable success, and boundaries.
It produces one immutable Product Goal candidate. It does not define features,
technical behavior, or implementation tasks.

Incomplete Goal interview turns are resumable draft state. A human accepts,
rejects, or provides feedback against the exact Goal fingerprint. Feedback
creates another revision of the same Goal candidate. Acceptance creates the
single active Product Goal under the current Vision.

An accepted Product Goal is immutable and may span multiple Sprints. Weekly
ideas become Product Backlog candidates under that Goal. A candidate that does
not contribute to the active Goal is rejected, deferred, or triggers an
explicit human decision to abandon the Goal. AgileForge permits another Goal
interview only after the active Goal is recorded as fulfilled or abandoned.

The Goal lifecycle is explicit:

```text
interview draft
-> immutable review candidate
-> accepted and active
-> fulfilled | abandoned
```

Rejected candidates and feedback revisions never become active. Exactly one
accepted Goal may lack an outcome for a Project. `fulfilled` means the human has
confirmed that the valuable future state has been achieved. `abandoned` means
the human has intentionally stopped pursuing it and supplied a rationale. A
weekly release, Sprint closure, or empty Sprint backlog does not implicitly
finish the Goal. Either outcome is available only when no Sprint is active and
every closed Sprint under the Goal has completed post-Sprint triage; abandoning
a Goal never silently cancels execution work.

### Product Artifact Lineage

The initial and recurring lineage is explicit:

```text
Project
-> accepted Vision
-> accepted Product Goal
-> accepted Grill Me With Docs discovery artifact
-> accepted To Spec specification
-> accepted Specification Authority
-> accepted Product Backlog changes
-> Roadmap
-> User Stories
-> Sprint
```

Each arrow carries the source artifact identity and fingerprint. Replacing an
upstream artifact makes dependent downstream artifacts stale; it never mutates
them in place.

Vision is Project-owned, not Authority-owned. Its generation request, graph
rule, persistence record, and fingerprint therefore have no Authority
prerequisite or Authority lineage fields. Product Goal references the accepted
Vision. Specification references the accepted Vision, Product Goal, and
discovery artifact. Authority references the accepted specification. Product
Backlog changes reference both the accepted Product Goal and Authority. Roadmap,
Stories, and Sprint artifacts retain their downstream lineage guards.

### Repository Binding

A Project may have zero or one active `RepositoryBinding`:

```text
RepositoryBinding
- project_id
- worktree_path
- common_git_dir
- head_sha
- branch_name | null
- detached_head
- dirty
- status_fingerprint
- remotes[]
- probe_version
- inspected_at
```

The binding is optional. Its presence does not add, remove, or reorder workflow
nodes. A later successful attachment replaces the prior active binding through
the normal guarded mutation contract.

The binding records observation provenance. It is not accepted product
authority and does not claim that any feature exists or works.

### Repository Probe

The required boundary is intentionally small:

```python
class RepositoryProbe(Protocol):
    def inspect(self, path: Path | str) -> RepositoryProbeResult: ...
```

`RepositoryProbeResult` contains the fields needed to create a
`RepositoryBinding` plus typed warnings. The production adapter uses the
already-pinned GitPython dependency.

The probe may inspect only Git metadata and status:

- canonical worktree root;
- common Git directory;
- HEAD commit;
- active branch or detached HEAD;
- tracked, staged, deleted, renamed, and untracked status entries;
- configured remote URLs; and
- a canonical status fingerprint.

It must not read source-file content, enumerate a complete model context, call a
model, contact a remote service, or mutate Git state.

## Probe Consistency And Fingerprinting

The adapter reads HEAD before and after status collection. If HEAD changes during
inspection, the operation returns `REPOSITORY_CHANGED_DURING_PROBE` and writes no
binding.

The status fingerprint is a canonical hash over:

```text
probe schema version
canonical worktree path
common Git directory
HEAD SHA
branch name or detached marker
sorted normalized status entries
sorted normalized remote URLs
```

The fingerprint does not contain file contents. Untracked paths participate in
the fingerprint, so `dirty=false` and `dirty=true` snapshots cannot collide.

Dirty repositories are accepted with a typed warning. The UI must display the
warning, and the CLI must return it in structured output. Dirty state is not a
setup blocker.

## Workflow

### Project Creation Without A Repository

1. The operator supplies `name` and may supply an optional description.
2. AgileForge creates the Project under the common graph.
3. The Project Vision interview is immediately available.
4. Repository attachment remains available as an independent action.

### Project Creation With A Repository

1. The operator supplies `name`, an optional description, and a local path.
2. AgileForge probes the path before mutating Project state.
3. If probing fails, Project creation fails without a partial Project record.
4. If probing succeeds, Project and binding are committed atomically.
5. The Project Vision interview is immediately available, exactly as when no
   repository is attached.

### Initial Project Vision

1. AgileForge starts or resumes the interview using recorded human answers and
   any prior incomplete Vision components.
2. Each turn returns the current draft and focused follow-up questions; the UI
   never asks the operator for raw JSON or workflow guard values.
3. When all required Vision components are present, AgileForge produces one
   immutable Vision candidate.
4. Human feedback resumes the Vision interview without accepting the artifact.
5. Human acceptance records the Vision decision and unlocks the Product Goal
   interview, not discovery or Authority.

### Product Goal Interview And Review

1. AgileForge starts or resumes a Goal interview using the accepted Vision and
   recorded human answers.
2. Each turn returns the current Goal components and focused follow-up
   questions. The agent may reuse accepted Vision context but may not rewrite
   Vision.
3. When the Goal has a valuable future state, beneficiary, value, observable
   success, and boundaries, AgileForge produces one immutable Goal candidate.
4. Human feedback creates another revision of that Goal candidate.
5. Human acceptance records the single active Product Goal and unlocks
   `grill-me-with-docs` discovery.
6. A later Goal interview remains unavailable until the active Goal has an
   explicit fulfilled or abandoned outcome.
7. Goal fulfillment or abandonment is a separate fingerprint-bound human
   decision. It cannot be inferred from Sprint state or generated by a model.

### Discovery, Specification, And Authority

1. `grill-me-with-docs` receives the accepted Vision and Product Goal as
   baseline context and gathers goal-specific documents, constraints, examples,
   edge cases, and unresolved decisions.
2. `to spec` converts that accepted discovery output into a desired-behavior
   specification.
3. A human reviews and accepts the exact specification version.
4. Authority compilation converts only that accepted specification into
   versioned invariants and guardrails. It does not invent Vision or Product
   Goal content.
5. A human reviews and accepts, rejects, or provides feedback on the compiled
   Authority.
6. Accepted Authority unlocks Product Backlog extraction and refinement,
   followed by Roadmap, User Stories, Sprint planning, and execution.

Authority therefore never gates the initial Vision interview. It gates
downstream delivery artifacts that must remain consistent with the accepted
specification.

For later increments, AgileForge keeps both the accepted Vision and active
Product Goal. Repeating weekly feature work is refined under that Goal and does
not recreate the Project, rerun the Vision interview, or create another Product
Goal. After the Goal is fulfilled or abandoned, AgileForge starts a new Goal
interview under the accepted Vision; the accepted replacement then follows the
same discovery, specification, Authority, and backlog-admission sequence.

### Later Repository Attachment

1. The operator selects one existing Project and supplies a local path.
2. AgileForge probes the path.
3. A successful guarded mutation stores the binding.
4. The graph position does not change except for repository-status projection.
5. A failed probe leaves the prior binding and Project state untouched.

### Refresh

Repository status is refreshed only through an explicit probe action or as a
cheap preflight for a later evidence-dependent operation. Merely opening the
dashboard does not trigger repository scanning, provider calls, or mutation.

## Interfaces

### Human UI

The create form contains:

- Project Name;
- optional Description; and
- optional Repository Path.

After creation, the UI opens the guided Vision interview. It presents one or a
small related set of plain-language questions per turn, preserves prior answers,
and shows the completed Vision for human review. After Vision acceptance, the
same page opens the separate Goal interview and later shows the Goal candidate
with its accepted parent Vision for a separate human decision.

There is no setup-type selector. Repository details are presented as plain
status: path, branch or detached HEAD, short commit, clean or dirty, and latest
inspection time. Fingerprints and internal guard values are not operator input.

The Project page provides familiar attach, replace, and refresh commands. It
does not expose graph node identifiers, raw JSON, Git object plumbing, or model
configuration for setup.

### Agent CLI

The CLI exposes task-specific structured commands:

```text
agileforge project create --name ... [--description ...] [--repository-path ...]
agileforge vision respond --project-id ... --text ...
agileforge vision status --project-id ...
agileforge vision review --project-id ... --decision ...
agileforge goal respond --project-id ... --text ...
agileforge goal status --project-id ...
agileforge goal review --project-id ... --decision ...
agileforge goal complete --project-id ... --rationale ...
agileforge goal abandon --project-id ... --rationale ...
agileforge repository attach --project-id ... --path ...
agileforge repository status --project-id ...
agileforge repository refresh --project-id ...
```

Mutating commands retain graph, fact, decision, idempotency, and actor guards.
Vision and Goal responses return their current components, completion status,
and focused questions in structured output. Repository responses include
provenance, warnings, and typed errors. The agent never supplies derived commit,
dirty-state, remote, artifact-fingerprint, or workflow-guard data.

`goal complete` records the `fulfilled` outcome. Both Goal outcome commands
require a human rationale and resolve the exact active Goal internally; the
operator does not paste its fingerprint.

### Optional Future Providers

Later modules may implement independent interfaces such as:

```text
RepositoryContextProvider
RemoteRepositoryProvider
```

CodeGraph may implement targeted structural context. A GitHub adapter may
provide remote metadata, issues, and pull requests. Neither provider owns local
repository truth or participates in basic Project setup.

## Errors

The repository boundary returns typed failures for:

- path missing;
- path not a directory;
- path not a Git worktree;
- unreadable Git metadata;
- unborn HEAD;
- repository changed during probe; and
- unsupported or malformed path encoding.

Detached HEAD and dirty state are successful results with explicit fields;
dirty state also emits a warning. Missing remotes are valid.

The product-definition boundary returns typed failures when:

- a Goal interview starts without an accepted Vision;
- another Product Goal is already active;
- Vision acceptance is attempted while a Product Goal is active;
- a Goal outcome is recorded for a stale or already resolved Goal; or
- a review decision does not match the exact immutable candidate.

These failures write no partial artifact, decision, or outcome row.

No failure path may create a partial binding, silently fall back to a full
filesystem scan, call a model, or reinterpret the Project workflow.

## Hard-Break Deletion Policy

Implementation removes rather than adapts the former architecture:

- the Project origin column, constraint, request field, and fact field;
- setup branches, nodes, transitions, reasons, and route renderers;
- specialized repository reconstruction models and tables;
- specialized curation and current-state agents, recipes, prompts, contracts,
  commands, endpoints, services, annotations, and caches;
- compatibility aliases, translation layers, and migration code;
- specialized UI controls and copy;
- tests whose only purpose is preserving retired behavior; and
- active specs, plans, examples, manuals, and feedback artifacts that would
  teach fresh agents to reconstruct the deleted workflow.

Neutral Git path encoding, Git worktree handling, canonical hashing, and other
small utilities may remain only when consumed by the new probe and renamed to
describe their actual responsibility.

The two retired origin labels currently accepted by `Project.origin` must have
zero case-insensitive matches in the live source tree after implementation. This
spec deliberately does not repeat those labels so it can remain active after
the cleanup. Git history and external issue history are not rewritten.

## Database And Runtime Cutover

This feature provides no schema migration. Development and acceptance profiles
must initialize fresh business and trace databases. The worktree-local
`agileforge-dev` launcher remains the supported test boundary.

caRtola, ASA Deep Process Control Advisory System, and MyFinance are recreated
through the common Project path for acceptance. Their old AgileForge rows are
not imported.

## Testing

### Repository Probe

Provider-free temporary-repository tests cover:

- clean branch;
- staged, unstaged, deleted, renamed, and untracked changes;
- detached HEAD;
- linked Git worktree;
- zero, one, and multiple remotes;
- non-ASCII and surrogateescaped paths;
- missing path and non-repository directory;
- unborn HEAD;
- HEAD change during probe; and
- deterministic fingerprint replay.

### Domain And Persistence

- Project creation requires a name and does not require generated product
  content.
- Project creation succeeds without a repository.
- Project creation with a valid repository stores Project and binding atomically.
- Failed probing creates neither Project nor binding.
- The Vision interview is available immediately with or without a repository.
- Vision acceptance is fingerprint-bound and unlocks only the Product Goal
  interview.
- Product Goal acceptance is separately fingerprint-bound and unlocks
  discovery.
- Exactly one accepted Product Goal is active at a time.
- A later Product Goal can begin under the accepted Vision only after the active
  Goal is fulfilled or abandoned.
- Product Goal feedback revisions do not rewrite Vision history.
- Vision revision acceptance requires any active Product Goal to be fulfilled
  or abandoned first.
- Later repository attachment does not alter product-definition availability.
- Reattachment is guarded and replaces only the active binding.
- No origin field or specialized setup table exists in the fresh schema.
- Domain facts and fingerprints are identical in shape regardless of repository
  attachment, except for the optional binding projection.

### CLI, API, And UI

- CLI and API reject legacy request fields as unknown input.
- CLI emits structured repository provenance and warnings.
- The create modal has no setup-type selector.
- The create modal requires only Project Name and contains optional Description
  and Repository Path fields.
- Project creation opens the guided Vision interview.
- Vision interview turns preserve accepted answers and expose focused questions,
  not raw JSON.
- Human Vision review shows only the exact Vision candidate.
- Accepted Vision opens a separate guided Product Goal interview.
- Human Goal review shows the exact Goal candidate and accepted parent Vision.
- Human forms never request commit, dirty state, remotes, or fingerprints.
- Project pages expose attach, replace, and refresh repository actions.
- Playwright verifies creation and attachment on desktop and mobile viewports.

### Absence And Retained Behavior

- A whole-tree case-insensitive scan finds neither retired origin label.
- Package-resource tests prove deleted prompts and agents are absent.
- Project Vision interview and review, `grill-me-with-docs`, `to spec`,
  specification and authority review, Product Backlog, Roadmap, User Story
  refinement and dependency review, Sprint planning and review, execution,
  closure, and post-Sprint triage contract tests remain green.
- Graph tests prove that Vision is available before Authority and Authority is
  unavailable until an exact specification version has human acceptance.
- A clean-source wheel and sdist contain no retired modules or resources.
- `uv run --frozen pyrepo-check --all` passes without typing suppressions.
- `git diff --check` passes.

### Acceptance Repositories

For each of caRtola, ASA Deep Process Control Advisory System, and MyFinance:

1. initialize a fresh isolated AgileForge profile;
2. create the Project with its name, using repository-at-creation for at least
   one Project and later attachment for at least one other Project;
3. attach the local Git worktree when it was not supplied during creation;
4. verify the projected path, commit, branch or detached state, and dirty state;
5. verify the Vision interview is the first available product action;
6. confirm creation and repository probing performed no provider-backed model
   call;
7. conduct and human-review the Vision;
8. conduct and human-review the Product Goal under the accepted Vision;
9. verify accepted Vision and Product Goal unlock `grill-me-with-docs` rather
   than Authority compilation; and
10. stop before further paid discovery unless the operator explicitly approves
   that repository's real feature test.

## Acceptance Criteria

- One Project graph serves every Project.
- Repository attachment never selects a workflow variant.
- The guided Vision interview is the first product action after valid Project
  creation.
- Project creation requires no Product Goal or provider call.
- Human Vision acceptance unlocks only the Product Goal interview.
- Human Product Goal acceptance unlocks discovery.
- One accepted Product Goal remains active until a human records it fulfilled
  or abandoned; weekly features do not create replacement Goals.
- Authority compilation occurs only after human acceptance of a `to spec`
  specification and before Product Backlog extraction.
- Authority never gates or authors the initial Project Vision.
- The deterministic probe performs no content scan, network call, or model call.
- Dirty and detached repositories are represented honestly without blocking.
- The Project domain, database schema, CLI, API, frontend, tests, package, and
  active documentation contain no retired origin vocabulary or specialized
  setup machinery.
- No compatibility or migration path exists.
- The established discovery-to-execution behavior, including every named
  artifact and human review stage in the expanded lifecycle, remains covered
  and passing.
- The three named acceptance repositories can be recreated and attached through
  the same operator and agent surfaces.

## Follow-Up

After this hard break is accepted, design the separate feature-level
`CurrentStateAssessment` module. That module will use deterministic retrieval
first, optional CodeGraph context second, and broader analysis only when risk or
dependency evidence requires it. It will not reintroduce a Project lifecycle
variant.
