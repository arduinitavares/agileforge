# AgileForge Domain Language

## Project

The single durable aggregate that owns one product's Vision, Product Goals,
Specification, Authority, planning, and execution history.

Avoid: workflow session, graph cursor, repository row.

## Project Vision

The accepted enduring direction for a Project. A Vision may guide multiple
Product Goals and can be revised through an explicit reviewed revision.

Avoid: Product Goal, specification, feature request.

## Product Goal

One accepted valuable future state pursued under the current Vision. A Project
has at most one active Product Goal. The goal ends through an explicit fulfilled
or abandoned outcome.

Avoid: Sprint Goal, Backlog item, automatic completion.

## Discovery

Optional investigation performed through `grill-with-docs`, repository work,
research, interviews, or prototypes before an external agent runs `to-spec`.
Discovery is an activity. It is not a persisted artifact, review gate, or
lifecycle node.

Avoid: Discovery Artifact, mandatory phase, accepted lifecycle record.

## Specification Source

An immutable host capture of one external `to-spec` document, the deterministic
present-or-absent `CONTEXT.md` state, applicable ADRs, repository revision, and
exact accepted Vision/Product Goal lineage. It gates Specification structuring
but is not itself human-reviewed.

Avoid: canonical Specification, Discovery Artifact, mutable file reference.

## Specification Candidate

An immutable canonical `agileforge.spec.v2` proposal produced by the internal
Specification Structuring Agent from one exact Specification Source. Human
acceptance creates the reviewed Specification lineage used for Authority
compilation.

Avoid: accepted authority, direct Backlog input.

## Accepted Authority

Reviewed compiled specification authority that may govern Backlog, Roadmap,
Story, Sprint, and execution work.

Avoid: pending compiler output, unreviewed suggestion.

## Backlog

The accepted ranked requirements for the active Product Goal and current
Authority.

Avoid: User Story set, repository inventory, implementation status report.

## Roadmap

A reviewed planning artifact derived from accepted Backlog lineage.

Avoid: Sprint plan, release promise, persisted graph position.

## Story

A reviewed vertical slice derived from accepted Roadmap and Backlog facts.
Readiness and dependency decisions are durable facts.

Avoid: Backlog requirement, Task.

## Sprint Plan

A reviewed candidate set and Task plan bound to exact Story, dependency,
capacity, and Authority facts.

Avoid: active Sprint, ad hoc Task list.

## Workflow Fact

A typed durable record whose current value can affect graph decisions.

Avoid: transient routing flag, provider trace, cached phase state.

## Workflow Position

The available, waiting, blocked, and invalid decisions derived from the current
Workflow Facts. Position is recalculated and is not stored as independent
authority.

Avoid: persisted graph cursor, transport-owned phase.

## Node Attempt

A durable lease and normalized input identity for one model-backed graph node.
Its outcome records success, failure, or obsolescence without becoming routing
authority.

Avoid: Workflow Position, business artifact.

## Repository Binding

The operator-selected repository identity attached to a Project. Deterministic
repository observations may inform Project work without creating a second
lifecycle.

Avoid: Project identity, automatic source ownership.

## Idempotency Receipt

A durable record that replays one exact request kind, key, and request payload.
Changing request semantics requires a new key.

Avoid: stale-guard bypass, retry of a different request.
