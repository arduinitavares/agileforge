# Guided accepted Backlog correction design

**Status:** Approved for implementation planning on 2026-09-04

**Baseline commit:** `56caf1cd6c36fcc43f174329c4dbeefac268da44`

## Objective

Let an operator correct an already accepted Backlog with explicit guidance while
preserving immutable history, exact workflow lineage, idempotent retries, and the
existing human review gate.

This is a reusable AgileForge capability for corrections discovered during
Roadmap review, before Story or Sprint planning has begun. The immediate P&ID
use case will use it once to split two overloaded Backlog items before Roadmap
generation resumes. The production code must not contain P&ID-specific item
names or split rules.

## The current failure

Roadmap Attempt 18 did not fail because of OpenRouter or malformed model output.
The Roadmap contract requires every Backlog item ID to appear exactly once. The
accepted Backlog contains two items whose work belongs in more than one
dependency stage:

- `PBI-000006` combines an early consent-audit foundation, a later governance
  gate, and final gold publication.
- `PBI-000009` combines early local bootstrap and mounted model assets with final
  release, backup, restore, and recovery proof.

For example, consent audit storage must exist before consent-gated ingestion,
but gold publication must occur after synchronization and independent review.
Assigning `PBI-000006` to one milestone loses one of those dependency positions.
Assigning it to two milestones violates the exact-once Roadmap contract. The
model therefore returned `is_complete=false` and asked whether the Backlog could
be split. AgileForge correctly recorded `WORKFLOW_FACT_CONFLICT`.

The accepted Backlog can already advertise `BACKLOG_CORRECTION_AVAILABLE`, but
the public generation request has no human guidance or exact accepted-artifact
binding. Calling `backlog generate` cannot safely express the requested split.

## Decision

Add a dedicated `backlog correct` operation. It will reuse the existing
`backlog.generate` workflow node, Backlog provider, output schema, artifact
table, and review node. It will not add a workflow node or database migration.

The first operation is available only for the exact optional re-entry decision
whose reason is `BACKLOG_CORRECTION_AVAILABLE`. A failed or expired correction
remains available only through correction-specific recovery reasons. The caller
must bind the request to:

- the current decision fingerprint;
- the exact accepted Backlog artifact ID and fingerprint;
- nonblank human guidance;
- project, actor, correlation ID, and idempotency key.

The generic `backlog generate` operation must reject the initial correction and
its correction-specific recovery decisions. This prevents a caller from
bypassing the guidance and accepted-artifact checks.

## Public contract

The application request is:

```python
class BacklogCorrectionRequest(FrozenModel):
    project_id: int
    expected_decision_fingerprint: str
    accepted_backlog_artifact_id: int
    accepted_backlog_artifact_fingerprint: str
    guidance: str
    idempotency_key: str
    actor: str
    correlation_id: str | None = None
```

Validation rules:

- fingerprints match `^sha256:[0-9a-f]{64}$`;
- the artifact ID is a positive integer;
- guidance is trimmed, nonblank, and at most 32,768 characters;
- idempotency key and actor are trimmed and nonblank using the existing semantic
  text validation;
- the request model remains frozen and rejects extra fields.

The CLI command is:

```text
agileforge backlog correct \
  --project-id <project-id> \
  --guidance <guidance> \
  --accepted-backlog-artifact-id <artifact-id> \
  --accepted-backlog-artifact-fingerprint <sha256> \
  --expected-decision-fingerprint <sha256> \
  --idempotency-key <idempotency-key> \
  --actor <actor> \
  [--correlation-id <correlation-id>]
```

The HTTP operation is:

```text
POST /api/projects/{project_id}/backlog/correct
X-AgileForge-Expected-Decision: sha256:...
```

Its closed JSON body contains `guidance`, the accepted Backlog artifact ID and
fingerprint, and the usual mutation metadata.

`workflow next` renders the dedicated CLI command with the exact decision and
artifact binding. The first implementation does not advertise this optional
operation through the browser action list and does not add a browser form. The
CLI is the controlled operator path. Browser support can be added as a separate
feature once the semantic contract has production evidence.

## Host-side target resolution

`AgileForgeApplication.correct_backlog()` performs replay before reading current
state. For a new initial request it then requires one exact available decision
with:

- node `backlog.generate`;
- request kind `record_backlog_draft`;
- reason `BACKLOG_CORRECTION_AVAILABLE`;
- recommendation kind `optional_reentry`;
- no instance key;
- exactly one positive-integer reference for each of `backlog`,
  `specification`, and `product_goal`;
