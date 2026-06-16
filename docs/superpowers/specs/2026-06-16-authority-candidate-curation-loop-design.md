# Authority Candidate Curation Loop Design

**Date:** 2026-06-16
**Status:** Draft
**Spec mode:** proposed_change
**Owner:** AgileForge maintainers
**Scope:** Authority compilation, review feedback, candidate repair, quality gates, and authority acceptance readiness

## Revision History

- 2026-06-16: Drafted after the ASA scope-extension authority review showed
  that structural coverage repair can produce an `accept_ready` packet with
  over-split, duplicate, brittle, or materially wrong invariants.
- 2026-06-16: Revised to make Google ADK the v1 agent-loop substrate, matching
  AgileForge's existing agent architecture.
- 2026-06-16: Revised to target ADK 2.0 graph/dynamic workflows and keep the
  Loop template workflow as the required behavioral pattern.
- 2026-06-16: Added transient curation status, migration hook, invariant repair
  lineage, and authority projection requirements from design review.

## Summary

AgileForge should stop treating compiled authority as a one-shot compiler
artifact. Authority compilation can now satisfy structural coverage while still
producing bad authority. The missing product capability is a bounded curation
loop:

```text
compile candidate
-> critique authority quality and spec fidelity
-> emit structured feedback
-> repair targeted authority regions
-> validate the diff
-> repeat until gates pass or fail closed
```

The loop must not auto-accept authority. It produces a better pending authority
candidate and a reviewable diff. Human acceptance remains the bridge into the
next AgileForge phase.

## Problem

The ASA offline advisory scope extension exposed the current failure mode:

- initial authority compile failed with
  `STRUCTURED_COVERAGE_INCOMPLETE: MISSING_ACCEPTED_MUST_AUTHORITY`;
- coverage repair fixed that narrow compiler failure;
- the regenerated authority reached `authority_pending_review` and
  `accept_ready`;
- review still found quality risks: over-split invariants, near-duplicates,
  high compiler gaps/assumptions, and at least one materially wrong invariant.

The most important defect was an overstrong invariant derived from
`REQ.delayed-outcome-predictor`:

```text
learned_model_score >= max(no_action_score, persistence_score,
operator_action_replay_score, simple_regression_baseline_score)
```

The source spec required baseline comparison. It did not require the learned
model to beat every baseline in every context. That is a semantic authority
error, not a formatting problem.

Current rejection records a human reason, but regeneration has no structured
feedback input and no target-invariant repair contract. A blind regenerate may
change unrelated authority, recreate the same defect, or introduce new defects.

## Goals

- Add a first-class authority candidate curation loop before authority
  acceptance.
- Store structured feedback against specific authority targets such as
  invariant id, source item id, source level, gap id, assumption id, or quality
  group id.
- Run multiple authority critics with separate responsibilities:
  - coverage and source-map critic;
  - semantic fidelity critic;
  - quality and reviewability critic;
  - deterministic host validator.
- Support targeted repair when the defect is localized.
- Preserve unchanged authority items byte-for-byte where possible.
- Produce a candidate diff from previous authority to repaired authority.
- Stop after bounded iterations and fail closed when quality does not converge.
- Keep human authority review and accept/reject as explicit guarded decisions.
- Keep the design project-agnostic; ASA is a regression scenario, not a special
  rule source.

## Non-Goals

- Do not auto-accept authority after curation.
- Do not weaken source-map validation or provenance checks.
- Do not make a blind full regenerate the default answer to human rejection.
- Do not require a new model provider or change `config/models.yaml`.
- Do not build a general-purpose workflow engine beyond this authority loop.
- Do not add backlog, roadmap, story, sprint, brownfield, or scope-extension
  behavior except where those flows already call authority compile/review.
- Do not patch persisted accepted authority in place.

## Current Behavior

Current public authority flow is:

```text
authority compile
-> authority review
-> authority accept | authority reject
-> authority regenerate after rejection
```

Important current seams:

- `authority review` builds a deterministic review packet for the pending
  authority.
- `authority reject` records a human reason and transitions setup status to
  `authority_rejected`.
- `authority regenerate` accepts `project_id`, `spec_version_id`, optional
  `compiler_model`, and `idempotency_key`.
