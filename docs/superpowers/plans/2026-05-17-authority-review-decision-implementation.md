# Authority Review Decision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a complete pending Spec Authority review, accept, and reject path so projects in `authority_pending_review` can be manually canonicalized or rejected without dead-ending the CLI or dashboard.

**Architecture:** Add one review projection and one authority decision runner behind `AgentWorkbenchApplication`, then expose them through CLI, API, workflow-next, and dashboard actions. Review is read-only and produces a deterministic review token; accept/reject are guarded mutations using the existing mutation ledger and a single service boundary that owns canonical authority projection, decision logging, workflow advancement, and stale-context errors.

**Tech Stack:** Python 3, SQLModel/SQLite, FastAPI, vanilla JS dashboard, pytest, existing AgileForge mutation ledger and CLI envelope contracts.

---

## Source Of Truth

- Accepted spec: `docs/superpowers/specs/2026-05-17-authority-review-decision-design.md`
- Current CLI facade: `services/agent_workbench/application.py`
- Current authority projection: `services/agent_workbench/authority_projection.py`
- Current project mutation pattern: `services/agent_workbench/project_setup.py`
- Current mutation ledger: `services/agent_workbench/mutation_ledger.py`
- Current dashboard API/UI: `api.py`, `frontend/project.js`, `frontend/project.html`

## Implementation Rules

- Keep `cli/main.py` thin: parse args, build request objects, call `AgentWorkbenchApplication`, print one JSON envelope on stdout.
- Do not auto-accept authority during project creation or review.
- Do not make AgileForge run model-backed assessment in this slice.
- Do not let rejected rows satisfy accepted-authority reads.
- Do not accept with an incomplete review unless an explicit override flag and rationale are persisted.
- Do not allow dashboard decisions guarded only by authority fingerprint.
- Use the existing mutation ledger for accept/reject idempotency and recovery.
- Keep source-file hashes strict: `disk_sha256` hashes raw bytes, `source_content_sha256` hashes returned UTF-8 source text re-encoded exactly, and `source_spec_hash` is the persisted `SpecRegistry.spec_hash`.
- Commit after each task when its focused tests pass.

## File Map

### Create

- `services/agent_workbench/authority_review.py`  
  Builds the read-only review packet, source outline, coverage summary, coverage fingerprint, and review token.

- `services/agent_workbench/authority_decision.py`  
  Defines accept/reject request models, guard normalization, decision validation, idempotency handling, terminal decision writing, and workflow advancement.

- `tests/test_agent_workbench_authority_review.py`  
  Unit tests for review packet, token, source hashing, coverage summary, source limits, and malformed Markdown diagnostics.

- `tests/test_agent_workbench_authority_decision.py`  
  Unit tests for accept/reject service behavior, replay, stale guards, incomplete-review gating, terminal uniqueness, rejected-row safety, and workflow transitions.

- `tests/test_agent_workbench_authority_decision_cli.py`  
  CLI parser and JSON-envelope tests for `authority review`, `authority accept`, and `authority reject`.

- `tests/test_db_migrations_authority_decision.py`  
  Migration/readiness tests for decision provenance columns, terminal uniqueness, legacy backfill, and duplicate-terminal rejection.

### Modify

- `models/specs.py`  
  Add nullable provenance fields to `SpecAuthorityAcceptance` and update field descriptions for policy/actor modes.

- `db/migrations.py`  
  Bump storage schema to `3`, add decision columns, enforce terminal decision uniqueness, and backfill historical accepted rows.

- `services/agent_workbench/version.py`  
  Bump `STORAGE_SCHEMA_VERSION` to `3`.

- `services/agent_workbench/schema_readiness.py`  
  Add authority decision readiness requirements, including provenance columns and terminal-decision invariant verification.

- `services/agent_workbench/error_codes.py`  
  Add authority decision error codes and registry entries.

- `services/agent_workbench/authority_projection.py`  
  Route accepted-authority reads through status-filtered helpers, expose accepted decision provenance, and avoid treating rejected rows as accepted.

- `services/agent_workbench/application.py`  
  Add facade methods for review/accept/reject and replace `workflow_next` setup handling with authority-aware routing.

- `services/agent_workbench/command_registry.py`  
  Register `agileforge authority review`, `agileforge authority accept`, and `agileforge authority reject`.

- `services/agent_workbench/command_schema.py`  
  Ensure command schemas expose the new guard fields, review token mode, incomplete-review override, and error codes.

- `cli/main.py`  
  Add authority subcommands, human/agent argument validation, interactive confirmation phrases, and one-JSON-stdout behavior.

- `api.py`  
  Add dashboard API endpoints for authority review, accept, and reject using the same application facade; update project-state handling for `authority_pending_review` and `authority_rejected`.

- `frontend/project.html`  
  Add UI containers/buttons for pending authority review and rejection states.

- `frontend/project.js`  
  Render `Pending Authority Review`, fetch review packet, submit review-token decisions, reject fingerprint-only mutations, and refresh stale pages.

- `docs/agent-cli-manual.md`  
  Document the new authority review and decision flow after implementation tests pass.

- Existing tests:
  - `tests/test_agent_workbench_application.py`
  - `tests/test_agent_workbench_authority_projection.py`
  - `tests/test_agent_workbench_cli.py`
  - `tests/test_agent_workbench_command_schema.py`
  - `tests/test_api_dashboard.py`

---

## Task 1: Storage Contract, Migration, Readiness, And Error Codes

**Purpose:** Make the database and error taxonomy capable of storing guarded authority decisions before any service writes rows.

**Files:**
- Modify: `models/specs.py`
- Modify: `db/migrations.py`
- Modify: `services/agent_workbench/version.py`
- Modify: `services/agent_workbench/schema_readiness.py`
- Modify: `services/agent_workbench/error_codes.py`
- Test: `tests/test_db_migrations_authority_decision.py`
- Test: `tests/test_agent_workbench_error_codes.py`

- [ ] **Step 1: Write failing migration tests**

