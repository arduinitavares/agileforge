# Context-Grounded Project Vision Bootstrap Design

**Status:** Approved design

**Date:** 2026-08-10

**Implementation base:** `af10910d3e8a6719235b78862e9bb4dd4e29f7e7`

## Purpose

AgileForge currently begins Project Vision work by showing static questions and
requiring a nonblank human response before invoking the Vision agent. The host
passes only Project metadata, that response, and prior Vision state. The prompt
also explicitly forbids inference from repository contents.

That contract conflicts with the intended experience. AgileForge should first
form a transparent Vision hypothesis from the context already available, then
let a human correct, refine, accept, or reject it. A repository is optional
context, not a Project type or an onboarding path.

The central authority rule is:

> AI may propose product intent from available evidence. Only human acceptance
> makes that intent authoritative.

## Goals

1. Add an explicit `vision.bootstrap` action that generates the first Vision
   draft without requiring a human response.
2. Ground the draft in deterministic, bounded Project and repository evidence.
3. Expose the evidence, assumptions, conflicts, and inference basis behind each
   proposed Vision component.
4. Preserve an iterative CLI and UI loop for human clarification and revision.
5. Keep model calls explicit, replay-safe, and free from GET-side effects.
6. Keep one Project lifecycle for Projects with or without repositories.
7. Make both the human UI and agent CLI semantic: neither caller supplies raw
   workflow JSON, fingerprints, repository-derived fields, or internal IDs.

## Non-Goals

- Automatically accepting a Vision.
- Generating a Product Goal, backlog item, feature list, specification, or task
  during Vision work.
- Restoring greenfield or brownfield lifecycle branches.
- Performing repository inventory, source-code analysis, GitHub research, or
  network calls during Vision evidence collection.
- Reading arbitrary documentation or source files.
- Migrating existing experimental databases or acceptance profiles.
- Running automated acceptance against String Calculator Lab or any user
  Project.

## Decision

### Selected approach: explicit context-grounded bootstrap

The initial graph position advertises `vision.bootstrap`. The operator or CLI
agent explicitly executes it. The host gathers a deterministic evidence
snapshot, performs repository freshness checks, and then invokes the Vision
model.

An incomplete result advances to `vision.interview`, where the human answers
model-generated questions. A complete result advances to `vision.review`, where
the human accepts, rejects, or sends feedback. Feedback resumes the interview
without restarting the Project.

### Rejected alternatives

1. **Keep the human-first interview.** This preserves the current contract but
   wastes available context and makes the operator do work that the agent can
   prepare safely.
2. **Generate automatically on Project creation or page load.** This hides a
   paid side effect inside an unrelated action or GET request, complicates
   replay, and can spend money without clear operator intent.
3. **Let the model inspect the repository directly.** This weakens security,
   reproducibility, size controls, provenance, and testability.

## Lifecycle

The initial lifecycle becomes:

```text
Create Project
    -> optionally attach Repository
    -> vision.bootstrap
        -> incomplete: vision.interview
            -> human response
            -> vision.interview or vision.review
        -> complete: vision.review
            -> accepted: Product Goal becomes available
            -> feedback/rejected: vision.interview
```

The repository may be attached before or after Project creation. Its presence
does not select a different graph. A Project without a repository uses Project
metadata as its evidence snapshot and can complete the same Vision flow.

An accepted Vision revision retains the existing explicit
`vision.revision.start` guard. Once a revision intent is open,
`vision.bootstrap` builds a revision proposal from the accepted Vision, revision
reason, current evidence, and eligible Goal state. Later clarification turns use
`vision.interview`.

## Human And Agent Authority

The CLI agent may run the complete discussion loop:

1. Read `workflow position` and `workflow next`.
2. Run the advertised `vision bootstrap` command.
3. Read `vision status` and present the draft, basis, assumptions, conflicts,
   and questions.
4. Obtain ordinary-language human feedback when product intent is unknown or
   disputed.
5. Run `vision respond --text ...` and repeat as needed.
6. Present the complete candidate for an explicit human decision.
7. Execute accept, feedback, or rejection only from that human decision.

The agent may analyze, challenge, and recommend. It may not treat its preferred
draft as accepted product intent. The human never authors workflow JSON,
fingerprints, evidence IDs, or candidate IDs.

