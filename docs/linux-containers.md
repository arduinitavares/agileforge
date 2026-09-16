# Linux containers: operator runbook

This is the supported Linux/amd64 container workflow for AgileForge #263. It
uses pinned images, named Linux volumes, and a fixed non-root runtime user.

Use the recorded validation evidence for the tested revision. These commands
describe the operator workflow and do not authorize a live-data cutover.
An amd64 image run on an ARM64 Docker VM is functional emulation only; it is
not a native amd64 performance baseline.

## Paths, ownership, and scope

The image pins are in `containers/pins.json`. The application user is
`agileforge` (`UID:GID 10001:10001`). Do not run application commands as root.

| Purpose | Fixed Linux path | Storage |
| --- | --- | --- |
| Development checkout | `/workspace/repos/agileforge` | `<project>-workspace` |
| Linked worktrees | `/workspace/worktrees/<task>` | `<project>-workspace` |
| Registered repositories | `/workspace/repos/targets/<name>` | `<project>-workspace` |
| Disposable tool/browser cache | `/cache` | `<project>-cache` |
| Production profiles | `/var/lib/agileforge/profiles/<name>` | `<project>-production-state` |
| Packaged build identity | `/opt/agileforge/build.json` | image, root-owned read-only |
| Runtime secret file | `/run/secrets/agileforge` | explicit runtime mount only |

Do not bind-mount a macOS or Windows source tree into `/workspace`. The host
controller uses `uv`; project work, Git, tests, and developer commands run in
the Linux container. `compose.yaml` publishes the two UI ports only on
loopback: development `127.0.0.1:8765` and production `127.0.0.1:8766`.

Set a lower-case project namespace once per shell. It names the task-owned
container and volumes, so do not reuse it for an unrelated workspace.

```sh
project_name=agileforge
controller() { uv run --no-project --python 3.13.15 python scripts/container.py "$@"; }
```
All following controller commands run from a reviewed AgileForge checkout. The
source checkout used for import and production build must be a clean, committed
Git worktree. Never use a profile-bearing active checkout as a source of files.

## Development bootstrap

Build the development image from an explicit tracked Git export, create the
named Linux volumes, then perform the one-time bundle import. `import-source`
refuses an existing Linux checkout; it does not overwrite it.

```sh
controller --checkout "$PWD" --project-name "$project_name" \
  build --target development --tag agileforge-development:local
controller --project-name "$project_name" up
controller --checkout "$PWD" --project-name "$project_name" import-source
```
The stable checkout path is always `/workspace/repos/agileforge`. Normal work
must go through `exec`, which holds the shared workspace fence for the child
command's whole lifetime:

```sh
controller --project-name "$project_name" exec -- git status --short
controller --project-name "$project_name" exec -- \
  /workspace/repos/agileforge/agileforge-dev info --profile dev --json
controller --project-name "$project_name" exec -- \
  /workspace/repos/agileforge/agileforge-dev ui --profile dev \
  --host 0.0.0.0 --port 8765
```
The dashboard remains reachable only from the same host at
`http://127.0.0.1:8765`. The container listener is `0.0.0.0` so Docker can
reach it across its network namespace; that is not a public publication.

For an agent or editor change, review the patch on the host, send it by stdin,
then inspect the resulting diff inside the container. This never shares the
host source directory with the container:

```sh
controller --project-name "$project_name" exec -- \
  git -C /workspace/repos/agileforge apply --whitespace=error - < reviewed.patch
controller --project-name "$project_name" exec -- \
  git -C /workspace/repos/agileforge diff --check
controller --project-name "$project_name" exec -- \
  git -C /workspace/repos/agileforge diff
```
Create linked worktrees under the shared volume, then use that worktree's own
launcher and profile. Replace the branch and task values with reviewed values.

```sh
controller --project-name "$project_name" exec -- \
  git -C /workspace/repos/agileforge worktree add \
  /workspace/worktrees/task-263 -b dev/task-263
controller --project-name "$project_name" exec -- \
  /workspace/worktrees/task-263/agileforge-dev info --profile task-263 --json
```
Stop only the development service when normal work is done. This retains the
workspace and cache volumes.

```sh
controller --project-name "$project_name" stop
```
## Test image and canonical gate

Build the separate test target from a clean committed checkout. Run the
canonical repository gate inside that image against the Linux workspace volume.
```sh
controller --checkout "$PWD" --project-name "$project_name" \
  build --target test --tag agileforge-test:local
docker run --rm --platform linux/amd64 \
  --mount "type=volume,source=$project_name-workspace,target=/workspace" \
  --workdir /workspace/repos/agileforge agileforge-test:local \
  ./agileforge-dev check
```
Record the actual source revision, image identity, host architecture, command,
and result with the validation evidence. Do not infer pass counts, timings, or
a native Linux performance result from this run.

## Production image and installed runtime

Build a production image only from a clean committed checkout. The build writes
the immutable revision, source hash, lock hash, Python, uv, and package version
to `/opt/agileforge/build.json`; it is distinct from mutable durable state.

```sh
controller --checkout "$PWD" --project-name "$project_name" \
  build --target production --tag agileforge-production:local
docker volume create "$project_name-production-state"
```
For a fresh Compose rehearsal with a different reviewed tag, set
`AGILEFORGE_DEVELOPMENT_IMAGE` or `AGILEFORGE_PRODUCTION_IMAGE` to that tag
before invoking Compose. This avoids replacing another local rehearsal's image
tag.
Use the installed runtime through the production Compose service. `init` creates
a new profile; `info` validates the build and state manifests; `serve` remains
in the foreground; `cli` requires `--` before the product arguments. Use the
controller maintenance transport for `init`, `backup`, and `restore`: it
resolves the supplied image reference to an exact local image ID and refuses
any running container that can write either durable volume.

