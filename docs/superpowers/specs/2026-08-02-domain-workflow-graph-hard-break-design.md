# Domain Workflow Graph Hard-Break Design

**Date:** 2026-08-02
**Status:** In review
**Spec mode:** proposed_change
**Scope:** Project onboarding, workflow routing, durable workflow facts,
human-review gates, ADK execution, CLI/API/frontend projections, brownfield
inventory, model policy, legacy runtime removal, and three-repository acceptance

## Summary

AgileForge will replace its scattered finite-state-machine and session-derived
routing logic with one framework-neutral hierarchical domain graph.

The graph will not store a current state or cursor. `WorkflowDomain.position()`
will derive available, waiting, blocked, and invalid nodes from typed durable
facts every time it is called. `WorkflowDomain.transition()` will validate a
typed request against the same facts and commit the resulting facts atomically.

Google ADK 2 graph workflows will execute eligible agent work, but ADK sessions
will remain disposable execution traces. They will not decide what AgileForge
may do. Deleting all ADK and product-workflow session records must not change
the position returned for a Project.

This is a hard break:

- no migration of the current AgileForge database;
- no compatibility package or dual routing path;
- no persisted scalar workflow state as independent authority;
- no `orchestrator_agent/` namespace in the final tree; and
- no old FSM, session-routing, or orchestrator references in active code,
  configuration, tests, or documentation.

The completed system will be accepted on a fresh database by onboarding
caRtola, ASA Deep Process Control Advisory System, and MyFinance. MyFinance will
also carry the real "Statement Streams and Coverage" feature through the full
workflow and isolated synthetic-data verification.

## Context And Evidence

### Current Routing Is Split Across Sources

The current `agileforge workflow next` entry point reaches
`AgentWorkbenchApplication.workflow_next()` in
`services/agent_workbench/application.py`. Routing decisions are then spread
through a large application facade and phase-specific helpers.

`services/agent_workbench/read_projection.py` hydrates workflow information from
product-workflow session state. Other route decisions query normalized tables
directly. The result is not one FSM. It is a collection of partially overlapping
state machines whose answers can disagree.

Issue #193 is a concrete example. An accepted spec-amendment draft remained
advertised as a valid scope-extension start after the same content had already
become the accepted specification. The advertised command then failed against
newer durable authority facts. A local suppression fixed that symptom, but the
architectural defect is broader: command advertisement and command validation
were not evaluating one fact snapshot through one rule set.

### Pre-Project Greenfield State Adds A Second Identity

Greenfield discovery currently uses a `context_key` before a Project exists.
Only after the challenge artifact, PRD, and initial spec draft are accepted does
project creation consume that chain.

Repository audit and runtime probes found that this promotion boundary can:

- reread changed file content instead of using the accepted canonical content;
- consume one accepted context into multiple Projects;
- overwrite the context's Project ownership; and
- lose greenfield provenance during setup recovery.

Brownfield setup already creates a Project identity first and stores scan and
curation records under that Project. The hard-break design makes greenfield use
the same ownership rule.

### The Brownfield Scanner Does Not Scale To The Acceptance Repositories

`services/agent_workbench/brownfield_curation.py` currently walks every directory
except `.git`, does not honor Git ignore rules, and returns after the first
`MAX_SCAN_MANIFEST_FILES = 1_000` accepted files. Cache and generated files can
consume the limit before relevant source files are reached.

caRtola, ASA, and MyFinance exceed this accidental boundary. Their acceptance
run therefore requires a Git-aware inventory boundary rather than a larger
magic number.

### ADK Graphs Are Useful But Not Domain Authority

Google ADK 2.2.0 supports graph nodes, nested workflows, routing, parallel
branches, joins, retries, and resumable execution. Those capabilities fit the
execution side of AgileForge.

ADK resume semantics are at-least-once for tool and external side effects, and
ADK CLI/Web resume support is not a reliable product-state boundary. ADK also
does not own AgileForge's accepted authority, reviews, backlog, or sprint facts.
Using its session cursor as the product source of truth would recreate the
current split-state problem inside a new framework.

## Goals

