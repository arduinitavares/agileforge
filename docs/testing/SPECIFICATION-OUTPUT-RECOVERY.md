# Specification Output Diagnostics & Recovery Guide

## 1. Architecture & Lifecycle Flow

AgileForge maintains a strict boundary between external specification preparation, internal semantic structuring, and human-governed delivery:

```
[External: grill-with-docs]
       │ (Human interview & reconciliation)
       ▼
[External: to-spec]
       │ (Generates Markdown source document)
       ▼
[AgileForge: Source Registration]
       │ (Stores exact UTF-8 bytes, Lineage & ADR bindings)
       ▼
[AgileForge: Internal Structurer (ADK Leaf)]
       │ (At most one model dispatch, strict output schema, validation callback)
       ▼
[AgileForge: Human-Accepted Specification (agileforge.spec.v2)]
       │ (Human review & formal acceptance)
       ▼
[AgileForge: Backlog Generation] ──► [Roadmap] ──► [Sprints & TDD]
```

### Architectural Boundaries
- **No Format Coupling:** External Markdown headings and formats are not synchronized into Roadmap or Backlog models.
- **No Mandatory Input Markdown Heading Requirements:** The structurer accepts arbitrary registered source Markdown adhering to to-spec conventions without requiring proprietary custom heading schemas.
- **No Unreviewed Injection:** Model outputs never bypass human review. No automatic candidate creation or acceptance occurs on failure.
- **Exact Source-Byte Preservation:** Production registration and input assembly preserve exact bytes, including LF and CRLF. They do not normalize line endings; different bytes produce different fingerprints. (Only the controlled issue-200 test fixtures receive newline normalization for cross-platform test determinism).

---

## 2. Error Taxonomy & Classifications

When specification structuring fails, the outcome is classified into one of four durable error codes emitted by the runner:

| Error Code | HTTP Status | Definition & Cause | UI Behavior |
| :--- | :--- | :--- | :--- |
| `INVALID_SPECIFICATION_PAYLOAD` | 409 Conflict | The model returned output that could not be validated as a valid v2 Specification payload. This covers: (1) semantic domain violations (e.g. unknown relation endpoint, missing items, unanchored relations); (2) malformed non-EOF JSON syntax; (3) missing wrapper or required fields, or top-level schema validation errors; and (4) empty model responses with explicit provider non-truncation finish reasons (`SAFETY` or `OTHER`). | **Definitive Failure:** Informs the user that structuring failed with the specific error message, confirms no new candidate was produced, and indicates whether the registered source or prior candidate/feedback remains current. |
| `UNSUPPORTED_SPECIFICATION_SCHEMA` | 409 Conflict | The model returned an explicit, non-null `payload.schema_version` that differs from `agileforge.spec.v2` (for example, `agileforge.spec.v1`). It does not mean a malformed wrapper. | **Definitive Failure:** Informs the user that an unsupported schema version was returned and no new candidate was produced. |
| `SPECIFICATION_OUTPUT_INCOMPLETE` | 409 Conflict | The model output was truncated before completion. Detection uses ADK `MAX_TOKENS`, or absent/`STOP` finish metadata with empty text or an EOF-truncated JSON stream (`_contains_incomplete_json`). This classification indicates truncated output; it does not claim every incomplete response proves token exhaustion. | **Definitive Failure:** Advises the operator to increase `SPECIFICATION_STRUCTURER_MAX_TOKENS` or select a provider that can return the complete structured payload. |
| `SPECIFICATION_PRODUCER_FAILED` | 409 Conflict | Model provider execution failure, API error, or unhandled exception during the model dispatch. | **Definitive Failure:** Reports that the structurer provider execution failed. |

### Note on Timeouts
A timeout during model execution is an **execution failure** (`SPECIFICATION_PRODUCER_FAILED`). It is **not** evidence that the provider was offline or that a network outage occurred. In Phase 1, timeouts remain classified as execution failures and are not conflated with network outages.

### Precedence of Revalidation and Authority Errors
The four codes above do not represent every possible structuring failure. Source revalidation (`STALE_SPECIFICATION_INPUT`), attempt revalidation, cancellation, and superseding authority failures take precedence before or during structuring and exit without leaf output diagnostics.