```sh
controller --project-name "$project_name" production-maintenance \
  --image agileforge-production:local -- init --profile demo --json
AGILEFORGE_CONTAINER_PROJECT="$project_name" docker compose \
  --project-name "$project_name" --profile production run --rm production \
  info --profile demo --json
AGILEFORGE_CONTAINER_PROJECT="$project_name" docker compose \
  --project-name "$project_name" --profile production run --rm production \
  cli --profile demo -- --help
AGILEFORGE_CONTAINER_PROJECT="$project_name" docker compose \
  --project-name "$project_name" --profile production run --rm --service-ports \
  production serve --profile demo --port 8765
```
The last command is available at `http://127.0.0.1:8766` while it runs. The
packaged production default is `serve --profile default`; use an explicit
command when the selected profile differs.

Credentials are runtime-only. Never put them in a Dockerfile, build argument,
image layer, Git bundle, backup, checkout `.env`, or ambient host environment.
For authorized provider work, mount one regular non-symlink file
at `/run/secrets/agileforge`, owned by UID 10001 and mode `0600`, and pass
`--secrets-file /run/secrets/agileforge` to `serve` or `cli`. On a Linux Docker
host, supply the operator-owned `secret_file` with those container-visible
permissions, using a read-only runtime mount:

```sh
AGILEFORGE_CONTAINER_PROJECT="$project_name" docker compose \
  --project-name "$project_name" --profile production run --rm \
  --volume "$secret_file:/run/secrets/agileforge:ro" production \
  cli --profile demo --secrets-file /run/secrets/agileforge -- project list
```
The synthetic rehearsal tests the same file contract in a private tmpfs mount;
it does not access host credentials.

## Backup, restore, and repository relocation

Backups and restores require exclusive maintenance. First stop every known
supported service and worker that can write the selected state. Then run the
development command through `exec --maintenance`; it refuses known writable
volume consumers and starts a temporary container from the stopped development
container's exact image. It is the supported path for maintenance work.

```sh
controller --project-name "$project_name" stop
controller --project-name "$project_name" exec --maintenance -- \
  /workspace/repos/agileforge/agileforge-dev backup --profile dev \
  --destination /workspace/backups/dev-transfer --json
controller --project-name "$project_name" exec --maintenance -- \
  /workspace/repos/agileforge/agileforge-dev verify-backup \
  --bundle /workspace/backups/dev-transfer --json
controller --project-name "$project_name" exec --maintenance -- \
  /workspace/repos/agileforge/agileforge-dev restore --profile restored-dev \
  --bundle /workspace/backups/dev-transfer --json
```

A development backup discovers all registered repositories and captures their
Git common directories and linked worktree groups. Extra `--repository` paths
are only for an explicit reviewed addition. On native POSIX source systems,
supported writers may use the owned-home fence only when every included path is
inside that home. A repository, Git directory, common directory, or linked
worktree outside the owned domain must be rejected.

Do not call raw `docker exec` and claim it is maintenance-fenced. Raw editors,
Git commands, older binaries, direct SQLite writers, and any known container
with a writable selected-volume mount must be quiesced and verified manually
before a backup or restore. The system cannot detect arbitrary embedded secrets;
approve the live inventory and every exclusion before any copy.

After a restore, compare the verified inventory, tracked model configuration
hash, profile identity, and last publication before treating it as usable.
`repository-relocations.json` records source-to-restored bindings. It
requires a guarded `attach_repository` operation before normal UI or provider
CLI use; do not edit historical SQL rows or use `refresh_repository` as a
relocation mechanism.

Production maintenance runs in a fresh, one-shot installed-runtime container.
Stop every supported producer first; the controller then checks all running
containers for writable mounts of the selected workspace and production-state
volumes before it starts. It mounts only those two volumes, never host paths or
host secrets. The image reference may be a reviewed tag, because the controller
inspects it and runs the resulting immutable image ID.

```sh
controller --project-name "$project_name" production-maintenance \
  --image agileforge-production:local -- backup --profile demo \
  --destination /workspace/backups/demo-transfer --json
controller --project-name "$project_name" production-maintenance \
  --image agileforge-production:local -- restore --profile restored-demo \
  --bundle /workspace/backups/demo-transfer --json
```

## Live cutover gates

Synthetic implementation work does not grant any of these approvals. Keep
native macOS and Windows jobs/adapters active and keep #263 open until all three
gates have been explicitly approved and the final acceptance succeeds.

1. **Read-only inventory approval.** Present the exact real profiles,
   repositories, linked worktrees, owner IDs, dirty/untracked observations,
   secret exclusions, and proposed backup destination. Do no copying here.
2. **Exact copy and cutover-set approval.** Stop and verify source writers,
   create and verify the paired source backup, stage the Linux restore and Git
   repair, and perform guarded offline replacement bindings with providers
   disabled. Present the resulting destination inventory.
3. **Switch approval.** Only after the operator approves the displayed result,
   enable the destination's live writes. Do not run source and destination
   writers together.

Before the first live destination write, rollback means discard the candidate
destination and resume the verified, fenced source. After that first write,
rollback is no longer safe: stop the destination and perform forward recovery,
or obtain approval for a separately designed reverse transfer.

## Current limits

Source import transfers only one clean Git bundle; it excludes profiles, state,
caches, secrets, and host editor configuration. Use `exec`, or after stopping
consumers `exec --maintenance`, rather than raw Docker execution. This document
does not claim validation completion, timing, or native amd64 performance.
