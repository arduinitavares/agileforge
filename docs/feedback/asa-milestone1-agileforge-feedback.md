# ASA Milestone 1 AgileForge Feedback

This log records factual AgileForge product feedback observed while completing
ASA Milestone 1 through the CLI/backend rituals.

Scope: feedback only. Do not treat this file as an AgileForge product-fix task
inside the ASA execution goal.

## 2026-06-10

### Sprint generation validation failure was recoverable but hard to diagnose

- Project: ASA `project_id=3`
- Context: post-Sprint-14 continuation from `post_sprint_story_continuation_available`
- Command: `agileforge sprint generate --project-id 3`
- Observed result: first broad sprint generation returned `ok=false`, error code
  `MUTATION_FAILED`, message `Sprint output validation failed: poor task decomposition quality`.
- Follow-up inspection: `agileforge sprint history --project-id 3` showed
  `sprint-attempt-1`, `failure_stage=output_validation`,
  `failure_summary=Sprint output validation failed: poor task decomposition quality`.
- Additional clue in the recorded input context: previous system feedback said
  `capacity analysis does not match locked Sprint selection`.
- Recovery used: generated a narrower sprint with explicit selected story IDs
  and capacity guidance. That produced a valid `SPRINT_DRAFT`.
- Product feedback: the failure was recoverable, but the CLI summary did not
  surface enough actionable validation detail by itself. Users need a clear
  way to inspect the exact validation findings and offending fields without
  reading raw history payloads.

### Active sprint state hides sprint generation attempt history

- Project: ASA `project_id=3`
- Context: after saving and starting Sprint 15.
- Command: `agileforge sprint history --project-id 3`
- Observed result: `ok=true`, `history_count=0`.
- Expected user need: while a sprint is active, it is still useful to inspect
  the generation attempt that produced that active sprint, especially after a
  prior failed generation attempt.
- Product feedback: consider exposing current active sprint generation history,
  or clarifying in CLI help/output that `sprint history` only reports draft
  generation attempts in specific workflow states.

### Guard values are correct but nested deeply for repeated task/story updates

- Project: ASA `project_id=3`
- Context: closing Sprint 14 tasks and stories.
- Commands:
  - `agileforge sprint task show --project-id 3 --task-id <task_id>`
  - `agileforge sprint story readiness --project-id 3 --story-id <story_id>`
- Observed result: required guard values are present and accurate, but task
  update guards live under `data.task_ticket.guards`, and story close guards
  live under `data.guards`.
- Product feedback: the guard model works well, but bulk CLI execution would be
  easier if `task show`, `story readiness`, or companion commands exposed a
  compact machine-readable summary mode with `id`, `status`, `fingerprint`, and
  next update command only.