## Evidence Collection

### Boundary

Add one deterministic host service, `VisionEvidenceCollector`. It owns evidence
selection, parsing, sanitization, ordering, truncation, and fingerprinting. The
model receives only the collector result. It receives no filesystem access.

The collector is modular internally, with small source providers for:

- Project metadata
- Repository provenance
- `README.md`
- `CONTEXT.md`
- `pyproject.toml`
- Canonical technical specification candidates

Repository identity comes from the active durable `RepositoryBinding`, then a
fresh `RepositoryProbe` immediately before the paid call. The model-facing
repository evidence may include a sanitized remote URL, branch, commit SHA,
detached state, and dirty flag. It must not include absolute paths, Git common
directories, status-entry paths, URL credentials, query strings, or fragments.

### File allowlist

Only these exact repository-relative paths are eligible:

```text
README.md
CONTEXT.md
pyproject.toml
specs/spec.json
specs/spec.md
docs/spec/spec.json
docs/spec/spec.md
```

The collector does not traverse directories and does not follow a symlink whose
resolved target leaves the repository worktree. Environment files, arbitrary
documents, source code, Git history, issues, pull requests, generated outputs,
and secret files are outside the boundary.

`pyproject.toml` is parsed with `tomllib`; only Project name, description,
keywords, and declared console scripts are exposed. A JSON specification is
parsed and validated with AgileForge's `TechnicalSpecArtifact` model before it
is included. Markdown is a fallback when the corresponding JSON candidate is
absent or invalid. Invalid or unsupported candidates are omitted with a typed
warning rather than sent as raw content.

### Bounds and order

- Maximum evidence items: 8
- Maximum serialized content per item: 32 KiB
- Maximum serialized evidence content: 96 KiB
- Text encoding: strict UTF-8
- Fingerprint: SHA-256 over canonical structured content
- Paths: repository-relative POSIX paths only
- Truncation: deterministic and explicitly marked

Stable priority is:

1. Project metadata
2. Repository provenance, when attached
3. Valid `docs/spec/spec.json`
4. Valid `specs/spec.json`
5. `CONTEXT.md`
6. `README.md`
7. Selected `pyproject.toml` metadata
8. Specification Markdown fallback, preferring `docs/spec/spec.md` over
   `specs/spec.md`

Both valid JSON specification locations may be included so conflicts are not
silently hidden. At most one Markdown fallback is included when no valid JSON
candidate exists. Excluded, invalid, unreadable, oversized, or conflicting
sources produce stable warnings. Text evidence is truncated at a valid UTF-8
boundary. Structured evidence is never made syntactically invalid by
truncation; an oversized structured item is omitted with a warning.

### Evidence contracts

The collector emits a strict structure equivalent to:

```python
class VisionEvidenceItem(BaseModel):
    evidence_id: str
    kind: Literal[
        "project_metadata",
        "repository_provenance",
        "readme",
        "context",
        "package_metadata",
        "technical_specification",
    ]
    relative_path: str | None
    content_fingerprint: str
    trust: Literal[
        "operator_provided",
        "observed_provenance",
        "unreviewed_repository_evidence",
    ]
    content: str | JsonObject
    truncated: bool


class VisionEvidenceWarning(BaseModel):
    code: str
    source: str
    message: str


class VisionEvidenceBundle(BaseModel):
    schema_version: Literal["agileforge.vision-evidence.v1"]
    items: tuple[VisionEvidenceItem, ...]
    warnings: tuple[VisionEvidenceWarning, ...]
    evidence_fingerprint: str
```

Evidence IDs are deterministic within the snapshot. Repository documents,
including an existing specification, are evidence only. They do not become
accepted Vision or specification authority through this flow.

## Model Input Contracts

Replace the current single `VisionInterviewInput` with a discriminated strict
union. Callers never construct these inputs; the host does.

### `VisionBootstrapInput`

- `schema_version`
- `operation = "bootstrap"`
- Project name and optional description
- Evidence snapshot ID and fingerprint
- Evidence items and warnings

It contains no required human response.

### `VisionClarificationInput`

- `schema_version`
- `operation = "clarification"`
- Project identity
- The same persisted evidence snapshot used by the draft lineage
- Current draft components and statement
- Current component basis, assumptions, and conflicts
- Human response as ordinary text
- IDs of the questions that response addresses, derived by the host

