# CLI administration, diagnostics and recovery

Use CLI when the user requests administration/recovery or when authorized diagnostics require it. Ordinary Project and workflow work uses MCP.

## Select the exact installation

A CLI process can address different state from the MCP service. Verify the executable or container, release/checkout, selected profile, and business-state identity before relying on its results. Use the selected deployment's documented container transport on macOS/Windows; AgileForge runs on Linux.

For a stable installation inside its supported runtime:

```sh
command -v agileforge
agileforge --version
agileforge project list
```

The stable `agileforge` CLI has no `info` subcommand. Do not search parent directories for a development launcher. For an explicitly selected source checkout, run that checkout's supported launcher inside its Linux container:

```sh
./agileforge-dev info --profile <profile> --json
./agileforge-dev cli --profile <profile> -- project list
```

Packaged container runtime administration uses its own `cli.container_runtime` interface, including `info --profile <profile> --json`. Verify the selected release's help/runbook before using it; do not confuse this interface with the stable `agileforge` command.

## Bounded diagnostics

Read-only CLI examples, after identity verification:

```sh
agileforge project show --project-id <id>
agileforge workflow position --project-id <id>
agileforge workflow next --project-id <id>
```

Use the selected development prefix when applicable. CLI and MCP reads should agree for the same state and Project. Bound and parse JSON rather than dumping full packets. A mismatch affecting decisions or durable state stops mutations pending diagnosis.

Consult the selected release's [container runbook](https://github.com/arduinitavares/agileforge/blob/master/docs/linux-containers.md) for profile inspection, backup, restore, schema maintenance, relocation, or service recovery. Inspect current help before an administrative command. Each write or destructive action needs its applicable user authority and recovery checks. A skill invocation does not authorize a reset, restoration, migration, restart, deployment, or credential change.

Do not use CLI as a fallback for an authentication denial, to manufacture acceptance, or to execute a stale workflow template. Once recovery is verified, reconnect MCP, confirm `service_identity`, and refresh both workflow reads.
