# Dashboard refresh performance (#268)

Measured on 2026-09-23 against baseline
`85d0ce735fa1fdbc4d4efaf56c105158e775caad` and the changes on
`alex/issue-268-dashboard-refresh`.

## Changes

The dashboard obtains its 16 initial projections in one request. It captures the
injected clock before reading facts, then copies SQLite into a request-owned,
read-only in-memory database. The live connection returns to its pool as soon as
the copy finishes. The existing projection and review-selection code loads one
fact snapshot, evaluates one workflow position, and checks durable rows, candidate
identities, and lineage against the copy. This lets writers commit while dashboard
projections run, including with SQLite's default rollback journal.

The copy is discarded after the projections finish. Live host capability checks
then use the original application. They can load additional facts; they are not
cached with the dashboard view. Standalone and bundled position reads both retain
the evaluation time captured before fact loading, including across lease expiry.

SQLite backup progress has a one-second cooperative deadline. A busy backup step
can wait for the existing database busy timeout before the deadline is checked;
this does not impose a one-second limit on pool checkout or the whole request.
A capture timeout returns HTTP 503 with `Retry-After: 1`, without partial panels.
Other database failures propagate normally. The implementation does not change
the database's journal mode or writer timeout.

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

The accepted trial's complete browser reloads took **5.067, 5.158, and 6.018 seconds**
with no test or probe containers running. Every run displayed the expected project, confirmed
the refresh, and had no dashboard error. The first run followed a benchmark
process restart. This is about a 96% reduction from the measured baseline; the
five-second working target was approached but not consistently met. These are
copied-profile results, not measurements of a production deployment.

| Accepted trial sample | Dashboard API | Task inventory API | Complete browser reload |
| --- | ---: | ---: | ---: |
| First after restart | 3.634 s | 0.972 s | 5.067 s |
| Warm repeat 1 | 3.577 s | 1.126 s | 5.158 s |
| Warm repeat 2 | 4.473 s | 1.014 s | 6.018 s |

### Review follow-up on 2026-09-24

The read-only-copy and clock corrections preserved all 16 standalone statuses
and bodies on the same logical profile data. The bundle still used two snapshot
loads including live capability checks, one graph evaluation, and 2,088 SQL
statements. SQL counts include the copied engine and exclude low-level SQLite
backup page operations. The copy held the live connection for 13 ms in this run;
later live capability checks acquired their own connections.

The profile database occupied about 20 MB. Each overlapping dashboard request
owns a separate temporary in-memory copy. Idle complete browser reloads took
**5.149, 5.209, and 5.492 seconds**, including Task inventory. An additional run
overlapped a read-only database digest and took 5.710 seconds; it is retained in
the evidence but excluded from the idle samples. All four confirmed the expected
project and Sprint without dashboard errors. These measurements remain within
the accepted trial's range; they do not establish a consistent sub-five-second
refresh.

Private timing JSON, call counts, cProfile files, source export, and probe scripts
are retained with the task's local evidence. No profile contents or source
documents are included in this repository.

## Regression checks

`tests/adapters/test_api_dashboard_bundle.py` checks projection parity on fresh
and active-Sprint fixtures, one primary snapshot/evaluation, freshness across
requests, overlapping requests, and SQLite read consistency during concurrent
writes with both DELETE and WAL journals. They also cover the evaluation timestamp
across a lease boundary and release of the live connection before capability
checks. Private copies reject writes and close on normal and exceptional exits.
An exclusive-lock regression checks HTTP 503, source-pool reuse, and recovery
after the lock is released; a watchdog prevents an unbounded test hang. A
structural adapter test verifies that snapshot evaluation does not require the
concrete domain class. Unexpected errors in optional sections remain local to
those sections and return a generic error without exposing exception details.

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

The checks above describe validation completed with the refresh measurements.
The full release gate, package installation checks, and installed-container
startup rehearsals are separate from those measurements.

For the review follow-up, 400 Linux API and graph tests passed, including all
14 dashboard cases. Ruff, annotations, Ty, and Bandit passed on the changed Python
files. The timeout case passed again after its watchdog cleanup was refined.
Independent review found no remaining production issue. Hosted CI for the final
commit is a separate release check.

The owner tested the copied PIDExtract dashboard on 2026-09-23 and approved the
refresh improvement. This records browser acceptance; release checks remain a
separate gate.