- Make one domain module decide every available workflow action.
- Derive position from typed durable facts after every command and restart.
- Support multiple simultaneously available nodes and explicit joins.
- Preserve accepted specification, authority, review, backlog, roadmap, story,
  sprint, task, and audit facts as durable business truth.
- Give greenfield and brownfield Projects one durable identity from the start.
- Make human review a durable stop that survives process and session loss.
- Reject stale advertised actions transactionally.
- Make retries idempotent for database effects and explicit about external
  at-least-once behavior.
- Keep CLI, API, frontend, and ADK as adapters over the same domain interface.
- Delete obsolete orchestrator, FSM, and session-routing machinery.
- Re-onboard the three selected repositories and prove one real feature flow.

## Non-Goals

- No migration or preservation of the current AgileForge database.
- No compatibility behavior for old session payloads, FSM states, context keys,
  command envelopes, or internal Python imports.
- No event-sourced rewrite of accepted business authority.
- No generic JSON fact table.
- No separate intake aggregate or multi-Project discovery.
- No new multi-tenant authorization system. `changed_by` remains audit
  attribution, not an access-control claim.
- No promise of exactly-once external model or tool execution.
- No automatic production deployment or use of private MyFinance records.
- No model upgrade based only on benchmark marketing or intuition.

## Settled Decisions

1. One discovery may create at most one Project.
2. A Project Shell exists before greenfield discovery or brownfield curation.
3. A Project Shell owns durable artifacts but cannot create Executable Work.
4. Accepted Authority, not a shell lifecycle flag, unlocks Executable Work.
5. Workflow position is derived and never persisted as independent authority.
6. Product-workflow sessions are not a source of truth.
7. Existing normalized domain tables remain where their concepts are sound.
8. New typed tables replace routing facts that currently exist only in session
   JSON or ambiguous mutable status fields.
9. Artifacts are immutable versions; reviews and corrections append typed
   decision or supersession facts.
10. The domain graph contains no `google.adk` type.
11. ADK executes graph decisions but does not make domain decisions.
12. The first implementation uses one full fact-set fingerprint per Project.
13. CLI commands remain task-specific; users do not submit a generic graph
    transition command.
14. The current database is discarded at cutover.
15. The final runtime has no compatibility package and no orchestrator agent.

## Domain Boundary

### Canonical Aggregate

`Project` is the canonical aggregate name. It owns one product's discovery,
authority, planning, and execution history. A repository is evidence associated
with a Project; it is not the Project identity. A session is an execution trace;
it is not the Project identity.

The persistence implementation should align model and table names with this
language on the fresh schema. Historical Git commits remain the record of the
old `Product` naming.

### Deep Interface

All callers use one two-method module:

```python
class WorkflowDomain:
    def position(self, project_id: int) -> WorkflowPosition: ...
    def transition(self, request: TransitionRequest) -> TransitionResult: ...
```

`position()` is the only routing query. `transition()` is the only workflow
mutation entry point. Internal repositories, transaction management, graph
evaluation, fingerprints, and audit writes stay hidden behind this interface.

The interface is intentionally small. It prevents callers from rebuilding
routing decisions by composing lower-level query helpers.

### Project Bootstrap

`OpenProjectShell` is the one transition variant that does not yet have a
`project_id` or position fingerprint. It requires:

- a Project name;
- an origin of `greenfield` or `brownfield`;
- an idempotency key;
- an actor; and
- optional correlation metadata.

It creates one durable Project identity and returns its ID. The name is unique
among existing Projects. Deleting an incomplete shell releases the name.

All later transitions require a `project_id`, graph version, expected fact
fingerprint, expected node-decision fingerprint, idempotency key, and actor.

### Closed Transition Union

`TransitionRequest` is a closed discriminated union of command-specific request
types. Examples include:

```text
OpenProjectShell
RecordChallengeArtifact
RecordPrdVersion
DecidePrd
RecordInitialSpecDraft
DecideInitialSpecDraft
RegisterInitialScope
CompileAuthority
DecideAuthority
RecordVisionDraft
DecideVision
RecordBacklogDraft
PlanSprint
CompleteTask
CloseSprint
RecordPostSprintTriage
StartScopeExtension
AbandonProjectShell
```

