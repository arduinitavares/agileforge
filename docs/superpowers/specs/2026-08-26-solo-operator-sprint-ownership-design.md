# Solo-Operator Sprint Ownership Design

**Issue:** #224

**Status:** Approved with amendments on 2026-08-26

**Governing handoff:**
`docs/feedback/2026-08-24-story-refinement-to-sprint-selection-design-handoff.md`

## Objective

Make a project-scoped solo-operator role the default Sprint owner without
weakening named-team support, immutable Sprint-plan evidence, replay identity,
transactional acceptance, or the existing Story-selection and dependency
controls.

AgileForge has no durable fact that identifies a particular human. Issue #224
therefore models the default as the accountable solo role for one durable
Project. It does not add a person name, authentication identity, Profile owner,
or sticky preferred Team.

## Terms

- `solo_project`: the default accountable role for one durable Project.
- `named_team`: a new plan generated with an explicit meaningful Team override.
- `legacy_named_team`: a projection-only kind for an artifact whose durable
  generation attempt predates owner-kind evidence.
- `owner_label`: the exact value retained in the existing
  `SprintPlanEnvelope.team_name` field.
- `owner_key`: a deterministic non-person identity used in projections and
  rendered evidence.

Agents, request actors, OS users, email addresses, Profile names, and product
personas are never Sprint owners.

## Reserved Namespace

The complete reserved namespace is every canonically trimmed string whose
case-folded value begins with:

```text
[agileforge:sprint-owner:
```

An explicit named-Team override in this namespace is invalid. It must fail
before node-attempt creation and provider dispatch.

The v1 solo owner key is:

```text
agileforge:sprint-owner:solo-project:v1:project:{project_id}
```

The exact persisted label is:

```text
[{owner_key}] Solo operator for {project_name_snapshot}
```

For example:

```text
[agileforge:sprint-owner:solo-project:v1:project:42] Solo operator for AgileForge
```

The key depends only on the stable `project_id`. The label snapshots the
Project name visible when generation begins. The persisted Project name must be
nonblank, at most 200 characters, and contain no Unicode control characters or
line separators. Otherwise default resolution fails as
`SPRINT_OWNER_UNAVAILABLE`.

Named and legacy owner keys are derived from the SHA-256 of the exact UTF-8
owner label:

```text
agileforge:sprint-owner:named-team:v1:sha256:{digest}
agileforge:sprint-owner:legacy-named-team:v1:sha256:{digest}
```

Kind is never inferred from a label or key prefix. The namespace is used only
to reject spoofed overrides and validate a kind already proven by durable host
evidence.

## Request and Default Semantics

The public API and application request retain the compatibility field
`team_name`, now as an optional named-Team override.

| Input | Meaning |
| --- | --- |
| property omitted | resolve `solo_project` |
| JSON `null` | resolve `solo_project` |
| nonblank string | trim and resolve `named_team` |
| empty or whitespace string | invalid; never default |
| reserved-namespace string | `SPRINT_OWNER_CONFLICT` |

The CLI makes `--team-name` optional. Existing explicit invocations retain
their exact named-Team behavior.

The browser shows the provider-free resolved solo owner before enabling Sprint
generation. Its optional input is labelled `Named team override`. Leaving it
empty omits `team_name` from the request; the browser never sends an empty
string as a default request.

## Shared Resolver

`services/sprint_ownership.py` owns normalization, namespace validation,
Project resolution, owner-key derivation, owner-evidence loading, and reserved
Team validation. API, CLI, read projections, planning, replay, acceptance, and
packet generation must use this contract rather than duplicate it.

The generation resolver performs these steps before replay or attempt
creation:

1. Load the exact Project.
2. Validate explicit input or the default Project identity.
3. Produce `owner_kind`, `owner_key`, and `owner_label`.
4. For `solo_project`, perform the provider-free reserved-Team collision check.
5. Return a closed error without creating an attempt or invoking a provider.

The canonical resolved `owner_label` remains the normalized `team_name` inside
the existing planner host payload.

## Durable Owner-Kind Evidence

`SprintPlanEnvelope v1` remains unchanged. Its exact fields continue to be:

```text
schema_version
team_name
spec_version_id
spec_hash
candidate_set_fingerprint
planner_output
```

New Sprint attempts add `owner_kind` to the trusted host normalized input next
to the resolved `team_name`. Existing `WorkflowNodeAttempt` persistence and the
`start_node_attempt` transition receipt canonicalize and fingerprint both
values. No database column is added.

`_SprintRecipePayload.owner_kind` is optional only to parse a legacy stored or
in-flight attempt. Every newly prepared attempt must supply either
`solo_project` or `named_team`.

The unchanged `RecordSprintPlan` request carries the durable attempt ID and
attempt fingerprint. The Sprint output stores the resulting artifact ID and
plan fingerprint in the attempt outcome. This chain binds one artifact to the
host-owned owner kind without changing the envelope.

`load_sprint_owner_evidence(...)` recovers an artifact owner as follows:

