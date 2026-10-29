# MCP workflow reference

Use names relative to the selected connection; client tool prefixes vary. Discover the actual input schemas. The supported service uses authenticated Streamable HTTP at `/mcp`, with stateless JSON responses. It has no separate stdio or SSE endpoint. Connection configuration and credentials belong in private client settings, outside this skill and target repository.

## Reads and commands

| Operation | MCP tools |
| --- | --- |
| Verify service and select Project | `service_identity`, `project_list`, `project_show`, `repository_status` |
| Route current work | `workflow_position`, `workflow_next` |
| Read planning state | `vision_status`, `product_goal_status`, `specification_status`, `artifact_history` |
| Read human-review candidates | `specification_review`, `backlog_review`, `roadmap_review`, `story_reviews`, `sprint_plan_review` |
| Read delivery contracts | `accepted_roadmap`, `story_pending`, `story_show`, `sprint_status`, `sprint_tasks`, `sprint_task_show`, `sprint_task_history`, `sprint_history` |
| Create/bind Project | `create_project`, `attach_repository` |
| Vision/Goal input | `bootstrap_vision`, `respond_to_vision`, `respond_to_product_goal` |
| Specification input | `register_specification_source`, `structure_specification` |
| Generate planning artifacts | `generate_backlog`, `generate_roadmap`, `generate_story`, `generate_sprint` |
| Record Task/Story delivery | `complete_task`, `close_story` |

This table describes available capabilities, not a command sequence or permission grant. Human decisions and some lifecycle transitions are intentionally absent. Use the human dashboard for its selected review action. Report an unsupported operation instead of inventing a tool or silently changing transports.

## Map routing evidence to a tool

1. Read `workflow_position` and `workflow_next` for the same Project.
2. Identify the selected business action, instance selector, and required operator inputs.
3. Locate its matching tool in the discovered catalogue. For example, `specification source register` maps to `register_specification_source`.
4. Supply only fields declared by that tool. Obtain required fingerprints and selectors from current reads. A CLI template does not specify the complete MCP input schema.
5. Confirm user authority for the action. Provider-backed work and human decisions retain their existing gates.
6. Submit once with an explicit actor and idempotency key. Parse the business result, then refresh both workflow reads after success.

Read tools use positive integer identities. Command schemas can require additional selectors, freshness fingerprints, or worktree evidence. Do not synthesize missing input from a title, filename, or remembered request.

## Results and retries

Use MCP structured content, or parse its compatible JSON text representation when structured content is unavailable. Inspect MCP `isError` as well as application `ok` and stable error codes. Preserve `replayed`, `applied_node_id`, `output`, and returned position when relevant.

For lost responses or reconnects, retain the exact request and original idempotency key. Inspect current workflow/status/history before retrying. A disconnected client does not cancel server work or authorize another generation attempt. Distinct changed work needs a new key after the reported cause is resolved.

For stale fingerprints or changed workflow, stop using the old request and reread current routing and candidate state. An authentication or authorization rejection stops the affected action; do not route around it with CLI. A temporary connection failure is not proof that the service, state, or profile was deleted.

## Endpoint boundaries

Use the selected deployment's address. A loopback MCP URL belongs to the machine running the client. A Windows client cannot reach the Mac's MCP through its own `localhost`. A working LAN dashboard does not imply supported LAN MCP access. Do not change bindings, proxies, permissions, secrets, or client settings as part of ordinary workflow use.
