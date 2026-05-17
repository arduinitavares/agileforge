# Authority IR Compiler Phase 2D Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace coarse section-level authority review with a conservative, candidate-level traceability pipeline that makes omitted, weakly mapped, or uncertain spec requirements block authority acceptance by default.

**Architecture:** Extract the existing Markdown parsing and coverage code from `services/agent_workbench/authority_review.py` into one shared deterministic IR module, then make `authority review` and `authority accept` recompute coverage from that module. Source units are containers; atomic requirement candidates are the coverage target. Model output may carry suggested IR, but host code owns parsing, provenance, coverage status, blocking findings, and acceptance gates.

**Tech Stack:** Python 3.13, Pydantic v2, SQLModel/SQLite persistence, existing AgileForge CLI envelopes, existing `pyrepo-check --all` verification.

---

## Review-Driven Corrections

This plan incorporates external review feedback from GPT, Gemini, and Claude. The accepted points are:

- Do not create a second Markdown parser that can diverge from the current review-token parser. Phase 2D extracts the existing parser into `utils/spec_authority_ir.py` and updates `authority_review.py` to use it.
- Do not use `source_unit` as the final coverage target. A source unit can contain multiple requirements. Coverage must target atomic `RequirementCandidate` records.
- Do not let requirement-bearing heuristics define a hidden denominator. Every parsed source unit must resolve to one of: one or more candidates, an intentional non-requirement classification, or `uncertain`. `uncertain`, `partial`, `uncovered`, and `weak_mapping` block acceptance.
- Do not trust model-supplied `gaps`, `review_findings`, or coverage status. Host code recomputes blocking findings at review time and decision time from current source, candidates, mappings, and provenance.
- Do not backfill legacy IR in a way that appears authoritative. Legacy or host-parsed IR must carry explicit provenance and remains review-incomplete unless host recomputation proves all candidates are covered or intentionally classified.
- Do not bloat the canonical authority artifact with full source text. Store stable IDs, line ranges, short excerpts, quote hashes, provenance, and mappings. Full source text remains controlled by `authority review --include-spec`.
- Delay compiler prompt expansion until the host-side IR and review rendering are stable. The model can emit candidate hints later, but the host remains authoritative.
- Do not treat absence of normative keywords as proof that text is non-requirement. `non_requirement` requires positive evidence; ordinary product prose defaults to `uncertain`.
- Do not treat traceability to a gap, assumption, or unsupported note as semantic coverage. Candidate classification and authority target kind must be compatible before a mapping can count as covered.

Rejected or deferred points:

- Full ReqIF/OSLC adoption is deferred. Phase 2D borrows traceability discipline without adding a standard import/export surface.
- A separate IR database table is deferred. Phase 2D stores compact IR metadata in compiled authority JSON and recomputes review state from source when possible.
- Mandatory author-authored requirement IDs are deferred. They are useful, but AgileForge must still support normal Markdown specs.

## Scope

In scope:

- Extract current review parsing into one shared parser used by both compiler normalization and review rendering.
- Add stable source-unit IDs and atomic requirement-candidate IDs.
- Add conservative classification: requirement candidate, intentional non-requirement, or uncertain.
- Add candidate-level coverage and mapping validation.
- Add explicit IR provenance.
- Recompute blocking review findings from IR state instead of trusting stored fields.
- Preserve legacy compiled authority loading without pretending legacy artifacts are fully reviewed.
- Add tests for duplicate headings, nested lists, tables, code fences, Mermaid blocks, front matter, CRLF, Unicode, non-English normative examples, candidate splitting, weak mappings, and legacy provenance.

Out of scope:

- Domain-specific rules for caRtola or any other product.
- Project spec update/recompile command.
- Replacing the model provider.
- Full requirements-management standard support.
- Dedicated IR persistence tables.

## Core Invariants

These invariants are mandatory for this phase:

