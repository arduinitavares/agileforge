import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';

const projectHtmlPath = path.resolve(import.meta.dirname, '../frontend/project.html');
const projectJsPath = path.resolve(import.meta.dirname, '../frontend/project.js');
const projectHtmlSource = fs.readFileSync(projectHtmlPath, 'utf8');
const projectJsSource = fs.readFileSync(projectJsPath, 'utf8');

function functionSource(name) {
    const start = projectJsSource.indexOf(`function ${name}(`);
    const asyncStart = projectJsSource.indexOf(`async function ${name}(`);
    const functionStart = asyncStart >= 0 ? asyncStart : start;
    assert.notEqual(functionStart, -1, `${name} should exist in frontend/project.js`);

    const bodyStart = projectJsSource.indexOf('{', functionStart);
    assert.notEqual(bodyStart, -1, `${name} should have a function body`);

    let depth = 0;
    for (let index = bodyStart; index < projectJsSource.length; index += 1) {
        if (projectJsSource[index] === '{') depth += 1;
        if (projectJsSource[index] === '}') depth -= 1;
        if (depth === 0) return projectJsSource.slice(functionStart, index + 1);
    }

    assert.fail(`${name} should have a complete function body`);
}

test('project.js parses as a browser script', () => {
    assert.doesNotThrow(() => {
        new vm.Script(projectJsSource, { filename: projectJsPath });
    });
});

test('Story phase exposes saved requirement selection controls', () => {
    assert.match(projectHtmlSource, /btn-complete-story-selection/);
    assert.match(projectHtmlSource, /Plan Sprint from Selection/);
});

test('Story phase completion can post an explicit selected scope', () => {
    const source = functionSource('completeSelectedStoryScope');

    assert.match(projectJsSource, /function toggleStorySelectionRequirement/);
    assert.match(source, /headers: \{ 'Content-Type': 'application\/json' \}/);
    assert.match(source, /expected_state: 'STORY_PERSISTENCE'/);
    assert.match(source, /complete-story-selection-/);
    assert.match(source, /scope: 'selection'/);
    assert.match(source, /parent_requirements: selectedRequirements/);
});

test('whole Story phase completion posts an explicit guarded JSON body', () => {
    const source = functionSource('completeStoryPhase');

    assert.match(source, /headers: \{ 'Content-Type': 'application\/json' \}/);
    assert.match(source, /expected_state: 'STORY_PERSISTENCE'/);
    assert.match(source, /complete-story-full-/);
});

test('Story selection button state depends on selectable requirements and selected count', () => {
    const source = functionSource('updateCompleteStoryPhaseButton');

    assert.match(source, /btn-complete-story-selection/);
    assert.match(source, /const selectedCount = selectedStoryScopeNames\(\)\.length/);
    assert.match(source, /selectionBtn\.disabled = selectedCount === 0/);
    assert.match(source, /const hasSelectableRequirements = storyRequirements\.some\(r => isResolvedStoryStatus\(r\.status\)\)/);
    assert.match(source, /selectionBtn\.className = hasSelectableRequirements/);
    assert.match(source, /: 'hidden items-center/);
});

test('unresolved Story rows render non-intercepting selection placeholders', () => {
    const source = functionSource('renderStoryRequirementsList');

    assert.match(source, /const selectionControl = isSelectableForScope\s*\?/);
    assert.match(source, /<button type="button" data-story-scope-toggle/);
    assert.match(source, /:\s*`[\s\S]*<span class="\$\{selectionClasses\}"[\s\S]*aria-hidden="true"/);
    assert.match(source, /if \(scopeToggle && isSelectableForScope\)/);
});

test('loadStoryRequirements prunes stale selected requirements to current resolved items', () => {
    const source = functionSource('loadStoryRequirements');

    assert.match(source, /selectedStoryScopeRequirements = new Set\(/);
    assert.match(source, /filter\(req => isResolvedStoryStatus\(req\.status\)\)/);
    assert.match(source, /filter\(requirement => selectableRequirementNames\.has\(requirement\)\)/);
});

test('Story selection handlers are exported for inline browser controls', () => {
    assert.match(projectJsSource, /window\.toggleStorySelectionRequirement = toggleStorySelectionRequirement/);
    assert.match(projectJsSource, /window\.completeSelectedStoryScope = completeSelectedStoryScope/);
});
