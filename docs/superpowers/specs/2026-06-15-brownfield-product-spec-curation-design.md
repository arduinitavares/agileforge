# Brownfield Product-Spec Curation Design

**Status:** Draft
**Version:** 0.2
**Created:** 2026-06-15
**Last Updated:** 2026-06-15
**Owner:** AgileForge maintainers
**Reviewers:** User and implementation agent
**Spec Mode:** `proposed_change`
**Issue:** https://github.com/arduinitavares/agileforge/issues/129

## 1. Summary

Add an explicit brownfield setup mode that separates raw implementation
inventory from compileable product authority. Brownfield projects start as a
project/workflow shell, collect non-authoritative source and scan artifacts,
produce a curated product-spec draft, require human approval of one explicit
draft attempt, and only then register the compileable `agileforge.spec.v1`
version used by `authority compile`.

The central invariant is:

```text
Raw brownfield input must never enter SpecRegistry.
```

## 2. Problem Statement

Issue #129 reports that brownfield setup can turn current implementation
inventory into product authority. The concrete failure shape is route or field
contracts that are technically true, but too implementation-heavy to be useful
as product authority.

Current setup behavior is greenfield-oriented:

- `ProjectCreateRequest` requires `spec_file`.
- `project create` registers a pending spec version before authority compile.
- `project create` writes `setup_spec_file_path`, `setup_spec_hash`, and
  `setup_spec_version_id` into workflow state.
- `authority compile` compiles from those setup spec fields after stale guards
  pass.

That behavior is correct for curated greenfield specs. It is unsafe for messy
brownfield notes, repository inventory, route dumps, API field lists, or future
architecture gaps.

Existing `as-built assess` is not the missing workflow. It compares accepted
authority against repository evidence after authority exists. Brownfield
product-spec curation must happen before authority compilation.

## 3. Goals And Non-Goals

### Goals

- Keep greenfield `project create --spec-file specs/spec.json` behavior
  unchanged.
- Add brownfield setup without registering raw brownfield input as a
  compileable spec.
- Store brownfield source, scan, draft, and approval artifacts separately from
  `SpecRegistry` until approval.
- Require explicit human approval of a specific draft attempt before authority
  compilation.
- Keep persisted setup statuses minimal and derive brownfield progress from
  artifact fingerprints.
- Prevent stale drafts from being approved after newer source or scan artifacts
  supersede their chain.
- Make `authority compile` runnable only after brownfield approval has produced
  the curated compileable spec version.
- Warn when a brownfield draft is mostly route, field, table, framework, or
  implementation contracts instead of product-level rules.

### Non-Goals

- Do not overload `as-built assess`; it remains post-authority evidence
  assessment.
- Do not auto-promote routes, fields, database tables, framework details, or
  internal jobs to authority.
- Do not add an active draft pointer or persisted `current_draft_attempt_id`.
- Do not add setup statuses for every brownfield step unless a future workflow
  proves that artifact-derived progress is insufficient.
- Do not change authority review or authority acceptance semantics.
- Do not implement scope extension, backlog generation, roadmap generation,
  story generation, sprint planning, or post-authority evidence collection in
  this change.

## 4. Users And Stakeholders

- **Primary users:** Agents and operators creating AgileForge projects from an
  existing codebase or messy brownfield notes.
- **Internal stakeholders:** AgileForge CLI, dashboard/API, workflow state,
  authority compiler, and spec registry maintainers.
- **External systems:** The target product repository scanned during
  brownfield setup.

## 5. Current State

The prior project-create split design established setup substates under
`fsm_state="SETUP_REQUIRED"` and left brownfield product-spec curation to this
issue. Greenfield setup currently moves through:

```text
project create -> authority_compile_required -> authority compile
-> authority_pending_review -> authority accept -> workflow next
```

The existing setup state for `authority_compile_required` requires:

- `setup_status="authority_compile_required"`
- `setup_spec_file_path`
- `setup_spec_hash`
- `setup_spec_version_id`
- `setup_next_actions=[authority compile action]`

This design adds a brownfield setup branch before those fields exist.

## 6. Proposed Specification

### 6.1 Functional Requirements

