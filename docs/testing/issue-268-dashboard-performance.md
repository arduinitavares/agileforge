# Dashboard refresh performance (#268)

Measured on 2026-09-23 against baseline
`85d0ce735fa1fdbc4d4efaf56c105158e775caad` and the changes on
`alex/issue-268-dashboard-refresh`.

## Changes

The dashboard obtains its 16 initial projections in one request. A request-owned
SQLite read transaction loads one fact snapshot and evaluates one workflow
position. The existing projection and review-selection code still checks durable
rows, candidate identities, and lineage using that transaction. Live host
capability checks run after the transaction closes, using the original application.
They can load additional facts; they are not cached with the dashboard view.

Standalone reads and mutations retain their fresh-read paths. A dashboard view
does not survive its request, and submitting an action still rechecks current
authority. Missing or conflicting facts fail the entire dashboard read; in that
case every bundled slot reports the common failure, even if an independent narrow
endpoint such as repository status could still succeed.

Three smaller changes also reduce repeated computation:

- Attempt overlays and recovery references share a business fingerprint within
  one graph evaluation. A later evaluation computes a fresh fingerprint.
- A lock prevents overlapping Pydantic snapshot dumps, which were substantially
  slower in the tested runtime. Canonical normalization and hashing are unchanged.
- Candidate-contract validation remembers only successful validation of exact
  serialized text and the expected fingerprint. It retains at most 16 entries,
  bypasses text above 256K characters, and returns freshly parsed models on every
  call. Accepted Specification registry and lineage checks still query the database.

## Measurement method

The representative profile copy contained 12 Stories, 32 Tasks, 10 Sprints, one
Specification version, 20 planning artifacts, nine dependency edges, and 38 node
attempts. The production state and workspace volumes were mounted read-only into
an isolated Linux/amd64 container. SQLite backups were served from temporary
storage, with no provider access. Production services and data were not changed.
Database and configuration state were copied; profile artifact files were not
duplicated. Both variants used the same read-only workspace and those same
capability-check conditions. This was a read-performance comparison, not a
production rollout rehearsal.

Both variants used the same Python 3.13.15 / uv 0.12.8 image dependencies, including
Pydantic 2.12.3. Baseline source was pinned to the revision above; modified source
was exported into the same runtime. Application composition was warmed before
serving. Startup, migrations, and readiness were excluded.

A loopback GET-only relay instrumented the end of `loadDashboard()` and exposed
its duration as a visible DOM value. Browser timings include rendering and the
subsequent Task inventory request. They are individual runs, not percentiles.
The working target is a warm complete refresh below five seconds on this host.

## Results

| Work in the initial dashboard read | Separate endpoints | Bundled endpoint |
| --- | ---: | ---: |
| HTTP requests | 16 | 1 |
| Fact snapshot loads, including live capability checks | 16 | 2 |
| Graph evaluations | 5 | 1 |
| SQL statements | 15,350 | 2,089 |

The request/query counts were measured on the copied profile. One additional
Task-inventory request follows either initial read. A fixed-clock comparison of
the bundle with the standalone endpoints matched every slot's status and body,
including all actions and the three unavailable-review responses. The full
workflow-position digest also matched the pre-change baseline.

The original copied-profile browser baseline was 129.067 seconds. The three
backend computation changes alone reduced an idle reload to 40.611 seconds.
An experiment serializing the separate requests took 29.153 seconds; request
scheduling alone was therefore not used as the fix.

Final complete browser reloads took **5.067, 5.158, and 6.018 seconds** with no test
or probe containers running. Every run displayed the expected project, confirmed
the refresh, and had no dashboard error. The first run followed a benchmark
process restart. This is about a 96% reduction from the measured baseline; the
five-second working target was approached but not consistently met. These are
copied-profile results, not measurements of a production deployment.

| Final sample | Dashboard API | Task inventory API | Complete browser reload |
| --- | ---: | ---: | ---: |
| First after restart | 3.634 s | 0.972 s | 5.067 s |
| Warm repeat 1 | 3.577 s | 1.126 s | 5.158 s |
| Warm repeat 2 | 4.473 s | 1.014 s | 6.018 s |

Private timing JSON, call counts, cProfile files, source export, and probe scripts
are retained with the task's local evidence. No profile contents or source
documents are included in this repository.

## Regression checks

`tests/adapters/test_api_dashboard_bundle.py` checks projection parity on fresh
and active-Sprint fixtures, one primary snapshot/evaluation, freshness across
requests, overlapping requests, SQLite read consistency during a concurrent WAL
write, and release of the read connection before capability checks. Unexpected
errors in optional sections remain local to those sections and return a generic
error without exposing exception details.

`tests/test_dashboard_bundle.mjs` checks the single request, projection ordering,
optional error semantics, and rejection of incomplete responses. The existing
frontend action-lock, retry, superseded-refresh, and reconciliation suites run
against the bundled response. The new suite is included in CI.

Fingerprint and candidate-contract regressions cover attempt/recovery decisions,
fresh fingerprints after nested changes, concurrent serialization and error
release, altered candidate bytes/fingerprints, fresh model instances, and bounded
validation reuse. Timing thresholds are not asserted in CI; the structural
request/load/evaluation counts detect recurrence without host-speed assumptions.

The Linux validation run passed 485 API and projection tests, 58 graph and
fingerprint tests, and 54 candidate-contract and accepted-Specification tests.
The final optional-error correction passed all eight bundle API tests.
The eight Node dashboard suites passed all 253 tests. Repository-wide type
checking and lint/format checks on the changed Python files also passed.
An earlier run passed 906 workflow tests before the dashboard bundle and
candidate-validation cache were added; that is supporting evidence, not a full
suite result for the final patch.

The complete CI workflow, package installation checks, and production rollout
rehearsal have not been run. The change has not been deployed.
