# Task Packet v2 Schema Reference

## Summary

Task Packet v2 is the canonical task-local execution artifact. It is assembled deterministically from persisted project data, anchored by `task_id + sprint_id`, and designed to be consumed after the parent story context has already been bootstrapped separately through `story_packet.v1`.

The packet is intentionally narrow: it describes the local execution slice, not the full story completion contract.

The public endpoint can also return an optional rendered view through the `flavor` query parameter. The API envelope remains `{ "status": "success", "data": <canonical packet> }`, with an optional `data.render` field when `flavor` is supplied. Task renderings use task-checklist semantics, and the agent task prompt render assumes the parent story bootstrap has already been loaded.

## Canonical Schema

```ts
type TaskPacket = {
  schema_version: "task_packet.v2";

  metadata: {
    packet_id: string;
    generated_at: string;
    generator_version: "v2";
    source_fingerprint: string;
  };

  source_snapshot: {
    project_id: number;
    sprint_id: number;
    story_id: number;
    task_id: number;

    project_updated_at: string;
    sprint_updated_at: string;
    sprint_story_added_at: string;
    story_updated_at: string;
    story_ac_updated_at: string | null;
    task_updated_at: string;
    task_metadata_hash: string;

    accepted_spec_version_id: number | null;
    validation_validated_at: string | null;
    validation_input_hash: string | null;
    validation_evidence_hash: string | null;
    compiled_authority_id: number | null;
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
    checklist_items: string[];
    is_executable: boolean;
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

    project: {
      project_id: number;
      name: string;
      vision_excerpt: string | null;
    };
  };

  constraints: {
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

- A Task Packet is valid only if the requested task belongs to a story and that story is linked to the requested sprint through `SprintStory`.
- The API must reject invalid task/sprint combinations instead of inferring context.

### Task-local completion contract

- `checklist_items` are sourced only from canonical task metadata.
- `is_executable` is `true` only when `checklist_items` is non-empty.
- Story acceptance criteria are not promoted into `checklist_items`.
- If a task has no checklist items, the packet remains valid but represents a non-executable/reference-only task slice.

### Deterministic helper fields

- `packet_id` is derived from `(schema_version, sprint_id, task_id)`.
- `label` is a deterministic display label derived from the task description.
- `vision_excerpt` is the first non-empty paragraph of product vision, truncated deterministically.

### Spec authority and validation

- Task Packet v2 continues to use story-pinned authority only.
- `task_hard_constraints` are resolved from task-local `relevant_invariant_ids` against the pinned compiled authority.
- `story_compliance_boundaries` are still derived from validation finding invariant IDs because those are relevant execution boundaries inherited from the parent story.
- The packet never falls back to newer Project authority when a story is unpinned.

### Story/task separation

- Story completion context lives canonically in `story_packet.v1`.
- Task Packet v2 keeps only compact story orientation fields plus inherited spec/validation metadata needed during execution.
- Renderers may compose story bootstrap context with the task delta later, but the canonical Task Packet v2 payload stays task-local.

### Source fingerprint

- `source_fingerprint` must change when task, story, sprint, sprint membership, task metadata, pinned authority, validation evidence, or Project timestamps relevant to the packet change.
- `validation_evidence_hash` is the canonical SHA-256 hash of the complete parsed `ValidationEvidence` record. It covers validity, validator and input/spec binding, checked rules and invariants, finding boundary IDs, failures, warnings, and alignment findings. JSON object key order and storage formatting do not affect it.

## Public Endpoint

`GET /api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/packet`

The response shape is `{ "status": "success", "data": <canonical packet> }`. If `flavor` is supplied, `data.render` contains a derived prompt or brief for the requested presentation style.
The recognized flavor families are `human|markdown|brief` for human-readable briefs and `cursor|copilot|agent|xml` for agent-style prompts; unknown values fall back to agent-style rendering.
