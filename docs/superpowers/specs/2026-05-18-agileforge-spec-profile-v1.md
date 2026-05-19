# AgileForge Spec Profile v1

**Status:** Draft
**Version:** 0.2
**Created:** 2026-05-18
**Last Updated:** 2026-05-18
**Owner:** AgileForge maintainer/operator
**Reviewers:** Unknown

## 1. Summary

AgileForge needs a first-party specification profile that lets LLMs produce
project specs in a vocabulary that downstream AgileForge authority, Vision,
Backlog, Roadmap, Story, and Sprint phases can consume without guessing
semantic intent from ordinary prose.

The profile defines a canonical structured spec artifact, rendered Markdown for
human review, a controlled item vocabulary, trace links, and validation rules.
The LLM remains responsible for semantic authoring. Deterministic code validates
shape, IDs, links, freshness, and compiler readiness; it must not infer the
project's requirements from arbitrary prose.

## 2. Problem Statement

Phase 2D made authority review safer by blocking incomplete coverage. It also
exposed a deeper problem: AgileForge currently tries to derive authority from
free-form Markdown using deterministic candidate extraction. That caused
non-requirement text, including document metadata headers, to become review
blockers.

This is the wrong trust boundary. LLMs are better suited to semantic extraction
and authoring. Deterministic code is better suited to validating explicit
structure. AgileForge should therefore make spec generation produce explicit
structure at the source instead of trying to recover that structure later.

## 3. Goals And Non-Goals

### Goals

- Define a project-agnostic AgileForge spec vocabulary for generated specs.
- Make generated specs easy for humans to review in Markdown.
- Make generated specs easy for machines to validate as JSON.
- Separate normative requirements from context, rationale, examples, and notes.
- Give every semantically important item a stable ID, type, status, and trace
  links.
- Support goals, requirements, quality attributes, constraints, interfaces,
  data contracts, decisions, assumptions, risks, examples, and open questions.
- Let the authority compiler consume explicit spec items instead of relying on
  deterministic prose extraction.
- Preserve project agnosticism across software products, research projects,
  operations workflows, data products, and other domains.

### Non-Goals

- Requiring StrictDoc, Doorstop, ReqIF, OSLC, Gherkin, or another external tool
  as AgileForge's canonical authoring format.
- Making JSON pleasant for humans to edit directly.
- Treating Gherkin scenarios as a replacement for requirements.
- Inferring hidden requirements from unstructured prose with deterministic
  heuristics.
- Requiring perfect specs before a project can advance.
- Implementing the generator, compiler changes, or migrations in this document.

## 4. Current State

AgileForge already has a human-readable technical spec template with sections
such as goals, non-goals, functional requirements, quality attributes,
dependencies, rollout, metrics, and open questions. That structure is useful,
but it is not strict enough for authority compilation because:

- IDs are not required for all important items.
- Requirements and context can appear in the same prose block.
- Acceptance criteria are not always linked to specific requirements.
- Non-goals, assumptions, risks, quality attributes, and dependencies are not
  represented as first-class authority inputs.
- The authority compiler receives raw Markdown and must infer too much.

The Phase 2D host candidate extractor attempted to compensate by extracting
requirement candidates from Markdown. That approach created false blockers and
should not be the long-term authority boundary.

## 5. Proposed Specification

### 5.1 Canonical Artifact

The source of truth for generated AgileForge specs is a JSON artifact named
`TechnicalSpecArtifact`. Markdown is a deterministic view rendered from that
artifact.

Required top-level fields:

```json
{
  "schema_version": "agileforge.spec.v1",
  "artifact_id": "SPEC.project-slug",
  "title": "Project or feature title",
  "status": "draft",
  "version": "0.1",
  "created_at": "2026-05-18",
  "updated_at": "2026-05-18",
  "summary": "...",
  "problem_statement": "...",
  "items": [],
  "relations": [],
  "controlled_terms": [],
  "external_references": [],
  "rendering": {
    "markdown_profile": "agileforge.spec_markdown.v1",
    "rendered_markdown_sha256": "sha256:..."
  }
}
```