- `AUTHORITY_COVERAGE_INCOMPLETE` is a derived host-side finding, not a model-provided flag.
- `gaps=[]` cannot coexist with any blocking coverage state.
- Acceptance recomputes blocking findings from current source and stored authority data before writing a decision.
- Every source unit must be represented in coverage accounting as `candidate_extracted`, `intentionally_classified`, `non_requirement`, or `uncertain`.
- Every requirement candidate must have one or more exact source quotes with hashes and line ranges.
- Every authority mapping must reference a valid candidate ID and a valid authority item ID.
- `weak_mapping`, `uncertain`, `partial`, and `uncovered` block accept unless an explicit human override references specific candidate IDs with rationale.
- Legacy artifacts without IR load successfully but are never presented as model-emitted IR.
- `non_requirement` is a positive classification only. Lack of signal yields `uncertain`.
- A quote hash match is strong source evidence only. Coverage also requires a compatible authority target kind and acceptable mapping provenance.
- Source text unavailable at decision time fails closed with a non-accept-ready error; compact stored IR is not sufficient for acceptance.
- Review packets enforce hard size limits. Truncated candidate/finding lists create a blocking `AUTHORITY_REVIEW_PACKET_TRUNCATED` finding with `override_allowed=false`.
- Source text unavailable at decision time returns `AUTHORITY_SOURCE_UNAVAILABLE`; do not reuse stale-source or generic exception errors.
- Packet limit constants live in `utils/spec_authority_ir.py` and are imported wherever review packets are rendered.
- Generated non-invariant targets that mappings can reference, such as gaps and assumptions, must have stable IDs derived from candidate ID, finding code or target kind, and normalized text. Position-only IDs are invalid.

## File Map

- Create `utils/spec_authority_ir.py`
  - Shared parser, source-unit and candidate models, coverage recomputation, provenance types, and finding derivation.
  - This is extracted from the current `authority_review.py` parser instead of being a second implementation.
- Modify `services/agent_workbench/authority_review.py`
  - Remove local parser duplication.
  - Render source units, candidates, coverage, and derived findings from the shared IR module.
- Modify `services/agent_workbench/authority_decision.py`
  - Recompute IR coverage before accept and reject decisions.
  - Block accept on derived blocking findings unless candidate-specific override is supplied.
- Modify `services/agent_workbench/error_codes.py`
  - Register `AUTHORITY_SOURCE_UNAVAILABLE` with remediation and command metadata support.
- Modify `cli/main.py`
  - Parse candidate-specific override flags and reject legacy broad incomplete-review flags without candidate IDs.
- Modify `api.py`
  - Accept candidate-specific overrides in dashboard/API authority decision requests.
- Modify `frontend/project.js`
  - Send candidate-specific override payloads and reject broad dashboard overrides.
- Modify `frontend/project.html`
  - Render candidate-specific override controls or acknowledgement copy for incomplete review findings.
- Modify `models/specs.py`
  - Persist candidate-specific incomplete-review overrides on authority decisions.
- Modify `db/migrations.py`
  - Add idempotent migration/readiness support for the override payload column.
- Modify `utils/spec_schemas.py`
  - Add compact optional IR metadata to `SpecAuthorityCompilationSuccess`.
  - Keep historical compiled JSON valid.
- Modify `orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py`
  - Validate model-emitted source mappings against host-parsed candidates.
  - Store provenance-aware compact IR metadata.
  - Do not mark host-parsed legacy IR as model-emitted.
- Modify `orchestrator_agent/agent_tools/spec_authority_compiler_agent/instructions.txt`
  - Only after host IR tests pass, ask the model to emit candidate/mapping hints.
- Test `tests/test_spec_authority_ir.py`
  - Parser, candidate extraction, ID stability, coverage recomputation, and finding derivation.
- Modify `tests/test_agent_workbench_authority_review.py`
  - Review packet rendering and no parser-divergence tests.
- Modify `tests/test_agent_workbench_authority_decision.py`
  - Acceptance recomputation and candidate-specific override tests.
- Modify `tests/test_agent_workbench_authority_decision_cli.py`
  - CLI override parsing, legacy broad-override rejection, and interactive confirmation tests.
- Modify `tests/test_agent_workbench_error_codes.py`
  - Error registry coverage for `AUTHORITY_SOURCE_UNAVAILABLE`.
- Modify `tests/test_api_dashboard.py`
  - Dashboard/API override request tests.
- Modify `tests/test_db_migrations_authority_decision.py`
  - Migration/readiness tests for candidate-specific override persistence.
- Modify `tests/test_spec_authority_compiler_normalizer.py`
  - Provenance, legacy artifact, weak mapping, and model-emitted hint tests.

## IR Shape

Add these concepts in `utils/spec_authority_ir.py`. Exact Pydantic implementation may evolve, but field names and semantics must remain consistent across tests.

