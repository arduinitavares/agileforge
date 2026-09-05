# AgileForge Issue #245 Closure Audit Report

## 1. Environment and Checkout Baseline

- **Repository**: `C:\Users\atavares\Projects\agileforge`
- **Branch**: `fix/issue-245-specification-validation`
- **Current HEAD**: `0aee602bd5cd3ae3a7ca47082e3974dd5a75d283`
- **Initial Working-Tree State**: Clean (`nothing to commit, working tree clean`)
- **Upstream Merge Base**: `223601b0e52107309e9d2a4e3ab391246c1b935c` (`master` / `origin/master`)
- **Issue Reference**: [#245 Specification leaf output validation is misreported as provider execution failure](https://github.com/arduinitavares/agileforge/issues/245)
- **Primary Planning Documents**:
  - `docs/superpowers/plans/2026-09-02-issue-245-specification-validation-and-recovery.md`
  - `docs/testing/SPECIFICATION-OUTPUT-RECOVERY.md`

### Commit History on Branch Since Master
1. `4ff02299` - `docs: record issue 245 validation and recovery plan`
2. `c16e96ac` - `test: share Windows socketpair support for offline suites`
3. `0902b735` - `fix(git): release cached probe subprocesses before returning`
4. `2f2c9781` - `fix(adk): classify invalid specification output at the leaf`
5. `741b90ef` - `fix(adk): persist correlated specification output diagnostics`
6. `fdb18bad` - `test(adk): strengthen specification diagnostic coverage`
7. `81d77e25` - `fix(ui): report specification validation failures as terminal outcomes`
8. `73dafc92` - `fix(adk): clarify specification identity and historical fact preservation`
9. `cbc3680e` - `docs: document specification output diagnostics and investigation`
10. `5fa36156` - `fix(dev): support isolated Windows launcher processes`
11. `39f5f202` - `fix(workflow): validate specification recovery lineage`
12. `01414513` - `chore(models): route planning through Terra`
13. `56caf1cd` - `test(models): reflect Terra planning routes`
14. `790d0ad4` - `feat(backlog): support guided accepted-backlog correction`
15. `ca0287f6` - `fix(dev): provide profile-owned Windows temp`
16. `0aee602b` - `test: reconcile issue 245 verification evidence`

---

## 2. Acceptance Criteria Matrix

| Criterion | Evaluation | Evidence & Technical References |
| :--- | :---: | :--- |
| **A. Invalid generated output is classified at the actual ADK leaf boundary rather than mislabeled as a provider failure.** | **PASS** | **Implementation**: In [`adapters/adk/agents/specification_author.py`](file:///C:/Users/atavares/Projects/agileforge/adapters/adk/agents/specification_author.py#L101-L152), `root_agent` defines `after_model_callback=validate_specification_output`. The callback invokes [`validate_specification_response(...)`](file:///C:/Users/atavares/Projects/agileforge/adapters/adk/specification_output.py#L182-L314) before ADK's leaf output schema conversion (`output_schema=SpecificationStructuringOutput`) can run. Syntax, schema, or relation validation failures raise typed `SpecificationOutputValidationError(code="INVALID_SPECIFICATION_PAYLOAD")`. In ADK 2.2.0, `Context.run_node` wraps this in `DynamicNodeFailError`, which `NodeRunner` unwraps and re-raises. [`adapters/adk/runner.py`](file:///C:/Users/atavares/Projects/agileforge/adapters/adk/runner.py#L606-L617) catches `SpecificationOutputValidationError` and directly maps it to `WorkflowErrorCode.INVALID_SPECIFICATION_PAYLOAD`.<br>**Tests**: [`test_real_leaf_dangling_endpoint_is_invalid_payload`](file:///C:/Users/atavares/Projects/agileforge/tests/adapters/test_adk_specification_authoring_failures.py#L572-L602) and [`test_real_runner_output_validation_classification_matrix`](file:///C:/Users/atavares/Projects/agileforge/tests/adapters/test_adk_specification_authoring_failures.py#L604-L720).<br>**Commit**: `2f2c9781`. |
| **B. Invalid, unsupported, incomplete, and provider/transport failures retain intended distinctions across durable outcomes, CLI, and UI.** | **PASS** | **Durable outcomes**: [`workflow/contracts.py`](file:///C:/Users/atavares/Projects/agileforge/workflow/contracts.py) and [`adapters/adk/specification_output.py`](file:///C:/Users/atavares/Projects/agileforge/adapters/adk/specification_output.py) strictly separate `INVALID_SPECIFICATION_PAYLOAD`, `UNSUPPORTED_SPECIFICATION_SCHEMA`, `SPECIFICATION_OUTPUT_INCOMPLETE`, and `SPECIFICATION_PRODUCER_FAILED`.<br>**CLI**: [`cli/workflow_commands.py`](file:///C:/Users/atavares/Projects/agileforge/cli/workflow_commands.py) renders the structured error payload and exits nonzero, verified by [`test_specification_structure_cli_returns_nonzero_on_invalid_payload`](file:///C:/Users/atavares/Projects/agileforge/tests/adapters/test_cli_workflow_domain.py).<br>**API**: Returns HTTP 409 with structured error envelope, verified by [`test_specification_structure_api_returns_409_conflict_on_invalid_payload`](file:///C:/Users/atavares/Projects/agileforge/tests/adapters/test_api_workflow_domain.py).<br>**UI**: [`frontend/project.js`](file:///C:/Users/atavares/Projects/agileforge/frontend/project.js#L5056-L5075) `specificationStructuringFailureMessage(...)` maintains `terminalCodes = new Set(['SPECIFICATION_PRODUCER_FAILED', 'INVALID_SPECIFICATION_PAYLOAD', 'UNSUPPORTED_SPECIFICATION_SCHEMA', 'SPECIFICATION_OUTPUT_INCOMPLETE'])`. Terminal failures render definitive messages ("Specification structuring failed. No new candidate was produced."), distinguishing initial mode ("The registered source remains current.") from feedback mode ("The prior candidate and Feedback remain current."). Uncertain errors (503, connection drops) preserve instructions to refresh and verify state.<br>**Tests**: Verified by 10 dedicated cases in [`tests/test_workflow_position_display.mjs`](file:///C:/Users/atavares/Projects/agileforge/tests/test_workflow_position_display.mjs#L3320-L3388) (tests 82–91).<br>**Commits**: `2f2c9781`, `81d77e25`. |
| **C. Safe diagnostics retain attempt/session/invocation correlation; diagnostic persistence failures do not mask original failure.** | **PASS** | **Correlation & Safety**: In [`adapters/adk/runner.py`](file:///C:/Users/atavares/Projects/agileforge/adapters/adk/runner.py#L591-L663), `run_async(..., yield_user_message=True)` captures `invocation_id` from the initial user event (`author == "user"`). On `SpecificationOutputValidationError`, [`_append_specification_output_diagnostic(...)`](file:///C:/Users/atavares/Projects/agileforge/adapters/adk/runner.py#L625-L664) appends `Event(author="specification_output_validator", invocation_id=invocation_id, actions=EventActions(state_delta={"specification_output_diagnostic": diagnostic}))`. Diagnostics contain only schema `agileforge.specification-output-diagnostic.v1` with bounded fields (response SHA-256, byte count, token counts, item/relation counts, missing IDs capped at 100). No raw prompt/prose, statements, rationale, credentials, or Pydantic error traces are recorded.<br>**Fail-Safe Persistence**: Diagnostic append errors catch generic `Exception`, emit a fixed safe warning without `exc_info`, and preserve the original business failure (`INVALID_SPECIFICATION_PAYLOAD`).<br>**Evidence Scope Qualification**: Test-backed diagnostics are strictly distinguished from live recovery evidence. The runtime database (`pid-verification-terra`) contains no live diagnostic event because historical Attempt 6 predated commit `741b90ef`, Attempts 7 & 9 failed with provider rate limits, Attempt 8 failed on stale input, and Attempts 10 & 11 succeeded. Durable proof for diagnostic event persistence, attempt/invocation correlation, schema constraints, and fail-safe non-masking is provided exclusively by automated integration tests, not by live recovery traces.<br>**Tests**: Verified by [`test_invalid_output_diagnostics_do_not_echo_response_prose`](file:///C:/Users/atavares/Projects/agileforge/tests/adapters/test_specification_output.py#L124-L145), [`test_output_validation_failure_persists_correlated_diagnostic_in_memory`](file:///C:/Users/atavares/Projects/agileforge/tests/adapters/test_adk_specification_authoring_failures.py#L1753-L1874), [`test_output_validation_failure_persists_correlated_diagnostic_sqlite`](file:///C:/Users/atavares/Projects/agileforge/tests/adapters/test_adk_specification_authoring_failures.py#L1876-L2024), [`test_specification_output_diagnostic_append_failure_preserves_business_failure`](file:///C:/Users/atavares/Projects/agileforge/tests/adapters/test_adk_specification_authoring_failures.py#L2026-L2090), and [`test_precedence_post_call_revalidation_supersedes_output_diagnostic`](file:///C:/Users/atavares/Projects/agileforge/tests/adapters/test_adk_specification_authoring_failures.py#L2092-L2155).<br>**Commits**: `741b90ef`, `fdb18bad`. |
| **D. Dangling relations remain rejected. Registered sources, approved requirements, and human acceptance remain protected.** | **PASS** | **Validation Invariant**: [`utils/agileforge_spec_profile_v2.py`](file:///C:/Users/atavares/Projects/agileforge/utils/agileforge_spec_profile_v2.py#L254-L281) `_validate_relations(...)` verifies that every relation endpoint names an existing item ID in `items`. Missing endpoints raise `ValidationError("unknown relation endpoint: ...")`. [`adapters/adk/specification_output.py`](file:///C:/Users/atavares/Projects/agileforge/adapters/adk/specification_output.py#L275-L313) surfaces missing endpoint IDs in the safe error message up to 5 endpoints (and `(+N more)` beyond).<br>**Lineage & Authority Guards**: [`workflow/handlers/product_discovery.py`](file:///C:/Users/atavares/Projects/agileforge/workflow/handlers/product_discovery.py#L648-L760) enforces strict source registration fingerprints, immutable frozen lineage, and recovery references via [`_recovered_attempt(...)`](file:///C:/Users/atavares/Projects/agileforge/workflow/handlers/product_discovery.py#L648-L715). Invalid output creates no candidate; valid output creates a review candidate; acceptance remains strictly human-gated pursuant to ADR 0005.<br>**Tests**: [`test_real_leaf_dangling_endpoint_is_invalid_payload`](file:///C:/Users/atavares/Projects/agileforge/tests/adapters/test_adk_specification_authoring_failures.py#L572-L602) (source remains registered, 0 candidates), [`test_issue_245_valid_output_preserves_relation_and_creates_candidate`](file:///C:/Users/atavares/Projects/agileforge/tests/adapters/test_adk_specification_authoring_failures.py) and [`test_issue_245_omitted_historical_item_fails_and_preserves_source`](file:///C:/Users/atavares/Projects/agileforge/tests/adapters/test_adk_specification_authoring_failures.py) (exercising historical INFORMATIVE item, normative requirement, and relation using sanitized fixture [`tests/fixtures/issue_245/`](file:///C:/Users/atavares/Projects/agileforge/tests/fixtures/issue_245/)), [`test_unknown_relation_endpoint_formatting_up_to_five`](file:///C:/Users/atavares/Projects/agileforge/tests/adapters/test_specification_output.py#L147-L168), [`test_unknown_relation_endpoint_formatting_exceeding_five`](file:///C:/Users/atavares/Projects/agileforge/tests/adapters/test_specification_output.py#L170-L185), [`test_unknown_relation_endpoint_formatting_exceeding_one_hundred`](file:///C:/Users/atavares/Projects/agileforge/tests/adapters/test_specification_output.py#L227-L245).<br>**Commits**: `2f2c9781`, `39f5f202`. |
| **E. Valid output still produces a candidate; failure paths do not add hidden retries or automatic acceptance.** | **PASS** | **Dispatch Limits**: [`adapters/adk/recipes.py`](file:///C:/Users/atavares/Projects/agileforge/adapters/adk/recipes.py#L1076-L1133) configures `RetryConfig(max_attempts=1)` for both the `@node` and `Workflow`. Single-turn mode (`mode="single_turn"`, `disallow_transfer_to_parent=True`, `disallow_transfer_to_peers=True`) in [`adapters/adk/agents/specification_author.py`](file:///C:/Users/atavares/Projects/agileforge/adapters/adk/agents/specification_author.py#L137-L152). At most one model call is dispatched per attempt.<br>**Review & Acceptance Boundary**: Invalid output creates no candidate; valid output creates a review candidate; acceptance remains strictly human-gated.<br>**Tests**: [`test_real_runner_max_attempts_two_makes_single_dispatch_on_invalid_output`](file:///C:/Users/atavares/Projects/agileforge/tests/adapters/test_adk_specification_authoring_failures.py#L765-L804) verifies that even with `max_attempts=2` in settings, invalid output fails immediately after 1 dispatch (`model.calls == ["provider"]`), producing 0 candidates and 0 automatic approvals. [`test_complete_realistic_response_persists_one_exact_canonical_candidate`](file:///C:/Users/atavares/Projects/agileforge/tests/adapters/test_adk_specification_authoring_failures.py#L1066-L1155) verifies valid output produces one exact canonical candidate without auto-acceptance. [`test_issue_245_valid_output_preserves_relation_and_creates_candidate`](file:///C:/Users/atavares/Projects/agileforge/tests/adapters/test_adk_specification_authoring_failures.py) confirms review candidate creation without automatic acceptance.<br>**Commit**: `2f2c9781`. |
| **F. Historical facts, target requirements, and prompt provenance are handled as specified.** | **PASS** | **Prompt Clarifications**: [`adapters/adk/prompts/specification_author.txt`](file:///C:/Users/atavares/Projects/agileforge/adapters/adk/prompts/specification_author.txt#L7-L9) explicitly guides the model to preserve typed item IDs and relations, and keep historical implementation facts distinct from target requirements (`INFORMATIVE` level, no silent ID replacement, preservation of normative transition force).<br>**Provenance & Contracts**: [`services/contracts/specification_authoring.py`](file:///C:/Users/atavares/Projects/agileforge/services/contracts/specification_authoring.py#L39-L54): `SPECIFICATION_STRUCTURER_VERSION = "1.0.2"`, `SPECIFICATION_STRUCTURER_PROMPT_VERSION = "agileforge.specification-structurer.prompt.v3"`, `SPECIFICATION_STRUCTURER_PROMPT_HASH = "sha256:ecc68026d01a9ade96707e345c47d2fe07acf3fcf37da82b7a739f9cfed6d00f"`.<br>**Source-Byte Preservation**: Production inputs preserve exact bytes for LF (`\n`) and CRLF (`\r\n`).<br>**Source-Contract Analysis**: Evaluated against the `to-spec` template (`SKILL.md`) in [`tests/fixtures/issue_245/README.md`](file:///C:/Users/atavares/Projects/agileforge/tests/fixtures/issue_245/README.md), highlighting that `to-spec` produces unstructured prose across seven sections without typed IDs or closed relation schemas, while AgileForge's structurer prompt bridges this by extracting declared IDs and enforcing `agileforge.spec.v2` graph closure.<br>**Semantic Preservation Qualification**: Prompt and provenance tests verify instruction packaging, configuration parameters, and SHA-256 hash bindings, but do not guarantee semantic preservation or completeness of non-deterministic model outputs. Structural validation rejection combined with human review and acceptance remain mandatory.<br>**Tests**: [`test_structurer_prompt_hash_binds_the_actual_packaged_instructions`](file:///C:/Users/atavares/Projects/agileforge/tests/adapters/test_specification_author_agent.py#L176-L200), [`test_changing_structurer_instructions_changes_prompt_hash`](file:///C:/Users/atavares/Projects/agileforge/tests/adapters/test_specification_author_agent.py#L202-L214), [`test_production_structurer_receives_explicit_id_and_historical_fact_instructions`](file:///C:/Users/atavares/Projects/agileforge/tests/adapters/test_specification_author_agent.py#L254-L266), [`test_real_runner_preserves_exact_source_bytes_lf_and_crlf`](file:///C:/Users/atavares/Projects/agileforge/tests/adapters/test_adk_specification_authoring_failures.py#L862-L913), [`test_issue_245_valid_output_preserves_relation_and_creates_candidate`](file:///C:/Users/atavares/Projects/agileforge/tests/adapters/test_adk_specification_authoring_failures.py).<br>**Commits**: `2f2c9781`, `73dafc92`. |
| **G. An authorized successful P&ID structuring run and semantic acceptance are supported by durable evidence.** | **PASS** | **Durable SQLite Records** (inspected via `file:...sqlite3?mode=ro` in `.agileforge/dev/profiles/pid-verification-terra/business.sqlite3` and `adk-trace.sqlite3`):<br>1. *Initial Defect Run*: Attempt 6 (`2026-09-02 15:29:35`, `input_fingerprint=sha256:314a907e...`, `attempt_fingerprint=sha256:677e888e10...`, registered source ID 2) recorded `SPECIFICATION_PRODUCER_FAILED`, with ADK trace event recording `Value error, unknown relation endpoint: DATA.review-state-current`.<br>2. *Authorized Structuring Attempt 10* (`2026-09-03 15:43:59`, identical `input_fingerprint=sha256:314a907e...` and source ID 2, `attempt_fingerprint=sha256:f6785eec4b...`): Status `success`. Model: `openrouter/openai/gpt-5.6-luna`. Produced `SpecificationCandidate` 1 (50 items including `DATA.review-state-current`, 24 closed relations, 0 missing endpoints; envelope `producer_version="1.0.2"`, `prompt_version="agileforge.specification-structurer.prompt.v3"`, `prompt_fingerprint="sha256:ecc68026..."`, `payload_fingerprint="sha256:8194b838..."`).<br>3. *Operator Feedback Decision*: `SpecificationDecision` 1 (`2026-09-03 16:01:00`, decision `feedback`, reviewer `operator`) requesting restoration of approved source requirements for `REQ.two-consent-model`, `REQ.retention-and-recovery`, `REQ.runtime-and-upgrades`, and `REQ.local-pilot-bootstrap`.<br>4. *Authorized Structuring Attempt 11* (`2026-09-03 16:01:40`, amendment input, source ID 2, `attempt_fingerprint=sha256:04e7e7aea...`): Status `success`. Produced `SpecificationCandidate` 2 (50 items, 24 relations, 0 missing endpoints, `DATA.review-state-current` present; envelope `payload_fingerprint="sha256:ad016666..."`).<br>5. *Human Semantic Acceptance*: `SpecificationDecision` 2 (`2026-09-03 16:11:24`, decision `accepted`, reviewer `operator`).<br>6. *Approved Delivery Authority*: `spec_registry` record (`spec_version_id=1`, `spec_hash="sha256:ad016666e7f02fb5ff8397cff2974113ba1057c28dfe68831faaad3e6489710a"`, `status="approved"`, `created_at="2026-09-03 16:11:25"`).<br>7. *Corroborating Downstream Progress*: Backlog Artifact 4 (`version_number=3`, `supersedes_backlog_artifact_id=3`, `2026-09-04 10:55:36`), Roadmap Artifact 2 (`backlog_artifact_id=4`, `version_number=1`, `2026-09-04 13:33:44`), Story Artifact 3 for PBI-000001 (`version_number=3`, `supersedes_story_artifact_id=2`, `2026-09-04 20:02:03`).<br>*Causation & Evidence Qualification*: Live recovery evidence demonstrates workflow unblocking, review candidate production, feedback reconciliation, and human acceptance; it is distinct from test-backed diagnostics (which verify failure logging and telemetry). Furthermore, successful recovery does not establish the root cause of the model's historical omission in Attempt 6 or guarantee future semantic completeness. |

---

## 3. Focused Verification Results

Sections A–F record the original 267 focused passes on the earlier `ca0287f6b40fa779ce20b795373a29710e9ec4c8` baseline. Section H records the subsequent recipe-registry verification; Section I records the two new regressions verified in the working tree based on `0aee602bd5cd3ae3a7ca47082e3974dd5a75d283`. Python test targets used `uv run --no-sync --offline` with the repository's pytest-socket configuration. Frontend test targets ran locally using `node --test`, which does not itself enforce process-level network blocking.

> [!NOTE] **Repository Suite & Verification Scope**
> The results below accumulate 270 distinct focused passes across those verification stages; they are not one fresh 270-test run. The historical full repository suite did not pass. This audit documents bounded #245 verification, not repository-wide greenness.

### A. Frontend Failure Presentation & Uncertainty Suite
```powershell
node --test tests/test_workflow_position_display.mjs
```
- **Passed**: 91 / 91 tests (452 ms)
- **Failures**: 0
- **Coverage Highlights**:
  - `known invalid Specification output (SPECIFICATION_PRODUCER_FAILED)` for initial and amendment structuring (tests 82–83)
  - `known invalid Specification output (INVALID_SPECIFICATION_PAYLOAD)` for initial and amendment structuring (tests 84–85)
  - `known invalid Specification output (UNSUPPORTED_SPECIFICATION_SCHEMA)` for initial and amendment structuring (tests 86–87)
  - `known invalid Specification output (SPECIFICATION_OUTPUT_INCOMPLETE)` for initial and amendment structuring (tests 88–89)
  - 503 network failure retaining uncertain outcome message and refresh instructions (test 90)
  - Unknown 409 error retaining uncertain outcome message (test 91)

### B. Pure Specification Output Classifier & Diagnostic Suite
```powershell
uv run --no-sync --offline pytest tests/adapters/test_specification_output.py -v
```
- **Passed**: 15 / 15 tests (4.00 s)
- **Failures**: 0
- **Coverage Highlights**:
  - Structurally valid response returns `SpecificationStructuringOutput`
  - Classification matrix across malformed, incomplete, and unsupported versions
  - Redaction test verifying private sentinels are not echoed in error, repr, or diagnostic payload
  - Missing relation endpoint formatting (up to 5, >5 with `(+N more)`, and >100)
  - Empty model responses with `SAFETY` or `OTHER` finish reasons classified as invalid payload

### C. Structurer Agent Specification & Configuration Suite
```powershell
uv run --no-sync --offline pytest tests/adapters/test_specification_author_agent.py -v
```
- **Passed**: 22 / 22 tests (17.72 s)
- **Failures**: 0
- **Coverage Highlights**:
  - Clean-process import boundary check
  - Dedicated ADK generation config mapping (`max_output_tokens=24576`, production `32768`)
  - Binding of `SPECIFICATION_STRUCTURER_PROMPT_HASH` (`sha256:ecc68026...`) to packaged instructions
  - Invalidation of prompt hash when primary instructions are modified
  - Verification of explicit ID preservation and historical fact instruction phrases

### D. ADK Specification Authoring Failures & Real-Runner Regression Suite
```powershell
uv run --no-sync --offline pytest tests/adapters/test_adk_specification_authoring_failures.py -v
```
- **Passed**: 44 / 44 tests (174.71 s)
- **Failures**: 0
- **Coverage Highlights**:
  - `test_real_leaf_dangling_endpoint_is_invalid_payload`: Deterministic integration regression reproducing the exact issue #245 dangling relation defect using a real ADK `Agent` with `SpecificationStructuringInput` and `SpecificationStructuringOutput`. Asserts failure code `INVALID_SPECIFICATION_PAYLOAD`, 0 candidates, and preserved source registration.
  - Parameterized real-runner classification matrix across dangling endpoints, missing required fields, non-EOF malformed JSON, explicit v1 schema, cut-off JSON (old and new callbacks), max-tokens truncation (old and new callbacks), and empty safety/other responses.
  - Fail-closed single dispatch with `max_attempts=2` settings.
  - Correlated safe diagnostic event persistence in memory and SQLite ADK trace databases, matching user, leaf, and diagnostic `invocation_id`.
  - Diagnostic append failure safety (warning logged, business error preserved, 0 retries).
  - Byte-exact preservation of LF and CRLF source registrations.
  - Source drift precedence over leaf diagnostics.

### E. Specification API and CLI Workflow Domain Suite
```powershell
uv run --no-sync --offline pytest tests/adapters/test_api_workflow_domain.py tests/adapters/test_cli_workflow_domain.py -k "specification or structuring" -v
```
- **Passed**: 50 / 50 matching tests (7.93 s; 459 deselected)
- **Failures**: 0
- **Coverage Highlights**:
  - `test_specification_structure_api_returns_409_conflict_on_invalid_payload`: Confirms API 409 conflict envelope on invalid payload.
  - `test_specification_structure_cli_returns_nonzero_on_invalid_payload`: Confirms CLI returns nonzero exit code and prints structured error payload.
  - Registration, preview, review, and semantic command parsing suites.

### F. ADK Workflow Runner General Integration Suite
```powershell
uv run --no-sync --offline pytest tests/adapters/test_adk_workflow_runner.py -v
```
- **Passed**: 45 / 45 tests (28.52 s)
- **Failures**: 0

### G. Static Analysis & Lint Gate
```powershell
uv run --no-sync --offline ruff check adapters/adk/ services/contracts/specification_authoring.py utils/agileforge_spec_profile_v2.py tests/adapters/
```
- **Status**: Passed (0 errors, 0 warnings).

**Accumulated Focused Evidence**: **270 distinct passes across separate verification stages (91 node + 179 pytest: 176 base + 1 recipe registry + 2 issue #245 regressions).**

### H. Agentic Recipe Registry Correction & Verification
Following the initial audit pass, a recipe registry test failure was identified in `tests/adapters/test_adk_graph_recipes.py::test_recipe_registry_covers_each_stable_agentic_domain_node_once`. The node `specification.structure` intentionally runs with production `max_attempts=1` (per `adapters/adk/recipes.py:1076`), but was missing from the registry test's intentional single-attempt set.

- **Correction Applied**: Added `"specification.structure"` to the intentional single-attempt set in `test_recipe_registry_covers_each_stable_agentic_domain_node_once`, preserving production `max_attempts=1`.
- **Verification Command**:
  ```powershell
  uv run --no-sync --offline pytest tests/adapters/test_adk_graph_recipes.py::test_recipe_registry_covers_each_stable_agentic_domain_node_once -q
  ```
- **Result**: `1 passed, 4 warnings in 2.66s`.

### I. Sanitized Historical & Normative Relation Regression Suite (Issue #245 Fixture)
Generic synthetic specification fixtures under [`tests/fixtures/issue_245/`](file:///C:/Users/atavares/Projects/agileforge/tests/fixtures/issue_245/) (`source.md` and `README.md`) provide an invented specification containing:
- An informative historical baseline item: `DATA.legacy-audit-record` (level `INFORMATIVE`, verification `inspection`).
- A normative replacement requirement: `REQ.audit-trail-authority` (level `MUST`, verification `integration-test`).
- A supported cross-item relation referencing the historical item: `REQ.audit-trail-authority tracks DATA.legacy-audit-record`.

Two focused regression tests were added in [`tests/adapters/test_adk_specification_authoring_failures.py`](file:///C:/Users/atavares/Projects/agileforge/tests/adapters/test_adk_specification_authoring_failures.py):
1. `test_issue_245_valid_output_preserves_relation_and_creates_candidate`: Confirms that valid output preserving both items and their relation executes with a single dispatch, creates one review candidate in `specification_candidates` with preserved items and relations, leaves `spec_registry` empty (no automatic approval), and advances workflow position to `specification.review`.
2. `test_issue_245_omitted_historical_item_fails_and_preserves_source`: Confirms that output omitting the historical item while retaining its relation fails at the ADK leaf callback with `INVALID_SPECIFICATION_PAYLOAD` ("Unknown relation endpoint: DATA.legacy-audit-record."), dispatches exactly once, creates no candidate in SQLite, and preserves registered-source bytes, byte length, and SHA-256 fingerprint in the database and on disk.

- **Verification Command (New Issue #245 Regressions)**:
  ```powershell
  uv run --no-sync --offline pytest tests/adapters/test_adk_specification_authoring_failures.py -k "test_issue_245" -v
  ```
- **Result**: `2 passed, 44 deselected, 6 warnings in 14.32s`.

- **Verification Command (Existing Real-Leaf Dangling-Endpoint Regression)**:
  ```powershell
  uv run --no-sync --offline pytest tests/adapters/test_adk_specification_authoring_failures.py::test_real_leaf_dangling_endpoint_is_invalid_payload -v
  ```
- **Result**: `1 passed, 5 warnings in 10.37s`.

- **Verification Command (Static Analysis & Type Gate)**:
  ```powershell
  uv run --no-sync --offline ruff check tests/adapters/test_adk_specification_authoring_failures.py
  uv run --no-sync --offline ty check tests/adapters/test_adk_specification_authoring_failures.py
  ```
- **Result**: Both passed (0 errors, 0 warnings).

---

## 4. Audit Qualifications & Review Scope

The audit conclusions are subject to the following explicit qualifications:
1. **Invalid Output vs. Candidate vs. Acceptance Gate**: Invalid output creates no candidate; the registered source or prior candidate/feedback remains current. Valid output creates a review candidate; it does not automatically approve. Acceptance remains strictly human-gated.
2. **Test-Backed Diagnostics vs. Live Recovery Evidence**: Diagnostics generation, payload sizing, redaction, and persistence are verified through automated integration tests (`test_adk_specification_authoring_failures.py`). The live P&ID recovery run (`pid-verification-terra`) contains no diagnostic events (as Attempt 6 predated diagnostic telemetry and Attempts 10/11 succeeded); live recovery evidence is strictly confined to candidate production, feedback reconciliation, and human acceptance.
3. **Prompt, Provenance, & Synthetic Testing Limits**: Prompt instructions and SHA-256 provenance hashes are bound and tested, but prompt/provenance tests do not guarantee semantic preservation or completeness of model output. Furthermore, synthetic unit and integration tests using stubbed models (including [`tests/fixtures/issue_245/`](file:///C:/Users/atavares/Projects/agileforge/tests/fixtures/issue_245/)) verify the leaf boundary, single dispatch, error classification, and candidate gating mechanics, but cannot establish the root cause of the non-deterministic model failure in historical Attempt 6 (whether caused by attention limits, context-length token pressure, layout nuance, or stochastic sampling).
4. **Historical Full Repository Suite**: The 270 focused passes are accumulated evidence from separate verification stages, not a fresh full-repository result. The historical full repository suite did not pass.
5. **Network Blocking Claim**: `uv --offline` controls package-network access; the repository's pytest-socket configuration blocks network access during Python tests. `node --test` executed a local unit test suite without asserting process-level network blocking.
6. **No Unilateral Closure**: This audit does not declare final closure. Independent PR review is required to evaluate the changes and determine final issue closure.

---

## 5. Separate Follow-ups (Non-Blocking for #245)

1. **Centralized Token Limits**: Architectural refactoring to centralize token budgeting and dynamic capacity estimation across agents remains deferred and is not required for #245 resolution.
2. **Remaining PBI Generation**: Generation of the remaining eleven PBIs (PBI-000002 through PBI-000012) in Planning is part of normal delivery execution, not specification structuring defect remediation.
3. **Phase 2 Automated Multi-turn Recovery**: An automated additive repair or multi-turn re-prompting subsystem remains an explicitly deferred architectural investigation pursuant to `docs/superpowers/plans/2026-09-02-issue-245-specification-validation-and-recovery.md` and `docs/testing/SPECIFICATION-OUTPUT-RECOVERY.md`. The current fail-closed, operator-reviewed recovery path is fully functional and supported.

---

## 6. Recommendation

**RECOMMEND INDEPENDENT PR REVIEW; DO NOT DECLARE FINAL CLOSURE.**
This audit recommends that an independent PR review evaluate the branch changes before concluding issue closure:
- Review the fail-closed ADK leaf boundary classification (`SpecificationOutputValidationError`) and single-dispatch guarantee (`max_attempts=1`).
- Verify the test-backed diagnostic persistence and safety properties alongside the live P&ID recovery evidence (Attempt 11 Candidate 2 approval).
- Review the qualified scope: invalid output creates no candidate; valid output creates a review candidate; acceptance remains human-gated; prompt/provenance tests do not guarantee semantic preservation.
- Evaluate repository-wide readiness separately from the accumulated 270 focused passes; the historical full repository test run did not pass.

Final closure should be decided through the independent PR review process rather than unilaterally declared by this audit.