The JSON Schema must use explicit enums, required fields, and
`additionalProperties: false` for core objects. The v1 schema must be accepted
by the selected structured-output adapter in CI. Unsupported JSON Schema
features for that adapter are forbidden in v1 rather than hidden behind prompt
instructions.

### 5.1.1 Storage And Fingerprints

For v1, `spec.json` is the canonical artifact. `spec.md` is generated output
for human review.

Storage contract:

- `SpecRegistry.content` stores the canonical `TechnicalSpecArtifact` JSON as
  canonical JSON.
- `SpecRegistry.content_ref` points to the canonical JSON source path when one
  exists.
- `spec.md` may live beside `spec.json`, but it is not compiled directly.
- If both files exist, authority compilation must verify that `spec.md` is the
  deterministic render of `spec.json` for the declared Markdown profile.
- Manual Markdown edits outside the review-notes block make the Markdown
  non-canonical and block authority compilation until JSON is regenerated or
  the Markdown is re-rendered.

Canonical JSON uses UTF-8, sorted object keys, no insignificant whitespace, and
the exact scalar values after schema validation. The canonical spec hash is:

```text
sha256(canonical_json_bytes)
```

The review snapshot fingerprint is SHA-256 over canonical JSON with this
payload:

```json
{
  "schema": "agileforge.spec_review_snapshot.v1",
  "spec_schema_version": "agileforge.spec.v1",
  "artifact_id": "SPEC.project-slug",
  "canonical_spec_sha256": "sha256:...",
  "render_profile": "agileforge.spec_markdown.v1",
  "rendered_markdown_sha256": "sha256:...",
  "compiler_version": "1.0.0",
  "compiler_support_profile": "agileforge.authority_support.v1"
}
```

Authority review tokens must bind this snapshot fingerprint in addition to the
pending authority fingerprint, workflow state, setup status, and source path
guards.

### 5.2 Canonical Item Types

Every normative statement, explicit exclusion, unresolved blocker, accepted
decision, assumption, risk, data contract, and interface contract must be
represented as exactly one typed item. Each typed item uses one of these types:

| Type | Meaning | Authority Role |
| --- | --- | --- |
| `GOAL` | Desired product or project outcome | Guides prioritization and Vision |
| `NON_GOAL` | Explicitly excluded scope | Prevents scope drift |
| `REQ` | Functional or behavioral requirement | Normative authority input |
| `QUALITY` | Performance, reliability, security, usability, accessibility, or operations requirement | Normative authority input when measurable |
| `CONSTRAINT` | Technical, legal, timing, process, platform, cost, or compatibility boundary | Normative authority input |
| `INTERFACE` | CLI, API, UI, file, event, webhook, or integration contract | Normative authority input |
| `DATA` | Entity, field, lifecycle, retention, privacy, or state contract | Normative authority input |
| `DECISION` | Accepted design or product decision | Canonical decision context |
| `ASSUMPTION` | Premise accepted for planning until disproven | Reviewable planning risk |
| `RISK` | Known uncertainty, hazard, or failure mode | Reviewable planning risk |
| `EXAMPLE` | Scenario or illustrative behavior | Supports review and acceptance |
| `OPEN_QUESTION` | Unresolved question with impact | Blocks or constrains downstream scope |

The generator may include informative narrative, but only typed items are
authority inputs.

### 5.3 Item Schema

Each item has this common shape:

```json
{
  "id": "REQ.auth.session-timeout",
  "type": "REQ",
  "status": "proposed",
  "level": "MUST",
  "title": "Session timeout",
  "statement": "When a user is inactive for 30 minutes, AgileForge MUST invalidate the session.",
  "rationale": "Reduces risk from unattended authenticated sessions.",
  "verification": "system-test",
  "acceptance": [
    "Given an authenticated user has been inactive for 30 minutes, when the user sends another request, then the system rejects the request and requires re-authentication."
  ],
  "tags": ["auth", "security"],
  "source_notes": []
}
```