| ID | Requirement | Acceptance Criteria | Priority |
| --- | --- | --- | --- |
| FR-001 | Greenfield project creation remains spec-first and compile-ready. | `project create --setup-mode greenfield --spec-file specs/spec.json` and default `project create --spec-file specs/spec.json` register a spec version and return `setup_status=authority_compile_required`. | Must |
| FR-002 | Brownfield project creation creates a project/workflow shell without compileable spec fields. | `project create --setup-mode brownfield --name InvoicePortal` creates the project, sets `setup_mode=brownfield`, sets `setup_status=brownfield_curation_required`, and does not create a `SpecRegistry` row or write `setup_spec_file_path`, `setup_spec_hash`, or `setup_spec_version_id`. | Must |
| FR-003 | Brownfield create is shell-only. | `project create --setup-mode brownfield` accepts project setup metadata only; it rejects `--spec-file`, `--source-file`, and `--repo-path`. Raw source and repository inputs are recorded by brownfield source/scan commands. | Must |
| FR-004 | Brownfield source and scan artifacts are non-authoritative. | Source and scan artifacts cannot be compiled as authority and cannot create backlog, roadmap, story, or sprint artifacts. | Must |
| FR-005 | Draft artifacts contain curated product-spec candidates. | A `brownfield_spec_draft` contains or references a curated `agileforge.spec.v1` candidate plus warnings, source links, parent fingerprints, and an `attempt_id`. | Must |
| FR-006 | Approval uses explicit attempt identity. | `brownfield spec approve` requires `--attempt-id`, `--expected-artifact-fingerprint`, `--expected-state SETUP_REQUIRED`, `--expected-setup-status brownfield_curation_required`, and `--idempotency-key`. | Must |
| FR-007 | Approval validates the full artifact chain. | Approval succeeds only when the draft exists, is complete/reusable, belongs to the project, matches the expected fingerprint, was generated from the current scan, and the current scan was generated from the current source/repo snapshot. | Must |
| FR-008 | Approval is the only bridge into `authority_compile_required`. | Successful approval atomically registers the curated `SpecRegistry` version, writes setup spec fields, records approval metadata, transitions to `setup_status=authority_compile_required`, and persists the idempotency response. | Must |
| FR-009 | Authority compile rejects incomplete brownfield setup. | For `setup_mode=brownfield`, `authority compile` requires `setup_status=authority_compile_required`, present setup spec fields, and approval metadata proving the setup spec version came from brownfield approval. | Must |
| FR-010 | Brownfield progress is projection-only derived data. | API/CLI responses may include `brownfield_progress`, but no persisted `current_draft_attempt_id` or active draft pointer exists. | Must |
| FR-011 | Implementation-heavy drafts produce machine-readable warnings. | Draft responses include `BROWNFIELD_SPEC_IMPLEMENTATION_HEAVY` when the curated candidate is dominated by route, field, table, framework, or internal implementation details. | Should |
| FR-012 | Human-edited curated specs are first-class draft attempts. | `brownfield spec import --curated-spec-file curated/spec.json` validates and records the edited curated spec as a new `brownfield_spec_draft` attempt with `origin=human_import`. Approval uses that imported attempt id. | Must |
| FR-013 | Brownfield artifacts use dedicated database tables in v1. | Source, scan, draft, and approval history are stored in dedicated SQLModel tables, not workflow-state blobs. Workflow state stores only setup status and the approved setup spec fields. | Must |

### 6.2 Public Workflow

Greenfield remains:

```text
agileforge project create --name InvoicePortal --spec-file specs/spec.json
agileforge authority compile --project-id 42 ...
agileforge authority review --project-id 42
agileforge authority accept ...
```

Brownfield becomes:

```text
agileforge project create --setup-mode brownfield --name InvoicePortal
agileforge brownfield source import --project-id 42 --source-file notes.md
agileforge brownfield scan --project-id 42 --repo-path /workspace/invoice-portal --source-attempt-id source-123
agileforge brownfield spec draft --project-id 42 --user-input "prioritize product rules"
agileforge brownfield spec approve \
  --project-id 42 \
  --attempt-id draft-123 \
  --expected-artifact-fingerprint sha256:... \
  --expected-state SETUP_REQUIRED \
  --expected-setup-status brownfield_curation_required \
  --idempotency-key approve-draft-123
agileforge authority compile --project-id 42 ...
```

