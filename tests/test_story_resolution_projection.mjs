import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

const projectJsPath = path.resolve(import.meta.dirname, 'legacy_project.js');
const projectJsSource = fs.readFileSync(projectJsPath, 'utf8');

function loadFunction(name) {
    const match = projectJsSource.match(
        new RegExp(`function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`),
    );
    assert.ok(match, `${name} should exist in frontend/project.js`);
    return new Function(`${match[0]}; return ${name};`)();
}

test('deriveStoryProjectionState includes merge recommendation data', () => {
    const deriveStoryProjectionState = loadFunction('deriveStoryProjectionState');

    const state = deriveStoryProjectionState({
        current_draft: {
            attempt_id: 'attempt-1',
            kind: 'incomplete_draft',
            is_complete: false,
        },
        retry: {
            available: false,
            target_attempt_id: null,
        },
        save: {
            available: false,
        },
        resolution: {
            available: true,
            current: null,
            recommendation: {
                action: 'merge_into_requirement',
                owner_requirement: 'Updated Source Code Package',
                reason: 'Merge this duplicate validation requirement.',
                acceptance_criteria_to_move: ['Verify evidence exists.'],
            },
        },
    });

    assert.deepEqual(state, {
        isComplete: false,
        retryAvailable: false,
        retryTargetAttemptId: null,
        saveAvailable: false,
        draftKind: 'incomplete_draft',
        resolutionAvailable: true,
        resolutionCurrent: null,
        resolutionRecommendation: {
            action: 'merge_into_requirement',
            owner_requirement: 'Updated Source Code Package',
            reason: 'Merge this duplicate validation requirement.',
            acceptance_criteria_to_move: ['Verify evidence exists.'],
        },
    });
});

test('isResolvedStoryStatus treats merged requirements as resolved', () => {
    const isResolvedStoryStatus = loadFunction('isResolvedStoryStatus');

    assert.equal(isResolvedStoryStatus('Saved'), true);
    assert.equal(isResolvedStoryStatus('Merged'), true);
    assert.equal(isResolvedStoryStatus('Attempted'), false);
    assert.equal(isResolvedStoryStatus('Pending'), false);
});
