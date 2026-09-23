# Graph data model research for AgileForge

Research baseline: 2026-08-29, branch `dev/issue-218-progressive-story-readiness`, commit `42a744946b8bc993c7525556ebd0b8347d78acb9`. This document consolidates the architectural findings from that investigation. Repository behavior and vendor status have not been rechecked for this edition.

## Recommendation

Retain SQLite/SQLModel as the durable store and introduce a focused graph domain module where shared rules justify it. Combine that module with explicit relational relationships and transactional enforcement. This is the hybrid option, built on relational hardening.

The strongest immediate need is a consistent Story dependency lifecycle across supersession, review, readiness, and Sprint selection. A graph database would make relationship queries more convenient, but the application would still need to define and enforce those lifecycle rules.

Task concurrency and requirement-to-code impact analysis are separate capabilities. They can use the same durable facts and graph algorithms, but neither should be treated as an automatic consequence of replacing the database.

## Context

AgileForge coordinates Product Owner, Scrum Master, and Developer agents through reviewed workflow artifacts. Its delivery chain includes AcceptedSpecification, BacklogArtifact, RoadmapArtifact, StoryArtifact, operational UserStory rows, and Tasks.

The original evaluation prioritized local operation, no external database daemon, simple installation, deterministic review gates, and preserved historical lineage. Those constraints favor an embedded durable store and explicit application rules.

## Findings from the four problem scenarios

### 1. Story supersession can leave obsolete dependency endpoints

Suppose Story B depends on Story A. A reviewed correction replaces A with C. In the inspected code, `_materialize_story_rows` marked prior Story rows superseded and created replacements without reconciling incident dependency rows.

Readiness still consumed active dependency rows and checked the prerequisite's completion status. Other projections excluded superseded Stories or limited prerequisite IDs to current candidates. An obsolete prerequisite could therefore remain a blocker while becoming less visible elsewhere.

This is a lifecycle consistency problem. A foreign key proves that A exists; it does not prove that A remains a valid prerequisite. Similarly, a property-graph relationship to a superseded node remains present unless an application rule or supported database mechanism explicitly changes its executable meaning.

The recommended policy is to preserve historical evidence, make the effect of replacement explicit, and require renewed dependency review when meaning changes. Blindly rewriting B→A into B→C can transfer human approval to work the reviewer never evaluated. Splits, merges, and one-to-many replacements make this particularly unsafe.

Replacement handling must also distinguish new planning from active work pinned to historical accepted facts. Supersession must not silently rewrite that historical execution contract. Simply dropping an edge from the active projection is insufficient if it makes B appear ready without a review-required blocker.

Relevant implementation locations at the baseline were `services/agent_workbench/story_phase.py`, `services/story_dependencies.py`, `repositories/workflow.py`, `workflow/definitions/planning.py`, and `services/application.py`.

### 2. Task metadata does not provide concurrency control

The inspected Task model stored a parent Story and canonical metadata JSON. The metadata was typed and validated, including Specification evidence, Sprint-plan identity, artifact targets, workstream tags, and checklist items. The issue was not unvalidated JSON. It was the absence of explicit Task-to-Task dependency and artifact-claim facts in the inspected paths.

Task dependency satisfaction was derived from parent Story blockers. The execution rule exposed one eligible Task, preferring in-progress work and otherwise selecting by Task ID. That recommendation rule does not establish exclusive execution across independent workers or processes.

Safe parallel execution needs two distinct mechanisms:

- A dependency graph determines which Tasks may proceed after their prerequisites satisfy the domain's completion rules.
- Durable resource claims determine which workers may operate on overlapping artifacts at the same time.

Two otherwise independent Tasks that edit the same file have a resource conflict. That conflict does not necessarily justify a permanent dependency edge between them.