If a human edits the curated spec before approval, the edited file is imported
as a new draft attempt and that imported attempt is approved:

```text
agileforge brownfield spec import \
  --project-id 42 \
  --curated-spec-file curated/spec.json \
  --parent-draft-attempt-id draft-123 \
  --expected-scan-fingerprint sha256:...
agileforge brownfield spec approve \
  --project-id 42 \
  --attempt-id draft-import-456 \
  --expected-artifact-fingerprint sha256:... \
  --expected-state SETUP_REQUIRED \
  --expected-setup-status brownfield_curation_required \
  --idempotency-key approve-draft-import-456
```

If the user already has a clean curated `agileforge.spec.v1` file, they should
use the greenfield path. Brownfield mode is for raw, mixed, or messy sources
that need curation before authority compilation.

### 6.3 Setup States

Persisted setup statuses stay minimal.

| FSM state | setup_status | Meaning | Runnable commands |
| --- | --- | --- | --- |
| `SETUP_REQUIRED` | `brownfield_curation_required` | Brownfield source/scan/draft/approval is still in progress; no compileable spec version is installed. | `brownfield source import`, `brownfield scan`, `brownfield spec draft`, `brownfield spec import`, `brownfield spec approve` when derived guards allow it |
| `SETUP_REQUIRED` | `authority_compile_required` | A compileable curated spec version exists and is ready for authority compilation. | `authority compile`, `authority status` |
| `SETUP_REQUIRED` | existing authority statuses | Authority compilation/review/decision continues through the existing setup flow. | Existing authority commands |

No `brownfield_scan_recorded`, `brownfield_spec_draft_ready`, or
`brownfield_spec_approved` setup status is persisted. Those labels are derived
from artifact state.

### 6.4 Source Recording

Brownfield project creation is shell-only. It does not record source files or
repository paths.

Raw inputs are recorded after project creation:

- `brownfield source import` records source documents, notes, product briefs,
  route dumps, or implementation inventories as non-authoritative source
  artifacts.
- `brownfield scan` records repository snapshot metadata and implementation
  facts as non-authoritative scan artifacts.
- `brownfield spec draft` generates a curated product-spec draft from the
  current source/scan chain.
- `brownfield spec import` records a human-edited curated `agileforge.spec.v1`
  file as a new draft attempt with `origin=human_import`.

No command before `brownfield spec approve` may create a `SpecRegistry` row or
write setup spec fields into workflow state.

### 6.5 Storage Contract

V1 brownfield artifact history uses dedicated SQLModel tables created through
the business database readiness path. It must not be stored only as workflow
state JSON.

Required tables:

| Table | Purpose | Required identity |
| --- | --- | --- |
| `brownfield_source_artifacts` | Raw source documents and source metadata. | Unique `(project_id, attempt_id)` and indexed `artifact_fingerprint`. |
| `brownfield_scan_attempts` | Repository scans and implementation facts. | Unique `(project_id, attempt_id)` and indexed `source_fingerprint`, `repo_commit`, `artifact_fingerprint`. |
| `brownfield_spec_draft_attempts` | Generated or imported curated spec candidates. | Unique `(project_id, attempt_id)` and indexed `scan_fingerprint`, `source_fingerprint`, `artifact_fingerprint`, `origin`. |
| `brownfield_spec_approvals` | Approval bridge from a draft attempt to `SpecRegistry`. | Unique `approval_fingerprint` and indexed `project_id`, `draft_attempt_id`, `spec_version_id`, `mutation_event_id`. |

Common artifact fields:

- `project_id`
- `attempt_id` for source, scan, and draft attempts
- `artifact_fingerprint`
- `status` such as `complete`, `failed`, or `incomplete`
- parent fingerprint fields
- tool or agent version
- relevant user input hash
- created timestamp
- warning and error metadata JSON

Approval fields:

- `approval_fingerprint`
- `draft_attempt_id`
- `draft_fingerprint`
- `scan_fingerprint`
- `source_fingerprint`
- `spec_hash`
- `spec_version_id`
- `managed_spec_file_path`
- `mutation_event_id`
- `status` such as `started`, `spec_registered`, `workflow_written`,
  `complete`, or `recovery_required`