Each type carries only the fields valid for that transition. The domain must not
accept `action: str` plus an untyped dictionary.

### Workflow Position

`WorkflowPosition` contains:

```text
project_id
graph_version
fact_fingerprint
evaluated_at
available_nodes
waiting_nodes
blocked_nodes
invalid_nodes
terminal
decisions
```

Node decisions are typed values with a stable node ID, decision fingerprint,
reason code, recommendation kind, required input schema, relevant fact
references, blockers, and optional validity deadline. Command text is not part
of the domain decision.

Recommendation kind is one of `required`, `optional_reentry`, or `recovery`.
Starting a brand-new scope extension after current scope completes is optional
re-entry. It is valid but does not make `workflow next` report unfinished work.

Position categories mean:

- `available`: prerequisites are satisfied and the transition may run now;
- `waiting`: a durable agent attempt or human-review artifact is outstanding;
- `blocked`: the node is valid but an earlier prerequisite is incomplete;
- `invalid`: durable facts contradict graph invariants and normal mutation is
  unsafe; and
- `terminal`: no valid required or recovery work remains in the selected graph;
  optional re-entry may still be available.

Completed nodes are internal graph evidence. They do not need a separate public
command category.

## Hierarchical Graph

The root Project graph contains these child graphs:

1. onboarding;
2. authority;
3. vision;
4. backlog;
5. planning;
6. execution; and
7. scope extension.

`planning` contains roadmap, story, and sprint-planning graphs. `execution`
contains sprint, task, review, and post-sprint-triage graphs. `scope extension`
contains challenge artifact, PRD, spec amendment, authority, and downstream
reconciliation graphs.

Each node declares:

- a stable node ID;
- typed prerequisite and invalidity rules;
- the fact selector proving completion;
- the transition request type it accepts;
- its recommendation kind; and
- an optional child graph or join rule.

Rules are pure functions over one immutable fact snapshot and an explicit
evaluation time. They never read a hidden system clock. Nodes do not query a
database, inspect an ADK session, render CLI commands, or perform side effects.

Time-sensitive decisions, such as recovery after an attempt lease expires,
carry a validity deadline in their decision fingerprint. A fact fingerprint
therefore remains a hash of durable facts, while a decision fingerprint also
pins time-sensitive evaluation inputs.

The graph may expose multiple available nodes. A join becomes available only
when all required branch facts exist. This is the capability the scalar FSM
cannot represent cleanly.

The graph definition has an explicit version. Position and transition requests
carry that version so a request offered by one deployed graph cannot silently
execute against another.

## Durable Workflow Facts

### Principles

A Workflow Fact is a typed durable record whose current value can affect graph
evaluation. Facts live in domain-specific tables. The graph does not parse a
generic event stream or arbitrary JSON blob to discover truth.

The existing mutation ledger may continue to provide audit and idempotency
evidence. It is not queried as the primary business read model.

Session records, ADK events, logs, rendered packets, and exported files are not
Workflow Facts unless a typed domain record explicitly adopts their validated
content.

### Core Project-Owned Records

The fresh schema needs these conceptual records:

- `Project`: identity, origin, name, and creation provenance;
- `DiscoveryRun`: Project-owned identity with purpose `initial` or `extension`;
- `ChallengeArtifact`: immutable version attached to one DiscoveryRun;
- `PrdVersion`: immutable PRD version attached to one DiscoveryRun;
- `PrdDecision`: append-only human decision over one exact PRD version;
- `SpecDraft`: immutable canonical content with kind `initial` or `amendment`;
- `SpecDraftDecision`: append-only human decision over one exact draft;
- `InitialScopeRegistration`: one-to-one binding from Project, initial
  DiscoveryRun, and initial SpecDraft to the first SpecRegistry version;
- `WorkflowNodeAttempt`: durable start receipt with node, graph version, model,
  input fingerprint, lease, actor, and correlation data; and
- `WorkflowNodeAttemptOutcome`: one terminal success, failure, or obsolete
  result for an attempt.

