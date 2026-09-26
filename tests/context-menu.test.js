const test = require('node:test');
const assert = require('node:assert/strict');
const {targetModel, targetWorkflow, targetPath, targetWorkcenterDocument, targetSkill, position, actions, workflowActions, navigationActions, install} = require('../frontend/context-menu.js');

class Element {
  constructor(tag, {classes = [], dataset = {}, attrs = {}} = {}) {
    this.tag = tag; this.classes = new Set(classes); this.dataset = dataset; this.attrs = attrs; this.attributes = attrs;
    this.children = []; this.style = {}; this.isConnected = true; this.parent = null; this.disabled = false;
  }
  appendChild(child) { this.children.push(child); child.parent = this; return child; }
  remove() { this.isConnected = false; this.parent.children = this.parent.children.filter(item => item !== this); }
  contains(child) {
    return this === child || this.children.some(item => item.contains(child));
  }
  matchesOne(selector) {
    selector = selector.trim();
    if (!selector) return false;
    if (selector === 'input, textarea, select, option, [contenteditable]:not([contenteditable="false"])')
      return ['input', 'textarea', 'select', 'option'].includes(this.tag) || this.attrs.contenteditable === 'true';
    if (selector === 'a[href], button, summary, [role="button"], [role="link"]')
      return this.tag === 'button' || this.tag === 'summary' || (this.tag === 'a' && this.attrs.href !== undefined) || ['button', 'link'].includes(this.attrs.role);
    if (selector === 'button:not([disabled])') return this.tag === 'button' && !this.disabled;
    if (selector === 'button') return this.tag === 'button';
    if (selector.includes('contenteditable')) return this.attrs.contenteditable === 'true';
    if (selector.startsWith('#')) return this.attrs.id === selector.slice(1);
    if (selector.startsWith('.')) {
      const [klass, attribute] = selector.slice(1).split('[');
      return this.classes.has(klass) && (!attribute || this.dataset[attribute.replace(/\]$/, '').replace(/^data-/, '').replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] !== undefined);
    }
    if (selector.startsWith('[')) {
      const attr = selector.slice(1, -1).split('=')[0].replace(/^data-/, '').replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
      return this.dataset[attr] !== undefined || this.attrs[attr] !== undefined;
    }
    return this.tag === selector;
  }
  closest(selector) {
    const selectors = selector.split(',');
    for (let node = this; node; node = node.parent) if (selectors.some(value => node.matchesOne(value))) return node;
    return null;
  }
  querySelectorAll(selector) {
    const result = [];
    const visit = node => { for (const child of node.children) { if (selector.split(',').some(part => child.matchesOne(part))) result.push(child); visit(child); } };
    visit(this); return result;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  setAttribute(key, value) { this.attributes[key] = value; }
  getBoundingClientRect() { return {left: 990, top: 780, bottom: 790, width: 210, height: 190}; }
  focus() { if (!this.disabled) doc.activeElement = this; }
}

let doc;
function node(tag, options, parent) { const result = new Element(tag, options); if (parent) parent.appendChild(result); return result; }

test('model rows and cards resolve their own id; invalid ids and editable targets are ignored', () => {
  doc = { activeElement: null };
  const row = node('tr'); const link = node('button', {classes: ['model-link'], dataset: {id: '42'}}, row);
  const text = node('span', {}, row);
  assert.equal(targetModel(text).id, 42);
  assert.equal(targetModel(link).id, 42);
  assert.equal(targetModel(node('input', {}, row)), null);
  link.dataset.id = 'NaN';
  assert.equal(targetModel(text), null);
  const card = node('article', {dataset: {modelCard: ''}}); node('span', {classes: ['model-link'], dataset: {id: '9'}}, card);
  assert.equal(targetModel(card).id, 9);
});