### Network and Uncertain Outcomes
Uncertain outcomes (such as HTTP 503, connection dropouts, or unknown error codes) retain uncertain-outcome behavior in the UI, instructing the operator to refresh the dashboard and verify the current candidate before retrying.

---

## 3. Safe Diagnostics Schema & Privacy Scope

When a typed output failure reaches the runner via `after_model_callback`, a sanitized diagnostic event is appended to the ADK session trace.

### Exact Diagnostic Fields
The diagnostic payload adheres to `schema_version = "agileforge.specification-output-diagnostic.v1"` and contains only these fields:

| Field | Type | Description |
| :--- | :--- | :--- |
| `schema_version` | `str` | Fixed schema identifier: `"agileforge.specification-output-diagnostic.v1"`. |
| `stage` | `str` | Structuring stage: `"primary"`. |
| `code` | `str` | Classified failure code (`INVALID_SPECIFICATION_PAYLOAD`, `UNSUPPORTED_SPECIFICATION_SCHEMA`, or `SPECIFICATION_OUTPUT_INCOMPLETE`). |
| `response_sha256` | `str \| None` | `sha256:<hex>` digest of raw response bytes, or `None` if response text is `None`. |
| `response_bytes` | `int \| None` | Byte length of raw response UTF-8 bytes, or `None` if response text is `None`. |
| `finish_reason` | `str \| None` | Provider finish reason (e.g. `"STOP"`, `"MAX_TOKENS"`, `"SAFETY"`, `"OTHER"`), or `None`. |
| `prompt_token_count` | `int \| None` | Non-negative prompt token count reported in usage metadata, or `None`. |
| `candidates_token_count` | `int \| None` | Non-negative completion token count reported in usage metadata, or `None`. |
| `item_count` | `int \| None` | Total item count parsed from `payload.items`, or `None` if items list is absent. |
| `relation_count` | `int \| None` | Total relation count parsed from `payload.relations`, or `None` if relations list is absent. |
| `missing_item_count` | `int \| None` | Count of unique relation endpoint IDs missing from `items`, or `None`. |
| `item_ids` | `list[str]` | Sorted, deduplicated list of item IDs extracted from `payload.items`, capped at 100 entries. |
| `missing_item_ids` | `list[str]` | Sorted, deduplicated list of missing relation endpoint IDs, capped at 100 entries. |
| `ids_truncated` | `bool` | `True` if either `item_ids` or `missing_item_ids` exceeded the 100-entry cap; otherwise `False`. |

### Deliberately Excluded Fields
Fields such as `error_code`, `message`, `validation_issues`, `missing_endpoints`, and `unknown_item_types` are **not** present. Raw Pydantic error records, including `ctx`, are deliberately excluded to prevent leaking unvalidated text fragments into diagnostics.

### Redaction Scope & Limitations
Redaction guarantees are limited to the added diagnostic event and the tested application log paths. Tests do not establish universal telemetry redaction across external framework layers (for example, installed ADK can trace responses before callbacks; this review does not establish an actual exported leak).

### Fail-Safe Persistence Trigger
Diagnostics are appended when a typed output failure reaches the runner via `after_model_callback`. Cancellation, superseding authority, and pre-provider revalidation failures remain exceptions to that path. If diagnostic persistence fails, the exception is caught and logged; it **never** masks or overrides the primary failure result.

---

## 4. Trace Correlation & Operator Lookup

Each structuring attempt is correlated across the business lifecycle database, ADK trace database, and UI.

### Step 1: Obtain Attempt Fingerprint
In the configured business database (`AGILEFORGE_DB_URL`, e.g., `sqlite:///agileforge.db`), query `workflow_node_attempts` using `project_id` and `workflow_node_attempt_id`:
```sql
SELECT attempt_fingerprint
FROM workflow_node_attempts
WHERE project_id = :project_id
  AND workflow_node_attempt_id = :workflow_node_attempt_id;
```

### Step 2: Query ADK Trace Database
The ADK trace `session_id` is the `attempt_fingerprint` obtained in Step 1 (not `workflow-attempt:<id>`).