`DiscoveryRun` stores identity and purpose, not a scalar workflow state. An
abandonment is a typed fact. Its position is derived from its artifacts and
decisions.

Each Project has exactly one initial DiscoveryRun. It may have many extension
runs over time, but at most one unresolved extension run. A database constraint
or serialized domain guard must enforce these cardinalities.

Every artifact relationship is Project- and DiscoveryRun-scoped. Database
constraints must prevent cross-Project PRDs, drafts, reviews, registrations, or
authority references.

### Initial And Amendment Drafts

An initial draft has no base specification. An amendment draft must pin the
accepted base spec version and hash used to create it. One schema may use a
strict kind discriminator, but initial content must never be described or
validated as an amendment.

Canonical JSON content and its hash are stored when the draft is recorded.
Human decisions bind to that content fingerprint. Later registration reads the
stored content, never a mutable file path. File paths remain provenance only.

### Initial Scope Registration

Initial Scope Registration is one-shot and transactional. It:

1. verifies the accepted initial draft and current fact fingerprint;
2. inserts the first SpecRegistry version from stored canonical content;
3. records the unique Project-to-draft-to-spec binding; and
4. exposes authority compilation.

It does not unlock backlog or execution. Only Accepted Authority over that
registered specification does so.

A uniqueness constraint prevents one initial DiscoveryRun or SpecDraft from
registering more than one Project or more than one initial spec version.

### Corrections And Supersession

Draft content is immutable. Feedback produces a new version linked to the prior
version. Review decisions are append-only and unique for the reviewed artifact
under the command idempotency contract.

Accepted facts are not edited in place. A correction creates a reviewed
replacement through the applicable authority or scope-extension graph.

## Onboarding Flows

### Greenfield

```text
Open Project Shell
-> Grill with Docs Challenge Artifact
-> PRD Draft
-> Human PRD Decision
-> Initial Spec Draft
-> Human Initial Spec Decision
-> Initial Scope Registration
-> Authority Compile
-> Human Authority Decision
-> Vision
-> Backlog
```

The first available greenfield action after shell creation is the required
Grill with Docs path. A raw spec cannot bypass discovery and review.

### Brownfield

```text
Open Project Shell
-> Repository Baseline
-> Git-Aware Inventory And Scan
-> Product Spec Curation
-> Human Initial Spec Decision
-> Initial Scope Registration
-> Authority Compile
-> Human Authority Decision
-> As-Built Assessment
-> Vision And Backlog Reconciliation
```

Repository commit, dirty state, inventory fingerprint, scan evidence, and
curation versions are Project-owned facts. The repository itself is never
treated as accepted authority without review.

### Shared Cardinality

One initial DiscoveryRun belongs to one Project and may register at most one
initial specification. A scope-extension DiscoveryRun also belongs to exactly
one existing Project. Multi-Project discovery is explicitly unsupported.

## Command Data Flow

1. A caller asks `position(project_id)`.
2. The domain opens a read transaction and loads the full typed fact snapshot.
3. It canonicalizes and hashes that snapshot with the graph version.
4. Pure graph evaluation uses an explicit clock value and returns typed node
   decisions with decision fingerprints.
5. The CLI/API/frontend renders those decisions for its own transport.
6. A caller submits one typed `TransitionRequest` with the advertised graph
   version and fingerprint.
7. `transition()` opens a write transaction and reloads the facts.
8. It rejects a graph-version, fact-fingerprint, decision-fingerprint, or
   decision-validity mismatch before side effects.
9. It reevaluates the graph and validates node availability, actor input, and
   idempotency.
10. It writes domain facts, audit metadata, and the idempotency result in one
    transaction.
11. It returns the transition result and newly derived position.

The first version intentionally fingerprints the full fact snapshot. Partitioned
or node-local fingerprints are a later optimization only if profiling proves
the full snapshot too expensive.

## ADK Execution Adapter

### One Business Graph

AgileForge has one business workflow graph: the framework-neutral domain graph.
The ADK adapter does not mirror the root Project graph or maintain a second set
of prerequisites and edges.

