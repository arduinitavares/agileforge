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

### Workflow next routes to sprint generation when no sprint candidates remain

- Project: ASA `project_id=3`
- Context: after Sprint 15 was closed and post-sprint triage was recorded as
  `impact=none`.
- Command: `agileforge workflow next --project-id 3`
- Observed result: `status=post_sprint_story_continuation_available` with valid
  commands:
  - `agileforge story pending --project-id 3`
  - `agileforge sprint candidates --project-id 3`
  - `agileforge sprint generate --project-id 3`
- Command: `agileforge story pending --project-id 3`
- Observed result: Milestone 1 still has pending non-refined requirements,
  including `pyrepo-check Quality Gate Integration`, `Raw Data Ingestion
  Pipeline`, and `Canonical Process Event Record Definition and Validation`.
- Command: `agileforge sprint candidates --project-id 3`
- Observed result: `count=0`, message `Found 0 sprint candidates for
  selected-story scope. Excluded: 11 non-refined requirements.`
- Product feedback: this is a workflow-routing gap. When there are no sprint
  candidates and pending non-refined requirements remain, `workflow next` should
  route to Story generation/refinement or explicit reconciliation, not only to
  `sprint generate`.

### Story draft marked saveable despite unresolved dependency candidates

- Project: ASA `project_id=3`
- Context: generated stories for `pyrepo-check Quality Gate Integration`.
- Command: `agileforge story generate --project-id 3 --parent-requirement "pyrepo-check Quality Gate Integration"`
- Observed result: generated `attempt-1`, `is_complete=true`,
  `is_reusable=true`, `quality.coverage_status=complete`, and
  `save.available=true`.
- Follow-up command: `agileforge story save ... --attempt-id attempt-1 ...`
- Observed result: `ok=false`, error code `INVALID_COMMAND`, message
  `Dependency candidate did not resolve to an active story.`
- Cause in generated artifact: `dependency_candidates` referenced external or
  unresolved labels such as `Python Project Scaffold and uv Management
  Setup#...` and unqualified current-draft story names.
- Recovery used: refined the same requirement with explicit feedback to omit
  unresolved dependency candidates. `attempt-2` saved successfully.
- Product feedback: the Story quality/saveability gate should catch unresolved
  dependency candidates before reporting `save.available=true`, or the save
  error should be surfaced as part of the draft quality findings.

### Task update response can look like the task stayed open

- Project: ASA `project_id=3`
- Context: updating Sprint 16 task tickets `208-220` to `Done`.
- Command: `agileforge sprint task update ... --status Done ...`
- Observed result: each update returned `ok=true`, but the summarized response
  exposed a `status=To Do` and the fingerprint for the next task ticket.
- Follow-up verification: `agileforge sprint tasks --project-id 3` showed all
  13 tasks were actually `Done`.
- Product feedback: the task update response should clearly identify the
  updated task status and separately label any next-task ticket. Otherwise a
  CLI user can reasonably think the mutation was accepted but did not change
  task state.

### Sprint count/history summaries are inconsistent across commands

- Project: ASA `project_id=3`
- Context: Sprint 16 pyrepo-check planning and execution.
- Command: `agileforge sprint start --project-id 3`
- Observed result: response reported `story_count=4` and `task_count=3`.
- Command: `agileforge sprint tasks --project-id 3`
- Observed result: listed 13 actionable task rows for the same sprint.
- Command: `agileforge sprint history --project-id 3` after generating
  `sprint-attempt-1`.
- Observed result: history summary showed `attempt_count=0` even though the
  active draft/save path had just used `sprint-attempt-1`.
- Product feedback: clarify whether `task_count` means story/workstream groups
  or actionable tasks, and expose the active sprint generation attempt in
  history or a dedicated active-sprint planning summary.
