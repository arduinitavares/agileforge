# Authority Review Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the dashboard authority review card into a human-friendly review console that shows summary confidence signals, artifact drill-down tabs, and safe accept/refinement controls using the existing authority decision endpoints.

**Architecture:** This is a dashboard-only enhancement. `frontend/project.html` owns the stable DOM regions and IDs, `frontend/project.js` owns DOM-safe rendering and client-side filters, and existing `/api/projects/{project_id}/authority/*` endpoints remain unchanged except for one regression assertion that accepted fallback packets still expose the artifact payload needed by the UI.

**Tech Stack:** FastAPI dashboard routes, vanilla JavaScript, Tailwind utility classes already used in `frontend/project.html`, Python `pytest`, Node `node:test`, and `pyrepo-check`.

---

## File Structure

- Modify `frontend/project.html`
  - Replace the current authority card body with a review-console structure:
    summary band, overview/invariants/spec/raw tabs, and a sticky decision rail.
  - Preserve the existing IDs used by current behavior:
    `authority-review-card`, `authority-card-title`,
    `authority-review-summary`, `btn-refresh-authority-review`,
    `authority-review-preview`, `btn-accept-authority`,
    `authority-reject-reason`, `btn-reject-authority`,
    `authority-review-error`.

- Modify `frontend/project.js`
  - Keep current API calls and guard-token handling.
  - Add small renderer helpers for safe text, empty states, metrics, findings,
    and decision status.
  - Make `overview` the default authority review tab.
  - Add invariant search and support filtering.
  - Use `textContent`, `innerText` for fixed strings only, or DOM text nodes for
    all spec/authority payload content.

- Modify `tests/test_api_dashboard.py`
  - Extend the accepted-authority fallback regression test so it asserts the API
    packet still exposes artifact, spec source, and post-accept state needed by
    the UI.

- Create `tests/test_authority_review_console.mjs`
  - Static DOM contract checks for the review-console IDs.
  - Function-level tests for summary state, safe source rendering, empty states,
    invariant filtering, and decision rail behavior.

## Existing Context

Current relevant functions and IDs:

- `frontend/project.js`
  - `let currentAuthorityReviewActiveTab = 'invariants';`
  - `switchAuthorityReviewTab(tabName)`
  - `createInvariantCard(invariant)`
  - `createSimpleCard(item, badgeColorClass)`
  - `renderAuthorityReviewCard(visible)`
  - `loadAuthorityReview()`
  - `acceptAuthorityReview()`
  - `collectIncompleteReviewOverrides()`
  - `rejectAuthorityReview()`
  - global assignments near the end of the file.

- `frontend/project.html`
  - `#authority-review-card`
  - `#tab-btn-invariants`, `#tab-btn-spec`, `#tab-btn-raw`
  - `#tab-content-invariants`, `#tab-content-spec`, `#tab-content-raw`
  - `#invariants-list`, `#gaps-list`, `#assumptions-list`,
    `#exclusions-list`, `#eligible-rules-list`
  - `#authority-review-actions-container`

The current card already uses safe DOM APIs in most places. This plan preserves
that and adds stronger tests around it.

## Implementation Tasks

### Task 1: Add Console DOM Contract Tests

**Files:**
- Create: `tests/test_authority_review_console.mjs`
- Read: `frontend/project.html`
- Read: `frontend/project.js`

- [ ] **Step 1: Write the failing static DOM contract test**

Create `tests/test_authority_review_console.mjs` with this initial content:

```javascript
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

const projectHtmlPath = path.resolve(import.meta.dirname, '../frontend/project.html');
const projectHtmlSource = fs.readFileSync(projectHtmlPath, 'utf8');

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
```

- [ ] **Step 2: Run the new test and verify RED**

Run:

```bash
uv run --frozen node --test tests/test_authority_review_console.mjs
```

Expected: FAIL because `authority-review-state-badge` and the new review-console
IDs do not exist yet.

- [ ] **Step 3: Commit nothing**

Do not commit after RED. Continue to Task 2.

### Task 2: Replace Authority Card Markup With Review Console Regions

**Files:**
- Modify: `frontend/project.html`
- Test: `tests/test_authority_review_console.mjs`

- [ ] **Step 1: Update the card shell and summary band**

In `frontend/project.html`, replace the contents of `#authority-review-card`
with this structure. Keep it inside the existing setup section where the current
card lives.