An available agentic domain node may reference one ADK execution recipe. That
recipe can itself use ADK sequential, parallel, loop, and join nodes to produce
the requested artifact. Its start and finish are bounded by the domain node's
typed attempt protocol.

No `BaseNode`, ADK event, runner, or session type appears in domain contracts.

### Responsibility

ADK lives under `adapters/adk/`. It may:

- map an available node to an ADK graph workflow;
- run nested sequential, parallel, loop, and join execution;
- invoke leaf agents and tools;
- collect validated structured output;
- record execution trace and token/model metadata; and
- submit a typed transition back to `WorkflowDomain`.

ADK may not:

- decide that a blocked node is available;
- mutate domain tables directly;
- store accepted business authority only in session state;
- choose the next CLI command;
- bypass a human decision; or
- treat resume position as Project workflow truth.

### Execution Protocol

Before invoking an external model or tool, the adapter obtains a durable
`WorkflowNodeAttempt` through a typed transition. The attempt pins:

- Project and node IDs;
- graph version and fact fingerprint;
- normalized input fingerprint;
- model ID and relevant execution settings;
- idempotency key, actor, and correlation ID; and
- a bounded lease.

The adapter runs ADK outside the domain transaction. It validates output at the
boundary, records an outcome, and submits the output through the node's typed
transition. A successful model response alone does not advance the graph.

If facts changed while the model ran, the output becomes an obsolete attempt
and cannot enter authority. If the process crashes, an expired attempt can be
retried. A crash after provider execution but before durable outcome recording
may repeat provider cost; the design does not claim otherwise.

### Version Boundary

The implementation will pin the exact tested ADK release rather than retain the
current broad `>=2.0.0,<3.0.0` range. The initial tested release is 2.2.0.
Upgrades require the graph adapter and resume contract suite to pass.

## CLI, API, And Frontend

### CLI

User-facing commands remain specific, such as `agileforge authority accept` or
`agileforge sprint plan`. Command handlers build typed requests and call the
domain. They do not encode prerequisites.

`agileforge workflow next --project-id ...` becomes a renderer over
`WorkflowPosition`. It advertises every available `required` or `recovery`
decision and no blocked, optional-re-entry, or already-satisfied command. A
zero-command response must be explained by waiting, invalid, or terminal node
data.

An operator may explicitly begin a new scope-extension run from a terminal
Project. The domain validates that `optional_reentry` decision even though
`workflow next` does not present it as unfinished current work.

There is no public `agileforge workflow transition --action <string>` escape
hatch.

### API And Frontend

API routes translate transport schemas to the same typed requests. The frontend
renders position categories and decisions from the API. It must not import or
duplicate a state enum.

The frontend cutover and deletion of `orchestrator_agent/fsm/states.py` happen
in the same runtime cutover. No UI build may continue to depend on old state
labels after the domain graph becomes authoritative.

### Command Registry

The command registry maps stable decision types to transport-specific command
renderers. It owns spelling, flags, and help text only. It contains no workflow
conditions.

## Failure And Recovery

### Domain Errors

- `STALE_POSITION`: graph version, fact fingerprint, decision fingerprint, or
  validity window changed. Return the newly derived position and perform no
  mutation.
- `TRANSITION_NOT_AVAILABLE`: the requested node is blocked, waiting, satisfied,
  or otherwise unavailable. Return typed blockers.
- `WORKFLOW_FACT_CONFLICT`: durable facts violate graph invariants. Block normal
  mutations and expose diagnostic or corrective transitions only.
- `ATTEMPT_OBSOLETE`: an external result targets an old fact fingerprint.
- `EXTERNAL_EXECUTION_FAILED`: the durable attempt failed without changing
  downstream domain position.

Error codes are transport-independent. CLI and API envelopes render the same
domain failure.

### Idempotency And Transactions

Every mutation request has a database-enforced idempotency key in its command
scope. Replaying the same request returns the recorded result. Reusing the key
with a different request fingerprint fails closed.

Fact writes, audit records, and idempotency completion commit atomically. An
exception rolls them all back. Query-then-write application checks are not a
substitute for uniqueness constraints.

### Human Review

