# Task Packet v1 Schema Reference

## Summary

Task Packet v1 is the original deterministic task-packet contract that shipped before the Task 3 split introduced `story_packet.v1` and promoted the live task endpoint to `task_packet.v2`.

This document is retained as a historical schema reference for the superseded v1 payload. It does **not** describe the active response shape of the live `/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/packet` endpoint anymore.

For the active task endpoint contract, see [Task Packet v2 Schema Reference](./task-packet-schema-v2.md).

## Canonical Schema

```ts
type TaskPacket = {
  schema_version: "task_packet.v1";

  metadata: {
    packet_id: string;
    generated_at: string;
    generator_version: "v1";
    source_fingerprint: string;
  };

  source_snapshot: {
    product_id: number;
    sprint_id: number;
    story_id: number;
    task_id: number;

    product_updated_at: string;
    sprint_updated_at: string;
    sprint_story_added_at: string;
    story_updated_at: string;
    story_ac_updated_at: string | null;
    task_updated_at: string;
    task_metadata_hash: string;

    accepted_spec_version_id: number | null;
    validation_validated_at: string | null;
    validation_input_hash: string | null;
    compiled_authority_compiled_at: string | null;
  };

  task: {
    task_id: number;
    label: string;
    description: string;
    status: "To Do" | "In Progress" | "Done" | "Cancelled";
    assignee_member_id: number | null;
    assignee_name: string | null;
    task_kind: "analysis" | "design" | "implementation" | "testing" | "documentation" | "refactor" | "other";
    artifact_targets: string[];
    workstream_tags: string[];
  };

  context: {
    story: {
      story_id: number;
      title: string;
      persona: string | null;
      story_description: string | null;
      status: "To Do" | "In Progress" | "Done" | "Accepted";
      story_points: number | null;
      rank: string | null;
      source_requirement: string | null;
    };

    sprint: {
      sprint_id: number;
      goal: string | null;
      status: "Planned" | "Active" | "Completed";
      started_at: string | null;
      start_date: string;
      end_date: string;
      team_id: number | null;
      team_name: string | null;
    };

    product: {
      product_id: number;
      name: string;
      vision_excerpt: string | null;
    };
  };

  constraints: {
    acceptance_criteria_text: string | null;
    acceptance_criteria_items: string[];

    spec_binding: {
      mode: "pinned_story_authority";
      binding_status: "pinned" | "unpinned";
      spec_version_id: number | null;
      authority_artifact_status: "available" | "missing";
    };

    validation: {
      present: boolean;
      passed: boolean | null;
      freshness_status: "current" | "stale" | "missing";
      validated_at: string | null;
      validator_version: string | null;
      current_story_input_hash: string;
      validation_input_hash: string | null;
      input_hash_matches: boolean | null;
      rules_checked: string[];
    };

    task_hard_constraints: Array<{
      invariant_id: string;
      type: "FORBIDDEN_CAPABILITY" | "REQUIRED_FIELD" | "MAX_VALUE";
      parameters: Record<string, string | number>;
      source_excerpt: string | null;
      source_location: string | null;
    }>;

    story_compliance_boundaries: Array<{
      invariant_id: string;
      type: "FORBIDDEN_CAPABILITY" | "REQUIRED_FIELD" | "MAX_VALUE";
      parameters: Record<string, string | number>;
      source_excerpt: string | null;
      source_location: string | null;
    }>;

    findings: Array<{
      severity: "warning" | "failure";
      source: "validation_failure" | "validation_warning" | "alignment_warning" | "alignment_failure";
      code: string;
      message: string;
      invariant_id: string | null;
      rule: string | null;
      capability: string | null;
    }>;
  };
};
```

## Assembly Rules

### Identity and linkage
- A packet is valid only if the requested task belongs to a story and that story is linked to the requested sprint through `SprintStory`.
- The API must reject invalid task/sprint combinations instead of inferring context.

### Deterministic helper fields
- `packet_id` is deterministic and derived from the tuple `(schema_version, sprint_id, task_id)`.
- `label` is a deterministic display label derived from the task description.
- `vision_excerpt` is the first non-empty paragraph of product vision, truncated deterministically.
- `acceptance_criteria_items` are derived by normalizing the raw acceptance criteria text into ordered line items.

### Spec authority
- Task Packet v1 uses **story-pinned authority only**.
- If `accepted_spec_version_id` exists, the packet may expose relevant invariants from that exact compiled authority.
- If `accepted_spec_version_id` is missing, the packet must set the spec binding to `unpinned`.
- The packet must never silently fall back to the latest product authority.

### Scoped Constraints
- Task Packet v1 removes `relevant_invariants` to avoid polluting narrow task bounds with broad story-level architectural context.
- `task_hard_constraints` handles specific execution bounds for the task. In Phase 2 it is populated only from task-local metadata bindings (`relevant_invariant_ids`) resolved against the story's pinned compiled authority.
- `story_compliance_boundaries` is derived from the pinned compiled authority joined to invariant IDs referenced by alignment findings (`finding_invariant_ids`).
- If the compiled authority artifact is unavailable or unparsable, `authority_artifact_status` becomes `missing` and both constraint arrays remain empty.

### Structured task metadata
- Persisted tasks now carry canonical metadata in `Task.metadata_json`.
- The canonical metadata object is:
  - `version: "task_metadata.v1"`
  - `task_kind`
  - `artifact_targets`
  - `workstream_tags`
  - `checklist_items`
  - `relevant_invariant_ids`
- Existing tasks are backfilled one-way to the canonical empty metadata object.
- Unknown invariant IDs in task metadata are ignored during packet assembly and never promoted into `task_hard_constraints`.

### Validation freshness
- Validation freshness is based on the recomputed current story input hash versus the stored validation evidence input hash.
- `current` means the hashes match.
- `stale` means validation evidence exists but the hashes differ.
- `missing` means no validation evidence is present.

### Source fingerprint
- `source_fingerprint` must change when any of the following change:
  - task update
  - story update
  - acceptance criteria update
  - sprint update
  - sprint-story membership timestamp
  - validation evidence timestamp or input hash
  - task metadata hash
  - pinned spec version
  - compiled authority compilation timestamp
  - product update

## Sprint Planner Task Shape

Sprint planner decomposition now emits structured task objects, not plain strings:

```ts
type StructuredTaskSpec = {
  description: string;
  task_kind: "analysis" | "design" | "implementation" | "testing" | "documentation" | "refactor" | "other";
  artifact_targets: string[];
  workstream_tags: string[];
  checklist_items: string[];
  relevant_invariant_ids: string[];
};
```

Validation rules:
- `description` must be non-empty.
- `task_kind` must use the allowed enum values.
- `artifact_targets`, `workstream_tags`, `checklist_items`, and `relevant_invariant_ids` are trimmed, deduped, and reject empty-string values.
- `relevant_invariant_ids` must be a subset of the parent story's `evaluated_invariant_ids`.

## Historical Endpoint Note

Task Packet v1 was originally returned by:

`GET /api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/packet`

That public endpoint now returns `task_packet.v2`. This file remains only as a historical record of the superseded v1 schema.

## Explicit Non-Goals

Task Packet v1 does **not** include:
- prompt text
- human brief text
- agent-flavor formatting
- roadmap dumps
- sibling task dumps
- target file guesses
- codebase hints not already modeled upstream

Those belong in later renderers or future schema extensions, not in the canonical v1 packet.
