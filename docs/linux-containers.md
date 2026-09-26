# Linux containers: operator runbook

This is the supported Linux/amd64 container workflow for AgileForge #263. It
uses pinned images, named Linux volumes, and a fixed non-root runtime user.

Use the recorded validation evidence for the tested revision. These commands
describe the operator workflow and do not authorize a live-data cutover.
An amd64 image run on an ARM64 Docker VM is functional emulation only; it is
not a native amd64 performance baseline.

## Run the packaged app

Everyday startup needs Docker Engine with Compose. Python, `uv`, a source import,
and the development controller are not required on the host. The `production`
service is the default; the development toolbox is opt-in.

Use a reviewed image available locally as `agileforge-production:local`, or set
`AGILEFORGE_PRODUCTION_IMAGE` to the reviewed image tag or digest you have loaded
or pulled. This repository does not yet publish an image to a registry. Building
an image from source is a separate maintainer workflow described below.

From the directory containing `compose.yaml`, initialize an empty installation
once:

```sh
docker compose run --rm production init --profile default
```

Compose creates the workspace and production-state volumes. Initialization takes
the exclusive runtime fence and refuses an existing profile. It does not migrate
existing data; use the verified backup/restore workflow for that.

Start and manage the app using normal Compose commands:

```sh
docker compose up -d
docker compose logs -f
docker compose stop
```

Open <http://localhost:8766/dashboard> after the readiness message appears in the
logs. `Ctrl+C` exits log following; use `stop` to stop the background service.
Run `up -d` again to start it. `docker compose down` removes the service and
network but retains data; `docker compose down --volumes` deletes the named
volumes and their data. Do not use `--volumes` for an installation you want to keep.

The default UI port is host-loopback only. Set `AGILEFORGE_PORT` to another host
port when needed. For independent installations, use `docker compose -p NAME`
consistently for init, up, logs, and stop; each project gets separate named
volumes. The existing `AGILEFORGE_CONTAINER_PROJECT` setting remains supported,
and the standard Compose project selection controls resource names.

Previously created volumes retain their existing names. Compose may warn that
older volumes were not created by Compose; it reuses their contents. Do not
delete them to silence the warning. Use a fresh project name for a trial run.

Provider-backed actions require an explicit runtime secret mount as described
below. Startup itself does not load the host's credentials.

Project Vision generation uses a built-in 128,000 completion-token limit for
both its primary and semantic-repair calls. Its recipe has a 600-second total
deadline and a 660-second durable attempt lease. It makes one generation call
and, only for a semantically invalid complete draft, at most one repair call.
New attempts record the effective limit, deadline, and retry policy. Incomplete
output and invalid payloads surface as `VISION_OUTPUT_INCOMPLETE` and
`INVALID_VISION_PAYLOAD` with bounded diagnostic metadata. Other agentic roles
retain their existing limits. The production launcher does not forward
`VISION_INTERVIEWER_MAX_TOKENS`; the built-in default needs no override.

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
the Linux container. AgileForge itself refuses to start on a non-Linux host:
`agileforge-dev`, the production CLI, the container runtime CLI, and the API
exit with code 2 and point here. `compose.yaml` publishes the two UI ports only on
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

Build the development image from an explicit tracked Git export, start the
opt-in development service, then perform the one-time bundle import. Compose
creates its named Linux volumes. `import-source`
refuses an existing Linux checkout; it does not overwrite it.

```sh
controller --checkout "$PWD" --project-name "$project_name" \
  build --target development --tag agileforge-development:local
controller --project-name "$project_name" up
controller --checkout "$PWD" --project-name "$project_name" import-source
```
The controller selects only `development`. The equivalent service selection is
`docker compose --profile development up -d development`; it does not start the
packaged app. The remaining controller commands are for contributor work inside
the Linux source workspace.
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
```
For a fresh Compose rehearsal with a different reviewed tag, set
`AGILEFORGE_DEVELOPMENT_IMAGE` or `AGILEFORGE_PRODUCTION_IMAGE` to that tag
before invoking Compose. This avoids replacing another local rehearsal's image
tag.
Use the installed runtime through the default production Compose service.
The first-launch commands above initialize and serve the `default` profile.
`info` validates the build and state manifests; `cli` requires `--` before the
product arguments. Explicit initialization holds an exclusive runtime fence and
never replaces existing state. Use the controller maintenance transport for
`backup` and `restore`: it
resolves the supplied image reference to an exact local image ID and refuses
any running container that can write either durable volume.

```sh
AGILEFORGE_CONTAINER_PROJECT="$project_name" docker compose \
  --project-name "$project_name" run --rm production \
  info --profile default --json
