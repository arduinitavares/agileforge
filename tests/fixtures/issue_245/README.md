# Issue #245 Sanitized Fixture & Source-Contract Comparison

## 1. Purpose

This directory contains a sanitized, generic synthetic specification fixture designed to reproduce and verify the historical/normative relation handling of AgileForge Issue #245.

The fixture contains:
- A historical implementation fact with level `INFORMATIVE` (`DATA.legacy-audit-record`).
- A normative replacement requirement with level `MUST` (`REQ.audit-trail-authority`).
- A supported architectural relation referencing the historical item (`REQ.audit-trail-authority tracks DATA.legacy-audit-record`).

---

## 2. Source-Contract Comparison: `to-spec` vs. AgileForge

### `to-spec` Skill Template (`SKILL.md`)
The `to-spec` skill generates conversational Markdown documents intended for human reading and issue tracker publication. Its structure consists of seven top-level sections:
1. `## Problem Statement`
2. `## Solution`
3. `## User Stories` (numbered "As an <actor>, I want... so that...")
4. `## Implementation Decisions` (bulleted architectural choices, omitting file paths and code snippets except for prototype-derived models)
5. `## Testing Decisions` (identifying primary and secondary test seams and external behaviors)
6. `## Out of Scope`
7. `## Further Notes`

`to-spec` does not define or enforce a formal schema for:
- Discrete typed item identifiers (such as `REQ.*`, `DATA.*`, `RISK.*`).
- Controlled requirement levels (`MUST`, `SHOULD`, `MAY`, `INFORMATIVE`).
- Verification methods or structured acceptance criteria lists.
- A closed directed graph of typed relations (`tracks`, `satisfies`, `decomposes`, etc.).

### AgileForge Source and Structuring Contracts
AgileForge processes specifications through explicit, typed boundary contracts:
- **Registration Contract** (`services/contracts/specification_source.py`): Ingests the Markdown source file as an immutable byte stream, verifying exact UTF-8 bytes and SHA-256 fingerprints (`content_fingerprint`, `source_fingerprint`) bound to Git repository evidence.
- **Structuring Input Contract** (`services/contracts/specification_authoring.py`): Projects the registered source alongside accepted Vision, Product Goal, and ADRs into a closed `SpecificationStructuringInput` DTO.
- **Structuring Output Contract** (`utils/agileforge_spec_profile_v2.py` / `SpecificationStructuringOutput`): Requires the model to return a typed `agileforge.spec.v2` payload containing:
  - Strongly typed items whose IDs prefix-match their type (`REQ.*`, `DATA.*`).
  - Mandatory requirement level, verification method, and non-empty acceptance criteria for normative and data items.
  - A strictly closed relation graph: every relation endpoint (`from`, `to`) must resolve to an extant item in `items`. Missing endpoints violate schema invariants and raise `ValidationError("unknown relation endpoint: ...")`.

### Contract Bridging
In project workflows using both tools, teams embed typed item IDs and relationship statements (such as `REQ.xxx tracks DATA.yyy`) within the prose of `to-spec` sections (typically in `Implementation Decisions`). AgileForge's structurer prompt (`adapters/adk/prompts/specification_author.txt`) acts as an operational bridge by directing the model to discover and preserve explicitly declared IDs, classify historical implementation facts as `INFORMATIVE`, and ensure every relation endpoint is retained in the item set.

---

## 3. Uncertainty Record

Synthetic regression tests with stubbed LLM responses verify that:
1. Valid structured output preserving both items and their relation produces a review candidate without automatic acceptance.
2. Output omitting the historical item while retaining its relation fails at the ADK leaf boundary with `INVALID_SPECIFICATION_PAYLOAD`, executes exactly one dispatch, produces no candidate, and preserves registered-source bytes.

However, **synthetic tests cannot establish the root cause of the model's historical omission in live Attempt 6**. Whether the model omitted `DATA.review-state-current` due to prompt token pressure, attention dilution over long contexts, section placement nuance, or non-deterministic sampling cannot be determined through synthetic reproduction.
