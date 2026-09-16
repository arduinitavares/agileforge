# Linux container runtime implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the approved issue #263 container workflow and verified synthetic transfer tooling, ready for the separately approved operator cutover.

**Architecture:** A pinned Docker build supplies development/test and installed production targets. Workspaces and production profiles use separate Linux volumes. Explicit runtime identity, process ownership, maintenance fencing, and staged transfer publication preserve existing authority and durable history.

**Tech stack:** Ubuntu 24.04 Linux/amd64, Python 3.13.15, uv 0.12.8, Node 24, SQLite, Docker Compose, existing pytest/Playwright/pyrepo-check.

**Spec:** `docs/superpowers/specs/2026-09-16-issue-263-linux-containers-design.md`

**Delivery status:** Implementation, independent review, synthetic rehearsals,
and the complete Linux gate passed at `3edd7bb051ddd5468af65239f5da657725e33757`.
See [validation evidence](../../testing/issue-263-container-evidence.md).
Native amd64 CI has not run for this unpushed candidate; only the pre-change
baseline is available. Live cutover, native retirement, and issue closure remain
outside this approved delivery phase.

## Global constraints

- Base revision `7e193694c1da0746f257e3b48ce63b326e709cca`; task branch `dev/issue-263-linux-containers`.
- Current-schema synthetic state only; never inspect protected profiles or credentials.
- No provider calls, push, merge, deployment, active-data transfer, or native-platform removal in the pre-cutover delivery.
- Linux/amd64 is the supported target. Record ARM-host emulation separately from native CI timings.
- Preserve uv lock, Python 3.13.15, controller commit `ac41bbe8e8588b8f4232979c47d26be37d78d413`, test selection, coverage thresholds, and timeouts.
- Run application and verification commands in the Linux container once bootstrap completes. Bootstrap Docker files/control commands may run on the host. Copy source exports only, never host state.
- Development commands use that checkout's `./agileforge-dev`; inspect `info --json` before runtime/state mutation.
- Tests precede behavioral code. Record red and green evidence. Do not duplicate implementation logic in expected values.
- Workers own disjoint files. Shared-entrypoint integration belongs to Task 4; no worker reverts another worker's changes.

## File responsibilities

| Area | Files | Owner |
| --- | --- | --- |
| Build and workflow | `Dockerfile`, `.dockerignore`, `compose.yaml`, `containers/pins.json`, `scripts/container.py`, `tests/test_container_build.py` | Task 1 |
| Maintenance and transfer primitives | `utils/runtime_fence.py`, `cli/state_transfer.py`, `tests/container_runtime/test_runtime_fence.py`, `tests/container_runtime/test_state_transfer.py` | Task 2 |
| Installed state and runtime | `cli/production_state.py`, `cli/container_runtime.py`, `utils/build_identity.py`, `tests/container_runtime/test_production_state.py`, `tests/container_runtime/test_container_runtime.py` | Task 3 |
| Runtime integration | `cli/dev_main.py`, `cli/dev_server.py`, `cli/main.py`, `api.py`, `utils/runtime_controls.py`, related existing tests | Task 4 |
| Rehearsal and gate | `scripts/verify_container.py`, `tests/container_runtime/test_relocation.py`, `.github/workflows/ci.yml` | Task 5 |
| Operator documentation/evidence | `docs/linux-containers.md`, `README.md`, `docs/testing/issue-263-container-evidence.md` | Task 6 |

## Task 1: Pinned container workflow

**Interfaces:** `scripts/container.py` is a stdlib-only host transport invoked through uv. It owns build-context export, `build`, `up`, `exec`, `stop`, and metadata output. Development/test commands execute in `/workspace/repos/agileforge`; Compose uses an external named workspace volume and non-root UID/GID 10001. The production image invokes `python -m cli.container_runtime` from its installed environment.

- [x] Add tests for source export rejecting dirty production builds and excluding runtime databases, environment files, Git configuration, caches, and synthetic secret canaries.

```python
def test_export_omits_untracked_runtime_state(tmp_path):
    checkout = make_committed_repository(tmp_path)
    (checkout / '.env').write_text('CANARY=container-test-only')
    archive = export_source(checkout, tmp_path / 'context')
    assert not (archive / '.env').exists()
```

- [x] Run the focused test and record the missing-export failure. Implement explicit tracked-source export plus a build identity file, never an unrestricted `COPY` of the user's working directory.
- [x] Resolve official immutable image/tool pins. Use an Ubuntu package snapshot, exact Node binary/checksum, Python 3.13.15, uv 0.12.8, and the existing controller SHA.
- [x] Add shared dependencies, development, test, wheel builder, and production Docker targets. Use locked uv resolution and an installed wheel. Production carries Git and required runtime libraries, not checkout source or development credentials.

```dockerfile
ARG SOURCE_REVISION
LABEL org.opencontainers.image.revision=$SOURCE_REVISION
WORKDIR /workspace/repos/agileforge
```