```python
class IrProvenance(StrEnum):
    MODEL_EMITTED = "model_emitted"
    HOST_PARSED = "host_parsed"
    MIXED = "mixed"
    LEGACY_ABSENT = "legacy_absent"


class SourceUnitDisposition(StrEnum):
    CANDIDATE_EXTRACTED = "candidate_extracted"
    INTENTIONALLY_CLASSIFIED = "intentionally_classified"
    NON_REQUIREMENT = "non_requirement"
    UNCERTAIN = "uncertain"


class CoverageStatus(StrEnum):
    COVERED = "covered"
    PARTIAL = "partial"
    INTENTIONALLY_CLASSIFIED = "intentionally_classified"
    UNCOVERED = "uncovered"
    UNCERTAIN = "uncertain"
    WEAK_MAPPING = "weak_mapping"


class MappingProvenance(StrEnum):
    MODEL_QUOTE = "model_quote"
    HOST_REPAIRED_QUOTE = "host_repaired_quote"
    HOST_INFERRED = "host_inferred"
    LEGACY_ABSENT = "legacy_absent"


class AuthorityTargetKind(StrEnum):
    INVARIANT = "invariant"
    ELIGIBLE_FEATURE_RULE = "eligible_feature_rule"
    REJECTED_FEATURE = "rejected_feature"
    GAP = "gap"
    ASSUMPTION = "assumption"


class OverrideScope(StrEnum):
    CANDIDATE = "candidate"
```

`SourceUnit`:

- `unit_id`: stable ID derived from heading path, normalized block text hash, and occurrence index.
- `section_id`: stable section ID derived from heading path and occurrence index.
- `heading_path`: full heading path.
- `kind`: `paragraph`, `list_item`, `table_row`, `fenced_block`, `blockquote`, `front_matter`, or `html_block`.
- `line_start`, `line_end`.
- `text_hash`: SHA-256 over normalized exact source text.
- `text_excerpt`: bounded excerpt for review display, not full source storage.
- `disposition`: source-unit disposition.
- `disposition_reason`.

`RequirementCandidate`:

- `candidate_id`: stable ID derived from source unit ID, quote hash, and candidate index.
- `source_unit_id`.
- `statement`: normalized candidate statement.
- `source_quote`: exact quoted span, bounded to a safe display limit.
- `quote_hash`: SHA-256 over exact UTF-8 bytes of the quote.
- `line_start`, `line_end`.
- `classification`: requirement, goal, non-goal, acceptance criterion, constraint, quality attribute, dependency, open question, assumption, or uncertain.
- `provenance`: host parsed, model emitted, mixed, or legacy absent.

`AuthorityMapping`:

- `candidate_id`.
- `authority_item_id`.
- `authority_target_kind`: invariant, eligible feature rule, rejected feature, gap, or assumption.
- `mapping_status`: covered, partial, intentionally classified, uncovered, uncertain, or weak mapping.
- `mapping_rationale`.
- `source_quote_hash`.
- `mapping_provenance`: model quote, host-repaired quote, host-inferred, or legacy absent.

`AuthorityReviewFinding`:

- `finding_id`.
- `severity`: blocking or warning.
- `code`.
- `message`.
- `candidate_ids`.
- `source_unit_ids`.
- `override_allowed`: boolean.

`IncompleteReviewOverride`:

- `candidate_id`.
- `finding_code`.
- `rationale`.
- `scope`: must be `candidate`.

Generated authority target IDs:

- `gap_id`: `GAP-` plus the first 16 hex chars of SHA-256 over canonical JSON `{candidate_id, finding_code, normalized_gap_text}`.
- `assumption_id`: `ASM-` plus the first 16 hex chars of SHA-256 over canonical JSON `{candidate_id, target_kind, normalized_assumption_text}`.
- `eligible_feature_rule_id` and `rejected_feature_id` keep their existing IDs when supplied by the compiler; host-generated fallback IDs use the same canonical JSON pattern with prefixes `EFR-` and `RF-`.
- These IDs must be stable across packet rendering and decision recomputation for unchanged source and authority content.

## Authority Target Compatibility Matrix

Coverage is valid only when the candidate classification and target kind are compatible.

| Candidate classification | Target kinds that may satisfy coverage | Notes |
| --- | --- | --- |
| `requirement` | `invariant` | Gaps, assumptions, and eligible rules do not satisfy accepted requirement coverage. |
| `acceptance_criterion` | `invariant` | Later phases may derive tests, but acceptance coverage still requires canonical authority. |
| `constraint` | `invariant`, `rejected_feature` | `rejected_feature` is valid only for forbidden/safety constraints. |
| `quality_attribute` | `invariant` | If no invariant type can represent it, it remains blocking until a new authority item type exists. |
| `dependency` | `invariant` | Assumptions may document uncertainty but do not satisfy coverage. |
| `goal` | `invariant`, `eligible_feature_rule` | `eligible_feature_rule` is weak by default. It counts only when a deterministic host classifier marks the candidate as `future_scope_constraint`; otherwise it is `weak_mapping`. |
| `non_goal` | `rejected_feature`, `invariant` | Use rejected feature for excluded capabilities or invariant for hard constraints. |
| `open_question` | none by default | Must be resolved or explicitly deferred with candidate-specific override. |
| `assumption` | `assumption` only as intentionally classified | Does not count as requirement coverage. |
| `uncertain` | none | Blocks acceptance until reclassified or overridden. |