A draft plus absence of a terminal decision derives a waiting review node. A
restart or deleted ADK session does not alter it. Acceptance, rejection, and
feedback are explicit durable decisions.

### Files And Projections

Database content is canonical. Markdown, JSON, packets, and managed spec files
are hashed projections. A failed export is visible and retryable. Rebuilding a
projection cannot change the accepted content it represents.

### Project Shell Cleanup

An incomplete shell may be abandoned, which hides it from normal Project lists,
or hard-deleted with all Project-owned discovery facts. Hard deletion is allowed
only before Accepted Authority. After authority acceptance, corrections use
reviewed replacement flows and existing guarded Project-deletion policy.

## Brownfield Repository Inventory

Inventory and model content selection become separate steps.

For a Git worktree, inventory uses Git's tracked and non-ignored file view, with
deterministic ordering and NUL-safe path handling. It honors repository,
exclude, and global ignore rules. Secret and oversized-file safeguards remain.

The complete inventory records path, size, content hash where safe, repository
commit, and dirty state. A later deterministic selector chooses a bounded subset
for model context based on file type, path, size, and relevance. Reaching a model
context budget does not truncate or invalidate the repository inventory.

For a non-Git directory, the fallback walker applies an explicit ignore policy
and reports that Git provenance is unavailable.

Resource bounds fail with an explicit count, byte total, and remediation. They
must not silently return the first 1,000 files. Default bounds must admit
caRtola, ASA, and MyFinance.

## Model Policy

### Production

Production roles initially resolve to:

```text
openrouter/openai/gpt-5.6-luna
```

GPT-5.6 Luna is the lowest-priced GPT-5.6 model in the cited OpenAI announcement
and supports the required text, reasoning, and structured agent workloads.

This is a default, not a claim that one model is optimal forever. Each role has
a representative evaluation set and quality threshold. A role moves to Terra
or Sol only when Luna fails that threshold and the more capable model passes.
The selected model is the cheapest model that meets the role's measured quality
requirement.

Production keeps OpenRouter privacy routing with data collection denied and ZDR
required.

### Tests

All test roles resolve to the pinned free model:

```text
openrouter/openai/gpt-oss-20b:free
```

OpenRouter documents this model as free with function calling and structured
output support. It is pinned rather than using the random `openrouter/free`
router.

`pyrepo-check --all` and the default pytest suite are offline. They mock provider
execution and must not consume model credits. The free model is a cost backstop
for construction and explicit synthetic integration tests, not permission to
make unit tests network-dependent.

Live model evaluations are separate opt-in commands. They require an explicit
budget, record token and model usage, and are not part of `pyrepo-check --all`.

## Hard Cutover And Deletion

### Fresh Schema

The cutover starts with a fresh AgileForge database. The implementation does not
write a migration that interprets old workflow sessions, FSM states, greenfield
context keys, or routing status fields.

Source repositories are not migrated. They are re-onboarded through the new
Project Shell flow.

### Delete

The final tree deletes:

- root orchestrator agent and prompt/eval assets;
- `orchestrator_agent/fsm/` controllers, definitions, states, and deterministic
  adapters;
- legacy resilience and conditional-loop wrappers superseded by the ADK 2 graph
  adapter;
- pre-Project greenfield context tables and `context_key` APIs;
- product-workflow session fields and readers used for routing;
- obsolete `WorkflowService` routing methods;
- obsolete orchestrator context/query services;
- scripts and tests whose only purpose is the deleted runtime; and
- stale active documentation and configuration references.

There is no `orchestrator_agent` compatibility package. Git history is the
archive.

Historical design records may name deleted paths when explaining the cutover.
No executable code, configuration, test, current operator guide, or API document
may import, invoke, or advertise the deleted namespace.

### Move Or Rewrite

Useful leaf behavior is retained only after classification:

- LLM/ADK agent definitions and workflows move to `adapters/adk/`;
- deterministic Pydantic contracts move to the owning domain or service module;
- host-side validation remains outside model prompts;
- mixed legacy `tools.py` modules are split by responsibility rather than moved
  intact; and
- shared model invocation, trace, and validation code becomes adapter
  infrastructure without domain routing policy.

