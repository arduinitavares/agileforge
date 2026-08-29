# Sprint Review INVEST Regression Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Project each selected Story's exact accepted immutable `invest_assessment` into Sprint-plan review data so API, CLI, browser, and Playwright review surfaces show valid #221 evidence without falsely describing Sprint acceptance as disabled.

**Architecture:** Extend only `DurableReadProjectionService.sprint_plan_review`: after its existing accepted-artifact and item-fingerprint checks, serialize `source_item.invest_assessment` into `candidate.selected_stories[]`. Keep browser and CLI production renderers unchanged because they already consume this field correctly; add focused service, CLI, Node, and profile-free Playwright guards around the corrected contract.

**Tech Stack:** Python 3.13.15, Pydantic v2, SQLModel, pytest, Node.js built-in test runner, Playwright, checkout-local `./agileforge-dev`, and uv.

**Spec:** [`docs/superpowers/specs/2026-06-08-story-draft-quality-contract-design.md`](../specs/2026-06-08-story-draft-quality-contract-design.md) and [GitHub issue #221](https://github.com/arduinitavares/agileforge/issues/221). Supporting audit: `/tmp/agileforge-221-sprint-review-audit.md`.

## Global Constraints

- Start only from `/Users/aaat/projects/agileforge/.worktrees/issue-221-sprint-review-invest-regression` at exact HEAD `4d4b6ce40164b50f68c931eb54f81950bb0ffbeb`.
- TDD is mandatory: establish the immutable read-projection RED before editing production code.
- Make no provider calls.
- Do not initialize, inspect, or mutate any AgileForge profile, business database, trace database, or Sprint state during implementation.
- The Playwright guard must launch Chromium directly against injected local markup and `frontend/project.js`; it must not use `dashboard_harness`, `./agileforge-dev init`, `./agileforge-dev ui`, or a retained profile.
- Change exactly one production field in `services/read_projections.py`; derive it from the already verified immutable `source_item`.
- Do not change database schemas, migrations, persisted Sprint-plan envelopes, Sprint planner input/output contracts, provider prompts or adapters, packet contracts or renderers, API routes or schema version, frontend production code, or CLI production code.
- Preserve `agileforge.planning-artifact-review.v2`; the new field is additive.
- Do not add an INVEST-based Sprint acceptance gate. Per-dimension `pass`, `concern`, and `fail` remain advisory human-review evidence under #221.
- Preserve #225 first-Sprint capacity behavior, including positive-integer `max_story_points`, editable recommendations, unavailable-capacity fail-closed behavior, and unchanged Sprint-generation transport.
- Keep all verification provider-free. Use uv for focused Python/Node commands and checkout-local `./agileforge-dev check --json` for the repository gate.
- After focused and static verification passes, audit and commit the implementation, tests, and this plan locally. Run the full repository aggregate only on that clean local commit because its acceptance-launcher smoke tests require a clean checkout. Do not push, merge, mutate profiles, take any Sprint action, or clean up the worktree.

---

### Task 1: Project Accepted INVEST Evidence Through Every Sprint-review Surface

**Files:**
- Modify: `services/read_projections.py:2638-2655`
- Test: `tests/services/test_durable_product_definition_projections.py:4450-4542`
- Test: `tests/adapters/test_cli_workflow_domain.py:1301-1422,1573-1591`
- Test: `tests/test_workflow_position_display.mjs:44-77,1611-1623`
- Test: `tests/e2e/test_single_project_lifecycle_ui.py:113-145` and add the focused test near the existing Sprint browser tests

**Interfaces:**
- Consumes: `source_item: CanonicalStoryItem`, already loaded from the accepted immutable `StoryArtifact` and verified against `UserStory.source_story_artifact_fingerprint` and `UserStory.source_story_item_fingerprint`.
- Consumes: `source_item.invest_assessment: StoryInvestAssessment`.
- Produces: `candidate.selected_stories[*].invest_assessment: dict[str, dict[str, str]]` from `source_item.invest_assessment.model_dump(mode="json")`, with exactly the six keys `independent`, `negotiable`, `valuable`, `estimable`, `small`, and `testable`.
- Preserves: the existing `agileforge.planning-artifact-review.v2` envelope, Sprint acceptance behavior, packet evidence, frontend/CLI production renderers, and #225 capacity transport.

- [ ] **Step 1: Verify the execution boundary and authorities**

Run from the isolated worktree:

```bash
pwd
git rev-parse HEAD
git status --short --branch
sed -n '1,16p;94,124p' docs/superpowers/specs/2026-06-08-story-draft-quality-contract-design.md
sed -n '1,74p;151,236p' /tmp/agileforge-221-sprint-review-audit.md
```

Expected:

- `pwd` is `/Users/aaat/projects/agileforge/.worktrees/issue-221-sprint-review-invest-regression`.
- HEAD is `4d4b6ce40164b50f68c931eb54f81950bb0ffbeb`.
- The only pre-existing worktree change is this untracked plan file.
- The design authority states that every Story has a complete six-dimension assessment, semantic results are advisory, and malformed evidence gates Story review rather than Sprint review.

- [ ] **Step 2: Add the immutable service RED**

In `tests/services/test_durable_product_definition_projections.py`, extend the existing import from `tests.workflow.test_planning_transitions` with `_invest_assessment`:

```python
    from tests.workflow.test_planning_transitions import (  # noqa: PLC0415
        _domain,
        _guards,
        _invest_assessment,
        _record_and_accept_roadmap,
        _record_and_accept_story,
        _record_sprint_plan_draft,
        _seed_accepted_backlog,
    )
```

In `test_sprint_plan_review_is_durable_before_activation_and_after_drift`, place this exact assertion immediately after `assert selected_story["story_id"] == story_id`:

```python
    assert selected_story["invest_assessment"] == _invest_assessment().model_dump(
        mode="json"
    )
```

This compares the complete accepted assessment, including every result, rationale, and evidence string. Keep the existing terminal reread assertion `assert _json_object(terminal_selected[0]) == selected_story`; it proves the assessment remains pinned after operational `UserStory` drift.

- [ ] **Step 3: Run the service test and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest -p no:cacheprovider -q \
  tests/services/test_durable_product_definition_projections.py::test_sprint_plan_review_is_durable_before_activation_and_after_drift
```

Expected: **FAIL** at the new assertion with `KeyError: 'invest_assessment'`. A failure caused by provider access, profile lookup, database reuse, or another missing key is not the required RED; stop and diagnose the test setup before production edits.

- [ ] **Step 4: Add the one-field immutable projection**

In `services/read_projections.py`, add exactly this field after `acceptance_criteria` and before `specification_evidence` in the selected-Story object:

```python
                            "invest_assessment": (
                                source_item.invest_assessment.model_dump(mode="json")
                            ),
```

The resulting local block must be:

```python
                            "acceptance_criteria": list(
                                source_item.acceptance_criteria
                            ),
                            "invest_assessment": (
                                source_item.invest_assessment.model_dump(mode="json")
                            ),
                            "specification_evidence": story_evidence,
```

Do not read assessment data from `UserStory`, `SprintPlannerSelectedStory`, the provider, a packet, or a new persistence field.

- [ ] **Step 5: Run the service test and verify GREEN**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest -p no:cacheprovider -q \
  tests/services/test_durable_product_definition_projections.py::test_sprint_plan_review_is_durable_before_activation_and_after_drift
```

Expected: **PASS**. The existing post-acceptance operational-drift and corrupt-artifact checks must remain green.

- [ ] **Step 6: Add the focused CLI contract guard**

In `tests/adapters/test_cli_workflow_domain.py`, add this helper immediately before `_planning_review`:

```python
def _valid_invest_assessment() -> dict[str, object]:
    return {
        "independent": {
            "result": "pass",
            "rationale": "Delivers self-contained increment.",
            "evidence": "No unbuilt dependencies.",
        },
        "negotiable": {
            "result": "pass",
            "rationale": "Implementation details open to refinement.",
            "evidence": "Focuses on user outcome.",
        },
        "valuable": {
            "result": "pass",
            "rationale": "Directly delivers user capability.",
            "evidence": "Addresses requirement.",
        },
        "estimable": {
            "result": "pass",
            "rationale": "Scope is clear and bounded.",
            "evidence": "Discrete criteria.",
        },
        "small": {
            "result": "pass",
            "rationale": "Sized for single iteration.",
            "evidence": "Effort is M.",
        },
        "testable": {
            "result": "pass",
            "rationale": "Verifiable pass/fail criteria.",
            "evidence": "Observable verification steps.",
        },
    }
```

Replace the existing inline Story-phase assessment dictionary with:

```python
                    "invest_assessment": _valid_invest_assessment(),
```

Add the same field to the Sprint-phase selected Story after `acceptance_criteria`:

```python
                    "invest_assessment": _valid_invest_assessment(),
```

Rename `test_sprint_plan_review_renders_owner_kind_and_human_display` to
`test_sprint_plan_review_renders_owner_and_accepted_invest_assessment`, and add these assertions after the existing owner assertions:

```python
    assert "INVEST assessment:" in output
    assert (
        "- Independent [PASS]: Delivers self-contained increment. "
        "(Evidence: No unbuilt dependencies.)"
    ) in output
    assert "[INVALID / MISSING]" not in output
    assert "required quality evidence is incomplete" not in output
```

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest -p no:cacheprovider -q \
  tests/adapters/test_cli_workflow_domain.py::test_sprint_plan_review_renders_owner_and_accepted_invest_assessment
```

Expected: **PASS** without changing `cli/main.py`.

- [ ] **Step 7: Add the focused Node browser-contract guard**

In `tests/test_workflow_position_display.mjs`, replace the existing test named
`Sprint review renders human owner display without the durable key` with:

```javascript
test('Sprint review renders accepted INVEST evidence and keeps acceptance enabled', async () => {
    const context = loadFrontend();
    const sprintOwner = await validatedSprintOwner(context);
    const acceptedStory = storyReview('backlog_item:PBI-000003')
        .review.candidate.story_items[0];
    const selected = {
        binding: {
            decision_fingerprint: 'sha256:sprint-review-decision',
            instance_key: null,
        },
        review: {
            phase: 'sprint_plan',
            project_id: 7,
            candidate: {
                team_name: sprintOwner.label,
                sprint_owner: sprintOwner,
                sprint_goal: 'Ship the browser boundary.',
                selected_stories: [{
                    ...acceptedStory,
                    reason_for_selection: 'Highest accepted value.',
                    tasks: [],
                }],
            },
        },
    };

    const markup = context.planningReviewCardMarkup(
        'Sprint plan review', selected, 'sprint', 0,
    );
    assert.ok(markup.includes('Sprint owner'));
    assert.ok(markup.includes('Solo project'));
    assert.ok(markup.includes('Solo operator for Exact Project'));
    assert.ok(!markup.includes('agileforge:sprint-owner:'));
    assert.ok(markup.includes('data-invest-assessment="true"'));
    assert.ok(markup.includes('Self-contained logic.'));
    assert.ok(!markup.includes('Quality Assessment Incomplete'));
    assert.ok(!markup.includes('Acceptance is disabled.'));
    assert.ok(markup.includes('data-review-decision="accepted" class='));
    assert.ok(!markup.includes('data-review-decision="accepted" disabled'));
    assert.notStrictEqual(
        context.planningReviewBinding(selected, 'sprint', 'accepted'),
        null,
    );
});
```

Run the new guard together with #225's focused capacity guards:

```bash
uv run --frozen node --test \
  --test-name-pattern='Sprint review renders accepted INVEST evidence and keeps acceptance enabled|Sprint generation prepopulates an editable metrics capacity|Sprint generation fails closed for unavailable capacity projections|Sprint generation rejects a string-valued server capacity recommendation' \
  tests/test_workflow_position_display.mjs
```

Expected: **4 tests passed**, with no skipped matching test and no change to `frontend/project.js`.

- [ ] **Step 8: Add a profile-free Playwright guard**

In `tests/e2e/test_single_project_lifecycle_ui.py`, add this test near the existing Sprint browser tests. It deliberately does not request `dashboard_harness` and does not construct `FakeLifecycle`, so it cannot initialize a profile or database:

```python
def test_sprint_review_browser_shows_accepted_invest_without_false_gate() -> None:
    """Render accepted INVEST evidence without changing profile or Sprint state."""
    source = (_PROJECT_ROOT / "frontend" / "project.js").read_text(encoding="utf-8")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport=_DESKTOP_VIEWPORT)
            page.set_content('<main id="review-root"></main>')
            page.add_script_tag(content=source)
            page.evaluate(
                """async (assessment) => {
                    const owner = {
                        kind: 'solo_project',
                        key: 'agileforge:sprint-owner:solo-project:v1:project:1',
                        label: '[agileforge:sprint-owner:solo-project:v1:project:1] Solo operator for Exact Project',
                        display_label: 'Solo operator for Exact Project',
                    };
                    if (!await validateSprintOwnerProjection(owner, 1)) {
                        throw new Error('Sprint owner fixture is invalid.');
                    }
                    const selected = {
                        binding: {
                            decision_fingerprint: 'sha256:sprint-review-decision',
                            instance_key: null,
                        },
                        review: {
                            phase: 'sprint_plan',
                            project_id: 1,
                            candidate: {
                                sprint_owner: owner,
                                sprint_goal: 'Deliver exact accepted evidence.',
                                selected_stories: [{
                                    title: 'Delivery story draft',
                                    statement: 'As an operator, I want exact review evidence.',
                                    persona: 'operator',
                                    acceptance_criteria: ['Accepted evidence is visible.'],
                                    specification_evidence: [],
                                    invest_assessment: assessment,
                                    reason_for_selection: 'Highest accepted value.',
                                    tasks: [{
                                        description: 'Render the accepted evidence',
                                        task_kind: 'implementation',
                                        checklist_items: ['Verify review output'],
                                        specification_evidence: [],
                                    }],
                                }],
                            },
                        },
                    };
                    document.querySelector('#review-root').innerHTML =
                        planningReviewCardMarkup(
                            'Sprint plan review', selected, 'sprint', 0,
                        );
                }""",
                _valid_invest_assessment_payload(),
            )

            review = page.locator('[data-planning-review-card="sprint"]')
            expect(review).to_be_visible()
            expect(review.locator('[data-invest-assessment="true"]')).to_be_visible()
            expect(review).to_contain_text("Self-contained Story increment.")
            expect(review).not_to_contain_text("Quality Assessment Incomplete")
            expect(review).not_to_contain_text("Acceptance is disabled.")
            expect(
                review.locator(
                    '[data-planning-review="sprint"]'
                    '[data-review-decision="accepted"]'
                )
            ).to_be_enabled()
        finally:
            browser.close()