- [x] Build the development image, bootstrap a clean Git clone into a named Linux volume, and prove a file edit, Git diff, linked worktree, uv Python, Node, and checkout launcher command. Recreate the service and verify the workspace survives.
- [x] Verify Compose publishes only `127.0.0.1`, uses init, and mounts stable Linux volume paths. Record resources, architecture/emulation, pins, build time, and startup time.
- [x] Run the focused source-export tests in the container; review the build diff and commit this slice locally.

## Task 2: Maintenance fencing and portable transfer

**Interfaces:**

```python
@contextmanager
def runtime_fence(root: Path, *, exclusive: bool = False) -> Iterator[None]: ...

class StateLayout:
    root: Path
    business_database: Path
    trace_database: Path
    artifacts: Path
    model_config: Path

def backup_state(layout: StateLayout, destination: Path) -> Path: ...
def verify_backup(bundle: Path) -> TransferManifest: ...
def restore_payload(bundle: Path, destination: Path) -> TransferManifest: ...
```

`runtime_fence` fails immediately on contention and rejects unsafe lock paths. Use POSIX `flock` for the supported environment, with a pre-cutover native implementation or explicit unsupported-source error where safe locking cannot be established. Shared locks live as long as services/commands; backup and restore require exclusive ownership. `StateLayout` accepts only validated regular owned paths; transfer never initializes application stores. Task 4 connects supported entrypoints to the fence. Task 5 checks other Docker writable mounts before maintenance.

- [x] Write a real multiprocessing contention test before the lock. A process holding shared access prevents exclusive access; an exclusive holder prevents a new shared process. A dead process releases the lock. Symlinked lock paths fail.
- [x] Implement the fence and run those tests inside Linux.
- [x] Write backup/verify tests using two real temporary SQLite stores with tables, blobs, duplicate values, sequences, foreign keys, and a synthetic artifact. Corrupt one byte, delete a manifest entry, and add an unlisted payload: all must fail verification.

```python
def test_corrupt_artifact_prevents_restore(layout, tmp_path):
    bundle = backup_state(layout, tmp_path / 'backup')
    (bundle / 'artifacts' / 'accepted.md').write_bytes(b'corrupt')
    with pytest.raises(TransferError):
        restore_payload(bundle, tmp_path / 'restored')
    assert not (tmp_path / 'restored').exists()
```

- [x] Implement quiescent paired SQLite backups, integrity/foreign-key checks, canonical typed row/schema inventory, complete artifact byte inventory, and atomic manifest publication using `agileforge.transfer.v1`. Preserve all tables and unlinked trace sessions; compare observed attempt/session links without requiring a trace for every attempt.
- [x] Reject special files, traversal, aliases, nonempty destinations, unknown formats, corrupt/partial bundles, unsupported application schemas, and interrupted publication. Use staging and atomic publication; never silently create empty state.
- [x] Keep repositories as an explicit inventoried transfer component. Capture dirty/untracked bytes, modes, safe symlinks, common-directory data, and excluded secret paths. Do not dereference external symlinks or print repository contents/remotes with credentials.
- [x] Run the red/green tests and focused security checks; record exact API details for Tasks 3–5, review, and commit locally.

## Task 3: Installed build and durable production state

**Interfaces:** `utils/build_identity.py` validates versioned `BuildIdentity` from `/opt/agileforge/build.json`. `cli/production_state.py` validates `ProductionStateManifest` from `/var/lib/agileforge/profiles/<name>/runtime.json`; the manifest owns state ID, separate databases, model config, and artifacts. `cli/container_runtime.py` exposes explicit `init`, `info`, `serve`, `cli`, `backup`, and `restore`. It consumes Task 2's fence and transfer primitives. The default service refuses missing state; initialization is explicit.

- [x] Add tests for full-SHA build identity, root-owned immutable metadata, known versions, state ID persistence, model-config mismatch, symlink/alias rejection, and missing/partial profile refusal.

```python
def test_image_replacement_keeps_state_identity(production_profile, build_a, build_b):
    first = load_production_state(production_profile, build=build_a)
    second = load_production_state(production_profile, build=build_b)
    assert second.state_id == first.state_id
```

- [x] Implement authoritative loaders with injected paths only for unit tests. Runtime/API identity comes from validated records, never identity environment overrides. Preserve current development `RuntimeProfile` ownership.
- [x] Add explicit production initialization using existing current-schema bootstrap and model config. Publish `runtime.json` last; reject an existing unfinalized destination. The trace store may legitimately be absent before the first trace; define and preserve that state rather than fabricate a session.
- [x] Implement foreground service supervision with fresh nonce, identity-aware readiness, explicit `0.0.0.0` container listener, non-root secrets validation, sanitized environment, finite TERM/KILL cleanup of the complete owned process group, and log redaction.
- [x] Add real subprocess tests for descendant cleanup, early failure, foreign readiness, and signals, plus runtime-only synthetic secret canaries. Keep provider calls disabled.
- [x] Wire backup/restore to reserved production paths and manifest publication. Restore never overwrites tracked source files or silently initializes empty state.
- [x] Run focused tests and review/commit the installed-runtime slice locally.

