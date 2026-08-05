# Single Project Lifecycle Hard-Break Design

**Date:** 2026-08-05
**Status:** Approved for written-spec review
**Supersedes:** `2026-08-02-domain-workflow-graph-hard-break-design.md`
**Scope:** Project creation, optional repository attachment, deterministic local
repository inspection, workflow convergence, and removal of the former
dual-origin setup architecture

## Summary

AgileForge will have one Project lifecycle. A Project is created with a name and
human-owned Product Goal. A local Git repository may be attached during creation
or later, but repository attachment does not select a different workflow.

The existing product-discovery sequence remains intact:

```text
Create Project
-> Product Goal
-> Grill Me With Docs
-> To Spec
-> Human Review
-> Extract And Refine Product Backlog
-> Sprint Planning
-> Execution
```

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
- Require a human-owned Product Goal before product discovery starts.
- Allow one local Git repository to be attached during creation or later.
- Inspect repository identity and working-tree state without provider calls.
- Preserve `grill-me-with-docs`, `to spec`, specification review, extraction,
  Product Backlog refinement, planning, and execution behavior.
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
- No redesign of the established discovery, specification, extraction,
  planning, or execution stages beyond reconnecting them to one lifecycle.

Feature-level implementation assessment is a separate follow-up module. It will
compare one accepted desired outcome with targeted repository evidence after
`to spec` and before remaining work is admitted to the Product Backlog.

## Domain Model

### Project

`Project` has no origin or setup-mode field. Its setup identity contains:

- `project_id`;
- `name`;
- `product_goal`;
- optional description;
- creation and update timestamps; and
- the existing product artifacts accumulated by the common workflow.

`product_goal` is concise human intent. It is not generated repository truth and
does not replace a richer reviewed Vision artifact if the downstream workflow
produces one.

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

1. The operator supplies `name` and `product_goal`.
2. AgileForge creates the Project under the common graph.
3. Product discovery is immediately available.
4. Repository attachment remains available as an independent action.

### Project Creation With A Repository

1. The operator supplies `name`, `product_goal`, and an optional local path.
2. AgileForge probes the path before mutating Project state.
3. If probing fails, Project creation fails without a partial Project record.
4. If probing succeeds, Project and binding are committed atomically.
5. Product discovery is immediately available, exactly as when no repository is
   attached.

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
- Product Goal; and
- optional Repository Path.

There is no setup-type selector. Repository details are presented as plain
status: path, branch or detached HEAD, short commit, clean or dirty, and latest
inspection time. Fingerprints and internal guard values are not operator input.

The Project page provides familiar attach, replace, and refresh commands. It
does not expose graph node identifiers, raw JSON, Git object plumbing, or model
configuration for setup.

### Agent CLI

The CLI exposes task-specific structured commands:

```text
agileforge project create --name ... --product-goal ... [--repository-path ...]
agileforge repository attach --project-id ... --path ...
agileforge repository status --project-id ...
agileforge repository refresh --project-id ...
```

Mutating commands retain graph, fact, decision, idempotency, and actor guards.
JSON responses include repository provenance, warnings, and typed errors. The
agent never supplies derived commit, dirty-state, remote, or fingerprint data.

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

- Project creation requires name and Product Goal.
- Project creation succeeds without a repository.
- Project creation with a valid repository stores Project and binding atomically.
- Failed probing creates neither Project nor binding.
- Later attachment does not alter discovery availability.
- Reattachment is guarded and replaces only the active binding.
- No origin field or specialized setup table exists in the fresh schema.
- Domain facts and fingerprints are identical in shape regardless of repository
  attachment, except for the optional binding projection.

### CLI, API, And UI

- CLI and API reject legacy request fields as unknown input.
- CLI emits structured repository provenance and warnings.
- The create modal has no setup-type selector.
- Human forms never request commit, dirty state, remotes, or fingerprints.
- Project pages expose attach, replace, and refresh repository actions.
- Playwright verifies creation and attachment on desktop and mobile viewports.

### Absence And Retained Behavior

- A whole-tree case-insensitive scan finds neither retired origin label.
- Package-resource tests prove deleted prompts and agents are absent.
- `grill-me-with-docs`, `to spec`, review, extraction, backlog, planning, and
  execution contract tests remain green.
- A clean-source wheel and sdist contain no retired modules or resources.
- `uv run --frozen pyrepo-check --all` passes without typing suppressions.
- `git diff --check` passes.

### Acceptance Repositories

For each of caRtola, ASA Deep Process Control Advisory System, and MyFinance:

1. initialize a fresh isolated AgileForge profile;
2. create the Project with name and Product Goal;
3. attach the local Git worktree;
4. verify the projected path, commit, branch or detached state, and dirty state;
5. verify product discovery is immediately available;
6. confirm setup performed no provider-backed model call; and
7. stop before running paid discovery unless the operator explicitly approves
   that repository's real feature test.

## Acceptance Criteria

- One Project graph serves every Project.
- Repository attachment never selects a workflow variant.
- Product discovery is available immediately after valid Project creation.
- The deterministic probe performs no content scan, network call, or model call.
- Dirty and detached repositories are represented honestly without blocking.
- The Project domain, database schema, CLI, API, frontend, tests, package, and
  active documentation contain no retired origin vocabulary or specialized
  setup machinery.
- No compatibility or migration path exists.
- The established discovery-to-execution behavior remains covered and passing.
- The three named acceptance repositories can be recreated and attached through
  the same operator and agent surfaces.

## Follow-Up

After this hard break is accepted, design the separate feature-level
`CurrentStateAssessment` module. That module will use deterministic retrieval
first, optional CodeGraph context second, and broader analysis only when risk or
dependency evidence requires it. It will not reintroduce a Project lifecycle
variant.
