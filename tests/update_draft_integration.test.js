'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {mount} = require('../frontend/app-update.js');
const app = readFileSync(path.join(__dirname, '../frontend/app.js'), 'utf8');
const drawerCode = app.slice(app.indexOf('  // ---------- 模型详情抽屉 ----------'), app.indexOf('  // ================= 更新中心'));
const closeCode = app.slice(app.indexOf('  function closeDrawer()'), app.indexOf('  $("#drawer-mask").onclick'));
const settingsCode = app.slice(app.indexOf('  pages.settings ='), app.indexOf('  // ---------- 全局动作'));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));

// Exercise the actual page handlers against a small DOM boundary. No handler
// logic is duplicated here; the native draft guard supplies revision tracking.
function harness() {
  const listeners = {}, nodes = new Map(), messages = [], requests = [];
  let html = '';
  const make = (id, key) => ({id, value:'', dataset:key ? {appDraftKey:key} : {},
    disabled:false, isConnected:true, innerHTML:'', scrollTop:0,
    matches:selector => selector.includes('input'), closest:() => null, focus:() => {},
    querySelectorAll:() => [], classList:{toggle:() => {}, contains:() => false, add:() => {}}});
  const spans = Array.from({length:10}, (_, i) => {
    let on = false;
    return {dataset:{i:String(i + 1)}, classList:{toggle:(_, value) => { on = value; }, contains:() => on}};
  });
  const host = make('host');
  Object.defineProperty(host, 'innerHTML', {get:() => html, set:value => {
    html = value; nodes.clear();
    for (const match of value.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)) {
      const control = make(match[1], match[0].match(/data-app-draft-key="([^"]+)"/)?.[1]);
      control.value = match[0].match(/value="([^"]*)"/)?.[1] || '';
      nodes.set(control.id, control);
    }
    nodes.set('drawer', host); nodes.set('close', make('close'));
    nodes.set('drawer-mask', make('drawer-mask'));
  }});
  host.matches = () => false;
  host.querySelectorAll = () => [...nodes.values()].filter(control => control !== host && control.dataset.appDraftKey);
  const dollar = selector => selector === '.close' ? nodes.get('close') : nodes.get(selector.slice(1));
  const dollars = (selector, container) => {
    if (selector === 'span' || selector === '#stars span') return spans;
    if (selector === 'input,textarea') return [...nodes.values()].filter(control => control.id.startsWith('s-') && !['s-save','s-detect','s-rescan'].includes(control.id));
    if (selector === '[data-app-draft-key]') return host.querySelectorAll();
    return [];
  };
  const win = {document:{addEventListener:(kind, callback) => { listeners[kind] = callback; }, getElementById:() => null}};
  mount(win);
  let respond = async url => url.startsWith('/api/model/') ? model() : settings();
  const context = {
    window:win, pages:{}, $:dollar, $$:dollars, esc:escape,
    api:async (url, opts = {}) => { requests.push({url, body:opts.body}); return respond(url, opts); },
    toast:(message, kind) => messages.push({message, kind}),
    drawerRequest:0, activeDrawerId:null, activeDrawerPath:null, openDrawer:value => { host.innerHTML = value; },
    icon:() => '', typeBadge:() => '', stateBadge:() => '', loraIntro:() => '', classificationSection:() => '',
    scopes:{}, thumbURL:() => '', copyPath:() => {}, openLightbox:() => {},
    AIHubWorkspace:{banner:() => ''}, overlays:{hide(){}},
  };
  vm.runInNewContext(closeCode + drawerCode + settingsCode + '\nthis.open=openModelDrawer;this.close=closeDrawer;this.captureSettings=captureSettingsDraft;', context);
  return {context, host, nodes, spans, win, messages, requests,
    respond:callback => { respond = callback; },
    input:(id, value) => { const control = nodes.get(id); control.value = value; listeners.input({target:control}); },
    open:() => context.open(42), settings:restored => context.pages.settings(host, null, restored),
    html:() => html};
}
const model = () => ({filename:'sample.safetensors', path:'D:/Models/sample.safetensors', images:[], rating:0,
  notes:'', size_h:'1 MiB', value_score:0, value_tag:'暂无引用', source_url:'', update_state:'unchecked'});