test('workflow and generic path targets return exact supplied path metadata', () => {
  const row = node('tr');
  const path = "F:\\模型\\O'Neil & 测试.json";
  const button = node('button', {dataset: {workflow: path, workflowCopy: 'F:\\review\\修正版.json'}}, row);
  const cell = node('td', {}, row);
  assert.deepEqual(targetWorkflow(cell), {path, copyPath: 'F:\\review\\修正版.json', element: button});
  const file = node('div', {dataset: {path}});
  assert.equal(targetPath(file).path, path);
  assert.equal(targetWorkflow(node('div')), null);
  assert.equal(targetPath(node('div')), null);
});

test('workcenter document target binds id and root from its rendered page', () => {
  const page = node('div', {dataset: {wcRoot: 'F:\\Studio'}});
  const card = node('button', {dataset: {wcDocument: 'doc_0123456789abcdef0123456789abcdef', contextPath: 'G:\\Reports\\weekly.md'}}, page);
  assert.deepEqual(targetWorkcenterDocument(node('span', {}, card)), {
    id: 'doc_0123456789abcdef0123456789abcdef', root: 'F:\\Studio', path: 'G:\\Reports\\weekly.md', element: card,
  });
  assert.equal(targetWorkcenterDocument(node('button', {dataset: {wcDocument: ''}}, page)), null);
});

test('skill cards expose only their declared path', () => {
  const path = 'C:\\Users\\me\\.codex\\skills\\local-skill';
  const card = node('article', {dataset: {skillPath: path}});
  assert.deepEqual(targetSkill(card), {path, element: card});
  assert.equal(targetSkill(node('article')), null);
});

test('menu placement stays inside the viewport at each edge', () => {
  assert.deepEqual(position(990, 790, 210, 190, 1000, 800), {left: 782, top: 602});
  assert.deepEqual(position(-5, -5, 210, 190, 1000, 800), {left: 8, top: 8});
});

test('model actions retain selected id, use the existing reveal API, and copy exact returned path', async () => {
  const calls = [], copied = [], details = [];
  const path = "F:\\模型\\O'Neil & 测试.safetensors";
  const menu = actions(42, {api: async (url, options) => { calls.push([url, options]); return {path, filename: '测试.safetensors'}; },
    clipboard: {writeText: async text => copied.push(text)}, openDetails: id => details.push(id), toast() {}});
  for (const action of menu) await action.run();
  assert.deepEqual(calls[0], ['/api/model/42/reveal', {body: {}}]);
  assert.deepEqual(copied, [path, '测试.safetensors']);
  assert.deepEqual(details, [42]);
});

test('workflow folder action is shown only with host support and paths are copied verbatim', async () => {
  const copied = [], opened = [], notices = [];
  const original = "F:\\AI\\Flow & O'Neil.json";
  const workflow = {path: original, copyPath: 'F:\\AI\\fixed.json'};
  const deps = {clipboard: {writeText: async value => copied.push(value)}, toast: text => notices.push(text)};
  assert.deepEqual(workflowActions(workflow, deps).map(item => item.label), ['复制完整路径', '复制修正版路径']);
  const menu = workflowActions(workflow, {...deps, openWorkflowFolder: async value => opened.push(value)});
  await menu[0].run(); await menu[1].run(); await menu[2].run();
  assert.deepEqual(opened, [original]);
  assert.deepEqual(copied, [original, 'F:\\AI\\fixed.json']);
  assert.deepEqual(notices, ['已在资源管理器中定位', '工作流路径已复制', '修正版路径已复制']);
});

test('failed reveal and denied clipboard reject without success feedback', async () => {
  const notices = [];
  const menu = actions(7, {api: async url => { if (url.endsWith('/reveal')) throw Error('文件已移动'); return {path: 'test'}; },
    clipboard: {writeText: async () => { throw Error('复制被拒绝'); } }, toast: text => notices.push(text)});
  await assert.rejects(menu[0].run, /文件已移动/);
  await assert.rejects(menu[1].run, /复制被拒绝/);
  assert.deepEqual(notices, []);
});

