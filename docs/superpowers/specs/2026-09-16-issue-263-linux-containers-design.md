# Issue 263: Linux container runtime design

Status: approved by the user on 2026-09-16. Implementation is in progress;
validation results are recorded separately from this design.

Issue: <https://github.com/arduinitavares/agileforge/issues/263>

Source baseline: `7e193694c1da0746f257e3b48ce63b326e709cca`.

## Decision and scope

Use one pinned Docker build for Linux/amd64 development, testing, and packaged
production. Use Docker Compose for local lifecycle and named Linux volumes for
repositories and durable state. Keep uv, SQLite, existing workflow authority,
accepted artifacts, source identity checks, and separate business/trace stores.

The host editor or agent controller may run on macOS or Windows. Application
commands, Git worktree operations, tests, and development-agent shell commands
run inside the Linux development container. Do not bind a macOS/Windows source
directory into the supported workspace. Linux/arm64 support is outside this
initial delivery; an emulated amd64 run is useful functional evidence but is
not a native amd64 performance baseline.

Deliver the container workflow, synthetic restore rehearsal, tests, and operator
runbook first. Live-profile inspection or copies, provider-backed acceptance,
deployment, active data transfer, and installation cutover require separate
operator approval. Keep #263 open until cutover and final acceptance succeed.

## Alternatives considered

1. **Docker build and Compose with Linux named volumes, recommended.** Small
   runtime contract, shared local/CI images, stable paths, and an explicit
   production target. The host agent needs a documented `docker compose exec`
   command/edit transport.
2. **A mandatory editor-specific dev container.** Convenient for one editor,
   but does not solve production, transfer, or agent execution. An editor can
   attach to the recommended development service without making it a dependency.
3. **A full Linux VM as the application packaging unit.** Linux filesystem
   semantics work, but build identity and lifecycle diverge from the requested
   container CI/production contract. Do not add another packaging system.

## Current evidence

- CI runs a Linux full gate, a Linux Node job, a macOS lifecycle smoke, a Windows
  full gate, and two Windows security/runtime jobs. There is no tracked Docker,
  Compose, or dev-container build definition.
- `cli/dev_checks.py` preserves the ordered lock, Python quality, Node,
  whitespace, and isolated distribution checks. Default pytest excludes
  `integration`; provider-backed tests cannot become an implicit migration gate.
- `cli/dev_profiles.py` validates checkout ownership and distinct absolute
  business/trace paths. A moved manifest is not a valid relocated profile.
- `cli/dev_server.py` binds to `127.0.0.1`. Its readiness contract checks source
  root, commit, database paths, PID, and launch nonce. Docker port publishing
  needs an explicit container listener reachable across the network namespace.
- `api.py::_runtime_provenance` reports `installed:agileforge@<version>` outside
  a Git checkout. The production image needs immutable source revision identity.
- `cli/dev_server.py::stop_ui` terminates one tracked child. Container lifecycle
  tests must establish complete process ownership and cleanup, including a
  parent exiting before its descendants.
- The inspected workstation has Docker 29.8.0 on a Linux ARM64 VM, 24 visible
  CPUs and 8,316,473,344 bytes of memory. It is not the native amd64 CI baseline.
- CI run `35089329884` targets this source SHA. Both full gates were still
  running when inspected; no successful baseline result is claimed here.

## Build and dependency contract

Use Ubuntu 24.04 for the initial amd64 image family, with an immutable base
digest. Pin uv to the repository's current CI version, `0.12.8`, and Python to
`3.13.15`. Keep the pyrepo-check controller pin
`ac41bbe8e8588b8f4232979c47d26be37d78d413` separate from the project's locked uv
environment. Resolve and record an exact Node 24 patch version, base/image
digests, and OS package snapshot during implementation; commit those values
before claiming reproducibility. Do not use floating `latest` inputs.

One Dockerfile supplies shared runtime dependencies, a development target, a
test target with Chromium and required system libraries, and a production
target containing the built wheel installed against `uv.lock`. Production must
work outside a source checkout and without editable imports. Keep current
declared dependencies; reorganizing dependency groups is separate work.

Build from an explicit source export. Exclude `.env*`, `.agileforge`, databases,
backups, logs, virtual environments, caches, `.git` credentials/configuration,
and local artifacts from the build context. Test exclusions with synthetic
canary files and inspect final image contents/layers for those canaries.
Record build revision and source/lock hashes in a read-only packaged manifest
and matching image labels. Production/acceptance builds require a clean,
committed source revision. A development image's toolchain identity is distinct
from the mutable workspace's current Git revision.

