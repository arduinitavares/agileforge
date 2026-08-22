# Register the to-spec source before Specification structuring

**Status:** Superseded by
[ADR 0005](0005-use-accepted-specification-as-delivery-contract.md)

**Date:** 2026-08-12

**Supersedes:**
[ADR 0003](0003-make-to-spec-the-canonical-specification-boundary.md)

## Context

ADR 0003 correctly removed persisted Discovery and established the typed
`agileforge.spec.v2` candidate and Authority boundary. It incorrectly assigned
the external `to-spec` authoring responsibility to AgileForge's internal agent.
That collapsed source preparation, source provenance, and canonical structuring
into one opaque provider call.

The external workflow already owns `grill-with-docs`, lazy domain modeling in
`CONTEXT.md`, warranted ADR creation, and `to-spec`. AgileForge must capture the
result of that work exactly before it structures canonical product authority.

## Decision

An accepted Vision and active Product Goal enable immutable Specification Source
registration, not direct model authoring. The operator selects one external
`to-spec` source and applicable ADR paths. AgileForge captures exact bytes,
captures root `CONTEXT.md` as deterministically present or absent, pins the
repository revision and exact Vision/Goal fingerprints, and records producer
capability `to-spec` plus preparation attestation `grill-with-docs`.

The internal model is a Specification Structuring Agent. It receives the exact
registered bundle, repository evidence/revision, pinned base Specification, and
prior human feedback. Its closed output contains only the semantic
`agileforge.spec.v2` payload and amendment declarations. AgileForge owns all
identity, ordering, lineage, hashes, timestamps, persistence, and rendering.

Source registration is required before structuring but has no human review. It
does not recreate Discovery persistence: there is no Discovery artifact, node,
route, command, card, or acceptance gate.

Authority continues to consume only the accepted typed Specification. It never
reads the registered Markdown, `CONTEXT.md`, ADR prose, or repository prose.

This is a hard break with no migration or compatibility path.

## Consequences

- The external source remains human-readable and byte-exact.
- Optional Context absence is explicit and reproducible.
- Source, Context, ADR, repository, Vision, or Goal drift can obsolete a
  structuring attempt and block stale acceptance.
- ADK exposes the real closed nested v2 output schema to the provider.
- Fresh databases are required for the new source lineage.

## Alternatives considered

### Keep direct internal authoring

Rejected. It cannot prove or preserve the external `to-spec` source boundary.

### Parse Markdown directly into Authority

Rejected. Authority must remain derived only from accepted typed clauses.

### Reintroduce Discovery review

Rejected. Source provenance is not a second semantic acceptance decision.
