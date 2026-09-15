# Repository Instructions

Follow the documentation-lookup workflow in the active user-level `AGENTS.md` when dependency behavior, configuration, setup, or versions need verification. Keep its procedure in the `find-docs` skill rather than duplicating it here.

## Pull Request Reviews

When reviewing a pull request, always fetch and consider the existing PR comments before writing the review.

## Worktrees

Remove task-created temporary worktrees after their work has been integrated or deliberately preserved elsewhere, required evidence is retained, and no worker is still using them. Do not force-remove a dirty worktree or discard unique changes merely because the task is ending.

## Development Branch Runtime

Use only uv. For a development branch or linked worktree, invoke that checkout's
`./agileforge-dev`; never use a bare or user-level `agileforge` shim. Run
`info --json` before AgileForge runtime or state mutations through its CLI.
This check is not required for unrelated documentation edits. Each worktree owns
separate profiles, business and ADK trace databases, and UI ports. Older branches
must merge or rebase the launcher change before using it.

## Typing Style

Keep repo-level style guidance short, specific, and broadly applicable.
Prefer explicit annotations for important module-level values when the type matters for readability, review, or future agent edits.
When a file-level path banner is used, keep the repository-relative path as the first line of the file.

Prefer:
`logger: logging.Logger = logging.getLogger(name=__name__)`

Over:
`logger = logging.getLogger(__name__)`

Prefer:
`# utils/response_parser.py`

Followed by the module docstring and imports.

If a style rule grows beyond a small, reusable convention, move the detailed policy into tooling or a dedicated style document and keep `AGENTS.md` as the short entrypoint.