## Storage and agent workflow

| Container path | Ownership and purpose |
| --- | --- |
| `/workspace/repos/agileforge` | Normal development Git clone in a named Linux volume |
| `/workspace/worktrees/<task>` | Linked worktrees in the same volume, with their own `.agileforge/dev/profiles/<name>` directories |
| `/workspace/repos/targets/<name>` | Registered target repositories and their Git common directories |
| `/cache` | Disposable uv, tool, and browser caches; never authoritative state |
| `/opt/agileforge` | Read-only packaged build metadata and application environment |
| `/var/lib/agileforge/profiles/<name>` | Production profile metadata, distinct databases, configuration, and artifacts in a durable volume |
| `/run/secrets/agileforge` | Explicit runtime secret file, never copied into durable state or images |

Use a non-root application user with fixed documented UID/GID. Provision owned
volumes explicitly; do not solve permissions with world-writable files. Git
common directories, worktrees, and target repositories must remain reachable
under the same container paths after recreation. A worktree profile stays below
that worktree's real root; no external symlink workaround for ownership checks.

Bootstrap a fresh development clone into the volume. A local candidate can be
transported as a reviewed Git bundle/patch; do not copy the active checkout's
profiles or secrets. Document agent commands through `docker compose exec`,
including a file edit through stdin, Git diff, linked-worktree creation, and
worktree-owned `./agileforge-dev` invocations. A host editor can attach remotely.
Verify those exact operations rather than calling an interactive shell proof
that an agent workflow works.

## Runtime, identity, and secrets

Development continues through each checkout's `./agileforge-dev`. Add an
explicit container listener option while retaining a separate loopback address
for in-container readiness probes. Compose publishes only
`127.0.0.1:<host-port>:<container-port>`. Use a private application network and
one explicit port per UI instance; do not use host networking. Test the host
publication configuration as well as the in-container listener.

Production starts the installed package through a small foreground entrypoint.
Give installed profiles an explicit deployment-root ownership contract and
build manifest, rather than pretending the wheel installation is a Git
checkout or weakening `RuntimeProfile` validation. Reuse existing database,
schema, configuration-hash, and secret-file validation where their contracts
match. Production state metadata records a durable state ID, schema and
model-config hashes, distinct owned paths, and creation/restore provenance.
Keep immutable build revision and Python/uv versions in the packaged build
manifest; an image replacement must not silently change the durable state ID.

Define versioned `BuildIdentity` at `/opt/agileforge/build.json` and
`ProductionStateManifest` at
`/var/lib/agileforge/profiles/<name>/runtime.json`. The build record is owned by
root and not writable by the runtime user. The state record is a regular file
under the selected profile's owned real directory. Reject symlinks, aliased
database paths, missing records, unknown versions, unsupported schemas, and
configuration-hash mismatches before initializing the application. Entrypoint
and API share one validated loader. Readiness derives revision from the build
record and state ID/paths from the state record, never from caller-supplied
identity environment variables. Compatible image replacement updates build
identity only; incompatible state fails closed without automatic migration.

Every launch creates a fresh nonce and owns the serving process tree. Verify
readiness against expected build or checkout revision, state ID for production,
state paths, PID, and nonce before reporting ready. PID/nonce state is transient.
Compose uses an init process; the supervisor forwards TERM/INT, waits a finite time, then
terminates the owned process group if needed. Test descendants, early startup
failure, reload where supported, occupied ports, and foreign readiness replies.
An init process alone is not proof of complete cleanup.

Credentials are optional for provider-free verification and explicitly supplied
for authorized provider work. Reuse secure file opening and redaction; do not
inherit ambient host credentials, load the checkout `.env`, or accept secrets
as Docker build arguments. Verify non-root ownership and mode constraints for
the documented secret mount. Logs and diagnostics report credential presence
only, plus non-secret provenance.

## Backup, restore, and relocation

Support current-schema profiles only. Refuse missing/partial state, unknown
schemas, unsafe paths, and nonempty restore destinations. Never fall back to a
fresh empty profile when a requested restore fails.

Introduce a maintenance fence for each durable workspace/deployment root.
Every supported service, CLI, provider worker, and development shell acquires
a shared lifetime lock before any state access. Backup/restore acquires an
exclusive lock, after stopping the corresponding services and their workers.
The lock covers repositories and artifacts as well as both databases. Define
a fixed acquisition order by canonical root when several roots participate.
Startup fails while maintenance owns the lock; the backup worker holds it
until capture and verification finish. Check for other running containers with
writable mounts of the selected volumes and refuse export if one exists.
Raw Docker mounts and direct database writers outside this contract are
unsupported; known bypasses or uncertain writer ownership block backup.

