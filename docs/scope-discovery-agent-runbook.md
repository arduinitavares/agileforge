# Scope Discovery Agent Runbook

This runbook is for agents turning resolved product discovery into AgileForge
state. It covers both existing-project scope extension and greenfield project
creation.

The split is intentional:

- Codex or another interactive agent runs `grill-with-docs` and `to-prd`.
- AgileForge stores, validates, gates, and routes the resulting artifacts.
- AgileForge does not run the interactive skills and does not generate the
  Spec Amendment Draft itself.

Do not create backlog, roadmap, stories, tasks, or sprints directly from an
idea, chat transcript, PRD draft, or Spec Amendment Draft. Executable work must
come from accepted authority.

## When Discovery Is Already Resolved

If the project already has high-quality `grill-with-docs` documentation, do not
restart the interview by default. Read the resolved context and continue from
artifact emission.

Restart or extend the interview only when one of these is true:

- open questions remain;
- evidence conflicts are unresolved;
- new domain terms were settled but not committed to `CONTEXT.md`;
- the documentation does not support a `ready_for_prd` Challenge Artifact.

## Artifact Files

Keep generated source files in the caller project, using a stable folder such as
`docs/scope-discovery/<scope-key>/` unless the project has a stronger local
convention. After recording, AgileForge state is the workflow source of truth;
the files remain useful review/provenance material.

### Challenge Artifact JSON

Record a JSON object produced by `grill-with-docs`:

```json
{
  "producer": "grill-with-docs",
  "readiness": "ready_for_prd",
  "original_idea": "Add product reporting after the accepted scope is exhausted.",
  "content": {
    "questions": [
      {
        "question": "What new scope is being introduced?",
        "answer": "Add product reporting for accepted project data."
      }
    ],
    "reviewed_evidence": [
      {
        "source": "CONTEXT.md",
        "summary": "Defines the project language used by the new scope."
      }
    ],
    "evidence_conflicts": [],
    "assumptions": ["Existing accepted authority remains the execution source."],
    "non_goals": ["Do not bypass PRD or authority acceptance."],
    "risks": [
      {
        "risk": "Agent output could be treated as accepted scope too early.",
        "mitigation": "Persist drafts and require explicit human acceptance."
      }
    ],
    "open_questions": [],
    "glossary_changes": [
      {
        "term": "Product Reporting",
        "change": "Settled language for the new reporting capability.",
        "committed_to_project_glossary": true,
        "evidence": "CONTEXT.md"
      }
    ]
  }
}
```

For `readiness: "ready_for_prd"`, AgileForge requires:

- `questions` is a non-empty list with `question` and `answer`;
- `reviewed_evidence` is a non-empty list with `source` and `summary` or
  `finding`;
- `evidence_conflicts` is a list, and each conflict is resolved by
  `resolved: true` or a non-empty `resolution`;
- `assumptions`, `non_goals`, `risks`, `open_questions`, and
  `glossary_changes` are lists;
- `open_questions` is empty;
- each glossary change is committed with
  `committed_to_project_glossary: true` and non-empty `evidence`.

See `docs/examples/scope-discovery/challenge-artifact.example.json` for a
complete example.

### PRD Draft JSON

Record a JSON object produced by `to-prd`:

```json
{
  "producer": "to-prd",
  "source_challenge_artifact_id": 123,
  "title": "Product Reporting PRD",
  "content": {
    "problem_statement": "Users need reviewed reporting from accepted project data.",
    "solution": "Add a reporting workflow grounded in accepted authority.",
    "user_stories": [],
    "implementation_decisions": [],
    "testing_decisions": [],
    "out_of_scope": [],
    "further_notes": []
  }
}
```

`source_challenge_artifact_id` must match the AgileForge Challenge Artifact ID
returned by the record command.

### Spec Amendment Draft File

The agent translates an accepted PRD into a structured AgileForge spec amendment
file. AgileForge records and validates that file against the current accepted
spec. The draft remains non-authoritative until a human accepts it.

## Existing Project Flow

Start from the caller project directory so artifact paths are natural to the
project using AgileForge.

```sh
agileforge workflow next --project-id <project_id>
```

Record the Challenge Artifact:

```sh
agileforge discovery challenge record \
  --project-id <project_id> \
  --artifact-file docs/scope-discovery/<scope-key>/challenge-artifact.json \
  --idempotency-key <scope-key>-challenge-record-001
```

Record the PRD draft:

```sh
agileforge discovery prd draft record \
  --project-id <project_id> \
  --challenge-artifact-id <challenge_artifact_id> \
  --prd-file docs/scope-discovery/<scope-key>/prd.json \
  --idempotency-key <scope-key>-prd-draft-record-001
```

Stop for human PRD review. Only run this after explicit approval:

```sh
agileforge discovery prd accept \
  --project-id <project_id> \
  --prd-id <prd_id> \
  --reviewer <reviewer> \
  --acceptance-notes "<acceptance_notes>" \
  --idempotency-key <scope-key>-prd-accept-001
```

