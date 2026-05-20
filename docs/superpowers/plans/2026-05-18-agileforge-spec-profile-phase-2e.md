# AgileForge Spec Profile Phase 2E Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace deterministic Markdown requirement extraction as the authority gate with a structured AgileForge spec profile: canonical JSON, deterministic Markdown rendering, structural validation, and authority compilation from typed spec items.

**Architecture:** LLMs author semantic spec structure in `agileforge.spec.v1` JSON. AgileForge validates schema, IDs, relations, hashes, rendered Markdown freshness, and source citations. Authority compilation consumes typed items when available and keeps legacy Markdown support as an explicit compatibility path without host-generated requirement-candidate blockers.

**Tech Stack:** Python 3.13, Pydantic v2, SQLModel, pytest, ruff, ty, AgileForge CLI.

---

## Supersedes

This plan supersedes `docs/superpowers/plans/2026-05-18-remove-deterministic-authority-extraction-phase-2e.md`.

The old plan correctly removes deterministic candidate extraction, but it does
not install the replacement path. This plan does both:

1. Remove the current host candidate-manifest / candidate-review gate.
2. Add structured spec profile models, rendering, validation, storage
   normalization, compiler input metadata, and CLI support.

## Implementation Boundaries

In scope:

- Delete or bypass host-generated `requirement_candidates` as an acceptance
  denominator.
- Add `agileforge.spec.v1` Pydantic models and JSON Schema export.
- Add canonical JSON serialization and `sha256:` hashes.
- Add deterministic Markdown rendering and render hash checks.
- Let `project create --spec-file <path>` accept either legacy Markdown or
  structured spec JSON.
- Add CLI commands to print the schema and validate/render a structured spec.
- Teach the authority compiler prompt to consume structured JSON when present.
- Update review packets to expose structured spec metadata instead of
  candidate extraction blockers.

Out of scope:

- A first-party LLM spec generator agent.
- ReqIF, OSLC, StrictDoc, or Doorstop import/export.
- Full migration of existing legacy specs.
- Expanding the stored authority invariant schema beyond the current v1 support
  matrix.

## File Map

- Create `utils/agileforge_spec_profile.py`
  - Pydantic models for `TechnicalSpecArtifact`.
  - Canonical JSON and hash helpers.
  - Structural validation helpers.
  - Deterministic Markdown renderer.
  - JSON Schema export helper.

- Create `tests/test_agileforge_spec_profile.py`
  - Unit tests for model validation, relation validation, hashing, rendering,
    and non-canonical Markdown behavior.

- Modify `utils/spec_schemas.py`
  - Remove candidate-manifest compiler input.
  - Add `spec_source_format` to `SpecAuthorityCompilerInput`.
  - Keep compiled authority output backward compatible while removing compact IR
    acceptance dependency.

- Modify `services/specs/compiler_service.py`
  - Remove `_candidate_manifest_for_compiler`.
  - Normalize structured specs before storage and compilation.
  - Pass `spec_source_format` to the compiler.

- Modify `services/specs/pending_authority_service.py`
  - Canonicalize structured `spec.json` files before creating `SpecRegistry`
    rows.
  - Keep legacy Markdown as `agileforge.spec_legacy_markdown.v1`.

- Modify `orchestrator_agent/agent_tools/spec_authority_compiler_agent/instructions.txt`
  - Remove `candidate_manifest` instructions.
  - Add structured spec input instructions and v1 support matrix.

- Modify `orchestrator_agent/agent_tools/spec_authority_compiler_agent/normalizer.py`
  - Stop deriving compact host IR for acceptability.
  - Keep source-map quote validation, ID normalization, duplicate prevention,
    and gap insertion.

- Modify `services/agent_workbench/authority_review.py`
  - Remove candidate review findings from acceptability.
  - Add structured spec snapshot metadata when the pending spec is
    `agileforge.spec.v1`.

- Modify `services/agent_workbench/authority_decision.py`
  - Remove candidate-specific override requirement from acceptance.
  - Keep stale review, token, source hash, transaction, and fatal finding guards.

- Modify `cli/main.py`
  - Add `agileforge spec profile schema`.
  - Add `agileforge spec profile validate --spec-file <path> [--render-md <path>]`.

- Modify `services/agent_workbench/command_registry.py`
  - Publish the new command schemas.

- Modify tests:
  - `tests/test_spec_authority_compiler_agent.py`
  - `tests/test_spec_authority_compiler_normalizer.py`
  - `tests/test_specs_compiler_service.py`
  - `tests/test_agent_workbench_authority_review.py`
  - `tests/test_agent_workbench_authority_decision.py`
  - `tests/test_agent_workbench_command_schema.py`
  - `tests/test_agent_workbench_cli.py`

- Candidate for deletion after imports are gone:
  - `utils/spec_authority_ir.py`
  - `tests/test_spec_authority_ir.py`

## Task 0: Branch Hygiene

**Files:**
- Delete if present: `agileforge-phase2e-review-6.json`
- Delete if present: `agileforge-phase2e-review-6.pretty.json`
- Delete if present: `tmp/phase2e-review-inspection`

- [ ] **Step 1: Inspect the current worktree**

Run:

```bash
git status --short --branch
```

Expected: branch is `dev/authority-coverage-matrix-phase-2e`. Existing modified
files may include the previous candidate-manifest WIP. Do not reset the branch;
later tasks replace those changes intentionally.

- [ ] **Step 2: Remove generated smoke artifacts**

Run:

```bash
rm -f agileforge-phase2e-review-6.json agileforge-phase2e-review-6.pretty.json
rm -rf tmp/phase2e-review-inspection
```

Expected: only generated inspection artifacts are removed.

- [ ] **Step 3: Verify only source/doc changes remain**

Run:

```bash
git status --short
```

Expected: no `agileforge-phase2e-review-6*.json` files and no
`tmp/phase2e-review-inspection` entry.

## Task 1: Add AgileForge Spec Profile Models

**Files:**
- Create: `utils/agileforge_spec_profile.py`
- Create: `tests/test_agileforge_spec_profile.py`

- [ ] **Step 1: Write failing model tests**

Create `tests/test_agileforge_spec_profile.py` with these initial tests:

```python
"""Tests for AgileForge structured spec profile v1."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from utils.agileforge_spec_profile import (
    AgileForgeSpecStatus,
    AgileForgeSpecType,
    TechnicalSpecArtifact,
    canonical_spec_hash,
    canonical_spec_json,
    export_agileforge_spec_schema,
)


def _artifact_payload() -> dict[str, object]:
    return {
        "schema_version": "agileforge.spec.v1",
        "artifact_id": "SPEC.cartola",
        "title": "Cartola Champion Squad Selector",
        "status": "draft",
        "version": "0.1",
        "created_at": "2026-05-18",
        "updated_at": "2026-05-18",
        "summary": "Recommend a valid champion squad.",
        "problem_statement": "Operators need repeatable squad recommendations.",
        "items": [
            {
                "id": "GOAL.cartola.weekly-decision",
                "type": "GOAL",
                "status": "proposed",
                "title": "Weekly decision support",
                "statement": "Help the operator choose a weekly squad.",
            },
            {
                "id": "REQ.cartola.budget",
                "type": "REQ",
                "status": "proposed",
                "level": "MUST",
                "title": "Budget constraint",
                "statement": "The selected squad MUST satisfy budget_used <= budget.",
                "verification": "system-test",
                "acceptance": [
                    "Given a configured budget, when a squad is recommended, then budget_used is less than or equal to budget."
                ],
            },
        ],
        "relations": [
            {
                "from": "REQ.cartola.budget",
                "type": "satisfies",
                "to": "GOAL.cartola.weekly-decision",
                "rationale": "Budget validity supports weekly squad selection.",
            }
        ],
        "controlled_terms": [],
        "external_references": [],
        "rendering": {
            "markdown_profile": "agileforge.spec_markdown.v1",
            "rendered_markdown_sha256": None,
        },
    }


def test_valid_profile_artifact_parses() -> None:
    artifact = TechnicalSpecArtifact.model_validate(_artifact_payload())

    assert artifact.schema_version == "agileforge.spec.v1"
    assert artifact.status is AgileForgeSpecStatus.DRAFT
    assert artifact.items[1].type is AgileForgeSpecType.REQ


def test_relation_endpoint_must_exist() -> None:
    payload = _artifact_payload()
    payload["relations"] = [
        {
            "from": "REQ.cartola.budget",
            "type": "satisfies",
            "to": "GOAL.missing",
            "rationale": "Broken relation.",
        }
    ]

    with pytest.raises(ValidationError, match="unknown relation endpoint"):
        TechnicalSpecArtifact.model_validate(payload)


def test_normative_item_requires_verification_and_acceptance() -> None:
    payload = _artifact_payload()
    req = payload["items"][1]
    assert isinstance(req, dict)
    req.pop("verification")

    with pytest.raises(ValidationError, match="verification"):
        TechnicalSpecArtifact.model_validate(payload)


def test_canonical_spec_json_is_stable() -> None:
    artifact = TechnicalSpecArtifact.model_validate(_artifact_payload())
    first = canonical_spec_json(artifact)
    second = canonical_spec_json(TechnicalSpecArtifact.model_validate(json.loads(first)))

    assert first == second
    assert canonical_spec_hash(artifact).startswith("sha256:")


def test_schema_export_contains_closed_item_schema() -> None:
    schema = export_agileforge_spec_schema()

    assert schema["$id"] == "https://agileforge.local/schemas/agileforge.spec.v1.json"
    assert schema["additionalProperties"] is False
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run --frozen pytest tests/test_agileforge_spec_profile.py -q
```

Expected: fail with `ModuleNotFoundError: No module named 'utils.agileforge_spec_profile'`.

- [ ] **Step 3: Implement profile models**

Create `utils/agileforge_spec_profile.py`:

```python
"""AgileForge structured specification profile v1."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION: str = "agileforge.spec.v1"
MARKDOWN_PROFILE: str = "agileforge.spec_markdown.v1"
JSON_SCHEMA_ID: str = "https://agileforge.local/schemas/agileforge.spec.v1.json"
_ITEM_ID_RE: re.Pattern[str] = re.compile(
    r"^(GOAL|NON_GOAL|REQ|QUALITY|CONSTRAINT|INTERFACE|DATA|DECISION|"
    r"ASSUMPTION|RISK|EXAMPLE|OPEN_QUESTION)\.[a-z0-9][a-z0-9.-]{1,96}$"
)


class AgileForgeSpecType(StrEnum):
    """Supported typed item categories in AgileForge spec profile v1."""

    GOAL = "GOAL"
    NON_GOAL = "NON_GOAL"
    REQ = "REQ"
    QUALITY = "QUALITY"
    CONSTRAINT = "CONSTRAINT"
    INTERFACE = "INTERFACE"
    DATA = "DATA"
    DECISION = "DECISION"
    ASSUMPTION = "ASSUMPTION"
    RISK = "RISK"
    EXAMPLE = "EXAMPLE"
    OPEN_QUESTION = "OPEN_QUESTION"


class AgileForgeSpecStatus(StrEnum):
    """Lifecycle states for spec artifacts and items."""

    DRAFT = "draft"
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    CHANGED = "changed"
    DEFERRED = "deferred"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class RequirementLevel(StrEnum):
    """Controlled requirement levels."""

    MUST = "MUST"
    MUST_NOT = "MUST_NOT"
    SHOULD = "SHOULD"
    MAY = "MAY"
    INFORMATIVE = "INFORMATIVE"


class VerificationMethod(StrEnum):
    """Supported verification methods."""

    INSPECTION = "inspection"
    ANALYSIS = "analysis"
    UNIT_TEST = "unit-test"
    INTEGRATION_TEST = "integration-test"
    SYSTEM_TEST = "system-test"
    ACCEPTANCE_TEST = "acceptance-test"
    MANUAL_REVIEW = "manual-review"
    MONITORING = "monitoring"
    NOT_YET_DEFINED = "not-yet-defined"


class RelationType(StrEnum):
    """Supported relation edge types."""

    SATISFIES = "satisfies"
    DECOMPOSES = "decomposes"
    CONSTRAINS = "constrains"
    DEPENDS_ON = "depends_on"
    IMPLEMENTS = "implements"
    VERIFIES = "verifies"
    TRACKS = "tracks"
    SUPERSEDES = "supersedes"
    CONFLICTS_WITH = "conflicts_with"
    CLARIFIES = "clarifies"


class SpecRendering(BaseModel):
    """Rendering metadata for a deterministic Markdown view."""

    model_config = ConfigDict(extra="forbid")

    markdown_profile: Literal["agileforge.spec_markdown.v1"] = MARKDOWN_PROFILE
    rendered_markdown_sha256: str | None = None


class ControlledTerm(BaseModel):
    """A project-local term definition."""

    model_config = ConfigDict(extra="forbid")

    term: Annotated[str, Field(min_length=1)]
    definition: Annotated[str, Field(min_length=1)]
    scope: Literal["artifact", "domain", "project"] = "artifact"


class ExternalReference(BaseModel):
    """External reference metadata. Linked content is not authority by itself."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(pattern=r"^EXT\.[a-z0-9][a-z0-9.-]{1,96}$")]
    title: Annotated[str, Field(min_length=1)]
    url: str | None = None
    summary: Annotated[str, Field(min_length=1)]


class SourceNote(BaseModel):
    """Source context captured by the spec generator."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["user_note", "interview", "import", "external_summary"]
    text: Annotated[str, Field(min_length=1)]
    external_ref_id: str | None = None


class SpecItem(BaseModel):
    """One typed AgileForge spec item."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(pattern=_ITEM_ID_RE.pattern)]
    type: AgileForgeSpecType
    status: AgileForgeSpecStatus
    title: Annotated[str, Field(min_length=1)]
    statement: Annotated[str, Field(min_length=1)]
    level: RequirementLevel | None = None
    rationale: str | None = None
    verification: VerificationMethod | None = None
    acceptance: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    tags: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    source_notes: list[SourceNote] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_item_contract(self) -> Self:
        """Enforce ID/type consistency and normative evidence."""
        prefix = self.id.split(".", maxsplit=1)[0]
        if prefix != self.type.value:
            raise ValueError("item id prefix must match item type")
        normative_types = {
            AgileForgeSpecType.REQ,
            AgileForgeSpecType.QUALITY,
            AgileForgeSpecType.CONSTRAINT,
            AgileForgeSpecType.INTERFACE,
            AgileForgeSpecType.DATA,
        }
        if self.type in normative_types:
            missing: list[str] = []
            if self.level is None:
                missing.append("level")
            if self.verification is None:
                missing.append("verification")
            if not self.acceptance:
                missing.append("acceptance")
            if missing:
                raise ValueError(
                    f"normative item {self.id} missing required fields: "
                    + ", ".join(missing)
                )
        return self


class SpecRelation(BaseModel):
    """A first-class relation edge between spec items."""

    model_config = ConfigDict(extra="forbid")

    from_: Annotated[str, Field(alias="from", pattern=_ITEM_ID_RE.pattern)]
    type: RelationType
    to: Annotated[str, Field(pattern=_ITEM_ID_RE.pattern)]
    rationale: str | None = None


class TechnicalSpecArtifact(BaseModel):
    """Canonical AgileForge spec profile v1 artifact."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal["agileforge.spec.v1"] = SCHEMA_VERSION
    artifact_id: Annotated[str, Field(pattern=r"^SPEC\.[a-z0-9][a-z0-9.-]{1,96}$")]
    title: Annotated[str, Field(min_length=1)]
    status: AgileForgeSpecStatus
    version: Annotated[str, Field(min_length=1)]
    created_at: Annotated[str, Field(min_length=1)]
    updated_at: Annotated[str, Field(min_length=1)]
    summary: Annotated[str, Field(min_length=1)]
    problem_statement: Annotated[str, Field(min_length=1)]
    items: list[SpecItem]
    relations: list[SpecRelation] = Field(default_factory=list)
    controlled_terms: list[ControlledTerm] = Field(default_factory=list)
    external_references: list[ExternalReference] = Field(default_factory=list)
    rendering: SpecRendering = Field(default_factory=SpecRendering)

    @model_validator(mode="after")
    def validate_relations(self) -> Self:
        """Ensure relations only reference existing items."""
        item_ids = {item.id for item in self.items}
        duplicate_ids = sorted(
            item_id for item_id in item_ids if [item.id for item in self.items].count(item_id) > 1
        )
        if duplicate_ids:
            raise ValueError(f"duplicate item ids: {', '.join(duplicate_ids)}")
        for relation in self.relations:
            for endpoint in (relation.from_, relation.to):
                if endpoint not in item_ids:
                    raise ValueError(f"unknown relation endpoint: {endpoint}")
        external_ids = {reference.id for reference in self.external_references}
        for item in self.items:
            for note in item.source_notes:
                if note.external_ref_id and note.external_ref_id not in external_ids:
                    raise ValueError(
                        f"unknown external reference endpoint: {note.external_ref_id}"
                    )
        return self


def canonical_spec_json(artifact: TechnicalSpecArtifact) -> str:
    """Return deterministic JSON for storage and hashing."""
    return json.dumps(
        artifact.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def canonical_spec_hash(artifact: TechnicalSpecArtifact) -> str:
    """Return sha256 hash over canonical spec JSON bytes."""
    digest = hashlib.sha256(canonical_spec_json(artifact).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def export_agileforge_spec_schema() -> dict[str, Any]:
    """Return JSON Schema for the AgileForge spec profile."""
    schema = TechnicalSpecArtifact.model_json_schema()
    schema["$id"] = JSON_SCHEMA_ID
    schema["additionalProperties"] = False
    return schema
```

- [ ] **Step 4: Run model tests**

Run:

```bash
uv run --frozen pytest tests/test_agileforge_spec_profile.py -q
```

Expected: pass.

## Task 2: Add Deterministic Markdown Rendering

**Files:**
- Modify: `utils/agileforge_spec_profile.py`
- Modify: `tests/test_agileforge_spec_profile.py`

- [ ] **Step 1: Add failing renderer tests**

Append to `tests/test_agileforge_spec_profile.py`:

```python
from utils.agileforge_spec_profile import (
    MARKDOWN_PROFILE,
    rendered_markdown_hash,
    render_markdown,
)


def test_render_markdown_is_deterministic() -> None:
    artifact = TechnicalSpecArtifact.model_validate(_artifact_payload())

    markdown = render_markdown(artifact)

    assert "# Cartola Champion Squad Selector" in markdown
    assert "Schema: agileforge.spec.v1" in markdown
    assert "Markdown profile: agileforge.spec_markdown.v1" in markdown
    assert "### REQ.cartola.budget - Budget constraint" in markdown
    assert "<!-- agileforge-review-notes:start -->" in markdown
    assert rendered_markdown_hash(markdown).startswith("sha256:")


def test_rendered_markdown_hash_is_stable() -> None:
    artifact = TechnicalSpecArtifact.model_validate(_artifact_payload())

    first = render_markdown(artifact)
    second = render_markdown(artifact)

    assert first == second
    assert rendered_markdown_hash(first) == rendered_markdown_hash(second)
    assert artifact.rendering.markdown_profile == MARKDOWN_PROFILE
```

- [ ] **Step 2: Run renderer tests and verify failure**

Run:

```bash
uv run --frozen pytest tests/test_agileforge_spec_profile.py::test_render_markdown_is_deterministic tests/test_agileforge_spec_profile.py::test_rendered_markdown_hash_is_stable -q
```

Expected: fail with import errors for `render_markdown` and
`rendered_markdown_hash`.

- [ ] **Step 3: Implement renderer helpers**

