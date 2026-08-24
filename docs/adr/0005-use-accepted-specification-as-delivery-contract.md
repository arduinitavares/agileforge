# Use the accepted Specification as the delivery contract

**Status:** Accepted on 2026-08-21

**Date:** 2026-08-21

**Supersedes:** The separate Authority compilation and review decisions in
[ADR 0004](0004-register-to-spec-source-before-structuring.md). Source
registration and canonical Specification structuring remain unchanged.

## Context

The accepted `agileforge.spec.v2` payload already contains stable identities,
exact normative statements, levels, verification methods, ordered acceptance
criteria, relations, controlled terms, and source lineage. Authority compilation
asked a provider to reinterpret those reviewed bytes into a second artifact and
then required a second human review before delivery.

The compiled invariant algebra does not faithfully represent common callable,
grammar, CLI, observable-output, or diagnostic behavior. Most downstream code
uses Authority only as duplicated text or as a lineage gate. The String
Calculator audit showed that this extra transformation can block delivery even
when the accepted Specification is complete.

There is no installed-data or compatibility requirement for this hard break.

## Decision

The exact human-accepted Specification Version is the sole product-definition
contract for delivery. Accepting a Specification makes Backlog generation
available immediately. It does not automatically invoke a provider.

AgileForge has no compiled Authority artifact, Authority compiler, Authority
repair, Authority review, or Authority workflow phase. Delivery lineage binds
the Product Goal and exact `spec_version_id` plus `spec_hash`. Backlog, Roadmap,
Story, validation, and execution packets consume the canonical accepted
Specification directly.

An accepted Specification registry row is relationally bound to the exact
`SpecificationDecision(decision="accepted")`, candidate identity, and candidate
fingerprint. At most one accepted version is current per Project. A later
accepted amendment supersedes the prior version for new planning without
rewriting its bytes, decision, or historical descendants.

Stable Specification item IDs provide evidence through Backlog, Story, Task,
semantic findings, and packets. The host validates structure, decision,
identity, ownership, exact bytes, references, parent boundaries, and freshness.
It does not infer semantic relevance or machine checks from prose. Mandatory
human artifact reviews judge meaning.

Draft planning artifacts are immutable candidates. In particular, a Story draft
does not create or mutate operational Stories. Accepting its exact artifact
atomically creates new Story rows; feedback or rejection leaves the prior
accepted rows untouched. Accepted replacement is resolved through transitive
artifact ancestry, including feedback or rejected intermediate drafts.

The same boundary applies to Sprint planning. A draft persists only the
immutable plan artifact; accepting it atomically creates or replaces the
unstarted Sprint membership and Tasks. Feedback or rejection cannot change a
previously accepted, startable plan.

Artifact history is lineage-scoped: Backlog by exact Goal and Specification,
Roadmap by exact Backlog, Story by exact Backlog item, and Sprint plan by exact
Specification plus a host-minted planning-stream identity. Versioning,
fingerprint uniqueness, and supersession use those same keys.

New planning must use the current accepted Specification. A planned Sprint
cannot start after its Specification is superseded. An already active Sprint
may finish only its existing Tasks against its pinned historical version;
packets expose that version as superseded and never silently rebind it.

New deterministic enforcement metadata may be added to the Specification only
when a concrete host consumer exists.

## Alternatives considered

### Keep provider-compiled typed Authority

Rejected. It duplicates reviewed semantics, introduces domain vocabulary
failures, and has little proven downstream enforcement value.

### Persist a deterministic Authority snapshot

Rejected for the hard-break end state. It removes provider risk but still
duplicates the accepted Specification and a second approval over unchanged
meaning.

### Add a separate delivery-activation decision

Rejected. Specification acceptance already targets exact immutable bytes.
Backlog generation remains a separate explicit action, so another semantic
approval adds no evidence or safety.

### Infer enforceable invariant types from Specification prose

Rejected. Moving the same interpretation into host code would preserve the
cue-word defect under a different name. A future executable consumer must drive
the smallest explicit, human-reviewed Specification field it needs.

## Consequences

- Specification acceptance is the only human semantic approval before Backlog.
- Authority-specific behavior from issues #205, #207, #208, and #209 becomes
  obsolete rather than carried forward as dead compatibility machinery. Their
  general fail-closed provider-boundary principles remain where providers still
  exist.
- The lexical `FORBIDDEN_CAPABILITY` and `REQUIRED_FIELD` Story checks are
  removed. Optional semantic Story review must cite valid Specification item
  IDs, and unbound output fails closed.
- Backlog, Story, Sprint, Task, and packet contracts use exact Specification
  identity and item references instead of compiled invariant identities.
- Story history is keyed by exact Backlog artifact and item identity, not by
  normalized requirement prose; unaccepted Story drafts cannot alter selectable
  planning state.
- Sprint-plan drafts do not create operational Tasks; only the exact accepted
  plan can materialize them.
- Superseded accepted versions remain readable for audit and already active
  work, but cannot seed new planning.
- Existing development profiles are intentionally unsupported by the hard
  break. Fresh databases use only the direct-Specification schema, with no
  migration, compatibility reader, or dual-write path; old schemas fail at
  startup with the existing `UNSUPPORTED_BUSINESS_SCHEMA` contract.