```html
<div id="authority-review-card" class="hidden mt-4 rounded-lg border border-sky-200 bg-sky-50 p-4 text-sm text-sky-900 dark:border-sky-800 dark:bg-sky-950/40 dark:text-sky-100">
    <div class="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
        <div class="min-w-0">
            <div class="flex flex-wrap items-center gap-2">
                <h4 id="authority-card-title" class="font-bold">Pending Authority Review</h4>
                <span id="authority-review-state-badge" class="rounded-full bg-sky-100 px-2.5 py-1 text-[11px] font-black uppercase tracking-wide text-sky-800 dark:bg-sky-900/60 dark:text-sky-100"></span>
            </div>
            <p id="authority-review-summary" class="mt-1 text-sm text-slate-700 dark:text-slate-300"></p>
        </div>
        <button id="btn-refresh-authority-review" onclick="loadAuthorityReview()" class="inline-flex shrink-0 items-center gap-2 rounded-lg border border-sky-200 bg-white/70 px-3 py-2 text-xs font-bold text-sky-900 transition-colors hover:bg-sky-100 dark:border-sky-800 dark:bg-slate-950/30 dark:text-sky-100 dark:hover:bg-sky-900">
            <span class="material-symbols-outlined text-sm">refresh</span> Refresh
        </button>
    </div>

    <div id="authority-review-metrics" class="mt-4 grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-6"></div>

    <div class="mt-4 grid grid-cols-1 gap-2 rounded-lg border border-sky-200/70 bg-white/70 p-3 text-xs dark:border-sky-900/70 dark:bg-slate-950/20 lg:grid-cols-2">
        <div class="min-w-0">
            <div class="font-bold uppercase tracking-wide text-slate-500 dark:text-slate-400">Specification</div>
            <div id="authority-spec-location" class="mt-1 break-words font-mono text-slate-700 dark:text-slate-200"></div>
            <div id="authority-spec-hash" class="mt-1 break-all font-mono text-slate-500 dark:text-slate-400"></div>
        </div>
        <div class="min-w-0">
            <div class="font-bold uppercase tracking-wide text-slate-500 dark:text-slate-400">Authority</div>
            <div id="authority-fingerprint" class="mt-1 break-all font-mono text-slate-700 dark:text-slate-200"></div>
            <div id="authority-compiled-at" class="mt-1 text-slate-500 dark:text-slate-400"></div>
            <div id="authority-source-state" class="mt-1 text-slate-500 dark:text-slate-400"></div>
        </div>
    </div>

    <div id="authority-review-findings" class="mt-3 text-xs font-semibold text-amber-800 dark:text-amber-200"></div>

    <div class="mt-4 flex flex-wrap gap-2 border-b border-sky-200/70 dark:border-sky-800/70" role="tablist" aria-label="Authority review artifacts">
        <button type="button" id="tab-btn-overview" onclick="switchAuthorityReviewTab('overview')" class="px-4 py-2 -mb-px text-xs font-bold border-b-2 border-sky-600 text-sky-600 dark:border-sky-400 dark:text-sky-300 transition-colors focus:outline-none">
            Overview
        </button>
        <button type="button" id="tab-btn-invariants" onclick="switchAuthorityReviewTab('invariants')" class="px-4 py-2 -mb-px text-xs font-bold border-b-2 border-transparent text-slate-500 hover:text-sky-600 dark:text-slate-400 dark:hover:text-sky-300 transition-colors focus:outline-none">
            Invariants
        </button>
        <button type="button" id="tab-btn-spec" onclick="switchAuthorityReviewTab('spec')" class="px-4 py-2 -mb-px text-xs font-bold border-b-2 border-transparent text-slate-500 hover:text-sky-600 dark:text-slate-400 dark:hover:text-sky-300 transition-colors focus:outline-none">
            Spec Source
        </button>
        <button type="button" id="tab-btn-raw" onclick="switchAuthorityReviewTab('raw')" class="px-4 py-2 -mb-px text-xs font-bold border-b-2 border-transparent text-slate-500 hover:text-sky-600 dark:text-slate-400 dark:hover:text-sky-300 transition-colors focus:outline-none">
            Raw JSON
        </button>
    </div>

    <div class="mt-4">
        <div id="tab-content-overview" class="space-y-4">
            <div class="grid grid-cols-1 gap-4 lg:grid-cols-2">
                <section class="space-y-2">
                    <h5 class="text-xs uppercase tracking-wider font-bold text-slate-500 dark:text-slate-400">Review Findings</h5>
                    <div id="overview-findings-list" class="space-y-2"></div>
                </section>
                <section class="space-y-2">
                    <h5 class="text-xs uppercase tracking-wider font-bold text-amber-600 dark:text-amber-400">Spec Gaps</h5>
                    <div id="overview-gaps-list" class="space-y-2"></div>
                </section>
                <section class="space-y-2">
                    <h5 class="text-xs uppercase tracking-wider font-bold text-indigo-600 dark:text-indigo-400">Compiler Assumptions</h5>
                    <div id="overview-assumptions-list" class="space-y-2"></div>
                </section>
                <section class="space-y-2">
                    <h5 class="text-xs uppercase tracking-wider font-bold text-rose-600 dark:text-rose-400">Excluded Features</h5>
                    <div id="overview-exclusions-list" class="space-y-2"></div>
                </section>
                <section class="space-y-2 lg:col-span-2">
                    <h5 class="text-xs uppercase tracking-wider font-bold text-emerald-600 dark:text-emerald-400">Eligible Feature Rules</h5>
                    <div id="overview-eligible-rules-list" class="grid grid-cols-1 gap-2 lg:grid-cols-2"></div>
                </section>
            </div>
        </div>

        <div id="tab-content-invariants" class="hidden space-y-4">
            <div class="flex flex-wrap items-center gap-3">
                <span class="text-xs font-bold text-slate-500 uppercase tracking-wider dark:text-slate-400">Domain:</span>
                <span id="authority-domain-badge" class="px-2.5 py-1 rounded bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-200 font-bold text-xs"></span>
                <span class="text-xs font-bold text-slate-500 uppercase tracking-wider dark:text-slate-400">Themes:</span>
                <div id="authority-themes-container" class="flex flex-wrap gap-1.5"></div>
            </div>
            <div class="flex flex-col gap-2 rounded-lg border border-slate-200 bg-white/70 p-3 dark:border-slate-800 dark:bg-slate-950/20 md:flex-row md:items-center">
                <input id="authority-invariant-search" type="search" placeholder="Search invariant ID, text, source refs, or excerpt" class="min-w-0 flex-1 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:ring-2 focus:ring-sky-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100" />
                <select id="authority-support-filter" class="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:ring-2 focus:ring-sky-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100">
                    <option value="all">All support</option>
                    <option value="direct">Direct</option>
                    <option value="inferred">Inferred</option>
                </select>
            </div>
            <div id="authority-invariant-filter-status" class="text-xs font-semibold text-slate-500 dark:text-slate-400"></div>
            <div id="invariants-list" class="grid max-h-[34rem] grid-cols-1 gap-2 overflow-y-auto pr-1 xl:grid-cols-2"></div>
        </div>

        <div id="tab-content-spec" class="hidden space-y-3">
            <div id="spec-truncation-banner" class="hidden p-3 border border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200 rounded-lg text-xs flex items-start gap-2">
                <span class="material-symbols-outlined text-base">info</span>
                <div>
                    <span class="font-bold">Content Truncated:</span> The technical specification is too large to render completely. An excerpt is shown below.
                </div>
            </div>
            <div id="spec-source-viewer" class="max-h-[34rem] overflow-auto rounded bg-white/70 p-3 text-xs dark:bg-slate-900/70 font-mono whitespace-pre-wrap leading-relaxed border border-slate-200/50 dark:border-slate-800/50"></div>
        </div>

        <div id="tab-content-raw" class="hidden">
            <pre id="authority-review-preview" class="max-h-[34rem] overflow-auto rounded bg-white/70 p-3 text-xs dark:bg-slate-900/70 font-mono border border-slate-200/50 dark:border-slate-800/50"></pre>
        </div>
    </div>

    <div id="authority-decision-rail" class="sticky bottom-3 mt-4 rounded-lg border border-slate-200 bg-white/90 p-3 shadow-sm backdrop-blur dark:border-slate-800 dark:bg-slate-950/90">
        <div class="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
            <div class="min-w-0">
                <div id="authority-decision-status" class="text-sm font-bold text-slate-800 dark:text-slate-100"></div>
                <div id="authority-blocked-reason" class="mt-1 text-xs text-slate-600 dark:text-slate-400"></div>
            </div>
            <div id="authority-review-actions-container" class="flex flex-wrap items-center gap-3">
                <button id="btn-accept-authority" onclick="acceptAuthorityReview()" class="inline-flex items-center gap-2 rounded-lg bg-emerald-600 px-4 py-2 text-sm font-bold text-white transition-colors hover:bg-emerald-700 active:bg-emerald-800 disabled:cursor-not-allowed disabled:bg-slate-400">
                    <span class="material-symbols-outlined text-sm">verified</span> Accept Authority
                </button>
                <input id="authority-reject-reason" type="text" placeholder="Reason required to request refinement" class="min-w-[18rem] rounded-lg border border-slate-300 px-3 py-2 text-sm focus:ring-2 focus:ring-rose-500 dark:border-slate-700 dark:bg-slate-900" />
                <button id="btn-reject-authority" onclick="rejectAuthorityReview()" class="inline-flex items-center gap-2 rounded-lg bg-rose-600 px-4 py-2 text-sm font-bold text-white transition-colors hover:bg-rose-700 active:bg-rose-800">
                    <span class="material-symbols-outlined text-sm">block</span> Request Refinement
                </button>
            </div>
        </div>
    </div>
    <p id="authority-review-error" class="mt-3 hidden text-sm font-semibold text-rose-700 dark:text-rose-300"></p>
</div>
```