Append to `utils/agileforge_spec_profile.py`:

```python
def rendered_markdown_hash(markdown: str) -> str:
    """Return sha256 hash over rendered Markdown bytes."""
    digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _line(label: str, value: object | None) -> str:
    if value is None:
        return f"{label}:"
    if isinstance(value, StrEnum):
        return f"{label}: {value.value}"
    return f"{label}: {value}"


def render_markdown(artifact: TechnicalSpecArtifact) -> str:
    """Render deterministic Markdown from a structured spec artifact."""
    lines: list[str] = [
        f"# {artifact.title}",
        "",
        f"Schema: {artifact.schema_version}",
        f"Artifact ID: {artifact.artifact_id}",
        f"Status: {artifact.status.value}",
        f"Version: {artifact.version}",
        f"Created: {artifact.created_at}",
        f"Updated: {artifact.updated_at}",
        f"Markdown profile: {artifact.rendering.markdown_profile}",
        "",
        "## Summary",
        "",
        artifact.summary,
        "",
        "## Problem Statement",
        "",
        artifact.problem_statement,
        "",
    ]
    if artifact.controlled_terms:
        lines.extend(["## Controlled Terms", ""])
        for term in sorted(artifact.controlled_terms, key=lambda item: item.term):
            lines.extend(
                [
                    f"### {term.term}",
                    "",
                    f"Scope: {term.scope}",
                    "",
                    term.definition,
                    "",
                ]
            )
    lines.extend(["## Spec Items", ""])
    for item in sorted(artifact.items, key=lambda spec_item: spec_item.id):
        lines.extend(
            [
                f"### {item.id} - {item.title}",
                "",
                _line("Type", item.type),
                _line("Status", item.status),
                _line("Level", item.level),
                _line("Verification", item.verification),
                "Tags: " + ", ".join(sorted(item.tags)),
                "",
                "Statement:",
                item.statement,
                "",
            ]
        )
        if item.rationale:
            lines.extend(["Rationale:", item.rationale, ""])
        if item.acceptance:
            lines.extend(["Acceptance:"])
            lines.extend(f"- {entry}" for entry in item.acceptance)
            lines.append("")
    if artifact.relations:
        lines.extend(["## Relations", ""])
        for relation in sorted(
            artifact.relations,
            key=lambda edge: (edge.from_, edge.type.value, edge.to),
        ):
            rationale = relation.rationale or ""
            lines.append(
                f"- {relation.from_} {relation.type.value} {relation.to}"
                + (f" - {rationale}" if rationale else "")
            )
        lines.append("")
    lines.extend(
        [
            "<!-- agileforge-review-notes:start -->",
            "<!-- agileforge-review-notes:end -->",
            "",
        ]
    )
    return "\n".join(lines)
```

- [ ] **Step 4: Run profile tests**

Run:

```bash
uv run --frozen pytest tests/test_agileforge_spec_profile.py -q
```

Expected: pass.

## Task 3: Remove Candidate Manifest From Compiler Input

**Files:**
- Modify: `utils/spec_schemas.py`
- Modify: `services/specs/compiler_service.py`
- Modify: `orchestrator_agent/agent_tools/spec_authority_compiler_agent/instructions.txt`
- Test: `tests/test_specs_compiler_service.py`
- Test: `tests/test_spec_authority_compiler_agent.py`

- [ ] **Step 1: Replace candidate-manifest compiler tests**

In `tests/test_specs_compiler_service.py`, replace
`test_candidate_manifest_for_compiler_uses_host_ir_candidates` and
`test_default_compiler_invocation_sends_candidate_manifest` with:

```python
def test_default_compiler_invocation_does_not_send_host_candidate_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compiler input should contain source text, not host semantic candidates."""
    captured: list[SpecAuthorityCompilerInput] = []

    async def fake_invoke(payload: SpecAuthorityCompilerInput) -> str:
        captured.append(payload)
        return _success_payload_json()

    monkeypatch.setattr(
        "services.specs.compiler_service._invoke_spec_authority_compiler_async",
        fake_invoke,
    )

    compiler_service._default_invoke_spec_authority_compiler(
        spec_content="# Spec\n\nThe system must record audit evidence.",
        content_ref=None,
        product_id=4,
        spec_version_id=9,
    )

    assert len(captured) == 1
    assert not hasattr(captured[0], "candidate_manifest")
    assert captured[0].spec_source_format == "agileforge.spec_legacy_markdown.v1"
```

In `tests/test_spec_authority_compiler_agent.py`, replace instruction assertions
that require `candidate_manifest` with:

```python
def test_compiler_instructions_do_not_require_candidate_manifest() -> None:
    """Compiler prompt must not depend on host candidate extraction."""
    instructions = _compiler_instructions()

    assert "candidate_manifest" not in instructions
    assert "agileforge.spec.v1" in instructions
    assert "Do not infer authority from Markdown narrative" in instructions
```

- [ ] **Step 2: Run replaced tests and verify failure**

Run:

```bash
uv run --frozen pytest tests/test_specs_compiler_service.py::test_default_compiler_invocation_does_not_send_host_candidate_manifest tests/test_spec_authority_compiler_agent.py::test_compiler_instructions_do_not_require_candidate_manifest -q
```

Expected: fail because `candidate_manifest` still exists in schema, service,
and prompt.

- [ ] **Step 3: Update compiler input schema**

In `utils/spec_schemas.py`:

- Remove `SpecAuthorityCompilerCandidateManifestEntry`.
- Remove `candidate_manifest_schema_version`.
- Remove `candidate_manifest`.
- Remove `candidate_manifest_truncated`.
- Add this field to `SpecAuthorityCompilerInput`:

```python
    spec_source_format: Annotated[
        str,
        Field(
            description=(
                "Input format: agileforge.spec.v1 or "
                "agileforge.spec_legacy_markdown.v1."
            )
        ),
    ] = "agileforge.spec_legacy_markdown.v1"
```

- [ ] **Step 4: Remove compiler service manifest creation**

In `services/specs/compiler_service.py`, remove imports from
`utils.spec_authority_ir` and remove
`SpecAuthorityCompilerCandidateManifestEntry` from the `utils.spec_schemas`
import list.

Replace `_default_invoke_spec_authority_compiler` with:

```python
def _default_invoke_spec_authority_compiler(
    spec_content: str,
    content_ref: str | None,
    product_id: int | None,
    spec_version_id: int | None,
) -> str:
    """Invoke the compiler agent from sync code and return raw JSON text."""
    del content_ref
    spec_source_format = _detect_spec_source_format(spec_content)
    input_payload = SpecAuthorityCompilerInput(
        spec_source=spec_content,
        spec_content_ref=None,
        domain_hint=None,
        product_id=product_id,
        spec_version_id=spec_version_id,
        spec_source_format=spec_source_format,
    )
    return _run_async_task(_invoke_spec_authority_compiler_async(input_payload))
```

