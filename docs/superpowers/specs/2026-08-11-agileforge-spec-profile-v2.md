# AgileForge Specification Profile v2

**Status:** Authoritative

**Schema:** `agileforge.spec.v2`

**Renderer:** `agileforge.spec_review.v2`

This profile is the sole active semantic Specification contract. It is a hard
break from `agileforge.spec.v1`: active runtime entrypoints reject v1, prose,
Markdown, files, and partially shaped JSON. There is no migration, fallback,
dual-read, or compatibility alias.

## Boundary

An accepted Vision and active accepted Product Goal enable
`specification.source.register`. An external agent performs any useful
Discovery through `grill-with-docs`, resolves terminology in `CONTEXT.md` when
needed, creates warranted ADRs, runs `to-spec`, and writes one human-readable
source Specification. AgileForge captures those exact bytes before exposing
`specification.structure`.

The internal Specification Structuring Agent receives the registered source,
optional captured Context, applicable ADRs, repository evidence/revision,
accepted Vision and Goal, a pinned base for amendments, and prior human
feedback for revisions. Humans provide normal-language review decisions and
feedback. They never author payload JSON, IDs, hashes, fingerprints, lineage,
source manifests, or attempt metadata.

The structuring provider returns only:

- one complete `SpecificationPayload`;
- removal justifications for an amendment; and
- explicit stable-ID replacements for an amendment.

Discovery remains an optional `grill-with-docs` activity. The registered
Specification Source is immutable provenance, not a Discovery artifact or
review gate. Preparation capability is an attestation; AgileForge does not
claim to prove the external agent's internal reasoning.

## Canonical semantic payload

`SpecificationPayload` is frozen and rejects unknown fields. It contains:

- `schema_version`, `artifact_id`, `title`, `summary`, and
  `problem_statement`;
- typed `items` with stable IDs, requirement levels, verification methods,
  acceptance criteria, tags, and bounded source notes;
- typed `relations` between known item IDs;
- controlled terms; and
- external references used by source notes.

Stable item ID prefixes must match the item type. Normative `REQ`, `QUALITY`,
`CONSTRAINT`, `INTERFACE`, and `DATA` items require a level, verification
method, and at least one acceptance criterion. Duplicate IDs, normalized tags,
terms, relations, references, and dangling relation/reference endpoints are
invalid.

Canonical JSON uses UTF-8, sorted object keys, compact separators, and a
deterministic order for set-like collections. Declared ordered prose, acceptance
criteria, and source notes retain their supplied order and bytes. The payload
fingerprint is SHA-256 over those canonical semantic bytes.

## Host-owned candidate envelope

Lifecycle and execution evidence never enters the semantic payload. The
immutable `agileforge.spec-candidate-envelope.v2` envelope binds the exact
payload to:

- accepted Vision and Product Goal identities and fingerprints;
- exact registered source fingerprint plus `to-spec` producer and
  `grill-with-docs` preparation capabilities;
- the complete source manifest and accepted-fact fingerprint;
- structurer input, capability, version, model configuration, prompt version,
  and prompt fingerprint;
- workflow attempt, correlation, and production time;
- canonical payload and deterministic review-view fingerprints; and
- the resulting candidate fingerprint.

Execution metadata remains visible and immutable in the persisted envelope and
review view. Host database IDs, attempt and correlation identifiers, record
timestamps, and the derived review-view fingerprint are excluded from the
cross-run candidate identity projection. Equivalent payload, lineage, source,
producer, model, prompt, and amendment evidence therefore has one candidate
identity across executions.

Production source IDs are stable semantic roles rather than database-row IDs:
`SRC.vision.accepted`, `SRC.product-goal.active`,
`SRC.specification-source.primary`, `SRC.specification-source.context`, and
path-derived `SRC.specification-source.adr.*` IDs. Accepted-fact and
structurer-input fingerprints
use portable semantic projections that exclude project, artifact, attempt, and
version row IDs while retaining the exact accepted content, source, base,
producer, model, prompt, and amendment evidence.

An initial candidate has no base. An amendment pins one accepted Specification
base and includes deterministic added, changed, and removed keys across items,
relations, controlled terms, and external references. Every removal requires a
justification. Replacing a stable item ID requires an explicit old-to-new
mapping and justification.

The source registration selects one canonical repository-relative `to-spec`
path and zero or more applicable `docs/adr/*.md` paths. Root `CONTEXT.md` is
captured automatically as exactly present or absent; it is never mandatory.
The host preserves exact UTF-8 bytes, including BOM, newline spelling, trailing
whitespace, and trailing newline. It rejects traversal, aliases, symlinks,
non-regular files, invalid UTF-8, oversize bundles, and capture races instead of
normalizing or truncating input.

Vision, Goal, repository revision, source, Context, and ADR fingerprints are
revalidated before and after each provider call, immediately before candidate
persistence, and inside accepted-decision persistence. Drift obsoletes the
attempt without creating or accepting a candidate.

## Review, acceptance, and Authority

AgileForge renders one deterministic, complete Markdown review packet from the
canonical payload and envelope. API, CLI, and dashboard readers expose that
same immutable candidate. A human decision binds to its candidate fingerprint
and cannot alter its bytes.

Acceptance records a registry row that references the accepted candidate and
payload fingerprint. Authority compilation loads only that accepted typed v2
payload. It projects normative semantics deterministically; Markdown and
provenance prose are never Authority input. Human Authority review remains a
separate mandatory gate before Backlog generation.

## Source of truth

The executable contract is defined by
`utils/agileforge_spec_profile_v2.py` and
`services/specs/candidate_contract.py`. Architecture rationale is recorded in
[ADR 0004](../../adr/0004-register-to-spec-source-before-structuring.md).