The final architecture has one dependency direction:

```text
CLI / API / Frontend / ADK
            |
            v
      WorkflowDomain
            |
            v
 typed repositories and domain services
```

The domain never imports an adapter.

### Atomic Runtime Boundary

Development may proceed in reviewable commits on one branch, but no published
runtime uses two routing authorities. The final cutover changes all production
callers to `WorkflowDomain` before deleting the old runtime in the same branch.
Acceptance begins only after the old imports and source-of-truth fields are gone.

## Testing Strategy

### Pure Graph Tests

Table-driven tests cover every node for:

- absent prerequisites;
- satisfied prerequisites;
- pending human review;
- rejected and superseded artifacts;
- parallel branches and joins;
- contradictory facts;
- terminal Projects;
- graph-version changes; and
- full fact-fingerprint stability.

These tests use no database, ADK, network, real clock, or filesystem. An explicit
fixed evaluation time covers lease and validity rules.

### Domain Integration Tests

Fresh-database tests cover:

- Project Shell bootstrap idempotency;
- one discovery to one Project cardinality;
- cross-Project provenance rejection;
- canonical content pinning despite file drift;
- one-shot Initial Scope Registration;
- append-only review and supersession;
- concurrent idempotency and review races;
- stale advertised transitions;
- node-attempt lease expiry and retry;
- obsolete late model results;
- transaction rollback; and
- incomplete-shell deletion cascades.

### Adapter Contract Tests

CLI, API, frontend, and ADK tests use the same position fixtures. They prove:

- every available decision has the correct rendered command or action;
- blocked and satisfied nodes are never advertised;
- zero-command responses explain waiting, invalid, or terminal position;
- adapter input becomes the correct typed request;
- adapters cannot mutate repositories directly; and
- deleting sessions does not change position.

### Regression Tests

Issue #193 receives an explicit regression:

1. accept a scope-extension draft;
2. register and accept the resulting authority;
3. recompute position from the complete fact snapshot;
4. prove the completed run's scope-extension start is satisfied and absent from
   required next actions;
5. prove a previously advertised start request fails as `STALE_POSITION`.

A separate assertion proves that starting a brand-new extension remains an
explicit optional re-entry decision and cannot reuse the applied draft.

The pre-Project probe failures also become regressions: no file drift after
review, no double materialization, and no provenance loss after retry.

### Static Quality Gate

Every implementation slice and final branch must pass:

```bash
uv run --frozen pyrepo-check --all
```

Typing failures are fixed with accurate annotations, narrowing, protocols, or
validated boundary types. The implementation may not silence them with
`# type: ignore`, typing-related `# noqa`, checker exclusions, or `Any` added only
to satisfy the checker. Existing touched suppressions must be removed when the
new architecture gives the code a proper type boundary.

Ruff, annotation checks, `ty`, Bandit, and pytest must all pass.

## Acceptance Repositories

Only these repositories are architecture acceptance targets:

- caRtola;
- ASA Deep Process Control Advisory System; and
- MyFinance.

Other directories under `/projects` are not implied acceptance targets.

### caRtola

On a fresh AgileForge database:

1. open a brownfield Project Shell;
2. record repository baseline and Git-aware inventory;
3. curate and review the initial specification;
4. compile and accept authority; and
5. prove the derived next position is correct after session deletion and
   process restart.

### ASA Deep Process Control Advisory System

Repeat the brownfield flow against the current accepted ASA source scope. Prove
the inventory is not truncated by generated/cache files and the correct
authority and next action remain after session deletion.

### MyFinance

Onboard MyFinance through the same brownfield path, then take "Statement Streams
and Coverage" through:

```text
accepted authority
-> backlog
-> roadmap/story
-> sprint planning
-> task execution
-> review
-> sprint close/post-sprint triage
```

The resulting MyFinance change is verified in an isolated Compose environment
with synthetic data only. No private household records, inbox files, or live
financial database are copied into the test environment.

The acceptance record includes Project fact fingerprints, graph versions,
commands executed, authority IDs and hashes, model IDs, test results, and final
positions.

## Implementation Sequence