Compatibility is checked by host code. An incompatible target produces `weak_mapping` or `unsupported_target_kind`, both blocking.

## Task 1: Extract the Existing Parser Into Shared IR

**Files:**
- Create: `utils/spec_authority_ir.py`
- Modify: `services/agent_workbench/authority_review.py`
- Create: `tests/test_spec_authority_ir.py`
- Modify: `tests/test_agent_workbench_authority_review.py`

- [ ] **Step 1: Write parser parity tests**

Add tests proving that the new shared parser preserves the current review parser behavior for:

- headings and heading-less root content
- nested headings
- list items
- table rows
- fenced code blocks
- tilde fences
- unclosed fences
- CRLF line endings

Command:

```bash
uv run --frozen pytest tests/test_spec_authority_ir.py::test_shared_parser_preserves_current_section_boundaries -q
```

Expected before implementation: fails because `utils.spec_authority_ir` does not exist.

- [ ] **Step 2: Extract parser code**

Move parser responsibilities from `authority_review.py` into `utils/spec_authority_ir.py`:

- `_parse_markdown_sections`
- `_parse_section_blocks`
- `_FenceMarker`
- `_fence_marker`
- `_content_block`
- normative and heading regex constants

Keep `authority_review.py` importing and using the shared parser. There must not be two independently maintained parsers after this task.

- [ ] **Step 3: Add source-unit IDs**

Add stable IDs to parsed blocks:

```text
unit_id = SRC-<section_slug_hash>-<block_text_hash>-<occurrence>
```

Use hash components rather than line numbers alone so unrelated line drift does not detach mappings. Keep line ranges for display and diagnostics.

- [ ] **Step 4: Verify parser parity**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority_ir.py tests/test_agent_workbench_authority_review.py -q
```

Expected: parser and review tests pass.

- [ ] **Step 5: Commit**

```bash
git add utils/spec_authority_ir.py services/agent_workbench/authority_review.py tests/test_spec_authority_ir.py tests/test_agent_workbench_authority_review.py
git commit -m "refactor: share authority review parser"
```

## Task 2: Atomic Requirement Candidates

**Files:**
- Modify: `utils/spec_authority_ir.py`
- Modify: `tests/test_spec_authority_ir.py`

- [ ] **Step 1: Write candidate extraction tests**

Add tests for:

- one paragraph with two `must` clauses produces two candidates
- one table row with requirement text produces one candidate per requirement-bearing cell or clause
- non-English normative words such as `deve`, `obrigatório`, `proibido`, `não deve` produce candidates
- non-normative product prose such as “Users can export audit logs for selected projects” becomes `uncertain`, not `non_requirement`
- ambiguous text without clear requirement signals becomes `uncertain`, not `non_requirement`
- background/example headings become `non_requirement` only when they have positive non-requirement evidence and lack normative/product capability signals
- positive non-requirement rules are exact and deterministic, including heading matches, marker matches, and blocker signals

Command:

```bash
uv run --frozen pytest tests/test_spec_authority_ir.py::test_atomic_candidates_split_multi_clause_unit tests/test_spec_authority_ir.py::test_uncertain_units_block_silent_false_negatives -q
```

Expected before implementation: fails because candidate extraction is missing or coarse.

- [ ] **Step 2: Implement conservative candidate extraction**

Implement `extract_requirement_candidates(source_units)` with these rules:

- Split candidate-bearing units by sentence boundaries, semicolons, table cells, and numbered acceptance criteria.
- If a source unit has normative or requirement-heading signals but cannot be confidently split, emit one `uncertain` candidate.
- If a source unit has no requirement signals and no requirement-heading context, emit one `uncertain` candidate unless a positive non-requirement rule applies.
- Positive non-requirement rules are limited to exact normalized heading leaf matches in `background`, `context`, `glossary`, `terminology`, `example`, `examples`, `rationale`, `notes`, `changelog`, `revision history`, or `implementation notes`, or exact block prefixes `Note:`, `Example:`, or `Rationale:`.
- Positive non-requirement rules are disallowed when the block or ancestor heading contains product-capability verbs, acceptance markers, constraint/security/compliance markers, or normative signals. Product-capability verbs include at least `can`, `allow`, `allows`, `support`, `supports`, `create`, `export`, `import`, `submit`, `approve`, `reject`, `configure`, `select`, `generate`, and `validate`.
- If a source unit is under open questions, emit `open_question` candidates. Open questions are review-blocking unless intentionally deferred.
- If a source unit is under background/example but contains normative words, emit `uncertain` or a real candidate. Do not silently ignore it.
- `non_requirement` disposition must include `disposition_reason` naming the positive rule. A fallback reason such as “no requirement signal found” is invalid.

- [ ] **Step 3: Verify candidate extraction**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority_ir.py -q
```

