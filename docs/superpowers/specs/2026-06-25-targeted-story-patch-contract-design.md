# Targeted Story Patch Contract Design

**Status:** In Review  
**Version:** 0.1  
**Created:** 2026-06-25  
**Last Updated:** 2026-06-25  
**Owner:** AgileForge maintainers  
**Reviewers:** Alexandre Tavares  
**Spec Mode:** proposed_change  

## 1. Summary

AgileForge needs a durable targeted story refinement contract for Issue #160. A targeted refinement must let an agent update one existing `To Do` story without resending, rewriting, superseding, or dependency-mutating completed or sprint-linked sibling stories under the same parent requirement.

The design introduces a separate `UserStoryPatchOutput` draft contract and `story_patch` draft kind. Full parent decomposition remains `UserStoryWriterOutput` with `user_stories: list[UserStoryItem]`; targeted refinement becomes `UserStoryPatchOutput` with one `story: UserStoryItem`. Host code saves the patch story directly and never infers patch intent from list length or slot-index heuristics.

## 2. Problem Statement

Issue #159 made completed sibling stories safe during full-list story save by preserving unchanged progressed rows. That avoided one trap, but it left the root design problem: the story writer agent still uses a parent-level full-list output schema even when the user intends to refine only one existing story.

Issue #160 requires a stronger behavior:

- A parent requirement with Story A `Done` and Story B `To Do` can refine Story B without the agent outputting Story A.
- Story A is not rewritten, superseded, dependency-mutated, or included in `updated_story_ids`.
- Attempts to target Story A directly are rejected.
- Attempts to target a story outside the parent requirement are rejected.
- Existing full-list save remains compatible until an explicit migration/deprecation decision.

The current partial #160 branch adds `story save-patch`, but the reviewed artifact still comes from `UserStoryWriterOutput.user_stories`. For target slot `2`, service code extracts `user_stories[1]`. If a patch artifact contains only the target story, that extraction fails. A list-length fallback would be a compatibility heuristic, not a permanent contract, because it would make patch intent depend on how many list items the model happened to emit.

## 3. Goals And Non-Goals

### Goals

- Define an explicit targeted story patch output schema with one target story.
- Preserve the existing full-list story refinement contract for backwards compatibility.
- Make generation, draft storage, review history, save behavior, idempotency, and event metadata distinguish full drafts from targeted patches.
- Save targeted patches without indexing into `user_stories`.
- Preserve sibling story database rows and legacy `story_outputs` entries when saving a targeted patch.
- Reject unsafe targets before persistence.

### Non-Goals

- Do not deprecate or remove `agileforge story save`.
- Do not allow edits to completed, accepted, sprint-linked, or otherwise progressed stories.
- Do not support multi-story patch batches in this design.
- Do not change sprint planning semantics directly.
- Do not add a UI-specific design beyond preserving API/CLI contracts that a UI can consume.

## 4. Users And Stakeholders

- **Primary users:** AgileForge CLI/API users refining a backlog story after sibling stories already progressed.
- **Internal stakeholders:** Story phase maintainers, runtime/persistence maintainers, future agents consuming story history.
- **External systems:** GitHub issue workflow and downstream AgileForge sprint planning that consumes active user stories.

## 5. Current State

Current story generation uses `UserStoryWriterOutput` in `orchestrator_agent/agent_tools/user_story_writer_tool/schemes.py`. That output requires `user_stories: list[UserStoryItem]` and has no target field.

Runtime repair feedback in `services/story_runtime.py` instructs the model to match `UserStoryWriterOutput` and return top-level `parent_requirement`, `user_stories`, `is_complete`, and `clarifying_questions`.

The current partial #160 branch adds:

- `SaveStoryPatchInput` and `save_story_patch_tool` in `orchestrator_agent/agent_tools/user_story_writer_tool/tools.py`.
- `save_story_patch` in `services/phases/story_service.py`.
- `agileforge story save-patch` in `cli/main.py`.
- `POST /api/projects/{project_id}/story/save-patch` in `api.py`.

