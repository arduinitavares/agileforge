# Authority Review And Decision Design

**Date:** 2026-05-17
**Status:** Accepted
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
row with an explicit decision policy, the reviewer identity, compiler
provenance, prompt hash, and spec hash.

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
- source content inclusion flag
- source coverage omission assessment
- source coverage summary digest
- review token schema version: `agileforge.authority_review.v1`

The canonical payload uses these field names and scalar types:

```json
{
  "schema": "agileforge.authority_review.v1",
  "project_id": 4,
  "pending_authority_id": 4,
  "authority_fingerprint": "sha256:...",
  "source_spec_hash": "sha256:...",
  "disk_spec_hash": "sha256:...",
  "resolved_spec_path": "/absolute/path/to/spec.md",
  "compiler_version": "1.0.0",
  "prompt_hash": "sha256:...",
  "fsm_state": "SETUP_REQUIRED",
  "setup_status": "authority_pending_review",
  "content_included": true,
  "omission_assessment": "complete",
  "coverage_summary_fingerprint": "sha256:..."
}
```

Human commands use the review token so humans do not manually type fingerprints
or pending ids. Expert and agent commands support either the review token or
the complete explicit guard set. Accept commands must include either a review
token or a complete explicit guard set including authority, source, workflow,
and review-completeness guards. Explicit accept requests that omit any
review-completeness guard fail before decision validation. Explicit
review-completeness guards must match the service-computed review snapshot for
the current pending authority, source spec, disk spec, and workflow state. The
dashboard must combine the review token with normal dashboard authentication and
CSRF protections; the review token alone must never authorize a decision.

### Coverage Summary Fingerprint

`coverage_summary_fingerprint` has this format:

```text
sha256:<digest>
```

`<digest>` is SHA-256 over canonical JSON with sorted keys, no insignificant
whitespace, UTF-8 encoding, and this namespaced payload:

```json
{
  "schema": "agileforge.authority_coverage_summary.v1",
  "spec_version_id": 4,
  "resolved_spec_path": "/absolute/path/to/spec.md",
  "source_content_sha256": "sha256:...",
  "content_included": true,
  "content_truncated": false,
  "source_outline": [
    {
      "section_id": "S1",
      "heading": "Submission Contract",
      "line_start": 12,
      "line_end": 48,
      "coverage_status": "covered",
      "covered_by": ["INV-1", "INV-2"],
      "classification_reason": null
    }
  ],
  "coverage_summary": {
    "covered_sections": 8,
    "partial_sections": 0,
    "intentionally_classified_sections": 1,
    "uncovered_sections": 0,
    "unclassified_content_blocks": 0,
    "omission_assessment": "complete"
  }
}
```

The `source_outline` array is ordered by `line_start`, then `section_id`.
Every nested list in the fingerprint payload must be canonicalized before
hashing. `covered_by`, `source_refs`, and classification id arrays are sorted
lexicographically after converting ids to strings. Duplicate ids are removed.
Diagnostics are sorted by `section_id`, then diagnostic code, then message.
Completeness is true only when every parsed source section has
`coverage_status` in `covered|intentionally_classified`, and the parser reports
`unclassified_content_blocks=0`. If the outline parser cannot parse a section,
cannot classify a content block, or cannot prove coverage for any section,
`omission_assessment` must be `incomplete` and the review packet must include a
machine-readable coverage diagnostic. Review packet generation does not fail for
malformed Markdown structure; it fails only when the source file is missing,
unreadable, too large for the requested output mode, or cannot be decoded by the
source decode policy.

Section parsing uses Markdown heading boundaries. Content before the first
heading is represented as a synthetic section with `section_id="ROOT"`.
Tables, code blocks, paragraphs, and list blocks are content blocks inside their
nearest section. A requirement-bearing block is any paragraph, list item, table
row, or fenced code block line that contains normative language such as `must`,
`required`, `shall`, `only`, `never`, `cannot`, `forbidden`, `accepted when`,
`rejected when`, `input`, `output`, `schema`, `field`, or `constraint`, or that
appears under a heading containing `requirements`, `invariants`, `rules`,
`acceptance`, `security`, `scope`, `out of scope`, `schema`, or `contract`. A
section is `covered` only when each requirement-bearing block has at least one
`source_ref` from an authority item or an explicit non-authority classification.
A section is `intentionally_classified` only when every non-covered
requirement-bearing block is represented by a gap, assumption, rejected feature,
or out-of-scope classification with a non-empty `classification_reason`.
`classification_reason` must identify the authority item id or classifier
result that explains why the block is not represented as an invariant or rule.
`partial` and `uncovered` sections make omission assessment incomplete.