- [ ] **Step 2: Run the DOM contract test and verify GREEN for markup**

Run:

```bash
uv run --frozen node --test tests/test_authority_review_console.mjs
```

Expected: PASS for the static contract test.

- [ ] **Step 3: Commit the markup contract**

Run:

```bash
git add frontend/project.html tests/test_authority_review_console.mjs
git commit -m "feat: add authority review console shell"
```

Expected: commit succeeds.

### Task 3: Add Renderer Helper Tests and Minimal Helper Functions

**Files:**
- Modify: `tests/test_authority_review_console.mjs`
- Modify: `frontend/project.js`

- [ ] **Step 1: Add JS helper-loading utilities and failing helper tests**

Append this to `tests/test_authority_review_console.mjs`:

```javascript
const projectJsPath = path.resolve(import.meta.dirname, '../frontend/project.js');
const projectJsSource = fs.readFileSync(projectJsPath, 'utf8');

function loadAuthorityConsoleFunction(name, functionNames) {
    const source = functionNames.map((functionName) => {
        const pattern = new RegExp(`function ${functionName}\\\\([^)]*\\\\) \\\\{[\\\\s\\\\S]*?\\\\n\\\\}`);
        const match = projectJsSource.match(pattern);
        assert.ok(match, `${functionName} should exist in frontend/project.js`);
        return match[0];
    }).join('\n');
    return new Function(`${source}; return ${name};`)();
}

function createDocumentStub() {
    const elements = new Map();
    const documentStub = {
        createElement(tagName) {
            return createElementStub(tagName);
        },
        createTextNode(text) {
            return { nodeType: 3, textContent: String(text) };
        },
        getElementById(id) {
            if (!elements.has(id)) {
                const element = createElementStub('div');
                element.id = id;
                elements.set(id, element);
            }
            return elements.get(id);
        },
    };
    return { documentStub, elements };
}

function createElementStub(tagName) {
    const classes = new Set();
    const element = {
        tagName: tagName.toUpperCase(),
        children: [],
        className: '',
        dataset: {},
        disabled: false,
        value: '',
        textContent: '',
        innerText: '',
        innerHTML: '',
        classList: {
            add(...names) {
                for (const name of names) classes.add(name);
                element.className = Array.from(classes).join(' ');
            },
            remove(...names) {
                for (const name of names) classes.delete(name);
                element.className = Array.from(classes).join(' ');
            },
            contains(name) {
                return classes.has(name);
            },
            toggle(name, force) {
                if (force === true) classes.add(name);
                else if (force === false) classes.delete(name);
                else if (classes.has(name)) classes.delete(name);
                else classes.add(name);
                element.className = Array.from(classes).join(' ');
            },
        },
        appendChild(child) {
            this.children.push(child);
            if (child?.textContent) this.textContent += child.textContent;
            return child;
        },
        replaceChildren(...children) {
            this.children = [...children];
            this.textContent = children.map((child) => child?.textContent || '').join('');
        },
        addEventListener() {},
        setAttribute(name, value) {
            this[name] = String(value);
        },
        removeAttribute(name) {
            delete this[name];
        },
    };
    return element;
}

test('safeArray normalizes missing and non-array values', () => {
    const safeArray = loadAuthorityConsoleFunction('safeArray', ['safeArray']);

    assert.deepEqual(safeArray(null), []);
    assert.deepEqual(safeArray({ length: 2 }), []);
    assert.deepEqual(safeArray(['gap']), ['gap']);
});

test('authorityReviewState classifies accepted, blocked, overrideable, and ready packets', () => {
    const authorityReviewState = loadAuthorityConsoleFunction(
        'authorityReviewState',
        ['safeArray', 'authorityReviewState'],
    );

    assert.equal(authorityReviewState({ post_accept: true }).label, 'Accepted');
    assert.equal(authorityReviewState({
        pending_authority: {
            review_findings: [
                { code: 'BLOCKED', severity: 'blocking', override_allowed: false },
            ],
        },
    }).label, 'Blocked');
    assert.equal(authorityReviewState({
        pending_authority: {
            review_findings: [
                { code: 'OVERRIDE', severity: 'blocking', override_allowed: true },
            ],
        },
    }).label, 'Override Required');
    assert.equal(authorityReviewState({ pending_authority: { review_findings: [] } }).label, 'Accept Ready');
});
```

- [ ] **Step 2: Run the helper tests and verify RED**

Run:

```bash
uv run --frozen node --test tests/test_authority_review_console.mjs
```

Expected: FAIL because `safeArray` and `authorityReviewState` do not exist.

- [ ] **Step 3: Add minimal helper functions**

In `frontend/project.js`, immediately after `let currentAuthorityReviewActiveTab = 'invariants';`, change the active tab default and add these helpers:

```javascript
let currentAuthorityReviewActiveTab = 'overview';
let authorityInvariantSearchQuery = '';
let authoritySupportFilter = 'all';

function safeArray(value) {
    return Array.isArray(value) ? value : [];
}

function authorityReviewState(review) {
    if (review?.post_accept === true) {
        return {
            label: 'Accepted',
            tone: 'accepted',
            acceptDisabled: true,
            decision: 'Authority accepted. Vision is unlocked.',
            reason: '',
        };
    }

    const findings = safeArray(review?.pending_authority?.review_findings);
    const blocking = findings.filter((finding) => finding?.severity === 'blocking');
    const nonOverrideable = blocking.filter((finding) => finding?.override_allowed === false);

    if (nonOverrideable.length > 0) {
        const first = nonOverrideable[0];
        return {
            label: 'Blocked',
            tone: 'blocked',
            acceptDisabled: true,
            decision: 'Authority cannot be accepted yet.',
            reason: `${first.code || 'Blocking finding'} must be resolved before acceptance.`,
        };
    }

    if (blocking.length > 0) {
        return {
            label: 'Override Required',
            tone: 'warning',
            acceptDisabled: false,
            decision: 'Authority has overrideable blocking findings.',
            reason: 'Accepting will request candidate-specific override rationale.',
        };
    }

    return {
        label: 'Accept Ready',
        tone: 'ready',
        acceptDisabled: false,
        decision: 'Authority is ready for human acceptance.',
        reason: 'Review the compiled artifacts, then accept or request refinement.',
    };
}
```

Remove the old `let currentAuthorityReviewActiveTab = 'invariants';` line so only
the new `overview` default exists.

- [ ] **Step 4: Run helper tests and verify GREEN**

Run:

```bash
uv run --frozen node --test tests/test_authority_review_console.mjs
```

Expected: PASS.

- [ ] **Step 5: Commit helper tests and implementation**

Run:

```bash
git add frontend/project.js tests/test_authority_review_console.mjs
git commit -m "feat: add authority review console state helpers"
```

Expected: commit succeeds.

### Task 4: Render Summary Band and Decision Rail

**Files:**
- Modify: `tests/test_authority_review_console.mjs`
- Modify: `frontend/project.js`

- [ ] **Step 1: Add failing tests for summary and decision rendering**

Append this test to `tests/test_authority_review_console.mjs`:

```javascript
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
    assert.match(documentStub.getElementById('authority-review-metrics').textContent, /Invariants2/);
    assert.equal(documentStub.getElementById('btn-accept-authority').disabled, true);
    assert.match(documentStub.getElementById('authority-blocked-reason').textContent, /NO_OVERRIDE/);
});
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
uv run --frozen node --test tests/test_authority_review_console.mjs
```

Expected: FAIL because `renderAuthoritySummary`, `setTextContent`, and
`createMetricElement` do not exist.

- [ ] **Step 3: Add summary and decision helpers**

In `frontend/project.js`, after `authorityReviewState`, add:

```javascript
function setTextContent(id, value) {
    const element = document.getElementById(id);
    if (!element) return;
    element.textContent = value == null ? '' : String(value);
}

function createMetricElement(label, value) {
    const item = document.createElement('div');
    item.className = 'rounded-lg border border-slate-200 bg-white/80 p-2 dark:border-slate-800 dark:bg-slate-950/30';

    const labelEl = document.createElement('div');
    labelEl.className = 'text-[10px] font-black uppercase tracking-wide text-slate-500 dark:text-slate-400';
    labelEl.textContent = label;
    item.appendChild(labelEl);

    const valueEl = document.createElement('div');
    valueEl.className = 'mt-1 text-lg font-black text-slate-900 dark:text-slate-100';
    valueEl.textContent = String(value);
    item.appendChild(valueEl);

    return item;
}

function sourceStateLabel(spec) {
    if (spec?.content_truncated === true) return 'Source excerpt shown; full content is truncated.';
    if (spec?.content_included === true) return 'Full source included.';
    if (spec?.excerpt) return 'Source excerpt available.';
    return 'Source content unavailable in review packet.';
}

function applyStateBadgeTone(element, tone) {
    if (!element) return;
    const base = 'rounded-full px-2.5 py-1 text-[11px] font-black uppercase tracking-wide';
    const tones = {
        accepted: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/50 dark:text-emerald-100',
        blocked: 'bg-rose-100 text-rose-800 dark:bg-rose-900/50 dark:text-rose-100',
        warning: 'bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-100',
        ready: 'bg-sky-100 text-sky-800 dark:bg-sky-900/50 dark:text-sky-100',
    };
    element.className = `${base} ${tones[tone] || tones.ready}`;
}

function renderAuthoritySummary(review) {
    const project = review?.project || {};
    const spec = review?.spec || {};
    const pending = review?.pending_authority || {};
    const artifact = pending.artifact || {};
    const state = authorityReviewState(review);
    const findings = safeArray(pending.review_findings);

    const title = review?.post_accept === true
        ? 'Accepted Authority Invariants'
        : 'Pending Authority Review';
    setTextContent('authority-card-title', title);
    setTextContent('authority-review-state-badge', state.label);
    applyStateBadgeTone(document.getElementById('authority-review-state-badge'), state.tone);

    const sourcePath = spec.resolved_path || 'linked specification';
    const authorityLabel = pending.authority_id || pending.pending_authority_id || 'pending';
    const summaryPrefix = review?.post_accept === true
        ? `${project.name || 'Project'} authority is accepted.`
        : `${project.name || 'Project'} authority ${authorityLabel} awaits review.`;
    setTextContent(
        'authority-review-summary',
        `${summaryPrefix} Coverage: ${spec.coverage_summary?.omission_assessment || 'unknown'}. Source: ${sourcePath}.`,
    );

    const metrics = document.getElementById('authority-review-metrics');
    if (metrics) {
        metrics.replaceChildren(
            createMetricElement('Blockers', findings.filter((finding) => finding?.severity === 'blocking').length),
            createMetricElement('Invariants', safeArray(artifact.invariants).length),
            createMetricElement('Gaps', safeArray(artifact.gaps).length),
            createMetricElement('Assumptions', safeArray(artifact.assumptions).length),
            createMetricElement('Excluded', safeArray(artifact.rejected_features).length),
            createMetricElement('Rules', safeArray(artifact.eligible_feature_rules).length),
        );
    }

    setTextContent('authority-spec-location', sourcePath);
    setTextContent('authority-spec-hash', spec.spec_hash || spec.disk_sha256 || 'No spec hash available');
    setTextContent('authority-fingerprint', pending.authority_fingerprint || 'No authority fingerprint available');
    setTextContent(
        'authority-compiled-at',
        [pending.compiled_at || 'Compile time unavailable', pending.compiler_version || 'compiler unknown'].join(' · '),
    );
    setTextContent('authority-source-state', sourceStateLabel(spec));
    setTextContent('authority-decision-status', state.decision);
    setTextContent('authority-blocked-reason', state.reason);

    const acceptButton = document.getElementById('btn-accept-authority');
    if (acceptButton) acceptButton.disabled = state.acceptDisabled;

    const actionsContainer = document.getElementById('authority-review-actions-container');
    const findingsEl = document.getElementById('authority-review-findings');
    if (review?.post_accept === true) {
        if (actionsContainer) actionsContainer.classList.add('hidden');
        if (findingsEl) findingsEl.classList.add('hidden');
    } else {
        if (actionsContainer) actionsContainer.classList.remove('hidden');
        if (findingsEl) {
            const blockingCount = findings.filter((finding) => finding?.severity === 'blocking').length;
            findingsEl.textContent = blockingCount
                ? `${blockingCount} blocking review finding(s) require resolution or candidate-specific overrides.`
                : '';
            findingsEl.classList.toggle('hidden', blockingCount === 0);
        }
    }
}
```

