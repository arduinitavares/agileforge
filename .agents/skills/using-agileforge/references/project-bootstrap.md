# Project bootstrap

Use this procedure when entering a target repository or when AgileForge project identity is unknown.

## Inspect the target repository

Read the nearest applicable `AGENTS.md` and other repository instructions. Then record:

```sh
repo_root="$(git rev-parse --show-toplevel)"
git status --short --branch
git rev-parse HEAD
```

Do not discard, stash, or rewrite unrelated changes.

## Select the AgileForge runtime

Follow explicit user or repository configuration first.

For a stable installation, verify `agileforge` is available and use its JSON-first CLI:

```sh
command -v agileforge
agileforge --version
agileforge project list
```

Do not run `agileforge info`; stable releases have no such command. Provenance through `info --json` belongs to `agileforge-dev` only.

Do not scan parent directories for `agileforge-dev`, copy it into the target repository, or assume every repository has a development launcher.

For an explicitly selected AgileForge source checkout, change to that exact checkout and run its launcher there:

```sh
./agileforge-dev info --profile <profile> --json
./agileforge-dev cli --profile <profile> -- project list
```

Add `--secrets-file` only for an authorized provider-backed action. Never inspect or print credential values.

If neither runtime is available, stop and ask the user which AgileForge installation and profile to use. Do not invent a path.

## Resolve project identity

Parse `project list` as structured JSON. Compare repository bindings against the resolved target `repo_root`. Use `project show --project-id <id>` when list output is insufficient.

- One exact binding match: use that project ID.
- No match: create only when the user asked to start or create the AgileForge project.
- More than one match, a stale binding, or a name-only guess: stop and present the ambiguity.

Never default to project ID `1`.

## Create when authorized

An instruction to create or start the project when absent is approval for this local creation. Do not ask again after resolving the inputs.

Use the approved product name from the specification or ask only when the name is unresolved. Use the resolved repository root, a fresh idempotency key, and an explicit actor:

```sh
agileforge project create \
  --name "<approved-product-name>" \
  --repository-path "$repo_root" \
  --idempotency-key "<new-key>" \
  --actor "codex"
```

For a source checkout, apply the same semantic arguments after its `./agileforge-dev cli --profile <profile> --` prefix.

Parse the result. If the transport result is uncertain, preserve the exact request and key before retrying. Do not change either.

## Establish the next step

For the resolved project ID, run:

```sh
agileforge workflow position --project-id <id>
agileforge workflow next --project-id <id>
```

Use the selected source-checkout prefix when applicable. Report the current reason code and exact advertised command. Do not mutate until the action is within the user's authority.