function harness({navigation, openWorkflowFolder, openWorkflowDetails, openPathFolder, openSkillFolder, openWorkcenterDocument, clipboard = {}, api} = {}) {
  const handlers = new Map();
  const add = (map, name, fn) => { const values = map.get(name) || []; values.push(fn); map.set(name, values); };
  const events = {addEventListener(name, fn) { add(handlers, name, fn); }, removeEventListener(name, fn) { handlers.set(name, (handlers.get(name) || []).filter(value => value !== fn)); }};
  doc = {...events, activeElement: null, body: new Element('body'), selection: {isCollapsed: true, toString: () => ''},
    createElement: tag => new Element(tag), querySelector: selector => selector === '#page' ? page : null,
    getSelection() { return this.selection; }};
  const app = node('div', {attrs: {id: 'app'}}, doc.body);
  const page = node('div', {attrs: {id: 'page'}}, app);
  const winHandlers = new Map();
  const win = {innerWidth: 1000, innerHeight: 800, addEventListener(name, fn) { add(winHandlers, name, fn); },
    removeEventListener(name, fn) { winHandlers.set(name, (winHandlers.get(name) || []).filter(value => value !== fn)); },
    getSelection: () => doc.selection};
  const origin = node('button', {classes: ['model-link'], dataset: {id: '42'}}, page);
  const notices = [], calls = [];
  const instance = install({document: doc, window: win, api: api || (async url => { calls.push(url); throw Error('模拟失败'); }),
    openDetails() {}, clipboard, toast: text => notices.push(text), navigation, openWorkflowFolder, openWorkflowDetails, openPathFolder, openSkillFolder, openWorkcenterDocument});
  function fire(map, name, event) { for (const listener of [...(map.get(name) || [])]) listener(event); }
  function event(key, target = origin, coords = {clientX: 990, clientY: 790}) {
    return {key, target, ...coords, preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; }};
  }
  return {handlers, winHandlers, doc, app, page, origin, instance, notices, calls, event,
    context(target = origin, coords) { const e = event(undefined, target, coords); fire(handlers, 'contextmenu', e); return e; },
    key(key, target = origin, modifiers = {}) { const e = {...event(key, target), ...modifiers}; fire(handlers, 'keydown', e); return e; },
    pointer(target) { fire(handlers, 'pointerdown', event(undefined, target)); },
    focus(target) { fire(handlers, 'focusin', event(undefined, target)); },
    blur() { fire(winHandlers, 'blur', {}); }};
}

test('model menu supports arrows, Escape restores focus, outside click and blur dismiss', () => {
  const h = harness();
  const click = h.context();
  assert.equal(click.prevented, true);
  const menu = h.doc.body.children.find(item => item.attributes.role === 'menu');
  assert.equal(menu.attributes.role, 'menu');
  const buttons = menu.querySelectorAll('button:not([disabled])');
  assert.equal(h.doc.activeElement, buttons[0]);
  h.key('ArrowUp'); assert.equal(h.doc.activeElement, buttons[buttons.length - 1]);
  h.key('Home'); assert.equal(h.doc.activeElement, buttons[0]);
  h.key('Escape'); assert.equal(h.doc.body.children.length, 1); // app root remains
  assert.equal(h.doc.body.children.filter(item => item.attributes.role === 'menu').length, 0);
  assert.equal(h.doc.activeElement, h.origin);
  h.context(); h.pointer({});
  assert.equal(h.doc.body.children.filter(item => item.attributes.role === 'menu').length, 0);
  h.context(); h.blur();
  assert.equal(h.doc.body.children.filter(item => item.attributes.role === 'menu').length, 0);
});