1. Locate the single successful `planning.sprint.plan` attempt outcome whose
   output contains the exact artifact ID and plan fingerprint.
2. Load the exact Project-scoped node attempt.
3. Load its `start_node_attempt` receipt by idempotency key.
4. Revalidate receipt request fingerprint, attempt input fingerprint, node,
   Project, artifact identity, and exact normalized input.
5. Require attempt and receipt `team_name` to equal the v1 envelope value.
6. Equal non-null kinds produce `solo_project` or `named_team` evidence.
7. Kind absent from both old inputs produces `legacy_named_team`.
8. Missing on only one side, mismatched, unknown, duplicate, or malformed
   evidence fails closed.

An artifact with no matching attempt outcome is legacy named-Team evidence.
This preserves direct and historical v1 records. Such an artifact is never
reclassified as solo by inspecting its label.

## Legacy Replay

Replay starts from the stored normalized input.

- A stored attempt with `owner_kind` requires exact requested kind and label.
- A stored attempt without `owner_kind` may replay only an explicit named-Team
  request with the exact stored label.
- The replay merger must not inject `owner_kind` into a legacy normalized
  input, preserving its historical start-receipt fingerprint.
- An omitted/default solo request cannot replay a legacy named-Team attempt.

## Team Persistence and Collision Matrix

The `Team` row used by `solo_project` is an internal persistence carrier for a
single accountable role. It is not evidence of a multi-person Scrum Team.
Existing named Teams retain their current exact-name, cross-Project reuse
semantics.

A reserved solo-owner Team may be linked exclusively to its encoded Project.
The identical collision check runs during provider-free resolution and again
inside the Sprint-acceptance transaction.

| Matching reserved Team row | Project links | Result |
| --- | --- | --- |
| absent | none | preflight succeeds; acceptance creates Team and current link |
| exactly one | current Project only | reuse exact Team |
| exactly one | no links | conflict |
| exactly one | another Project | conflict |
| exactly one | current plus another Project | conflict |
| exactly one | multiple other Projects | conflict |
| multiple matching rows | any | conflict |

Acceptance revalidates the artifact, owner evidence, exact label, namespace,
and current Team links after obtaining the workflow write transaction and
before projecting a Sprint. Any conflict rolls back Team, ProjectTeam, Sprint,
tasks, Sprint membership, and the Sprint decision. Pre-provider success is
never trusted as acceptance-time authority.

## Project Rename Semantics

There is no public Project rename command today. The contract nevertheless
treats Project names as future-mutable display state:

- the solo key and Team identity remain bound to immutable `project_id`;
- the attempt and envelope retain the generation-time name snapshot;
- historical review and packets render that stored snapshot;
- a later rename changes only future default labels;
- a rename during an attempt is rejected by existing business-fact freshness
  checks.

## Read and Rendered Evidence

The provider-free Sprint-candidate projection adds:

```json
{
  "sprint_owner": {
    "kind": "solo_project",
    "key": "agileforge:sprint-owner:solo-project:v1:project:42",
    "label": "[agileforge:sprint-owner:solo-project:v1:project:42] Solo operator for AgileForge",
    "named_team_override_allowed": true
  }
}
```

Sprint review reload uses durable owner evidence and advances its Sprint-only
review schema to `agileforge.planning-artifact-review.v2`. Other planning
artifact reviews remain v1.

Canonical Story and Task packet contexts add `owner_kind` and `owner_key` while
retaining `team_name` as the exact owner label. Their schema versions advance:

```text
story_packet.v2 -> story_packet.v3
task_packet.v3 -> task_packet.v4
```

Legacy artifacts project `legacy_named_team`. New named-Team artifacts project
`named_team`. Renderers use `Sprint owner`, never infer kind from the label,
and preserve the exact historical value.

## Errors and Transport Mapping

| Condition | Code | HTTP |
| --- | --- | --- |
| explicit empty/whitespace input | request validation | 422 |
| Project absent | `PROJECT_NOT_FOUND` | 404 |
| malformed/unavailable default identity | `SPRINT_OWNER_UNAVAILABLE` | 409 |
| reserved explicit override | `SPRINT_OWNER_CONFLICT` | 409 |
| reserved Team collision or evidence conflict | `SPRINT_OWNER_CONFLICT` | 409 |

CLI commands return nonzero and render the same closed error code. Read
projections return their existing error envelope; the browser treats either
ownership error as generation-blocking.

## Protected Boundaries

- No provider call or Sprint generation is used to verify #224.
- Existing named-Team artifacts, fingerprints, receipts, Team rows, Sprint
  rows, and accepted evidence are not rewritten.
- No database schema or v1 Sprint-envelope migration is introduced.
- Selected Story scope, dependency confirmation, freshness, lineage, capacity,
  provider confirmation, stale-action, duplicate-submission, and human-review
  behavior remain unchanged.
- No person identity, authentication model, sticky Team preference, or agent
  accountability is added.
