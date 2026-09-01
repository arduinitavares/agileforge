---
name: using-agileforge
description: Use when the user asks Codex to start, continue, or deliver a target repository through AgileForge, including bootstrapping from an approved specification or implementing AgileForge-planned Sprint work.
---

# Using AgileForge

## Purpose

Guide one target repository through AgileForge while keeping product decisions with the user. AgileForge chooses the workflow route. The target repository's instructions choose how code is built and tested.

Do not use this skill for ordinary repository work that the user did not ask AgileForge to manage.

## Core contract

1. Treat the current repository as the product being developed. Do not assume it contains AgileForge's source checkout or `./agileforge-dev`.
2. Read the target repository's instructions and Git state before acting. Preserve unrelated user changes.
3. From a target repository, run `command -v agileforge` and use the stable `agileforge` command. The stable CLI has no `info` subcommand. Do not search for or invoke a nearby `./agileforge-dev`. Use `info --json` only with a selected development launcher and profile.
4. Reject legacy v1 bootstrap instructions involving `specs/spec.json`, `spec profile validate`, `project create --dry-run`, or authority compilation. The current v2 lifecycle creates the Project first and routes through Vision, Product Goal, and exact Specification source registration.
5. Use JSON CLI reads as workflow authority. Bound potentially large output and parse selected fields. The browser is a human review surface and a cross-check, not routing authority.
6. For an existing project, always run both `workflow position --project-id <id>` and `workflow next --project-id <id>`. Do not substitute `status`, help output, a UI control, or a previous route. Execute only the current `workflow next` command template.
7. Parse every mutation result before continuing. After a successful mutation, read both routing commands again.
8. Use a new idempotency key for each distinct request. Reuse the same key only when retrying the exact request after an uncertain transport result.
9. Treat a user's instruction as authority only for the actions it names. "Create the AgileForge project if it does not exist" authorizes one matching local project creation. Do not ask for that approval again. Do not stretch it to provider calls, product decisions, external records, merges, pushes, or cleanup.

## Operating loop

### Start or resume

Read [project-bootstrap.md](references/project-bootstrap.md). Resolve the target repository, AgileForge runtime, profile when applicable, and exact project identity.

If no matching project exists, create one when the user asked to start or create it. Resolve required inputs, run the creation, and report the result without asking for duplicate approval. If identity is ambiguous, stop and ask instead of selecting by name or numeric order.

For a stable runtime, the mandatory command shapes are:

```sh
command -v agileforge
agileforge --version
agileforge project list

agileforge project create \
  --name "<approved-product-name>" \
  --repository-path "<absolute-target-repository>" \
  --idempotency-key "<new-key>" \
  --actor "codex"

agileforge workflow position --project-id <returned-or-matched-id>
agileforge workflow next --project-id <returned-or-matched-id>
```

Run `project create` only when no exact repository binding exists and creation is authorized. The development runtime uses the same semantic commands after `./agileforge-dev cli --profile <profile> --` and requires `./agileforge-dev info --profile <profile> --json` first.

There is no `agileforge info`, `project bootstrap`, `--repository`, or `--spec` command in this workflow. Do not invent substitutes. Never omit `--idempotency-key` or `--actor` from a mutation.

### Follow product planning

Read [specification-handoff.md](references/specification-handoff.md) when an approved external specification exists or the workflow is in Vision, Product Goal, Specification, Backlog, Roadmap, Story, or Sprint planning.

Present the exact candidate and decision requested by AgileForge. The user decides acceptance, rejection, feedback, Story selection, dependencies, sizing corrections, and Sprint commitment.

An approved source is not registered until `workflow next` advertises `specification source register`. Register with `--source-path`, the advertised preparation capability, a new idempotency key, and actor. Then reread both routes. Do not run provider-backed `specification structure` until the user authorizes that provider action.

### Deliver Sprint work

Read [sprint-delivery.md](references/sprint-delivery.md) when AgileForge reports an active Sprint Task or execution transition. Implement only the current Task and its parent Story contract. Use the target repository's own workflow, test commands, and commit rules.

### Handle defects and recovery

Read [approvals-and-recovery.md](references/approvals-and-recovery.md) for stale state, retries, provider failures, CLI and UI disagreement, AgileForge defects, issue filing, or any unclear authority boundary.

## Step report

After each meaningful step, report only:

- target repository, branch, and HEAD
- AgileForge runtime, profile when applicable, and project ID
- current workflow position and reason code
- the exact next action or decision
- CLI evidence and, when available, the UI cross-check
- what needs the user's decision

Do not bury the next action in a lifecycle recap.
