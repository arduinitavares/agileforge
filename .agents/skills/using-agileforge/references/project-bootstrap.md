# Project bootstrap

Read the target repository's instructions. Record its absolute root, branch, HEAD, and dirty state. Do not assume it contains AgileForge source or a development launcher.

## Select the service and Project

Use the explicitly selected MCP connection. Discover tools and call `service_identity`, then `project_list`. Confirm the runtime/profile and business-state identity before commands. Use `project_show` when list output does not establish the repository binding.

Match the actual target repository to its service-side binding. A container or remote service can use a different path. Resolve that mapping from the selected deployment or explicit user input; do not guess, create a duplicate Project, or reattach a repository to make paths appear equal.

- One verified binding: use that Project ID.
- No verified match: establish identity before creation.
- Multiple matches or stale bindings: present the ambiguity.

Do not select by name or numeric order alone.

## Create when authorized

If the user asked to start or create the Project and no matching binding exists, call discovered `create_project`. Supply the approved `name`, explicit `actor`, and a new `idempotency_key`. Use `repository_path` only when the path is verified in the service's filesystem. A client path is not automatically a server path.

Do not initialize or reset a runtime/profile to connect MCP. Project creation does not authorize provider generation or product acceptance.

Parse the result. Read `workflow_position` and `workflow_next` using the returned Project ID. Follow the newly advertised action. See [mcp-workflow.md](mcp-workflow.md) for schema and route handling.

## Connection setup gaps

When the client has no AgileForge MCP connection, report the required endpoint and credential provisioning as a setup task. Do not install a new server or edit personal settings without authorization. See the selected release's [MCP runbook](https://github.com/arduinitavares/agileforge/blob/master/docs/mcp.md).

For explicit CLI diagnostics or recovery, use [cli-administration.md](cli-administration.md). Select the exact installation/profile before running commands; do not search nearby folders for an arbitrary `agileforge-dev`.