```

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest -p no:cacheprovider -q \
  tests/e2e/test_single_project_lifecycle_ui.py::test_sprint_review_browser_shows_accepted_invest_without_false_gate
```

Expected: **PASS** with a headless local Chromium process only. The command must not create `.agileforge-dev`, a profile manifest, a business database, a trace database, or Sprint rows.

- [ ] **Step 9: Run the complete focused GREEN set**

Run the Python guards together:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest -p no:cacheprovider -q \
  tests/services/test_durable_product_definition_projections.py::test_sprint_plan_review_is_durable_before_activation_and_after_drift \
  tests/adapters/test_cli_workflow_domain.py::test_sprint_plan_review_renders_owner_and_accepted_invest_assessment \
  tests/e2e/test_single_project_lifecycle_ui.py::test_sprint_review_browser_shows_accepted_invest_without_false_gate
```

Expected: **3 passed**.

Run the complete Node file so the new Sprint guard and every #225 capacity guard execute under the repository-supported runner:

```bash
uv run --frozen node --test tests/test_workflow_position_display.mjs
```

Expected: **PASS** for the full file with zero failures.

- [ ] **Step 10: Audit the final diff, commit locally, and record the stable HEAD**

Run:

```bash
git diff --check
git diff --name-only | sed -n '1,40p'
git diff --stat
git status --short --branch
```

Expected worktree paths are exactly:

```text
docs/superpowers/plans/2026-08-29-sprint-review-invest-regression.md
services/read_projections.py
tests/adapters/test_cli_workflow_domain.py
tests/e2e/test_single_project_lifecycle_ui.py
tests/services/test_durable_product_definition_projections.py
tests/test_workflow_position_display.mjs
```

Because this plan starts untracked, `git diff --name-only` lists the five
implementation/test files and `git status --short` lists the plan with `??` plus
those five modified files.

Inspect the production diff separately:

```bash
git diff -U8 -- services/read_projections.py | sed -n '1,100p'
```

Expected: the only production change is the `source_item.invest_assessment.model_dump(mode="json")` field. Confirm there are no changes to `frontend/project.js`, `cli/main.py`, `api.py`, `routers/sprint.py`, `services/contracts/story.py`, `services/contracts/sprint.py`, packet files, prompts, migrations, lockfiles, profiles, or databases.

Stage exactly the six expected files, audit the staged diff, and create the local implementation commit:

```bash
git add \
  docs/superpowers/plans/2026-08-29-sprint-review-invest-regression.md \
  services/read_projections.py \
  tests/adapters/test_cli_workflow_domain.py \
  tests/e2e/test_single_project_lifecycle_ui.py \
  tests/services/test_durable_product_definition_projections.py \
  tests/test_workflow_position_display.mjs
