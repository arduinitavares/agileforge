import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

const projectHtmlPath = path.resolve(import.meta.dirname, '../frontend/project.html');
const projectHtmlSource = fs.readFileSync(projectHtmlPath, 'utf8');
const projectJsPath = path.resolve(import.meta.dirname, '../frontend/project.js');
const projectJsSource = fs.readFileSync(projectJsPath, 'utf8');

test('authority review console exposes summary, tabs, filters, and decision rail', () => {
    const expectedIds = [
        'authority-review-state-badge',
        'authority-review-metrics',
        'authority-spec-location',
        'authority-spec-hash',
        'authority-fingerprint',
        'authority-compiled-at',
        'authority-source-state',
        'tab-btn-overview',
        'tab-content-overview',
        'overview-findings-list',
        'overview-gaps-list',
        'overview-assumptions-list',
        'overview-exclusions-list',
        'overview-eligible-rules-list',
        'authority-invariant-search',
        'authority-support-filter',
        'authority-invariant-filter-status',
        'authority-decision-rail',
        'authority-decision-status',
        'authority-blocked-reason',
    ];

    for (const id of expectedIds) {
        assert.match(projectHtmlSource, new RegExp(`id="${id}"`));
    }

    assert.match(projectHtmlSource, /Accept Authority/);
    assert.match(projectHtmlSource, /Request Refinement/);
});

test('authority review console preserves legacy renderer hook IDs', () => {
    const expectedIds = [
        'gaps-list',
        'assumptions-list',
        'exclusions-list',
        'eligible-rules-list',
    ];

    for (const id of expectedIds) {
        assert.match(projectHtmlSource, new RegExp(`id="${id}"`));
    }
});

test('authority review tabs default to overview and manage all tab panels', () => {
    assert.match(projectJsSource, /let currentAuthorityReviewActiveTab = 'overview';/);

    const tabsMatch = projectJsSource.match(
        /function switchAuthorityReviewTab\(tabName\)[\s\S]*?const tabs = \[([^\]]+)\];/
    );
    assert.ok(tabsMatch, 'switchAuthorityReviewTab should define a tabs array');

    const tabs = [...tabsMatch[1].matchAll(/'([^']+)'/g)].map((match) => match[1]);
    assert.deepEqual(tabs, ['overview', 'invariants', 'spec', 'raw']);
});

function extractFunctionSource(functionName) {
    const start = projectJsSource.indexOf(`function ${functionName}(`);
    assert.notEqual(start, -1, `${functionName} should exist in frontend/project.js`);

    const bodyStart = projectJsSource.indexOf('{', start);
    assert.notEqual(bodyStart, -1, `${functionName} should have a function body`);

    let depth = 0;
    for (let index = bodyStart; index < projectJsSource.length; index += 1) {
        const character = projectJsSource[index];
        if (character === '{') depth += 1;
        if (character === '}') depth -= 1;
        if (depth === 0) {
            return projectJsSource.slice(start, index + 1);
        }
    }

    assert.fail(`${functionName} should have a complete function body`);
}

function loadAuthorityConsoleFunction(name, functionNames) {
    const source = functionNames.map((functionName) => extractFunctionSource(functionName)).join('\n');
    return new Function(`${source}; return ${name};`)();
}

test('safeArray normalizes missing and non-array values', () => {
    const safeArray = loadAuthorityConsoleFunction('safeArray', ['safeArray']);

    assert.deepEqual(safeArray(null), []);
    assert.deepEqual(safeArray(undefined), []);
    assert.deepEqual(safeArray({ length: 2 }), []);
    assert.deepEqual(safeArray(['gap']), ['gap']);
});

test('authorityReviewState classifies accepted authority packets', () => {
    const authorityReviewState = loadAuthorityConsoleFunction(
        'authorityReviewState',
        ['safeArray', 'authorityReviewState'],
    );

    assert.deepEqual(authorityReviewState({ post_accept: true }), {
        label: 'Accepted',
        tone: 'accepted',
        acceptDisabled: true,
        decision: 'Authority accepted. Vision is unlocked.',
        reason: '',
    });
});

test('authorityReviewState blocks non-overrideable blocking findings', () => {
    const authorityReviewState = loadAuthorityConsoleFunction(
        'authorityReviewState',
        ['safeArray', 'authorityReviewState'],
    );

    const state = authorityReviewState({
        pending_authority: {
            review_findings: [
                { code: 'SPEC_GAP', severity: 'blocking', override_allowed: false },
            ],
        },
    });

    assert.equal(state.label, 'Blocked');
    assert.equal(state.tone, 'blocked');
    assert.equal(state.acceptDisabled, true);
    assert.equal(state.decision, 'Authority cannot be accepted yet.');
    assert.match(state.reason, /SPEC_GAP/);
    assert.match(state.reason, /must be resolved before acceptance\./);
});

test('authorityReviewState allows overrideable blocking findings', () => {
    const authorityReviewState = loadAuthorityConsoleFunction(
        'authorityReviewState',
        ['safeArray', 'authorityReviewState'],
    );

    assert.deepEqual(authorityReviewState({
        pending_authority: {
            review_findings: [
                { code: 'OVERRIDE', severity: 'blocking', override_allowed: true },
            ],
        },
    }), {
        label: 'Override Required',
        tone: 'warning',
        acceptDisabled: false,
        decision: 'Authority has overrideable blocking findings.',
        reason: 'Accepting will request candidate-specific override rationale.',
    });
});

test('authorityReviewState marks packets without blocking findings ready', () => {
    const authorityReviewState = loadAuthorityConsoleFunction(
        'authorityReviewState',
        ['safeArray', 'authorityReviewState'],
    );

    assert.deepEqual(authorityReviewState({ pending_authority: { review_findings: [] } }), {
        label: 'Accept Ready',
        tone: 'ready',
        acceptDisabled: false,
        decision: 'Authority is ready for human acceptance.',
        reason: 'Review the compiled artifacts, then accept or request refinement.',
    });
});