The addressed-question collection may be empty for feedback on a complete
review candidate. A caller never supplies question IDs; the host binds the
response to the currently active draft and its open questions.

### `VisionRevisionInput`

- `schema_version`
- `operation = "revision"`
- Project identity
- Current evidence snapshot
- Accepted Vision and fingerprint
- Human-provided revision reason
- Eligible active Product Goal status
- Prior review feedback, when present

All inputs forbid extra fields. Absolute local paths never enter any model
input.

## Model Output Contract

Replace `VisionInterviewOutput` with a strict output containing:

- `schema_version`
- All seven `VisionComponents`
- `component_basis`
- `draft_statement`
- `assumptions`
- `conflicts`
- Structured `clarifying_questions`
- `is_complete`

Each component basis entry names exactly one Vision component and includes:

- One or more source kinds: `human`, `evidence`, or `inference`
- Referenced evidence IDs
- Referenced assumption IDs

Each assumption and conflict has a stable ID and affected component names. Each
question has a stable ID and affected component names. Question text remains
ordinary language.

Semantic validation enforces:

1. All IDs are unique within their collection.
2. Every referenced evidence, assumption, conflict, and question ID exists.
3. Every non-null Vision component has exactly one component-basis entry.
4. `evidence` basis requires at least one evidence ID.
5. `inference` basis requires at least one assumption ID.
6. `human` basis is allowed only when the lineage contains human input.
7. `is_complete` is true exactly when all Vision components are substantive,
   no unresolved conflict exists, and no clarifying question remains.
8. An incomplete result includes at least one clarifying question.
9. Every unresolved conflict is addressed by at least one question.
10. A complete result may retain clearly disclosed assumptions; human review is
    the authority that accepts or challenges them.
11. Output contains no Product Goal, feature, requirement, story, task, or
    implementation plan.

Do not add numeric confidence scores. They imply precision the evidence cannot
support.

## Prompt Contract

The Vision prompt changes from "do not infer from repository contents" to:

- Propose a durable product-direction hypothesis from the supplied evidence.
- Distinguish direct human statements, direct evidence, and inference.
- Expose assumptions and conflicts instead of hiding them.
- Ask only questions that materially improve or resolve Vision components.
- Preserve human corrections across turns.
- Produce Vision only, never a Product Goal or delivery scope.
- Treat repository evidence as unreviewed context, not authority.
- Return only the strict output schema.

The model still has no tools and cannot read the repository itself.

## Persistence

### `VisionEvidenceSnapshot`

Add one immutable table with:

- `vision_evidence_snapshot_id`
- `project_id`
- Nullable `repository_binding_id`
- `workflow_node_attempt_id` for the attempt whose trusted input created it
- Canonical `evidence_json`
- `evidence_fingerprint`
- Canonical `warnings_json`
- `created_at`

The snapshot owns the exact evidence used for a successful generation. Its JSON
contains the collector schema version, so a separate mutable collector-version
column is unnecessary. The evidence bundle first enters the existing durable
`WorkflowNodeAttempt.normalized_input_json`; therefore the exact paid input is
recorded before provider dispatch. After strict output validation succeeds, the
output adapter binds that trusted attempt input to the positioned request, and
the graph handler creates the evidence snapshot and Vision turn atomically.
Failed or obsolete attempts retain diagnostic attempt input but create no
Vision evidence snapshot or Vision business fact.

### Vision lineage

Extend `VisionInterviewTurn` with:

- `operation`: `bootstrap`, `clarification`, or `revision`
- `vision_evidence_snapshot_id`
- Nullable `user_text`, because bootstrap has no human response
- `component_basis_json`
- `assumptions_json`
- `conflicts_json`

Every clarification turn in one draft lineage references the same evidence
snapshot. `VisionArtifact` also stores the source snapshot ID and final basis,
assumptions, and conflicts so review and later audits do not depend on replaying
transient model output.

Existing append-only artifact, decision, attempt, fingerprint, and revision
lineage rules remain. This is a hard break: schemas and initialization target a
fresh database. No compatibility columns, aliases, or data migration are added.
Old experimental databases and profiles are unsupported and must be recreated;
operating them is outside this change.

## Repository Freshness