A scheduling snapshot can propose work, but a database transaction must arbitrate claims and revalidate relevant state. Completion needs to reject obsolete or unauthorized attempts. Lease expiration alone cannot stop a paused worker from later writing a file, so filesystem execution must also be controlled, for example through isolated workspaces and validated integration.

Artifact identity requires a policy for repository identity, path normalization, directories or patterns, undeclared writes, and shared generated files. A graph engine cannot infer these rules from an `artifact_targets` string.

Relevant locations were `models/core.py`, `utils/task_metadata.py`, `services/contracts/sprint.py`, `services/agent_workbench/sprint_phase.py`, `repositories/workflow.py`, `workflow/definitions/execution.py`, and `services/task_execution_service.py`.

### 3. Much of the Delivery Lineage already exists

The code already contained exact artifact ancestry, accepted Specification identity, fingerprints, and Task metadata tied to reviewed Sprint plans. The investigation therefore found repeated lineage assembly and JSON parsing, rather than a complete absence of traceability.

Two existing modules were promising places to consolidate that behavior:

- `services/planning_lineage.py` validated ancestry, versions, branches, and accepted-leaf selection.
- `workflow/execution_integrity.py` validated the relationship among Sprint Start, the accepted plan, dependency review, Stories, and Tasks.

Callers still repeated node construction, decision translation, chain selection, and exact identity comparisons. Deepening those modules could reduce repetition before introducing another graph projection.

A derived impact view could answer which Stories and Tasks cite a Specification item and which artifact targets they declare. It must distinguish declared targets from observed implementation evidence. A path named in a plan does not prove that a particular code revision implements the requirement. Revision-bound change evidence and human review remain necessary for stronger claims.

The baseline's ADR 0005 required accepted product meaning and historical delivery lineage to remain immutable. A derived graph should be rebuildable from durable facts and should not become another source of approval or currentness.

### 4. Graph traversal logic is duplicated

The investigation found cycle detection or graph construction in Story dependency writes, planning integrity, planning rules, execution rules, and Sprint selection. Different callers returned cycle paths, booleans, reason codes, or priority-aware topological order.

A shared implementation could provide deterministic normalization, reachability, cycle evidence, and ordering. Domain policy still needs to specify which graph is being evaluated: selected planning scope, current dependencies, or a pinned historical execution graph.

Consolidation is worthwhile when it removes repeated caller obligations. A generic wrapper that leaves every caller constructing its own semantics offers little improvement. A small implementation or standard-library facility may suffice; adding NetworkX needs a concrete algorithmic benefit.

## Architecture options

| Option | Main advantages | Main costs and risks | Fit for the investigated needs |
| --- | --- | --- | --- |
| Embedded graph database | Natural relationship queries and traversals; potentially useful for broad graph analytics; no mandatory external daemon for genuinely embedded engines | Native packaging, another query language and persistence model, migration and recovery tooling, engine-specific process constraints; pairing with SQLite creates cross-store consistency work | Unproven benefit for the immediate defects; requires a maintained engine and a measured workload that justifies the switch |
| SQLite plus an in-memory graph projection | Existing durable transactions and deployment retained; shared pure graph logic is easy to exercise; graph can be rebuilt from facts | Snapshot staleness, projection correctness, loading cost, and transaction revalidation remain application responsibilities | Best overall fit for shared dependency and lineage behavior |
| Relational hardening | Smallest immediate change; explicit edge tables and constraints; one durable authority | Traversal and lifecycle policy can remain scattered if only schema patches are made; complex recursive queries may become hard to maintain | Strong foundation and a reasonable stopping point if focused domain functions meet the need |

These options overlap. A relational edge table is already a way to persist a graph. The important distinction is whether graph behavior has coherent ownership, not whether storage is marketed as a graph database.

### Embedded engine considerations