Required fields:

- `id`
- `type`
- `status`
- `title`
- `statement`

Required for normative item types `REQ`, `QUALITY`, `CONSTRAINT`,
`INTERFACE`, and `DATA`:

- `level`
- `verification`
- at least one `acceptance` entry or an explicit linked `EXAMPLE`

Allowed `status` values:

- `draft`
- `proposed`
- `accepted`
- `changed`
- `deferred`
- `rejected`
- `superseded`

Allowed `level` values:

- `MUST`
- `MUST_NOT`
- `SHOULD`
- `MAY`
- `INFORMATIVE`

Normative meaning follows the RFC 2119 / RFC 8174 uppercase keyword convention.
`INFORMATIVE` items are never compiled as enforceable authority.

Allowed `verification` values:

- `inspection`
- `analysis`
- `unit-test`
- `integration-test`
- `system-test`
- `acceptance-test`
- `manual-review`
- `monitoring`
- `not-yet-defined`

ID grammar:

```text
^(GOAL|NON_GOAL|REQ|QUALITY|CONSTRAINT|INTERFACE|DATA|DECISION|ASSUMPTION|RISK|EXAMPLE|OPEN_QUESTION)\.[a-z0-9][a-z0-9.-]{1,96}$
```

IDs are stable within an artifact version. Renaming an item creates a
`supersedes` relation from the new ID to the old ID unless the old item was
never reviewed.

`controlled_terms` entries use this shape:

```json
{
  "term": "operator",
  "definition": "The person configuring and running the project workflow.",
  "scope": "artifact"
}
```

`source_notes` entries use this shape:

```json
{
  "kind": "user_note",
  "text": "Original interview answer or source summary.",
  "external_ref_id": "EXT.cartola.spec-input"
}
```

External references are metadata only. A linked document is not authority unless
its relevant content is summarized in a typed item.

### 5.4 Relation Schema

Relations are first-class edges, not prose hints.

```json
{
  "from": "REQ.auth.session-timeout",
  "type": "satisfies",
  "to": "GOAL.auth.reduce-session-risk",
  "rationale": "The timeout directly reduces stale-session exposure."
}
```

Allowed relation types:

- `satisfies`
- `decomposes`
- `constrains`
- `depends_on`
- `implements`
- `verifies`
- `tracks`
- `supersedes`
- `conflicts_with`
- `clarifies`

All relation endpoints must reference existing item IDs. Unknown or orphan
relations are validation errors.

The top-level `relations` array is canonical. Inline relation fields such as
`Satisfies:` and `Depends on:` in Markdown are render-only aliases derived from
the top-level relation array. The JSON item objects do not store duplicate
relation arrays.

### 5.4.1 Lifecycle Matrix

Artifact and item status jointly determine compiler behavior:

| Artifact Status | Item Status | Normative Item Behavior | Downstream Visibility |
| --- | --- | --- | --- |
| `draft` | `draft` | Not compiled as enforceable authority | Review context only |
| `draft` | `proposed` | Compiled as a pending authority candidate, not canonical until authority acceptance | Visible as proposed scope |
| `draft` | `accepted` | Compiled as a pending authority candidate | Visible as stronger proposed scope |
| `accepted` | `proposed` | Invalid profile state unless an explicit review decision promotes the item during acceptance | Blocks compilation |
| `accepted` | `accepted` | Compiled as enforceable authority when supported | Canonical downstream input |
| any | `deferred` | Preserved as planning context, not enforceable | Visible as deferred |
| any | `rejected` | Preserved as exclusion or rejected context, not enforceable | Prevents accidental reintroduction |
| any | `superseded` | Not compiled; retained for traceability | Historical only |

An item with unresolved `conflicts_with` relations or an `OPEN_QUESTION` that
names it as impacted cannot become enforceable authority. The compiler must
surface such items as review context or gaps until the conflict or question is
resolved.