Expected: candidate tests pass.

- [ ] **Step 4: Commit**

```bash
git add utils/spec_authority_ir.py tests/test_spec_authority_ir.py
git commit -m "feat: extract atomic authority requirement candidates"
```

## Task 3: Candidate-Level Coverage and Findings

**Files:**
- Modify: `utils/spec_authority_ir.py`
- Modify: `tests/test_spec_authority_ir.py`

- [ ] **Step 1: Write coverage tests**

Add tests for:

- a candidate with exact quote and authority item mapping is `covered`
- a candidate with only a substring or broad section mapping is `weak_mapping`
- a candidate with exact quote but incompatible authority target kind is blocking
- a quality attribute mapped only to an assumption is blocking
- an open question mapped to a gap is intentionally classified but not covered
- a goal mapped to an eligible feature rule is `weak_mapping` unless the deterministic `future_scope_constraint` classifier passes
- an unmapped candidate is `uncovered`
- an `uncertain` candidate is blocking
- a source unit with one covered candidate and one uncovered candidate is not globally covered
- `AUTHORITY_COVERAGE_INCOMPLETE` is recomputed even if model output says `gaps=[]`
- generated gap and assumption target IDs are stable across two recomputations with unchanged source and authority content

Command:

```bash
uv run --frozen pytest tests/test_spec_authority_ir.py::test_candidate_level_coverage_blocks_partial_units tests/test_spec_authority_ir.py::test_findings_are_recomputed_from_coverage -q
```

Expected before implementation: fails because coverage remains section/block coarse.

- [ ] **Step 2: Implement candidate-level coverage**

Implement:

- `build_authority_mappings(candidates, authority_items, source_map_entries)`
- `derive_review_findings(source_units, candidates, mappings, provenance)`
- `coverage_summary_from_findings(findings, mappings)`

Required behavior:

- Host code computes all findings.
- `uncovered`, `partial`, `uncertain`, and `weak_mapping` create blocking findings.
- Exact quote hash matches are strong source evidence, not sufficient semantic coverage by themselves.
- Covered status requires source evidence, compatible authority target kind, and acceptable mapping provenance.
- Section names or broad source refs without quote match are weak evidence.
- Open questions are blocking unless explicitly classified as deferred with rationale.
- Gaps and assumptions may explain a candidate but do not satisfy accepted coverage for requirements, constraints, quality attributes, dependencies, or acceptance criteria.
- Mappings are many-to-many. A candidate may require multiple authority items; an authority item may satisfy multiple candidates. The mapping result for a candidate is blocking until all required compatible coverage is present or explicitly overridden.

- [ ] **Step 3: Verify coverage tests**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority_ir.py -q
```

Expected: coverage tests pass.

- [ ] **Step 4: Commit**

```bash
git add utils/spec_authority_ir.py tests/test_spec_authority_ir.py
git commit -m "feat: compute candidate-level authority coverage"
```

## Task 4: Schema Extension With Provenance

**Files:**
- Modify: `utils/spec_schemas.py`
- Modify: `tests/test_spec_authority_compiler_normalizer.py`

- [ ] **Step 1: Write schema tests**

Add tests proving:

- historical compiled authority JSON without IR still loads
- model-emitted IR with provenance loads
- mappings referencing missing candidate IDs fail validation
- candidates referencing missing source unit IDs fail validation
- compact IR fields do not require full source text

Command:

```bash
uv run --frozen pytest tests/test_spec_authority_compiler_normalizer.py::test_success_schema_accepts_compact_ir_with_provenance tests/test_spec_authority_compiler_normalizer.py::test_legacy_success_without_ir_stays_valid -q
```

Expected before implementation: fails because schema lacks IR fields.

- [ ] **Step 2: Add compact IR fields**

Extend `SpecAuthorityCompilationSuccess` with optional/defaulted fields:

- `ir_schema_version`
- `ir_provenance`
- `source_units`
- `requirement_candidates`
- `authority_mappings`
- `ir_packet_limits`: max candidates, max findings, max excerpt bytes, and truncation flag

Do not add trusted `review_findings` as canonical compiler truth. Review findings are derived by host code.

- [ ] **Step 3: Add model validators**

Validate:

- candidate source-unit IDs exist
- mapping candidate IDs exist
- mapping authority item IDs exist in invariants, eligible feature rules, rejected features, gaps, or assumptions
- mapping target kind is compatible with the referenced authority item collection
- mapping provenance is one of `model_quote`, `host_repaired_quote`, `host_inferred`, or `legacy_absent`
- `ir_provenance` is present when any IR field is non-empty
- compact IR excerpts are bounded; full source text is not persisted in compiled authority JSON

- [ ] **Step 4: Verify schema tests**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority_compiler_normalizer.py tests/test_spec_schema_modules.py -q
```

