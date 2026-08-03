# Story Packet v1 Schema Reference

## Summary

Story Packet v1 is the canonical story-session bootstrap artifact. It is assembled deterministically from persisted project data, anchored by `story_id + sprint_id`, and carries the story completion contract plus inherited Project/Sprint/spec context.

This packet is the stable bootstrap companion to `task_packet.v2`.

The public endpoint can also return an optional rendered view through the `flavor` query parameter. The API envelope remains `{ "status": "success", "data": <canonical packet> }`, with an optional `data.render` field when `flavor` is supplied. Story renderings use story-acceptance-criteria and bootstrap semantics.

## Canonical Schema

```ts
type StoryPacket = {
  schema_version: "story_packet.v1";

  metadata: {
    packet_id: string;
    generated_at: string;
    generator_version: "v1";
    source_fingerprint: string;
  };

  source_snapshot: {
    project_id: number;
    sprint_id: number;
    story_id: number;

    project_updated_at: string;
    sprint_updated_at: string;
    sprint_story_added_at: string;
    story_updated_at: string;
    story_ac_updated_at: string | null;

    accepted_spec_version_id: number | null;
    validation_validated_at: string | null;
    validation_input_hash: string | null;
    compiled_authority_id: number | null;
    compiled_authority_compiled_at: string | null;
    task_plan_hash: string;
  };

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

  task_plan: {
    tasks: Array<{
      id: number;
      description: string;
      status: "To Do" | "In Progress" | "Done" | "Cancelled";
      task_kind: "analysis" | "design" | "implementation" | "testing" | "documentation" | "refactor" | "other";
      artifact_targets: string[];
      workstream_tags: string[];
      checklist_items: string[];
      is_executable: boolean;
    }>;
  };

  context: {
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
    story_acceptance_criteria_text: string | null;
    story_acceptance_criteria_items: string[];

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

- A Story Packet is valid only if the requested story belongs to the project and is linked to the requested sprint through `SprintStory`.
- The API must reject invalid story/sprint combinations instead of inferring context.

### Story completion contract

- `story_acceptance_criteria_text` is copied directly from the persisted story and exposed under `constraints`.
- `story_acceptance_criteria_items` are derived by deterministically normalizing the acceptance criteria text into ordered line items.
- Story completion remains story-scoped; task-local checklist items are not stored in the `story` block.

### Story task plan

- `task_plan.tasks` is the ordered list of tasks currently decomposed under the story for the requested story+sprint packet context.
- Because tasks are currently stored at story scope and there is no separate `task -> sprint` relation, the task plan is story-scoped rather than filtered to sprint-only task membership.
- Each task entry reuses the canonical sprint task serialization shape, including task-local metadata fields such as `checklist_items` and `is_executable`.
- `task_plan` is orientation/bootstrap context for the story session; it does not replace `task_packet.v2` as the canonical task-local execution artifact.

### Spec authority and validation

- Story Packet v1 uses only story-pinned authority.
- `story_compliance_boundaries` are derived from validation finding invariant IDs resolved against the pinned compiled authority.
- If the story is unpinned or the compiled artifact is unavailable, the spec binding reflects that state and the boundaries array is empty.

### Source fingerprint

- `source_fingerprint` must change when story, sprint, sprint membership, validation, authority, Project, or task-plan inputs relevant to the packet change.
- `task_plan_hash` captures the serialized `task_plan.tasks` payload so task description, status, metadata, checklist, and other serialized task-plan changes affect story-packet freshness deterministically.

## Public Endpoint

`GET /api/projects/{project_id}/sprints/{sprint_id}/stories/{story_id}/packet`

The response shape is `{ "status": "success", "data": <canonical packet> }`. If `flavor` is supplied, `data.render` contains a derived prompt or brief for the requested presentation style.
