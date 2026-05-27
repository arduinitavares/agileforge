# Evidence-Aware Backlog Reconciliation Design

**Date:** 2026-05-27
**Status:** Approved for review
**Scope:** Phase 1a evidence collection, transient reconciliation report, backlog-agent input

## Summary

AgileForge currently generates Product Backlog drafts from product vision,
technical spec content, compiled authority, prior backlog state, and user input.
That works for greenfield planning, but it failed on caRtola because the repo
already implemented part of the accepted product behavior. The backlog agent had
no implementation evidence, so it treated accepted requirements as new work
instead of distinguishing missing work from verification, hardening, or already
evidenced behavior.

Phase 1a adds an inspectable evidence collection step before backlog generation.
The collector produces a raw `ReconciliationReport` JSON document and stores it
in workflow state as `implementation_evidence_cached`. Backlog generation then
passes that JSON to the backlog agent as advisory context.

This phase deliberately does not add OpenSpec integration, new database tables,
task metadata changes, sprint planner changes, or semantic code analysis.

## Goals

- Give the backlog agent fresh implementation evidence before it drafts work.
- Keep evidence collection inspectable, re-runnable, and debuggable separately
  from backlog generation.
- Store the report in workflow state without introducing a persistent ledger.
- Use exact reference matching only, so Phase 1a remains deterministic and
  bounded.
- Prevent the collector from overstating proof of implementation.
- Validate the value hypothesis on caRtola before building deeper persistence or
  OpenSpec integration.

## Non-Goals

- No OpenSpec command integration.
- No `/opsx:apply`, `/opsx:archive`, or OpenSpec lifecycle coupling.
- No new database tables.
- No `TaskMetadata` v2.
- No sprint planner evidence model.
- No semantic codebase analyzer.
- No automatic Product Backlog mutation from findings.
- No test execution by the evidence collector.

## Architecture

Phase 1a introduces this flow:

```text
accepted authority/spec profile
        +
target repo scan or imported report
        ->
ReconciliationReport JSON
        ->
workflow_state["implementation_evidence_cached"]
        ->
backlog agent InputSchema.implementation_evidence
        ->
evidence-aware backlog draft
```

The report is a workflow-state cache, similar in role to cached authority
content. It is not the durable source of truth for reconciliation decisions.

## Command Contract

Add:

```sh
agileforge evidence collect \
  --project-id 2 \
  --repo-path /path/to/cartola \
  --idempotency-key evidence-cartola-001
```

and:

```sh
agileforge evidence collect \
  --project-id 2 \
  --from-file evidence_report.json \
  --idempotency-key evidence-cartola-manual-001
```

Rules:

- Exactly one of `--repo-path` or `--from-file` is required.
- `--repo-path` runs the exact-match collector against the target repository.
- `--from-file` validates and imports a supplied `ReconciliationReport`.
- `--from-file` fingerprints the file content, not only the file path.
- Imported reports must match the project's current accepted authority
  fingerprint. Authority-fingerprint mismatch is a guard error.
- The command writes canonical JSON to
  `workflow_state["implementation_evidence_cached"]`.
- The command requires a non-empty `--idempotency-key`.
- Reusing the same idempotency key with the same request fingerprint returns an
  idempotent replay response.
- Reusing the same idempotency key with a different request fingerprint returns
  a guard error.

The command uses the standard agent workbench envelope:

```json
{
  "ok": true,
  "data": {
    "project_id": 2,
    "report_fingerprint": "sha256:...",
    "stored_state_key": "implementation_evidence_cached",
    "report": {}
  },
  "warnings": [],
  "errors": []
}
```

Warnings are used for non-fatal diagnostics such as dirty repo state, skipped
files, unreadable files, and missing verification methods.

## Reconciliation Report Schema

Canonical shape:

```json
{
  "schema_version": "agileforge.reconciliation_report.v1",
  "project_id": 2,
  "spec_version_id": 7,
  "compiled_authority_fingerprint": "sha256:...",
  "repo": {
    "path": "/path/to/cartola",
    "git_commit": "abc123",
    "dirty": false
  },
  "generated_at": "2026-05-27T12:00:00Z",
  "collector": {
    "strategy": "exact_tag_match",
    "version": "agileforge.evidence_collect.v1"
  },
  "summary": {
    "finding_count": 4,
    "evidenced": 1,
    "evidence_missing": 1,
    "missing": 1,
    "unknown": 1
  },
  "findings": [
    {
      "spec_item_id": "REQ.budget-validation",
      "item_type": "REQ",
      "verification_method": "unit-test",
      "status": "evidence_missing",
      "confidence": "medium",
      "validation_state": "not_run",
      "evidence_paths": [
        {
          "path": "scripts/run_live_round.py",
          "kind": "source",
          "match_count": 1,
          "matched_terms": ["REQ.budget-validation"]
        }
      ],
      "notes": [
        "Exact source reference found. No test reference found. Tests were not executed."
      ]
    }
  ]
}
```