Expected: selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add utils/spec_schemas.py tests/test_spec_authority_compiler_normalizer.py
git commit -m "feat: add provenance-aware authority IR schema"
```

## Task 5: Normalizer Integration Without False Confidence

**Files:**
- Modify: `orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py`
- Modify: `tests/test_spec_authority_compiler_normalizer.py`

- [ ] **Step 1: Write normalizer tests**

Add tests proving:

- legacy output gets `ir_provenance=legacy_absent` or `host_parsed`, never `model_emitted`
- host-parsed legacy IR remains review-incomplete when any candidate is uncovered, uncertain, weakly mapped, or partially mapped
- model-emitted mappings are validated against host-parsed candidates
- host-repaired source quotes get `mapping_provenance=host_repaired_quote`, not `model_quote`
- host-repaired or host-inferred mappings cannot alone produce accept-ready coverage
- unrelated source refs become `weak_mapping` or blocking findings
- exact quote mismatch blocks acceptance readiness

Command:

```bash
uv run --frozen pytest tests/test_spec_authority_compiler_normalizer.py::test_legacy_ir_provenance_does_not_create_accept_ready_packet tests/test_spec_authority_compiler_normalizer.py::test_unrelated_source_refs_become_weak_mappings -q
```

Expected before implementation: fails.

- [ ] **Step 2: Integrate host IR**

Use `utils.spec_authority_ir` inside the normalizer to:

- parse source units
- extract candidates
- validate model-emitted candidate hints
- validate source-map quotes
- store compact IR with provenance

Do not use host-parsed IR to assert semantic completeness. It can detect likely omissions and weak mappings; it cannot prove the model reasoned over every requirement.

Mapping provenance rules:

- `model_quote`: model supplied an exact quote that matches current source and a compatible authority target.
- `host_repaired_quote`: host repaired or replaced a model source ref with an exact source quote.
- `host_inferred`: host inferred a mapping from text similarity, headings, or legacy persisted fields.
- `legacy_absent`: no mapping evidence exists in the legacy artifact.

Only `model_quote` can produce `covered` without additional review. `host_repaired_quote` and `host_inferred` are `weak_mapping` unless there is a persisted candidate-specific override from the decision flow.

- [ ] **Step 3: Verify normalizer tests**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority_compiler_normalizer.py tests/test_spec_authority_compile_tool.py -q
```

Expected: selected tests pass.

- [ ] **Step 4: Commit**

```bash
git add orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py tests/test_spec_authority_compiler_normalizer.py tests/test_spec_authority_compile_tool.py
git commit -m "feat: validate authority IR during normalization"
```

## Task 6: Review and Decision Recompute

**Files:**
- Modify: `models/specs.py`
- Modify: `db/migrations.py`
- Modify: `services/agent_workbench/authority_review.py`
- Modify: `services/agent_workbench/authority_decision.py`
- Modify: `services/agent_workbench/error_codes.py`
- Modify: `cli/main.py`
- Modify: `api.py`
- Modify: `frontend/project.js`
- Modify: `frontend/project.html`
- Modify: `tests/test_agent_workbench_authority_review.py`
- Modify: `tests/test_agent_workbench_authority_decision.py`
- Modify: `tests/test_agent_workbench_authority_decision_cli.py`
- Modify: `tests/test_agent_workbench_error_codes.py`
- Modify: `tests/test_api_dashboard.py`
- Modify: `tests/test_db_migrations_authority_decision.py`

- [ ] **Step 1: Write review/decision tests**

Add tests proving:

- review packet renders `source_units`, `requirement_candidates`, `authority_mappings`, `derived_review_findings`, and `ir_provenance`
- `gaps=[]` in compiled JSON is overridden by derived blocking findings
- accept recomputes findings after review token lookup
- accept fails when derived findings are blocking
- incomplete-review override must name affected candidate IDs and rationale
- broad boolean override is rejected
- old `--allow-incomplete-review --incomplete-review-rationale` alone is rejected by CLI, API, dashboard route, and service
- dashboard/API requests accept `incomplete_review_overrides` and reject broad override payloads
- override payload persists candidate ID, finding code, rationale, actor, and recorded timestamp
- stale source hash causes recomputation failure before decision
- source text unavailable at accept time fails closed
- packet truncation creates a blocking `AUTHORITY_REVIEW_PACKET_TRUNCATED` finding
- `AUTHORITY_SOURCE_UNAVAILABLE` is registered in the central error registry and appears in command metadata
- interactive CLI accept builds the same guarded request as non-interactive accept and cannot use broad incomplete-review override alone