### 5.5 Markdown Rendering Profile

The generated Markdown is a view over the JSON artifact. Humans review Markdown;
AgileForge stores and compiles the JSON artifact.

Markdown item rendering:

```markdown
### REQ.auth.session-timeout - Session timeout

Type: REQ
Status: proposed
Level: MUST
Verification: system-test
Satisfies: GOAL.auth.reduce-session-risk
Depends on: REQ.auth.clock-source
Tags: auth, security

Statement:
When a user is inactive for 30 minutes, AgileForge MUST invalidate the session.

Rationale:
Reduces risk from unattended authenticated sessions.

Acceptance:
- Given an authenticated user has been inactive for 30 minutes, when the user sends another request, then the system rejects the request and requires re-authentication.
```

Manual Markdown review notes are allowed only inside this block:

```markdown
<!-- agileforge-review-notes:start -->
Reviewer notes here.
<!-- agileforge-review-notes:end -->
```

Any other manual edit makes the Markdown non-canonical. Non-canonical Markdown
is never compiled and cannot be used for authority acceptance until it is
reconciled into `spec.json` and re-rendered.

### 5.6 Spec Generation Workflow

The spec generator should run as a structured authoring pipeline:

```text
User idea, notes, interview answers, or rough PRD
-> LLM produces TechnicalSpecArtifact JSON using strict structured output
-> deterministic schema validation
-> deterministic structural validation
-> LLM semantic critique/gap pass
-> deterministic Markdown rendering
-> human review
-> authority compiler consumes structured items
```

Structural validators check:

- ID format and uniqueness.
- Required fields by item type.
- Allowed status, level, item type, verification, and relation values.
- Orphan relation endpoints.
- Missing acceptance evidence for normative items.
- Unsupported item types for the current authority compiler version.

Semantic concerns such as vague wording, duplicate intent, contradiction, weak
acceptance evidence, or missing business context are not host-inferred from
free prose. They are produced by the LLM spec generator, LLM semantic critique,
or human review as typed `GAP`, `OPEN_QUESTION`, `RISK`, or `DECISION` items.
The host may block on the presence of such typed items when the lifecycle matrix
or compiler support matrix says they are blocking, but it must not create them
by scanning arbitrary prose.

### 5.7 Authority Compiler Contract

The authority compiler consumes typed spec items and source notes, not a
deterministic candidate manifest extracted from Markdown.

Compiler behavior:

- Compile supported normative items according to the lifecycle matrix and
  compiler support matrix.
- Preserve explicit `NON_GOAL`, `ASSUMPTION`, `RISK`, and `OPEN_QUESTION`
  items as reviewable authority context.
- Preserve LLM-authored gaps when a normative item is too vague,
  contradictory, untestable, or unsupported by the current authority schema.
- Cite item IDs and exact source statements from the structured spec.
- Never generate authority from Markdown narrative that has no typed item.

Host behavior:

- Validate JSON shape and schema version.
- Validate source freshness and artifact fingerprints.
- Validate source item IDs and quote hashes.
- Validate duplicate IDs and relation integrity.
- Validate authority source citations against the structured spec.
- Do not create semantic requirements by scanning arbitrary prose.

### 5.7.1 Compiler Support Matrix

The v1 compiler must declare this support profile before compiling an
`agileforge.spec.v1` artifact:

