# Story Packet Schema v2

`story_packet.v2` is the deterministic Story-session bootstrap for one exact
accepted delivery chain.

Its ordered root is `schema_version`, `packet_kind`, `metadata`, `lineage`,
`context`, `evidence`, and `work`. The discriminator pair is
`story_packet.v2` / `story`.

The metadata, lineage, context, and evidence rules are identical to Task Packet
v3 except Story lineage has no Task member. Evidence resolves the historical
accepted Specification by exact version and hash; a newer current Specification
is used only to classify the pin as `current` or `superseded`, never as a
content substitute.

`work` is ordered `story`, then `tasks`. The Story contains accepted title,
statement, persona, ordered acceptance criteria, and current operational
status/points/rank. Tasks preserve accepted Sprint-plan/materialization order
and each contains exact canonical `task_metadata.v2`.

The packet is byte-deterministic for the same durable state. It has no wall-clock
field and no compiled rule compatibility layer. Human rendering hides database
identities, hashes, fingerprints, and repeated-instance keys. Agent rendering
contains only execution-relevant domain evidence.

Canonical render flavors are exactly `human` and `agent`. Old schemas and flavor
aliases fail with the closed packet errors. Omitted flavor returns the exact
seven-key packet. A flavored read returns a separate closed view containing
`packet` and `render`; it never appends presentation text to the canonical root.
An explicitly empty flavor is unsupported.

Before rendering, the shared canonical validator checks the complete closed
current schema, nested domain contracts, canonical hashes, and derived packet
identity. Missing or unknown fields, malformed values, and non-finite numbers
fail closed instead of becoming blank presentation content.
