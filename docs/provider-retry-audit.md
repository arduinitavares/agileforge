# Provider retry audit lookup indexes

Provider tries remain in `workflow_events`. Three nonunique partial indexes make
action/call ownership checks and local reconstruction use identity seeks instead
of scanning provider history. No table, column, enum storage, or audit-row format
changes. Global identity checks still include all historical events and NULL
owners. Local damaged JSON still blocks audit operations. Append validation and
insertion retain their independently committed `BEGIN IMMEDIATE` transaction.

The indexes add disk space and safe JSON-expression work to provider-event writes.
Installing them requires a one-time build over existing events while startup holds
SQLite's writer reservation. Local reconstruction still reads at most twenty-one
call rows; this limit detects unsupported history rather than hiding old evidence.

## Installation and accepted baselines

`ensure_business_db_ready` is the database DDL installation primitive. It validates the existing
exact structural manifests without changing their table, column, unique, foreign
key, or CHECK rules. The accepted chain is:

| Existing database | Atomic startup result |
| --- | --- |
| Empty | Current tables and all three indexes |
| Exact frozen pre-retry schema, no owned indexes | Nine retry tables and all three indexes |
| Exact prior-current schema, no owned indexes | Only the three indexes |
| Exact current schema, all three canonical indexes | Verification only; no DDL |

The independently frozen prior-current baseline consists of
`tests/fixtures/issue_260/pre_retry_business_schema_da3dbf63.sql` plus
`tests/fixtures/issue_230/prior_current_retry_additions_8b4ee1f.sql`, captured before
the index change at `8b4ee1fac488c9c93e1733a57ce7ce1b32af856f`. The latter contains
only the nine retry tables and their existing indexes.

Partial owned sets, changed definitions, renamed/case-changed owned names, extra
objects in the `ix_workflow_events_provider_` namespace, and unknown structural
baselines fail before DDL. A pre-retry schema with owned indexes is unsupported.
Successful installation is atomic, so startup does not repair partial states.
Failure rolls back all DDL from that attempt, including any retry tables.

Installed production profiles require explicit maintenance before normal startup
can use a registered prior schema. Build the reviewed image, stop all writers,
run `docker compose --project-name "$project_name" run --rm production upgrade --profile default --json`, then
start production. The upgrade retains a verified full pre-upgrade bundle and
durable journal before running the registered migration steps. It publishes the
expected schema hash only after verification. A registered prior profile remains
backupable.
See the [production upgrade runbook](linux-containers.md#upgrade-an-existing-production-profile)
for exact evidence paths, interrupted-upgrade recovery, and model-marker handling.
Production restore migrates only the installed copy of an exact registered
historical bundle; its original source bytes and provenance remain rollback
evidence.

The verifier compares canonical stored DDL exactly, preserving string-literal case,
and checks indexed key order/collation with `PRAGMA index_xinfo`. SQLAlchemy's
structural reflection does not represent expression indexes. Only its specific
warnings for these two reviewed expression-index names are suppressed during
structural inspection; other reflection warnings remain visible.

An explicitly supplied audit engine is checked read-only on its repository's first
provider operation. Missing or changed indexes become a local `ProviderAuditError`
before audit queries or provider sends. The repository never installs indexes.
It remembers successful readiness for that instance; schema maintenance therefore
requires quiesced users and fresh repository instances after reopening.

## Canonical owned definitions

These statements are the canonical stored definitions. The trusted JSON paths,
CASE constants, and event names also appear literally in prepared lookup SQL;
caller/project/action/call values remain bound parameters.

```sql
CREATE INDEX ix_workflow_events_provider_action ON workflow_events (json_extract(CASE WHEN json_valid(event_metadata) = 1 THEN event_metadata ELSE '{}' END, '$.action_id'), project_id, event_id) WHERE event_type IN ('PROVIDER_TRY_STARTED', 'PROVIDER_TRY_FINISHED');
CREATE INDEX ix_workflow_events_provider_call ON workflow_events (json_extract(CASE WHEN json_valid(event_metadata) = 1 THEN event_metadata ELSE '{}' END, '$.call_id'), project_id, event_id) WHERE event_type IN ('PROVIDER_TRY_STARTED', 'PROVIDER_TRY_FINISHED');
CREATE INDEX ix_workflow_events_provider_invalid ON workflow_events (project_id, event_id) WHERE event_type IN ('PROVIDER_TRY_STARTED', 'PROVIDER_TRY_FINISHED') AND coalesce(json_valid(event_metadata), 0) != 1;
```

## Production rollback and future schema releases

To roll back this production schema release, stop every writer and select the
prior reviewed image. Restore the full verified pre-upgrade bundle into a fresh
profile, validate it with that image, and select the restored profile before
restarting. Retain the original profile and rollback evidence. Manual index drops
do not preserve the production manifest contract. Saved model-only recovery pairs
also belong to their original schema release; crossing that boundary requires
the full bundle and prior image.

The finite production registry lives in `cli/production_schema_upgrade.py`.
Its `master-b3a4fb4` and `issue230-provider-indexes` releases independently pin
both frozen-fixture and SQLAlchemy DDL encodings, with exact source/target hash
pairs. Every `SchemaTransition` also owns a typed `migration(Connection) -> None`
callable. Production resolves the exact ordered path before one `BEGIN IMMEDIATE`,
rechecks the source, and verifies each step's complete hash and registered
structural fingerprint through that same connection before continuing. It checks
the final current manifest before committing. Wrong intermediate output or a
callable failure rolls back the whole chain, retaining the pending journal and
verified pre-upgrade bundle.

These are trusted internal callables: use the supplied connection for all DDL and
DML, open no separate connection, and never commit, roll back, or create a
savepoint. A narrow SQLite transaction-boundary guard rejects these operations
while a callable runs; the coordinator clears it before its own rollback or
commit. Historical steps must use their frozen release definitions. In
particular, the issue230 step installs only its frozen canonical provider indexes
and never dispatches through `ensure_business_db_ready` or changing current
metadata. Fresh and development bootstrap keep their existing readiness and
development-only PRE_RETRY behavior.

For #300, preserve every historical release, hash, and transition. Append exactly
one `SchemaRelease` with independently frozen complete hashes for all supported
DDL encodings and its own structural fingerprint. Append exactly one
`SchemaTransition` from the previous current release, with its migration callable
and every exact source/target hash pair; then select the new `CURRENT_SCHEMA_ID`.
Update the current models and reviewed manifest for the new release, and retain
frozen fixtures for both fresh and migrated outputs. Populate
`tests/fixtures/issue_230/schema_releases.json` for every registered encoding.
The callback/source-fixture matrix automatically collects every registered
transition and exact hash pair, executes its callable from the frozen source,
and checks the declared target hash and structural fingerprint without matrix
edits for new registrations. Extend the pure registry
reachability, endpoint/uniqueness, fixture fingerprint, ordered-step, rollback,
and restore checks before acceptance. Do not extend a generic latest-schema
bootstrap to recognize production history, learn an allowed hash from the profile
being upgraded, or normalize stored DDL to make an unknown schema match.

Development profiles use `schema_source_sha256` to hash schema source files,
rather than SQLite schema rows. Their source-drift gate and checkout-local
launcher contract remain separate from production's complete database hashes.