Before `vision.bootstrap` invokes the provider, the host re-probes the attached
repository and compares the observation with the active binding. A mismatch in
worktree identity, HEAD, branch/detached state, dirty state, status fingerprint,
or remotes returns `REPOSITORY_PROVENANCE_STALE`. No paid call occurs. The
response advertises `agileforge repository refresh`.

Evidence reads also verify each selected file's identity, size, and modification
time around the read, then repeat the repository probe after collection. A
concurrent change returns `REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION`; no
paid call occurs.

Clarification and revision calls use their persisted snapshot and never
silently replace its model-facing content. Before a later paid call, the host
recollects the bounded evidence only for comparison and requires the current
evidence fingerprint to equal the lineage snapshot fingerprint. This detects
content changes even when a dirty file remains represented by the same Git
status entry. A mismatch returns `VISION_EVIDENCE_STALE` and blocks the call.
After repository refresh, an unchanged evidence fingerprint may continue the
existing lineage. A changed fingerprint leaves the old draft stale and the
workflow advertises `vision.bootstrap` to create a new explicit snapshot and
draft lineage.

Projects without repositories skip repository freshness checks.

## Graph And Requests

Add a required agentic node:

```text
node_id: vision.bootstrap
request_kind: generate_vision_bootstrap
required semantic inputs: none
```

`vision.interview` remains agentic but is available only when a persisted draft
has clarifying questions or human review returned feedback/rejection.
`vision.review` and `vision.revision.start` retain explicit human guards.

Add a positioned bootstrap request with request kind
`generate_vision_bootstrap` and keep `RecordVisionInterviewTurn` for later
clarification turns. Their output adapters bind model output to trusted attempt
input; the model cannot choose the evidence bundle, Project, mode, or snapshot
lineage. A shared handler persists the resulting turn and, for bootstrap or
revision generation, its evidence snapshot. The graph rejects cross-Project
attempts or snapshots, broken draft lineage, invalid basis references, and stale
positioned requests.

The graph position and `workflow next` output must advertise exactly one current
semantic command template. No caller-provided fingerprint or derived repository
field is exposed as a required input.

## Application, API, CLI, And UI

### Application service

Add `bootstrap_vision(...)` alongside `respond_to_vision(...)`. Both use the
same replay-safe agentic execution boundary. Bootstrap builds the evidence
bundle before dispatch; the existing node-attempt transaction durably records
it as normalized input. Clarification loads the lineage snapshot for model input
and recollects evidence only to enforce freshness.

### HTTP API

Add:

```text
POST /api/projects/{project_id}/vision/bootstrap
POST /api/projects/{project_id}/vision/respond
POST /api/projects/{project_id}/vision/review
GET  /api/projects/{project_id}/vision/status
```

The bootstrap body contains transport metadata only: idempotency key, actor,
and optional correlation ID. GET requests never invoke a model or mutate state.

### CLI

Add:

```bash
agileforge vision bootstrap \
  --project-id PROJECT_ID \
  --idempotency-key KEY \
  --actor ACTOR
```

Keep:

```bash
agileforge vision status --project-id PROJECT_ID
agileforge vision respond --project-id PROJECT_ID --text TEXT ...
agileforge vision review --project-id PROJECT_ID --decision ... --rationale ...
```

The CLI agent rereads `workflow position` and `workflow next` before every
mutation. It may iterate through bootstrap, status, response, and review for as
many human-guided turns as needed.

### Human UI

The initial Vision panel shows:

- A concise statement that AgileForge can propose a draft from available
  Project evidence
- A concise context summary: Project metadata and whether a repository is
  attached
- A `Generate Vision draft` button

It does not show fabricated fallback questions or an empty required response
box. During generation, the button has one stable loading state and cannot be
submitted twice.

An incomplete result shows the current draft, component basis, assumptions,
conflicts, focused questions, and one ordinary-language response field. A
complete result shows the same provenance plus Accept, Feedback, and Reject
controls. No raw JSON, internal fingerprints, snapshot IDs, or derived Git
fields are editable by the human.

## Replay, Failure, And Cost Safety

1. The caller supplies an idempotency key for every paid action.
2. The existing node-attempt lease permits only one active attempt for the
   selected Project, node, instance, and decision fingerprint. A competing
   request is rejected before provider invocation.