Add near `_default_invoke_spec_authority_compiler`:

```python
def _detect_spec_source_format(spec_content: str) -> str:
    """Return the source format marker for compiler input."""
    try:
        parsed = json.loads(spec_content)
    except json.JSONDecodeError:
        return "agileforge.spec_legacy_markdown.v1"
    if isinstance(parsed, dict) and parsed.get("schema_version") == "agileforge.spec.v1":
        return "agileforge.spec.v1"
    return "agileforge.spec_legacy_markdown.v1"
```

Delete `_candidate_manifest_for_compiler`.

- [ ] **Step 5: Update compiler prompt contract**

In `orchestrator_agent/agent_tools/spec_authority_compiler_agent/instructions.txt`:

- Remove every `candidate_manifest` input key.
- Remove every `requirement_candidates` or `authority_mappings` requirement.
- Add this source-format rule:

```text
Input includes:
- spec_source_format: "agileforge.spec.v1" or "agileforge.spec_legacy_markdown.v1"

When spec_source_format is "agileforge.spec.v1":
- spec_source is canonical TechnicalSpecArtifact JSON.
- Consume typed items, relations, status, level, acceptance, and source_notes.
- Do not infer authority from Markdown narrative.
- Cite typed item IDs in source_map.location when possible.
- Apply the AgileForge authority support profile:
  - REQ/DATA/INTERFACE become REQUIRED_FIELD or RELATION_CONSTRAINT only when explicit.
  - QUALITY/CONSTRAINT become MAX_VALUE or RELATION_CONSTRAINT only when measurable.
  - NON_GOAL/MUST_NOT become FORBIDDEN_CAPABILITY only when an explicit forbidden capability is present.
  - Unsupported normative items become gaps with their item IDs preserved.

When spec_source_format is "agileforge.spec_legacy_markdown.v1":
- spec_source is human-authored Markdown.
- Compile best-effort authority from the spec.
- Return gaps and assumptions for vague, contradictory, or unsupported items.
```

- [ ] **Step 6: Run compiler-input tests**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority_compiler_agent.py tests/test_specs_compiler_service.py -q
```

Expected: pass after obsolete candidate-manifest tests are removed or replaced.

## Task 4: Normalize Structured Specs Before Registry Storage

**Files:**
- Create: `services/specs/profile_content.py`
- Modify: `services/specs/compiler_service.py`
- Modify: `services/specs/pending_authority_service.py`
- Test: `tests/test_specs_compiler_service.py`
- Test: `tests/test_agent_workbench_project_create_cli_integration.py`

- [ ] **Step 1: Write failing content normalization tests**

Append to `tests/test_specs_compiler_service.py`:

```python
from services.specs.profile_content import normalize_spec_content_for_registry


def test_normalize_structured_spec_content_canonicalizes_json() -> None:
    raw_json = json.dumps(_agileforge_spec_profile_payload(), indent=2)

    normalized = normalize_spec_content_for_registry(raw_json)

    assert normalized.format == "agileforge.spec.v1"
    assert normalized.spec_hash.startswith("sha256:")
    assert "\n" not in normalized.content
    assert json.loads(normalized.content)["schema_version"] == "agileforge.spec.v1"


def test_normalize_legacy_markdown_content_keeps_existing_hash_shape() -> None:
    raw_markdown = "# Spec\n\nThe system must record audit evidence.\n"

    normalized = normalize_spec_content_for_registry(raw_markdown)

    assert normalized.format == "agileforge.spec_legacy_markdown.v1"
    assert normalized.spec_hash == hashlib.sha256(raw_markdown.encode("utf-8")).hexdigest()
    assert normalized.content == raw_markdown