Workflow state stores only:

- `setup_mode`
- `setup_status`
- `setup_error`
- `setup_spec_file_path`, `setup_spec_hash`, and `setup_spec_version_id` after
  approval
- `setup_next_actions`

`CliMutationLedger` remains the idempotency and recovery authority for mutating
commands. `WorkflowEvent` may record audit events, but it is not the source of
truth for brownfield artifact history.

### 6.6 Artifact Chain

Brownfield artifacts form a parent-fingerprint chain:

```text
source_fingerprint
  -> scan_fingerprint
  -> draft_fingerprint
  -> approval_fingerprint
  -> spec_version_id
```

Freshness rules:

- A `brownfield_source` is current when it is the newest complete source record
  for the project.
- A `brownfield_scan` is current only when it is complete and matches the
  current source fingerprint, repo path, repo commit, repo dirty flag, scanner
  version, and scan user input hash.
- A `brownfield_spec_draft` is reusable only when it is complete, matches the
  current scan fingerprint, records the same source fingerprint, records the
  drafter version and user input hash used for generation/import, and contains
  a valid curated spec candidate.
- A `brownfield_spec_approval` is current only when it records the approved
  draft fingerprint and the registered `spec_version_id`.

A newer complete source or scan supersedes older drafts. Older drafts remain in
history but become unapprovable.

### 6.7 Draft Selection

There is no active draft pointer.

Approval always names the draft explicitly:

```text
--attempt-id draft-123
--expected-artifact-fingerprint sha256:...
```

The service loads that exact draft and validates it against the current chain.
The submitted attempt may differ from the projection's recommended draft, but
it must still be complete, reusable, and current-chain valid.

Projection responses may expose a recommendation:

```json
{
  "brownfield_progress": {
    "source": "current",
    "scan": "current",
    "draft": "ready",
    "approval": "required",
    "recommended_draft_attempt_id": "draft-123"
  }
}
```

`recommended_draft_attempt_id` is not authoritative storage. It is derived from
the latest complete reusable draft on the current chain.

### 6.8 Approval Contract

`brownfield spec approve` is a guarded mutation. It must validate all of these
conditions before any side effects:

- project exists
- workflow state is `SETUP_REQUIRED`
- setup status is `brownfield_curation_required`
- draft exists
- draft belongs to the project
- draft is complete/reusable
- draft fingerprint matches `--expected-artifact-fingerprint`
- draft records the current scan fingerprint
- current scan records the current source fingerprint and repo snapshot
- no newer source or scan supersedes the chain
- curated artifact validates as `agileforge.spec.v1`
- no compileable curated spec for this brownfield curation flow already exists,
  unless replaying the same idempotent approval

On success, the mutation atomically:

1. writes the canonical approved spec JSON to the managed approval path
2. registers that managed spec in `SpecRegistry`
3. records approval metadata with draft and chain fingerprints
4. writes `setup_spec_file_path`, `setup_spec_hash`, and `setup_spec_version_id`
5. transitions `setup_status` to `authority_compile_required`
6. writes `setup_next_actions=[authority compile action]`
7. persists the idempotency response

If any side effect fails after the mutation starts, recovery must be explicit
through the mutation ledger. A replay must resume recovery, return the original
response, or return a recovery/idempotency conflict according to the invariant
in Section 6.10.

### 6.9 Managed Approved Spec Path

`setup_spec_file_path` must never point to a raw brownfield source file, a route
dump, or an arbitrary human edit path.

Before registering `SpecRegistry`, approval materializes the reviewed curated
spec to an AgileForge-managed canonical JSON path:

```text
${AGILEFORGE_CONFIG_ROOT}/artifacts/brownfield/{project_id}/approvals/{approval_attempt_id}/spec.json
```

If the curated spec came from a generated draft, this file contains the
canonical generated spec candidate. If the curated spec came from
`brownfield spec import`, this file contains the normalized imported
`agileforge.spec.v1` content. The original imported file path is retained only
as draft provenance metadata.