The later implementation plan will break the work into reviewable vertical
slices, but the architectural order is fixed:

1. introduce pure contracts, fact snapshot, graph kernel, and fingerprinting;
2. introduce Project Shell, greenfield/brownfield onboarding, and authority
   convergence on the fresh schema;
3. move vision, backlog, and planning decisions behind the domain graph;
4. move execution, post-sprint triage, and scope extension behind the graph;
5. add ADK graph execution and durable node-attempt handling;
6. switch CLI, API, frontend, and command rendering to the domain;
7. delete the old orchestrator, FSM, sessions-as-routing, and stale assets;
8. repair and validate brownfield inventory; and
9. run fresh-database acceptance on caRtola, ASA, and MyFinance.

No slice is considered complete while its production caller still consults both
the old and new routing sources.

## Risks And Mitigations

### Graph Becomes A New Monolith

Risk: one graph file accumulates every business rule.

Mitigation: retain one public deep interface but split child graph definitions
and fact selectors by domain boundary. Cross-graph joins remain explicit at the
root.

### Hidden Session Dependency Survives

Risk: an adapter still reads session JSON to decide availability.

Mitigation: dependency tests prevent the domain and command renderer from
importing session modules. Acceptance deletes all sessions between transitions.

### Generic Fact Abstraction Recreates JSON State

Risk: a generic key/value or event payload becomes a less typed session store.

Mitigation: every routing-relevant concept has a named model and repository;
graph selectors consume typed values.

### ADK Retry Duplicates External Work

Risk: resume or crash repeats a model/tool call.

Mitigation: durable attempts, leases, input fingerprints, idempotent domain
commit, and explicit at-least-once documentation. Side-effecting tools require
their own idempotency contract.

### Luna Misses A Quality Threshold

Risk: the cheapest GPT-5.6 model is inadequate for one production role.

Mitigation: role-specific evals and cheapest-passing-model selection. Promote
only the failing role, not every agent.

### Hard Break Leaves No Usable Runtime Mid-Branch

Risk: intermediate commits cannot run complete workflows.

Mitigation: keep work isolated on the implementation branch, use vertical test
fixtures, and do not publish the cutover until all production callers and the
three acceptance paths are complete.

## Acceptance Criteria

The design is implemented only when all statements below are true:

- `WorkflowDomain.position()` is the only routing query.
- `WorkflowDomain.transition()` is the only workflow mutation entry point.
- Position is reproducible from a fresh process and typed durable facts.
- Deleting ADK and workflow sessions does not change position.
- Stale commands fail before mutation with the new position returned.
- Human-review stops survive restart without a live ADK invocation.
- Initial and amendment drafts have distinct validated semantics.
- Accepted content is loaded from canonical stored content, not mutable files.
- One discovery cannot create or register multiple Projects.
- Brownfield inventory honors Git ignores and does not silently truncate at
  1,000 files.
- CLI, API, frontend, and ADK contain no routing policy.
- Executable code, configuration, tests, current operator guides, and API docs
  contain no `orchestrator_agent` namespace, old FSM, or session-derived routing
  source.
- Production and test model policies match this design.
- Default tests make no provider calls.
- No new typing suppression is used to pass the quality gate.
- `uv run --frozen pyrepo-check --all` passes.
- caRtola and ASA pass fresh brownfield onboarding and restart routing proof.
- MyFinance passes onboarding and the real "Statement Streams and Coverage"
  execution proof using synthetic data.

## Sources

- Google ADK graph workflows: <https://adk.dev/graphs/>
- Google ADK resume behavior: <https://adk.dev/runtime/resume/>
- OpenAI GPT-5.6 price/performance announcement:
  <https://openai.com/index/advancing-the-price-performance-frontier-with-gpt-5-6/>
- OpenAI GPT-5.6 Luna model reference:
  <https://developers.openai.com/api/docs/models/gpt-5.6-luna>
- OpenRouter GPT-5.6 Luna route:
  <https://openrouter.ai/openai/gpt-5.6-luna-20260709>
- OpenRouter gpt-oss-20b free route:
  <https://openrouter.ai/openai/gpt-oss-20b%3Afree>
