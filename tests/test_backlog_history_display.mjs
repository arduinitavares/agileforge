import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

const projectJsPath = path.resolve(import.meta.dirname, '../frontend/project.js');
const projectJsSource = fs.readFileSync(projectJsPath, 'utf8');

function loadBacklogHistoryHelpers() {
    const helperNames = [
        'isCurrentBacklogAttempt',
        'currentBacklogHistoryItems',
        'latestCurrentBacklogAttempt',
    ];
    const source = helperNames.map((name) => {
        const match = projectJsSource.match(
            new RegExp(`function ${name}\\([\\s\\S]*?\\n\\}`),
        );
        assert.ok(match, `${name} should exist in frontend/project.js`);
        return match[0];
    }).join('\n');
    return new Function(`${source}; return { ${helperNames.join(', ')} };`)();
}

test('latestCurrentBacklogAttempt ignores stale scope-extension history items', () => {
    const { currentBacklogHistoryItems, latestCurrentBacklogAttempt } = loadBacklogHistoryHelpers();
    const oldCompleteAttempt = {
        attempt_id: 'backlog-attempt-1',
        is_complete: true,
        is_current: false,
        is_stale: true,
        stale_reason: 'scope_extension_pending',
    };
    const currentIncompleteAttempt = {
        attempt_id: 'backlog-attempt-2',
        is_complete: false,
        is_current: true,
        is_stale: false,
    };

    assert.deepEqual(currentBacklogHistoryItems([oldCompleteAttempt]), []);
    assert.equal(
        latestCurrentBacklogAttempt([oldCompleteAttempt, currentIncompleteAttempt]),
        currentIncompleteAttempt,
    );
});

test('saveBacklogDraft sends guarded Backlog save payload', () => {
    assert.match(projectJsSource, /attempt_id:\s*latestBacklogAttemptId/);
    assert.match(
        projectJsSource,
        /expected_artifact_fingerprint:\s*latestBacklogArtifactFingerprint/,
    );
    assert.match(projectJsSource, /expected_state:\s*'BACKLOG_REVIEW'/);
    assert.match(projectJsSource, /idempotency_key:\s*backlogSaveIdempotencyKey\(\)/);
});
