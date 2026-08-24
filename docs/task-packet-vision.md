# Execution Packet Vision

## Purpose

AgileForge hands accepted planning evidence to delivery through two canonical,
deterministic artifacts:

- `story_packet.v2` bootstraps one Story session.
- `task_packet.v3` describes one Task-sized execution slice.

The packet is the durable product. Human briefs and agent prompts are closed
renderings of that packet, not alternative sources of truth.

## Accepted delivery chain

Both packet kinds are reconstructed from one exact accepted chain:

```text
Specification -> Backlog item -> Roadmap release -> Story item
              -> accepted Sprint plan -> activated Sprint -> Task
```

The builder loads the exact Specification pinned by that chain. It never
substitutes the current Specification. A pinned accepted Specification is
labelled `current`; an amended historical pin is labelled `superseded`.

Accepted activation is sufficient for a planned Sprint packet. `SprintStart`
proof is needed only for the older-lineage execution exception: an active or
terminal Sprint may retain its exact superseded Specification lineage when the
shared execution-integrity contract proves the exact accepted plan, decision,
Story set, Task set, and dependency snapshot. A planned Sprint or loose Story
or Task cannot use that exception.

## Trust and determinism

Canonical packets:

- contain only the exact ordered root defined by their schema;
- derive `packet_id` from the schema and complete durable lineage;
- derive `source_fingerprint` from ordered lineage, context, evidence, and work;
- contain no wall-clock generation timestamp;
- require canonical `task_metadata.v2` bytes and exact accepted-plan identity;
- fail closed on absent, ambiguous, corrupt, stale, or inconsistent evidence.

The same durable state therefore produces byte-equivalent canonical JSON.

## Story and Task ownership

The Story Packet owns Story-level acceptance criteria and the ordered Task plan.
The Task Packet owns one Task description, checklist, targets, workstream, and
strict metadata. Story acceptance criteria never become a Task checklist by
implication.

## Rendering

The only rendering flavors are `human` and `agent`.

- Human output presents normal-language evidence and currentness while hiding
  database IDs, hashes, fingerprints, instance keys, and lineage metadata.
- Agent output may retain only domain item identifiers required to execute and
  report work. It omits persistence, graph, receipt, and provider internals.

The canonical packet remains an exact seven-key object. Omitted flavor returns
that object. A selected flavor returns a separate `packet` plus `render` view;
empty and unknown flavors fail closed. Both renderers call the shared complete
canonical validator, so missing, unknown, malformed, or non-finite packet
content cannot be converted into usable-looking text.

See [Story Packet v2](./story-packet-schema-v2.md) and
[Task Packet v3](./task-packet-schema-v3.md) for the exact public contracts.