AGILEFORGE_CONTAINER_PROJECT="$project_name" docker compose \
  --project-name "$project_name" run --rm production \
  cli --profile default -- --help
```
The packaged production default is `serve --profile default`; use an explicit
command when running a different profile for a maintenance rehearsal.

Credentials are runtime-only. Never put them in a Dockerfile, build argument,
image layer, Git bundle, backup, checkout `.env`, or ambient host environment.
For authorized provider work, mount one regular non-symlink file
at `/run/secrets/agileforge`, owned by UID 10001 and mode `0600`, and pass
`--secrets-file /run/secrets/agileforge` to `serve` or `cli`. On a Linux Docker
host, supply the operator-owned `secret_file` with those container-visible
permissions, using a read-only runtime mount:

```sh
AGILEFORGE_CONTAINER_PROJECT="$project_name" docker compose \
  --project-name "$project_name" run --rm \
  --volume "$secret_file:/run/secrets/agileforge:ro" production \
  cli --profile default --secrets-file /run/secrets/agileforge -- project list
```
The synthetic rehearsal tests the same file contract in a private tmpfs mount;
it does not access host credentials.

For the background dashboard, put the following in a local
`compose.override.yaml`, set `AGILEFORGE_SECRETS_FILE` to the absolute path of
the prepared secret file, and use the same `docker compose up -d` command:

```yaml
services:
  production:
    command: [serve, --profile, default, --secrets-file, /run/secrets/agileforge]
    volumes:
      - type: bind
        source: ${AGILEFORGE_SECRETS_FILE:?Set the prepared secret file path}
        target: /run/secrets/agileforge
        read_only: true
        bind:
          create_host_path: false
```

The container-visible owner and mode requirements above still apply. Keep the
secret file outside the repository and pass its path, never its contents, in
Compose configuration. This optional override is not needed to start without
provider credentials.

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
  --destination /workspace/dev-transfer --json
controller --project-name "$project_name" exec --maintenance -- \
  /workspace/repos/agileforge/agileforge-dev verify-backup \
  --bundle /workspace/dev-transfer --json
controller --project-name "$project_name" exec --maintenance -- \
  /workspace/repos/agileforge/agileforge-dev restore --profile restored-dev \
  --bundle /workspace/dev-transfer --json
```

A development backup discovers all registered repositories and captures their
Git common directories and linked worktree groups. With `--repository`, supply
the complete reviewed set, including all registered main and linked worktrees.
The destination parent must exist and be outside every captured repository,
Git-admin directory, and artifact tree. On native POSIX source systems,
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
  --image agileforge-production:local -- backup --profile default \
  --destination /workspace/default-transfer --json
controller --project-name "$project_name" production-maintenance \
  --image agileforge-production:local -- restore --profile restored-demo \
  --bundle /workspace/default-transfer --json
```

To change an existing production profile's model choices, place a complete
`models.yaml` in the workspace volume and prepare an owned, private parent for
the recovery record outside every registered repository and Git admin tree.
The target directory must not already exist. Keep it on `/workspace` or
`/var/lib/agileforge` so the same maintenance container can reach it. Stop the
service and other writers as for backup, then run:

```sh
controller --project-name "$project_name" production-maintenance \
  --image agileforge-production:local -- configure-models --profile default \
  --model-config /workspace/model-candidates/models.yaml \
  --backup-directory /workspace/model-recovery/default-2026-09-26 --json
```

The result gives the profile and state ID, old and new model configuration
hashes, and the private recovery directory. The operation validates every
retained role and optional reasoning settings, preserves the profile's identity
and history, and records the exact previous `models.yaml` and `runtime.json`
bytes. Restart the service after a successful update so its adapters load the
new configuration. Keep the recovery directory private and intact until a
later update supersedes it or the rollback window is deliberately closed.

If an update or recovery is interrupted, normal startup refuses the pending
operation. Use the directory recorded in the update result or the profile's
`model-config-update.json` marker to recover explicitly:

```sh
controller --project-name "$project_name" production-maintenance \
  --image agileforge-production:local -- recover-models --profile default \
  --backup-directory /workspace/model-recovery/default-2026-09-26 --json
