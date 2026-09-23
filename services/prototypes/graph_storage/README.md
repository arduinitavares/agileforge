# Throwaway graph storage prototype

Question: would migrating AgileForge to a graph database fix #188, simplify
dependency and relationship analysis, or improve its performance?

Branch: `alex/prototype-graph-storage`, based on `85d0ce73`. Keep this experiment
out of master. No application modules, schema, dependencies, profiles, or data
are changed. The original question concerns architecture; this is not a migration
implementation or a complete design for fixing #188.

Open **demo.html** directly in a browser. It is one self-contained file with
guided walkthroughs, free-play actions, actual recorded measurements and query
source. It makes no network requests. Its interactive lifecycle is explicitly
simulated; the embedded measurements come from the real database experiment.

Read **VERDICT.md** for the answer and limits. **results.json** is the raw result.
**compare.py** contains the complete experiment, including both query languages
and the independent Python traversal. **research-baseline.md** preserves the
user-supplied August investigation as historical input.

## Run the actual comparison again

Run these commands in WSL/Linux from this directory. Docker Compose and the
existing local AgileForge image are required. All Python execution uses uv in a
Linux container. No native Python installation, provider credentials, live
AgileForge profile, or external graph service is used.

```sh
docker compose up -d graph
docker compose run --rm --no-deps probe > results.json
docker compose down
```

Only this dedicated Compose project is addressed. Neo4j is pinned by image
digest. The runner uses `agileforge-production:local` only as an existing Python/uv
toolbox, overrides the application entrypoint, and imports no application code.
The evidence records its original build and image identity. Reusing a different
local toolbox image may change Python and SQLite versions; the result records them.

Neo4j has no published ports, no host credentials and no production volumes.
The network is internal; authentication is disabled only on this isolated scratch
network. Its database lives in disposable container tmpfs. SQLite is a scratch
file in the runner's tmpfs. Removing these containers discards experiment data.
The runner mounts only its single script, read-only, outside `/workspace`.

Rerunning writes new JSON but does not automatically rewrite the standalone HTML.
To refresh the demo, replace the contents of its `benchmark-data` JSON script
with the new results. Do not treat embedded measurements as live measurements.

For a local preview instead of double-click:

```sh
docker compose --profile preview up -d preview
# Open http://127.0.0.1:8877/demo.html
docker compose --profile preview down
```

## What is compared

1. Indexed SQLite node/edge tables queried using joins and recursive CTEs.
2. A Python adjacency projection rebuilt from those SQLite tables.
3. Neo4j Community 2026.09.0 with typed relationships and Cypher queries.

The synthetic fixtures have 48, 480 and 4,800 original Stories, plus replacement
Stories, Requirements, Tasks and File targets. Groups of 12 Stories have tree
dependencies and a diamond; group roots form a connected binary tree. Paths stay
below the explicit 32-hop Cypher bound. These are recorded relationships, including
historical ones. Current planning eligibility is a separate policy.

Five queries cover downstream dependency closure, requirement-to-Story/Task/File
impact, all simple blocking paths between a pair, shared declared file targets,
and dependencies on superseded Stories. The fixture includes shared files and
obsolete prerequisites. Shared declared targets indicate possible conflicts;
they do not prove concurrent writes or create durable execution claims.

Every result is compared across all three implementations, including exact rows
and ordering. The script stops on disagreement. These checks answer the prototype
question; no production test suite or new test files were added.

Each query has one warm-up and seven measured calls. Raw samples, medians, fixture
hashes, result hashes, counts, sample rows, query source and engine versions are
retained. Projection rebuild includes reading both SQLite tables. Neo4j client
timings include a new local HTTP connection and JSON transfer/decoding on every
call; SQLite and Python timings are in-process. Do not interpret their ratio as
an engine-only speed comparison. Imports and projection builds are reported
separately. Readiness wait is not a cold-start benchmark.

The lifecycle probe replaces A with C in both actual stores, verifies that the
old B→A edge survives, then applies the same explicit review policy. It retains
the historical review and pinned snapshot only in Python memory for this small
experiment. Durable approval storage, optimistic concurrency, crash recovery,
active-Sprint guards and actual schema migration are outside this experiment.

## Sources

- Existing AgileForge #188 reproduction at baseline 85d0ce73 is preserved as
  `issue188-baseline.json` and `issue188-reproduction.py`.
- `models/core.py`: Story dependency edge table.
- `services/story_dependencies.py`: selected-scope fingerprint and active endpoint checks.
- `workflow/definitions/planning.py`: dependency review and Sprint lifecycle locks.
- `workflow/execution_integrity.py`: pinned execution dependency snapshots.
- [Cypher variable-length paths](https://neo4j.com/docs/cypher-manual/25/patterns/variable-length-paths/)
- [Neo4j Query API](https://neo4j.com/docs/query-api/current/query/)
- [Neo4j Docker setup](https://neo4j.com/docs/operations-manual/current/docker/introduction/)
- [SQLite recursive graph queries](https://www.sqlite.org/lang_with.html#queries_against_a_graph)

Built using the user's `prototype` skill. The HTML is illustrative and throwaway;
an accepted design would need a separate production implementation and review.
