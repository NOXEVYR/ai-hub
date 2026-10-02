const test = require('node:test');
const assert = require('node:assert/strict');
const {createDraftGuard, mount} = require('../frontend/app-update.js');

function editor(key) {
  return {dataset:key ? {appDraftKey:key} : {},
    matches:() => true, querySelectorAll:() => [], closest:() => null};
}
function page() {
  const listeners = {};
  const win = {document:{addEventListener:(event, callback) => { listeners[event] = callback; },
    getElementById:() => null}};
  mount(win);
  return {win, input:target => listeners.input({target}), change:target => listeners.change({target})};
}

test('unsaved notes block; only successful save clears their submitted draft', () => {
  const {win, input} = page(), notes = editor('model:1:notes');
  input(notes);
  const snapshot = win.AIHubAppUpdate.snapshotControls([notes]);
  assert.equal(win.aiHubHasUnsavedChanges(), true); // A failed request leaves the snapshot untouched.
  win.AIHubAppUpdate.savedControls(snapshot);
  assert.equal(win.aiHubHasUnsavedChanges(), false);
});

test('rating click and later edits remain dirty after saving an earlier note version', () => {
  const {win, input} = page(), notes = editor('model:1:notes'), rating = editor('model:1:rating');
  input(notes); win.AIHubAppUpdate.edit(rating);
  const snapshot = win.AIHubAppUpdate.snapshotControls([notes, rating]);
  input(notes);
  win.AIHubAppUpdate.savedControls(snapshot);
  assert.equal(win.aiHubHasUnsavedChanges(), true);
  win.AIHubAppUpdate.savedControls(win.AIHubAppUpdate.snapshotControls([notes]));
  assert.equal(win.aiHubHasUnsavedChanges(), false);
});

test('stable model keys clear detached saved editors without clearing another model', () => {
  const guard = createDraftGuard(), original = editor('model:1:notes'), other = editor('model:2:notes');
  guard.edit(original); guard.edit(other);
  const replacement = editor('model:1:notes');
  guard.saved([replacement], guard.snapshot([replacement]));
  assert.equal(guard.dirty(), true);
  guard.saved([other]);
  assert.equal(guard.dirty(), false);
});

test('new control for the same draft cannot be cleared by a pending older save', () => {
  const guard = createDraftGuard(), first = editor('model:1:notes');
  guard.edit(first);
  const pending = guard.snapshot([first]), replacement = editor('model:1:notes');
  guard.edit(replacement);
  guard.savedSnapshot(pending);
  assert.equal(guard.dirty(), true);
});

test('model/gallery filters and ordinary search do not become unsavable drafts', () => {
  const {win, input, change} = page();
  input(editor()); change(editor());
  const search = editor('search'); search.type = 'search'; input(search);
  assert.equal(win.aiHubHasUnsavedChanges(), false);
  input(editor('settings:ai_root'));
  assert.equal(win.aiHubHasUnsavedChanges(), true);
});

test('single editor and container save APIs retain existing settings compatibility', () => {
  const {win, input} = page(), settings = editor('settings:root');
  input(settings);
  const container = {querySelectorAll:() => [settings]};
  const snapshot = win.AIHubAppUpdate.snapshot(container);
  win.AIHubAppUpdate.saved(container, snapshot);
  assert.equal(win.aiHubHasUnsavedChanges(), false);
  input(settings);
  win.AIHubAppUpdate.saved(settings);
  assert.equal(win.aiHubHasUnsavedChanges(), false);
});