- [ ] **Step 4: Run summary tests and verify GREEN**

Run:

```bash
uv run --frozen node --test tests/test_authority_review_console.mjs
```

Expected: PASS.

- [ ] **Step 5: Commit summary rendering**

Run:

```bash
git add frontend/project.js tests/test_authority_review_console.mjs
git commit -m "feat: render authority review summary rail"
```

Expected: commit succeeds.

### Task 5: Render Overview Lists and Safe Empty States

**Files:**
- Modify: `tests/test_authority_review_console.mjs`
- Modify: `frontend/project.js`

- [ ] **Step 1: Add failing overview-renderer tests**

Append:

```javascript
test('renderAuthorityOverview renders escaped findings and empty states', () => {
    const { documentStub } = createDocumentStub();
    globalThis.document = documentStub;

    const renderAuthorityOverview = loadAuthorityConsoleFunction(
        'renderAuthorityOverview',
        [
            'safeArray',
            'createEmptyState',
            'createFindingCard',
            'createSimpleCard',
            'renderListSection',
            'renderAuthorityOverview',
        ],
    );

    renderAuthorityOverview({
        pending_authority: {
            review_findings: [
                {
                    code: 'SCRIPT_FINDING',
                    severity: 'blocking',
                    message: '<script>alert("x")</script>',
                    candidate_ids: ['REQ.one'],
                },
            ],
            artifact: {
                gaps: [],
                assumptions: [{ id: 'ASM.one', text: '<img src=x onerror=alert(1)>' }],
                rejected_features: [],
                eligible_feature_rules: [],
            },
        },
    });

    const findings = documentStub.getElementById('overview-findings-list');
    const gaps = documentStub.getElementById('overview-gaps-list');
    const assumptions = documentStub.getElementById('overview-assumptions-list');

    assert.match(findings.textContent, /SCRIPT_FINDING/);
    assert.match(findings.textContent, /<script>alert/);
    assert.equal(findings.innerHTML, '');
    assert.match(gaps.textContent, /No identified gaps/);
    assert.match(assumptions.textContent, /<img src=x/);
});
```

- [ ] **Step 2: Run overview tests and verify RED**

Run:

```bash
uv run --frozen node --test tests/test_authority_review_console.mjs
```

Expected: FAIL because `renderAuthorityOverview`, `createEmptyState`,
`createFindingCard`, and `renderListSection` do not exist.

- [ ] **Step 3: Add overview helpers**

In `frontend/project.js`, after `createSimpleCard`, add:

```javascript
function createEmptyState(text) {
    const empty = document.createElement('div');
    empty.className = 'rounded-lg border border-dashed border-slate-200 bg-white/50 p-3 text-xs font-medium italic text-slate-500 dark:border-slate-800 dark:bg-slate-950/20 dark:text-slate-400';
    empty.textContent = text;
    return empty;
}

function createFindingCard(finding) {
    const card = document.createElement('div');
    card.className = 'rounded-lg border border-slate-200 bg-white/70 p-3 text-xs shadow-sm dark:border-slate-800 dark:bg-slate-950/30';

    const header = document.createElement('div');
    header.className = 'flex flex-wrap items-center gap-2';

    const code = document.createElement('span');
    code.className = 'rounded bg-slate-100 px-2 py-0.5 text-[10px] font-black uppercase text-slate-700 dark:bg-slate-800 dark:text-slate-200';
    code.textContent = finding?.code || 'FINDING';
    header.appendChild(code);

    const severity = document.createElement('span');
    severity.className = finding?.severity === 'blocking'
        ? 'rounded bg-rose-100 px-2 py-0.5 text-[10px] font-bold uppercase text-rose-800 dark:bg-rose-900/40 dark:text-rose-200'
        : 'rounded bg-amber-100 px-2 py-0.5 text-[10px] font-bold uppercase text-amber-800 dark:bg-amber-900/40 dark:text-amber-200';
    severity.textContent = finding?.severity || 'info';
    header.appendChild(severity);

    card.appendChild(header);

    const message = document.createElement('p');
    message.className = 'mt-2 whitespace-pre-wrap text-slate-700 dark:text-slate-300';
    message.textContent = finding?.message || finding?.detail || '';
    card.appendChild(message);

    const candidateIds = safeArray(finding?.candidate_ids);
    if (candidateIds.length > 0) {
        const candidates = document.createElement('div');
        candidates.className = 'mt-2 break-words font-mono text-[10px] text-slate-500 dark:text-slate-400';
        candidates.textContent = `Candidates: ${candidateIds.join(', ')}`;
        card.appendChild(candidates);
    }

    return card;
}

function renderListSection(containerId, items, emptyText, createItem) {
    const container = document.getElementById(containerId);
    if (!container) return;
    const normalized = safeArray(items);
    if (normalized.length === 0) {
        container.replaceChildren(createEmptyState(emptyText));
        return;
    }
    container.replaceChildren(...normalized.map((item) => createItem(item)));
}

function renderAuthorityOverview(review) {
    const pending = review?.pending_authority || {};
    const artifact = pending.artifact || {};

    renderListSection(
        'overview-findings-list',
        pending.review_findings,
        'No review findings.',
        createFindingCard,
    );
    renderListSection(
        'overview-gaps-list',
        artifact.gaps,
        'No identified gaps.',
        (gap) => createSimpleCard(gap, 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200'),
    );
    renderListSection(
        'overview-assumptions-list',
        artifact.assumptions,
        'No compiler assumptions.',
        (assumption) => createSimpleCard(assumption, 'bg-indigo-100 text-indigo-800 dark:bg-indigo-900/40 dark:text-indigo-200'),
    );
    renderListSection(
        'overview-exclusions-list',
        artifact.rejected_features,
        'No excluded features.',
        (feature) => createSimpleCard(feature, 'bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-200'),
    );
    renderListSection(
        'overview-eligible-rules-list',
        artifact.eligible_feature_rules,
        'No eligible feature rules.',
        (rule) => createSimpleCard(rule, 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200'),
    );
}
```