test('menu item reports action errors and navigation changes dismiss stale menus', async () => {
  const h = harness(); h.context();
  const menu = h.doc.body.children.find(item => item.attributes.role === 'menu');
  menu.querySelector('button:not([disabled])').onclick(h.event());
  assert.equal(h.doc.body.children.some(item => item.attributes.role === 'menu'), false);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(h.calls, ['/api/model/42/reveal']);
  assert.deepEqual(h.notices, ['模拟失败']);
  h.context(); h.winHandlers.get('hashchange')[0]();
  assert.equal(h.doc.body.children.some(item => item.attributes.role === 'menu'), false);
});

test('workflow context menu uses its declared path and exposes directory opening only via host callback', async () => {
  const opened = [], copied = [];
  const path = "F:\\workflow\\O'Neil.json";
  const h = harness({openWorkflowFolder: async value => opened.push(value),
    clipboard: {writeText: async value => copied.push(value)}});
  const row = node('tr', {}, h.page);
  const button = node('button', {dataset: {workflow: path}}, row);
  const e = h.context(row);
  assert.equal(e.prevented, true);
  const menu = h.doc.body.children.find(item => item.attributes.role === 'menu');
  assert.deepEqual(menu.querySelectorAll('button').map(item => item.textContent), ['打开所在文件夹', '复制完整路径']);
  menu.querySelectorAll('button')[0].onclick(h.event());
  await new Promise(resolve => setImmediate(resolve));
  h.context(row);
  const currentMenu = h.doc.body.children.find(item => item.attributes.role === 'menu');
  currentMenu.querySelectorAll('button')[1].onclick(h.event());
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(opened, [path]);
  assert.deepEqual(copied, [path]);
  assert.equal(button.dataset.workflow, path);
});

test('skill card opens only through the host callback and copies its exact declared path', async () => {
  const opened = [], copied = [];
  const path = 'C:\\Users\\me\\.codex\\skills\\local-skill';
  const h = harness({openSkillFolder: async value => opened.push(value), clipboard: {writeText: async value => copied.push(value)}});
  const card = node('article', {dataset: {skillPath: path}}, h.page);
  h.context(card);
  const menu = h.doc.body.children.find(item => item.attributes.role === 'menu');
  assert.deepEqual(menu.querySelectorAll('button').map(button => button.textContent), ['打开所在文件夹', '复制路径']);
  menu.querySelectorAll('button')[0].onclick(h.event());
  await new Promise(resolve => setImmediate(resolve));
  h.context(card);
  const copyMenu = h.doc.body.children.find(item => item.attributes.role === 'menu');
  copyMenu.querySelectorAll('button')[1].onclick(h.event());
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(opened, [path]);
  assert.deepEqual(copied, [path]);
});

test('workcenter document menu reveals by document id with the rendered workspace root', async () => {
  const opened = [], copied = [], genericOpened = [];
  const path = 'G:\\Reports\\weekly.md';
  const h = harness({openWorkcenterDocument: async (id, root) => opened.push([id, root]),
    openPathFolder: async value => genericOpened.push(value),
    clipboard: {writeText: async value => copied.push(value)}});
  h.page.dataset.wcRoot = 'F:\\Studio';
  const card = node('button', {dataset: {wcDocument: 'doc_0123456789abcdef0123456789abcdef', contextPath: path}}, h.page);
  const child = node('span', {}, card);
  h.context(child);
  const menu = h.doc.body.children.find(item => item.attributes.role === 'menu');
  assert.deepEqual(menu.querySelectorAll('button').map(button => button.textContent), ['打开所在文件夹', '复制路径']);
  menu.querySelectorAll('button')[0].onclick(h.event());
  await new Promise(resolve => setImmediate(resolve));
  h.context(child);
  const copyMenu = h.doc.body.children.find(item => item.attributes.role === 'menu');
  copyMenu.querySelectorAll('button')[1].onclick(h.event());
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(opened, [['doc_0123456789abcdef0123456789abcdef', 'F:\\Studio']]);
  assert.deepEqual(copied, [path]);
  assert.deepEqual(genericOpened, []);
  assert.deepEqual(h.notices, ['已在资源管理器中定位', '路径已复制']);
  h.instance.destroy();
});