- `authority regenerate` forces a full recompile for the approved spec version.
- The rejection reason is not a structured compiler input.
- The current quality gate can surface near-duplicate and over-split groups,
  but those findings are review metadata rather than targeted repair inputs.

## Proposed Approach

Use Google ADK 2.0 as the v1 execution framework for the authority curation
loop. This matches AgileForge's current agent architecture while aligning new
agentic work to ADK 2.0's workflow runtime:

- `orchestrator_agent/agent.py` wires ADK agents and `AgentTool` instances;
- existing agent tools use `google.adk.agents.Agent` or `LlmAgent`;
- tool contracts already use Pydantic schemas;
- `orchestrator_agent/agent_tools/utils/resilience.py` already contains a
  `ConditionalLoopAgent(BaseAgent)` loop primitive.

The design should not rebuild generic loop execution in services code. ADK 2.0
workflow graphs or dynamic workflows own ordered agent execution, iterative
control, retry integration, and workflow events. Services own persistence,
idempotency, workflow guards, artifact lineage, and deterministic quality gates.

The Loop template workflow agent is still part of the design as the behavioral
template:

- execute sub-agents in a deterministic order;
- cap iterations with `max_iterations`;
- use an explicit exit signal rather than model confidence;
- allow individual sub-agents to be LLM-backed or deterministic.

Because ADK 2.0 documentation says templated workflows are superseded by
graph-based and dynamic workflows, the preferred implementation is an ADK 2.0
graph or dynamic workflow that preserves Loop template semantics. If ADK 2.0
still exposes `LoopAgent` compatibly and the API remains supported, it may be
used as the concrete implementation. If not, build the curation loop as an ADK
2.0 dynamic workflow, not as service-only Python loop code.

Either way, the public contract is:

```text
ADK 2.0 workflow executes critics and repair agents
-> host services validate each loop result
-> host services decide exit/pass/fail
```

The critical rule is that loop exit must be decided by host-visible quality
gates, not by model confidence.

Recommended v1 loop:

```text
CompileCandidate
-> CoverageCritic
-> SemanticFidelityCritic
-> QualityCritic
-> RepairPlanner
-> TargetedRepairCompiler
-> DiffValidator
-> GateDecision
```

The host owns candidate lineage, idempotency, persistence, and acceptance
readiness. ADK 2.0 agents and workflow nodes may produce feedback or repairs,
but host code must validate outputs before a candidate becomes pending review.

## ADK 2.0 Migration Requirements

AgileForge currently depends on `google-adk>=1.16.0` and the lockfile resolves
`google-adk 1.16.0`. Implementation planning must include an ADK 2.0 migration
step before the curation loop is coded.

Migration requirements:

- update dependency constraints and lockfile to ADK Python 2.0;
- migrate custom `BaseAgent` execution logic away from legacy `_run_async_impl`
  overrides where ADK 2.0 workflow runtime would bypass them;
- use ADK 2.0 callbacks or workflow nodes for lifecycle hooks;
- update session/event storage and readers for ADK 2.0 event fields such as
  `node_info` and `output` if AgileForge persists ADK events;
- avoid direct mutation or manual append of ADK session events;
- let tool exceptions propagate where ADK 2.0 retry/HITL behavior must see
  them;
- keep Pydantic structured outputs as the boundary for critic, repair, and gate
  node payloads.

## Alternatives Considered

### 1. Blind regenerate after rejection

This is the current effective behavior. It is simple but does not use rejection
feedback. It can recreate the same bad invariant and can disturb unrelated
authority. Reject.

### 2. Manual spec amendment for every bad invariant

This keeps authority compiler behavior simple, but it pushes compiler mistakes
back into product specs. ASA showed the spec was not necessarily wrong; the
authority interpretation was. Reject as the default path.

### 3. Service-only targeted curation loop

This keeps dependencies small, but it duplicates loop orchestration that
AgileForge already models through ADK agents. It also splits agentic behavior
between services and ADK without a strong reason. Reject for v1.

### 4. ADK 2.0 workflow curation loop with host gates

This adds explicit feedback, bounded repair, and diff review while preserving
existing authority acceptance semantics. It reuses AgileForge's established ADK
agent pattern, adopts ADK 2.0 for new agentic workflow work, and keeps
deterministic workflow truth in services. Accept for v1.