```

Add this helper in the same test module if it does not already exist:

```python
def _agileforge_spec_profile_payload() -> dict[str, object]:
    return {
        "schema_version": "agileforge.spec.v1",
        "artifact_id": "SPEC.test",
        "title": "Test Spec",
        "status": "draft",
        "version": "0.1",
        "created_at": "2026-05-18",
        "updated_at": "2026-05-18",
        "summary": "Test summary.",
        "problem_statement": "Test problem.",
        "items": [
            {
                "id": "REQ.test.audit",
                "type": "REQ",
                "status": "proposed",
                "level": "MUST",
                "title": "Audit evidence",
                "statement": "The system MUST record audit evidence.",
                "verification": "system-test",
                "acceptance": ["Audit evidence is stored for each operation."],
            }
        ],
        "relations": [],
        "controlled_terms": [],
        "external_references": [],
        "rendering": {
            "markdown_profile": "agileforge.spec_markdown.v1",
            "rendered_markdown_sha256": None,
        },
    }
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run --frozen pytest tests/test_specs_compiler_service.py::test_normalize_structured_spec_content_canonicalizes_json tests/test_specs_compiler_service.py::test_normalize_legacy_markdown_content_keeps_existing_hash_shape -q
```

Expected: fail because `services.specs.profile_content` does not exist.

- [ ] **Step 3: Implement profile content normalization**

Create `services/specs/profile_content.py`:

```python
"""Helpers for normalizing spec content before registry storage."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from pydantic import ValidationError

from utils.agileforge_spec_profile import (
    TechnicalSpecArtifact,
    canonical_spec_hash,
    canonical_spec_json,
)

STRUCTURED_SPEC_FORMAT: str = "agileforge.spec.v1"
LEGACY_MARKDOWN_FORMAT: str = "agileforge.spec_legacy_markdown.v1"


@dataclass(frozen=True)
class NormalizedSpecContent:
    """Normalized spec content ready for SpecRegistry."""

    content: str
    spec_hash: str
    format: str


def normalize_spec_content_for_registry(raw_content: str) -> NormalizedSpecContent:
    """Canonicalize structured specs and preserve legacy Markdown behavior."""
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError:
        return _legacy(raw_content)
    if not isinstance(parsed, dict) or parsed.get("schema_version") != STRUCTURED_SPEC_FORMAT:
        return _legacy(raw_content)
    try:
        artifact = TechnicalSpecArtifact.model_validate(parsed)
    except ValidationError:
        raise
    return NormalizedSpecContent(
        content=canonical_spec_json(artifact),
        spec_hash=canonical_spec_hash(artifact),
        format=STRUCTURED_SPEC_FORMAT,
    )


def _legacy(raw_content: str) -> NormalizedSpecContent:
    return NormalizedSpecContent(
        content=raw_content,
        spec_hash=hashlib.sha256(raw_content.encode("utf-8")).hexdigest(),
        format=LEGACY_MARKDOWN_FORMAT,
    )
```

- [ ] **Step 4: Wire normalization into compiler service update path**

In `services/specs/compiler_service.py`, import:

```python
from services.specs.profile_content import normalize_spec_content_for_registry
```

In `update_spec_and_compile_authority`, replace:

```python
    spec_hash = hashlib.sha256(spec_content.encode("utf-8")).hexdigest()
```

With:

```python
    normalized_spec = normalize_spec_content_for_registry(spec_content)
    spec_content = normalized_spec.content
    spec_hash = normalized_spec.spec_hash
```

- [ ] **Step 5: Wire normalization into pending authority project-create path**

In `services/specs/pending_authority_service.py`, import:

```python
from services.specs.profile_content import normalize_spec_content_for_registry
```

After `_load_spec_file(spec_path)` returns `resolved_path, spec_content, spec_hash`,
replace the loaded hash with normalized content and hash:

```python
    resolved_path, spec_content, spec_hash = loaded
    normalized_spec = normalize_spec_content_for_registry(spec_content)
    spec_content = normalized_spec.content
    spec_hash = normalized_spec.spec_hash
```

- [ ] **Step 6: Run normalization tests**

Run:

```bash
uv run --frozen pytest tests/test_specs_compiler_service.py tests/test_agent_workbench_project_create_cli_integration.py -q
```

Expected: pass.

## Task 5: Add Spec Profile CLI Commands

**Files:**
- Modify: `cli/main.py`
- Modify: `services/agent_workbench/command_registry.py`
- Test: `tests/test_agent_workbench_cli.py`
- Test: `tests/test_agent_workbench_command_schema.py`

- [ ] **Step 1: Write failing CLI tests**

Append to `tests/test_agent_workbench_cli.py`:

```python
def test_spec_profile_schema_command_outputs_json(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli_main(["spec", "profile", "schema"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["data"]["schema"]["$id"].endswith("agileforge.spec.v1.json")


def test_spec_profile_validate_can_render_markdown(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec_path = tmp_path / "spec.json"
    render_path = tmp_path / "spec.md"
    spec_path.write_text(json.dumps(_agileforge_spec_profile_payload()), encoding="utf-8")

    exit_code = cli_main(
        [
            "spec",
            "profile",
            "validate",
            "--spec-file",
            str(spec_path),
            "--render-md",
            str(render_path),
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["data"]["format"] == "agileforge.spec.v1"
    assert render_path.read_text(encoding="utf-8").startswith("# Test Spec")
```

Add the same `_agileforge_spec_profile_payload` helper used in Task 4 if this
test module does not have access to it.

- [ ] **Step 2: Run CLI tests and verify failure**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_cli.py::test_spec_profile_schema_command_outputs_json tests/test_agent_workbench_cli.py::test_spec_profile_validate_can_render_markdown -q
```

Expected: fail because `spec profile` commands do not exist.

- [ ] **Step 3: Add CLI parser**

In `cli/main.py`, add a `spec` subparser with nested `profile` commands:

```python
    spec_parser = subparsers.add_parser("spec")
    spec_subparsers = spec_parser.add_subparsers(dest="spec_command")

    profile_parser = spec_subparsers.add_parser("profile")
    profile_subparsers = profile_parser.add_subparsers(dest="profile_command")

    profile_subparsers.add_parser("schema")

    profile_validate = profile_subparsers.add_parser("validate")
    profile_validate.add_argument("--spec-file", required=True)
    profile_validate.add_argument("--render-md", required=False)
```

In the CLI routing function, route these commands:

```python
    if args.command == "spec" and args.spec_command == "profile":
        if args.profile_command == "schema":
            return _cmd_spec_profile_schema()
        if args.profile_command == "validate":
            return _cmd_spec_profile_validate(args)
```

Add helpers:

```python
def _cmd_spec_profile_schema() -> tuple[str, dict[str, Any]]:
    from utils.agileforge_spec_profile import export_agileforge_spec_schema

    return (
        "agileforge spec profile schema",
        {"ok": True, "data": {"schema": export_agileforge_spec_schema()}},
    )


def _cmd_spec_profile_validate(args: Any) -> tuple[str, dict[str, Any]]:
    from pathlib import Path

    from pydantic import ValidationError

    from utils.agileforge_spec_profile import (
        TechnicalSpecArtifact,
        canonical_spec_hash,
        render_markdown,
        rendered_markdown_hash,
    )

    spec_path = Path(str(args.spec_file)).expanduser().resolve()
    try:
        artifact = TechnicalSpecArtifact.model_validate_json(
            spec_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, ValidationError) as exc:
        return (
            "agileforge spec profile validate",
            {
                "ok": False,
                "errors": [
                    {
                        "code": "SPEC_PROFILE_INVALID",
                        "message": str(exc),
                    }
                ],
            },
        )
    markdown = render_markdown(artifact)
    render_path = getattr(args, "render_md", None)
    if render_path:
        Path(str(render_path)).expanduser().resolve().write_text(
            markdown,
            encoding="utf-8",
        )
    return (
        "agileforge spec profile validate",
        {
            "ok": True,
            "data": {
                "format": "agileforge.spec.v1",
                "spec_sha256": canonical_spec_hash(artifact),
                "rendered_markdown_sha256": rendered_markdown_hash(markdown),
            },
        },
    )
```

- [ ] **Step 4: Register command schemas**

In `services/agent_workbench/command_registry.py`, add capability entries for:

- `agileforge spec profile schema`
- `agileforge spec profile validate`

The validate command schema must include:

```json
{
  "spec_file": "string",
  "render_md": "string?"
}
```

- [ ] **Step 5: Run CLI and schema tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_cli.py tests/test_agent_workbench_command_schema.py -q
```

Expected: pass.

## Task 6: Remove Candidate Findings From Review Acceptability

**Files:**
- Modify: `services/agent_workbench/authority_review.py`
- Modify: `services/agent_workbench/authority_decision.py`
- Modify: `tests/test_agent_workbench_authority_review.py`
- Modify: `tests/test_agent_workbench_authority_decision.py`
- Modify: `tests/test_agent_workbench_authority_decision_cli.py`
- Modify: `tests/test_api_dashboard.py`

- [ ] **Step 1: Write failing review test for metadata header**

Add to `tests/test_agent_workbench_authority_review.py`:

```python
def test_review_does_not_treat_document_metadata_as_requirement_candidate(
    session: Session,
    tmp_path: Path,
) -> None:
    """Document metadata must not create host candidate blockers."""
    spec_content = """# Product Spec