Command:

```bash
uv run --frozen pytest tests/test_agent_workbench_authority_review.py::test_review_renders_derived_ir_findings tests/test_agent_workbench_authority_decision.py::test_accept_recomputes_candidate_coverage_before_decision -q
```

Expected before implementation: fails.

- [ ] **Step 2: Add override request and storage shape**

Add a request model field:

```python
class IncompleteReviewOverride(BaseModel):
    candidate_id: str
    finding_code: str
    rationale: str
```

Add `incomplete_review_overrides: list[IncompleteReviewOverride]` to `AuthorityAcceptRequest`.

Validate request overrides against the current recomputed blocking findings during the active accept attempt. Persist overrides as JSON on the authority decision row only after the terminal decision write succeeds. The stored shape must include:

- `candidate_id`
- `finding_code`
- `rationale`
- `actor`
- `recorded_at`
- `review_token`

The existing broad `allow_incomplete_review` boolean may remain temporarily for CLI compatibility, but it must be translated into candidate-specific overrides before the decision service accepts it. If no candidate IDs are supplied, return `INVALID_COMMAND`.

Add CLI/API/dashboard request fields for explicit overrides:

- `--incomplete-review-override <candidate_id>:<finding_code>:<rationale>`
- repeated values are allowed
- `--allow-incomplete-review` without at least one override is rejected for non-interactive CLI calls
- API/dashboard JSON field: `incomplete_review_overrides: [{candidate_id, finding_code, rationale}]`

- [ ] **Step 3: Render derived IR**

Update `authority_review.py` so packet data includes:

- `spec.source_units`
- `pending_authority.requirement_candidates`
- `pending_authority.authority_mappings`
- `review_findings`
- `ir_provenance`
- `coverage_summary`

Render omissions first: blocking findings, weak mappings, uncovered candidates, uncertain candidates, then covered candidates.

- Apply packet limits:
  - define `MAX_REVIEW_SOURCE_UNITS = 500` in `utils/spec_authority_ir.py`
  - define `MAX_REVIEW_CANDIDATES = 1_000` in `utils/spec_authority_ir.py`
  - define `MAX_REVIEW_FINDINGS = 200` in `utils/spec_authority_ir.py`
  - define `MAX_REVIEW_EXCERPT_BYTES = 1_000` in `utils/spec_authority_ir.py`
  - any truncation creates blocking, non-overrideable `AUTHORITY_REVIEW_PACKET_TRUNCATED`

- [ ] **Step 4: Recompute during decisions**

Update `authority_decision.py` so accept calls the same host-side derivation used by review. The review token remains a freshness guard, not proof that stored findings are correct.

Required failure behavior:

- blocking finding without override returns `AUTHORITY_REVIEW_INCOMPLETE`
- override without candidate IDs returns `INVALID_COMMAND`
- override for candidate IDs not currently blocking returns `INVALID_COMMAND`
- override for `AUTHORITY_REVIEW_PACKET_TRUNCATED` returns `INVALID_COMMAND`
- override rationale is persisted in the authority decision row
- source text unavailable returns `AUTHORITY_SOURCE_UNAVAILABLE` before any decision write

- [ ] **Step 5: Verify review and decision tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_authority_review.py tests/test_agent_workbench_authority_decision.py tests/test_agent_workbench_authority_decision_cli.py tests/test_agent_workbench_error_codes.py tests/test_api_dashboard.py -q
```

Expected: selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add models/specs.py db/migrations.py services/agent_workbench/authority_review.py services/agent_workbench/authority_decision.py services/agent_workbench/error_codes.py cli/main.py api.py frontend/project.js frontend/project.html tests/test_agent_workbench_authority_review.py tests/test_agent_workbench_authority_decision.py tests/test_agent_workbench_authority_decision_cli.py tests/test_agent_workbench_error_codes.py tests/test_api_dashboard.py tests/test_db_migrations_authority_decision.py
git commit -m "feat: gate authority decisions on derived IR coverage"
```

## Task 7: Compiler Prompt Hints

**Files:**
- Modify: `orchestrator_agent/agent_tools/spec_authority_compiler_agent/instructions.txt`
- Modify: `tests/test_spec_authority_compiler_agent.py`

- [ ] **Step 1: Write prompt contract test**