- a decision fingerprint equal to the caller's expected fingerprint;
- a Backlog reference equal to the caller's artifact ID and fingerprint.

For a retry after a failed or expired correction attempt, the operation instead
requires `BACKLOG_CORRECTION_FAILED` or
`BACKLOG_CORRECTION_RECOVERY_REQUIRED`, recommendation kind `recovery`, and
exactly one matching `node_attempt` reference in addition to the same Backlog,
Specification, and Product Goal binding. Generic `backlog generate` rejects all
three correction reasons.

The input service reopens the durable rows before provider dispatch. It proves
that the referenced artifact:

- belongs to the project;
- is canonical and matches its stored fingerprint;
- is the accepted physical leaf with no successor;
- has one terminal `accepted` review whose fingerprint matches;
- is bound to the current accepted Specification and Product Goal.

It also enforces the first-version stage boundary. A terminal Roadmap and its
feedback or failed generation attempt may exist. Correction is unavailable once
Story or Sprint planning has begun, as shown by any Story artifact or Story row,
dependency or dependency-review row, Sprint-plan artifact or decision, Sprint,
Sprint-start, or Task fact. Any current Story or Sprint-plan attempt, including a
failed, obsolete, expired, active, or successful attempt, also closes the
boundary. A pending Roadmap review blocks correction, and an active current
Roadmap attempt must finish first; terminal Roadmap attempts remain allowed.
These durable checks are repeated inside the record-draft transaction and before
accepting the successor, so a concurrent downstream start cannot cross the
boundary.

Any mismatch returns `TRANSITION_NOT_AVAILABLE` or `WORKFLOW_FACT_CONFLICT`
before an attempt or provider call.

## Provider input and retained evidence

The provider continues to receive the existing `BacklogBuilderInput` only.
The host sets:

- `prior_backlog_state` to the exact canonical JSON of the accepted Backlog;
- `user_input` to the operator's exact trimmed guidance.

The normalized attempt input also retains this closed host-only object:

```json
{
  "backlog_correction": {
    "accepted_backlog_artifact_id": 3,
    "accepted_backlog_artifact_fingerprint": "sha256:...",
    "guidance": "Split the overloaded items..."
  }
}
```

The existing attempt record already retains the decision fingerprint. Together,
these fields bind replay to the complete human request without exposing host
controls to the provider schema.

The existing Backlog prompt already instructs the provider to use
`prior_backlog_state` and `user_input`, preserve valid prior work, cite accepted
Specification items, and return clarification when the source cannot support a
complete answer. This change does not alter the prompt or its provenance.

## Persistence and review

The host validates provider output with the existing `BacklogAgentOutput` and
canonicalizes it with the existing ID-free contract. The host sorts by priority
and mints `PBI-000001` through `PBI-999999`. Guidance and provider output cannot
choose durable item IDs.

A successful correction appends a pending-review Backlog whose
`supersedes_backlog_artifact_id` is exactly the accepted source artifact. It does
not edit or delete the accepted Backlog. The accepted source remains authoritative
while correction runs, while its successor awaits review, and after successor
feedback or rejection. Authority changes only when a human accepts the exact
successor fingerprint.

If canonical correction output is byte-equivalent to the accepted parent, the
host returns `WORKFLOW_FACT_CONFLICT` with the stable message `Backlog correction
did not change the accepted artifact.` No artifact is inserted. This avoids
surfacing a database uniqueness exception.

## Replay contract

An exact retry with the same idempotency key replays the stored result without a
second provider call or artifact. Exact means the same project, node, actor,
correlation ID, decision fingerprint, accepted artifact ID and fingerprint, and
guidance. A retry after a failed or expired correction uses a new idempotency key
and the correction-specific recovery decision; it cannot fall through to generic
generation.

Reusing the key with any changed semantic field returns the existing conflict
`The idempotency key was already used for different input.` The same conflict
applies if a generic generation call reuses a correction key or a correction
call reuses a generic generation key. A deliberate retry after a provider
failure requires a new key. A late retry never retargets a newer Backlog leaf.

## Planning isolation and stale attempts

An active accepted-Backlog correction, or any unaccepted physical successor,
blocks Roadmap generation and review, Story generation and review,
dependency/readiness mutations, and Sprint planning, review, or start. The block
continues through successor feedback or rejection because that correction chain
is still unresolved. This avoids creating more planning artifacts against a
Backlog the operator is replacing.

