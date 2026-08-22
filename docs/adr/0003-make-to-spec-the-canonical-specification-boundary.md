# Make to-spec the canonical Specification boundary

**Status:** Superseded by
[ADR 0005](0005-use-accepted-specification-as-delivery-contract.md)

**Date:** 2026-08-11

**Supersedes:**
[ADR 0002](0002-store-discovery-artifacts-in-agileforge-state.md)

## Context

The former lifecycle persisted a Discovery artifact between an accepted Product
Goal and a Specification candidate. Both stages accepted caller-authored JSON,
which split semantic authoring across two weak contracts. The Discovery artifact
also became a required foreign-key hop, graph gate, transport surface, and
dashboard state.

The product workflow still needs interviews, `grill-with-docs`, research,
repository evidence, ADRs, and prototypes. Those activities create source
material and provenance. They do not require another accepted semantic artifact
before Specification review.

Authority needs one deterministic typed input. Humans need to review the exact
semantic payload plus all lineage and producer evidence that can affect that
input. Markdown, files, and GitHub issues cannot provide that identity because
they are mutable projections.

## Decision

An accepted Product Goal enables host-owned `specification.author` directly.
The host captures the accepted Vision and Product Goal, source manifest,
accepted-fact and producer-input fingerprints, producer, model, and prompt
versions, workflow attempt identity, and an exact accepted base for amendments.
The human does not submit raw JSON, a file, Markdown, IDs, hashes, fingerprints,
or lineage fields.

The configured `to-spec` producer returns one typed `agileforge.spec.v2`
semantic payload. Lifecycle state and host metadata remain outside those bytes.
AgileForge validates the payload and stores it with an immutable candidate
envelope. The envelope binds direct Vision and Product Goal lineage, source and
producer evidence, attempt identity, amendment base and deterministic diff,
payload fingerprint, rendered-review fingerprint, and candidate fingerprint.

AgileForge renders complete deterministic Markdown for human review. The human
decision targets the exact candidate fingerprint and does not rewrite payload
or envelope bytes. Acceptance creates a `SpecRegistry` row that references the
accepted candidate and payload fingerprint without copying semantic content.

Authority compilation consumes only the accepted typed v2 payload. It does not
consume Markdown, files, GitHub issues, arbitrary text, or provenance prose as
normative input. A separate human Authority review and acceptance gate remains
mandatory before Backlog work.

Discovery remains an activity and source of provenance. AgileForge has no
Discovery model, artifact, foreign key, graph node, request, API route, CLI
command, read projection, dashboard state, or acceptance gate.

This change is a hard break. AgileForge does not migrate or dual-read the former
Discovery and Specification schemas or accept `agileforge.spec.v1` at active
runtime entrypoints. Development and acceptance work use a fresh profile and
database initialized by the reviewed checkout.

## Consequences

- `to-spec` is the only semantic Specification authoring boundary.
- One complete review packet serves API, CLI, and dashboard readers.
- Initial candidates have no base. Amendments pin one accepted base and expose
  a deterministic diff with justified removals and stable-ID replacements.
- Candidate decisions, registry acceptance, and Authority compilation share one
  payload and candidate identity.
- Optional research and evidence work can evolve without adding lifecycle
  gates.
- Old profiles cannot be reused after the schema cutover.

## Alternatives Considered

### Keep Discovery as a mandatory artifact

Rejected. It preserves two semantic authoring steps, caller-authored JSON, and a
foreign-key gate that operators cannot distinguish from `to-spec`.

### Treat Markdown or a file as canonical

Rejected. Mutable prose cannot provide typed validation, deterministic
canonicalization, stable item identities, exact candidate acceptance, or safe
Authority filtering.

### Add a compatibility or migration layer

Rejected. Dual-read behavior would leave two authorities for Specification
semantics and weaken the schema/version hard break.