That persistence work is useful, but the draft contract remains ambiguous because patch save still extracts a target from a parent-level `user_stories` list.

## 6. Proposed Specification

### 6.1 Functional Requirements

| ID | Requirement | Acceptance Criteria | Priority |
| --- | --- | --- | --- |
| FR-001 | AgileForge must define a `UserStoryPatchOutput` schema for targeted story refinement. | The schema has `artifact_kind="story_patch"`, `parent_requirement`, required canonical `target_refinement_slot`, optional `target_story_id`, `story: UserStoryItem`, `is_complete`, `clarifying_questions`, and story quality fields. | Must |
| FR-002 | Patch-mode story generation must use the patch output schema. | A patch-mode generation request validates model output as `UserStoryPatchOutput`, not `UserStoryWriterOutput`. A valid patch artifact contains no sibling story list. | Must |
| FR-003 | Full-list story generation must remain compatible. | Existing full-list story generation still validates as `UserStoryWriterOutput` and produces `user_stories`. | Must |
| FR-004 | Patch attempts must be stored as patch attempts. | Attempt history records `draft_kind="story_patch"` and stores an output artifact with `artifact_kind="story_patch"` and one `story`. | Must |
| FR-005 | `story save-patch` must save only a patch attempt. | Save-patch rejects attempts whose current reusable draft is not `draft_kind="story_patch"` with a clear conflict error. | Must |
| FR-006 | Patch persistence must save the artifact story directly. | Save-patch reads `output_artifact["story"]` and never uses `user_stories[target_refinement_slot - 1]`. | Must |
| FR-007 | Patch save must preserve siblings. | When saving target slot `N`, database writes affect only the target row and dependency candidates for that target row. `story_outputs[parent_requirement].user_stories` preserves all non-target slots and replaces only slot `N`. | Must |
| FR-008 | Patch save must reject unsafe targets. | Completed, accepted, sprint-linked, superseded, wrong-product, or wrong-parent targets are rejected before persistence. | Must |
| FR-009 | Full-list save must reject patch attempts. | `agileforge story save` does not save `draft_kind="story_patch"` attempts. | Must |
| FR-010 | Idempotency and event metadata must distinguish operations. | Patch save idempotency keys and `WorkflowEvent` metadata include `operation="story_patch"` and the canonical target identifiers. Full save keys cannot replay patch saves, and patch keys cannot replay full saves. | Must |

### 6.2 User Scenarios

```gherkin
Scenario: Refine one To Do story without resending a completed sibling
  Given Story A is Done in refinement slot 1
  And Story B is To Do in refinement slot 2
  When the user generates a targeted patch for slot 2
  Then the reviewed artifact contains one story under the "story" field
  And the artifact records target_refinement_slot 2
  When the user saves the patch
  Then Story B is updated
  And Story A is not rewritten, superseded, dependency-mutated, or returned in updated_story_ids
```

```gherkin
Scenario: Reject a direct patch to a progressed story
  Given Story A is Done in refinement slot 1
  When the user generates or saves a targeted patch for Story A
  Then AgileForge rejects the operation with STORY_REPLACEMENT_UNSAFE or an equivalent phase conflict
  And no story rows are changed
```

```gherkin
Scenario: Keep full-list refinement behavior
  Given a parent requirement has no progressed sibling conflict
  When the user runs normal story generation
  Then AgileForge produces UserStoryWriterOutput with user_stories
  And story save persists the full-list draft through the existing compatible path
```

### 6.3 Data And State

#### Full Draft Artifact

```python
class UserStoryWriterOutput(BaseModel):
    parent_requirement: str
    user_stories: list[UserStoryItem]
    quality_schema_version: Literal["agileforge.story_quality.v1"]
    coverage_status: Literal["complete", "partial_capacity_limited", "needs_clarification"]
    remaining_scope: list[str]
    quality_findings: list[StoryQualityFinding]
    is_complete: bool
    clarifying_questions: list[str]
```

