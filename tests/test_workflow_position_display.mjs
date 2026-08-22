import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';

const sourcePath = path.resolve(import.meta.dirname, '../frontend/project.js');
const source = fs.readFileSync(sourcePath, 'utf8');

function loadFrontend() {
    const createElement = () => ({
        _textContent: '',
        innerHTML: '',
        set textContent(value) {
            this._textContent = String(value ?? '');
            this.innerHTML = this._textContent
                .replaceAll('&', '&amp;')
                .replaceAll('<', '&lt;')
                .replaceAll('>', '&gt;')
                .replaceAll('"', '&quot;');
        },
        get textContent() { return this._textContent; },
    });
    const context = vm.createContext({
        console,
        crypto: { randomUUID: () => 'uuid-1' },
        document: {
            createElement,
            getElementById() { return null; },
            querySelector() { return null; },
            addEventListener() {},
        },
        fetch: async () => ({ ok: true, text: async () => '{}' }),
        URLSearchParams,
        window: { addEventListener() {}, location: { href: '' } },
    });
    vm.runInContext(source, context, { filename: sourcePath });
    return context;
}

test('human lifecycle labels are the exact direct-Spec sequence', () => {
    const context = loadFrontend();
    assert.deepEqual(Array.from(context.lifecycleStageLabels()), [
        'Vision',
        'Product Goal',
        'Specification',
        'Backlog',
        'Roadmap',
        'Stories',
        'Sprint',
        'Execution',
        'Review',
    ]);
});

test('workflow position hides graph guards and machine fingerprints', () => {
    const context = loadFrontend();
    const markup = context.workflowPositionMarkup({
        graph_version: 'agileforge.workflow.v2',
        fact_fingerprint: 'sha256:secret-facts',
        decisions: [{
            node_id: 'backlog.generate',
            child_graph_id: 'backlog',
            request_kind: 'record_backlog_draft',
            category: 'available',
            reason_code: 'BACKLOG_REQUIRED',
            decision_fingerprint: 'sha256:secret-decision',
        }],
    });
    assert.ok(markup.includes('Backlog'));
    assert.ok(!markup.includes('secret-facts'));
    assert.ok(!markup.includes('secret-decision'));
    assert.ok(!markup.includes('backlog.generate'));
});