Create `tests/test_db_migrations_authority_decision.py` with tests named:

```python
def test_authority_decision_migration_adds_provenance_columns() -> None: ...
def test_authority_decision_migration_backfills_unambiguous_legacy_acceptance() -> None: ...
def test_authority_decision_migration_blocks_ambiguous_legacy_acceptance() -> None: ...
def test_terminal_decision_unique_key_blocks_duplicate_accept_reject_rows() -> None: ...
def test_schema_readiness_requires_terminal_decision_invariant() -> None: ...
```

The tests must create a temporary file-backed SQLite database, not an in-memory database shared across subprocesses. The minimal pre-migration `spec_authority_acceptance` table in the test must include only the current columns:

```sql
CREATE TABLE spec_authority_acceptance (
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL,
  spec_version_id INTEGER NOT NULL,
  status VARCHAR NOT NULL,
  policy VARCHAR NOT NULL,
  decided_by VARCHAR NOT NULL,
  decided_at DATETIME NOT NULL,
  rationale TEXT,
  compiler_version VARCHAR NOT NULL,
  prompt_hash VARCHAR NOT NULL,
  spec_hash VARCHAR NOT NULL
);
```

Expected failing output before implementation:

```text
FAILED tests/test_db_migrations_authority_decision.py::test_authority_decision_migration_adds_provenance_columns
```

- [ ] **Step 2: Add error-code tests**

Extend `tests/test_agent_workbench_error_codes.py` to assert the registry includes these stable codes:

```python
[
    "AUTHORITY_REVIEW_REQUIRED",
    "AUTHORITY_NOT_PENDING",
    "AUTHORITY_ALREADY_DECIDED",
    "AUTHORITY_SOURCE_CHANGED",
    "AUTHORITY_REVIEW_INCOMPLETE",
    "AUTHORITY_GUARD_INCOMPLETE",
]
```

Expected failing output:

```text
AssertionError: AUTHORITY_REVIEW_REQUIRED
```

- [ ] **Step 3: Add model fields**

In `models/specs.py`, extend `SpecAuthorityAcceptance` with nullable provenance fields:

```python
pending_authority_id: int | None = Field(default=None, index=True)
authority_fingerprint: str | None = Field(default=None, index=True)
review_token: str | None = Field(default=None, index=True)
review_fingerprint: str | None = Field(default=None)
disk_spec_hash: str | None = Field(default=None)
resolved_spec_path: str | None = Field(default=None)
actor_mode: str | None = Field(default=None)
review_completeness: str | None = Field(default=None)
incomplete_review_override: bool = Field(default=False)
incomplete_review_rationale: str | None = Field(default=None)
terminal_decision_key: str | None = Field(default=None, index=True)
provenance_source: str = Field(default="normal")
```

Update `policy` description to the accepted enum values:

```text
manual | agent_requested | dashboard_manual | test
```

Update `status` description to:

```text
accepted | rejected
```

- [ ] **Step 4: Add migration helpers and storage schema version**

In `db/migrations.py`, bump `AGENT_WORKBENCH_STORAGE_SCHEMA_VERSION` from `"2"` to `"3"`.

Add idempotent column additions for every new model field. Use a normalized `terminal_decision_key` to enforce terminal uniqueness without rebuilding the table:

```text
terminal_decision_key = '<product_id>:<spec_version_id>:<pending_authority_id>'
```

Create a unique index:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_spec_authority_terminal_decision_key
ON spec_authority_acceptance (terminal_decision_key)
WHERE terminal_decision_key IS NOT NULL;
```

Backfill historical terminal rows:

```text
For each accepted/rejected row with pending_authority_id IS NULL:
1. Find compiled_spec_authority rows with matching spec_version_id.
2. If exactly one row exists, set pending_authority_id to authority_id.
3. Set terminal_decision_key to product_id:spec_version_id:authority_id.
4. Set provenance_source to legacy_backfill.
5. If zero or multiple compiled rows exist, raise a migration error with remediation.
```

- [ ] **Step 5: Add readiness checks**

In `services/agent_workbench/schema_readiness.py`, add an authority decision requirement object that verifies:

```text
Table: spec_authority_acceptance
Columns:
- pending_authority_id
- authority_fingerprint
- review_token
- review_fingerprint
- disk_spec_hash
- resolved_spec_path
- actor_mode
- review_completeness
- incomplete_review_override
- incomplete_review_rationale
- terminal_decision_key
- provenance_source
Index:
- uq_spec_authority_terminal_decision_key
Storage schema version:
- 3
```

If the existing readiness helpers cannot verify indexes, add a small index query using:

```sql
PRAGMA index_list('spec_authority_acceptance')
```

- [ ] **Step 6: Add error codes**

In `services/agent_workbench/error_codes.py`, add registry entries with these retry semantics:

```text
AUTHORITY_REVIEW_REQUIRED       exit 4   retryable false
AUTHORITY_NOT_PENDING           exit 4   retryable false
AUTHORITY_ALREADY_DECIDED       exit 10  retryable false
AUTHORITY_SOURCE_CHANGED        exit 11  retryable true
AUTHORITY_REVIEW_INCOMPLETE     exit 20  retryable false
AUTHORITY_GUARD_INCOMPLETE      exit 2   retryable false
```

- [ ] **Step 7: Run focused tests**

Run:

```bash
uv run --frozen python -m pytest \
  tests/test_db_migrations_authority_decision.py \
  tests/test_agent_workbench_error_codes.py \
  -q
