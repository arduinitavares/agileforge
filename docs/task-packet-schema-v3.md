# Task Packet Schema v3

`task_packet.v3` is the deterministic task-local execution handoff derived from
one exact accepted Specification, Backlog, Roadmap, Story artifact, accepted
Sprint plan, activated Sprint, and materialized Task.

The ordered root is:

1. `schema_version` (`task_packet.v3`)
2. `packet_kind` (`task`)
3. `metadata`
4. `lineage`
5. `context`
6. `evidence`
7. `work`

`metadata` contains only `packet_id` and `source_fingerprint`. There is no
generation timestamp. `source_fingerprint` is the normal `sha256:` canonical
hash of the complete ordered `lineage`, `context`, `evidence`, and `work`.

`lineage` contains exact machine identity for `specification`, `backlog`,
`roadmap`, `story`, `sprint_plan`, `sprint`, and `task`, in that order. It is
never rendered to humans.

`context` contains Project name/identity and the Sprint goal, status, Team name,
start timestamp, and dates. `evidence` contains the pinned Specification
currentness and exact cited items, exact Backlog item, containing Roadmap
release, accepted canonical Story item, selected Sprint-plan Story, and strict
`agileforge.story-validation-evidence.v2` snapshot.

`work.story` keeps the accepted Story statement and acceptance criteria separate
from Task completion. `work.task` contains description, status, assignee display
name, and exact canonical `task_metadata.v2`; its `checklist_items` are the Task
completion contract.

Reads require accepted Sprint-plan activation. A planned Sprint is readable.
When the pinned Specification is superseded, exact SprintStart lineage is also
required and the complete frozen execution contract must match its exact plan,
decision, Stories, Tasks, and dependency proof. Superseded planned Sprints and
corrupt execution contracts fail closed. Missing or inconsistent lineage,
corrupt canonical content, and invalid Task metadata also fail closed.

Canonical render flavors are exactly `human` and `agent`. Omitted flavor returns
the exact seven-key canonical JSON. A flavored read returns a separate closed
`packet` plus `render` view and does not mutate that packet. Empty, old, and
unknown flavor values are unsupported. Both renderings use the shared complete
canonical validator before producing text.
