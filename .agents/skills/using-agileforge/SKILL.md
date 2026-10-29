---
name: using-agileforge
description: Use when the user asks an agent to start, continue, or deliver a target repository through AgileForge, including approved specification handoff and AgileForge-planned Sprint work.
---

# Using AgileForge

Guide the target repository through the selected AgileForge service. Use **MCP first** for Project and workflow operations. Keep CLI support for administration, diagnostics, and recovery. AgileForge owns workflow sequencing; repository instructions govern implementation; the human owns product decisions.

Use this skill only when the user asks AgileForge to manage the work.

## Connection and identity

1. Read the target repository's instructions and Git state. Preserve unrelated changes.
2. Use the user's selected AgileForge MCP connection. Discover its tools and schemas; tool prefixes depend on the client.
3. Call `service_identity` before Project operations. Verify the selected runtime/profile and state against the configured deployment. A reachable endpoint or familiar Project name is insufficient.
4. Call `project_list` and, when needed, `project_show`. Select the exact repository binding. For a remote or container service, verify the mapping between client and server paths. Never default to Project 1 or send a Mac/Windows path as an assumed Linux server path.
5. If the connection is absent, ambiguous, or rejects authentication, report the specific gap. Do not change client settings or switch to an unverified CLI profile.

Read [project-bootstrap.md](references/project-bootstrap.md) for Project selection or creation. Read [mcp-workflow.md](references/mcp-workflow.md) for supported tools, errors, and command mapping. Configuration belongs to the selected installation; this skill contains no bearer credentials or machine-specific service address.

## Workflow loop

- Read both `workflow_position` and `workflow_next` for the selected `project_id` before a mutation and after a successful mutation.
- Follow the current action, selectors, reason codes, and fingerprints. Do not substitute dashboard ordering, status summaries, or a remembered route.
- Match the advertised business action to its discovered MCP tool and input schema. `workflow_next` may return a CLI command template; it is routing evidence, not an instruction to launch CLI. Do not mechanically translate flags or invent a generic dispatch tool.
- Read the needed candidate or Task through MCP. Keep its exact identity and human-review binding.
- Mutate only within existing user authorization. Every command needs an explicit `actor` and `idempotency_key`. Use a new key for distinct work. Preserve the same key and identical input across an uncertain response or reconnect.
- Parse MCP `isError` and the structured application's `ok`, `error`, `replayed`, and `output` before continuing. A transport success is not business success.
- If the selected action has no MCP command, use the existing human dashboard or report the missing capability. MCP has no human-acceptance or generic transition tool. CLI access does not grant authority to decide a review.

Read [lifecycle.md](references/lifecycle.md) for orientation, not for a fixed sequence. Reject obsolete v1 bootstrap recipes involving `specs/spec.json`, `spec profile validate`, `project create --dry-run`, or authority compilation. The v2 lifecycle creates the Project before Vision, Product Goal, and exact Specification source registration.

## Planning and delivery

Read [specification-handoff.md](references/specification-handoff.md) for an approved external specification or planning work. Present the exact candidate and requested decision. Obtain human acceptance, rejection, clarification, Story selection, sizing, dependencies, and Sprint decisions through the existing review interface.

Read [sprint-delivery.md](references/sprint-delivery.md) for active Sprint Tasks. Implement only the current Task and its parent Story contract. Follow the target repository's build, test, review, and commit rules.

An instruction to start or create a missing Project authorizes that matching creation. It does not authorize provider generation, product acceptance, external issue filing, Git publication, cleanup, or deployment. Reuse existing applicable approvals; ask only for missing authority.

## Administration and recovery

Read [cli-administration.md](references/cli-administration.md) for explicit CLI administration, diagnostics, or recovery. Verify that it addresses the same runtime/profile and business state before use. A host CLI may target different data from the MCP service.

Read [approvals-and-recovery.md](references/approvals-and-recovery.md) for stale state, uncertain results, provider errors, and defects. An access denial stops the affected operation. CLI must not bypass MCP authentication, a denied action, or a required human decision.

## Step report

Report the target repository, branch and HEAD; selected service/profile and Project; workflow position and reason; next action or human decision; and bounded MCP evidence. Include CLI diagnostics only when used. Keep credentials and large private packets out of the report.
