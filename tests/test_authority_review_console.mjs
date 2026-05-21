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

function createDocumentStub() {
    class ElementStub {
        constructor(id = '') {
            this.id = id;
            this.children = [];
            this.className = '';
            this.disabled = false;
            this._textContent = '';
            this.classList = {
                add: (...classes) => {
                    const current = new Set(this.className.split(/\s+/).filter(Boolean));
                    classes.forEach((className) => current.add(className));
                    this.className = [...current].join(' ');
                },
                remove: (...classes) => {
                    const removed = new Set(classes);
                    this.className = this.className
                        .split(/\s+/)
                        .filter((className) => className && !removed.has(className))
                        .join(' ');
                },
                toggle: (className, force) => {
                    if (force === true) {
                        this.classList.add(className);
                        return true;
                    }
                    if (force === false) {
                        this.classList.remove(className);
                        return false;
                    }
                    if (this.classList.contains(className)) {
                        this.classList.remove(className);
                        return false;
                    }
                    this.classList.add(className);
                    return true;
                },
                contains: (className) => this.className.split(/\s+/).includes(className),
            };
        }

        get textContent() {
            return `${this._textContent}${this.children.map((child) => child.textContent).join('')}`;
        }

        set textContent(value) {
            this.children = [];
            this._textContent = value == null ? '' : String(value);
        }

        appendChild(child) {
            this.children.push(child);
            return child;
        }

        replaceChildren(...children) {
            this._textContent = '';
            this.children = children;
        }
    }

    const elements = new Map();
    const documentStub = {
        createElement: () => new ElementStub(),
        getElementById: (id) => {
            if (!elements.has(id)) {
                elements.set(id, new ElementStub(id));
            }
            return elements.get(id);
        },
    };

    return { documentStub, elements };
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

test('authorityReviewState fails closed for incomplete review packets', () => {
    const authorityReviewState = loadAuthorityConsoleFunction(
        'authorityReviewState',
        ['safeArray', 'authorityReviewState'],
    );
    const incompleteState = {
        label: 'Review Incomplete',
        tone: 'blocked',
        acceptDisabled: true,
        decision: 'Review packet is incomplete.',
        reason: 'Reload the authority review before deciding.',
    };

    assert.deepEqual(authorityReviewState(undefined), incompleteState);
    assert.deepEqual(authorityReviewState({}), incompleteState);
    assert.deepEqual(authorityReviewState({ pending_authority: {} }), incompleteState);
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

test('renderAuthoritySummary writes metrics and disables non-overrideable accept safely', () => {
    const { documentStub } = createDocumentStub();
    globalThis.document = documentStub;

    const renderAuthoritySummary = loadAuthorityConsoleFunction(
        'renderAuthoritySummary',
        [
            'safeArray',
            'authorityReviewState',
            'setTextContent',
            'createMetricElement',
            'sourceStateLabel',
            'applyStateBadgeTone',
            'renderAuthoritySummary',
        ],
    );

    renderAuthoritySummary({
        project: { name: 'Cartola' },
        spec: {
            resolved_path: '/tmp/spec.json',
            spec_hash: 'sha256:spec',
            content_included: true,
            content_truncated: false,
        },
        pending_authority: {
            authority_fingerprint: 'sha256:authority',
            compiled_at: '2026-05-21T13:00:00Z',
            compiler_version: '1.0.0',
            review_findings: [
                { code: 'NO_OVERRIDE', severity: 'blocking', override_allowed: false },
            ],
            artifact: {
                invariants: [{ id: 'REQ.one' }, { id: 'REQ.two' }],
                gaps: [{ id: 'GAP.one' }],
                assumptions: [{ id: 'ASM.one' }],
            },
        },
    });

    assert.equal(documentStub.getElementById('authority-review-state-badge').textContent, 'Blocked');
    assert.match(documentStub.getElementById('authority-review-summary').textContent, /Cartola authority/);
    assert.match(documentStub.getElementById('authority-spec-location').textContent, /\/tmp\/spec\.json/);
    assert.match(documentStub.getElementById('authority-spec-hash').textContent, /sha256:spec/);
    assert.match(documentStub.getElementById('authority-fingerprint').textContent, /sha256:authority/);
    assert.match(documentStub.getElementById('authority-compiled-at').textContent, /1\.0\.0/);
    assert.match(documentStub.getElementById('authority-source-state').textContent, /Full source included/);
    assert.match(documentStub.getElementById('authority-review-metrics').textContent, /Blockers1/);
    assert.match(documentStub.getElementById('authority-review-metrics').textContent, /Invariants2/);
    assert.match(documentStub.getElementById('authority-review-metrics').textContent, /Gaps1/);
    assert.match(documentStub.getElementById('authority-review-metrics').textContent, /Assumptions1/);
    assert.equal(documentStub.getElementById('btn-accept-authority').disabled, true);
    assert.match(documentStub.getElementById('authority-blocked-reason').textContent, /NO_OVERRIDE/);
});

test('renderAuthoritySummary hides actions and findings after accept', () => {
    const { documentStub } = createDocumentStub();
    globalThis.document = documentStub;

    const renderAuthoritySummary = loadAuthorityConsoleFunction(
        'renderAuthoritySummary',
        [
            'safeArray',
            'authorityReviewState',
            'setTextContent',
            'createMetricElement',
            'sourceStateLabel',
            'applyStateBadgeTone',
            'renderAuthoritySummary',
        ],
    );

    renderAuthoritySummary({
        post_accept: true,
        project: { name: 'Cartola' },
        spec: {
            resolved_path: '/tmp/spec.json',
            disk_sha256: 'sha256:disk',
            content_truncated: true,
        },
        pending_authority: {
            authority_fingerprint: 'sha256:accepted-authority',
            compiled_at: '2026-05-21T13:00:00Z',
            compiler_version: '1.0.0',
            artifact: {
                invariants: [{ id: 'REQ.one' }],
                gaps: [],
                assumptions: [],
            },
        },
    });

    assert.equal(documentStub.getElementById('authority-review-state-badge').textContent, 'Accepted');
    assert.match(documentStub.getElementById('authority-review-summary').textContent, /Cartola authority is accepted/);
    assert.match(documentStub.getElementById('authority-source-state').textContent, /Source excerpt shown/);
    assert.equal(documentStub.getElementById('authority-decision-status').textContent, 'Authority accepted. Vision is unlocked.');
    assert.equal(documentStub.getElementById('btn-accept-authority').disabled, true);
    assert.equal(documentStub.getElementById('authority-review-actions-container').classList.contains('hidden'), true);
    assert.equal(documentStub.getElementById('authority-review-findings').classList.contains('hidden'), true);
});
