# Authority Review And Decision Design

**Date:** 2026-05-17
**Status:** In Review
**Scope:** CLI-first pending Spec Authority review, assisted assessment, accept,
reject, and workflow advancement

## Summary

AgileForge can now create a project and compile a pending Spec Authority, but a
project can dead-end in `setup_status=authority_pending_review` because there is
no first-party review and decision path. The next slice must expose the compiled
authority as a complete review packet, let a human or AI-assisted reviewer assess
whether it faithfully represents the source spec, and then record an explicit
accept or reject decision.

The core human decision is:

> Yes, this compiled interpretation correctly represents the spec. Use it as
> the canonical authority for later phases.

Acceptance makes the compiled authority canonical and unlocks Vision. Rejection
keeps Vision locked and records why the pending authority must not be used.

## Problem

Project setup currently has a valid intermediate checkpoint:

```text
fsm_state = SETUP_REQUIRED
setup_status = authority_pending_review
authority.status = pending_acceptance
pending_authority_id = <compiled authority id>
```

This means setup compiled a reviewable authority artifact successfully. It does
not mean setup failed. The missing piece is a decision path.

The current product issue has three parts:

- The CLI exposes status and invariants, but not the full compiled authority
  packet needed for review.
- The CLI has no installed `authority accept` or `authority reject` command.
- The dashboard and workflow-next logic treat the state as generic setup
  required rather than pending authority review.

## Goals

- Let agents retrieve every relevant input needed to assess pending authority.
- Let humans review the same evidence without typing machine-only guard fields.
- Preserve a strict manual checkpoint before any authority becomes canonical.
- Support AI-assisted review without making AgileForge itself auto-accept.
- Provide explicit accept and reject decisions.
- Advance to Vision only after an accepted decision is recorded.
- Keep command output machine-readable by default while supporting readable text
  for human review.
- Keep advanced fingerprint and idempotency controls available for agents and
  deterministic automation.
- Prevent a reviewer from accepting a different authority than the one they
  reviewed.
- Revalidate source spec freshness before recording any decision.

## Non-Goals

- Auto-accepting authority after compilation.
- Building a model-provider-backed `authority assess` command in this slice.
- Implementing project spec update or recompile in this slice.
- Replacing the dashboard with the CLI.
- Making humans manually type fingerprints, pending ids, or idempotency keys for
  the normal happy path.

## Concepts

### Compiled Authority

The compiled authority is the normalized interpretation of a project spec. It is
stored in `compiled_spec_authority` and includes fields such as:

- domain
- scope themes
- invariants
- eligible feature rules
- rejected features
- gaps
- assumptions
- source map
- compiler version and prompt hash

This artifact is not canonical until accepted.

### Acceptance

Acceptance is an explicit decision that the compiled authority correctly
represents the reviewed spec. It writes an accepted `SpecAuthorityAcceptance`
row with `policy="human"`, the reviewer identity, compiler provenance, prompt
hash, and spec hash.

Acceptance is not a generic quality score. It is a canonicalization decision.
After acceptance, downstream workflow phases can trust that authority as the
project's current spec contract. In the current data model, canonical authority
is a projection derived from an accepted decision row and the matching compiled
authority row; this slice does not require adding a stored
`Product.authority_id` field.

### Rejection

Rejection is an explicit decision that the pending compiled authority must not
be used as canonical. It writes a rejected `SpecAuthorityAcceptance` row with a
required rationale and leaves the project in setup. Rejection makes the
next action clear: update the spec or recompile in a future command slice.

### Review Token

The review token is a deterministic freshness receipt returned by
`authority review`. It binds a later decision request to a specific
authority/spec/workflow snapshot. It does not prove that the actor read the
review packet. The token is a non-secret guard value, not an authentication or
authorization bearer token.

The token string has this format:

```text
agileforge.authority_review.v1:sha256:<digest>
```

`<digest>` is SHA-256 over canonical JSON with sorted keys, no insignificant
whitespace, UTF-8 encoding, and this namespaced payload:

- project id
- pending authority id
- pending authority fingerprint
- source spec hash used by the compiled authority
- current disk spec hash
- canonical resolved spec path
- compiler version
- prompt hash
- workflow `fsm_state`
- workflow `setup_status`
- review token schema version: `agileforge.authority_review.v1`