Add a test requiring instructions to say:

- host code owns coverage truth
- model may emit candidate and mapping hints
- model must include exact source quotes for mappings
- model must not claim coverage is complete

Command:

```bash
uv run --frozen pytest tests/test_spec_authority_compiler_agent.py::test_compiler_instructions_define_ir_hints_without_trusting_model_coverage -q
```

Expected before implementation: fails.

- [ ] **Step 2: Update instructions**

Add instruction text:

```text
Authority IR hints:
- You may emit requirement_candidates and authority_mappings as hints.
- Every mapping must include exact source quotes, source unit references when known, and a rationale.
- Do not emit final review_findings or assert coverage completeness. Host code derives coverage and blocking findings.
- If a requirement is unclear, mark it uncertain rather than ignoring it.
- Do not leave a known omission only in prose; include it as a gap or uncertain candidate hint.
```

- [ ] **Step 3: Verify prompt tests**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority_compiler_agent.py -q
```

Expected: selected tests pass.

- [ ] **Step 4: Commit**

```bash
git add orchestrator_agent/agent_tools/spec_authority_compiler_agent/instructions.txt tests/test_spec_authority_compiler_agent.py
git commit -m "docs: define authority IR compiler hints"
```

## Task 8: Documentation and Full Verification

**Files:**
- Modify: `docs/agent-cli-manual.md`
- Modify: `docs/superpowers/plans/2026-05-17-authority-ir-compiler-phase-2d.md` if implementation discoveries require a plan correction.

- [ ] **Step 1: Update CLI manual**

Document:

- `ir_provenance`
- `review_findings`
- candidate IDs
- weak mappings
- candidate-specific incomplete-review override
- dashboard/API override payloads
- packet limits and truncation behavior
- `AUTHORITY_REVIEW_PACKET_TRUNCATED` as blocking and non-overrideable
- `AUTHORITY_SOURCE_UNAVAILABLE`
- source-unavailable fail-closed behavior
- agent rule: never recommend acceptance when blocking findings exist
- legacy broad incomplete-review flags are invalid without candidate-specific overrides
- interactive CLI accept uses the same candidate-specific override model as non-interactive CLI/API/dashboard flows

- [ ] **Step 2: Run focused tests**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority_ir.py tests/test_spec_authority_compiler_normalizer.py tests/test_agent_workbench_authority_review.py tests/test_agent_workbench_authority_decision.py tests/test_agent_workbench_authority_decision_cli.py tests/test_agent_workbench_error_codes.py tests/test_api_dashboard.py -q
```

Expected: selected tests pass.

- [ ] **Step 3: Run full verification**

Run:

```bash
uv run --frozen pyrepo-check --all
```

Expected:

```text
ruff: All checks passed
annotations: All checks passed
ty: All checks passed
bandit: No issues identified
pytest: all selected tests passed
```

- [ ] **Step 4: Commit**

```bash
git add docs/agent-cli-manual.md docs/superpowers/plans/2026-05-17-authority-ir-compiler-phase-2d.md
git commit -m "docs: document authority IR review contract"
```

## Manual Smoke Test

After all tasks pass, run from a caller repo with a real spec:

```bash
cd /Users/aaat/projects/caRtola

agileforge project create \
  --name "Authority IR Smoke $(date +%s)" \
  --spec-file specs/app.md \
  --idempotency-key "authority-ir-smoke-$(date +%Y%m%d%H%M%S)" \
  --changed-by codex

agileforge authority review --project-id <new_project_id> > /tmp/agileforge-authority-ir-review.json

python3 - <<'PY'
import json
p=json.load(open("/tmp/agileforge-authority-ir-review.json"))
d=p["data"]
print("ok", p["ok"])
print("provenance", d["pending_authority"].get("ir_provenance"))
print("findings", d.get("review_findings", []))
print("candidate_count", len(d["pending_authority"].get("requirement_candidates", [])))
print("mapping_count", len(d["pending_authority"].get("authority_mappings", [])))
PY
```

Expected:

- stdout is one JSON envelope.
- `ir_provenance` is visible.
- blocking findings appear before covered items when coverage is incomplete.
- accept is blocked unless findings are absent or a candidate-specific human override is provided.
- no caRtola-specific code or output logic exists.

## Self-Review Checklist

- The plan no longer creates a second parser.
- Coverage target is atomic candidate, not section or source unit.
- Legacy/host-parsed IR cannot create false acceptance confidence.
- Review findings are derived and recomputed, not trusted from model JSON.
- Context bloat is limited by compact source metadata and bounded excerpts.
- The plan has tests for parser edge cases, weak mappings, provenance, acceptance recomputation, and override scope.