- [ ] **Step 4: Run overview tests and verify GREEN**

Run:

```bash
uv run --frozen node --test tests/test_authority_review_console.mjs
```

Expected: PASS.

- [ ] **Step 5: Commit overview rendering**

Run:

```bash
git add frontend/project.js tests/test_authority_review_console.mjs
git commit -m "feat: render authority review overview tab"
```

Expected: commit succeeds.

### Task 6: Render Filtered Invariants

**Files:**
- Modify: `tests/test_authority_review_console.mjs`
- Modify: `frontend/project.js`

- [ ] **Step 1: Add failing invariant filtering tests**

Append:

```javascript
test('filterAuthorityInvariants searches ID text refs excerpt and support', () => {
    const filterAuthorityInvariants = loadAuthorityConsoleFunction(
        'filterAuthorityInvariants',
        ['safeArray', 'invariantSearchText', 'filterAuthorityInvariants'],
    );
    const invariants = [
        {
            id: 'REQ.create',
            text: 'Create a todo',
            support: 'direct',
            source_refs: ['REQ.create'],
            source_excerpt: 'Enter key creates item',
        },
        {
            id: 'REQ.archive',
            text: 'Archive a todo',
            support: 'inferred',
            source_refs: ['REQ.archive'],
            source_excerpt: 'Move old item',
        },
    ];

    assert.deepEqual(
        filterAuthorityInvariants(invariants, 'enter', 'all').map((item) => item.id),
        ['REQ.create'],
    );
    assert.deepEqual(
        filterAuthorityInvariants(invariants, '', 'inferred').map((item) => item.id),
        ['REQ.archive'],
    );
});
```

- [ ] **Step 2: Run invariant tests and verify RED**

Run:

```bash
uv run --frozen node --test tests/test_authority_review_console.mjs
```

Expected: FAIL because `filterAuthorityInvariants` and `invariantSearchText`
do not exist.

- [ ] **Step 3: Add invariant filtering and renderer helpers**

In `frontend/project.js`, after `createInvariantCard`, add:

```javascript
function invariantSearchText(invariant) {
    return [
        invariant?.id,
        invariant?.text,
        invariant?.support,
        safeArray(invariant?.source_refs).join(' '),
        invariant?.source_excerpt,
    ].filter(Boolean).join(' ').toLowerCase();
}

function filterAuthorityInvariants(invariants, query, supportFilter) {
    const normalizedQuery = String(query || '').trim().toLowerCase();
    return safeArray(invariants).filter((invariant) => {
        const support = invariant?.support || 'inferred';
        const supportMatches = supportFilter === 'all' || support === supportFilter;
        const queryMatches = !normalizedQuery
            || invariantSearchText(invariant).includes(normalizedQuery);
        return supportMatches && queryMatches;
    });
}

function renderAuthorityInvariants(artifact) {
    const domainBadge = document.getElementById('authority-domain-badge');
    const themesContainer = document.getElementById('authority-themes-container');
    const invariantsList = document.getElementById('invariants-list');
    const status = document.getElementById('authority-invariant-filter-status');

    if (domainBadge) domainBadge.textContent = artifact?.domain || 'N/A';

    if (themesContainer) {
        const themes = safeArray(artifact?.scope_themes);
        if (themes.length === 0) {
            themesContainer.replaceChildren(createEmptyState('No scope themes.'));
        } else {
            const badges = themes.map((theme) => {
                const span = document.createElement('span');
                span.className = 'px-2 py-0.5 rounded-full bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300 font-semibold text-xs border border-slate-200 dark:border-slate-700';
                span.textContent = theme;
                return span;
            });
            themesContainer.replaceChildren(...badges);
        }
    }

    const allInvariants = safeArray(artifact?.invariants);
    const filtered = filterAuthorityInvariants(
        allInvariants,
        authorityInvariantSearchQuery,
        authoritySupportFilter,
    );

    if (status) {
        status.textContent = `${filtered.length} of ${allInvariants.length} invariants shown`;
    }

    if (!invariantsList) return;
    if (filtered.length === 0) {
        invariantsList.replaceChildren(createEmptyState('No invariants match the current filters.'));
        return;
    }
    invariantsList.replaceChildren(...filtered.map((invariant) => createInvariantCard(invariant)));
}

function attachAuthorityReviewControls() {
    const search = document.getElementById('authority-invariant-search');
    const support = document.getElementById('authority-support-filter');

    if (search && search.dataset.bound !== 'true') {
        search.dataset.bound = 'true';
        search.addEventListener('input', () => {
            authorityInvariantSearchQuery = search.value || '';
            renderAuthorityReviewCard(Boolean(currentAuthorityReview));
        });
    }

    if (support && support.dataset.bound !== 'true') {
        support.dataset.bound = 'true';
        support.addEventListener('change', () => {
            authoritySupportFilter = support.value || 'all';
            renderAuthorityReviewCard(Boolean(currentAuthorityReview));
        });
    }
}
```

- [ ] **Step 4: Run invariant tests and verify GREEN**

Run:

```bash
uv run --frozen node --test tests/test_authority_review_console.mjs
```

Expected: PASS.

- [ ] **Step 5: Commit invariant filtering**

Run:

```bash
git add frontend/project.js tests/test_authority_review_console.mjs
git commit -m "feat: filter authority review invariants"
```

Expected: commit succeeds.

### Task 7: Wire the New Renderers Into `renderAuthorityReviewCard`

**Files:**
- Modify: `frontend/project.js`
- Test: `tests/test_authority_review_console.mjs`