Human commands use the review token so humans do not manually type fingerprints
or pending ids. Expert and agent commands support either the review token or
explicit guard fields. The dashboard must combine the review token with normal
dashboard authentication and CSRF protections; the review token alone must never
authorize a decision.

### Decision Record

The existing `SpecAuthorityAcceptance` table already models decisions with
`status="accepted"` or `status="rejected"`. This slice keeps that table and
formalizes it as an append-only authority decision log. The implementation must
add decision-time provenance fields for the reviewed authority fingerprint,
review token or review fingerprint, source spec hash, disk spec hash, actor,
and actor mode.

All code must read accepted authority through repository or service methods that
filter `status="accepted"`. Direct ad hoc table reads that treat any row in
`spec_authority_acceptance` as acceptance are forbidden. Rejected rows must
never satisfy accepted-authority checks, never unlock Vision, and never appear
as canonical authority in read projections.

A future schema cleanup can rename the table to `SpecAuthorityDecision`, but
that rename is not required to unblock this feature.

### Review Packet

The review packet is the evidence bundle an agent or human needs before making
a decision. It must include both the source spec evidence and the compiled
interpretation, plus stable guard tokens for the reviewed pending authority.

## Command UX

The CLI has two command modes:

- Human mode: short commands, no manual fingerprints, no manual idempotency key.
- Expert/agent mode: explicit ids, fingerprints, and idempotency keys for
  deterministic automation and retry safety.

### Review

```bash
agileforge authority review --project-id 4
agileforge authority review --project-id 4 --format text
agileforge authority review --project-id 4 --include-spec full
```

Default JSON output returns the full structured review packet. Text output is
optimized for a human reading in a terminal.

The review command is read-only. It never records that a review happened and
never changes workflow state.

### Accept

Human mode:

```bash
agileforge authority accept --project-id 4 --review-token agileforge.authority_review.v1:sha256:...
agileforge authority accept --project-id 4
```

The first form is the non-interactive human command. The second form is allowed
only when stdin is an interactive terminal; it displays the pending authority
summary, fingerprint, and source spec hash, then requires an explicit typed
confirmation before submitting the same guarded decision request internally.

Expert/agent mode:

```bash
agileforge authority accept \
  --project-id 4 \
  --pending-authority-id 4 \
  --expected-authority-fingerprint sha256:... \
  --expected-source-spec-hash sha256:... \
  --expected-disk-spec-hash sha256:... \
  --expected-state SETUP_REQUIRED \
  --idempotency-key agent-unique-key
```

In human mode, AgileForge resolves the current pending authority, verifies the
review token or interactive confirmation against the current pending authority,
uses domain idempotence for already-accepted authority, and generates internal
tracing metadata. Humans do not type idempotency keys.

In expert/agent mode, the command fails if the review token or explicit pending
authority id, fingerprint, source spec hash, or expected workflow state no
longer matches what the agent reviewed.

### Reject

Human mode:

```bash
agileforge authority reject \
  --project-id 4 \
  --review-token agileforge.authority_review.v1:sha256:... \
  --reason "It missed the token storage rule."
agileforge authority reject --project-id 4
```

The interactive reject form requires the user to enter a rejection rationale
after displaying the pending authority summary.

Expert/agent mode:

```bash
agileforge authority reject \
  --project-id 4 \
  --pending-authority-id 4 \
  --expected-authority-fingerprint sha256:... \
  --expected-source-spec-hash sha256:... \
  --expected-disk-spec-hash sha256:... \
  --expected-state SETUP_REQUIRED \
  --reason "It missed the token storage rule." \
  --idempotency-key agent-unique-key
```

Rejection requires a rationale in both modes.

## Review Packet Contract

`agileforge authority review --project-id <id>` returns:

```json
{
  "project": {
    "project_id": 4,
    "name": "caRtola",
    "fsm_state": "SETUP_REQUIRED",
    "setup_status": "authority_pending_review"
  },
  "spec": {
    "spec_version_id": 4,
    "content_ref": "/absolute/path/to/spec.md",
    "resolved_path": "/absolute/path/to/spec.md",
    "spec_hash": "sha256...",
    "disk_status": "readable",
    "disk_sha256": "sha256...",
    "size_bytes": 12345,
    "source_outline": [
      {
        "section_id": "S1",
        "heading": "Submission Contract",
        "line_start": 12,
        "line_end": 48,
        "coverage_status": "covered | partial | uncovered",
        "covered_by": ["INV-1", "INV-2"]
      }
    ],
    "coverage_summary": {
      "covered_sections": 8,
      "partial_sections": 1,
      "uncovered_sections": 0,
      "omission_assessment": "complete | incomplete"
    },
    "excerpt": "...",
    "content_included": false,
    "content_truncated": false
  },
  "pending_authority": {
    "authority_id": 4,
    "spec_version_id": 4,
    "authority_fingerprint": "sha256:...",
    "compiler_version": "1.0.0",
    "prompt_hash": "sha256...",
    "compiled_at": "2026-05-16T17:37:22Z",
    "artifact": {
      "domain": {},
      "scope_themes": [],
      "invariants": [
        {
          "id": "INV-1",
          "text": "REQUIRED_FIELD:submission_plan.json",
          "support": "direct | inferred",
          "source_refs": [],
          "source_excerpt": "..."
        }
      ],
      "eligible_feature_rules": [],
      "rejected_features": [],
      "gaps": [
        {
          "id": "GAP-1",
          "text": "...",
          "source_refs": [],
          "source_excerpt": "..."
        }
      ],
      "assumptions": [
        {
          "id": "ASM-1",
          "text": "...",
          "support": "direct | inferred",
          "source_refs": []
        }
      ],
      "source_map": {}
    }
  },
  "review_guidance": {
    "decision_question": "Does this compiled interpretation correctly represent the spec?",
    "acceptance_statement": "Yes, this compiled interpretation correctly represents the spec. Use it as the canonical authority for later phases.",
    "checklist": [
      "Every mandatory requirement in the spec appears in the authority or is intentionally represented by a broader invariant.",
      "No authority invariant invents a requirement that is absent from the spec.",
      "Forbidden capabilities and security constraints are captured.",
      "Known gaps are real gaps, not missed requirements.",
      "The source map points back to directly supporting spec sections."
    ],
    "assessment_schema": {
      "recommendation": "accept | reject | needs_human",
      "confidence": "high | medium | low",
      "summary": "string",
      "blocking_findings": [],
      "non_blocking_findings": [],
      "missing_requirements": [],
      "invented_requirements": [],
      "gap_assessment": [],
      "decision_rationale": "string"
    }
  },
  "next_actions": [
    {
      "command": "agileforge authority accept --project-id 4 --review-token agileforge.authority_review.v1:sha256:...",
      "mode": "human",
      "reason": "Record the reviewed pending authority as canonical."
    },
    {
      "command": "agileforge authority reject --project-id 4 --review-token agileforge.authority_review.v1:sha256:... --reason \"...\"",
      "mode": "human",
      "reason": "Record that the pending authority must not be used."
    }
  ],
  "guard_tokens": {
    "review_token": "agileforge.authority_review.v1:sha256:...",
    "pending_authority_id": 4,
    "expected_authority_fingerprint": "sha256:...",
    "expected_source_spec_hash": "sha256:...",
    "expected_disk_spec_hash": "sha256:...",
    "expected_state": "SETUP_REQUIRED",
    "expected_setup_status": "authority_pending_review"
  }
}
```

Default JSON output includes full source content when the spec file is readable
and within the configured default review size limit. When full source content is
omitted because the file exceeds that limit or the caller requested a summarized
packet, the packet must include a complete source outline, section-level
coverage status, bounded source excerpts, compiled authority fields, and
source-map evidence. If full source is omitted, the packet must set
`coverage_summary.omission_assessment="incomplete"` unless the source outline
coverage rules can prove every source section is covered or intentionally
classified.

Every invariant, gap, assumption, rejected feature, and eligibility rule must
include `support`, `source_refs`, and `source_excerpt` fields. Items without
direct source support must use `support="inferred"`, `source_refs=[]`, and a
source excerpt only when one exists, so reviewers can distinguish source-backed
facts from compiler interpretation.

## Decision Behavior

Accept and reject must go through one shared authority decision service or
runner. The CLI, API, and dashboard must not duplicate decision logic.

Before recording either decision, the service must validate:

- the pending authority id is still the current pending authority for the latest
  project spec
- the pending authority fingerprint matches the review token or explicit
  expected fingerprint
- the source spec hash recorded with the pending authority still matches the
  source spec hash in the review token