| Item Type | Level | Supported v1 Mapping |
| --- | --- | --- |
| `GOAL` | any | `scope_themes` or eligible feature context; not an invariant |
| `NON_GOAL` | `MUST_NOT` or `INFORMATIVE` | `rejected_features`; `FORBIDDEN_CAPABILITY` only when an explicit forbidden capability is present |
| `REQ` | `MUST` or `SHOULD` | Invariant only when representable as `REQUIRED_FIELD`, `RELATION_CONSTRAINT`, or `MAX_VALUE`; otherwise review context plus gap |
| `REQ` | `MUST_NOT` | `FORBIDDEN_CAPABILITY` when explicit; otherwise `rejected_features` or gap |
| `QUALITY` | `MUST` or `SHOULD` | Invariant only when measurable as numeric max or relation; otherwise review context plus gap |
| `CONSTRAINT` | `MUST`, `MUST_NOT`, or `SHOULD` | Invariant when representable as forbidden capability, numeric max, or relation; otherwise review context plus gap |
| `INTERFACE` | `MUST` or `SHOULD` | `REQUIRED_FIELD` invariants for required fields/contracts when explicit; otherwise review context plus gap |
| `DATA` | `MUST` or `SHOULD` | `REQUIRED_FIELD` or `RELATION_CONSTRAINT` invariants when explicit; otherwise review context plus gap |
| `DECISION` | any | Review context; not an invariant |
| `ASSUMPTION` | any | `assumptions` |
| `RISK` | any | Review context or gap when it blocks downstream authority |
| `EXAMPLE` | any | Source evidence or acceptance context; not an invariant |
| `OPEN_QUESTION` | any | Gap when blocking; otherwise review context |

Unsupported normative items must not be silently dropped or forced into weak
invariant strings. They become explicit gaps or review context with their item
IDs preserved.

### 5.8 Examples And Scenarios

Gherkin-style examples may be rendered under `EXAMPLE` items. They clarify
behavior but do not replace normative item statements.

```json
{
  "id": "EXAMPLE.auth.session-timeout.expired-request",
  "type": "EXAMPLE",
  "status": "proposed",
  "title": "Expired request requires login",
  "statement": "Given an authenticated user has been inactive for 30 minutes, when the user sends another request, then the system rejects the request and requires re-authentication."
}
```

## 6. Error Handling And Edge Cases

| Case | Required Behavior | User/System Impact |
| --- | --- | --- |
| LLM returns schema-invalid JSON | Retry up to 2 times, then persist the raw failed output as a diagnostic artifact and do not write `spec.json` or `spec.md` | No malformed spec is persisted as canonical |
| LLM returns schema-valid but semantically weak normative item | LLM critique or human review records a typed gap or open question | Human sees actionable weakness before authority compilation |
| Markdown is manually edited outside the review-notes block | Mark `spec.md` non-canonical and block compilation until JSON is regenerated or Markdown is re-rendered | Avoids silent semantic drift |
| Relation references missing item | Validation error | Prevents broken traceability |
| Requirement lacks acceptance or verification | Blocking validation error unless explicitly deferred | Prevents untestable authority |
| Open question affects downstream scope | Keep affected items non-enforceable and compile the question as blocking context | Avoids pretending unresolved decisions are settled |
| External references are cited | Require a summary item and link metadata | Authority remains self-contained enough for review |

## 7. Quality Attributes

### Reviewability

The Markdown view must be readable in GitHub and local editors. Typed items must
be easy to scan by ID, status, level, type, and relation links.

### Provider Portability

The JSON Schema should use a conservative subset: plain objects, arrays, enums,
strings, booleans, numbers, required fields, nullable fields where needed, and
`additionalProperties: false`. Avoid deep unions and provider-specific schema
features unless isolated behind an adapter.

### Reliability

Spec generation must be repeatable enough for review, but it does not need to be
perfect. Validation failures should produce actionable errors and preserve the
invalid output for diagnosis.

### Project Agnosticism

No item type, validator, or compiler behavior may depend on a specific domain
such as caRtola, finance, betting, healthcare, or developer tooling. Domain
details live in item statements and tags, not in parser code.

## 8. Alternatives Considered

