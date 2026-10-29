# AgileForge MCP

AgileForge exposes an authenticated Model Context Protocol (MCP) Streamable HTTP
endpoint at `/mcp` in the existing API process. It uses the same selected profile,
application, business database, workflow and human-review interface as the HTTP
API and dashboard. Connecting a client does not initialize a profile or generate
an artifact.

## Start the selected runtime

Use the supported [Linux container workflow](linux-containers.md#run-the-packaged-app).
With the default packaged Compose port, the endpoint is
`http://localhost:8766/mcp` and the human interface is
`http://localhost:8766/dashboard`. Use the actual published port if it differs.
Keep the existing profile, volume names and loopback port binding. Initialize an
empty installation once before clients connect; do not initialize or reset an
existing profile to enable MCP.

Provision a dedicated random bearer credential in the existing private runtime
secret file:

```dotenv
AGILEFORGE_MCP_BEARER_TOKEN=<runtime-bearer>
```

Use the runbook's [private read-only secret mount](linux-containers.md#production-image-and-installed-runtime)
and pass `--secrets-file /run/secrets/agileforge` to `serve`. The packaged runtime
requires the mounted regular file to be owned by UID 10001 with mode `0600`.
Keep its contents outside the repository, image, Compose configuration and logs.
An optional `OPEN_ROUTER_API_KEY` in that file enables provider-backed work;
discovery and reads require only the MCP bearer.

For a development checkout inside its Linux container, use that checkout's
launcher and existing profile:

```sh
./agileforge-dev info --profile local --secrets-file /absolute/private/runtime.env --json
./agileforge-dev ui --profile local --secrets-file /absolute/private/runtime.env --host 0.0.0.0 --port 8765
```

The development secret file must satisfy the launcher's private-file checks for
its current runtime user. Explicit environment credentials take precedence over
the file in the development launcher. An explicitly empty bearer disables MCP.
The installed runtime accepts only its explicit private secret input.

Every MCP request, including initialization and tool discovery, needs
`Authorization: Bearer <runtime-bearer>`. Missing configuration and missing,
incorrect or duplicate credentials return HTTP 401. The endpoint accepts only
loopback Host identities (`localhost`, `127.0.0.1`, or `[::1]`, with an optional
numeric port). A supplied Origin must be an HTTP loopback origin; CLI requests
may omit Origin. Invalid Host and Origin values return 421 and 403 respectively.
Keep the outer Docker publication on host loopback. Public and LAN hosting are
outside this transport's supported configuration.

The adapter uses MCP Python SDK 1.30.0 and supports protocol `2025-11-25` over
stateless Streamable HTTP with JSON responses. It does not expose an SSE endpoint
or a separate stdio server.

## Connect Codex

Codex supports a remote Streamable HTTP URL and a bearer credential supplied by
an environment variable. Put this example in the client configuration selected
for your connection, such as an isolated `$CODEX_HOME/config.toml`:

```toml
[mcp_servers.agileforge]
url = "http://localhost:8766/mcp"
bearer_token_env_var = "AGILEFORGE_MCP_BEARER_TOKEN"
```

Make the same runtime bearer available securely to that Codex process through
the named environment variable. Do not put its value in TOML or command
arguments. For qualification, use a separate `CODEX_HOME` and preserve existing
installed settings and authenticated sessions.

See the official [Codex MCP documentation](https://developers.openai.com/codex/mcp)
and [configuration guidance](https://developers.openai.com/codex/config-advanced).

## Connect AGY

Actual AGY compatibility is **pending verification on the maintainer's Mac**.
The maintainer uses their existing AGY installation and sign-in for this external
check. They must verify authenticated initialization, tool discovery, and a read
of the same synthetic Project used by Codex. AGY is not installed or run inside
the service container for this check.

AGY uses `serverUrl` and an explicit Authorization header for a remote server.
The field names differ from Codex:

```json
{
  "mcpServers": {
    "agileforge": {
      "serverUrl": "http://localhost:8766/mcp",
      "headers": {
        "Authorization": "Bearer <runtime-bearer>"
      }
    }
  }
}
```

Replace the placeholder only in private client settings. Treat the resulting
file as a credential: restrict access, keep it outside version control and do
not paste it into diagnostics. This example does not assume AGY expands shell
environment expressions inside header values. Keep the two clients' settings
separate. Agent verification must not change personal installed client settings.

See the official [AGY MCP documentation](https://antigravity.google/docs/mcp).

## Inspect the shared state

Initialize the connection, discover tools, then call `service_identity` and
`project_list`. The identity is the existing dashboard readiness projection:
process ID, checkout and commit, business database, trace database, and installed
state ID where applicable. Compare it with `/api/dashboard/config` and the
selected launcher's `info --json` before issuing commands.

The catalogue is fixed. Each tool advertises its typed input schema and returns
a structured JSON object with a compatible text rendering. Discovery does not
compose the application or make a generation request.

| Read tool | Required selection |
| --- | --- |
| `service_identity`, `project_list` | No arguments |
| `project_show`, `repository_status` | `project_id` |
| `vision_status`, `product_goal_status`, `specification_status` | `project_id` |
| `specification_review`, `backlog_review`, `roadmap_review`, `story_reviews`, `sprint_plan_review` | `project_id` |
| `accepted_roadmap`, `story_pending`, `sprint_history` | `project_id` |
| `artifact_history` | `project_id`, `node_id`; optional `instance_key` |
| `story_show` | `story_id` |
| `sprint_status`, `sprint_tasks` | `project_id`; optional `sprint_id` |
| `sprint_task_show`, `sprint_task_history` | `project_id`, `task_id`; optional `sprint_id` |
| `workflow_position`, `workflow_next` | `project_id` |

Read identities are positive JSON integers. `workflow_position` retains current
decisions, fingerprints and canonical reason codes. `workflow_next` retains the
existing next-action projection, including human waiting, blocked and invalid
states. Review tools reveal candidate identity and review material; they do not
decide a review.

## Submit an allowed command

Every command requires caller-owned `actor` and `idempotency_key` fields.
`correlation_id` is optional. Every command except `create_project` requires
`project_id`. Unknown fields and invalid typed values are rejected before an
application call. The discovered schema is the complete input contract.

| Command tool | Business input beyond common metadata |
| --- | --- |
| `create_project` | `name`; optional `description`, `repository_path` |
| `attach_repository` | `path` for the Project's repository |
| `bootstrap_vision` | Existing Project and verified repository state |
| `respond_to_vision`, `respond_to_product_goal` | `text` |
| `register_specification_source` | `source_path`, `preparation_capability`, `expected_decision_fingerprint`, `expected_source_fingerprint`; optional `adr_paths` |
| `structure_specification` | `expected_decision_fingerprint` |
| `generate_backlog`, `generate_roadmap`, `generate_story` | Optional exact `instance_key` where the current decision requires a selector |
| `generate_sprint` | Optional planning guidance, Story selection, capacity and team fields from its schema |
| `complete_task` | `instance_key`, `outcome_summary`, `artifact_refs`, `acceptance_result`, `checklist_result`; optional worktree evidence |
| `close_story` | `instance_key`, `resolution`, `delivered`, `evidence`, `known_gaps` |

Specification source paths remain repository-bound application inputs. They do
not provide generic file access. Freshness fingerprints must come from the
observed current state. A stale input returns the existing domain rejection;
MCP does not refresh it or select a different identity silently.

Command results retain the application's `ok`, `replayed`, `applied_node_id`,
`output`, `position` and `error` values. Domain failures keep their stable reason
codes and set MCP `isError`. Transport input failures use `INVALID_ARGUMENTS`;
unknown names use `UNKNOWN_TOOL`. Runtime credentials are redacted from wire
strings and diagnostics without modifying business facts.

An identical request with the same idempotency key replays one durable outcome.
Changing input under that key is rejected. Keep the original key across a lost
response or reconnect. Inspect `artifact_history`, status and workflow reads to
observe a long action. A transport disconnect does not grant a new attempt
owner or authorize a replacement generation. After a terminal failure, resolve
the reported cause before explicitly requesting distinct work with a new key.

Generated drafts remain pending the existing human-review interface. Open the
dashboard and complete the required human action. MCP has no human-acceptance,
review-decision, generic transition/dispatch, shell, SQL, file-execution,
maintenance, provider-configuration or billing tool.