Record the agent-generated Spec Amendment Draft:

```sh
agileforge discovery spec-amendment draft record \
  --project-id <project_id> \
  --prd-id <prd_id> \
  --amendment-file docs/scope-discovery/<scope-key>/spec-amendment.json \
  --idempotency-key <scope-key>-spec-amendment-draft-record-001
```

Stop for human Spec Amendment review. Only run this after explicit approval:

```sh
agileforge discovery spec-amendment accept \
  --project-id <project_id> \
  --spec-amendment-draft-id <spec_amendment_draft_id> \
  --reviewer <reviewer> \
  --acceptance-notes "<acceptance_notes>" \
  --idempotency-key <scope-key>-spec-amendment-accept-001
```

Then ask AgileForge for the next route and follow the exact advertised command.
For an exhausted existing project, the bridge is usually:

```sh
agileforge scope extension start \
  --project-id <project_id> \
  --spec-amendment-draft-id <spec_amendment_draft_id> \
  --expected-state <state_from_workflow_next> \
  --idempotency-key <scope-key>-scope-extension-start-001
```

## Greenfield Flow

Use a stable `context-key` before the project exists. A good key is a lowercase
slug for the proposed product or scope.

```sh
agileforge discovery greenfield challenge record \
  --context-key <context_key> \
  --artifact-file docs/scope-discovery/<context-key>/challenge-artifact.json \
  --idempotency-key <context-key>-challenge-record-001

agileforge discovery greenfield prd draft record \
  --context-key <context_key> \
  --challenge-artifact-id <challenge_artifact_id> \
  --prd-file docs/scope-discovery/<context-key>/prd.json \
  --idempotency-key <context-key>-prd-draft-record-001
```

Stop for human PRD review. Only run this after explicit approval:

```sh
agileforge discovery greenfield prd accept \
  --context-key <context_key> \
  --prd-id <prd_id> \
  --reviewer <reviewer> \
  --acceptance-notes "<acceptance_notes>" \
  --idempotency-key <context-key>-prd-accept-001
```

Record the greenfield Spec Amendment Draft:

```sh
agileforge discovery greenfield spec-amendment draft record \
  --context-key <context_key> \
  --prd-id <prd_id> \
  --amendment-file docs/scope-discovery/<context-key>/spec-amendment.json \
  --idempotency-key <context-key>-spec-amendment-draft-record-001
```

Stop for human Spec Amendment review. Only run this after explicit approval:

```sh
agileforge discovery greenfield spec-amendment accept \
  --context-key <context_key> \
  --spec-amendment-draft-id <spec_amendment_draft_id> \
  --reviewer <reviewer> \
  --acceptance-notes "<acceptance_notes>" \
  --idempotency-key <context-key>-spec-amendment-accept-001
```

Create the project only from the accepted greenfield amendment:

```sh
agileforge project create \
  --name "<project_name>" \
  --setup-mode greenfield \
  --greenfield-spec-amendment-draft-id <spec_amendment_draft_id> \
  --idempotency-key <context-key>-project-create-001
```

## Agent Prompt Template

Use this when asking an agent to move from resolved `grill-with-docs`
documentation into AgileForge.

```text
Use the resolved grill-with-docs documentation as source context.

Do not create backlog, roadmap, stories, tasks, sprints, or issues directly
from the conversation or markdown. AgileForge must receive structured discovery
artifacts first.

Steps:

1. Read docs/scope-discovery-agent-runbook.md first and follow it as the
   operating contract for this handoff.
2. Read CONTEXT.md, docs/prds, docs/adr, and any project-specific discovery
   documents I provide.
3. If the grill-with-docs interview is already resolved, do not restart it
   unless open questions, unresolved evidence conflicts, or uncommitted glossary
   changes remain.
4. Emit a Challenge Artifact JSON file with producer "grill-with-docs" and the
   required rich content fields. Use readiness "ready_for_prd" only when there
   are no blockers.
5. Record the Challenge Artifact with the appropriate AgileForge discovery
   command.
6. Run to-prd from the resolved context and emit a PRD JSON file with producer
   "to-prd", source_challenge_artifact_id, title, and content.
7. Record the PRD draft with AgileForge.
8. Stop for my explicit PRD review. Do not accept or reject the PRD without my
   approval.
9. After I approve, accept the PRD, generate a structured Spec Amendment Draft
   file from that accepted PRD, and record it with AgileForge.
10. Stop for my explicit Spec Amendment review. Do not accept or reject the Spec
   Amendment without my approval.
11. After I approve, accept the Spec Amendment and run `agileforge workflow
    next --project-id <project_id>` or the greenfield project creation command
    advertised by the current flow.

Return the recorded AgileForge IDs and the exact next command after each
mutation. Parse JSON envelopes. Do not scrape human help text when command
output provides structured data.
```
