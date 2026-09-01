# Specification handoff and planning

Use this procedure when the target repository has an approved external specification, including one produced by grill-me-with-docs, or when AgileForge is still planning the product.

## Keep AgileForge in control of sequence

Do not translate the specification into an independent issue list or implementation plan before AgileForge requests it. First read:

```sh
agileforge workflow position --project-id <id>
agileforge workflow next --project-id <id>
```

Use the active runtime prefix. Vision and Product Goal work may precede source registration. Follow the exact current command template.

Do not construct a registration or planning command from `--help`, guessed flags, or the source filename. `workflow next` supplies the command template and required operator inputs.

## Register the exact source

When `workflow next` advertises `specification source register`:

1. Confirm the approved source exists inside the target repository.
2. Use its repository-relative path.
3. Identify only ADRs that actually govern the source.
4. Do not rewrite, normalize, or regenerate the approved source during registration.
5. Use the preparation capability exactly as the command template requires. Do not substitute the conversational name of the tool.

The current contract accepts `grill-with-docs` as the preparation capability, but the advertised template remains authoritative.

After registration, parse the response and reread `workflow position` and `workflow next`. Structure the source only when the new route advertises that action.

When the user named this approved source as part of the bootstrap request, deterministic source registration is within that authority once advertised. Specification structuring remains provider-backed and needs provider authorization.

## Provider-backed actions

Vision drafting, interviews, Specification structuring, Backlog generation, Roadmap generation, Story generation, and Sprint planning may call a configured model provider.

Before the first provider-backed action, show the exact action, runtime/profile, project, and whether credentials are configured. Proceed only when the user has authorized that provider action or an explicit bounded series of provider actions.

Do not expose credentials. A provider-free read or test does not authorize a provider call.

## Human decisions

The agent may explain candidates and identify concrete concerns. It must not choose the user's decision.

Obtain the user's decision for:

- accept, reject, or request changes
- clarification responses that define product behavior
- Story selection or deferral
- dependency confirmation and human-owned sizing corrections
- Sprint scope, capacity, plan acceptance, and start

Use the exact review packet selected by AgileForge. After the decision command completes, reread both workflow routes.

## CLI and UI review

Use CLI JSON as authority. When the dashboard is running, guide the user to the matching candidate or decision control and check that the visible projection agrees with the CLI packet. Record any mismatch as an AgileForge defect. Do not treat button color, ordering, or disappearance as proof that a transition completed.
