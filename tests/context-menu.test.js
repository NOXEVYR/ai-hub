const test = require('node:test');
const assert = require('node:assert/strict');
const {targetModel, position, actions, install} = require('../frontend/context-menu.js');

test('row whitespace resolves its own model; input keeps native editing menu', () => {
  const link = {dataset: {id: '42'}};
  const row = {querySelector: () => link};
  const target = {closest: selector => selector === 'tr, .flat-row' ? row : null};
  assert.equal(targetModel(target).id, 42);
  assert.equal(targetModel({closest: () => ({})}), null);
  link.dataset.id = 'NaN';
  assert.equal(targetModel(target), null);
  assert.equal(targetModel({closest: () => null}), null);
});

test('menu remains inside the viewport at each edge', () => {
  assert.deepEqual(position(990, 790, 210, 190, 1000, 800), {left: 782, top: 602});
  assert.deepEqual(position(-5, -5, 210, 190, 1000, 800), {left: 8, top: 8});
});

test('actions retain selected id, reuse reveal, and copy exact returned Unicode path', async () => {
  const calls = [], copied = [], details = [];
  const path = "F:\\模型\\O'Neil & 测试.safetensors";
  const menu = actions(42, {api: async (url, options) => {calls.push([url, options]); return {path, filename: '测试.safetensors'};},
    clipboard: {writeText: async text => copied.push(text)}, openDetails: id => details.push(id), toast() {}});
  for (const action of menu) await action.run();
  assert.deepEqual(calls[0], ['/api/model/42/reveal', {body: {}}]);
  assert.deepEqual(copied, [path, '测试.safetensors']);
  assert.deepEqual(details, [42]);
});

test('failed reveal and denied clipboard reject without success feedback', async () => {
  const notices = [];
  const menu = actions(7, {api: async url => {if (url.endsWith('/reveal')) throw Error('文件已移动'); return {path: 'test'};},
    clipboard: {writeText: async () => {throw Error('复制被拒绝');}}, toast: text => notices.push(text)});
  await assert.rejects(menu[0].run, /文件已移动/);
  await assert.rejects(menu[1].run, /复制被拒绝/);
  assert.deepEqual(notices, []);
});

function harness() {
  const handlers = new Map();
  let doc;
  class Element {
    constructor(tag) {this.tag = tag; this.children = []; this.style = {}; this.attributes = {}; this.isConnected = true;}
    setAttribute(key, value) {this.attributes[key] = value;}
    appendChild(child) {this.children.push(child); child.parent = this;}
    contains(child) {return this === child || this.children.some(item => item.contains(child));}
    querySelectorAll() {return this.children.filter(child => child.tag === 'button');}
    querySelector() {return this.querySelectorAll()[0];}
    getBoundingClientRect() {return {left: 20, bottom: 40, width: 210, height: 190};}
    focus() {doc.activeElement = this;}
    remove() {this.isConnected = false; this.parent.children = this.parent.children.filter(item => item !== this);}
  }
  const events = {addEventListener(name, fn) {handlers.set(name, fn);}, removeEventListener(name) {handlers.delete(name);}};
  doc = {...events, body: new Element('body'), createElement: tag => new Element(tag), querySelector: () => null};
  const win = {...events, innerWidth: 1000, innerHeight: 800};
  const origin = new Element('button'); origin.dataset = {id: '42'};
  origin.closest = selector => selector === '.model-link[data-id]' ? origin : null;
  const notices = [], calls = [];
  const instance = install({document: doc, window: win, api: async url => {calls.push(url); throw Error('模拟失败');},
    openDetails() {}, clipboard: {}, toast: text => notices.push(text)});
  function event(key, target = origin) {return {key, target, clientX: 990, clientY: 790, preventDefault() {this.prevented = true;}, stopPropagation() {}};}
  return {handlers, doc, origin, instance, notices, calls, event};
}

test('right click opens; keyboard cycles; Escape returns focus; outside click dismisses', () => {
  const h = harness();
  const click = h.event(); h.handlers.get('contextmenu')(click);
  assert.equal(click.prevented, true);
  const menu = h.doc.body.children[0];
  assert.equal(menu.attributes.role, 'menu');
  const buttons = menu.querySelectorAll();
  assert.equal(h.doc.activeElement, buttons[0]);
  h.handlers.get('keydown')(h.event('ArrowUp')); assert.equal(h.doc.activeElement, buttons[3]);
  h.handlers.get('keydown')(h.event('Home')); assert.equal(h.doc.activeElement, buttons[0]);
  h.handlers.get('keydown')(h.event('Escape')); assert.equal(h.doc.body.children.length, 0);
  assert.equal(h.doc.activeElement, h.origin);
  h.handlers.get('keydown')(h.event('ContextMenu')); assert.equal(h.doc.body.children.length, 1);
  h.handlers.get('pointerdown')(h.event(undefined, {})); assert.equal(h.doc.body.children.length, 0);
  h.instance.destroy(); assert.equal(h.handlers.size, 0);
});

test('action closes menu once, reports failure, and navigation dismisses stale menu', async () => {
  const h = harness(); h.handlers.get('contextmenu')(h.event());
  h.doc.body.children[0].querySelector().onclick(h.event());
  assert.equal(h.doc.body.children.length, 0);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(h.calls, ['/api/model/42/reveal']);
  assert.deepEqual(h.notices, ['模拟失败']);
  h.handlers.get('contextmenu')(h.event()); h.handlers.get('hashchange')();
  assert.equal(h.doc.body.children.length, 0);
});