#### Patch Draft Artifact

```python
class UserStoryPatchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_kind: Literal["story_patch"] = "story_patch"
    parent_requirement: str
    target_refinement_slot: int
    target_story_id: int | None = None
    story: UserStoryItem
    quality_schema_version: Literal["agileforge.story_quality.v1"] = STORY_QUALITY_SCHEMA_VERSION
    coverage_status: Literal["complete", "needs_clarification"] = "complete"
    remaining_scope: list[str] = []
    quality_findings: list[StoryQualityFinding] = []
    is_complete: bool
    clarifying_questions: list[str]
```

Rules:

- `target_refinement_slot` is canonical and required.
- `target_story_id` is optional because slot-targeted workflows may not need it, but host code should include it when the target row is known.
- `story` is the only story payload in a patch artifact.
- `UserStoryPatchOutput` must forbid extra fields, including `user_stories`, to prevent ambiguous mixed artifacts.

#### Runtime Attempt

Patch attempts use the existing attempt history shape with these distinguishing fields:

```python
{
    "attempt_id": "story-attempt-...",
    "draft_kind": "story_patch",
    "artifact_fingerprint": "sha256:...",
    "output_artifact": UserStoryPatchOutput,
    "target_refinement_slot": 2,
    "target_story_id": 29,
    "is_reusable": True,
}
```

Full drafts continue to use `draft_kind="complete_draft"` with `UserStoryWriterOutput`.

#### Draft Projection

`draft_projection` must identify the reusable artifact kind:

```python
{
    "latest_reusable_attempt_id": "...",
    "kind": "story_patch",
    "artifact_fingerprint": "sha256:...",
    "target_refinement_slot": 2,
    "target_story_id": 29,
}
```

Full-draft projection remains `kind="complete_draft"`.

### 6.4 Interfaces And Integrations

#### CLI Generation

Normal full-list generation remains:

```bash
agileforge story generate --project-id 1 --parent-requirement "Requirement A" --input "<feedback>"
```

Targeted patch generation adds an explicit target selector:

```bash
agileforge story generate \
  --project-id 1 \
  --parent-requirement "Requirement A" \
  --target-refinement-slot 2 \
  --input "<feedback>"
```

or:

```bash
agileforge story generate \
  --project-id 1 \
  --parent-requirement "Requirement A" \
  --target-story-id 29 \
  --input "<feedback>"
```

Generation rules:

- Exactly one target selector may be supplied.
- Without a target selector, generation is full-list mode.
- With a target selector, generation is patch mode and uses `UserStoryPatchOutput`.
- Host code resolves `target_story_id` to a canonical refinement slot before storing a reusable patch draft.

#### CLI Save

Full-list save remains:

```bash
agileforge story save \
  --project-id 1 \
  --parent-requirement "Requirement A" \
  --attempt-id <attempt_id> \
  --expected-artifact-fingerprint <artifact_fingerprint> \
  --expected-state STORY_REVIEW \
  --idempotency-key <key>
```

Patch save remains a separate command:

```bash
agileforge story save-patch \
  --project-id 1 \
  --parent-requirement "Requirement A" \
  --attempt-id <attempt_id> \
  --expected-artifact-fingerprint <artifact_fingerprint> \
  --expected-state STORY_REVIEW \
  --idempotency-key <key> \
  --target-refinement-slot 2
```

Patch save rules:

- Exactly one target selector is required as a guard.
- The selector must match the reviewed patch attempt's target.
- Save-patch rejects `draft_kind="complete_draft"`.
- Save-patch reads `output_artifact["story"]`.

#### API

Generation API gains the same optional target selector fields as the CLI request:

```python
class StoryGenerateRequest(BaseModel):
    user_input: str | None = None
    force_feedback: bool = False
    target_story_id: int | None = None
    target_refinement_slot: int | None = None
```

Save-patch request remains guarded and target-aware:

```python
class StorySavePatchRequest(StorySaveRequest):
    target_story_id: int | None = None
    target_refinement_slot: int | None = None
```

The API must reject both/neither target selectors for patch save. For generation, both selectors are invalid; neither means full-list mode.

#### ADK Agent Contract

Google ADK structured output is configured on the agent contract with `Agent(output_schema=...)`. AgileForge's current story writer factory binds `output_schema=UserStoryWriterOutput` when constructing the agent. Patch mode must therefore use an explicit ADK agent contract for `UserStoryPatchOutput`.

Accepted implementation shapes:

- A separate `create_user_story_patch_agent()` factory with `output_schema=UserStoryPatchOutput` and patch-specific instructions.
- Or a parameterized factory that constructs a fresh `Agent` with either `UserStoryWriterOutput` or `UserStoryPatchOutput` before runtime invocation.

Rejected implementation shape:

- Mutating or monkey-patching `output_schema` on the existing module-level `root_agent` at runtime. That makes intent implicit, risks cross-request contamination, and does not match the explicit draft-kind contract in this spec.

Patch-specific instructions must tell the agent to refine only the requested target story, preserve sibling story intent by omission, and avoid inventing new sibling stories.

### 6.5 Error Handling And Edge Cases

| Case | Required Behavior | User/System Impact |
| --- | --- | --- |
| Patch generation target does not exist | Reject before agent call with target mismatch conflict. | No model cost, no runtime mutation. |
| Patch generation target belongs to a different parent or project | Reject before agent call with target mismatch conflict. | Prevents cross-parent accidental edits. |
| Patch generation target is progressed or sprint-linked | Reject before agent call when status/linkage is available. Save-patch must also re-check. | Prevents unsafe edits even if state changes between generation and save. |
| Patch output includes `user_stories` | Schema validation fails. | Prevents mixed full/patch artifacts. |
| Patch output omits `story` | Schema validation fails. | Prevents empty patch attempts. |
| Save-patch receives a full-list draft | Reject with phase conflict; do not save. | Prevents implicit heuristic behavior. |
| Full save receives a patch draft | Reject with phase conflict; do not save. | Prevents patch artifact from being treated as a replacement draft. |
| Save command target mismatches artifact target | Reject with target mismatch conflict. | Prevents stale or copied commands from saving the wrong row. |
| Target row changes after generation | Save-patch revalidates product, parent, slot, superseded status, progress status, and sprint linkages. | Prevents stale draft saves from mutating unsafe rows. |
| No existing `story_outputs` for parent | Save-patch may create a minimal parent output only when it can construct a valid target slot representation; otherwise it rejects with a clear conflict. | Avoids silently converting a patch into an incomplete parent output. |

## 7. Quality Attributes

### Security And Privacy

No new authentication surface is introduced. Patch target validation must happen at API/CLI boundaries and again before database persistence because target status can change between generation and save.

### Performance And Scale

Patch mode should reduce model token use by prompting for one story instead of an entire parent decomposition. Persistence touches one story row and target-scoped dependency candidates, so it should not be slower than full-list save.

### Reliability And Operations

Patch save must be idempotent independently from full-list save. Event metadata must include `operation="story_patch"`, `target_story_id`, `target_refinement_slot`, request hash, and saved row IDs. This gives support and audit tools enough evidence to distinguish targeted edits from full replacements.

### Observability

History, workflow summaries, and saved events must expose draft kind and target identifiers. A reviewer should be able to tell whether a current `STORY_REVIEW` artifact is a full draft or a targeted patch without inspecting story payload shape.

### Accessibility And Localization

No direct UI accessibility or localization behavior is changed by this backend/CLI contract.

## 8. Alternatives Considered