This first version cannot be entered after Story or Sprint planning has begun, so
there is no active Sprint to preserve during a valid correction. Supporting a
later-stage correction would require a separate migration design for Story,
dependency, Sprint-plan, and execution lineage.

The graph currently selects failed, obsolete, and active attempts by node and
instance key without requiring their `business_fact_fingerprint` to match the
current snapshot. Its separate recovery-reference path has the same gap. This can
attach Roadmap Attempt 18 to a newly accepted Backlog because Roadmap generation
has no instance key. Both `_overlay_agentic_attempt()` and
`_decision_fact_references()` must filter every attempt by the current
business-fact fingerprint before choosing the latest attempt. Existing success
conflict behavior remains unchanged.

After a corrected Backlog is accepted:

- old Backlogs, Roadmaps, Roadmap reviews, and attempts remain immutable history;
- Roadmap generation is a clean `ROADMAP_GENERATION_REQUIRED` decision bound to
  the new Backlog, with no Attempt 18 reference or old Roadmap feedback;
- the new Roadmap starts its own Backlog-bound lineage and does not supersede a
  Roadmap owned by the old Backlog;
- Story and Sprint planning remain absent until a fresh Roadmap under the new
  Backlog is accepted.

## P&ID correction to apply later

After this feature is implemented, reviewed, and committed, a separate
authorized live operation will correct Backlog Artifact 3. The operator guidance
will preserve the seven unaffected items and produce twelve total items by
partitioning the two overloaded items as follows:

1. Preserve the final governed-gold semantics currently carried by
   `PBI-000006` as one successor item.
2. Preserve the final pilot release, retention, backup, restore, and recovery
   semantics currently carried by `PBI-000009` as one successor item.
3. Add an early consent-audit foundation from `DATA.job-consent-audit`.
4. Add the training-consent, revocation, and reviewer-guide governance gate from
   `CONSTRAINT.orise-training-data-gate`, `REQ.consent-revocation`, and
   `REQ.reviewer-guide`.
5. Add early local bootstrap, mounted model assets, repository quality, and
   reviewer-guide binding from `REQ.local-pilot-bootstrap`,
   `CONSTRAINT.model-assets-mounted`, `DECISION.repo-quality-gate`, and
   `REQ.reviewer-guide`.

The expected Roadmap dependency order is:

1. durable authority and security;
2. consent audit and local foundation;
3. extraction and synchronization;
4. submitter review and export;
5. training governance and guide approval;
6. independent review;
7. gold publication;
8. final pilot and recovery proof.

The live correction must remain pending for semantic review. It must preserve all
accepted Specification references and the `SHOULD` strength of mounted model
assets. The host remints every successor PBI ID after canonical sorting, so the
new IDs may differ from `PBI-000006` and `PBI-000009`. It must not duplicate one
new Backlog ID across Roadmap milestones.

## Non-goals

- Do not allow one Backlog ID in several Roadmap milestones.
- Do not add automatic Backlog splitting or automatic acceptance.
- Do not edit accepted artifacts in place or delete historical artifacts.
- Do not change Backlog, Roadmap, Story, or Sprint provider models.
- Do not change model routing, privacy policy, completion-token settings, or
  provider retry behavior.
- Do not change the accepted Specification or Product Goal.
- Do not implement the P&ID split in production code.
- Do not add browser UI in this first implementation.
- Do not support accepted Backlog correction after Story or Sprint planning has
  begun. That later-stage migration is a separate feature.

## Acceptance criteria

- The dedicated command and API reject blank or oversized guidance and malformed
  identities before provider execution.
- The operation is available only for the exact accepted optional correction
  decision, or its correction-specific recovery decision, and exact current
  artifact.
- A terminal Roadmap may precede correction. Pending Roadmap review, active
  downstream attempts, and any Story or Sprint planning state close the stage
  boundary without writes or provider execution.
- The provider receives canonical prior state plus exact guidance, while the
  normalized attempt retains the closed correction identity.
- Exact replay performs no second provider call. Changed semantics under the same
  key conflict.
- A successful result creates one pending successor and leaves the accepted
  parent authoritative until review acceptance.
- Identical output creates no artifact and returns the stable fact conflict.
- Active correction and every unaccepted successor state block all downstream
  planning until the correction chain is accepted.
- Accepting the successor exposes a clean, required Roadmap generation decision
  bound to the new Backlog. Attempt 18 and old planning artifacts remain history.
- Tests run offline with sockets blocked. Implementation testing does not touch
  live profiles, P&ID files, original databases, or providers.