3. Reusing a key with the same semantic request returns the durable result and
   does not call the provider again.
4. Reusing a key with different semantic input returns an idempotency conflict.
5. Provider or runner failure may persist diagnostic attempt evidence, but it
   does not persist a Vision turn/artifact or advance the graph.
6. Schema-valid but semantically invalid model output receives at most one
   compact repair call containing only validation findings and the invalid
   output needed for repair.
7. A failed repair persists no Vision business fact and returns a typed error.
8. Retrying after a terminal failure requires a new explicit idempotency key.
9. Repository freshness, evidence collection, and contract validation happen
   before the first paid call whenever possible.

No action retries paid calls in an unbounded loop.

## Testing

All automated tests use deterministic fake runners/providers, temporary local
repositories, and blocked external networking. They do not touch the manual
acceptance profile, String Calculator Lab, caRtola, ASA, MyFinance, or any other
user repository.

Required coverage:

1. Project-only bootstrap with no repository.
2. Repository bootstrap using sanitized provenance, README, CONTEXT, package
   metadata, and valid canonical specification evidence.
3. Stable ordering, item/byte limits, deterministic truncation, and canonical
   fingerprints.
4. Symlink escape, invalid UTF-8, invalid TOML, invalid specification, duplicate
   spec locations, and unsupported-file warnings.
5. No absolute paths, credentials, status-entry paths, environment files,
   source code, or arbitrary documents in model input.
6. Complete and incomplete model outputs, evidence-reference validation,
   assumptions, conflicts, duplicate IDs, and strict rejection of extra Product
   Goal or delivery fields.
7. Bootstrap to interview, bootstrap to review, clarification loops, feedback,
   rejection, acceptance, and accepted-Vision revision.
8. Same-key replay without a second provider call, conflicting-key reuse, and
   concurrent-attempt rejection before provider invocation.
9. Stale repository prevention before paid calls and explicit restart after
   refresh when the evidence fingerprint changed.
10. Provider failure, one compact repair, repair failure, and no business-fact
    persistence on invalid output.
11. CLI command rendering and iterative semantic commands.
12. API GET purity and bootstrap/respond/review routes.
13. UI initial generation, loading, draft provenance, questions, review, and
    absence of raw internal fields.
14. Retained Project Vision to Product Goal eligibility after human acceptance.

The final engineering gate is:

```bash
uv lock --check
uv run --frozen pyrepo-check --all
git diff --check
```

Typing failures must be fixed without suppressions.

## Removal Scope

Delete or replace all current assumptions that the first Vision turn requires
human text:

- Static fallback Vision questions in `frontend/project.js`
- The initial required textarea and submit path
- The prompt rule that forbids repository inference
- The single human-first `VisionInterviewInput` contract
- Input-service logic that cannot build a turn without `user_response`
- Graph requirements that expose `mode` and `user_text` for the initial action
- Tests asserting repository/specification context is absent from Vision input

Retain the useful parts of the current implementation:

- Strict Pydantic contracts
- Append-only Vision turns, artifacts, decisions, and revision intents
- ADK agent isolation
- Durable positioned requests and node attempts
- Idempotent replay
- Explicit human review
- Product Goal gating on accepted Vision

## Rollout And Manual Acceptance

Implementation occurs in a separate branch and worktree based on the reviewed
integration commit. The pinned acceptance checkout and profile remain
unchanged.

Because this is a hard break, the current experimental acceptance profile is
discarded after the fix is reviewed. A new profile and database are initialized
from the repaired commit. The human then restarts Manual Test 1 and determines
whether AgileForge works. Automated tests prove implementation contracts only;
they do not make that acceptance decision.

## Acceptance Criteria

The change is ready for manual testing when:

1. A new Project with no repository advertises `vision.bootstrap`.
2. A Project with an attached repository advertises the same action.
3. Executing bootstrap requires no human product response and no raw JSON.
4. The first model result visibly distinguishes evidence, inference,
   assumptions, conflicts, and human input.
5. The CLI and UI can continue clarification until a reviewable candidate
   exists.
6. Only explicit human review can accept the Vision and unlock Product Goal.
7. Refreshing pages or reading status never invokes a model.
8. Replay cannot duplicate paid work.
9. Repository drift blocks paid work before provider invocation.
10. The full uv-only engineering gate passes without typing suppressions.
