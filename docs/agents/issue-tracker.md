# Issue tracker

Track repository bugs and enhancements in [GitHub Issues for arduinitavares/agileforge](https://github.com/arduinitavares/agileforge/issues). Use the `gh` CLI with `--repo arduinitavares/agileforge` so the destination is explicit.

- Read an issue with `gh issue view <number> --repo arduinitavares/agileforge --json number,title,body,state,labels,comments`.
- Search open and closed issues before creating a new one.
- Create an issue with `gh issue create --repo arduinitavares/agileforge --title "..." --body-file <path>`.
- Use `--body-file <path>` for multiline issue bodies and comments. Preserve actual newlines and do not put credentials or customer details in reports.
- Read [triage-labels.md](triage-labels.md) before applying category or state labels. Use `gh issue edit <number> --repo arduinitavares/agileforge --add-label "..."` and `--remove-label "..."` for changes.
- Read back the issue after a mutation to verify the intended result.

When a skill says to publish a ticket to the issue tracker, create a GitHub issue. This configuration concerns repository development tickets; it does not replace AgileForge's own Project, Story, or Task records.

## Pull requests as a triage surface

**PRs as a request surface: no.** Do not include external pull requests in routine issue discovery. An explicitly requested PR review remains supported and must include its existing comments.

GitHub issues and pull requests share a number space. Resolve an ambiguous number before acting.