### Decision Record

The existing `SpecAuthorityAcceptance` table already models decisions with
`status="accepted"` or `status="rejected"`. This slice keeps that table and
formalizes it as an append-only authority decision log. The implementation must
add decision-time provenance fields for the reviewed authority fingerprint,
review token or review fingerprint, source spec hash, disk spec hash, actor,
actor mode, decision policy, review completeness, and incomplete-review override
rationale when applicable.

Allowed decision policy values are:

- `manual`: CLI human decision using a review token.
- `agent_requested`: non-interactive expert or agent decision.
- `dashboard_manual`: authenticated dashboard human decision.
- `test`: automated test fixture or test-only command path.

Allowed actor mode values are:

- `cli-human`
- `cli-agent`
- `dashboard-human`
- `test`

All code must read accepted authority through repository or service methods that
filter `status="accepted"`. Direct ad hoc table reads that treat any row in
`spec_authority_acceptance` as acceptance are forbidden. Rejected rows must
never satisfy accepted-authority checks, never unlock Vision, and never appear
as canonical authority in read projections.

The storage migration for this slice must add the provenance columns named in
this section, including `pending_authority_id`, and must add this SQLite
uniqueness invariant for terminal decisions:

```sql
-- table constraint in the rebuilt spec_authority_acceptance table
CONSTRAINT ck_spec_authority_terminal_pending_not_null CHECK (
  status NOT IN ('accepted', 'rejected')
  OR pending_authority_id IS NOT NULL
);

CREATE UNIQUE INDEX uq_spec_authority_terminal_decision
ON spec_authority_acceptance (
  product_id,
  spec_version_id,
  pending_authority_id
)
WHERE status IN ('accepted', 'rejected');
```

Terminal decision rows require non-null `pending_authority_id`. SQLite allows
multiple `NULL` values in unique indexes, so the non-null invariant is part of
the correctness contract, not a convenience. The migration must enforce it with
a table rebuild, a checked normalized decision key, or another SQLite-valid
constraint that schema readiness can verify.

If a future storage backend cannot express a partial unique index by terminal
status, its migration must add an equivalent normalized decision-key column or
service-enforced unique index and schema readiness must verify it. Existing
databases must fail `schema check` until the provenance columns and terminal
decision invariant exist.

Migration must handle historical accepted rows created before this slice. If a
historical accepted row can be joined to exactly one compiled authority for its
`spec_version_id`, the migration backfills `pending_authority_id` with that
compiled authority id and marks the provenance source as `legacy_backfill`. If
the row cannot be joined unambiguously, schema migration must stop with a
structured migration error and remediation that tells the operator to resolve or
archive the ambiguous historical decision before authority decisions are
enabled. Rejected historical rows without `pending_authority_id` are not
expected before this slice; if present, they follow the same backfill-or-block
rule. Non-terminal legacy rows are allowed to keep `pending_authority_id=null`.

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
confirmation before submitting the same guarded decision request internally. The
normal acceptance confirmation phrase is:

```text
ACCEPT AUTHORITY
```

If the review is incomplete and the interactive user chooses the explicit
override path, the confirmation phrase is:

```text
ACCEPT INCOMPLETE AUTHORITY
```

Interactive override also requires a non-empty rationale before submission.

Expert/agent mode:

```bash
agileforge authority accept \
  --project-id 4 \
  --pending-authority-id 4 \
  --expected-authority-fingerprint sha256:... \
  --expected-source-spec-hash sha256:... \
  --expected-disk-spec-hash sha256:... \
  --expected-resolved-spec-path /absolute/path/to/spec.md \
  --expected-state SETUP_REQUIRED \
  --expected-setup-status authority_pending_review \
  --expected-content-included true \
  --expected-omission-assessment complete \
  --expected-coverage-summary-fingerprint sha256:... \
  --idempotency-key agent-unique-key
```

Incomplete-review override:

```bash
agileforge authority accept \
  --project-id 4 \
  --review-token agileforge.authority_review.v1:sha256:... \
  --allow-incomplete-review \
  --incomplete-review-rationale "Full source exceeded the review packet limit; reviewed the attached source file separately."
```