| Option | Pros | Cons | Decision |
| --- | --- | --- | --- |
| Continue free-form Markdown plus deterministic extraction | Easy to keep current authoring format | Recreates false blockers and misses semantic intent | Rejected |
| Adopt StrictDoc SDoc as canonical | Mature requirement IDs, fields, traceability, export options | Too rigid as AgileForge's generated front door; not Markdown-first | Rejected for v1, borrow concepts |
| Adopt Doorstop item files | Git-native requirements model and item metadata | YAML-file-per-item is noisy for generated human review | Rejected for v1, borrow concepts |
| Adopt ReqIF or OSLC as canonical | Standardized requirements interchange concepts | Heavyweight and unpleasant as authoring source | Rejected for v1, consider export later |
| Use only Gherkin | Strong examples and acceptance scenarios | Not a complete requirements vocabulary | Rejected, use for examples only |
| Canonical JSON plus rendered Markdown | Strong validation and readable review | Requires renderer and schema discipline | Chosen |

## 9. Dependencies And Constraints

- Structured output provider support varies by model and vendor.
- Pydantic or equivalent models should be the application-level contract.
- JSON Schema should be generated from the application model or kept in lockstep
  by tests.
- Authority compiler versions must declare which item types they support.
- Existing free-form specs may need migration, regeneration, or a legacy path.
- Legacy Markdown compatibility must be behind a named compatibility mode:
  `agileforge.spec_legacy_markdown.v1`.

## 10. Rollout, Migration, And Compatibility

This profile should be introduced as an optional v1 authoring target before it
becomes required.

Compatibility rules:

- Existing Markdown specs remain readable legacy inputs.
- New LLM-generated specs should produce both `spec.json` and rendered
  `spec.md`.
- Authority compilation should prefer `spec.json` when present.
- Legacy Markdown authority compilation may continue temporarily, but without
  deterministic semantic candidate blockers.
- Legacy Markdown inputs may create pending authority for review, but only
  `agileforge.spec.v1` artifacts are eligible for the structured spec compiler
  path.
- The legacy path is eligible for removal after the multi-domain fixture suite
  can generate valid `agileforge.spec.v1` artifacts and compile them without
  using Markdown candidate extraction.
- A future migration can convert legacy specs into the profile with human
  review before acceptance.

## 11. Success Metrics

| Metric | Target | Measurement Source |
| --- | --- | --- |
| Schema-valid generated specs | >= 95% after retry | Spec generator telemetry |
| Normative items with acceptance evidence | >= 95% | Structural validator |
| Orphan relation rate | 0 accepted specs | Structural validator |
| Authority compiler setup failure rate | Downward trend across multi-domain fixtures | CLI smoke tests |
| Human review blockers caused by metadata/prose parsing | 0 | Authority review tests |
| Domain-specific parser rules | 0 | Code review |

## 12. References

- StrictDoc: requirements as explicit structured nodes with UIDs and
  traceability.
- Doorstop: git-native requirement metadata and review state.
- OpenFastTrace: Markdown-oriented spec items and trace coverage concepts.
- EARS: controlled natural language patterns for requirement statements.
- Gherkin: examples and acceptance scenarios, not full authority.
- ReqIF and OSLC RM: interchange and relation vocabulary concepts.
- RFC 2119 and RFC 8174: uppercase requirement level keywords.

## 13. Open Questions

| Question | Impact | Owner | Status |
| --- | --- | --- | --- |
| Which structured output adapter should be first: direct provider-native output, Instructor, BAML, or an internal wrapper? | Affects implementation complexity and provider portability | AgileForge maintainer/operator | Open |
| Should the first implementation add database columns for rendered Markdown hashes, or compute them from sidecar files only? | Affects migration size and review token implementation | AgileForge maintainer/operator | Open |
| What is the minimum accepted item set for Vision to begin when the spec artifact is still draft? | Affects setup gate strictness | AgileForge maintainer/operator | Open |

## 14. Revision History

| Date | Version | Change | Author |
| --- | --- | --- | --- |
| 2026-05-18 | 0.2 | Added storage/fingerprint contract, lifecycle matrix, support matrix, structural validator boundary, and Markdown edit model | Codex |
| 2026-05-18 | 0.1 | Initial draft of AgileForge Spec Profile v1 | Codex |