## Public CLI Contract

### Review Feedback Capture

Add a non-terminal feedback command that records structured feedback without
changing acceptance status:

```bash
agileforge authority feedback record \
  --project-id <project_id> \
  --pending-authority-id <authority_id> \
  --expected-authority-fingerprint <sha256> \
  --feedback-file authority-feedback.json \
  --idempotency-key <key>
```

Feedback file shape:

```json
{
  "schema_version": "agileforge.authority_feedback.v1",
  "authority_id": 6,
  "feedback_items": [
    {
      "feedback_id": "AFB-overstrong-baseline-001",
      "target_kind": "invariant",
      "target_id": "INV-a8df0215dd258af2",
      "source_item_id": "REQ.delayed-outcome-predictor",
      "issue_type": "overstrong_invariant",
      "severity": "blocking",
      "instruction": "Replace the baseline comparison as a reporting/comparison requirement, not a learned_model_score >= all baselines requirement."
    }
  ]
}
```

### Curation Run

Add a guarded mutation that creates a new authority candidate from a pending or
rejected authority plus structured feedback:

```bash
agileforge authority curate \
  --project-id <project_id> \
  --spec-version-id <spec_version_id> \
  --source-authority-id <authority_id> \
  --expected-source-authority-fingerprint <sha256> \
  --feedback-attempt-id <feedback_attempt_id> \
  --max-iterations 2 \
  --idempotency-key <key>
```

`authority curate` must stop at `authority_pending_review`. It must not call
`authority accept`.

### Regenerate Compatibility

`authority regenerate` remains available for unsupported schema recovery and
whole-authority recompilation. It should not be advertised as the preferred next
step when structured rejection feedback exists. `workflow next` should prefer
`authority curate` when feedback is recorded for the rejected authority.

## API Contract

Dashboard/API surfaces should mirror the CLI:

- record authority feedback;
- list feedback attempts for a pending/rejected authority;
- start a curation run;
- read candidate lineage and diff summary;
- review the curated pending authority;
- accept/reject remains unchanged.

Unknown feedback fields must be rejected. Feedback payloads are audit artifacts,
not free-form comments.

All new request, response, persisted-payload, and ADK node payload models must
use strict Pydantic schemas with `model_config = ConfigDict(extra="forbid")`.
Unknown fields should fail at the boundary rather than being silently ignored.

## Feedback Taxonomy

Supported v1 issue types:

- `overstrong_invariant`
- `understrong_invariant`
- `materially_wrong_invariant`
- `duplicate_invariant`
- `near_duplicate_invariant`
- `over_split_group`
- `brittle_wording`
- `missing_invariant`
- `invalid_gap`
- `invalid_assumption`
- `source_map_error`
- `coverage_gap`

Supported target kinds:

- `invariant`
- `gap`
- `assumption`
- `quality_group`
- `source_item`
- `authority_candidate`

Each blocking feedback item must name either a concrete target id or a concrete
source item id. Whole-candidate feedback is allowed only for summary defects
such as "candidate is too noisy to review".

## Candidate Lineage

Every curated authority candidate records:

- source authority id and fingerprint;
- source spec version id and spec hash;
- feedback attempt ids consumed;
- curation attempt id;
- loop iteration count;
- compiler model metadata;
- critic versions and prompt hashes;
- repair mode: `targeted`, `full_recompile`, or `failed_no_candidate`;
- invariant lineage for repaired invariants;
- diff fingerprint from source authority to candidate;
- quality gate result.

Accepted authority must always point to the final reviewed candidate, not to an
intermediate repair artifact.

## Repair Semantics

Targeted repair is preferred when all feedback items are localized to known
targets. For targeted repair:

- untouched invariants, gaps, assumptions, and source-map entries must remain
  byte-for-byte identical;
- repaired items must keep source item provenance;
- a repaired invariant must keep its existing id only when its canonical
  identity payload remains unchanged;
- a repaired invariant must receive a new deterministic id when its canonical
  type, parameters, source item id, or source level changes;
- `lineage_json` must map old invariant ids to new invariant ids, including a
  removal reason when an old invariant is removed without replacement;