In human mode, AgileForge resolves the current pending authority, verifies the
review token or interactive confirmation against the current pending authority,
checks for an existing decision matching the reviewed guard tuple before current
pending-state validation, and generates internal tracing metadata. Humans do not
type idempotency keys.

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
  --expected-resolved-spec-path /absolute/path/to/spec.md \
  --expected-state SETUP_REQUIRED \
  --expected-setup-status authority_pending_review \
  --expected-content-included true \
  --expected-omission-assessment complete \
  --expected-coverage-summary-fingerprint sha256:... \
  --reason "It missed the token storage rule." \
  --idempotency-key agent-unique-key
```

Rejection requires a rationale in both modes.

Explicit reject does not require review-completeness guards because it does not
canonicalize authority. If an explicit reject request supplies
`expected_content_included`, `expected_omission_assessment`, or
`expected_coverage_summary_fingerprint`, each supplied value must match the
service-computed review snapshot. Source, pending authority, and workflow guards
remain required for explicit reject, including `expected_resolved_spec_path`.

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
    "review_source_limit_bytes": 262144,
    "source_outline": [
      {
        "section_id": "S1",
        "heading": "Submission Contract",
        "line_start": 12,
        "line_end": 48,
        "coverage_status": "covered | intentionally_classified | partial | uncovered",
        "covered_by": ["INV-1", "INV-2"],
        "classification_reason": null
      }
    ],
    "coverage_summary": {
      "covered_sections": 8,
      "partial_sections": 1,
      "intentionally_classified_sections": 0,
      "uncovered_sections": 0,
      "unclassified_content_blocks": 0,
      "omission_assessment": "complete | incomplete"
    },
    "coverage_diagnostics": [],
    "excerpt": "...",
    "content_included": true,
    "content_truncated": false,
    "source_content": "# Submission Contract\n...",
    "source_content_sha256": "sha256:..."
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
    "expected_resolved_spec_path": "/absolute/path/to/spec.md",
    "expected_state": "SETUP_REQUIRED",
    "expected_setup_status": "authority_pending_review",
    "expected_content_included": true,
    "expected_omission_assessment": "complete",
    "expected_coverage_summary_fingerprint": "sha256:..."
  }
}
```

Default JSON output includes full source content when the spec file is readable
and its raw byte length is at or below the default review source limit:
`262144` bytes. The limit is configurable through
`AGILEFORGE_AUTHORITY_REVIEW_SOURCE_LIMIT_BYTES` and must be reported in the
review packet metadata. When full source content is omitted because the file
exceeds that limit or the caller requested a summarized packet, the packet must
include a complete source outline, section-level coverage status, bounded source
excerpts, compiled authority fields, and source-map evidence. If full source is
omitted, the packet must set
`coverage_summary.omission_assessment="incomplete"` unless every parsed source
section has `coverage_status` in `covered|intentionally_classified` and
`unclassified_content_blocks=0`.

When `content_included=true`, `source_content` must contain the full source text
used for review. The source decode policy is strict UTF-8 with no Unicode or
newline normalization; decode failure returns `SPEC_FILE_INVALID`. When
`content_included=false`, `source_content` and `source_content_sha256` must be
`null`. `disk_sha256` is SHA-256 over the exact raw bytes read from the resolved
disk path. `source_content_sha256` is SHA-256 over `source_content` re-encoded
as UTF-8 exactly as returned in the JSON payload. `source_spec_hash` is the
persisted hash recorded on the `SpecRegistry` row for the compiled spec version;
the decision service must compare all relevant hashes because registry content,
current disk content, and rendered review content can drift independently.

Every invariant, gap, assumption, rejected feature, and eligibility rule must
include `support`, `source_refs`, and `source_excerpt` fields. Items without
direct source support must use `support="inferred"`, `source_refs=[]`, and a
source excerpt only when one exists, so reviewers can distinguish source-backed
facts from compiler interpretation.

## Decision Behavior

Accept and reject must go through one shared authority decision service or
runner. The CLI, API, and dashboard must not duplicate decision logic.
Token-mode and explicit-guard mode must both be normalized into one internal
`ReviewedAuthoritySnapshot` value before validation. The rest of the decision
service validates only that normalized value, so interactive CLI, noninteractive
CLI, API, and dashboard paths cannot drift.