Kùzu and Cozo should not be treated as equivalent implementations. The prior research identified Kùzu as an embedded property-graph engine using Cypher, while Cozo represented graph structures through relations and a Datalog-based query language. Their query models, Python distribution, and concurrency restrictions require separate evaluation. Sources: [Kùzu repository](https://github.com/kuzudb/kuzu), [Cozo repository](https://github.com/cozodb/cozo), and [Cozo Python client](https://github.com/cozodb/pycozo).

The August research reported that Kùzu's repository was archived and that Cozo had an old release cadence with pre-1.0 compatibility caveats. Those were material maintenance objections to selecting either as a new durable default. These vendor findings are historical and require a fresh check before a procurement or implementation decision.

For a Python/uv project, the hidden costs include:

- Native wheel coverage for every supported Python version, architecture, and operating system. A lockfile cannot create a missing wheel or guarantee a successful native build.
- Engine-specific restrictions on simultaneous database handles, threads, and processes. Embedded does not imply that separate CLI and worker processes can all open the same database for writing.
- Schema and storage-format upgrades, export/import validation, backups, corruption recovery, and rollback procedures.
- Rebuilding relational identity, uniqueness, ownership, and acceptance invariants in a different persistence model.
- Contributor knowledge of the new query language, additional fixtures, and equivalent tests for failures and historical data.
- Dual-write failures if relational facts and graph state are both authoritative. A derived graph avoids that split but then needs rebuild and freshness guarantees.

The investigated branch used a strict business-schema manifest and rejected incompatible existing profiles. That lowers some installed-data migration obligations for a deliberate hard break, but it does not remove schema validation, export, audit, and recovery responsibilities. The applicable policy must be checked for any later implementation.

### External graph database

Neo4j may be justified if AgileForge evolves into a shared service with many remote clients, central graph queries, dedicated operations, or graph workloads that dominate the product. Those requirements were not established by the four scenarios.

The prior research found that Python access uses a running Neo4j database. This adds database startup, credentials, network connectivity, backup, and upgrade responsibilities. Packaging the server alongside the application can simplify installation, but those operational requirements still exist. Source: [Neo4j Python installation documentation](https://neo4j.com/docs/python-manual/current/install/).

For the local CLI requirements evaluated here, an external graph database is difficult to justify as a mandatory dependency. An optional analytical integration is a separate decision.

## Shape of the hybrid approach

The durable store retains reviewed facts, artifact versions, dependency rows, and any future execution claims. A typed graph projection derives decisions from a consistent snapshot.

The graph module's responsibilities should be narrow enough to name clearly:

- Validate graph scope, endpoint validity, and cycles with actionable diagnostics.
- Calculate prerequisite closure, deterministic order, and readiness explanations.
- Determine which dependency decisions are affected by a proposed Story replacement without silently inheriting approval.
- Support lineage impact queries over explicit evidence relationships.

An application transaction applies accepted changes and verifies that the facts used for the decision remain valid. If concurrent Task execution is introduced, a separate durable coordination mechanism arbitrates claims and attempts. A successful in-memory readiness calculation is not itself permission to mutate files.

Workflow control flow, Story dependencies, artifact lineage, and file conflicts should not be flattened into one undifferentiated DAG. Their edge meanings, direction, lifecycle, and validity rules differ.

## Recommended progression and unresolved evidence

First, establish and test the supersession policy across Story acceptance, dependency review, readiness, planning, and historical active work. Then consolidate duplicated graph operations where doing so reduces caller complexity. Reuse the existing lineage modules for shared proof and impact reads.

Task dependency and claim modeling should follow a concrete decision about supported parallel execution. A graph-database experiment should follow measured evidence that query complexity, graph size, latency, or analytical requirements exceed the simpler design.

No comparative benchmark or end-to-end concurrency experiment was performed in the original investigation. The recommendation rests on the inspected correctness gaps, existing architecture, and deployment constraints. Evidence that could change it includes sustained traversal workloads too expensive to project in memory, central multi-user requirements, or a maintained embedded engine that materially reduces total implementation and operational complexity.
