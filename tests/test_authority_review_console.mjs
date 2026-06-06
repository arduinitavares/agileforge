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
        'authority-quality-summary',
        'authority-quality-groups-list',
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

test('renderAuthorityReviewCard delegates production rendering to authority console helpers', () => {
    const source = extractFunctionSource('renderAuthorityReviewCard');

    assert.match(source, /attachAuthorityReviewControls\(\);/);
    assert.match(source, /renderAuthoritySummary\(currentAuthorityReview\);/);
    assert.match(source, /renderAuthorityOverview\(currentAuthorityReview\);/);
    assert.match(source, /renderAuthorityInvariants\(artifact\);/);
});

test('renderAuthorityReviewCard raw preview is textContent JSON with post accept state', () => {
    const source = extractFunctionSource('renderAuthorityReviewCard');

    assert.match(source, /post_accept:\s*currentAuthorityReview\.post_accept\s*===\s*true/);
    assert.match(source, /preview\.textContent\s*=\s*JSON\.stringify\(previewPayload,\s*null,\s*2\);/);
    assert.doesNotMatch(source, /preview\.innerText\s*=\s*JSON\.stringify/);
    assert.doesNotMatch(source, /preview\.innerHTML\s*=/);
});

test('renderAuthorityReviewCard shows explicit unavailable source fallback', () => {
    const source = extractFunctionSource('renderAuthorityReviewCard');

    assert.match(source, /Specification source content is unavailable in this review packet\./);
});