- removed items must appear in the candidate diff with a removal reason;
- new items must cite the feedback item that caused them;
- repaired items must pass the same normalizer and source-map validation used by
  normal authority compile.

Full recompile is allowed only when:

- feedback targets the entire candidate;
- the current candidate is structurally invalid;
- targeted repair fails validation and a full regenerate is explicitly
  requested by the operator.

Full recompile still consumes structured feedback and must report whether the
original feedback issues were resolved.

## Quality Gates

The final candidate is review-ready only when all are true:

- no blocking feedback item remains unresolved;
- all accepted `MUST` and `MUST_NOT` source items are covered or have explicit
  compiler gaps;
- no source-map validation errors exist;
- no material semantic-fidelity issue exists;
- no exact duplicate invariant remains;
- near-duplicate and over-split group counts are within configured thresholds or
  are explicitly marked non-blocking;
- no repaired invariant contradicts the source item acceptance criteria;
- diff validator reports only expected target changes for targeted repair.

If any gate fails after the max iteration count, the curation run returns
`authority_curation_failed` and leaves setup locked for review or another
explicit curation run.

## Loop Bounds

Default v1 bounds:

- max iterations: `2`;
- max targeted feedback items per run: `25`;
- max new/repaired authority items per targeted run: `50`;
- no automatic retry after a validation error introduced by repair;
- no automatic fallback from targeted repair to full recompile.

These defaults favor fail-closed behavior over endless repair loops.

## Storage

Use dedicated SQLModel tables rather than workflow-state blobs, but keep v1
small:

- `AuthorityFeedbackAttempt`
- `AuthorityCurationAttempt`

`AuthorityFeedbackAttempt` stores the canonical feedback JSON plus scalar index
columns for project id, source authority id, source authority fingerprint,
status, feedback fingerprint, created/changed metadata, and idempotency replay.

`AuthorityCurationAttempt` stores the curation request, candidate lineage, diff
summary, `lineage_json`, quality report, failure artifact id, status,
created/changed metadata, and idempotency replay.

Do not add separate item, lineage, diff, or quality-report tables in v1 unless
real query requirements prove the JSON columns are insufficient.

Workflow state stores only setup status and next-action projections. It should
not store full feedback payloads or candidate diffs.

Migration requirements:

- define the new SQLModel tables in `models/agent_workbench.py` or a focused
  model module imported by the runtime metadata path;
- update `db/migrations.py` so `ensure_schema_current()` creates both tables
  and required indexes idempotently;
- add schema-readiness checks for the new tables where read projections depend
  on them;
- add a readiness test proving a fresh database and an existing database both
  receive the curation tables and required columns.

## Workflow State

Add one transient setup status in v1: `authority_curating`.

`authority_curating` is set after request guards pass and before the ADK 2.0
workflow starts. It prevents concurrent CLI/API curation attempts from racing
the same rejected authority. While active, `workflow next` should point to
`agileforge mutation show` for the active curation mutation, matching the
existing `authority_compiling` pattern.

Reuse `authority_rejected` for the idle period between rejection, feedback
capture, and curation. If curation fails closed, return setup status to
`authority_rejected` and attach bounded curation failure metadata plus the
failure artifact id. The recorded feedback and curation attempt rows provide the
detailed progress state.

Existing statuses remain:

- `authority_pending_review`
- `authority_rejected`
- `authority_compile_failed`
- `authority_compiling`
- `authority_curating`

`workflow next` behavior:

- pending review with no blocking feedback: show review/accept/reject;
- rejected authority with no structured feedback: show feedback record and
  regenerate fallback;
- rejected authority with structured feedback: show `authority curate`;
- curating authority: show active mutation inspection command;
- failed curation: stay at `authority_rejected` and show curation retry with
  current feedback and failure artifact.

## Authority Projection

`services/agent_workbench/authority_projection.py` must query the feedback and
curation attempt tables so read-only status surfaces can explain the next
action. The projection should expose bounded fields such as:

- `has_blocking_feedback`;
- `latest_feedback_attempt_id`;
- `latest_curation_attempt_id`;
- `latest_curation_status`;
- `latest_curation_failure_artifact_id`;
- `curation_available`;
- `curation_in_progress`.

`workflow next` should consume these projection fields when deciding whether to
recommend `authority feedback record`, `authority curate`, `mutation show`, or
the regenerate fallback.