```

Expected:

```text
passed
```

- [ ] **Step 8: Commit**

Run:

```bash
git add models/specs.py db/migrations.py services/agent_workbench/version.py services/agent_workbench/schema_readiness.py services/agent_workbench/error_codes.py tests/test_db_migrations_authority_decision.py tests/test_agent_workbench_error_codes.py
git commit -m "feat: add authority decision storage contract"
```

---

## Task 2: Read-Only Authority Review Packet

**Purpose:** Give humans and agents a deterministic packet containing source evidence, compiled authority, coverage summary, guard tokens, and a review token.

**Files:**
- Create: `services/agent_workbench/authority_review.py`
- Modify: `services/agent_workbench/authority_projection.py`
- Test: `tests/test_agent_workbench_authority_review.py`

- [ ] **Step 1: Write failing review tests**

Create tests for:

```python
def test_review_returns_pending_authority_packet_with_guard_tokens() -> None: ...
def test_review_includes_full_source_under_default_limit() -> None: ...
def test_review_omits_large_source_and_marks_omission_incomplete() -> None: ...
def test_review_token_changes_when_disk_hash_changes() -> None: ...
def test_coverage_fingerprint_sorts_nested_covered_by_and_source_refs() -> None: ...
def test_malformed_markdown_emits_diagnostic_instead_of_failing() -> None: ...
def test_missing_spec_file_returns_spec_file_not_found() -> None: ...
def test_invalid_utf8_spec_file_returns_spec_file_invalid() -> None: ...
```

Expected failing output:

```text
ModuleNotFoundError: No module named 'services.agent_workbench.authority_review'
```

- [ ] **Step 2: Create review constants and hash helpers**

In `services/agent_workbench/authority_review.py`, define:

```python
AUTHORITY_REVIEW_COMMAND: Final[str] = "agileforge authority review"
REVIEW_TOKEN_SCHEMA: Final[str] = "agileforge.authority_review.v1"
COVERAGE_SCHEMA: Final[str] = "agileforge.authority_coverage_summary.v1"
DEFAULT_REVIEW_SOURCE_LIMIT_BYTES: Final[int] = 262_144
```

Add helpers:

```python
def sha256_prefixed(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def canonical_json_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return sha256_prefixed(encoded)
```

- [ ] **Step 3: Build source loading and outline parsing**

Implement strict source loading:

```text
1. Resolve the path from the latest SpecRegistry row.
2. Read raw bytes from the resolved disk path.
3. Return SPEC_FILE_NOT_FOUND when the file is missing.
4. Return SPEC_FILE_INVALID when strict UTF-8 decoding fails.
5. Hash raw bytes for disk_sha256.
6. Include source_content only when byte length <= configured limit or include-spec full is requested.
```

Implement Markdown section parsing:

```text
1. Split on heading lines matching ^#{1,6}\s+.
2. Create ROOT section for pre-heading content.
3. Track line_start and line_end.
4. Treat paragraphs, list items, table rows, and fenced code lines as content blocks.
5. Mark requirement-bearing blocks using the accepted normative-word and heading rules.
```

- [ ] **Step 4: Build coverage summary**

Generate `source_outline` entries with:

```json
{
  "section_id": "S1",
  "heading": "Submission Contract",
  "line_start": 12,
  "line_end": 48,
  "coverage_status": "covered",
  "covered_by": ["INV-1"],
  "classification_reason": null
}
```

Coverage rules:

```text
covered: every requirement-bearing block maps to at least one authority item source_ref.
intentionally_classified: every non-covered requirement-bearing block maps to a gap, assumption, rejected feature, or out-of-scope classification with non-empty reason.
partial: at least one requirement-bearing block is covered, and at least one remains unclassified.
uncovered: no requirement-bearing block has source coverage or classification.
```

Set `omission_assessment="complete"` only when every section is `covered` or `intentionally_classified` and `unclassified_content_blocks == 0`.

- [ ] **Step 5: Build pending authority artifact payload**

Use `services.specs.compiler_service.load_compiled_artifact` when possible so review and existing projections agree on compiled artifact structure. Normalize every authority item to include:

```text
id
text
support
source_refs
source_excerpt
```

For missing source refs:

```text
support = inferred
source_refs = []
source_excerpt = null
```

- [ ] **Step 6: Build guard token and review token payload**

The review token canonical payload must use exactly these fields:

```python
{
    "schema": REVIEW_TOKEN_SCHEMA,
    "project_id": project_id,
    "pending_authority_id": pending_authority_id,
    "authority_fingerprint": authority_fingerprint,
    "source_spec_hash": source_spec_hash,
    "disk_spec_hash": disk_spec_hash,
    "resolved_spec_path": resolved_spec_path,
    "compiler_version": compiler_version,
    "prompt_hash": prompt_hash,
    "fsm_state": fsm_state,
    "setup_status": setup_status,
    "content_included": content_included,
    "omission_assessment": omission_assessment,
    "coverage_summary_fingerprint": coverage_summary_fingerprint,
}
```

Return guard tokens:

```python
{
    "review_token": review_token,
    "pending_authority_id": pending_authority_id,
    "expected_authority_fingerprint": authority_fingerprint,
    "expected_source_spec_hash": source_spec_hash,
    "expected_disk_spec_hash": disk_spec_hash,
    "expected_resolved_spec_path": resolved_spec_path,
    "expected_state": "SETUP_REQUIRED",
    "expected_setup_status": "authority_pending_review",
    "expected_content_included": content_included,
    "expected_omission_assessment": omission_assessment,
    "expected_coverage_summary_fingerprint": coverage_summary_fingerprint,
}
```

- [ ] **Step 7: Add `AuthorityReviewService.review`**

Signature:

```python
class AuthorityReviewService:
    def __init__(self, *, engine: Engine | None = None) -> None: ...

    def review(
        self,
        *,
        project_id: int,
        include_spec: str = "auto",
        output_format: str = "json",
    ) -> dict[str, Any]: ...
```

Supported `include_spec` values:

```text
auto
full
summary
```

The service returns the repo-standard envelope shape:

```python
{"ok": True, "data": review_packet, "warnings": [], "errors": []}
```

- [ ] **Step 8: Run focused tests**

Run:

```bash
uv run --frozen python -m pytest tests/test_agent_workbench_authority_review.py -q
```

Expected:

```text
passed
```

- [ ] **Step 9: Commit**

Run:

```bash
git add services/agent_workbench/authority_review.py services/agent_workbench/authority_projection.py tests/test_agent_workbench_authority_review.py
git commit -m "feat: add authority review packet"
```

---

## Task 3: Authority Decision Runner

**Purpose:** Implement one guarded accept/reject mutation service that validates review tokens or explicit guards, writes exactly one terminal decision, promotes accepted authority, and updates workflow state.

**Files:**
- Create: `services/agent_workbench/authority_decision.py`
- Modify: `services/agent_workbench/authority_projection.py`
- Test: `tests/test_agent_workbench_authority_decision.py`

- [ ] **Step 1: Write failing decision tests**

Create tests named:

```python
def test_accept_with_review_token_promotes_authority_and_advances_to_vision() -> None: ...
def test_reject_with_review_token_records_rejection_and_keeps_setup_required() -> None: ...
def test_accept_fails_when_review_is_incomplete_without_override() -> None: ...
def test_accept_override_persists_rationale_policy_and_actor_mode() -> None: ...
def test_explicit_accept_missing_completeness_guards_fails() -> None: ...
def test_explicit_accept_fabricated_completeness_guards_fails() -> None: ...
def test_explicit_reject_allows_missing_completeness_guards() -> None: ...
def test_accept_after_reject_fails_authority_already_decided() -> None: ...
def test_reject_after_accept_fails_authority_already_decided() -> None: ...
def test_idempotency_same_key_replays_same_accept_response() -> None: ...
def test_idempotency_same_key_different_request_fails() -> None: ...
def test_decision_replay_runs_before_current_pending_state_validation() -> None: ...
def test_changed_disk_hash_after_review_fails_authority_source_changed() -> None: ...
def test_missing_disk_spec_at_decision_fails_specific_error() -> None: ...
def test_concurrent_accept_reject_records_one_terminal_decision() -> None: ...
def test_rejected_decision_never_satisfies_accepted_authority_projection() -> None: ...
```

Expected failing output:

```text
ModuleNotFoundError: No module named 'services.agent_workbench.authority_decision'
```

- [ ] **Step 2: Define request models**

In `services/agent_workbench/authority_decision.py`, define:

```python
class AuthorityDecisionBase(BaseModel):
    project_id: int
    review_token: str | None = None
    pending_authority_id: int | None = None
    expected_authority_fingerprint: str | None = None
    expected_source_spec_hash: str | None = None
    expected_disk_spec_hash: str | None = None
    expected_resolved_spec_path: str | None = None
    expected_state: str | None = None
    expected_setup_status: str | None = None
    expected_content_included: bool | None = None
    expected_omission_assessment: str | None = None
    expected_coverage_summary_fingerprint: str | None = None
    idempotency_key: str | None = None
    changed_by: str | None = None
    actor_mode: str = "cli-agent"
    policy: str = "agent_requested"
    correlation_id: str | None = None


class AuthorityAcceptRequest(AuthorityDecisionBase):
    allow_incomplete_review: bool = False
    incomplete_review_rationale: str | None = None


class AuthorityRejectRequest(AuthorityDecisionBase):
    reason: str
```

Validation rules:

```text
review_token mode: review_token is present and explicit guards may be omitted.
explicit mode: review_token is absent and pending/source/workflow guards are required.
explicit accept mode: completeness guards are required and match-validated.
explicit reject mode: completeness guards are optional, but supplied values are match-validated.
non-dry-run agent mode: idempotency_key is required.
human token mode: idempotency_key may be generated internally.
```

- [ ] **Step 3: Normalize both guard modes into one snapshot**

Define an internal immutable value:

```python
@dataclass(frozen=True)
class ReviewedAuthoritySnapshot:
    project_id: int
    pending_authority_id: int
    authority_fingerprint: str
    source_spec_hash: str
    disk_spec_hash: str
    resolved_spec_path: str
    compiler_version: str
    prompt_hash: str
    fsm_state: str
    setup_status: str
    content_included: bool
    omission_assessment: str
    coverage_summary_fingerprint: str
    review_token: str | None
```

Token mode parses and recomputes the current review snapshot, then compares token payload values.

Explicit mode recomputes the current review snapshot, then compares each supplied expected value to the recomputed value.

- [ ] **Step 4: Implement canonical request hash**

For idempotency, hash:

```text
command
decision
project_id
pending_authority_id
review_token or explicit guard tuple
expected_state
expected_setup_status
source hashes
expected_resolved_spec_path
expected_content_included
expected_omission_assessment
expected_coverage_summary_fingerprint
policy
actor_mode
allow_incomplete_review
incomplete_review_rationale
rejection reason
```

Exclude:

```text
correlation_id
generated timestamps
stdout format
tracing-only metadata
```

- [ ] **Step 5: Implement terminal decision key**

Add a helper:

```python
def terminal_decision_key(
    *,
    project_id: int,
    spec_version_id: int,
    pending_authority_id: int,
) -> str:
    return f"{project_id}:{spec_version_id}:{pending_authority_id}"
```

Every accepted/rejected row written by the service must set this value.

- [ ] **Step 6: Implement accept success**

Accept must perform these writes in one service method:

```text
1. Load or create mutation ledger row.
2. Acquire ledger lease.
3. Recompute review snapshot.
4. Check replayable existing terminal decision before stale current-state validation.
5. Validate pending authority is current.
6. Validate disk/source/workflow guards.
7. Validate no terminal decision exists for the terminal_decision_key.
8. Validate omission_assessment == complete unless override+rationale are present.
9. Insert SpecAuthorityAcceptance(status='accepted').
10. Store provenance fields.
11. Update workflow session to setup_status='passed', fsm_state='VISION_INTERVIEW', and clear setup error fields.
12. Finalize mutation ledger success with the response payload.
```

The success response must include:

```python
{
    "project_id": project_id,
    "authority_id": pending_authority_id,
    "accepted_decision_id": decision_id,
    "accepted_spec_version_id": spec_version_id,
    "authority_fingerprint": authority_fingerprint,
    "setup_status": "passed",
    "fsm_state": "VISION_INTERVIEW",
    "next_actions": [
        {
            "command": f"agileforge vision generate --project-id {project_id}",
            "reason": "Authority is accepted and Vision is unlocked.",
        }
    ],
}
```

- [ ] **Step 7: Implement reject success**

Reject must:

```text
1. Validate/replay idempotency.
2. Recompute review snapshot.
3. Validate source, pending authority, and workflow guards.
4. Insert SpecAuthorityAcceptance(status='rejected') with rationale.
5. Update workflow session to setup_status='authority_rejected' and fsm_state='SETUP_REQUIRED'.
6. Store rejection rationale in setup/status projection fields.
7. Finalize mutation ledger success.
```

The response must include:

```python
{
    "project_id": project_id,
    "pending_authority_id": pending_authority_id,
    "rejected_decision_id": decision_id,
    "setup_status": "authority_rejected",
    "fsm_state": "SETUP_REQUIRED",
    "reason": reason,
    "next_actions": [
        {
            "command": "agileforge project spec update --project-id <id> --spec-file <path>",
            "installed": False,
            "reason": "Spec update/recompile is required after rejection and is a later workflow slice.",
        }
    ],
}
```

- [ ] **Step 8: Implement recovery behavior for workflow update failure**

If the decision row is inserted and workflow update fails:

```text
1. Mark the mutation ledger recovery_required.
2. Include completed step decision_recorded.
3. Include next step workflow_state_written.
4. Replay same idempotency key by repairing workflow state from the terminal decision row.
```

Use the existing `ProjectSetupMutationRunner` progress/lease pattern as the implementation reference.

- [ ] **Step 9: Run focused tests**

Run:

```bash
uv run --frozen python -m pytest tests/test_agent_workbench_authority_decision.py -q
```

Expected:

```text
passed
```

- [ ] **Step 10: Commit**

Run:

```bash
git add services/agent_workbench/authority_decision.py services/agent_workbench/authority_projection.py tests/test_agent_workbench_authority_decision.py
git commit -m "feat: add guarded authority decisions"
```

---

## Task 4: Application Facade, Workflow Next, Command Registry, And Schemas

**Purpose:** Publish the feature through the stable application boundary and make agents discover the new commands and guard requirements.

**Files:**
- Modify: `services/agent_workbench/application.py`
- Modify: `services/agent_workbench/command_registry.py`
- Modify: `services/agent_workbench/command_schema.py`
- Test: `tests/test_agent_workbench_application.py`
- Test: `tests/test_agent_workbench_command_schema.py`

- [ ] **Step 1: Write failing application tests**

Add tests for:

```python
def test_application_authority_review_delegates_to_review_service() -> None: ...
def test_application_authority_accept_delegates_to_decision_runner() -> None: ...
def test_application_authority_reject_delegates_to_decision_runner() -> None: ...
def test_workflow_next_routes_pending_authority_to_review_and_decision_templates() -> None: ...
def test_workflow_next_routes_rejected_authority_to_recompile_unavailable_action() -> None: ...
def test_workflow_next_no_longer_calls_sprint_context_pack_when_setup_pending_review() -> None: ...
```

Expected failing output:

```text
AttributeError: 'AgentWorkbenchApplication' object has no attribute 'authority_review'
```

- [ ] **Step 2: Add facade protocols and methods**

In `services/agent_workbench/application.py`, add protocols for:

```python
class _AuthorityReview(Protocol):
    def review(self, *, project_id: int, include_spec: str = "auto", output_format: str = "json") -> dict[str, Any]: ...


class _AuthorityDecisionRunner(Protocol):
    def accept(self, request: AuthorityAcceptRequest) -> dict[str, Any]: ...
    def reject(self, request: AuthorityRejectRequest) -> dict[str, Any]: ...
```

Add facade methods:

```python
def authority_review(self, *, project_id: int, include_spec: str = "auto", output_format: str = "json") -> dict[str, Any]: ...
def authority_accept(self, request: AuthorityAcceptRequest) -> dict[str, Any]: ...
def authority_reject(self, request: AuthorityRejectRequest) -> dict[str, Any]: ...
```

- [ ] **Step 3: Replace workflow-next setup routing**

Update `workflow_next` so:

```text
If fsm_state == SETUP_REQUIRED and setup_status == authority_pending_review:
  return next_valid_commands ['agileforge authority review --project-id <id>']
  return decision_commands_after_review accept/reject templates with <review_token>
If fsm_state == SETUP_REQUIRED and setup_status == authority_rejected:
  return spec update/recompile action with installed=false
If fsm_state == SETUP_REQUIRED and setup_status == failed:
  return setup retry action
Otherwise:
  keep existing phase/context behavior
```

- [ ] **Step 4: Register command metadata**

In `services/agent_workbench/command_registry.py`, add installed command metadata:

```text
agileforge authority review
agileforge authority accept
agileforge authority reject
```

Required schema details:

```text
authority review:
  read_only=true
  inputs: project_id
  optional: include_spec, format
authority accept:
  mutating=true
  accepts review_token or complete explicit guard set
  errors include AUTHORITY_REVIEW_INCOMPLETE, AUTHORITY_ALREADY_DECIDED, AUTHORITY_SOURCE_CHANGED, AUTHORITY_GUARD_INCOMPLETE
authority reject:
  mutating=true
  requires reason
  accepts review_token or explicit source/workflow guards
  errors include AUTHORITY_ALREADY_DECIDED, AUTHORITY_SOURCE_CHANGED
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
uv run --frozen python -m pytest \
  tests/test_agent_workbench_application.py \
  tests/test_agent_workbench_command_schema.py \
  -q
```

Expected:

```text
passed
```

- [ ] **Step 6: Commit**

Run:

```bash
git add services/agent_workbench/application.py services/agent_workbench/command_registry.py services/agent_workbench/command_schema.py tests/test_agent_workbench_application.py tests/test_agent_workbench_command_schema.py
git commit -m "feat: publish authority decision commands"
```

---

## Task 5: CLI Commands And Human Ergonomics

**Purpose:** Install `agileforge authority review|accept|reject` with JSON-first behavior, clear help text, human token mode, agent explicit mode, and interactive confirmation.

**Files:**
- Modify: `cli/main.py`
- Modify: `tests/test_agent_workbench_cli.py`
- Create: `tests/test_agent_workbench_authority_decision_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Create tests for:

```python
def test_authority_review_parser_calls_application() -> None: ...
def test_authority_accept_with_review_token_does_not_require_idempotency_key() -> None: ...
def test_authority_accept_explicit_agent_mode_requires_idempotency_key() -> None: ...
def test_authority_accept_explicit_agent_mode_requires_completeness_guards() -> None: ...
def test_authority_reject_requires_reason() -> None: ...
def test_authority_reject_explicit_mode_requires_resolved_path_guard() -> None: ...
def test_authority_help_shows_review_accept_reject_examples() -> None: ...
def test_authority_review_keeps_stdout_json_clean_when_service_logs() -> None: ...
```

Expected failing output:

```text
invalid choice: 'review'
```

- [ ] **Step 2: Add parser commands**

In `cli/main.py`, add:

```text
agileforge authority review --project-id <id> [--include-spec auto|full|summary] [--format json|text]
agileforge authority accept --project-id <id> [--review-token <token>] [explicit guard fields] [--idempotency-key <key>] [--allow-incomplete-review] [--incomplete-review-rationale <text>] [--changed-by <actor>]
agileforge authority reject --project-id <id> [--review-token <token>] [explicit guard fields] --reason <text> [--idempotency-key <key>] [--changed-by <actor>]
```

Explicit guard fields:

```text
--pending-authority-id
--expected-authority-fingerprint
--expected-source-spec-hash
--expected-disk-spec-hash
--expected-resolved-spec-path
--expected-state
--expected-setup-status
--expected-content-included true|false
--expected-omission-assessment complete|incomplete
--expected-coverage-summary-fingerprint
```

- [ ] **Step 3: Add CLI validation**

Validation rules:

```text
accept/reject token mode:
  review-token present, idempotency-key optional, actor_mode cli-human unless changed-by indicates agent use
accept explicit mode:
  review-token absent, all explicit guards required, completeness guards required, idempotency-key required
reject explicit mode:
  review-token absent, source/workflow guards required, reason required, idempotency-key required, completeness guards optional but passed through if supplied
incomplete override:
  allow-incomplete-review requires incomplete-review-rationale
```

Return `INVALID_COMMAND` envelopes for parser/validation failures.

- [ ] **Step 4: Add interactive accept and reject**

When `authority accept --project-id <id>` has no `--review-token` and stdin is a TTY:

```text
1. Call authority review.
2. Print human-readable summary to stderr.
3. If omission assessment is complete, require typed phrase ACCEPT AUTHORITY.
4. If omission assessment is incomplete, require typed phrase ACCEPT INCOMPLETE AUTHORITY and a rationale.
5. Submit the same guarded internal request as token mode.
```

When `authority reject --project-id <id>` has no `--review-token` and stdin is a TTY:

```text
1. Call authority review.
2. Print human-readable summary to stderr.
3. Prompt for non-empty rationale.
4. Submit the same guarded internal request as token mode.
```

When stdin is not a TTY, missing token returns `AUTHORITY_REVIEW_REQUIRED`.

- [ ] **Step 5: Update CLI help examples**

Top-level help examples must include:

```text
agileforge authority review --project-id 1
agileforge authority accept --project-id 1 --review-token <review_token>
agileforge authority reject --project-id 1 --review-token <review_token> --reason "..."
```

`agileforge authority --help` must show `review`, `accept`, and `reject`.

- [ ] **Step 6: Run focused tests**

Run:

```bash
uv run --frozen python -m pytest \
  tests/test_agent_workbench_cli.py \
  tests/test_agent_workbench_authority_decision_cli.py \
  -q
```

Expected:

```text
passed
```

- [ ] **Step 7: Commit**

Run:

```bash
git add cli/main.py tests/test_agent_workbench_cli.py tests/test_agent_workbench_authority_decision_cli.py
git commit -m "feat: add authority decision cli"
```

---

## Task 6: Dashboard API And UI

**Purpose:** Make the human dashboard show pending authority review correctly and submit guarded decisions through the same application service.

**Files:**
- Modify: `api.py`
- Modify: `frontend/project.html`
- Modify: `frontend/project.js`
- Modify: `tests/test_api_dashboard.py`
- Add or modify JS tests if the existing test harness covers dashboard behavior.

- [ ] **Step 1: Write failing API/dashboard tests**

Add tests for:

```python
def test_project_state_preserves_authority_pending_review_not_failed() -> None: ...
def test_dashboard_authority_review_endpoint_returns_review_token() -> None: ...
def test_dashboard_accept_requires_review_token_or_full_guard_set() -> None: ...
def test_dashboard_accept_rejects_fingerprint_only_guard() -> None: ...
def test_dashboard_reject_records_reason_and_keeps_vision_locked() -> None: ...
def test_dashboard_pending_review_copy_is_not_project_setup_required() -> None: ...
```

Expected failing output:

```text
404 Not Found: /api/projects/4/authority/review
```

- [ ] **Step 2: Add API request/response models**

In `api.py`, add Pydantic request models:

```python
class AuthorityDecisionApiRequest(BaseModel):
    review_token: str | None = None
    pending_authority_id: int | None = None
    expected_authority_fingerprint: str | None = None
    expected_source_spec_hash: str | None = None
    expected_disk_spec_hash: str | None = None
    expected_resolved_spec_path: str | None = None
    expected_state: str | None = None
    expected_setup_status: str | None = None
    expected_content_included: bool | None = None
    expected_omission_assessment: str | None = None
    expected_coverage_summary_fingerprint: str | None = None
    allow_incomplete_review: bool = False
    incomplete_review_rationale: str | None = None


class AuthorityRejectApiRequest(AuthorityDecisionApiRequest):
    reason: str
```

- [ ] **Step 3: Add API routes**

Add routes:

```text
GET  /api/projects/{project_id}/authority/review
POST /api/projects/{project_id}/authority/accept
POST /api/projects/{project_id}/authority/reject
```

Each route must call `AgentWorkbenchApplication`, not duplicate decision logic.

Dashboard accept/reject request shaping:

```text
policy = dashboard_manual
actor_mode = dashboard-human
changed_by = authenticated dashboard user if available, else dashboard-human
review_token required unless complete explicit guard set is present
authority fingerprint alone rejected before service call
```

- [ ] **Step 4: Fix project-state classification**

Update `_effective_project_state` so:

```text
setup_status == authority_pending_review:
  fsm_state remains SETUP_REQUIRED
  setup_error remains null
  no blocker text is injected
setup_status == authority_rejected:
  fsm_state remains SETUP_REQUIRED
  setup_error explains rejection, not setup failure
setup_status == failed:
  existing failure behavior stays
setup_status == passed:
  Vision can unlock
```

- [ ] **Step 5: Add UI containers**

In `frontend/project.html`, add inside setup panel:

```html
<div id="authority-review-card" class="hidden rounded-lg border border-sky-200 bg-sky-50 p-4 text-sm text-sky-900 dark:border-sky-800 dark:bg-sky-950/40 dark:text-sky-100">
  <div class="flex items-start justify-between gap-4">
    <div>
      <h4 class="font-bold">Pending Authority Review</h4>
      <p id="authority-review-summary" class="mt-1 text-sm"></p>
    </div>
    <button id="btn-refresh-authority-review" onclick="loadAuthorityReview()" class="inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-xs font-bold">
      <span class="material-symbols-outlined text-sm">refresh</span> Refresh
    </button>
  </div>
  <pre id="authority-review-preview" class="mt-4 max-h-72 overflow-auto rounded bg-white/70 p-3 text-xs dark:bg-slate-900/70"></pre>
  <div class="mt-4 flex flex-wrap items-center gap-3">
    <button id="btn-accept-authority" onclick="acceptAuthorityReview()" class="inline-flex items-center gap-2 rounded-lg bg-emerald-600 px-4 py-2 text-sm font-bold text-white">
      <span class="material-symbols-outlined text-sm">verified</span> Accept Authority
    </button>
    <input id="authority-reject-reason" type="text" placeholder="Reason required to reject" class="min-w-[18rem] rounded-lg border px-3 py-2 text-sm dark:bg-slate-900" />
    <button id="btn-reject-authority" onclick="rejectAuthorityReview()" class="inline-flex items-center gap-2 rounded-lg bg-rose-600 px-4 py-2 text-sm font-bold text-white">
      <span class="material-symbols-outlined text-sm">block</span> Reject Authority
    </button>
  </div>
  <p id="authority-review-error" class="mt-3 hidden text-sm font-semibold text-rose-700 dark:text-rose-300"></p>
</div>
```

- [ ] **Step 6: Add UI state handling**

In `frontend/project.js`, add:

```javascript
let currentAuthorityReview = null;
```

Update `updateSetupStatusBanner()`:

```text
authority_pending_review -> title/message Pending Authority Review
authority_rejected -> title/message Authority Rejected
passed -> current green setup passed message
failed -> current amber failure message
```

Update `isPhaseReady('setup')`:

```text
true only when setup_status === 'passed' and activeFsmState !== 'SETUP_REQUIRED'
```

Update `fetchProjectFSMState()`:

```text
If setup_status == authority_pending_review, keep setup panel visible and call loadAuthorityReview().
If setup_status == authority_rejected, keep setup panel visible and do not show Vision.
```

- [ ] **Step 7: Add dashboard review/decision JS functions**

Add:

```javascript
async function loadAuthorityReview() { ... }
async function acceptAuthorityReview() { ... }
async function rejectAuthorityReview() { ... }
```

Rules:

```text
loadAuthorityReview fetches /api/projects/<id>/authority/review and stores currentAuthorityReview.
acceptAuthorityReview submits { review_token } from currentAuthorityReview.guard_tokens.review_token.
rejectAuthorityReview submits { review_token, reason }.
If API returns stale authority/source error, show reload/review-again message and call loadAuthorityReview().
Never submit authority fingerprint alone.
```

Expose functions on `window` as the file already does for other handlers.

- [ ] **Step 8: Run focused tests**

Run:

```bash
uv run --frozen python -m pytest tests/test_api_dashboard.py -q
```

Expected:

```text
passed
```

If JS test coverage exists for dashboard DOM behavior, run:

```bash
npm test
```

Expected:

```text
passed
```

If no `npm test` script exists, record that in the commit message body or final execution notes.

- [ ] **Step 9: Commit**

Run:

```bash
git add api.py frontend/project.html frontend/project.js tests/test_api_dashboard.py
git commit -m "feat: add dashboard authority decisions"
```

---

## Task 7: Documentation, Manual Flow, And End-To-End Verification

**Purpose:** Update the agent manual and prove the central shim, CLI, dashboard API, and project workflow work together.

**Files:**
- Modify: `docs/agent-cli-manual.md`
- Modify: tests touched by prior tasks as needed for final fixture consistency.

- [ ] **Step 1: Update CLI manual**

In `docs/agent-cli-manual.md`, add a section named:

```text
Authority Review And Decision
```

It must document:

```text
1. How to detect pending review:
   agileforge status --project-id <id>
   agileforge authority status --project-id <id>
   agileforge workflow next --project-id <id>

2. How to retrieve review packet:
   agileforge authority review --project-id <id>

3. What to ask an AI reviewer:
   "Does this compiled interpretation correctly represent the spec?"

4. How to accept:
   agileforge authority accept --project-id <id> --review-token <token>

5. How to reject:
   agileforge authority reject --project-id <id> --review-token <token> --reason "..."

6. What idempotency key means:
   A caller-generated retry key for non-interactive agent mutations. Human review-token mode hides it.

7. How to use explicit agent mode:
   include the complete guard tuple from guard_tokens and an idempotency key.

8. How to handle AUTHORITY_REVIEW_INCOMPLETE:
   rerun review with --include-spec full, or use explicit override with rationale if a human reviewed the omitted source out of band.
```

- [ ] **Step 2: Run focused Python test groups**

Run:

```bash
uv run --frozen python -m pytest \
  tests/test_agent_workbench_authority_review.py \
  tests/test_agent_workbench_authority_decision.py \
  tests/test_agent_workbench_authority_decision_cli.py \
  tests/test_agent_workbench_application.py \
  tests/test_agent_workbench_command_schema.py \
  tests/test_agent_workbench_authority_projection.py \
  tests/test_api_dashboard.py \
  tests/test_db_migrations_authority_decision.py \
  -q
```

Expected:

```text
passed
```

- [ ] **Step 3: Run full repository check**

Run:

```bash
pyrepo-check --all
```

Expected:

```text
All checks passed
```

If the exact success string differs, record the passing summary and failing command output in the execution notes.

- [ ] **Step 4: Run manual CLI smoke from a non-AgileForge repo**

Run from `/Users/aaat/projects/caRtola` or a temporary directory:

```bash
tmp_dir="$(mktemp -d)"
cd "$tmp_dir"
mkdir -p specs
cat > specs/app.md <<'SPEC'
# Product Requirements

The system must store submissions with a required submission id.
The system must reject submissions without a title.
The dashboard must show accepted submissions.
SPEC

agileforge project create \
  --name "Authority Decision Smoke $(date +%s)" \
  --spec-file specs/app.md \
  --idempotency-key "authority-smoke-create-$(date +%s)"
```

Capture `project_id` from the JSON response.

Run:

```bash
agileforge authority review --project-id "$project_id" > review.json
python -m json.tool review.json >/dev/null
review_token="$(python -c 'import json,sys; print(json.load(open("review.json"))["data"]["guard_tokens"]["review_token"])')"
agileforge authority accept --project-id "$project_id" --review-token "$review_token" > accept.json
python -m json.tool accept.json >/dev/null
agileforge authority status --project-id "$project_id" > status.json
python -m json.tool status.json >/dev/null
```

Expected assertions:

```bash
python - <<'PY'
import json
status = json.load(open("status.json"))
assert status["ok"] is True
assert status["data"]["status"] == "current"
assert status["data"]["authority_id"] is not None
assert status["data"]["pending_authority_id"] is None
print("authority accepted")
PY
```

- [ ] **Step 5: Run workflow-next smoke**

Before accept, `workflow next` must show review. After accept, it must no longer advertise authority review for the same project.

For a fresh pending project:

```bash
agileforge workflow next --project-id "$pending_project_id" > next-pending.json
python - <<'PY'
import json
payload = json.load(open("next-pending.json"))
commands = payload["data"]["next_valid_commands"]
assert "agileforge authority review --project-id" in " ".join(commands)
print("pending workflow-next ok")
PY
```

After accept:

```bash
agileforge workflow next --project-id "$project_id" > next-accepted.json
python -m json.tool next-accepted.json >/dev/null
```

- [ ] **Step 6: Commit docs and final fixture updates**

Run:

```bash
git add docs/agent-cli-manual.md tests services cli api.py frontend db models
git commit -m "docs: document authority decision workflow"
```

If there are no documentation-only changes after prior commits, skip this commit and record `git status --short`.

---

## Acceptance Criteria

- `agileforge authority review --project-id <id>` returns one JSON envelope with source evidence, compiled authority, coverage summary, guard tokens, assessment schema, and review token.
- `agileforge authority accept --project-id <id> --review-token <token>` records an accepted decision, promotes the accepted compiled authority in `authority status`, sets setup status to `passed`, and advances to `VISION_INTERVIEW`.
- `agileforge authority reject --project-id <id> --review-token <token> --reason "..."` records a rejected decision, keeps setup in `SETUP_REQUIRED`, sets setup status to `authority_rejected`, and does not unlock Vision.
- Explicit agent accept cannot bypass review completeness; fabricated completeness guards fail.
- Incomplete review cannot be accepted without explicit override and non-empty rationale.
- Authority fingerprint alone is never accepted as a dashboard mutation guard.
- Existing rejected rows never satisfy accepted-authority reads.
- `agileforge workflow next` advertises authority review while authority is pending.
- `agileforge capabilities` and `agileforge command schema` list review/accept/reject with documented errors.
- Dashboard labels pending review as `Pending Authority Review`, not `Project Setup Required`.
- `pyrepo-check --all` passes.

## Risk Checkpoints

Checkpoint after Task 1:
- Schema readiness cannot report ready without decision provenance columns and terminal uniqueness.

Checkpoint after Task 2:
- Review token is stable for the same snapshot and changes when source, authority, workflow, or coverage changes.

Checkpoint after Task 3:
- Accept/reject correctness works without CLI or dashboard code.

Checkpoint after Task 4:
- Agents can discover the next action through `workflow next` and command schema.

Checkpoint after Task 5:
- Humans can accept with a review token without understanding idempotency keys.

Checkpoint after Task 6:
- Dashboard cannot submit stale/fingerprint-only authority decisions.

## Execution Notes For Implementers

- Prefer a feature branch named `dev/authority-review-decision-phase-2c`.
- Use `rg` before editing shared files and avoid unrelated refactors.
- Keep accepted-authority reads behind service/repository helpers.
- Reuse `ProjectSetupMutationRunner` patterns for idempotency, leases, response replay, and recovery, but keep authority decision code in `authority_decision.py`.
- If dashboard authentication/CSRF is not implemented in this repo, the new API route must still require review token/full guard values and must not treat review token as authorization.
- The project spec update/recompile command after rejection is intentionally represented as `installed=false`; do not implement it in this slice.

## Final Verification Commands

Run before marking complete:

```bash
uv run --frozen python -m pytest tests/ -q
pyrepo-check --all
git status --short --branch
```

Expected:

```text
pytest passes
pyrepo-check passes
working tree clean except intentional uncommitted files
```

