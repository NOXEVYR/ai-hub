const test = require('node:test');
const assert = require('node:assert/strict');
const {createDraftGuard, describe, mount} = require('../frontend/app-update.js');

test('save clears only its own edits; detached page drafts remain guarded', () => {
  const guard = createDraftGuard(), settings = {}, otherPage = {};
  assert.equal(guard.dirty(), false);
  guard.edit(settings); guard.edit(otherPage);
  guard.saved([settings]);
  assert.equal(guard.dirty(), true);
  guard.saved([otherPage]);
  assert.equal(guard.dirty(), false);
});

test('desktop bridge receives only a constant open-dialog request', async () => {
  let click; const sent = [];
  const win = {document:{addEventListener(){}, getElementById(){return {addEventListener(_event, fn){click=fn;}};}},
    chrome:{webview:{postMessage(value){sent.push(value);}}},
    fetch(){throw new Error('web must not invoke privileged update API');}};
  mount(win); await click();
  assert.deepEqual(sent, ['open-app-update']);
  assert.equal(win.aiHubHasUnsavedChanges(), false);
});

test('software status includes current and target version', () => {
  assert.equal(describe({state:'ready',current_version:'2.12.0',latest_version:'2.12.1'}),
    '更新已准备好 · 当前 2.12.0 · 最新 2.12.1');
});
