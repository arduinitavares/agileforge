# Graph storage experiment verdict

2026-09-23. Source baseline `85d0ce73`. Throwaway branch
`alex/prototype-graph-storage`; no production change or migration.

## Answer

Migrating storage alone does not fix #188. A graph database does make several
relationship queries more direct to express. This experiment did not establish
a performance or capability requirement to migrate AgileForge's durable store.

The useful architectural change supported by this experiment is an explicit,
typed relationship model with shared dependency/lineage queries and explicit
replacement-review policy. It can be implemented over SQLite, with a derived
Python graph where useful. Neo4j remains a credible option if interactive graph
exploration and varied relationship queries become important enough to justify
its service and migration costs. The prototype leaves that product decision open.

## Evidence

Five query families on three connected synthetic datasets returned exactly the
same ordered rows in indexed SQLite, a Python projection rebuilt from SQLite,
and Neo4j Community 2026.09.0. That is 15 three-way query comparisons. The largest
fixture has 11,700 entities and 19,799 relationships. It returns 4,799 downstream
Stories from the first Story, so the closure experiment is not limited to small
disconnected islands. Raw samples, hashes, versions and query source are in
`results.json` and are embedded in `demo.html`.

In both actual stores, marking A superseded and creating C leaves B→A intact.
The same explicit application rule can detect the obsolete endpoint in either
store. Both can change that edge after an assumed new approval. That last step
does **not** execute a real human-review transition: it demonstrates the identical
storage update after a hypothetical decision. Approval/history copies in this
probe are in memory, not durable review or Sprint persistence.

Separately, `issue188-baseline.json` records a reproduction through the real
AgileForge Story draft/acceptance and dependency-review transitions at the source
baseline. There, the selected-scope fingerprint and review remained unchanged
after the real replacement, and planning blocked with
`STORY_DEPENDENCY_EXTERNAL_INCOMPLETE`. This is stronger evidence of the current
bug than the small synthetic lifecycle simulation.

## What graph storage improves

- Dependency closure is a direct repeated relationship pattern in Cypher. The
  equivalent SQLite query explicitly carries a recursive set of affected IDs.
- Cross-artifact traversal combines typed relationships in a compact pattern.
  Its SQLite equivalent is still short and workable.
- Blocking-path explanations are expressible in both. SQLite needs a path
  accumulator and cycle check; Cypher provides path values and node predicates.
- Shared file targets and superseded endpoints are simple joins in SQLite and
  simple patterns in Cypher. Those queries offer little migration benefit alone.

These are inspectable differences in the retained query code, not a measured
developer-productivity claim. All five analyses were possible without a graph
database. An explicit graph representation is the common requirement.

## Performance interpretation

The table below reports the final run, in milliseconds. All engines ran in Linux
containers on the same WSL2 host. SQLite used an indexed scratch file in tmpfs;
Neo4j used a tmpfs database and 512 MB maximum heap plus 256 MB page cache. See
`environment.json` for image identity and limits.

| Query, largest fixture | SQLite | Python projection | Neo4j HTTP | Returned rows |
| --- | ---: | ---: | ---: | ---: |
| dependents | 7.466 | 1.844 | 26.489 | 4799 |
| requirement_impact | 0.040 | 0.007 | 3.662 | 29 |
| blocking_paths | 0.008 | 0.002 | 36.468 | 2 |
| shared_files | 5.518 | 5.425 | 31.166 | 4800 |
| obsolete_endpoints | 0.177 | 0.757 | 5.761 | 300 |

Neo4j includes HTTP connection setup, execution, result transfer and JSON decoding.
SQLite and Python calls are in-process. The Python column excludes its separately
measured rebuild. The largest rebuild took about 41 ms in this run; including a
full rebuild on every query would change the practical comparison. Query latency
also varied between exploratory runs as the JVM warmed. One warm-up and seven
calls are enough for this small feasibility experiment, not a stable comparative
engine benchmark. No storage-engine speed ranking follows from these figures.

No timing here includes a production schema migration, cold startup, provider
calls, dashboard projections, or concurrent readers/writers. Some query outputs
grow with dataset size; requirement-impact and selected path output sizes remain
small. The fixture is tree-like with diamonds, not a dense arbitrary graph. All
dependencies are below the explicit 32-hop bound. Large cyclic or dense graphs,
long chains and sustained analytics need a separate workload if they become a
product requirement.

## Scope and verification

- The browser demo was driven through stale replacement, reviewed replacement,
  explicit removal, and active-Sprint pinned-history scenarios. Actions rendered
  their changed state and illegal review during an active Sprint was rejected.
- The demonstration collapses Sprint completion/triage and omits planned-Sprint,
  retry and untriaged-completion locks. It does not claim to reproduce the whole
  production workflow.
- A focused independent review checked the #188 lifecycle claims; wording was
  corrected to distinguish assumed approval from an executed review.
- No new production tests were added, consistent with the prototype skill. The
  actual comparisons and browser checks are the verification evidence.
- No production profile, provider, schema, dependency lockfile or master file
  was modified. The experiment is preserved on its own branch.

This does not demonstrate an economical full migration. The relational prototype
already uses an explicit node/edge representation instead of querying AgileForge's
current heterogeneous schema directly. Extracting current artifacts, reviews,
metadata and exact historical scopes is work either approach still needs.

The next production decision supported by this evidence is to define the #188
replacement-review rule and the shared relationship-query boundary. A full graph
database migration remains optional; this experiment provides query examples and
repeatable scratch evidence for evaluating it as the product's analysis needs grow.
