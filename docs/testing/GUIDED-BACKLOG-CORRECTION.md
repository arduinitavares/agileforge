# Guided Backlog Correction Operator Guide

## 1. Overview & Architecture

AgileForge enforces strict artifact immutability. Once a Backlog is formally accepted (`sh ./agileforge-dev cli --profile <profile> -- backlog decide --decision accepted`), its artifact record is never modified or overwritten in-place.

When business requirements or scope must be amended after initial acceptance, AgileForge provides **Guided Backlog Correction**. This capability allows an operator to submit targeted steering guidance (up to 32,768 characters) to generate a successor Backlog draft that supersedes the accepted version.

```
[Accepted Backlog v1] ───► [Roadmap Generation Required / Failed / Feedback]
         │
         │  (Operator invokes `backlog correct` with guidance)
         ▼
[Pending Backlog v2 (Correction Draft)]  ◄─── All 9 Planning Nodes Paused
         │
         │  (Operator reviews and accepts)
         ▼
[Accepted Backlog v2] ───► [Clean Roadmap Generation Required (Debt-Free)]
```

### Core Tenets
- **Development Branch Runtime**: Always use the checkout-local wrapper form: `sh ./agileforge-dev cli --profile <profile> -- ...`; run `sh ./agileforge-dev cli --profile <profile> -- info --json` before any runtime mutation. Never use a bare or user-level `agileforge` shim.
- **Guidance Boundary**: Guidance is human semantic steering input. It cannot override the accepted Specification or Product Goal.
- **Authority Preservation**: A successful correction call creates a pending successor artifact (`status == "pending_review"`). It does not automatically accept it. The original accepted Backlog remains authoritative while correction runs, while the successor awaits human review, and after successor feedback or rejection. Authority only transitions upon explicit human acceptance.
- **Live P&ID Separation**: The P&ID split remains a separate authorized live operation and human review; this feature provides the reusable, general-purpose engine capability and does not perform the live P&ID Backlog correction.

---

## 2. Boundary Constraints & Preconditions

Guided correction is designed specifically for early-stage corrections discovered before Roadmap review and downstream Story or Sprint planning commitments have been made.

### Preconditions for Correction
1. **Backlog Accepted**: The project must have an active accepted Backlog (`status == "accepted"`).
2. **Roadmap Boundary**: The project must be strictly before Roadmap review:
   - Roadmap generation required / failed / feedback. Terminal Roadmap history (such as Attempt 18 failure or previous Roadmap feedback) does not close the correction boundary.
   - Pending Roadmap review (`planning.roadmap.review`) closes the correction window.
3. **No Active Downstream Attempt**: `planning.roadmap.generate` must not have an active in-flight attempt lease. If one is running, the operator must wait for it to complete or expire (`BACKLOG_CORRECTION_DOWNSTREAM_ACTIVE`).

### Stage Closed Boundary
Once downstream planning begins or Roadmap review is pending, the correction window is strictly closed:
- If any Roadmap artifact in `pending_review` status exists (`planning.roadmap.review`),
- If any Story artifact (`artifact_type == "story"`) exists,
- If any Sprint plan artifact (`artifact_type == "sprint_plan"`) exists,
- If any durable Story, dependency, dependency review, Sprint, Sprint start, or Task row exists, or
- If any same-current-facts attempt has been recorded for `planning.story.*`, `planning.sprint.*`, or `execution.*`,

the transition is rejected with reason `BACKLOG_CORRECTION_STAGE_CLOSED` and HTTP status `409 Conflict` (`WORKFLOW_FACT_CONFLICT`), with the message:
`Guided Backlog correction is available only before Story or Sprint planning begins.`

### In-Transaction Recheck Guarantee
Both `execute_record_backlog_draft` and `execute_decide_backlog` execute database-level fact rechecks inside the database transaction:
1. Downstream attempt started first: blocks correction start and prevents draft recording.
2. Correction started first: blocks downstream attempts and prevents downstream planning mutations.
3. If downstream planning facts are injected while a correction successor is pending review, accepting the successor is transactionally refused with `WORKFLOW_FACT_CONFLICT`.