test('renderAuthorityReviewCard removed legacy unsafe authority list clearing', () => {
    const source = extractFunctionSource('renderAuthorityReviewCard');
    const removedPatterns = [
        /invariantsList\.innerHTML\s*=\s*''/,
        /gapsList\.innerHTML\s*=\s*''/,
        /assumptionsList\.innerHTML\s*=\s*''/,
        /exclusionsList\.innerHTML\s*=\s*''/,
        /eligibleRulesList\.innerHTML\s*=\s*''/,
        /themesContainer\.innerHTML\s*=\s*''/,
    ];

    for (const pattern of removedPatterns) {
        assert.doesNotMatch(source, pattern);
    }
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
    const dependencyNames = ['isPlainObject', 'authorityReviewIncompleteReason'];
    const loadedNames = [
        ...dependencyNames.filter((functionName) => (
            projectJsSource.includes(`function ${functionName}(`)
        )),
        ...functionNames,
    ];
    const source = [...new Set(loadedNames)]
        .map((functionName) => extractFunctionSource(functionName))
        .join('\n');
    return new Function(`${source}; return ${name};`)();
}

function completeAuthorityReviewPacket(overrides = {}) {
    return {
        spec: {
            resolved_path: '/tmp/spec.json',
            spec_hash: 'sha256:spec',
        },
        pending_authority: {
            review_findings: [],
            artifact: {
                scope_themes: [],
                invariants: [],
                eligible_feature_rules: [],
                rejected_features: [],
                gaps: [],
                assumptions: [],
                source_map: {},
            },
        },
        ...overrides,
    };
}

function createDocumentStub() {
    class ElementStub {
        constructor(id = '') {
            this.id = id;
            this.children = [];
            this.className = '';
            this.disabled = false;
            this._innerHTML = '';
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
            this._innerHTML = '';
            this._textContent = value == null ? '' : String(value);
        }

        get innerHTML() {
            return this._innerHTML;
        }

        set innerHTML(value) {
            this.children = [];
            this._textContent = '';
            this._innerHTML = value == null ? '' : String(value);
        }

        appendChild(child) {
            this.children.push(child);
            return child;
        }

        replaceChildren(...children) {
            this._textContent = '';
            this._innerHTML = '';
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

    const review = completeAuthorityReviewPacket();
    review.pending_authority.review_findings = [
        { code: 'SPEC_GAP', severity: 'blocking', override_allowed: false },
    ];
    const state = authorityReviewState(review);

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

    const review = completeAuthorityReviewPacket();
    review.pending_authority.review_findings = [
        { code: 'OVERRIDE', severity: 'blocking', override_allowed: true },
    ];

    assert.deepEqual(authorityReviewState(review), {
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
    assert.deepEqual(authorityReviewState({
        pending_authority: {
            review_findings: [],
        },
        spec: {
            resolved_path: '/tmp/spec.json',
            spec_hash: 'sha256:spec',
        },
    }), incompleteState);
    assert.deepEqual(authorityReviewState({
        pending_authority: {
            review_findings: [],
            artifact: {
                invariants: [],
                gaps: [],
                assumptions: [],
            },
        },
        spec: {
            resolved_path: '/tmp/spec.json',
            spec_hash: 'sha256:spec',
        },
    }), incompleteState);
    assert.deepEqual(authorityReviewState({
        pending_authority: {
            review_findings: [],
            artifact: {
                scope_themes: [],
                invariants: [],
                eligible_feature_rules: [],
                rejected_features: [],
                gaps: [],
                assumptions: [],
                source_map: {},
            },
        },
    }), incompleteState);
});

test('authorityReviewState marks packets without blocking findings ready', () => {
    const authorityReviewState = loadAuthorityConsoleFunction(
        'authorityReviewState',
        ['safeArray', 'authorityReviewState'],
    );

    assert.deepEqual(authorityReviewState(completeAuthorityReviewPacket()), {
        label: 'Accept Ready',
        tone: 'ready',
        acceptDisabled: false,
        decision: 'Authority is ready for human acceptance.',
        reason: 'Review the compiled artifacts, then accept or request refinement.',
    });
});

test('collectIncompleteReviewOverrides refuses incomplete authority review packets', () => {
    const source = [
        extractFunctionSource('safeArray'),
        extractFunctionSource('isPlainObject'),
        extractFunctionSource('authorityReviewIncompleteReason'),
        extractFunctionSource('collectIncompleteReviewOverrides'),
    ].join('\n');
    const runCollect = new Function(
        'review',
        'setError',
        `${source}; currentAuthorityReview = review; setAuthorityReviewError = setError; return collectIncompleteReviewOverrides();`,
    );
    const errors = [];

    const result = runCollect(
        {
            pending_authority: {
                review_findings: [],
            },
            spec: {
                resolved_path: '/tmp/spec.json',
                spec_hash: 'sha256:spec',
            },
        },
        (message) => errors.push(message),
    );

    assert.equal(result, null);
    assert.deepEqual(errors, ['Review packet is incomplete. Reload the authority review before deciding.']);
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
                scope_themes: [],
                invariants: [{ id: 'REQ.one' }, { id: 'REQ.two' }],
                eligible_feature_rules: [],
                rejected_features: [],
                gaps: [{ id: 'GAP.one' }],
                assumptions: [{ id: 'ASM.one' }],
                source_map: {},
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

test('renderAuthorityOverview renders findings and assumptions as literal text without innerHTML', () => {
    const { documentStub } = createDocumentStub();
    globalThis.document = documentStub;

    const renderAuthorityOverview = loadAuthorityConsoleFunction(
        'renderAuthorityOverview',
        [
            'safeArray',
            'createEmptyState',
            'createSimpleCard',
            'createFindingCard',
            'renderAuthorityQuality',
            'renderListSection',
            'renderAuthorityOverview',
        ],
    );

    renderAuthorityOverview({
        pending_authority: {
            review_findings: [
                {
                    code: '<script>alert("x")</script>',
                    severity: 'blocking',
                    override_allowed: false,
                    message: 'Finding message',
                },
            ],
            artifact: {
                gaps: [],
                assumptions: [
                    {
                        id: 'ASM.html',
                        text: '<img src=x onerror=alert(1)>',
                    },
                ],
                rejected_features: [],
                eligible_feature_rules: [],
            },
        },
    });

    const findingsList = documentStub.getElementById('overview-findings-list');
    const assumptionsList = documentStub.getElementById('overview-assumptions-list');

    assert.match(findingsList.textContent, /<script>alert\("x"\)<\/script>/);
    assert.match(assumptionsList.textContent, /<img src=x onerror=alert\(1\)>/);
    assert.equal(findingsList.innerHTML, '');
    assert.equal(assumptionsList.innerHTML, '');
});

test('renderAuthorityOverview renders authority quality summary and groups safely', () => {
    assert.match(projectHtmlSource, /Authority Quality/i);

    const { documentStub } = createDocumentStub();
    globalThis.document = documentStub;

    const renderAuthorityOverview = loadAuthorityConsoleFunction(
        'renderAuthorityOverview',
        [
            'safeArray',
            'createEmptyState',
            'createSimpleCard',
            'createFindingCard',
            'renderAuthorityQuality',
            'renderListSection',
            'renderAuthorityOverview',
        ],
    );

    renderAuthorityOverview({
        pending_authority: {
            review_findings: [],
            artifact: {
                authority_quality: {
                    summary: {
                        merged_invariant_count: 1,
                        merged_assumption_count: 2,
                        review_group_count: 1,
                    },
                    review_groups: [
                        {
                            group_id: 'AQ-001',
                            group_type: 'over_split_invariants',
                            reason: '<b>duplicate requirements</b>',
                            member_ids: ['REQ.one', 'REQ.two'],
                        },
                    ],
                },
                gaps: [],
                assumptions: [],
                rejected_features: [],
                eligible_feature_rules: [],
            },
        },
    });

    const summary = documentStub.getElementById('authority-quality-summary');
    const groupsList = documentStub.getElementById('authority-quality-groups-list');

    assert.equal(summary.textContent.includes('1 merged invariant'), true);
    assert.match(summary.textContent, /2 merged assumptions/);
    assert.match(summary.textContent, /1 review group/);
    assert.equal(groupsList.textContent.includes('over_split_invariants'), true);
    assert.match(groupsList.textContent, /<b>duplicate requirements<\/b>/);
    assert.equal(groupsList.innerHTML, '');
});

test('renderAuthorityOverview ignores malformed authority quality groups', () => {
    const { documentStub } = createDocumentStub();
    globalThis.document = documentStub;

    const renderAuthorityOverview = loadAuthorityConsoleFunction(
        'renderAuthorityOverview',
        [
            'safeArray',
            'createEmptyState',
            'createSimpleCard',
            'createFindingCard',
            'renderAuthorityQuality',
            'renderListSection',
            'renderAuthorityOverview',
        ],
    );

    assert.doesNotThrow(() => renderAuthorityOverview({
        pending_authority: {
            review_findings: [],
            artifact: {
                authority_quality: {
                    summary: {
                        review_group_count: 4,
                    },
                    review_groups: [
                        null,
                        'not-a-group',
                        7,
                        {
                            group_id: 'AQ-valid',
                            group_type: 'merged_requirements',
                            reason: 'Valid quality group',
                            member_ids: ['REQ.valid'],
                        },
                    ],
                },
                gaps: [],
                assumptions: [],
                rejected_features: [],
                eligible_feature_rules: [],
            },
        },
    }));

    const groupsList = documentStub.getElementById('authority-quality-groups-list');
    assert.match(groupsList.textContent, /AQ-valid/);
    assert.match(groupsList.textContent, /merged_requirements/);
    assert.doesNotMatch(groupsList.textContent, /not-a-group/);
    assert.equal(groupsList.innerHTML, '');

    assert.doesNotThrow(() => renderAuthorityOverview({
        pending_authority: {
            review_findings: [],
            artifact: {
                authority_quality: {
                    summary: {
                        review_group_count: 3,
                    },
                    review_groups: [
                        null,
                        'not-a-group',
                        7,
                    ],
                },
                gaps: [],
                assumptions: [],
                rejected_features: [],
                eligible_feature_rules: [],
            },
        },
    }));

    assert.equal(groupsList.textContent, 'No authority quality groups.');
    assert.equal(groupsList.innerHTML, '');
});

test('renderAuthorityOverview renders finding candidate IDs as literal provenance text', () => {
    const { documentStub } = createDocumentStub();
    globalThis.document = documentStub;

    const renderAuthorityOverview = loadAuthorityConsoleFunction(
        'renderAuthorityOverview',
        [
            'safeArray',
            'createEmptyState',
            'createSimpleCard',
            'createFindingCard',
            'renderAuthorityQuality',
            'renderListSection',
            'renderAuthorityOverview',
        ],
    );

    renderAuthorityOverview({
        pending_authority: {
            review_findings: [
                {
                    code: 'CANDIDATE_GAP',
                    severity: 'warning',
                    override_allowed: true,
                    candidate_ids: ['REQ.one', 'REQ.two'],
                },
            ],
            artifact: {},
        },
    });

    const findingsList = documentStub.getElementById('overview-findings-list');

    assert.match(findingsList.textContent, /Candidates: REQ\.one, REQ\.two/);
    assert.equal(findingsList.innerHTML, '');
});

test('renderAuthorityOverview renders explicit empty states without innerHTML', () => {
    const { documentStub } = createDocumentStub();
    globalThis.document = documentStub;

    const renderAuthorityOverview = loadAuthorityConsoleFunction(
        'renderAuthorityOverview',
        [
            'safeArray',
            'createEmptyState',
            'createSimpleCard',
            'createFindingCard',
            'renderAuthorityQuality',
            'renderListSection',
            'renderAuthorityOverview',
        ],
    );

    renderAuthorityOverview({
        pending_authority: {
            review_findings: [],
            artifact: {
                gaps: [],
                assumptions: [],
                rejected_features: [],
                eligible_feature_rules: [],
            },
        },
    });

    const expectedStates = [
        ['overview-findings-list', 'No review findings.'],
        ['overview-gaps-list', 'No identified gaps.'],
        ['overview-assumptions-list', 'No compiler assumptions.'],
        ['overview-exclusions-list', 'No excluded features.'],
        ['overview-eligible-rules-list', 'No eligible feature rules.'],
    ];

    for (const [id, expectedText] of expectedStates) {
        const element = documentStub.getElementById(id);
        assert.equal(element.textContent, expectedText);
        assert.equal(element.innerHTML, '');
    }
});

test('filterAuthorityInvariants searches ID text support refs and excerpt', () => {
    const filterAuthorityInvariants = loadAuthorityConsoleFunction(
        'filterAuthorityInvariants',
        ['safeArray', 'invariantSearchText', 'filterAuthorityInvariants'],
    );
    const invariants = [
        {
            id: 'REQ.create',
            text: 'Create a todo',
            support: 'direct',
            source_refs: ['SPEC.1'],
            source_excerpt: 'Enter key creates item',
        },
        {
            id: 'REQ.archive',
            text: 'Archive a todo',
            support: 'inferred',
            source_refs: ['SPEC.2'],
            source_excerpt: 'Move old item',
        },
        {
            id: 'DATA.owner',
            text: 'Owner field is required',
            support: 'direct',
            source_refs: ['DATA_MODEL'],
            source_excerpt: 'Each item stores owner',
        },
    ];

    assert.deepEqual(
        filterAuthorityInvariants(invariants, 'REQ.archive', 'all').map((item) => item.id),
        ['REQ.archive'],
    );
    assert.deepEqual(
        filterAuthorityInvariants(invariants, 'create a todo', 'all').map((item) => item.id),
        ['REQ.create'],
    );
    assert.deepEqual(
        filterAuthorityInvariants(invariants, 'direct', 'all').map((item) => item.id),
        ['REQ.create', 'DATA.owner'],
    );
    assert.deepEqual(
        filterAuthorityInvariants(invariants, 'DATA_MODEL', 'all').map((item) => item.id),
        ['DATA.owner'],
    );
    assert.deepEqual(
        filterAuthorityInvariants(invariants, 'enter key', 'all').map((item) => item.id),
        ['REQ.create'],
    );
});

test('filterAuthorityInvariants applies direct inferred and combined filters', () => {
    const filterAuthorityInvariants = loadAuthorityConsoleFunction(
        'filterAuthorityInvariants',
        ['safeArray', 'invariantSearchText', 'filterAuthorityInvariants'],
    );
    const invariants = [
        {
            id: 'REQ.direct',
            text: 'Direct behavior',
            support: 'direct',
            source_refs: ['SPEC.direct'],
            source_excerpt: 'User can submit',
        },
        {
            id: 'REQ.inferred',
            text: 'Inferred behavior',
            support: 'inferred',
            source_refs: ['SPEC.inferred'],
            source_excerpt: 'Derived from flow',
        },
        {
            id: 'REQ.default',
            text: 'Default inferred behavior',
            source_refs: ['SPEC.default'],
            source_excerpt: 'Derived by compiler',
        },
    ];

    assert.deepEqual(
        filterAuthorityInvariants(invariants, '', 'direct').map((item) => item.id),
        ['REQ.direct'],
    );
    assert.deepEqual(
        filterAuthorityInvariants(invariants, '', 'inferred').map((item) => item.id),
        ['REQ.inferred', 'REQ.default'],
    );
    assert.deepEqual(
        filterAuthorityInvariants(invariants, 'derived', 'inferred').map((item) => item.id),
        ['REQ.inferred', 'REQ.default'],
    );
    assert.deepEqual(
        filterAuthorityInvariants(invariants, 'submit', 'inferred').map((item) => item.id),
        [],
    );
});

test('renderAuthorityInvariants writes domain themes filtered count and empty filtered state', () => {
    const { documentStub } = createDocumentStub();
    globalThis.document = documentStub;

    const renderAuthorityInvariants = loadAuthorityConsoleFunction(
        'renderAuthorityInvariants',
        [
            'safeArray',
            'createEmptyState',
            'invariantSearchText',
            'filterAuthorityInvariants',
            'renderAuthorityInvariants',
        ],
    );

    globalThis.authorityInvariantSearchQuery = 'missing';
    globalThis.authoritySupportFilter = 'all';

    renderAuthorityInvariants({
        domain: 'todo',
        scope_themes: ['creation', 'archival'],
        invariants: [
            {
                id: 'REQ.create',
                text: 'Create a todo',
                support: 'direct',
                source_refs: ['SPEC.1'],
                source_excerpt: 'Enter key creates item',
            },
        ],
    });

    assert.equal(documentStub.getElementById('authority-domain-badge').textContent, 'todo');
    assert.equal(documentStub.getElementById('authority-themes-container').textContent, 'creationarchival');
    assert.equal(documentStub.getElementById('authority-invariant-filter-status').textContent, '0 of 1 invariants shown');
    assert.equal(documentStub.getElementById('invariants-list').textContent, 'No invariants match the current filters.');
    assert.equal(documentStub.getElementById('invariants-list').innerHTML, '');
});

test('renderAuthorityInvariants uses createInvariantCard for visible invariants', () => {
    const { documentStub } = createDocumentStub();
    globalThis.document = documentStub;

    const renderAuthorityInvariants = loadAuthorityConsoleFunction(
        'renderAuthorityInvariants',
        [
            'safeArray',
            'createEmptyState',
            'createInvariantCard',
            'invariantSearchText',
            'filterAuthorityInvariants',
            'renderAuthorityInvariants',
        ],
    );

    globalThis.authorityInvariantSearchQuery = 'enter';
    globalThis.authoritySupportFilter = 'direct';

    renderAuthorityInvariants({
        domain: 'todo',
        scope_themes: [],
        invariants: [
            {
                id: 'REQ.create',
                text: 'Create a todo',
                support: 'direct',
                source_refs: ['SPEC.1'],
                source_excerpt: 'Enter key creates item',
            },
            {
                id: 'REQ.archive',
                text: 'Archive a todo',
                support: 'inferred',
                source_refs: ['SPEC.2'],
                source_excerpt: 'Move old item',
            },
        ],
    });

    const invariantsList = documentStub.getElementById('invariants-list');

    assert.equal(documentStub.getElementById('authority-themes-container').textContent, 'No scope themes.');
    assert.equal(documentStub.getElementById('authority-invariant-filter-status').textContent, '1 of 2 invariants shown');
    assert.equal(invariantsList.children.length, 1);
    assert.match(invariantsList.textContent, /REQ\.create/);
    assert.doesNotMatch(invariantsList.textContent, /REQ\.archive/);
    assert.equal(invariantsList.innerHTML, '');
});

test('authority artifact renderer does not assign user-controlled content through innerHTML', () => {
    const unsafePatterns = [
        /specViewer\.innerHTML\s*=/,
        /preview\.innerHTML\s*=/,
        /findingsEl\.innerHTML\s*=/,
        /invariantsList\.innerHTML\s*=/,
        /gapsList\.innerHTML\s*=/,
        /assumptionsList\.innerHTML\s*=/,
        /exclusionsList\.innerHTML\s*=/,
        /eligibleRulesList\.innerHTML\s*=/,
        /message\.innerHTML\s*=/,
        /contentDiv\.innerHTML\s*=/,
    ];

    for (const pattern of unsafePatterns) {
        assert.doesNotMatch(projectJsSource, pattern);
    }
});