`SpecRegistry.content_ref` and workflow `setup_spec_file_path` both use the
managed approval path. `SpecRegistry.content` stores the same normalized spec
content. `setup_spec_hash` equals the canonical hash of the managed approved
spec file.

This keeps `authority compile`, audit output, and stale guards pointed at the
reviewed compileable spec, not the raw source or an editor-local file path.

### 6.10 Partial Approval Recovery Invariant

Approval has one non-negotiable recovery invariant:

```text
At most one SpecRegistry row may exist for a brownfield approval fingerprint.
```

If a retry finds that the `SpecRegistry` row exists but workflow setup fields
were not written, the same idempotency key must resume from the recorded
`brownfield_spec_approvals.spec_version_id` and complete the workflow write or
return `MUTATION_RECOVERY_REQUIRED`. It must never create a second
`SpecRegistry` row.

If a different idempotency key attempts to approve the same draft chain after a
curated spec row already exists, the command returns
`BROWNFIELD_CURATED_SPEC_ALREADY_REGISTERED` unless it is explicitly recovering
the original mutation through the mutation ledger.

Workflow state must remain `brownfield_curation_required` until all approval
side effects are consistent:

- managed approved spec file written
- `SpecRegistry` row registered
- `brownfield_spec_approvals` row records the `spec_version_id`
- workflow setup spec fields written
- `setup_status=authority_compile_required`
- idempotency response persisted

### 6.11 Error And Warning Codes

Approval guard failures use explicit error codes:

| Code | Meaning |
| --- | --- |
| `BROWNFIELD_DRAFT_NOT_FOUND` | The requested draft attempt does not exist for the project. |
| `BROWNFIELD_DRAFT_STALE` | The requested draft fingerprint does not match the expected fingerprint or has been superseded. |
| `BROWNFIELD_DRAFT_INCOMPLETE` | The draft exists but is failed, incomplete, or not reusable. |
| `BROWNFIELD_SOURCE_SUPERSEDED` | A newer source or repo snapshot superseded the draft chain. |
| `BROWNFIELD_APPROVAL_CHAIN_MISMATCH` | The draft fingerprint is valid, but its recorded source/scan chain does not match the current chain. |
| `BROWNFIELD_CURATED_SPEC_ALREADY_REGISTERED` | A curated compileable spec for this brownfield curation flow already exists and the request is not idempotent replay. |
| `BROWNFIELD_APPROVAL_STALE_GUARD` | `expected_state`, `expected_setup_status`, or other approval stale guards do not match current workflow state. |

Drafting warnings use machine-readable warning codes:

| Code | Meaning |
| --- | --- |
| `BROWNFIELD_SPEC_IMPLEMENTATION_HEAVY` | The draft is dominated by implementation contracts rather than product-level obligations. |

### 6.12 Implementation-Heavy Draft Detection

The drafter and host validation should treat the following as risk signals, not
automatic authority:

- route names and route methods
- database table names
- internal model fields
- framework/library details
- job queue or worker internals
- storage paths and generated file names
- future automation that is not current product behavior

The curated draft should separate:

- product requirements as `REQ`, `QUALITY`, `CONSTRAINT`, `INTERFACE`, or
  `DATA`
- accepted product decisions as `DECISION`
- excluded future scope as `NON_GOAL`
- hazards and gaps as `RISK`
- unresolved blockers as `OPEN_QUESTION`

Route or field contracts may appear only when they are explicit product-facing
contracts reviewed and accepted by a human.

## 7. Quality Attributes

### Security And Privacy

Brownfield source and scan artifacts may include repository paths, API shapes,
sample payloads, or implementation notes. Responses must not leak secrets, raw
credentials, private keys, tokens, or unredacted environment files. Scan
artifacts should include enough metadata for review and freshness checks without
copying sensitive files wholesale.

### Performance And Scale

Scan and draft commands may be long-running. They must be idempotent by request
fingerprint and resumable or retryable through attempt history. Large scan
outputs should be summarized into bounded evidence artifacts so `workflow next`
and status projections remain compact.

### Reliability And Operations

Approval is the reliability-critical boundary. It must be idempotent, stale
guarded, and recovery-aware. Partial approval must not leave `setup_status` at
`authority_compile_required` unless the setup spec fields and approval metadata
were written consistently.