---

## 3. Operator CLI Usage

### Reading Workflow Next
Always read `sh ./agileforge-dev cli --profile <profile> -- workflow next --project-id <project-id>` to obtain the exact rendered command and required decision fingerprint. When correction is available or recovery is required, `workflow next` renders the dedicated `backlog correct` command.

### Invoking Backlog Correction
To correct the current accepted Backlog with operator guidance:

```bash
sh ./agileforge-dev cli --profile <profile> -- backlog correct \
  --project-id <project-id> \
  --expected-decision-fingerprint <decision-fingerprint> \
  --accepted-backlog-artifact-id <artifact-id> \
  --accepted-backlog-artifact-fingerprint <artifact-fingerprint> \
  --guidance "Exclude offline cache synchronization from Phase 1 deliverables." \
  --idempotency-key "backlog-correct-41-01" \
  --actor "operator"
```

Options:
- `--project-id <INT>`: Required. Project identifier.
- `--expected-decision-fingerprint <SHA256>`: Required. Exact decision fingerprint from current workflow position.
- `--accepted-backlog-artifact-id <INT>`: Required. Positive integer ID of the accepted Backlog artifact being corrected.
- `--accepted-backlog-artifact-fingerprint <SHA256>`: Required. Content fingerprint of the accepted Backlog artifact.
- `--guidance <TEXT>`: Required. 1 to 32,768 characters of nonblank correction instructions.
- `--idempotency-key <KEY>`: Required. Nonblank key for retry safety and replay.
- `--actor <TEXT>`: Required. Operator identity.
- `--correlation-id <ID>`: Optional request correlation ID.

### Unchanged Output Detection
If the model produces output that canonicalizes to the exact same bytes and fingerprint as the accepted parent artifact, AgileForge returns `WORKFLOW_FACT_CONFLICT` with the stable message:
`Backlog correction did not change the accepted artifact.`
No artifact row is inserted into the database.

### Reviewing and Accepting the Corrected Backlog
After the correction attempt records a candidate draft (`status == "pending_review"`):

```bash
# Inspect the new draft candidate
sh ./agileforge-dev cli --profile <profile> -- status --project-id <project-id>

# Accept the corrected Backlog candidate
sh ./agileforge-dev cli --profile <profile> -- backlog decide \
  --project-id <project-id> \
  --decision accepted \
  --rationale "Revised scope accurately reflects Phase 1 deliverables." \
  --idempotency-key "backlog-decide-41-01" \
  --actor "operator"
```

---

## 4. HTTP API Operation

### Direct Correction Endpoint

```http
POST /api/projects/{project_id}/backlog/correct
Content-Type: application/json
X-AgileForge-Expected-Decision: sha256:7f... (decision fingerprint)

{
  "guidance": "Exclude offline cache synchronization from Phase 1 deliverables.",
  "accepted_backlog_artifact_id": 3,
  "accepted_backlog_artifact_fingerprint": "sha256:...",
  "idempotency_key": "backlog-correct-41-01",
  "actor": "operator",
  "correlation_id": "corr-backlog-correct-41-01"
}
```

### Response Status Codes
- `200 OK`: Correction attempt successfully processed. Returns `TransitionResult`.
- `422 Unprocessable Entity`: Validation error (e.g., whitespace-only guidance, guidance exceeding 32,768 characters, non-integer or non-positive artifact ID, malformed fingerprints, missing expected-decision header, or unknown extra fields).
- `409 Conflict`:
  - Decision mismatch (`X-AgileForge-Expected-Decision` does not match current graph state).
  - Boundary violation (`BACKLOG_CORRECTION_STAGE_CLOSED` or `BACKLOG_CORRECTION_DOWNSTREAM_ACTIVE`).
  - Fact conflict (Backlog not accepted, already superseded, or identical output returned).

---

## 5. Replay, Cache & Retention Semantics