test('workcenter document without host reveal callback offers only its exact copyable path', () => {
  const h = harness({clipboard: {writeText: async () => {}}});
  h.page.dataset.wcRoot = 'F:\\Studio';
  const card = node('button', {dataset: {wcDocument: 'doc_0123456789abcdef0123456789abcdef', contextPath: 'G:\\Reports\\weekly.md'}}, h.page);
  h.context(card);
  const menu = h.doc.body.children.find(item => item.attributes.role === 'menu');
  assert.deepEqual(menu.querySelectorAll('button').map(button => button.textContent), ['复制路径']);
  h.instance.destroy();
});

test('blank page menu delegates back, forward, and refresh to navigation with real availability', async () => {
  const calls = [];
  const navigation = {status: () => ({canBack: true, canForward: false}), back: () => calls.push('back'),
    forward: () => calls.push('forward'), refresh: () => calls.push('refresh')};
  const h = harness({navigation});
  const event = h.context(h.page, {clientX: 400, clientY: 300});
  assert.equal(event.prevented, true);
  const menu = h.doc.body.children.find(item => item.attributes.role === 'menu');
  const buttons = menu.querySelectorAll('button');
  assert.deepEqual(buttons.map(button => [button.textContent, button.disabled]), [
    ['返回上一页', false], ['前进到下一页', true], ['刷新当前页', false],
  ]);
  buttons[0].onclick(h.event()); buttons[2].onclick(h.event());
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(calls, ['back', 'refresh']);
  assert.deepEqual(navigationActions(null), []);
});

test('Shift+F10 and Menu key open keyboard menu; Escape closes and focus leaves cleanly', () => {
  const h = harness({navigation: {status: () => ({canBack: false, canForward: false}), refresh() {}}});
  const shift = h.key('F10', h.page, {shiftKey: true});
  assert.equal(shift.prevented, true);
  assert.equal(h.doc.body.children.some(item => item.attributes.role === 'menu'), true);
  h.key('Escape');
  assert.equal(h.doc.body.children.some(item => item.attributes.role === 'menu'), false);
  h.key('ContextMenu', h.page);
  assert.equal(h.doc.body.children.some(item => item.attributes.role === 'menu'), true);
});

test('editable controls, selected text, and unregistered interactive controls keep native context menus', () => {
  const h = harness({navigation: {status: () => ({canBack: true, canForward: true}), back() {}, forward() {}, refresh() {}}});
  const input = node('input', {}, h.page);
  assert.equal(h.context(input).prevented, undefined);
  assert.equal(h.doc.body.children.some(item => item.attributes.role === 'menu'), false);
  h.doc.selection = {isCollapsed: false, toString: () => 'selected text'};
  assert.equal(h.context(h.page).prevented, undefined);
  assert.equal(h.doc.body.children.some(item => item.attributes.role === 'menu'), false);
  h.doc.selection = {isCollapsed: true, toString: () => ''};
  const button = node('button', {}, h.page);
  assert.equal(h.context(button).prevented, undefined);
  assert.equal(h.doc.body.children.some(item => item.attributes.role === 'menu'), false);
});

test('generic path context action copies only the exact path supplied by the element', async () => {
  const copied = [];
  const h = harness({clipboard: {writeText: async value => copied.push(value)}});
  const path = "F:\\AI\\模型 & O'Neil.safetensors";
  const item = node('div', {dataset: {contextPath: path}}, h.page);
  h.context(item);
  const menu = h.doc.body.children.find(value => value.attributes.role === 'menu');
  assert.deepEqual(menu.querySelectorAll('button').map(button => button.textContent), ['复制路径']);
  menu.querySelector('button').onclick(h.event());
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(copied, [path]);
  h.instance.destroy();
});