const settings = () => ({network:{civitai_base:'https://example.test', hf_base:'https://example.test', request_interval:1.2},
  ai_root:'D:/AI', scan_roots:[], output_roots:[], ignore_dirs:[]});

test('note/rating save preserves later edits, restores the button and clears only submitted revisions', async () => {
  const h = harness(); await h.open();
  h.input('notes', 'submitted');
  h.nodes.get('stars').onclick({target:h.spans[2]});
  const pending = deferred(); h.respond(() => pending.promise);
  const button = h.nodes.get('btn-save-note');
  const save = button.onclick();
  assert.equal(button.disabled, true);
  assert.equal(h.requests.at(-1).body.notes, 'submitted');
  assert.equal(h.requests.at(-1).body.rating, 3);
  h.input('notes', 'new draft');
  pending.resolve({ok:true}); await save;
  assert.equal(button.disabled, false);
  assert.equal(h.win.aiHubHasUnsavedChanges(), true);
  h.respond(async () => ({ok:true})); await button.onclick();
  assert.equal(h.win.aiHubHasUnsavedChanges(), false);
});

test('failed note save leaves its draft available for retry', async () => {
  const h = harness(); await h.open(); h.input('notes', 'retry me');
  h.respond(async () => { throw new Error('offline'); });
  const button = h.nodes.get('btn-save-note'); await button.onclick();
  assert.equal(button.disabled, false);
  assert.equal(h.nodes.get('notes').value, 'retry me');
  assert.equal(h.win.aiHubHasUnsavedChanges(), true);
  assert.equal(h.messages.at(-1).kind, 'err');
  h.respond(async () => ({ok:true})); await button.onclick();
  assert.equal(h.win.aiHubHasUnsavedChanges(), false);
});

test('source bind and update check refresh metadata without replacing note/source controls', async () => {
  const h = harness(); await h.open();
  h.input('notes', 'keep these notes'); h.input('src-input', 'https://example.test/old');
  const notes = h.nodes.get('notes'), source = h.nodes.get('src-input');
  const pending = deferred();
  h.respond(async url => url.endsWith('/source') ? pending.promise : model());
  const save = h.nodes.get('btn-save-src').onclick();
  h.input('src-input', 'https://example.test/new'); pending.resolve({ok:true}); await save;
  assert.equal(h.nodes.get('notes'), notes);
  assert.equal(h.nodes.get('src-input'), source);
  assert.equal(notes.value, 'keep these notes');
  assert.equal(source.value, 'https://example.test/new');
  assert.equal(h.win.aiHubHasUnsavedChanges(), true);
  h.respond(async url => url.endsWith('/check') ? {result:{state:'ok', message:'checked'}} : model());
  await h.nodes.get('btn-check').onclick();
  assert.equal(h.nodes.get('notes'), notes);
  assert.equal(notes.value, 'keep these notes');
});

test('source discovery fills an empty candidate as a draft but never overwrites input typed during discovery', async () => {
  const h = harness(); await h.open();
  h.respond(async () => ({url:'https://example.test/candidate', note:'metadata'}));
  await h.nodes.get('btn-resolve').onclick();
  assert.equal(h.nodes.get('src-input').value, 'https://example.test/candidate');
  assert.equal(h.win.aiHubHasUnsavedChanges(), true);
  const next = harness(); await next.open(); const pending = deferred(); next.respond(() => pending.promise);
  const discovery = next.nodes.get('btn-resolve').onclick();
  next.input('src-input', 'https://example.test/manual');
  pending.resolve({url:'https://example.test/candidate', note:'metadata'}); await discovery;
  assert.equal(next.nodes.get('src-input').value, 'https://example.test/manual');
});