The pre-cutover compatibility tool must cover native source entrypoints too;
existing processes that predate the fence must be stopped and verified before
export. This source-side preparation is an explicitly approved part of active
cutover, not an action performed during synthetic implementation. Record lock
ownership and stopped services in the report. Use SQLite backup APIs and
integrity/foreign-key checks with the fence held across the whole operation;
independent live backups cannot establish a consistent business/trace pair.

The backup contains the business and trace databases, immutable and required
mutable artifacts, model configuration, repository/common-directory/worktree
content including dirty/untracked observations, and a versioned inventory with
file and logical-record hashes. Omit runtime PIDs/nonces, environments, caches,
and credentials. Keep repository contents out of diagnostic logs. Reject
archive traversal and unsafe special files; preserve Git modes and safe
in-repository symlinks without dereferencing them outside the selected roots.

Use inventory format `agileforge.transfer.v1`. Record the complete business and
trace SQLite schema definitions and every durable table/row, including currently
unlinked trace sessions. Canonical row encoding tags SQLite null, integer, real,
text, and blob values, preserves text/blob bytes, and includes column order.
Hash rows and their sorted multiset per table, retaining duplicate counts and
declared primary keys; exclude only SQLite's transient lock/WAL machinery,
not application columns or historical absolute paths. Include sequence state,
raw artifact byte hashes/sizes, all ADK session IDs, and the observed
session-to-attempt link set. Store runtime manifests separately as provenance;
their newly provisioned destination fields are the explicit equality exclusion.

Publish completed backup bundles atomically; interrupted export must not appear
as a usable backup. Restore under the exclusive fence into an undiscoverable
staging directory. Verify inventory, schemas, databases, repository identity,
and immutable evidence there first. Reject a pre-existing final destination.
For development, require the destination checkout's existing tracked
`config/models.yaml` hash to match the backup inventory. Do not overwrite it.
Then call `prepare_profile_record`, install only validated databases and
artifacts into its newly reserved paths, and call `finalize_profile_record`
last. The empty directories created by reservation
are expected, not a pre-existing destination. Production uses the same protocol:
reserve a new owned root, install validated databases, artifacts, and the
manifest-owned state-local model configuration, and atomically publish
`runtime.json` last. Readers require that final manifest. A crash leaves an
unpublished reservation which requires explicit recovery and cannot be silently
adopted or treated as empty state. Reprovision all runtime paths for the actual
Linux checkout or installed build. Run `info --json` before subsequent
development runtime/state mutations.

Keep same-container restorations at the recorded canonical paths. For a
path-changing native-to-Linux transfer, map every repository/common directory
and linked worktree explicitly. Restore the complete Git object/index/worktree
content, then use Git's supported worktree repair from the restored main clone
with the mapped linked-worktree paths. Reconstruct through supported Git
operations if repair cannot represent the source layout; fail if identity or
dirty content cannot be preserved. Before application reattachment, verify each
root/common directory, HEAD, branch, index, dirty/untracked file hashes and modes,
and approved remotes against the source inventory. Test moved main clones,
moved linked worktrees, and both moved together. Git administrative pointer
changes are allowed relocation metadata; accepted application evidence is not.

When repository paths change, use guarded `attach_repository` operations to
append new bindings. `refresh_repository` reprobes the old path and is not a
relocation operation. Retain old bindings and accepted evidence bytes.
Do not patch historical SQL rows or regenerate accepted artifacts. Preserve
project IDs, acceptance, idempotency records, workflow history, active/completed
Sprint lineage, and pinned plan versions. Require fresh explicit source capture
for future authoring against a replacement binding. Compare ADK session IDs and
their observed links to business `attempt_fingerprint` values; some business
attempts legitimately have no trace. Preserve that observed link set rather than
inventing a one-trace-per-attempt requirement.

Restore rehearsal uses synthetic current-schema projects with an accepted
Specification, historical repository bindings, active/completed Sprint history,
idempotency records, and linked trace evidence. Compare a logical inventory
before and after, plus exact accepted bytes/hashes. Include corruption, aliasing,
symlink escape, unsupported schema, destination conflict, and interrupted
publication cases. Restoration must preserve
the inventory exactly; subsequent guarded reattachment must leave all prior
rows unchanged except the project active-binding pointer. Compare appended
binding, receipt, and audit rows to the exact outcome of the existing attach
command for the inventoried project and idempotency key. No other row deletion,
update, or insertion is an allowed relocation delta. Verify restored projects
remain readable and safely operable. Whole SQLite-file byte equality is not a
restore criterion. A second backup/restore and container replacement must keep
the same durable identities.

