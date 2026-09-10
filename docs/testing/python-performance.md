# Python validation and timing

Use the checkout's locked Python 3.13.15 environment and the same `pyrepo-check`
controller pinned in `.github/workflows/ci.yml`. Run from that checkout's root.
`pyrepo-check` provisions and invokes the repository Python through uv; the full
gate remains `./agileforge-dev check` (`sh ./agileforge-dev check` in PowerShell).
Quality checks do not require an initialized operator profile. Before runtime
mutations, inspect the selected existing profile with
`./agileforge-dev info --profile <name> --json`.

## Everyday feedback

Select the owning tests, then expand to their callers when a contract changes:

```sh
# Pure graph decisions, fingerprints, ordering and guard properties.
pyrepo-check --python 3.13.15 pytest tests/workflow/test_graph_properties.py

# Database fixture ownership and canonical fingerprint compatibility.
pyrepo-check --python 3.13.15 pytest tests/test_database_fixture.py tests/test_agent_workbench_fingerprints.py tests/workflow/test_fingerprints.py

# One persisted retry lifecycle, including reloads and stale actions.
pyrepo-check --python 3.13.15 pytest tests/workflow/test_sprint_retry_execution.py::test_multistory_retries_preserve_history_reload_and_reject_stale_actions

# File-oriented checks for the changed Python files.
pyrepo-check --python 3.13.15 ruff annotations ty bandit workflow/fingerprints.py tests/conftest.py
```

These are focused selections. The pure graph file does not exercise database
transactions, API adapters, browser behavior, packaged installation or process
ownership. A changed fixture, shared hash, persistence contract or runtime
launcher needs its regression tests and the full gate. Focused coverage reports
are guidance, not a full-suite coverage result.

## Full validation

```sh
./agileforge-dev check
```

This still runs lock validation, the canonical Python quality checks and pytest,
the registered Node suites, whitespace validation and isolated distribution
verification in the existing order. Default pytest selection still excludes
`integration` and blocks external sockets. No timeout, platform marker or
required check is relaxed by the performance changes.

Run it before requesting final review or merging, after changes to shared test
fixtures or workflow authority, and when focused results leave affected callers
uncertain. Follow the separate operator acceptance checklist when real provider
or product acceptance is needed; this gate does not perform those workflows.

## Isolation and the optimization boundary

The autouse database guard is installed for every test. Its getters request the
function-scoped `engine` fixture on demand; an explicit `engine` or `session`
argument also constructs it normally. All requests in that test resolve to the
same fixture. A per-test lock also serializes concurrent first requests from
background or TestClient threads. Pure tests no longer build an unused schema.
Each requested engine
still creates a new in-memory SQLite database and the complete current schema.
No rows, engines or mutable schema snapshots are shared between tests.

The fixture's connection listener belongs to its engine. It no longer adds a
listener to the global `Engine` class for every test. Cleanup disposes the owned
in-memory pool in `finally`, including after setup failure, instead of issuing
DROP statements and disabling foreign keys. File-backed persistence, migration,
rollback and process-ownership tests keep their existing real resources.

Canonical normalization returns exact built-in JSON scalar values before
checking the more expensive datetime and container protocols. Subclasses keep
the previous dispatch path. Key-collision rejection, datetime conversion,
canonical bytes and SHA-256 inputs are unchanged. There is no cache of mutable
workflow facts or validation outcomes and no parallel pytest execution.

## Reproducing measurements

Retain the complete output in a new file for each run. Pytest reports every
setup, call and teardown duration through the repository's `--durations=0
--durations-min=0` configuration. This adds reporting without changing selection.
Sum by phase and module to see cumulative costs; the runner's separate top-ten
summary alone cannot establish where the suite spends all its time.

On Linux, for example:

```sh
/usr/bin/time -p ./agileforge-dev check > canonical-unique-run.log 2>&1
/usr/bin/time -p pyrepo-check --python 3.13.15 pytest tests/workflow/test_graph_properties.py > focused-unique-run.log 2>&1
```

Keep the exit code, commit and diff, platform and filesystem, CPU/memory,
Python/dependency/controller versions, exact command and selected node IDs.
Record whether dependencies and browser binaries are already installed. Run
one benchmark workload at a time, with fresh Python processes and fresh test
databases; compare warm dependency caches separately from environment setup.
Measure pytest time and total command wall time separately. The full gate also
performs checks and package verification outside pytest.

Timing budgets and the before/after measurements for issue #265 are recorded in
the retained validation report. They are investigation thresholds for the
measured environment, not test timeouts or a universal CI service-level promise.
The historical Windows runs of 5,547.66 and 6,106.09 pytest seconds used different
revisions and are not a controlled before/after comparison. Linux development
and CI measurements must remain separate from them. Container and platform
cutover work belongs to issue #263.