test('settings save leaves edits made in flight dirty, failures retry, and automatic detection marks its fill as a draft', async () => {
  const h = harness(); await h.settings();
  h.input('s-roots', '[]'); h.input('s-outputs', '[]'); h.input('s-root', 'D:/Initial');
  const pending = deferred(); h.respond(() => pending.promise);
  const button = h.nodes.get('s-save'), save = button.onclick();
  h.input('s-root', 'D:/Later'); pending.resolve({ok:true}); await save;
  assert.equal(button.disabled, false);
  assert.equal(h.win.aiHubHasUnsavedChanges(), true);
  h.respond(async () => { throw new Error('save failed'); }); await button.onclick();
  assert.equal(button.disabled, false); assert.equal(h.messages.at(-1).kind, 'err');
  h.respond(async () => ({ok:true})); await button.onclick();
  assert.equal(h.win.aiHubHasUnsavedChanges(), false);
  h.respond(async () => ({ai_root:'D:/Detected', scan_roots:[], output_roots:[]}));
  await h.nodes.get('s-detect').onclick();
  assert.equal(h.nodes.get('s-root').value, 'D:/Detected');
  assert.equal(h.win.aiHubHasUnsavedChanges(), true);
});

test('settings detection retains newer directory input and navigation restoration keeps marked editor values', async () => {
  const h = harness(); await h.settings({values:{'s-root':'D:/Restored', 's-roots':'[]', 's-outputs':'[]'}});
  assert.equal(h.nodes.get('s-root').value, 'D:/Restored');
  assert.equal(h.nodes.get('s-root').dataset.appDraftKey, 'settings:D:/AI:s-root');
  const pending = deferred(); h.respond(() => pending.promise);
  const detection = h.nodes.get('s-detect').onclick(); h.input('s-root', 'D:/New');
  pending.resolve({ai_root:'D:/Obsolete', scan_roots:[], output_roots:[]}); await detection;
  assert.equal(h.nodes.get('s-root').value, 'D:/New');
  assert.equal(h.nodes.get('s-detect').disabled, false);
});

test('closing and reopening the same model restores its draft and saving unblocks installation', async () => {
  const h = harness(); await h.open(); h.input('notes', 'closed draft');
  h.nodes.get('stars').onclick({target:h.spans[4]});
  h.context.close(); await h.open();
  assert.equal(h.nodes.get('notes').value, 'closed draft');
  assert.equal(h.spans.filter(span => span.classList.contains('on')).length, 5);
  h.respond(async () => ({ok:true})); await h.nodes.get('btn-save-note').onclick();
  assert.equal(h.win.aiHubHasUnsavedChanges(), false);
  h.context.close(); h.respond(async () => ({...model(), notes:'fresh server value'})); await h.open();
  assert(h.html().includes('fresh server value'));
  assert(!h.html().includes('closed draft'));
});

test('a reused model ID under another path never restores a different workspace model draft', async () => {
  const h = harness(); await h.open(); h.input('notes', 'original owner'); h.context.close();
  h.respond(async () => ({...model(), path:'E:/Other/sample.safetensors'})); await h.open();
  assert.equal(h.nodes.get('notes').value, '');
  assert.equal(h.nodes.get('notes').dataset.appDraftKey, 'model:42:E:/Other/sample.safetensors:notes');
});

test('settings drafts restore on a fresh navigation, clear after save, and remain scoped to their workspace', async () => {
  const h = harness(); await h.settings(); h.input('s-root', 'D:/Draft'); h.input('s-token', '');
  h.context.captureSettings(h.host);
  await h.settings(); assert.equal(h.nodes.get('s-root').value, 'D:/Draft');
  // The miniature DOM does not parse textarea content; these assignments model
  // the JSON values returned by the server without treating them as user edits.
  h.nodes.get('s-roots').value = '[]'; h.nodes.get('s-outputs').value = '[]';
  h.respond(async () => ({ok:true})); await h.nodes.get('s-save').onclick();
  assert.equal(h.win.aiHubHasUnsavedChanges(), false);
  h.respond(async () => ({...settings(), ai_root:'D:/Draft'})); await h.settings();
  assert.equal(h.nodes.get('s-root').value, 'D:/Draft');
  h.input('s-proxy', 'http://127.0.0.1:1000'); h.context.captureSettings(h.host);
  h.respond(async () => ({...settings(), ai_root:'E:/Other'})); await h.settings();
  assert.equal(h.nodes.get('s-proxy').value, '');
  assert.equal(h.nodes.get('s-root').dataset.appDraftKey, 'settings:E:/Other:s-root');
});