- the current disk spec hash still matches the reviewed disk spec hash
- `fsm_state=="SETUP_REQUIRED"`
- `setup_status=="authority_pending_review"`
- no terminal decision already exists for the same project id, spec version id,
  and pending authority id

If any validation fails, the command returns a stale structured error and tells
the reviewer to run `agileforge authority review --project-id <id>` again.

Decision writes must happen inside one transactional boundary that acquires a
SQLite write lock before validation and uses conditional writes against the
project id, spec version id, pending authority id, authority fingerprint, source
spec hash, disk spec hash, `fsm_state`, and `setup_status`. The mutation must
finalize exactly one terminal decision. If another process wins the terminal
decision first, the losing command returns `AUTHORITY_ALREADY_DECIDED`.

A repeated accept for the same project, spec version, pending authority,
fingerprint, source hashes, and decision returns the original accepted decision.
A repeated reject with the same project, spec version, pending authority,
fingerprint, source hashes, decision, and rationale returns the original
rejected decision. Any changed guard value or opposite terminal decision fails
with `AUTHORITY_ALREADY_DECIDED`.

If the disk spec is missing, unreadable, moved to a different canonical resolved
path, or has a different hash at decision time, the service must fail with
`AUTHORITY_SOURCE_CHANGED` or the more specific `SPEC_FILE_NOT_FOUND` /
`SPEC_FILE_INVALID` code and require a fresh review. It must not accept or
reject authority against an unverified source file.

### Accept Success

On successful acceptance:

- Write an accepted decision row for the pending spec version.
- Store the reviewed authority fingerprint, review token or review fingerprint,
  source spec hash, disk spec hash, actor, actor mode, policy, and rationale
  when provided.
- Promote the accepted authority in the canonical read projection: after commit,
  `authority status` must return `status="current"`, `authority_id` equal to the
  accepted compiled authority id, `accepted_spec_version_id` equal to the
  accepted spec version, `pending_authority_id=null`, and
  `authority_fingerprint` for the accepted authority.
- Set `setup_status="passed"`.
- Clear setup error fields.
- Advance the workflow by evaluating setup completion. In the current FSM, a
  fully accepted setup advances to `VISION_INTERVIEW` only after the canonical
  authority projection above is true.
- Return `authority_id`, `accepted_decision_id`, `accepted_spec_version_id`,
  `authority_fingerprint`, and next Vision actions.

### Reject Success

On successful rejection:

- Write a rejected decision row for the pending spec version.
- Store the reviewed authority fingerprint, review token or review fingerprint,
  source spec hash, disk spec hash, actor, actor mode, policy, and required
  rationale.
- Set `setup_status="authority_rejected"`.
- Keep `fsm_state="SETUP_REQUIRED"`.
- Store or expose the rejection rationale in setup/status projections.
- Return next actions that explain that spec update or recompile is required in
  a future command slice.

### Already Decided

If the pending authority was already accepted with the same decision guards,
`accept` returns the existing accepted decision instead of creating a duplicate.
If it was already rejected, `accept` fails with `AUTHORITY_ALREADY_DECIDED`.

If the pending authority was already rejected with the same decision guards and
rationale, `reject` returns the existing rejection instead of creating a
duplicate. If it was already accepted, `reject` fails with
`AUTHORITY_ALREADY_DECIDED`.

## Workflow Next Behavior

When authority status is `pending_acceptance`, `agileforge workflow next` must
return authority review as the next valid command and decision command templates
that require a review token:

```json
{
  "next_valid_commands": [
    "agileforge authority review --project-id 4"
  ],
  "decision_commands_after_review": [
    "agileforge authority accept --project-id 4 --review-token <review_token>",
    "agileforge authority reject --project-id 4 --review-token <review_token> --reason \"...\""
  ]
}
```

When authority is rejected, `workflow next` must not advertise Vision. It must
advertise the future spec update/recompile command as unavailable until that
slice is implemented.

## Output Formats

JSON remains the default output format for agent reliability. Human readability
is explicit:

```bash
agileforge authority review --project-id 4 --format text
```

Accept/reject success can also support `--format text`, but the JSON envelope
remains canonical.

## Error Handling

Required structured errors:

- `PROJECT_NOT_FOUND`
- `AUTHORITY_NOT_COMPILED`
- `AUTHORITY_NOT_ACCEPTED`
- `AUTHORITY_REVIEW_REQUIRED`
- `STALE_STATE`
- `STALE_ARTIFACT_FINGERPRINT`
- `STALE_CONTEXT_FINGERPRINT`
- `SPEC_FILE_NOT_FOUND`
- `SPEC_FILE_INVALID`
- `IDEMPOTENCY_KEY_REUSED`
- `MUTATION_IN_PROGRESS`
- `MUTATION_RECOVERY_REQUIRED`
- `MUTATION_FAILED`

Add these authority-specific errors:

- `AUTHORITY_NOT_PENDING`
- `AUTHORITY_ALREADY_DECIDED`
- `AUTHORITY_SOURCE_CHANGED`

Actor identity must be explicit in each decision response and persisted decision
record. CLI human decisions default to the local OS username and actor mode
`cli-human`; if the OS username cannot be resolved, the command must use
`cli-human` as the actor value. CLI agent decisions default to `cli-agent`
unless `--changed-by` is supplied; dashboard decisions use the authenticated
dashboard user and actor mode `dashboard-human`; automated tests use actor mode
`test`.

Human-mode commands must still return structured errors, but remediation must
use simple next commands. A stale accept must return this remediation:

```text
Run agileforge authority review --project-id 4 again, then accept or reject the current pending authority.
```

## Dashboard Behavior

The dashboard must treat `setup_status=authority_pending_review` as a distinct
successful setup checkpoint:

- Title: `Pending Authority Review`
- Message: setup compiled successfully and needs review before Vision.
- Show review evidence or a link/action to the authority review view.
- Show Accept and Reject actions that submit the exact review token or
  fingerprint rendered on the page.
- Show a stale-review message requiring reload if the authority or source spec
  changed after the page was rendered.
- Do not label this state as `Project Setup Required`.

The dashboard must treat `setup_status=authority_rejected` as:

- Title: `Authority Rejected`
- Message: authority was rejected and Vision remains locked until spec update or
  recompile is available.

## Testing Expectations

Tests must prove:

- `authority review` returns full pending authority evidence for project `4`
  style states.
- Review output includes full compiled artifact fields, source spec hash, disk
  status, evidence fields, guard tokens, review token, and assessment schema.
- Default review output includes full source content under the configured size
  limit.
- Review output that omits full source content includes a complete source
  outline, section coverage markers, bounded excerpts, and an explicit
  `omission_assessment` value.
- Human non-interactive accept requires a review token but does not require
  manually typing `pending_authority_id`, fingerprint, or idempotency key.
- Human interactive accept submits the same guarded decision request after typed
  confirmation.
- Expert accept rejects stale pending authority fingerprints.
- Accept and reject reject changed disk spec hashes after review.
- Accept and reject reject missing, unreadable, moved, or oversized unverified
  disk specs at decision time.
- Accept and reject reject changed pending authority ids after review.
- Accept-after-reject and reject-after-accept fail with
  `AUTHORITY_ALREADY_DECIDED`.
- Accept writes exactly one accepted decision and advances workflow to
  `VISION_INTERVIEW`.
- Accept promotes the canonical authority projection so `authority status`
  returns `status="current"`, accepted `authority_id`, accepted spec version,
  and no pending authority.
- Reject writes a rejected decision, keeps Vision locked, and changes setup
  status to `authority_rejected`.
- Rejected decision rows never satisfy accepted-authority reads and never unlock
  Vision.
- Concurrent accept/reject attempts record exactly one winning decision.
- `workflow next` advertises review while authority is pending and exposes
  accept/reject templates that require review tokens.
- Dashboard copy distinguishes pending review from setup failure.
- Dashboard stale-review submissions fail with a reload/review-again message.
- Existing project create behavior still ends in `authority_pending_review`.

## Revision History

- 2026-05-17: Initial design for authority review, accept, reject, workflow-next,
  and dashboard states.
- 2026-05-17: Incorporated external critique by replacing unguarded
  `--confirm-reviewed` human accept with review-token or interactive guarded
  decisions, adding decision-time source-spec freshness checks, evidence
  requirements, and authority-aware workflow-next routing.
- 2026-05-17: Tightened canonical authority projection, review-token semantics,
  source coverage requirements, terminal-decision concurrency, rejected-row
  safety, and decision-time source-file failure behavior.

## Open Follow-Up

Project spec update and recompile remain necessary after rejection, but they are
a separate workflow slice. This design intentionally stops at making the current
pending authority checkpoint reviewable and decidable.
