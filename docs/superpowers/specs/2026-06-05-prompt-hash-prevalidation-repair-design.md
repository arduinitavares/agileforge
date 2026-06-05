# Prompt Hash Prevalidation Repair Design

## Goal

Allow AgileForge authority normalization to repair invalid model-emitted `prompt_hash` values before strict schema validation, while preserving the final strict `SpecAuthorityCompilationSuccess` schema.

## Problem

`agileforge project create` for the ASA project reached authority compilation but failed with `SPEC_COMPILATION_FAILED: JSON_VALIDATION_FAILED`. The first validation error showed `prompt_hash` did not match `^[0-9a-f]{64}$`.

The normalizer already overwrites `success.prompt_hash` with `compute_prompt_hash(SPEC_AUTHORITY_COMPILER_INSTRUCTIONS)` after parsing. The bug is ordering: strict Pydantic validation rejects malformed `prompt_hash` before the repair code can run.

## Design

Patch `orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py`.

Before `SpecAuthorityCompilerOutput.model_validate(payload)`, add a success-shaped payload repair:

- If `prompt_hash` is missing, non-string, or not exactly 64 lowercase hex characters, replace it with `compute_prompt_hash(SPEC_AUTHORITY_COMPILER_INSTRUCTIONS)`.
- Recurse into envelope payloads shaped as `{"result": ...}`.
- Skip failure payloads that contain `error`.
- Keep final schema validation unchanged.

This mirrors the existing invalid invariant ID pre-validation repair and keeps the schema fail-closed for unrelated malformed data.

## Testing

Add regression coverage in `tests/test_spec_authority_compiler_normalizer.py`:

- A success-shaped compiler output with invalid `prompt_hash` normalizes successfully.
- The final `prompt_hash` equals `compute_prompt_hash(SPEC_AUTHORITY_COMPILER_INSTRUCTIONS)`.
- Envelope `{"result": success_payload}` also repairs.
- Existing normalizer tests continue to pass.
- Saved ASA failure artifact normalizes successfully or moves to the next semantic validation blocker, not `prompt_hash` schema validation.

## Non-Goals

- Do not weaken `SpecAuthorityCompilationSuccess` schema.
- Do not edit ASA spec.
- Do not rerun real `project create` in this fix.
- Do not change CLI, database, workflow, or authority acceptance behavior.