In the configured ADK trace database (`AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL`, e.g., `sqlite:///adk_traces.db`), queries must be scoped by:
- `app_name = 'agileforge_graph_execution'`
- `user_id = 'workflow_adapter'`
- `session_id = :attempt_fingerprint`

Installed ADK stores event fields (`author`, `actions`, `content`) inside the `event_data` JSON column. Query the events table:
```sql
SELECT
    id,
    session_id,
    invocation_id,
    timestamp,
    json_extract(event_data, '$.author') AS author,
    json_extract(event_data, '$.actions.state_delta.specification_output_diagnostic') AS diagnostic
FROM events
WHERE app_name = 'agileforge_graph_execution'
  AND user_id = 'workflow_adapter'
  AND session_id = :attempt_fingerprint
ORDER BY timestamp ASC;
```

Alternatively, query the accumulated session state from the `sessions` table:
```sql
SELECT
    id,
    json_extract(state, '$.specification_output_diagnostic') AS diagnostic
FROM sessions
WHERE app_name = 'agileforge_graph_execution'
  AND user_id = 'workflow_adapter'
  AND id = :attempt_fingerprint;
```

Or inspect the session via the Python public session API:
```python
session = await session_service.get_session(
    app_name="agileforge_graph_execution",
    user_id="workflow_adapter",
    session_id=attempt_fingerprint,
)
diagnostic = session.state.get("specification_output_diagnostic")
```

### Correlation Invariant
The user prompt event (`author = 'user'`), the leaf execution event (`author = 'specification_structuring'`), and the diagnostic event (`author = 'specification_output_validator'`) share the **exact same non-empty `invocation_id`** within that session attempt.

---

## 5. Source-Byte Guarantees

- Production registration and input assembly preserve exact bytes, including LF and CRLF. They do not normalize line endings; different bytes produce different fingerprints.
- Exact registered UTF-8 bytes and SHA-256 content fingerprints are verified at registration and checked before model input assembly.
- Only the controlled issue-200 test fixtures perform test-fixture newline normalization (`.replace("\r\n", "\n")`) to ensure cross-platform test repeatability.

---

## 6. Execution Limits & Provenance

- **Producer Capability:** `specification-structurer`
- **Producer Version:** `1.0.2`
- **Prompt Version:** `agileforge.specification-structurer.prompt.v3`
- **Prompt Hash:** `sha256:ecc68026d01a9ade96707e345c47d2fe07acf3fcf37da82b7a739f9cfed6d00f`
- **Dispatch Limit:** **At most one structurer dispatch** occurs per structuring attempt. Pre-provider rejection (such as stale input drift or invalid guards) or replay can result in zero dispatches. No hidden automated retries or token-escalation loops exist in Phase 1.
- **Prompt Guidance Scope:** The prompt v3 instructions explicitly instruct the model to preserve typed item IDs, include all relation endpoints, and distinguish historical implementation facts from target requirements. However, **prompt instructions reduce ambiguity but do not guarantee that the model will produce valid or complete output.**
- **Validation Scope Limitations:** Schema and graph validation check structural validity (ID format, closed relations, required fields), but do **not** prove semantic completeness or preservation of every source requirement. Host checks and human semantic review remain necessary before candidate acceptance.

---

## 7. Recovery Scope & Limitations

### Phase 1 Verified Scope
- Deterministic reproduction of leaf failure classification.
- Fail-closed validation in `after_model_callback`.
- Safe correlated diagnostic persistence.
- Definitive terminal error reporting across UI, API, and CLI.
- Prompt provenance binding and operator documentation.

### Deferred Phase 2 Work
Automatic recovery (such as whole-candidate re-prompting with validation findings, additive missing-item patching, or automated multi-turn repair) is **explicitly deferred** pending architectural review and explicit authorization.

### P&ID Structuring Status
The P&ID project specification structuring was verified through authorized live recovery: Attempt 11 produced Candidate 2, containing 50 items and 24 relations, which received human acceptance as approved Specification Version 1. However, successful recovery does not establish the original omission's cause or guarantee future semantic completeness.