1. **Host-Only Retention**:
   The correction identity is stored within `normalized_input_json` under the closed host-only envelope key `"backlog_correction"`:
   ```json
   {
     "backlog_correction": {
       "accepted_backlog_artifact_id": 3,
       "accepted_backlog_artifact_fingerprint": "sha256:...",
       "guidance": "Exclude offline cache synchronization from Phase 1 deliverables."
     }
   }
   ```
   This payload is kept server-side for audit and attempt replay. The underlying model provider receives only `BacklogBuilderInput` (`prior_backlog_state` and `user_input`); provider input and output schemas remain unchanged.

2. **Replay Validation & Operation-Closed Keys**:
   - **Exact Replay**: Submitting the exact same idempotency key with identical fields (actor, correlation ID, decision fingerprint, accepted artifact ID/fingerprint, guidance) replays the stored result without making a second provider call.
   - **Changed Input Conflict**: Reusing the same idempotency key with any modified field returns `WORKFLOW_FACT_CONFLICT` (`The idempotency key was already used for different input.`).
   - **Cross-Operation Conflict**: A generic `backlog generate` request attempting to reuse a correction idempotency key, or a `backlog correct` request attempting to reuse a generic generation key, immediately conflicts.
   - **Failure Replay & Retry**: An exact replay of a failed attempt returns the original failure without calling the provider. To retry a failed or expired correction, the operator must obtain the dedicated recovery decision (`BACKLOG_CORRECTION_FAILED` or `BACKLOG_CORRECTION_RECOVERY_REQUIRED`) and submit with a **new idempotency key**. Generic `backlog generate` cannot execute or bypass correction recovery decisions.

---

## 6. Planning Graph Lifecycle & Downstream Lineage Reset

### Temporary Planning Pause
While a Backlog correction is unresolved (an attempt is active, failed, obsolete, or expired, or a candidate Backlog successor is in `pending_review`, `feedback`, or `rejected` state):
- All 9 planning nodes are blocked:
  1. `planning.roadmap.generate`
  2. `planning.roadmap.review`
  3. `planning.story.generate`
  4. `planning.story.review`
  5. `planning.story_dependencies`
  6. `planning.story_readiness`
  7. `planning.sprint.plan`
  8. `planning.sprint.review`
  9. `planning.sprint.start`
- Each planning node returns category `BLOCKED` with reason code `BACKLOG_CORRECTION_IN_PROGRESS`.
- Downstream planning remains paused through successor feedback or rejection until the correction chain is accepted.

### Clean Downstream Reset
When the corrected Backlog candidate is formally accepted:
1. The prior Backlog artifact status transitions to `superseded`.
2. The planning pause is released.
3. `planning.roadmap.generate` transitions to `AVAILABLE` with recommendation `REQUIRED`, referencing the newly accepted Backlog.
4. Old Roadmap artifacts, feedback comments, and failed attempts (such as Attempt 18) belonging to the prior Backlog lineage remain in immutable history but are detached from active evaluation. Roadmap generation starts fresh under the new Backlog with clean `ROADMAP_GENERATION_REQUIRED` and no carry-over debt or old attempt references.
5. Story and Sprint planning remain absent until the new Roadmap is accepted.

---

## 7. Troubleshooting & Error Reference

| Error Code / Blocker | Cause | Operator Action |
| :--- | :--- | :--- |
| `BACKLOG_CORRECTION_STAGE_CLOSED` | Pending Roadmap review, Stories, dependencies, or Sprint plans have already been created or attempted. | Guided Backlog correction is no longer permitted for this project lifecycle. |
| `BACKLOG_CORRECTION_DOWNSTREAM_ACTIVE` | A Roadmap generation attempt is actively running. | Wait for the active Roadmap attempt lease to expire or complete. |
| `BACKLOG_CORRECTION_IN_PROGRESS` | A correction attempt or successor candidate is unresolved. | Resolve the correction attempt or review the pending candidate via `sh ./agileforge-dev cli --profile <profile> -- backlog decide --decision accepted` / `feedback`. |
| `WORKFLOW_FACT_CONFLICT` | Decision fingerprint mismatch, identical output, or concurrent modification. | Check `sh ./agileforge-dev cli --profile <profile> -- workflow next --project-id <project-id>` for current decision fingerprint and retry with valid binding. |