### Accessibility And Localization

No user-interface layout is specified here. CLI and API responses must expose
machine-readable state and warning codes so dashboard text can explain
brownfield progress without relying on free-form prose.

## 8. Alternatives Considered

| Option | Pros | Cons | Decision |
| --- | --- | --- | --- |
| Pre-project brownfield wizard | Keeps raw source outside project setup. | No project/session exists to store scan, draft, and approval artifacts. | Rejected. |
| Brownfield project shell first | Uses the existing project setup lifecycle and workflow state. | Requires explicit guards so no raw source becomes compileable spec. | Chosen. |
| Dedicated brownfield artifact tables | Supports history, fingerprint queries, idempotent approval recovery, and stale-chain validation. | Adds schema surface. | Chosen for v1. |
| Workflow-state brownfield blobs | Fewer tables. | Hard to query history, supersession, and partial approval recovery without drift. | Rejected. |
| Auto-curate inside `authority compile` | Fewer commands. | Hides the human review gate and recreates #129 risk. | Rejected. |
| Persist one setup status per brownfield step | Simple UI labels. | Duplicates artifact truth and can drift from executable command guards. | Rejected. |
| Active draft pointer | Easy approval default. | Adds another mutable source of truth and can drift from the artifact chain. | Rejected. |

## 9. Dependencies And Constraints

- The design depends on the existing `SETUP_REQUIRED` setup-state model.
- The design depends on `SpecRegistry` remaining the compileable spec source for
  authority compilation.
- The design depends on the `agileforge.spec.v1` profile item types:
  `REQ`, `QUALITY`, `CONSTRAINT`, `INTERFACE`, `DATA`, `DECISION`, `NON_GOAL`,
  `RISK`, and `OPEN_QUESTION`.
- The design assumes workflow projections can derive `brownfield_progress` from
  artifact history and fingerprints.
- Brownfield artifact history must use dedicated SQLModel tables in v1.
- Brownfield approval must materialize a managed approved spec file before
  registering `SpecRegistry`.

## 10. Rollout, Migration, And Compatibility

Existing projects and greenfield setup are unaffected.

Compatibility rules:

- Existing `project create --spec-file` behavior remains greenfield and
  compile-ready.
- Brownfield mode is opt-in through `--setup-mode brownfield`.
- Brownfield mode rejects `--spec-file` as raw input to avoid overloading the
  greenfield contract.
- Dashboard/API project creation must either stay greenfield-only until
  brownfield UI support exists or expose brownfield-specific source fields.
- No migration is required for existing `SpecRegistry` rows.

Rollback rule:

- If brownfield curation is disabled, existing greenfield project creation and
  authority compilation must continue to work.

## 11. Success Metrics

| Metric | Target | Measurement Source |
| --- | --- | --- |
| Raw brownfield input registered as compileable spec | 0 occurrences | Tests and mutation/audit event review |
| Brownfield authority compile before approval | 0 successful runs | CLI/API tests and workflow guards |
| Approval from superseded draft | 0 successful approvals | Brownfield approval tests |
| Duplicate `SpecRegistry` row for one approval fingerprint | 0 duplicate rows | Brownfield approval recovery tests |
| Implementation-heavy draft warning coverage | Route/field-heavy fixture emits `BROWNFIELD_SPEC_IMPLEMENTATION_HEAVY` | Draft validation tests |
| Greenfield setup regression | Existing project-create and authority-compile tests pass unchanged except for intentional interface additions | Test suite |

## 12. Open Questions

| Question | Impact | Owner | Status |
| --- | --- | --- | --- |
| Should the dashboard expose brownfield curation in the first implementation or remain greenfield-only until CLI contracts settle? | Affects UI scope, not the CLI/API setup contract. | Implementation agent | Deferred to implementation plan |

## 13. Revision History

| Date | Version | Change | Author |
| --- | --- | --- | --- |
| 2026-06-15 | 0.2 | Pinned shell-only create, dedicated artifact tables, human import, managed approved spec path, and partial approval recovery invariants. | Codex |
| 2026-06-15 | 0.1 | Initial design for issue #129 brownfield product-spec curation. | Codex |