**Status:** Draft
**Version:** 0.1
**Owner:** Operator

## Requirements

The system must include audit evidence.
"""
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
            artifact_json=_compiled_success_json(
                source_excerpt="The system must include audit evidence.",
            ),
        )
    )

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    pending = result["data"]["pending_authority"]
    assert "requirement_candidates" not in pending
    assert all(
        not str(finding.get("code", "")).startswith("AUTHORITY_CANDIDATE_")
        for finding in pending["review_findings"]
    )
```

- [ ] **Step 2: Run failing review test**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_authority_review.py::test_review_does_not_treat_document_metadata_as_requirement_candidate -q
```

Expected: fail because candidate IR still appears in review packets.

- [ ] **Step 3: Remove candidate IR from review snapshot**

In `services/agent_workbench/authority_review.py`:

- Remove use of `parse_markdown_sections`, `source_units_from_sections`,
  `extract_requirement_candidates`, `build_authority_mappings`, and
  `derive_review_findings` from review acceptability.
- Remove `requirement_candidates` from the public pending authority payload.
- Keep `review_findings` for fatal compiler/source-map problems.
- Keep `gaps`, `assumptions`, `source_map`, `invariants`,
  `eligible_feature_rules`, and `rejected_features`.

Replace candidate-derived review summary with:

```python
def _review_summary_from_artifact(artifact: SpecAuthorityCompilationSuccess) -> JsonDict:
    fatal_count = 0
    return {
        "acceptance_status": "reviewable",
        "fatal_finding_count": fatal_count,
        "compiler_gap_count": len(artifact.gaps),
        "assumption_count": len(artifact.assumptions),
        "invariant_count": len(artifact.invariants),
    }
```

- [ ] **Step 4: Remove candidate override requirement from decision service**

In `services/agent_workbench/authority_decision.py`:

- Keep request fields for backward compatibility during this migration if
  removing them would require a database migration.
- Stop requiring `incomplete_review_overrides` for accept.
- Stop treating `AUTHORITY_CANDIDATE_*` as blocking.
- Keep blocking for stale token, stale source, wrong authority fingerprint,
  packet computation failure, and non-overrideable fatal findings.

Replace `_accept_incomplete_error` logic with:

```python
def _accept_incomplete_error(
    *,
    request: AuthorityDecisionRequest,
    blocking_findings: list[JsonDict],
) -> JsonDict | None:
    """Return an accept error for fatal review findings only."""
    del request
    fatal_findings = [
        finding
        for finding in blocking_findings
        if not str(finding.get("code", "")).startswith("AUTHORITY_CANDIDATE_")
    ]
    if not fatal_findings:
        return None
    return _error(
        ErrorCode.AUTHORITY_REVIEW_INCOMPLETE.value,
        message="Authority review has fatal blocking findings.",
        details={"blocking_finding_count": len(fatal_findings)},
        remediation=["Reject the authority or fix the compiler/source evidence."],
    )
```

Adjust the exact signature to match the current local function shape.

- [ ] **Step 5: Update stale candidate-override tests**

In these test files, remove or rewrite tests that assert broad overrides are
rejected solely because candidate-specific overrides are missing:

- `tests/test_agent_workbench_authority_decision.py`
- `tests/test_agent_workbench_authority_decision_cli.py`
- `tests/test_api_dashboard.py`

Replace with tests proving:

- Candidate findings do not block accept.
- Fatal findings still block accept.
- CLI/dashboard may still pass old override fields without changing acceptance.

- [ ] **Step 6: Run review/decision tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_authority_review.py tests/test_agent_workbench_authority_decision.py tests/test_agent_workbench_authority_decision_cli.py tests/test_api_dashboard.py -q
```

Expected: pass.

## Task 7: Add Structured Spec Metadata To Review Packets

**Files:**
- Modify: `services/agent_workbench/authority_review.py`
- Test: `tests/test_agent_workbench_authority_review.py`

- [ ] **Step 1: Write failing review metadata test**

Add to `tests/test_agent_workbench_authority_review.py`:

```python
def test_review_includes_structured_spec_snapshot(
    session: Session,
    tmp_path: Path,
) -> None:
    spec_content = canonical_spec_json(
        TechnicalSpecArtifact.model_validate(_agileforge_spec_profile_payload())
    )
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
            artifact_json=_compiled_success_json(
                source_excerpt="The system MUST record audit evidence.",
            ),
        )
    )

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    spec = result["data"]["spec"]
    assert spec["format"] == "agileforge.spec.v1"
    assert spec["canonical_spec_sha256"].startswith("sha256:")
    assert spec["render_profile"] == "agileforge.spec_markdown.v1"
```

- [ ] **Step 2: Run metadata test and verify failure**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_authority_review.py::test_review_includes_structured_spec_snapshot -q
```

Expected: fail because review packets do not expose structured spec metadata.

- [ ] **Step 3: Implement structured spec snapshot detection**

In `services/agent_workbench/authority_review.py`, import:

```python
from pydantic import ValidationError
from utils.agileforge_spec_profile import (
    TechnicalSpecArtifact,
    canonical_spec_hash,
    render_markdown,
    rendered_markdown_hash,
)
```

Add helper:

```python
def _structured_spec_snapshot(spec_content: str) -> JsonDict | None:
    try:
        artifact = TechnicalSpecArtifact.model_validate_json(spec_content)
    except ValidationError:
        return None
    rendered = render_markdown(artifact)
    return {
        "format": "agileforge.spec.v1",
        "artifact_id": artifact.artifact_id,
        "canonical_spec_sha256": canonical_spec_hash(artifact),
        "render_profile": artifact.rendering.markdown_profile,
        "rendered_markdown_sha256": rendered_markdown_hash(rendered),
        "item_count": len(artifact.items),
        "relation_count": len(artifact.relations),
    }
```

When building the `spec` section of the review payload, merge this snapshot if
present.

- [ ] **Step 4: Run authority review tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_authority_review.py -q
```

Expected: pass.

## Task 8: Structured Spec Compiler Smoke Path

**Files:**
- Modify: `tests/test_specs_compiler_service.py`
- Modify: `orchestrator_agent/agent_tools/spec_authority_compiler_agent/instructions.txt`

- [ ] **Step 1: Add compiler input smoke test for structured spec**

Add to `tests/test_specs_compiler_service.py`:

```python
def test_default_compiler_invocation_marks_structured_spec_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[SpecAuthorityCompilerInput] = []

    async def fake_invoke(payload: SpecAuthorityCompilerInput) -> str:
        captured.append(payload)
        return _success_payload_json()

    monkeypatch.setattr(
        "services.specs.compiler_service._invoke_spec_authority_compiler_async",
        fake_invoke,
    )

    compiler_service._default_invoke_spec_authority_compiler(
        spec_content=canonical_spec_json(
            TechnicalSpecArtifact.model_validate(_agileforge_spec_profile_payload())
        ),
        content_ref=None,
        product_id=4,
        spec_version_id=9,
    )

    assert captured[0].spec_source_format == "agileforge.spec.v1"
```

- [ ] **Step 2: Run structured compiler test**

Run:

```bash
uv run --frozen pytest tests/test_specs_compiler_service.py::test_default_compiler_invocation_marks_structured_spec_format -q
```

Expected: pass after Task 3 and Task 4.

- [ ] **Step 3: Add prompt regression assertions**

In `tests/test_spec_authority_compiler_agent.py`, assert the support matrix is
present:

```python
def test_compiler_instructions_include_structured_spec_support_matrix() -> None:
    instructions = _compiler_instructions()

    assert "REQ/DATA/INTERFACE become REQUIRED_FIELD or RELATION_CONSTRAINT" in instructions
    assert "Unsupported normative items become gaps" in instructions
    assert "agileforge.spec_legacy_markdown.v1" in instructions
```

- [ ] **Step 4: Run compiler prompt tests**

Run:

```bash
uv run --frozen pytest tests/test_spec_authority_compiler_agent.py -q
```

Expected: pass.

## Task 9: Remove Obsolete IR Module If Unused

**Files:**
- Delete when safe: `utils/spec_authority_ir.py`
- Delete when safe: `tests/test_spec_authority_ir.py`

- [ ] **Step 1: Check remaining imports**

Run:

```bash
rg "spec_authority_ir|AUTHORITY_CANDIDATE_|requirement_candidates|authority_mappings|candidate_manifest" -n services utils orchestrator_agent tests
```

Expected: no production imports remain except database migration/readiness fields
that must stay for backward compatibility.

- [ ] **Step 2: Delete obsolete module and tests if there are no production imports**

Run:

```bash
rm -f utils/spec_authority_ir.py tests/test_spec_authority_ir.py
```

Expected: files are removed only after Step 1 proves no production imports remain.

- [ ] **Step 3: Run import boundary tests**

Run:

```bash
uv run --frozen pytest tests/test_tool_runtime_import_boundary.py -q
```

Expected: pass.

## Task 10: End-To-End Verification

**Files:**
- No new files.

- [ ] **Step 1: Run focused test suite**

Run:

```bash
uv run --frozen pytest \
  tests/test_agileforge_spec_profile.py \
  tests/test_spec_authority_compiler_agent.py \
  tests/test_spec_authority_compiler_normalizer.py \
  tests/test_specs_compiler_service.py \
  tests/test_agent_workbench_authority_review.py \
  tests/test_agent_workbench_authority_decision.py \
  tests/test_agent_workbench_cli.py \
  tests/test_agent_workbench_command_schema.py \
  -q
```

Expected: pass.

- [ ] **Step 2: Run static checks on touched Python files**

Run:

```bash
uv run --frozen ruff check \
  utils/agileforge_spec_profile.py \
  utils/spec_schemas.py \
  services/specs/profile_content.py \
  services/specs/compiler_service.py \
  services/specs/pending_authority_service.py \
  services/agent_workbench/authority_review.py \
  services/agent_workbench/authority_decision.py \
  cli/main.py
uv run --frozen ty check \
  utils/agileforge_spec_profile.py \
  utils/spec_schemas.py \
  services/specs/profile_content.py \
  services/specs/compiler_service.py \
  services/specs/pending_authority_service.py \
  services/agent_workbench/authority_review.py \
  services/agent_workbench/authority_decision.py \
  cli/main.py
```

Expected: both commands pass.

- [ ] **Step 3: Run repository check**

Run:

```bash
uv run --frozen pyrepo-check --all
```

Expected: pass.

- [ ] **Step 4: Manual CLI smoke with a structured spec**

Create `/tmp/agileforge-profile-smoke.json` with:

```json
{
  "schema_version": "agileforge.spec.v1",
  "artifact_id": "SPEC.profile-smoke",
  "title": "Profile Smoke Spec",
  "status": "draft",
  "version": "0.1",
  "created_at": "2026-05-18",
  "updated_at": "2026-05-18",
  "summary": "Validate structured spec profile flow.",
  "problem_statement": "The project needs a structured spec smoke fixture.",
  "items": [
    {
      "id": "REQ.profile.audit",
      "type": "REQ",
      "status": "proposed",
      "level": "MUST",
      "title": "Audit evidence",
      "statement": "The system MUST record audit evidence for authority decisions.",
      "verification": "system-test",
      "acceptance": [
        "Given an authority decision, when the decision is stored, then audit evidence records the decision actor and timestamp."
      ]
    }
  ],
  "relations": [],
  "controlled_terms": [],
  "external_references": [],
  "rendering": {
    "markdown_profile": "agileforge.spec_markdown.v1",
    "rendered_markdown_sha256": null
  }
}
```

Run:

```bash
agileforge spec profile validate \
  --spec-file /tmp/agileforge-profile-smoke.json \
  --render-md /tmp/agileforge-profile-smoke.md
```

Expected: `ok=true`, rendered Markdown file exists, and stdout is valid JSON.

- [ ] **Step 5: Manual project-create smoke**

Run:

```bash
agileforge project create \
  --name "Profile Smoke Project" \
  --spec-file /tmp/agileforge-profile-smoke.json \
  --idempotency-key profile-smoke-20260518-001 \
  --changed-by codex
```

Expected:

- `ok=true`
- `setup_status=authority_pending_review`
- `pending_authority_id` present
- stderr is clean

- [ ] **Step 6: Review smoke**

Run:

```bash
agileforge authority review --project-id <project_id> > /tmp/agileforge-profile-smoke-review.json
python3 -m json.tool /tmp/agileforge-profile-smoke-review.json >/tmp/agileforge-profile-smoke-review.pretty.json
```

Expected:

- JSON parses.
- `data.spec.format` is `agileforge.spec.v1`.
- No `AUTHORITY_CANDIDATE_*` findings appear.

## Self-Review Checklist

- Spec storage/fingerprint contract maps to Task 4 and Task 7.
- Item lifecycle and compiler support matrix map to Task 3 and Task 8.
- Deterministic validators are structural only in Task 1, Task 4, and Task 5.
- Legacy Markdown remains available but is explicitly marked by
  `agileforge.spec_legacy_markdown.v1`.
- Candidate extraction is removed from compiler input and review acceptability.
- CLI gives external LLM agents a concrete schema and validate/render loop.
