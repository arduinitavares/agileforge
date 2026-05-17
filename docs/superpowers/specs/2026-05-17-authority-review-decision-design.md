# Authority Review And Decision Design

**Date:** 2026-05-17
**Status:** Draft for user review
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
project's current spec contract.

### Rejection

Rejection is an explicit decision that the pending compiled authority must not
be used as canonical. It writes a rejected `SpecAuthorityAcceptance` row with a
required rationale and leaves the project in setup. Rejection makes the
next action clear: update the spec or recompile in a future command slice.

### Review Packet

The review packet is the evidence bundle an agent or human needs before making
a decision. It must include both the source spec evidence and the compiled
interpretation, plus a stable fingerprint of the pending authority.

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
agileforge authority accept --project-id 4 --confirm-reviewed
```

Expert/agent mode:

```bash
agileforge authority accept \
  --project-id 4 \
  --pending-authority-id 4 \
  --expected-authority-fingerprint sha256:... \
  --expected-state SETUP_REQUIRED \
  --idempotency-key agent-unique-key
```

In human mode, AgileForge resolves the current pending authority, recomputes the
current fingerprint, uses domain idempotence for already-accepted authority, and
generates internal tracing metadata. Humans do not type idempotency keys.

In expert/agent mode, the command fails if the pending authority id,
fingerprint, or expected workflow state no longer matches what the agent
reviewed.

### Reject

Human mode:

```bash
agileforge authority reject --project-id 4 --reason "It missed the token storage rule."
```

Expert/agent mode:

```bash
agileforge authority reject \
  --project-id 4 \
  --pending-authority-id 4 \
  --expected-authority-fingerprint sha256:... \
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
    "spec_hash": "sha256...",
    "disk_status": "readable",
    "disk_sha256": "sha256...",
    "content": "...",
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
      "invariants": [],
      "eligible_feature_rules": [],
      "rejected_features": [],
      "gaps": [],
      "assumptions": [],
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
      "The source map points back to relevant spec sections where possible."
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
      "command": "agileforge authority accept --project-id 4 --confirm-reviewed",
      "mode": "human",
      "reason": "Record the reviewed pending authority as canonical."
    },
    {
      "command": "agileforge authority reject --project-id 4 --reason \"...\"",
      "mode": "human",
      "reason": "Record that the pending authority must not be used."
    }
  ],
  "guard_tokens": {
    "pending_authority_id": 4,
    "expected_authority_fingerprint": "sha256:...",
    "expected_state": "SETUP_REQUIRED"
  }
}
```

The JSON packet includes spec content by default when it is readable and within
the configured spec size limit. Text output summarizes the source path and
compiled fields, and can include full spec content when `--include-spec full` is
passed.

## Decision Behavior

### Accept Success

On successful acceptance:

- Write an accepted decision row for the pending spec version.
- Set `setup_status="passed"`.
- Clear setup error fields.
- Set `fsm_state="VISION_INTERVIEW"`.
- Return `authority_id`, `accepted_decision_id`, `accepted_spec_version_id`,
  `authority_fingerprint`, and next Vision actions.

### Reject Success

On successful rejection:

- Write a rejected decision row for the pending spec version.
- Set `setup_status="authority_rejected"`.
- Keep `fsm_state="SETUP_REQUIRED"`.
- Store or expose the rejection rationale in setup/status projections.
- Return next actions that explain that spec update or recompile is required in
  a future command slice.

### Already Decided

If the pending authority was already accepted, `accept` returns the existing
accepted decision instead of creating a duplicate.

If the latest spec version was already rejected, `reject` returns the existing
rejection when the rationale and reviewed authority match; otherwise it fails
with a structured conflict so the user can inspect current authority status.

## Workflow Next Behavior

When authority status is `pending_acceptance`, `agileforge workflow next` must
return authority review and decision actions before any phase-planning actions:

```json
{
  "next_valid_commands": [
    "agileforge authority review --project-id 4",
    "agileforge authority accept --project-id 4 --confirm-reviewed",
    "agileforge authority reject --project-id 4 --reason \"...\""
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
- `STALE_STATE`
- `STALE_ARTIFACT_FINGERPRINT`
- `IDEMPOTENCY_KEY_REUSED`
- `MUTATION_IN_PROGRESS`
- `MUTATION_RECOVERY_REQUIRED`
- `MUTATION_FAILED`

Add these authority-specific errors:

- `AUTHORITY_NOT_PENDING`
- `AUTHORITY_ALREADY_DECIDED`
- `AUTHORITY_REVIEW_REQUIRED`

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
- Show Accept and Reject actions.
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
  status, guard tokens, and assessment schema.
- Human accept does not require `pending_authority_id`, fingerprint, or
  idempotency key.
- Expert accept rejects stale pending authority fingerprints.
- Accept writes exactly one accepted decision and advances workflow to
  `VISION_INTERVIEW`.
- Reject writes a rejected decision, keeps Vision locked, and changes setup
  status to `authority_rejected`.
- `workflow next` advertises review/accept/reject while authority is pending.
- Dashboard copy distinguishes pending review from setup failure.
- Existing project create behavior still ends in `authority_pending_review`.

## Open Follow-Up

Project spec update and recompile remain necessary after rejection, but they are
a separate workflow slice. This design intentionally stops at making the current
pending authority checkpoint reviewable and decidable.