| Option | Pros | Cons | Decision |
| --- | --- | --- | --- |
| List-length heuristic: `len(user_stories) == 1` means patch | Smallest code change. | Ambiguous, schema-unsafe, can misclassify a one-story full draft as a patch, and preserves the wrong model contract. | Rejected. |
| Host-only `save-patch` using full-list reviewed draft | Reuses existing generation flow. | Still requires the agent to output siblings, so it misses #160's main acceptance criterion. | Rejected as permanent solution. |
| Separate `UserStoryPatchOutput` and `story_patch` draft kind | Clear type boundary, no heuristics, target intent preserved end-to-end. | Requires runtime and prompt/schema changes. | Chosen. |
| Runtime mutation of the existing ADK story writer agent schema | Avoids adding a second factory. | Makes schema intent implicit and risks shared-agent contamination. | Rejected. |
| Separate or parameterized ADK agent factory for patch mode | Keeps ADK structured output explicit at agent construction time and allows patch-specific instructions. | Adds one more factory path to test. | Chosen. |
| Replace all story generation with patch-style partial updates | Simplifies one path long-term. | Breaks existing full parent decomposition and planning workflows. | Rejected for #160. |

## 9. Dependencies And Constraints

- **Dependencies:** Existing story runtime, story phase service, story writer schemas, CLI/API command surfaces, `WorkflowEvent` metadata, `UserStory` persistence model.
- **Constraints:** Full-list save must remain compatible. Existing accepted projects and story histories must remain readable. Google ADK structured output must remain explicit through agent construction with the correct output schema for the selected mode.
- **Assumptions:** Refinement slot remains the stable parent-local identity for story ordering. Story ID can be used as a user-facing selector when resolving it to the canonical slot.

## 10. Rollout, Migration, And Compatibility

This change is additive:

- Existing full-list story drafts and saves continue to work.
- New patch-mode attempts use `draft_kind="story_patch"`.
- History and save code must handle both artifact shapes.
- No database migration is required unless a later implementation chooses to persist draft kind outside existing JSON runtime state.
- Existing `story save-patch` work in the partial #160 branch should be refactored to reject full-list drafts instead of saving from `user_stories`.

Rollback is straightforward before release: remove patch-mode generation and `save-patch` command exposure. Full-list story save remains the stable path.

## 11. Success Metrics

| Metric | Target | Measurement Source |
| --- | --- | --- |
| Patch artifact shape correctness | Patch-mode generation produces `UserStoryPatchOutput` with exactly one `story` and no `user_stories`. | Unit tests for schema/runtime parsing. |
| Sibling mutation prevention | Targeted patch save does not change sibling DB rows or sibling `story_outputs` entries. | Regression tests with completed sibling and To Do target. |
| Unsafe target rejection | Completed/sprint-linked/wrong-parent targets are rejected before persistence. | Unit tests for API/service/tool layers. |
| Compatibility | Existing full-list story save tests continue to pass. | Existing test suite. |
| No heuristic extraction | No patch save code indexes into `user_stories` for targeted patches. | Code review and focused regression tests. |

## 12. Open Questions

| Question | Impact | Owner | Status |
| --- | --- | --- | --- |
| Should patch-mode generation be exposed through `story generate` flags or a separate `story patch-generate` command? | Separate command may be clearer but increases command surface. Flags preserve one command with explicit mode. | Maintainers | Proposed answer: use `story generate` flags. |
| Should `target_story_id` become required in `UserStoryPatchOutput` after host resolution? | Requiring it improves auditability but slot-only workflows can work with canonical slot. | Maintainers | Proposed answer: optional in schema, included when known. |

## 13. Revision History

| Date | Version | Change | Author |
| --- | --- | --- | --- |
| 2026-06-25 | 0.1 | Initial proposed-change design for explicit targeted story patch draft contract. | Codex |
| 2026-06-25 | 0.2 | Added explicit Google ADK agent factory/schema contract for patch mode. | Codex |
