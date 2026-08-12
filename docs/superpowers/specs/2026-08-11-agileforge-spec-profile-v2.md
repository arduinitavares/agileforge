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
`specification.author`. The host prepares the complete authoring input from
durable state and invokes the configured `to-spec` producer. Humans provide
normal-language review decisions and feedback. They never author payload JSON,
IDs, hashes, fingerprints, lineage, source manifests, or attempt metadata.

The producer returns only:

- one complete `SpecificationPayload`;
- removal justifications for an amendment; and
- explicit stable-ID replacements for an amendment.

Discovery, research, repository inspection, interviews, ADRs, and prototypes
remain optional activities and provenance sources. They are not persisted as a
Discovery lifecycle artifact or review gate.

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
immutable `agileforge.spec-candidate-envelope.v1` envelope binds the exact
payload to:

- accepted Vision and Product Goal identities and fingerprints;
- the complete source manifest and accepted-fact fingerprint;
- producer input, capability, version, model configuration, and prompt;
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
`SRC.repository-evidence.accepted-vision`, and
`SRC.repository-context.active`. Accepted-fact and producer-input fingerprints
use portable semantic projections that exclude project, artifact, attempt, and
version row IDs while retaining the exact accepted content, source, base,
producer, model, prompt, and amendment evidence.

An initial candidate has no base. An amendment pins one accepted Specification
base and includes deterministic added, changed, and removed keys across items,
relations, controlled terms, and external references. Every removal requires a
justification. Replacing a stable item ID requires an explicit old-to-new
mapping and justification.

Optional post-Goal repository sources use only `README.md`, `CONTEXT.md`,
`pyproject.toml`, `specs/spec.json`, `specs/spec.md`, `docs/spec/spec.json`, or
`docs/spec/spec.md`. Operators refresh the repository binding after writing
source material. The host captures a bounded source bundle and revalidates its
fingerprint at the last boundary before provider invocation. Drift obsoletes
the attempt without calling the provider. Each warning is rendered directly
under the source-manifest entry that produced it.

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
[ADR 0003](../../adr/0003-make-to-spec-the-canonical-specification-boundary.md).
