# Triage labels

Use these mappings for repository issues in `arduinitavares/agileforge`. Role names and GitHub label names are identical.

| Role | GitHub label | Meaning |
| --- | --- | --- |
| `bug` | `bug` | Existing behavior is broken. |
| `enhancement` | `enhancement` | A new feature or improvement is requested. |
| `needs-triage` | `needs-triage` | A maintainer needs to evaluate the issue. |
| `needs-info` | `needs-info` | More information is needed from the reporter. |
| `ready-for-agent` | `ready-for-agent` | The issue is fully specified for an implementation agent. |
| `ready-for-human` | `ready-for-human` | Implementation requires human judgment or access. |
| `wontfix` | `wontfix` | The issue will not be actioned. |

`bug` and `enhancement` are category roles. The remaining five roles are state roles. Every triaged issue should have exactly one category and one state. Additional topic labels may coexist with them.

An unlabeled issue normally enters `needs-triage`. Remove the previous state label when applying a new state. If existing state labels conflict, ask the maintainer to resolve the conflict before changing them. A `ready-for-agent` transition requires the brief described by the triage skill.

Keep the existing GitHub labels and their descriptions where they already exist. Do not rename labels or relabel unrelated issues as part of an individual triage request.
