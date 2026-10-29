# Install and update the AgileForge agent skill

The canonical `using-agileforge` package is versioned in this repository at
`.agents/skills/using-agileforge`. Install that package through the
[Skills CLI](https://github.com/vercel-labs/skills). Users do not need a separate
AgileForge source checkout to install it.

## Install once for your user

For Codex:

```sh
npx skills@latest add arduinitavares/agileforge --skill using-agileforge --agent codex --global
```

The installer tracks the GitHub source and places the shared package under
`~/.agents/skills/using-agileforge`. Select other supported agents through the
installer when needed. Windows uses the corresponding directory under the
user's home. Symlink support varies by agent and host; the installer can copy
where linking is unavailable.

Keep one user-level installation per machine. Avoid installing redundant copies
into every target repository. A repository-scoped skill can shadow a global
skill; inspect deliberate project overrides before removing or replacing them.
Preserve local customizations before replacing a manual installation.

## Update only this skill

```sh
npx skills@latest update using-agileforge --global
```

Inspect installed global skills with:

```sh
npx skills@latest list --global --agent codex
```

Start a fresh client session after updating and confirm which skill path loads.
An existing conversation may already contain older instructions.

The update command obtains the published source; it cannot retrieve uncommitted
maintainer edits. A new revision becomes available after the skill changes are
committed, published and merged into the repository's default branch. A copied
local folder has no automatic upstream tracking; migrate it through the GitHub
installation command after the desired revision is published.

For skill contributors, edit the canonical repository package and submit a
reviewed change. Other source checkouts receive it through their normal Git
update process. Do not mass-edit historical branches or linked worktrees to
simulate an update.

## Configure MCP separately

Installing the skill does not install or start AgileForge, configure a client
connection, provision credentials, or grant permissions. Follow the deployed
release's [MCP runbook](https://github.com/arduinitavares/agileforge/blob/master/docs/mcp.md) for those operations. Select the actual service
and verify its identity before commands.

The skill uses MCP first for ordinary Project and workflow operations. CLI
support remains for administration, diagnostics and recovery. Product decisions,
provider calls and destructive actions retain their existing authorization.

A LAN dashboard URL does not establish LAN MCP support. A loopback MCP endpoint
belongs to the machine running the client. Do not configure Windows to use its
own `localhost` as a route to the Mac's MCP server.

## Rigs and other machines

Each machine has its own installation and update state. Run the targeted update
on that machine. Rig images must package a reviewed skill revision through their
normal build process; changing a Mac skill does not update an existing container.
Activate a new rig skill version at a job/session boundary. Do not overwrite the
skills or context of a running job merely to synchronize versions.