## Failure Modes

- If feedback references a missing target id, reject the feedback with
  `AUTHORITY_FEEDBACK_TARGET_NOT_FOUND`.
- If source authority fingerprint mismatches, reject with
  `STALE_AUTHORITY_VERSION`.
- If targeted repair changes untargeted items, fail with
  `AUTHORITY_CURATED_DIFF_UNBOUNDED`.
- If repair output fails source-map validation, fail with
  `SPEC_COMPILE_FAILED` plus curation metadata.
- If the loop reaches max iterations, fail with
  `AUTHORITY_CURATION_MAX_ITERATIONS`.
- If a second curation starts while setup status is `authority_curating`, reject
  with a stale guard or mutation-in-progress error and point to the active
  mutation.
- If feedback is syntactically valid but all items are non-blocking, curation
  may produce a no-op candidate only if the operator explicitly requests it.

## Observability And Audit

Every curation attempt should emit bounded metadata:

- attempt id;
- source authority id;
- result status;
- iteration count;
- feedback item counts by issue type and severity;
- repaired/removed/added item counts;
- unchanged item count;
- quality gate summary;
- failure artifact id when failed.

Do not print full compiled authority JSON in CLI summaries.

## Security And Privacy

Authority curation may send product specs and authority candidates to the
configured compiler model. CLI and dashboard flows must make external model use
explicit when the selected compiler model routes through external providers.

Feedback artifacts may contain product-sensitive text. They must be stored in
the business database or managed artifact storage, not logs.

## Acceptance Criteria

- A rejected authority can receive structured feedback targeted at one invariant
  id.
- Starting curation transitions setup status to `authority_curating` before LLM
  work starts, and concurrent curation requests cannot run for the same
  authority.
- A curation run can repair that invariant without changing unrelated
  invariants.
- A repaired invariant with changed canonical payload receives a new
  deterministic id and records old-to-new mapping in `lineage_json`.
- The candidate diff shows removed, added, changed, and unchanged authority
  items.
- If repair introduces a source-map error, the curation run fails closed and
  does not publish a review-ready candidate.
- If all quality gates pass, AgileForge publishes a new pending authority
  candidate and returns `authority_pending_review`.
- `authority accept` still requires human review of the final candidate.
- `workflow next` prefers targeted curation over blind regenerate when
  structured feedback exists.
- Existing `authority regenerate` behavior remains available and tested.

## Tests

Required test surfaces:

- ADK 2.0 workflow runtime smoke test for the curation graph/dynamic workflow;
- ADK 2.0 session/event persistence compatibility for `node_info` and `output`
  if events are stored;
- SQL migration/readiness tests for `AuthorityFeedbackAttempt` and
  `AuthorityCurationAttempt`;
- authority status projection tests for feedback and curation flags;
- request, response, feedback, curation, and ADK node schemas reject unknown
  fields with `extra="forbid"`;
- `authority_curating` blocks concurrent curation and routes `workflow next` to
  mutation inspection;
- feedback file schema validation rejects unknown fields and missing targets;
- one-invariant overstrong feedback produces a targeted repair request;
- targeted repair cannot change unrelated invariants;
- targeted repair records old-to-new invariant lineage when deterministic ids
  change;
- duplicate and over-split feedback can be recorded against quality group ids;
- curation max-iteration failure leaves setup in a recoverable state;
- workflow next advertises `authority curate` only after structured feedback
  exists;
- accept/reject idempotency remains unchanged;
- regenerate remains available for unsupported schema recovery;
- CLI summaries stay bounded and do not print full authority JSON.

## Open Questions

- Should `authority reject` optionally accept `--feedback-file`, or should
  feedback recording remain a separate command for audit clarity?
- Should non-blocking feedback be allowed to trigger curation, or should v1
  require at least one blocking item?
- Should targeted repair be implemented as a new compiler mode inside the
  existing compiler agent or as a separate ADK 2.0 repair node with a narrower
  schema?
- Should quality thresholds be fixed constants in v1 or read from project
  configuration?
- Should the concrete ADK 2.0 implementation use a graph workflow or dynamic
  workflow for the loop? The required semantics stay the same either way.

None of these questions block the design direction. They should be resolved
before implementation planning.
