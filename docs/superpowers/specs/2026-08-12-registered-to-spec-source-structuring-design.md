# Registered To-Spec Source And Specification Structuring

**Status:** Approved

**Date:** 2026-08-12

**Supersedes:** the direct-authoring boundary in
[`2026-08-11-agileforge-spec-profile-v2.md`](2026-08-11-agileforge-spec-profile-v2.md)
and [ADR 0003](../../adr/0003-make-to-spec-the-canonical-specification-boundary.md)

## Decision

AgileForge no longer asks its internal model to author a Specification directly
from repository context. An external agent prepares the source material, runs
`grill-with-docs`, creates ADRs only when warranted, runs `to-spec`, and writes
one human-readable source Specification. AgileForge registers the exact source
bytes and then invokes an AgileForge-owned Specification Structuring Agent.

The lifecycle is:

```text
accepted Vision + active Product Goal
-> register exact to-spec source and supporting captures
-> structure source into agileforge.spec.v2
-> review the exact immutable candidate
-> compile Authority from the accepted typed payload only
```

The `specification` child graph has three public nodes:

- `specification.source.register`, a synchronous host capture;
- `specification.structure`, an agentic structuring step; and
- `specification.review`, the existing exact human decision.

There is no Discovery artifact, review, node, route, command, card, or gate.
Discovery is an activity performed with `grill-with-docs` and may influence the
registered source. The `preparation_capability="grill-with-docs"` field is an
attestation, not proof of an external agent's internal reasoning.

## Immutable source registration

`SpecificationSource` is an immutable host-owned record. It stores a closed,
canonical bundle with:

- exactly one source document produced by capability `to-spec`;
- the exact root `CONTEXT.md` bytes when present, or an explicit absent state;
- zero or more explicitly selected applicable ADRs in canonical path order;
- each document's stable role ID, repository-relative path, raw bytes encoded
  as base64, byte length, and SHA-256 fingerprint;
- the accepted Vision and active Product Goal fingerprints;
- the active repository binding and semantic revision (`head_sha`, dirty state,
  and status fingerprint);
- producer capability `to-spec` and preparation capability
  `grill-with-docs`; and
- optional immutable supersession lineage.

The portable source fingerprint commits to semantic revision, Vision/Goal
fingerprints, capabilities, context state, and document bytes. It excludes
database IDs, actor, and timestamps. The host rejects absolute paths, traversal,
aliases, backslashes, symlinks, non-regular files, invalid UTF-8, oversize
documents, duplicate paths, and capture races. It never strips, truncates,
normalizes newlines, parses, or reserializes registered source bytes.

`CONTEXT.md` is optional because `domain-modeling` creates it lazily. Presence
and absence are distinct canonical states. When present, its exact bytes are
part of both the source fingerprint and the structuring input.

Source registration is a hard gate for structuring. The current source is the
sole unsuperseded source whose Vision, Goal, and repository binding match the
current workflow facts. A replacement source appends a successor; immutable
rows are never rewritten. An accepted Specification remains current until a
replacement candidate is accepted.

## Structuring boundary

The host-built `SpecificationStructuringInput` contains only:

- the accepted Vision;
- the active Product Goal;
- the exact registered to-spec source;
- the captured `CONTEXT.md` when present;
- the captured applicable ADRs;
- the registered repository evidence and revision;
- the pinned accepted base Specification for amendments; and
- prior human feedback for revisions.

No fresh unregistered repository prose or historical Vision evidence is added
behind the registration boundary.

The provider-facing output is the existing semantic shape under its correct
name, `SpecificationStructuringOutput`. It is frozen and closed, requires
`payload`, and exposes the full nested `agileforge.spec.v2` schema to ADK. The
model owns only the semantic payload, removal justifications, and stable-ID
replacements. The host owns database identity, canonical ordering, lineage,
hashes, timestamps, attempt state, persistence, and rendering.

The candidate stores an exact composite reference to its source registration.
Its portable identity commits to the source fingerprint, not the source row ID.
The complete API, CLI, and dashboard review packet remains the exact candidate
target.

## Drift and transactions

The host revalidates Vision, Goal, repository binding/revision, source bytes,
Context state/bytes, ADR paths/bytes, and the canonical registration at input
construction, immediately before every provider invocation, immediately after
every provider invocation, immediately before candidate persistence, and
inside accepted-decision persistence. Drift returns
`STALE_SPECIFICATION_INPUT`, obsoletes the attempt where applicable, and creates
or accepts no candidate. Rejection and feedback remain available for an exact
candidate even if its source later drifts.

## Authority boundary

Authority loads only the accepted canonical `SpecificationPayload`. The
provider-facing Authority input contains no registered source Markdown,
`CONTEXT.md`, ADR prose, repository evidence prose, or source provenance prose.
Those bytes remain reviewable candidate provenance but cannot become Authority
input.

## Hard break

There is no compatibility path or data migration. Existing databases without
the source registration schema fail at startup with the stable unsupported
business-schema diagnostic. Fresh profiles are required.