git diff --cached --check
git diff --cached --name-only | sed -n '1,40p'
git commit -m "fix(story): project accepted INVEST in Sprint review (#221)"
```

Expected: the staged scope is exactly the six paths listed above, and the commit succeeds only after the focused and static verification steps are GREEN. The full repository aggregate follows this commit because its acceptance-launcher smoke tests require a clean checkout.

Verify clean status and record the stable commit SHA:

```bash
git status --short --branch
git rev-parse HEAD
git show -s --format='%H%n%s' HEAD
```

Expected: `git status --short --branch` has only the branch line and no changed-file entries; `HEAD` differs from base `4d4b6ce40164b50f68c931eb54f81950bb0ffbeb`; and the recorded subject is exactly `fix(story): project accepted INVEST in Sprint review (#221)`. Record the full commit SHA in the implementation handoff and review package. Do not push, merge, mutate profiles, take any Sprint action, or clean up the worktree.

- [ ] **Step 11: Run the checkout-local repository aggregate on the clean commit**

Ensure no other repository gate is running, then retain this run's complete
result outside the checkout:

```bash
mkdir -p /tmp/agileforge-221-clean-sha-gate
./agileforge-dev check --json \
  > /tmp/agileforge-221-clean-sha-gate/check.json.log 2>&1
exit_code=$?
printf '%s\n' "$exit_code" \
  > /tmp/agileforge-221-clean-sha-gate/check.exit-status
exit "$exit_code"
```

Expected: exit code `0`. This provider-free aggregate includes lock validation,
Python quality, repository frontend tests, whitespace validation, and
distribution verification. Do not substitute a bare `pytest`, `ruff`, `npm`,
or user-level `agileforge`. If it fails, retain the commit and the persistent
log/status files, capture the exact failed stages or nodes, and do not push or
integrate.