- [ ] **Step 1: Update tab switching for the Overview tab**

Replace the existing `switchAuthorityReviewTab` with:

```javascript
function switchAuthorityReviewTab(tabName) {
    currentAuthorityReviewActiveTab = tabName;
    const tabs = ['overview', 'invariants', 'spec', 'raw'];

    tabs.forEach((tab) => {
        const btn = document.getElementById(`tab-btn-${tab}`);
        const content = document.getElementById(`tab-content-${tab}`);
        if (!btn || !content) return;

        if (tab === tabName) {
            btn.className = 'px-4 py-2 -mb-px text-xs font-bold border-b-2 border-sky-600 text-sky-600 dark:border-sky-400 dark:text-sky-300 transition-colors focus:outline-none';
            content.classList.remove('hidden');
        } else {
            btn.className = 'px-4 py-2 -mb-px text-xs font-bold border-b-2 border-transparent text-slate-500 hover:text-sky-600 dark:text-slate-400 dark:hover:text-sky-300 transition-colors focus:outline-none';
            content.classList.add('hidden');
        }
    });
}
```

- [ ] **Step 2: Replace the body of `renderAuthorityReviewCard`**

Inside `frontend/project.js`, replace the existing `renderAuthorityReviewCard`
body with:

```javascript
function renderAuthorityReviewCard(visible) {
    const card = document.getElementById('authority-review-card');
    if (!card) return;

    if (!visible) {
        card.classList.add('hidden');
        return;
    }

    card.classList.remove('hidden');
    attachAuthorityReviewControls();

    const preview = document.getElementById('authority-review-preview');
    const specViewer = document.getElementById('spec-source-viewer');
    const truncationBanner = document.getElementById('spec-truncation-banner');

    if (!currentAuthorityReview) {
        setTextContent('authority-card-title', 'Loading Authority Review');
        setTextContent('authority-review-state-badge', 'Loading');
        setTextContent('authority-review-summary', 'Fetching review data from backend...');
        setTextContent('authority-decision-status', 'Loading review packet.');
        setTextContent('authority-blocked-reason', '');
        if (preview) preview.textContent = '';
        if (specViewer) specViewer.textContent = '';
        if (truncationBanner) truncationBanner.classList.add('hidden');
        renderAuthorityOverview({ pending_authority: { artifact: {} } });
        renderAuthorityInvariants({});
        return;
    }

    const isPostAccept = currentAuthorityReview.post_accept === true;
    if (isPostAccept) {
        card.className = 'mt-4 rounded-lg border border-slate-200 bg-slate-50 p-4 text-sm text-slate-900 dark:border-slate-800 dark:bg-slate-900/20 dark:text-slate-100';
    } else {
        card.className = 'mt-4 rounded-lg border border-sky-200 bg-sky-50 p-4 text-sm text-sky-900 dark:border-sky-800 dark:bg-sky-950/40 dark:text-sky-100';
    }

    const spec = currentAuthorityReview.spec || {};
    const pending = currentAuthorityReview.pending_authority || {};
    const artifact = pending.artifact || {};

    renderAuthoritySummary(currentAuthorityReview);
    renderAuthorityOverview(currentAuthorityReview);
    renderAuthorityInvariants(artifact);

    if (specViewer) {
        const specContent = spec.source_content || spec.excerpt || '';
        specViewer.textContent = specContent || 'Specification source content is unavailable in this review packet.';
    }
    if (truncationBanner) {
        truncationBanner.classList.toggle('hidden', spec.content_truncated !== true);
    }

    if (preview) {
        const previewPayload = {
            project: currentAuthorityReview.project || {},
            spec: {
                resolved_path: spec.resolved_path,
                spec_hash: spec.spec_hash,
                disk_sha256: spec.disk_sha256,
                content_included: spec.content_included,
                content_truncated: spec.content_truncated,
                coverage_summary: spec.coverage_summary,
                coverage_diagnostics: spec.coverage_diagnostics,
            },
            pending_authority: pending,
            post_accept: currentAuthorityReview.post_accept === true,
        };
        preview.textContent = JSON.stringify(previewPayload, null, 2);
    }

    switchAuthorityReviewTab(currentAuthorityReviewActiveTab);
}
```

- [ ] **Step 3: Ensure `createSimpleCard` is still DOM-safe**

Within `createSimpleCard`, ensure the content assignment still uses:

```javascript
contentDiv.textContent = item.text || item.message || item.code || '';
```

Do not use `innerHTML` for authority artifact content.

- [ ] **Step 4: Run JS console tests**

Run:

```bash
uv run --frozen node --test tests/test_authority_review_console.mjs
```

Expected: PASS.

- [ ] **Step 5: Run the focused Python dashboard test that checks card copy**

Run:

```bash
uv run --frozen pytest tests/test_api_dashboard.py::test_dashboard_pending_review_copy_is_not_project_setup_required -q
```

Expected: PASS.

- [ ] **Step 6: Commit the renderer wiring**

Run:

```bash
git add frontend/project.js
git commit -m "feat: wire authority review console renderer"
```

Expected: commit succeeds.

### Task 8: Strengthen Accepted Fallback API Regression

**Files:**
- Modify: `tests/test_api_dashboard.py`

- [ ] **Step 1: Add failing assertions to the accepted fallback test**

In `tests/test_api_dashboard.py`, extend
`test_get_project_authority_review_post_accept_fallback` after the existing
assertions:

```python
    assert payload["data"]["spec"]["source_content"] == "spec source content"
    assert payload["data"]["spec"]["content_included"] is True
    assert payload["data"]["spec"]["content_truncated"] is False
    assert payload["data"]["pending_authority"]["artifact"]["domain"] == "test"
    assert payload["data"]["pending_authority"]["review_findings"] == []
    assert payload["data"]["pending_authority"]["authority_fingerprint"] == (
        "fingerprint"
    )
```

- [ ] **Step 2: Run the focused test and verify result**

Run:

```bash
uv run --frozen pytest tests/test_api_dashboard.py::test_get_project_authority_review_post_accept_fallback -q
```

Expected: PASS if the existing endpoint already exposes the required fields. If
it fails, inspect the failure and make the smallest API response mapping change
in `api.py` to expose the missing field from the existing
`AuthorityReviewSnapshot`; do not change endpoint shape beyond adding the
missing existing fields.

- [ ] **Step 3: Commit the API regression**

Run:

```bash
git add tests/test_api_dashboard.py api.py
git commit -m "test: assert accepted authority review artifact payload"
```

Expected: commit succeeds. If `api.py` was not changed because the test already
passed, `git add api.py` is harmless but should not stage changes.

### Task 9: Add Safe Rendering Static Guard

**Files:**
- Modify: `tests/test_authority_review_console.mjs`
- Test: `frontend/project.js`

- [ ] **Step 1: Add a static safety test for authority content rendering**

Append:

```javascript
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
```

- [ ] **Step 2: Run the safety test and verify GREEN**

Run:

```bash
uv run --frozen node --test tests/test_authority_review_console.mjs
```

Expected: PASS. If this fails, replace the unsafe assignment with
`textContent`, `replaceChildren`, or fixed-string-only DOM creation.

- [ ] **Step 3: Commit the safety guard**

Run:

```bash
git add tests/test_authority_review_console.mjs frontend/project.js
git commit -m "test: guard authority review console safe rendering"
```

Expected: commit succeeds.

### Task 10: Focused Validation and Refactor

**Files:**
- Modify only if a verification failure identifies a concrete issue:
  - `frontend/project.html`
  - `frontend/project.js`
  - `tests/test_authority_review_console.mjs`
  - `tests/test_api_dashboard.py`
  - `api.py`

- [ ] **Step 1: Run focused frontend and API tests**

Run:

```bash
uv run --frozen node --test tests/test_authority_review_console.mjs
uv run --frozen pytest tests/test_api_dashboard.py -q
```

Expected:

- Node test: all tests pass.
- Pytest: `tests/test_api_dashboard.py` passes.

- [ ] **Step 2: Run lint/type checks for touched Python files**

Run:

```bash
uv run --frozen python -m ruff check api.py tests/test_api_dashboard.py
uv run --frozen python -m ty check api.py tests/test_api_dashboard.py
```

Expected: all checks pass.

- [ ] **Step 3: Refactor only after green**

Allowed refactors after tests are green:

- Extract repeated class strings into local constants only if a function becomes
  hard to read.
- Rename helper functions for clarity if all test loader names are updated in
  the same commit.
- Keep existing backend endpoint behavior unchanged.

Run the focused tests again after any refactor:

```bash
uv run --frozen node --test tests/test_authority_review_console.mjs
uv run --frozen pytest tests/test_api_dashboard.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit validation refactors if any files changed**

Run:

```bash
git status --short
git add frontend/project.html frontend/project.js tests/test_authority_review_console.mjs tests/test_api_dashboard.py api.py
git commit -m "refactor: polish authority review console rendering"
```

Expected: commit succeeds only if there are actual refactor changes. If
`git status --short` is empty, skip the commit.

### Task 11: Browser Smoke Test

**Files:**
- No planned source changes unless smoke testing exposes a concrete bug.

- [ ] **Step 1: Start the dashboard server**

Run:

```bash
uv run --frozen uvicorn api:app --reload --port 8000
```

Expected: server starts on `http://127.0.0.1:8000`.

- [ ] **Step 2: Open a project with pending or accepted authority**

Use the browser at:

```text
http://127.0.0.1:8000/dashboard/project.html?id=2
```

Expected:

- Summary band is visible.
- Overview tab is selected by default.
- Invariants tab search and support filter update the visible invariant count.
- Spec Source tab renders source text literally.
- Raw JSON tab remains readable.
- Pending authority shows `Accept Authority` and `Request Refinement`.
- Accepted authority hides decision inputs and remains readable.

- [ ] **Step 3: Stop the server**

Stop the server process with `Ctrl-C`.

- [ ] **Step 4: Fix only concrete smoke-test defects**

If smoke testing exposes a defect, write or update a test first, verify RED,
make the minimal fix, verify GREEN, then commit:

```bash
git add frontend/project.html frontend/project.js tests/test_authority_review_console.mjs tests/test_api_dashboard.py api.py
git commit -m "fix: correct authority review console smoke issue"
```

Expected: commit succeeds only when a smoke-test fix exists.

### Task 12: Full Repository Gate

**Files:**
- No source changes expected.

- [ ] **Step 1: Run the standard repository gate**

Run:

```bash
pyrepo-check
```

Expected:

- Ruff passes.
- Annotation check passes.
- `ty` passes.
- Bandit passes.
- Pytest passes.

- [ ] **Step 2: Check final branch status**

Run:

```bash
git status --short
git log --oneline --decorate -5
```

Expected:

- Working tree is clean.
- Latest commits belong to `dev/authority-review-console`.

- [ ] **Step 3: Request code review**

Use `superpowers:requesting-code-review` before merging or presenting the work
as complete. Review scope:

- `frontend/project.html`
- `frontend/project.js`
- `tests/test_authority_review_console.mjs`
- `tests/test_api_dashboard.py`
- `api.py` only if changed by Task 8

Expected: no critical findings. Any critical finding must be fixed with TDD
before finishing the branch.

## Self-Review

**Spec coverage:** The plan implements the approved review-console shape:
summary band in Task 2 and Task 4, overview/invariants/spec/raw tabs in Tasks 2
and 5-7, decision rail in Task 2 and Task 4, safe rendering in Tasks 5 and 9,
accepted fallback coverage in Task 8, and final browser/repo validation in Tasks
11-12.

**Placeholder scan:** The plan avoids open-ended placeholders. Every code change
step includes concrete code or an exact assertion. The only conditional code path
is Task 8, where the test is expected to pass unless the current API omits an
existing field; the allowed remediation is explicitly bounded to exposing fields
already present on `AuthorityReviewSnapshot`.

**Type/name consistency:** Helper names used by tests match the implementation
snippets: `safeArray`, `authorityReviewState`, `setTextContent`,
`createMetricElement`, `renderAuthoritySummary`, `createEmptyState`,
`createFindingCard`, `renderListSection`, `renderAuthorityOverview`,
`invariantSearchText`, `filterAuthorityInvariants`,
`renderAuthorityInvariants`, and `attachAuthorityReviewControls`.

## Execution Handoff

Plan complete and saved to
`docs/superpowers/plans/2026-05-21-authority-review-console.md`.

Two execution options:

1. **Subagent-Driven (recommended)** - dispatch a fresh subagent per task, review
   between tasks, fast iteration.
2. **Inline Execution** - execute tasks in this session using
   `superpowers:executing-plans`, batch execution with checkpoints.