```

Recovery checks the marker, receipt, saved bytes, state identity, and current
file hashes before restoring the literal previous pair. Repeating the same
recovery is safe. A completed update can also be rolled back before another
configuration update. The command refuses unrelated manual drift and a wrong
or modified recovery record; preserve both files and investigate such a refusal
rather than editing the manifest by hand.

### Promote a development profile into the packaged app

State that was born under `agileforge-dev` carries `provenance/profile.json`,
not `provenance/runtime.json`. Production `restore` refuses such a bundle unless
`--from-development` is given, and refuses a production bundle when the flag is
present. Promotion is a one-way move into a new production profile; the source
profile and bundle stay untouched.

On the source machine, capture state only. Registered repositories are never
part of a promotion; the target repository is cloned separately on the Linux
side.

```sh
./agileforge-dev backup --profile "$source_profile" --state-only \
  --destination "$bundle_dir" --json
./agileforge-dev verify-backup --bundle "$bundle_dir" --json
```

Every Project with an active repository binding in the bundle needs one
`--bind-repository PROJECT_ID=PATH` naming the absolute container path where
its Linux clone will live, normally `/workspace/repos/targets/<name>`. Restore
refuses a missing target, a target for a Project without an active binding, a
relative path, a duplicate Project, and a target that also appears as a bundled
repository. Nothing is written to `/var/lib/agileforge` when validation fails.

Copy the bundle and the target clone into the `workspace` volume as
`10001:10001` before the restore. Use a throwaway helper container for the copy;
the app itself never mounts host paths.

Docker gives a new volume the owner of the image directory it is first mounted
on. The production image owns `/workspace` and `/var/lib/agileforge` as
`10001:10001`; a helper image such as `alpine` has neither, so a volume the
helper creates is `root:root`, and the exclusive runtime fence then refuses the
restore with `runtime fence root is not owned by this user: /workspace`. After
copying, set the owner of the mount points themselves, not only the copied
trees, and confirm both before restoring:

```sh
docker run --rm \
  --mount "type=volume,source=$project_name-production-state,target=/var/lib/agileforge" \
  --mount "type=volume,source=$project_name-workspace,target=/workspace" \
  alpine sh -c 'chown 10001:10001 /var/lib/agileforge /workspace \
    && ls -ldn /var/lib/agileforge /workspace'
```

The fences are taken before the bundle is read, so a restore refused this way
writes nothing except the lock inode `.agileforge-runtime.lock`, which stays.

```sh
docker compose run --rm production restore --profile default \
  --bundle /workspace/transfer/"$bundle_name" --from-development \
  --bind-repository 1=/workspace/repos/targets/backend --json
```

The restored profile keeps the source descriptor as
`config/source-profile.json` and records each target in
`repository-relocations.json`. While a relocation is pending, `serve` refuses
to start and `cli` permits only the exact guarded attach for that Project and
path. Run it once per bound Project, then start the app normally.

```sh
docker compose run --rm production cli --profile default -- \
  repository attach --project-id 1 --path /workspace/repos/targets/backend \
  --idempotency-key "$attach_key" --actor "$operator"
docker compose up -d
```

`info --json` stays available while the relocation is pending. Product CLI
reads such as `cli --profile default -- status --project-id 1` work only after
the attach. A promotion never advances workflow state; a blocked Project stays
blocked.

## Live cutover gates

Native macOS and Windows CI jobs have been retired with maintainer approval.
The real launcher smoke now runs in the Linux container gate; shared security,
profile, secret, and lifecycle tests remain. After the approved live cutover,
the native Windows adapters, launcher branches, transfer paths, host migration
scripts, and `tests/windows/` were removed, and every entry point gained an
early Linux-only guard. Tests that execute the real launcher, API, or product
processes are marked Linux-only; the rest of the suite bypasses the guard
through an autouse fixture so it still runs on developer laptops.

Synthetic implementation work and CI retirement do not grant any of the
following approvals. Keep #263 open until all three gates have been explicitly
approved and the final acceptance succeeds.

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