For imported reports, `repo` may be `null` when the report was produced
manually or by an external process without repository metadata. The import
preserves the imported `repo` and `collector` blocks as-is after schema
validation. A missing `repo` block produces a warning, not a failure, because
the imported file content fingerprint still pins the mutation input.

The report must be serialized canonically before being stored and fingerprinted.

### Finding Statuses

Phase 1a supports exactly four statuses:

- `evidenced`: exact behavior reference exists and either a test reference
  exists or the verification method does not require tests.
- `evidence_missing`: exact behavior reference exists but required test evidence
  is missing, or exact test evidence exists but no behavior/source reference was
  found.
- `missing`: no behavior reference and no test reference exist.
- `unknown`: inputs are ambiguous or unsupported.

`partial` is intentionally excluded from Phase 1a. Exact reference matching can
find references; it cannot assess degree of implementation.

### Confidence

Phase 1a supports:

- `medium`: deterministic exact-match finding.
- `low`: missing, unknown, or unsupported finding.

The collector must never emit `strong` confidence in Phase 1a. Strong evidence
requires executed validation with recorded output, which is out of scope.

### Validation State

Phase 1a supports:

- `not_run`

Future values are reserved for later phases:

- `passed`
- `failed`
- `skipped`

Every Phase 1a finding must use `validation_state: "not_run"`.

### Evidence Path Kinds

Each evidence path uses one of:

- `source`
- `test`
- `doc`
- `config`

Classification logic uses a single `evidence_paths` list. Test evidence is not
stored in a separate list.

## Exact-Match Collector

The collector reads accepted authority/spec-profile information and extracts
normative item IDs and verification methods for supported item types:

- `REQ`
- `QUALITY`
- `CONSTRAINT`
- `INTERFACE`
- `DATA`

For each item, the collector must resolve associated invariant IDs such as
`INV-*` from compiled authority relations when those relations are available.
Resolved invariant IDs are treated as equivalent matched terms for the parent
item. If invariant relationships cannot be resolved, the collector matches only
the item ID and emits a warning.

The collector searches text files under the supplied repository path and ignores
known noisy or irrelevant locations:

- `.git`
- virtual environments
- cache directories
- build output directories
- SQLite/database files such as `.db`, `.sqlite`, and `.sqlite3`
- lockfiles such as `uv.lock`, `package-lock.json`, and similar dependency
  lock outputs
- binary files
- large generated files

Phase 1a uses a default file-size scan limit of 500 KiB per file. Files above
that limit are skipped and reported in warnings.

The collector performs exact matching only. It does not infer behavior from
function names, comments without IDs, natural language similarity, or semantic
code structure.

File kind classification is path-based:

- `test`: directories named exactly `test` or `tests`, or filenames matching
  conventional test patterns such as `test_*.py`, `*_test.py`, `*.test.js`,
  `*.spec.js`, and equivalent extension variants.
- `doc`: Markdown or documentation paths.
- `config`: common config file paths.
- `source`: other textual source files.

The collector must not classify a file as `test` merely because an arbitrary
path segment contains the substring `test`.

## Classification Rule

For each finding:

```text
has_behavior_ref = any(kind in {"source", "doc", "config"} for evidence_paths)
has_test_ref = any(kind == "test" for evidence_paths)
needs_test = verification_method in {
  "unit-test",
  "integration-test",
  "system-test",
  "acceptance-test"
}

if not has_behavior_ref and not has_test_ref:
    status = "missing"
    confidence = "low"
elif has_behavior_ref and needs_test and not has_test_ref:
    status = "evidence_missing"
    confidence = "medium"
elif has_test_ref and not has_behavior_ref:
    status = "evidence_missing"
    confidence = "medium"
elif has_behavior_ref and (has_test_ref or not needs_test):
    status = "evidenced"
    confidence = "medium"
else:
    status = "unknown"
    confidence = "low"
```

All Phase 1a findings use `validation_state = "not_run"`.

## Workflow State

On successful collection or import, AgileForge stores the canonical report JSON:

```text
workflow_state["implementation_evidence_cached"]
```

The implementation may also store derived metadata if that matches existing
workflow-state conventions:

```text
workflow_state["implementation_evidence_fingerprint"]
workflow_state["implementation_evidence_collected_at"]
workflow_state["implementation_evidence_source"]
```

Only `implementation_evidence_cached` is required for Phase 1a backlog-agent
consumption.

The command also records idempotency metadata in a `WorkflowEvent`. Phase 1a
adds a new event type:

```text
EVIDENCE_COLLECTED
```

The event metadata contains:

```json
{
  "action": "evidence_collected",
  "idempotency_key": "...",
  "request_fingerprint": "sha256:...",
  "report_fingerprint": "sha256:..."
}
```

Idempotent replay is resolved by scanning `EVIDENCE_COLLECTED` events for the
matching key. The report schema is not polluted with idempotency metadata.

## Backlog Agent Input

Extend the backlog primer `InputSchema` with:

```python
implementation_evidence: str
```

`build_backlog_input_context` populates it from
`workflow_state["implementation_evidence_cached"]`.

If no report is present, it passes:

```text
NO_EVIDENCE
```

The backlog prompt must treat evidence as advisory reference evidence, not proof
of runtime correctness.

Prompt guidance:

- For `evidenced`, avoid creating new implementation backlog items unless
  product authority still clearly requires unresolved work.
- For `evidence_missing`, create verification, hardening, test, or documentation
  work rather than reimplementation work.
- For `missing`, create normal product backlog work when authority requires it,
  but preserve the low-confidence caveat for Product Owner review.
- For `unknown`, flag the item for Product Owner review instead of treating it as
  missing.

## Error Handling

The command fails closed when:

- no accepted authority or supported spec items can be resolved for the project.
- `--repo-path` does not exist or is unreadable.
- `--from-file` does not parse or validate as
  `agileforge.reconciliation_report.v1`.
- an imported report's `compiled_authority_fingerprint` does not match the
  project's current accepted authority fingerprint.
- the idempotency key is reused with a different request fingerprint.

The command warns and continues when:

- the target repo is dirty.
- some files are unreadable.
- some files are skipped because they are binary, generated, or too large.
- a spec item lacks a verification method.
- a verification method is unsupported by Phase 1a classification.
- an imported report has `repo: null`.

Warnings must be returned in the envelope `warnings` array.

## Idempotency

The request fingerprint is computed with `canonical_hash` from
`services.agent_workbench.fingerprints` over deterministic JSON data. The hash
input includes:

- command name.
- project id.
- source mode: `repo_path` or `from_file`.
- compiled authority fingerprint.
- repo path and git commit for `--repo-path`.
- SHA-256 content fingerprint of the imported file for `--from-file`.
- collector strategy and version.

Idempotent replay returns the previously stored response for the same key and
same request fingerprint. A key collision with a different request fingerprint
returns a guard error.

## caRtola Validation Plan

After implementation, validate Phase 1a against caRtola:

1. Run backlog generation without implementation evidence and save the output.
2. Run `agileforge evidence collect --project-id 2 --repo-path <cartola>`.
3. Inspect the `ReconciliationReport` for false positives and false negatives.
4. Run backlog generation again with `implementation_evidence_cached` present.
5. Compare backlog classification and item scope.

The success criterion is not perfect evidence. The success criterion is that the
evidence-aware backlog is more useful to Product Owner review than the
greenfield-blind backlog.

## Test Plan

Unit tests:

- report schema validates canonical JSON.
- exact source plus exact test reference classifies as `evidenced`, `medium`,
  `not_run`.
- exact source reference without required test reference classifies as
  `evidence_missing`, `medium`, `not_run`.
- exact test reference without behavior/source reference classifies as
  `evidence_missing`, `medium`, `not_run`.
- no exact references classifies as `missing`, `low`, `not_run`.
- unsupported or ambiguous inputs classify as `unknown`, `low`, `not_run`.
- associated invariant IDs are resolved as equivalent matched terms for their
  parent item when compiled authority relations are available.
- test file classification only matches exact test directories and conventional
  test filename patterns, not arbitrary `test` substrings.
- database files, dependency lockfiles, binary files, and files above the size
  limit are skipped with warnings.
- `--from-file` validation stores canonical JSON.
- `--from-file` request fingerprint changes when the file content changes.
- `--from-file` rejects authority-fingerprint mismatch.
- backlog input includes `implementation_evidence`.
- absent evidence becomes `NO_EVIDENCE`.

Integration tests:

- `evidence collect` writes `implementation_evidence_cached` to workflow state.
- `evidence collect` records an `EVIDENCE_COLLECTED` workflow event with
  idempotency key, request fingerprint, and report fingerprint.
- repeated idempotency key and same request fingerprint replay the prior result.
- reused idempotency key with changed input returns a guard error.
- dirty repo state succeeds and appends a warning to the response envelope.
- warnings are returned for skipped files and missing verification methods
  without failing the command.

## Later Phases

Phase 1b may promote reports into a persistent Reconciliation Ledger if caRtola
shows the evidence loop improves backlog quality.

Later phases may add:

- validation command execution and `strong` confidence.
- Sprint planner evidence context.
- task ticket evidence projection.
- optional OpenSpec proposal references.
- OpenSpec import/export as advisory documentation integration.
- explicit backlog readiness states for behavior-contract review.

None of those later features are part of Phase 1a.
