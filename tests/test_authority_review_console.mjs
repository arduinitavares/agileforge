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