Before recording either decision, the service must validate:

- the pending authority id is still the current pending authority for the latest
  project spec
- the pending authority fingerprint matches the review token or explicit
  expected fingerprint
- the source spec hash recorded with the pending authority still matches the
  source spec hash in the review token or explicit guard
- the current disk spec hash still matches the reviewed disk spec hash from the
  review token or explicit guard
- the current canonical resolved spec path still matches the resolved spec path
  from the review token or explicit guard
- `fsm_state=="SETUP_REQUIRED"`
- `setup_status=="authority_pending_review"`
- explicit accept requests without a review token include
  `expected_content_included`, `expected_omission_assessment`, and
  `expected_coverage_summary_fingerprint`
- supplied explicit review-completeness guards match the service-computed review
  snapshot generated with the same canonical review algorithm as
  `authority review`
- no terminal decision already exists for the same project id, spec version id,
  and pending authority id

If any stale validation fails, the command returns a stale structured error and
tells the reviewer to run `agileforge authority review --project-id <id>` again.
Missing explicit accept completeness guards return `AUTHORITY_GUARD_INCOMPLETE`.
Mismatched explicit completeness guards return `STALE_CONTEXT_FINGERPRINT` with
the expected and actual review-completeness values in error details.

Acceptance must also validate review completeness. If the reviewed packet has
`coverage_summary.omission_assessment="incomplete"`, accept must fail with
`AUTHORITY_REVIEW_INCOMPLETE` by default. An incomplete review can be accepted
only through an explicit override:

- interactive CLI human mode after the user sees the incomplete-review warning
  and types the required confirmation phrase
- non-interactive human, dashboard, or expert/agent mode with both
  `--allow-incomplete-review` and a non-empty
  `--incomplete-review-rationale`

The decision record must persist the override flag and rationale. Agent mode
defaults to failure; an agent-supplied override is treated as an explicit
`agent_requested` policy decision, not as normal accept behavior. Rejection does
not require complete omission assessment because rejection does not canonicalize
authority, but it still uses the same source-freshness checks so the decision
log refers to a verified reviewed snapshot.

Decision handling must check replayable prior results before current pending
state validation. The order is:

1. If an idempotency key is present, load the stored mutation by command and
   key. Same key plus same canonical request hash returns the stored response;
   same key plus different canonical request hash returns
   `IDEMPOTENCY_KEY_REUSED`.
2. If no idempotency replay applies, look for an existing terminal decision
   matching the exact review token or explicit guard tuple, decision, decision
   policy, and rationale where relevant. A match returns the stored decision
   response even if `pending_authority_id` has since cleared and setup advanced.
3. Only when no replayable decision exists does the service validate current
   pending state and write a new decision.

Decision writes must happen inside one transactional boundary that acquires a
SQLite write lock before validation and uses conditional writes against the
project id, spec version id, pending authority id, authority fingerprint, source
spec hash, disk spec hash, `fsm_state`, and `setup_status`. The mutation must
finalize exactly one terminal decision. If another process wins the terminal
decision first, the losing command returns `AUTHORITY_ALREADY_DECIDED`.

The canonical request hash for idempotency must include the command name,
decision, project id, pending authority id, review token or explicit guard
tuple, expected state, expected setup status, source hashes,
`expected_resolved_spec_path`, `expected_content_included`,
`expected_omission_assessment`,
`expected_coverage_summary_fingerprint`, decision policy, actor mode,
incomplete-review override flag, incomplete-review rationale, and rejection
rationale. It must exclude correlation id, generated timestamps, and other
tracing-only metadata.

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
- Store `review_completeness`, incomplete-review override flag, and
  incomplete-review override rationale when applicable.
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
- Store `review_completeness` and the reviewed packet's omission assessment.
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
- `AUTHORITY_GUARD_INCOMPLETE`
- `IDEMPOTENCY_KEY_REUSED`
- `MUTATION_IN_PROGRESS`
- `MUTATION_RECOVERY_REQUIRED`
- `MUTATION_FAILED`

Add these authority-specific errors:

- `AUTHORITY_NOT_PENDING`
- `AUTHORITY_ALREADY_DECIDED`
- `AUTHORITY_SOURCE_CHANGED`
- `AUTHORITY_REVIEW_INCOMPLETE`

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
- Show Accept and Reject actions that submit the exact review token rendered on
  the page, or the complete explicit guard set: pending authority id, authority
  fingerprint, source spec hash, disk spec hash, expected resolved spec path,
  expected state, expected setup status, expected content included flag,
  expected omission assessment, and expected coverage summary fingerprint.
  Authority fingerprint alone is a display value and is never a sufficient
  mutation guard.
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
  limit and reports `review_source_limit_bytes`.
- Review output that omits full source content includes a complete source
  outline, section coverage markers, bounded excerpts, and an explicit
  `omission_assessment` value.
- Malformed Markdown structure does not fail review generation; it emits
  coverage diagnostics and `omission_assessment="incomplete"`.
- Review output includes `source_content` and `source_content_sha256` when
  `content_included=true`; both are `null` when `content_included=false`.
- Hash fields follow the documented semantics for raw disk bytes, returned
  UTF-8 source content, and persisted `SpecRegistry` hash.
- Coverage summary fingerprints remain stable when `covered_by`, `source_refs`,
  or diagnostics are emitted in different input orders.
- Human non-interactive accept requires a review token but does not require
  manually typing `pending_authority_id`, fingerprint, or idempotency key.
- Human interactive accept submits the same guarded decision request after typed
  confirmation phrase as non-interactive accept.
- Interactive incomplete-review override requires the
  `ACCEPT INCOMPLETE AUTHORITY` phrase and a non-empty rationale.
- Accept fails with `AUTHORITY_REVIEW_INCOMPLETE` when the reviewed packet has
  `omission_assessment="incomplete"` and no explicit override rationale is
  provided.
- Explicit accept without a review token fails with
  `AUTHORITY_GUARD_INCOMPLETE` unless it provides
  `expected_content_included`, `expected_omission_assessment`, and
  `expected_coverage_summary_fingerprint`.
- Explicit accept with fabricated or stale completeness guards fails with
  `STALE_CONTEXT_FINGERPRINT`.
- Token-mode accept and explicit-guard accept make the same decision for the
  same authority/source/workflow/review-completeness snapshot.
- Explicit reject without review-completeness guards is allowed, while supplied
  reject completeness guards are match-validated.
- Explicit reject detects resolved-path drift through `expected_resolved_spec_path`.
- Incomplete-review accept override persists the override flag, rationale,
  actor mode, and decision policy.
- Expert accept rejects stale pending authority fingerprints.
- Same idempotency key and same canonical request hash returns the stored
  response; same key and different canonical request hash returns
  `IDEMPOTENCY_KEY_REUSED`.
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
- The database enforces one terminal decision per project/spec/pending authority
  tuple, and schema readiness fails when the terminal-decision uniqueness
  invariant is missing.
- Terminal decision rows with `pending_authority_id=NULL` fail schema or write
  validation, and duplicate null-key terminal rows cannot be inserted.
- Legacy accepted rows are backfilled only when they join to exactly one
  compiled authority; ambiguous historical rows block migration with structured
  remediation.
- `workflow next` advertises review while authority is pending and exposes
  accept/reject templates that require review tokens.
- Dashboard copy distinguishes pending review from setup failure.
- Dashboard mutation requests that submit only an authority fingerprint are
  rejected before the decision service writes anything.
- Dashboard stale-review submissions fail with a reload/review-again message.
- Decision retry after successful accept or reject replays the stored response
  before stale current-state validation.
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
- 2026-05-17: Added incomplete-review acceptance gating, dashboard guard
  tightening, source-content schema fields, decision replay ordering, and
  explicit policy/actor-mode values.
- 2026-05-17: Aligned explicit guard mode with review-token completeness,
  defined coverage summary fingerprinting and section coverage, and specified
  the terminal-decision uniqueness invariant.
- 2026-05-17: Required explicit completeness guards to match the recomputed
  review snapshot, added terminal non-null decision-key enforcement, clarified
  hash/decode semantics, and defined explicit-reject completeness behavior.
- 2026-05-17: Defined requirement-bearing blocks, canonical nested-array
  ordering, explicit resolved-path guards, interactive confirmation phrases,
  incomplete-review override examples, and legacy decision migration behavior.

## Open Follow-Up

Project spec update and recompile remain necessary after rejection, but they are
a separate workflow slice. This design intentionally stops at making the current
pending authority checkpoint reviewable and decidable.