## Task 4: Existing launcher and application integration

**Consumes:** Task 2 `runtime_fence`; Task 3 build/state loaders and service lifecycle. **Produces:** checkout launcher container listener, guarded dev backup/restore, API production identity, and fenced supported state entrypoints.

- [x] Add failing launcher tests for `--host 0.0.0.0` while default remains loopback; readiness probes continue using loopback. Add process-group tests that catch a surviving descendant after parent exit.
- [x] Extend `UiRequest` and `start_ui` with a defaulted host field, preserve old callers, and use finite POSIX process-group cleanup. Preserve Windows behavior until cutover.
- [x] Add production state ID/build identity to dashboard readiness using Task 3's authoritative loader, while retaining checkout provenance and existing response compatibility.
- [x] Fence application lifetimes before state initialization: dev init/reset/CLI/UI, product CLI, production entrypoint, and API lifespan. Avoid nested-lock deadlock by using shared locks for normal parent/child command trees; exclusive transfer enters before normal runtime setup. Raw imports must not initialize durable state before the fence.
- [x] Add checkout backup/verify/restore commands with explicit destinations. Stage and verify first; require destination `config/models.yaml` hash equality, then reserve with `prepare_profile_record`, install databases/artifacts, and `finalize_profile_record` last. Preserve absence of an unused trace store.

```python
def test_restore_does_not_overwrite_checkout_configuration(checkout, bundle):
    before = (checkout / 'config/models.yaml').read_bytes()
    with pytest.raises(TransferError):
        restore_development_profile(checkout, 'restored', bundle)
    assert (checkout / 'config/models.yaml').read_bytes() == before
```

- [x] Run existing dev-runtime, API readiness, distribution, secret-file, and profile ownership regressions alongside new cases. Review and commit integration locally.

## Task 5: Synthetic transfer and container acceptance

**Files:** `scripts/verify_container.py`, `tests/container_runtime/test_relocation.py`, CI container jobs.

- [x] Build current-schema synthetic accepted Specifications, binding history, active/completed Sprint history, idempotency/audit rows, and a real disposable ADK trace session using existing fixtures. Assert exact inventory after transfer, including all trace sessions and observed cross-store links.
- [x] Restore repositories at different Linux paths and run supported Git worktree repair. Check HEAD, branch, common directory, dirty/untracked hashes, modes, and approved remotes before guarded `attach_repository`. Preserve old rows and accepted bytes; permit only the supported attach command's explicit binding/pointer/audit delta.
- [x] Recreate containers and verify same-name worktree profile isolation, separate databases, stable state IDs, build revision, fresh nonce/PID, and retained artifacts. Verify complete process-tree/endpoint shutdown.
- [x] Test source registration on documented paths using existing POSIX race/symlink/canonical-hash/stale-action suites. Run browser E2E and seven Node suites through the image.
- [x] Run `./agileforge-dev check --json` in a clean committed container checkout, with the existing controller. Record all stage outputs and packaged-production verification separately. Inspect existing native CI for a same-SHA baseline; label emulated local results honestly.
- [x] Add required Linux container validation to CI, retaining native jobs until approved cutover. Keep action pins, timeouts, coverage, and provider exclusions. Do not push to obtain CI without authorization.
- [x] Independently review the full branch for correctness/security and fix material findings. Retain logs and test evidence; no completion claim from configuration alone.

## Task 6: Runbook and operator handoff

- [x] Document fresh setup, exact pins/architecture, named volumes, UID/GID, host controller editing/exec transport, worktrees, target registration, runtime secrets, ports, backup, restore, and rollback.
- [x] Write exact synthetic evidence and remaining native validation limitations. Separate test time, image build/startup costs, and platform runner savings; no speedup assumption.
- [x] Document approvals in order: real-profile read-only inventory, exact copy/cutover set, final switch. List source fencing, final backup, staged restore/Git repair, guarded binding replacement, offline validation, switch, then live writes. Rollback expires at first live destination write.
- [x] Keep the native-removal inventory as the post-cutover checklist. Do not remove adapters/jobs or add rejection gates before the issue's prerequisite cutover.
- [x] Verify links/commands against implemented CLI help, commit docs and evidence, and report readiness plus exact outstanding operator work. Keep #263 open.

## Plan self-review

Task 1 supplies the Linux test environment. Tasks 2 and 3 own separate new modules;
Task 3 consumes only Task 2's documented lock/transfer interface. Task 4 owns all
shared-entrypoint changes, so workers do not race in `api.py` or `dev_main.py`.
Task 5 verifies cross-component behavior before Task 6 makes readiness claims.
Native removal remains after the separately approved cutover, as the spec requires.
No plan step authorizes protected-data access, provider execution, or publication.
