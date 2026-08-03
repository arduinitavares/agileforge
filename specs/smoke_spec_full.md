# Smoke Harness Specification (Full)

## Overview
This spec defines the minimal but complete requirements used by the smoke harness to validate
Spec Authority compilation + acceptance + story pipeline (draft/refine) + deterministic gate.

## Project Context
- Project: Smoke Harness Demo
- Primary persona: Automation Engineer
- Primary theme: Core Data Quality

## Goals
- Validate spec registration, compilation, normalization, persistence, and acceptance.
- Validate story drafting and optional refinement.
- Validate deterministic acceptance gate + persisted evidence.

## Requirements

### R1: Required Field Invariant
- The payload must include `user_id`.

### R2: Forbidden Capability Invariant
- The system must not use OAuth1 authentication.

### R3: Data Contract
- Payload fields must be documented in acceptance criteria.
- `user_id` must be referenced verbatim in the story OR ACs.

### R4: Validation Evidence
- Evidence must record invariant checks and any failures.
- Evidence must include `spec_version_id`.

## Acceptance Criteria
- Draft/Refiner may mention `user_id` and must NOT be rejected by alignment for doing so.
- Any story/feature that includes “OAuth1” MUST be rejected by alignment as forbidden capability.
- Validation evidence persists:
	- alignment_failures for forbidden capability usage
	- required-field failures if `user_id` is missing where required
	- spec_version_id in evidence

## Non-Goals
- Persistence across runs.
- External API calls.

## Assumptions
- SQLite is available.
- Agents are reachable in the current environment.