Rollback retains the stopped original installation and an independently verified
backup. Before destination writes are enabled, rollback means discarding the
candidate destination and resuming the fenced original after verification.
Destination-only transfer/reattachment changes made during offline preparation
are disposable. After the first live destination write, this rollback option
expires: stop the destination and perform forward recovery, or obtain approval
for a separately designed reverse transfer. There is no automatic SQL-history
merge and no restarting stale source data. Never run both installations' writers
simultaneously.

## Validation and staged cutover

1. Capture a native Linux/amd64 baseline with exact source revision, runner and
   filesystem, resources, pins, command, test selection, and cache conditions.
   Record stage times, total elapsed time, and runner consumption separately.
2. Build all targets and prove the development-agent workflow, source capture,
   same-name worktree profile isolation, and stable Git metadata after restart.
3. Run the unchanged canonical gate inside the test image. Confirm browser E2E
   collection/execution and the seven Node suites. Preserve Linux POSIX evidence,
   source-registration, stale-action, secrets, socket-marker, and lifecycle tests.
   Run the isolated distribution verifier and packaged production lifecycle test.
4. Verify host-loopback publication, fresh startup identity, complete process
   cleanup, and runtime-only secrets. Recreate containers and rehearse backup,
   restore, and path relocation using synthetic durable projects.
5. Deliver measured synthetic evidence and an executable operator runbook.
   First request permission to inventory real profiles/repositories read-only.
   Only after that approved inventory, request approval for the exact copy and
   cutover set, including paths, dirty files, secret exclusions, owner IDs,
   backup destination/retention, and recovery image. Keep native support active.
6. Execute approved cutover in order: establish source maintenance fencing and
   stop/verify writers; create and verify the final paired backup; stage the
   Linux restore and Git repair; append guarded replacement bindings offline;
   validate with providers disabled; present the resulting inventory and request
   final switch approval; activate the destination and enable live writes. The
   original remains stopped. Do not initialize the normal API just to perform
   read-only validation if its startup would mutate state.
7. After the approved cutover, remove Windows-only adapters/tests and macOS/
   Windows CI jobs. Move the real launcher smoke to Linux containers. Add early
   unsupported-platform rejection at executable/API entrypoints before any
   application state initialization or provider access. Remove obsolete native
   setup guidance and rerun the retained container gate.

The Windows evidence and source adapters can retire after cutover; their POSIX
security guarantees cannot. Retain generic launcher/secret/profile tests and
the isolated wheel/sdist verifier. Remove Windows interpreter redirector and
native-handle assertions only with an explicit inventory. Do not add broad
skips, reduce coverage, relax timeouts, or fix #265 performance in this migration.

Report Linux test time, build time, startup time, and removed platform runner
work independently. A successful ARM-host emulation run does not satisfy the
native timing comparison. CI changes remain local until publication is
authorized; an unrun CI configuration is not validation evidence.

## Implementation areas and completion boundary

Expected files include the Dockerfile/Compose/build-context configuration,
container bootstrap and verification scripts, `cli/dev_server.py`,
`cli/dev_main.py`, `cli/dev_profiles.py`, a small installed-runtime/transfer
module, `api.py`, packaging metadata, targeted tests, CI, and operator docs.
Relocation may require a bounded addition at the existing repository-binding
application service. Keep domain-history data transformations out of scope.

Before operator approval, report **ready for cutover** only if every build,
synthetic validation, and restore criterion above has evidence. Report remaining
validation blockers explicitly. Do not report the entire issue fixed or close
it while native removal, live cutover, or final acceptance remains incomplete.

## Documentation sources

- [Docker pinned build guidance](https://docs.docker.com/build/building/best-practices/)
- [Docker Compose service reference](https://docs.docker.com/reference/compose-file/services/)
- [Docker volume persistence and backup](https://docs.docker.com/engine/storage/volumes/)
- [Docker port publication](https://docs.docker.com/engine/network/port-publishing/)
- [Python SQLite backup API](https://docs.python.org/3.13/library/sqlite3.html#sqlite3.Connection.backup)
- [uv Docker integration](https://docs.astral.sh/uv/guides/integration/docker/)
- [Git worktree repair](https://git-scm.com/docs/git-worktree#Documentation/git-worktree.txt-repairltpathgt)
