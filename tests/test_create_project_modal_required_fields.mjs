import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

const indexHtmlPath = path.resolve(import.meta.dirname, '../frontend/index.html');
const indexHtmlSource = fs.readFileSync(indexHtmlPath, 'utf8');

test('create project modal marks required fields with decorative indicators', () => {
    assert.match(
        indexHtmlSource,
        /<label for="modal-project-name"[^>]*>Project Name\s*<span[^>]*aria-hidden="true"[^>]*>\*<\/span><\/label>/,
    );
    assert.match(
        indexHtmlSource,
        /<input id="modal-project-name"[^>]*required[^>]*>/,
    );
    assert.match(
        indexHtmlSource,
        /<label for="modal-project-origin"[^>]*>Origin\s*<span[^>]*aria-hidden="true"[^>]*>\*<\/span><\/label>/,
    );
    assert.match(
        indexHtmlSource,
        /<select id="modal-project-origin"[^>]*required[^>]*>/,
    );
});
