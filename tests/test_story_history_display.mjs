import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

const projectJsPath = path.resolve(import.meta.dirname, 'legacy_project.js');
const projectJsSource = fs.readFileSync(projectJsPath, 'utf8');

function loadResolveStoryDisplayAttempt() {
    const match = projectJsSource.match(
        /function resolveStoryDisplayAttempt\(items, payload\) \{[\s\S]*?\n\}/,
    );
    assert.ok(match, 'resolveStoryDisplayAttempt should exist in frontend/project.js');
    return new Function(`${match[0]}; return resolveStoryDisplayAttempt;`)();
}

test('resolveStoryDisplayAttempt prefers the projected reusable draft when save remains available', () => {
    const resolveStoryDisplayAttempt = loadResolveStoryDisplayAttempt();
    const reusableAttempt = {
        attempt_id: 'attempt-1',
        classification: 'reusable_content_result',
        output_artifact: { user_stories: [{ story_title: 'Saved draft' }] },
    };
    const failedRetryAttempt = {
        attempt_id: 'attempt-2',
        classification: 'nonreusable_transport_failure',
        output_artifact: { error: 'network lost' },
    };

    const selected = resolveStoryDisplayAttempt(
        [reusableAttempt, failedRetryAttempt],
        {
            current_draft: {
                attempt_id: 'attempt-1',
                kind: 'complete_draft',
                is_complete: true,
            },
            save: { available: true },
        },
    );

    assert.equal(selected, reusableAttempt);
});

test('resolveStoryDisplayAttempt falls back to the latest attempt when no projected save draft is available', () => {
    const resolveStoryDisplayAttempt = loadResolveStoryDisplayAttempt();
    const reusableAttempt = {
        attempt_id: 'attempt-1',
        classification: 'reusable_content_result',
    };
    const latestAttempt = {
        attempt_id: 'attempt-2',
        classification: 'nonreusable_schema_failure',
    };

    const selected = resolveStoryDisplayAttempt(
        [reusableAttempt, latestAttempt],
        {
            current_draft: {
                attempt_id: 'attempt-1',
                kind: 'incomplete_draft',
                is_complete: false,
            },
            save: { available: false },
        },
    );

    assert.equal(selected, latestAttempt);
});
