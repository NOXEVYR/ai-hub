const test = require('node:test');
const assert = require('node:assert/strict');
const library = require('../frontend/capability-library.js');

function toDatasetName(attribute) {
  return attribute.replace(/^data-/, '').replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
}

function decodeHtml(value) {
  return value.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>').replace(/&amp;/g, '&');
}

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(value) { this.values.add(value); }
  toggle(value, force) {
    const next = force === undefined ? !this.values.has(value) : !!force;
    if (next) this.values.add(value); else this.values.delete(value);
    return next;
  }
  contains(value) { return this.values.has(value); }
}

class FakeElement {
  constructor(tag = 'div', {id = '', dataset = {}, attrs = {}} = {}) {
    this.tagName = tag.toUpperCase(); this.id = id; this.dataset = {...dataset}; this.attributes = {...attrs};
    this.children = []; this.parentElement = null; this.classList = new FakeClassList();
    this.className = ''; this.innerHTML = ''; this.textContent = ''; this.value = '';
    this.hidden = false; this.disabled = false; this.isConnected = true; this.handlers = {};
  }
  appendChild(child) { child.parentElement = this; this.children.push(child); return child; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    const selectors = selector.split(',').map(value => value.trim());
    const result = [];
    const visit = parent => {
      for (const child of parent.children) {
        if (selectors.some(part => child.matches(part))) result.push(child);
        visit(child);
      }
    };
    visit(this);
    return result;
  }
  matches(selector) {
    if (selector.startsWith('#')) return this.id === selector.slice(1);
    if (selector.startsWith('[') && selector.endsWith(']')) {
      const attribute = selector.slice(1, -1).split('=')[0];
      const key = toDatasetName(attribute);
      return this.dataset[key] !== undefined || this.attributes[attribute] !== undefined;
    }
    if (selector === 'input') return this.tagName === 'INPUT';
    if (selector === 'select') return this.tagName === 'SELECT';
    if (selector === 'button') return this.tagName === 'BUTTON';
    return false;
  }
  set innerHTML(value) {
    this._innerHTML = String(value);
    if (this.id === 'library-source-editor') this.#readSourceRows(this._innerHTML);
    else if (this.id === 'library-tool') this.value = '';
  }
  get innerHTML() { return this._innerHTML || ''; }
  #readSourceRows(markup) {
    this.children = [];
    const blocks = markup.matchAll(/<div class="library-source-row" data-source-row>([\s\S]*?)<\/div>/g);
    for (const [, block] of blocks) {
      const row = this.appendChild(new FakeElement('div', {dataset: {sourceRow: ''}}));
      for (const [tag, key] of [['input', 'sourceTool'], ['input', 'sourcePath']]) {
        const match = block.match(new RegExp(`<input\\b[^>]*data-${key === 'sourceTool' ? 'source-tool' : 'source-path'}[^>]*>`));
        if (!match) continue;
        const value = decodeHtml(match[0].match(/\bvalue="([^"]*)"/)?.[1] || '');
        row.appendChild(new FakeElement(tag, {dataset: {[key]: ''}})).value = value;
      }
      const selectMarkup = block.match(/<select\b[^>]*data-source-kind[^>]*>([\s\S]*?)<\/select>/)?.[0] || '';
      const selectedKind = selectMarkup.match(/<option value="([^"]+)" selected>/)?.[1]
        || selectMarkup.match(/<option value="([^"]+)"/)?.[1] || '';
      row.appendChild(new FakeElement('select', {dataset: {sourceKind: ''}})).value = selectedKind;
      const remove = block.match(/<button\b[^>]*data-source-remove="([^"]+)"[^>]*>/);
      if (remove) row.appendChild(new FakeElement('button', {dataset: {sourceRemove: remove[1]}}));
    }
  }
  set oninput(callback) { this.handlers.input = callback; }
  get oninput() { return this.handlers.input; }
  set onchange(callback) { this.handlers.change = callback; }
  get onchange() { return this.handlers.change; }
  set onclick(callback) { this.handlers.click = callback; }
  get onclick() { return this.handlers.click; }
  async click() { return this.handlers.click?.(); }
  input() { return this.handlers.input?.(); }
  change() { return this.handlers.change?.({target: this}); }
}

const staticIds = [
  'refresh', 'tab-skills', 'tab-interfaces', 'tab-credentials', 'tab-tasks', 'error', 'inventory',
  'query', 'domain', 'tool', 'count', 'description', 'list', 'sources', 'source-list', 'source-editor',
  'source-add', 'source-save', 'source-result', 'tasks', 'more',
];

function makePage() {
  const root = new FakeElement('section');
  root.dataset = {};
  const originalSetter = Object.getOwnPropertyDescriptor(FakeElement.prototype, 'innerHTML').set;
  Object.defineProperty(root, 'innerHTML', {
    configurable: true,
    get() { return this._innerHTML || ''; },
    set(value) {
      originalSetter.call(this, value);
      this.children = [];
      for (const id of staticIds) this.appendChild(new FakeElement(id.startsWith('tab-') ? 'button' : 'div', {id: `library-${id}`}));
      const get = id => this.querySelector(`#library-${id}`);
      get('domain').appendChild(new FakeElement('option', {attrs: {value: ''}}));
      for (const value of ['image', 'video', 'audio', 'code', 'document', 'research', 'automation'])
        get('domain').appendChild(new FakeElement('option', {attrs: {value}}));
      get('tasks').hidden = true;
    },
  });
  return root;
}

function taskHarness() {
  const state = {loads: 0, factoryCalls: 0, captures: 0};
  const taskUI = {
    createPage() {
      state.factoryCalls++;
      return async (el, _params, restored) => {
        state.loads++;
        el.dataset.taskDraft = restored?.draft || 'unsaved task draft';
      };
    },
    capture(el) { state.captures++; return {draft: el.dataset.taskDraft}; },
  };
  return {taskUI, state};
}

function inventory() {
  return {
    root: 'F:\\AI\\Workspace', workspace_root: 'F:\\AI\\Workspace', sources_revision: 'revision-1',
    configured_sources: [], sources: [{tool: 'codex', kind: 'skills_root', path: 'C:\\Users\\me\\.codex\\skills'}],
    suggestions: [
      {name: 'video-codex-skill', path: 'C:\\codex\\SKILL.md', tool: 'codex', domains: ['video', 'code'], description: 'Video coding support'},
      {name: 'image-codex-skill', path: 'C:\\codex\\image.md', tools: ['codex'], domains: ['image']},
      {name: 'video-dsh-skill', path: 'C:\\dsh\\video.md', tools: ['dsh'], domains: ['video']},
    ],
    interfaces: [
      {name: 'video-codex-api', provider: 'video-codex-service', tools: ['codex'], domains: ['video']},
      {name: 'image-codex-api', provider: 'image-codex-service', tool: 'codex', domains: ['image']},
      {name: 'video-dsh-api', provider: 'video-dsh-service', tool: 'dsh', domains: ['video']},
    ],
    credentials: [
      {name: 'video-codex-token', provider: 'video-codex-service', variable: 'VIDEO_CODEX_TOKEN', tools: ['codex'], domains: ['video'], present: true},
      {name: 'image-codex-token', provider: 'image-codex-service', variable: 'IMAGE_CODEX_TOKEN', tools: ['codex'], domains: ['image'], present: false},
      {name: 'video-dsh-token', provider: 'video-dsh-service', variable: 'VIDEO_DSH_TOKEN', tools: ['dsh'], domains: ['video'], present: false},
    ],
  };
}

function inventoryAt(root, revision, configured_sources = []) {
  return {...inventory(), root, workspace_root: root, sources_revision: revision, configured_sources};
}

function editSourceRow(row, {tool, kind = 'skills_root', path}) {
  const toolInput = row.querySelector('[data-source-tool]');
  const kindSelect = row.querySelector('[data-source-kind]');
  const pathInput = row.querySelector('[data-source-path]');
  toolInput.value = tool; kindSelect.value = kind; pathInput.value = path;
  pathInput.input();
  return {toolInput, kindSelect, pathInput};
}

function makeLibrary({api, taskUI = taskHarness().taskUI, heading = () => '<header>能力中心</header>'}) {
  return library.createPage({api, heading, taskUI});
}

test('initial view auto-discovers read-only and never dispatches a task', async () => {
  const calls = [], tasks = taskHarness();
  const page = makeLibrary({taskUI: tasks.taskUI, api: async (url, options) => { calls.push([url, options]); return inventory(); }});
  const el = makePage();
  await page(el);
  assert.deepEqual(calls.map(([url]) => url), ['/api/capabilities/discover']);
  assert.equal(calls.some(([url]) => /dispatch/.test(url)), false);
  assert.equal(tasks.state.loads, 0);
  assert.equal(el.querySelector('#library-tab-skills').classList.contains('active'), true);
});

test('domain and work-end filters cross-apply to skills, interfaces, and credentials', async () => {
  const page = makeLibrary({api: async () => inventory()});
  const el = makePage(); await page(el);
  const domain = el.querySelector('#library-domain'), tool = el.querySelector('#library-tool');
  domain.value = 'video'; domain.change();
  tool.value = 'codex'; tool.change();
  assert.match(el.querySelector('#library-list').innerHTML, /video-codex-skill/);
  assert.doesNotMatch(el.querySelector('#library-list').innerHTML, /image-codex-skill|video-dsh-skill/);
  await el.querySelector('#library-tab-interfaces').click();
  assert.match(el.querySelector('#library-list').innerHTML, /video-codex-api/);
  assert.doesNotMatch(el.querySelector('#library-list').innerHTML, /image-codex-api|video-dsh-api/);
  await el.querySelector('#library-tab-credentials').click();
  assert.match(el.querySelector('#library-list').innerHTML, /VIDEO_CODEX_TOKEN/);
  assert.doesNotMatch(el.querySelector('#library-list').innerHTML, /IMAGE_CODEX_TOKEN|VIDEO_DSH_TOKEN/);
});

test('tasks load lazily once, remain hidden with their unsaved draft, and capture it', async () => {
  const tasks = taskHarness(), page = makeLibrary({api: async () => inventory(), taskUI: tasks.taskUI}), el = makePage();
  await page(el);
  assert.equal(tasks.state.loads, 0);
  await el.querySelector('#library-tab-tasks').click();
  const taskElement = el.querySelector('#library-tasks');
  assert.equal(tasks.state.loads, 1);
  assert.equal(taskElement.hidden, false);
  assert.equal(taskElement.dataset.taskDraft, 'unsaved task draft');
  await el.querySelector('#library-tab-skills').click();
  assert.equal(taskElement.hidden, true);
  assert.equal(taskElement.dataset.taskDraft, 'unsaved task draft');
  await el.querySelector('#library-tab-tasks').click();
  assert.equal(tasks.state.loads, 1);
  assert.equal(taskElement.hidden, false);
  assert.deepEqual(library.capture(el, tasks.taskUI).task, {draft: 'unsaved task draft'});
});

test('a discover response that resolves after leaving the page cannot write stale results', async () => {
  let resolveDiscover;
  const page = makeLibrary({api: () => new Promise(resolve => { resolveDiscover = resolve; })});
  const el = makePage();
  const loading = page(el);
  const countBeforeLeaving = el.querySelector('#library-count').textContent;
  el.isConnected = false;
  resolveDiscover({...inventory(), suggestions: [{name: 'late stale skill', domains: ['image']}]});
  await loading;
  assert.equal(el.querySelector('#library-list').innerHTML, '');
  assert.equal(el.querySelector('#library-count').textContent, countBeforeLeaving);
  assert.doesNotMatch(el.querySelector('#library-count').textContent, /late stale/);
});

test('failed rediscovery keeps the previously rendered data', async () => {
  let count = 0;
  const page = makeLibrary({api: async url => {
    if (url === '/api/capabilities/discover' && ++count === 1) return inventory();
    throw new Error('network unavailable');
  }});
  const el = makePage(); await page(el);
  const list = el.querySelector('#library-list'), before = list.innerHTML;
  await el.querySelector('#library-refresh').click();
  assert.match(before, /video-codex-skill/);
  assert.equal(list.innerHTML, before);
  assert.match(el.querySelector('#library-error').textContent, /已保留上次结果/);
  assert.equal(el.querySelector('#library-refresh').disabled, false);
});

test('source save sends workspace and revision, preserves a failed draft, and confirms a successful save', async () => {
  const calls = [], savedSources = [{tool: 'my-worker', kind: 'skills_root', path: 'D:\\agents\\skills'}];
  let discoveryCount = 0, sourceAttempt = 0;
  const page = makeLibrary({api: async (url, options) => {
    calls.push([url, options]);
    if (url === '/api/capabilities/discover') {
      discoveryCount++;
      return {...inventory(), sources_revision: sourceAttempt > 1 ? 'revision-2' : 'revision-1', configured_sources: sourceAttempt > 1 ? savedSources : []};
    }
    if (url === '/api/capabilities/sources') {
      sourceAttempt++;
      if (sourceAttempt === 1) throw new Error('revision conflict');
      return {saved: true, sources_revision: 'revision-2', configured_sources: savedSources, workspace_root: 'F:\\AI\\Workspace'};
    }
    throw new Error(`unexpected API call: ${url}`);
  }});
  const el = makePage(); await page(el);
  await el.querySelector('#library-source-add').click();
  const row = el.querySelector('#library-source-editor').querySelector('[data-source-row]');
  const tool = row.querySelector('[data-source-tool]'), kind = row.querySelector('[data-source-kind]'), path = row.querySelector('[data-source-path]');
  tool.value = ' my-worker '; kind.value = 'skills_root'; path.value = ' D:\\agents\\skills ';
  tool.input();
  await el.querySelector('#library-source-save').click();
  const firstSave = calls.filter(([url]) => url === '/api/capabilities/sources')[0][1].body;
  assert.deepEqual(firstSave, {sources: [{tool: 'my-worker', kind: 'skills_root', path: 'D:\\agents\\skills'}],
    revision: 'revision-1', _workspace_root: 'F:\\AI\\Workspace'});
  assert.equal(tool.value, ' my-worker ');
  assert.equal(path.value, ' D:\\agents\\skills ');
  assert.equal(kind.value, 'skills_root');
  assert.match(el.querySelector('#library-source-result').textContent, /revision conflict/);
  assert.equal(discoveryCount, 1);

  await el.querySelector('#library-source-save').click();
  const secondSave = calls.filter(([url]) => url === '/api/capabilities/sources')[1][1].body;
  assert.equal(secondSave.revision, 'revision-1');
  assert.equal(secondSave._workspace_root, 'F:\\AI\\Workspace');
  assert.deepEqual(secondSave.sources, savedSources);
  assert.match(el.querySelector('#library-source-result').textContent, /来源已保存/);
  assert.equal(discoveryCount, 2);
  assert.equal(el.querySelector('#library-source-editor').querySelector('[data-source-path]').value, savedSources[0].path);
});

test('captured source draft survives navigation and restores only into the same workspace', async () => {
  const tasks = taskHarness(), page = makeLibrary({taskUI: tasks.taskUI,
    api: async () => ({...inventory(), configured_sources: [{tool: 'codex', kind: 'skills_root', path: 'C:\\Users\\me\\.codex\\skills'}]})});
  const first = makePage(); await page(first);
  await first.querySelector('#library-source-add').click();
  const sourceRows = first.querySelector('#library-source-editor').querySelectorAll('[data-source-row]');
  const row = sourceRows[sourceRows.length - 1];
  const tool = row.querySelector('[data-source-tool]'), kind = row.querySelector('[data-source-kind]'), path = row.querySelector('[data-source-path]');
  tool.value = 'local-agent'; kind.value = 'capability_manifest'; path.value = 'D:\\agent\\.capabilities.json';
  path.input();
  const snapshot = library.capture(first, tasks.taskUI);
  assert.equal(snapshot.root, 'F:\\AI\\Workspace');
  assert.deepEqual(snapshot.sourceDraft, [{tool: 'codex', kind: 'skills_root', path: 'C:\\Users\\me\\.codex\\skills'},
    {tool: 'local-agent', kind: 'capability_manifest', path: 'D:\\agent\\.capabilities.json'}]);

  first.isConnected = false;
  const second = makePage(); await page(second, new URLSearchParams(), snapshot);
  assert.equal(second.dataset.librarySourceDirty, 'true');
  const restoredRows = second.querySelector('#library-source-editor').querySelectorAll('[data-source-row]');
  assert.equal(restoredRows.length, 2);
  assert.equal(restoredRows[1].querySelector('[data-source-tool]').value, 'local-agent');
  assert.equal(restoredRows[1].querySelector('[data-source-kind]').value, 'capability_manifest');
  assert.equal(restoredRows[1].querySelector('[data-source-path]').value, 'D:\\agent\\.capabilities.json');
});

test('a dirty source draft remains bound to its original workspace after rediscovery changes roots', async () => {
  const oldRoot = 'F:\\AI\\Workspace-A', newRoot = 'G:\\AI\\Workspace-B';
  let discoveryCount = 0, posted;
  const page = makeLibrary({api: async (url, options) => {
    if (url === '/api/capabilities/discover') return ++discoveryCount === 1
      ? inventoryAt(oldRoot, 'revision-A') : inventoryAt(newRoot, 'revision-B', [{tool: 'new-worker', kind: 'skills_root', path: 'G:\\AI\\skills'}]);
    if (url === '/api/capabilities/sources') { posted = options.body; throw new Error('工作环境已切换'); }
    throw new Error(`unexpected API call: ${url}`);
  }});
  const el = makePage(); await page(el);
  await el.querySelector('#library-source-add').click();
  const row = el.querySelector('#library-source-editor').querySelector('[data-source-row]');
  const draft = editSourceRow(row, {tool: 'old-worker', path: 'F:\\AI\\old-skills'});
  await el.querySelector('#library-refresh').click();
  assert.equal(el.dataset.libraryRoot, newRoot);
  assert.equal(el.dataset.librarySourceRoot, oldRoot);
  assert.equal(library.capture(el, taskHarness().taskUI).root, oldRoot);
  await el.querySelector('#library-source-save').click();
  assert.equal(posted._workspace_root, oldRoot);
  assert.equal(posted.revision, 'revision-B');
  assert.deepEqual(posted.sources, [{tool: 'old-worker', kind: 'skills_root', path: 'F:\\AI\\old-skills'}]);
  assert.equal(draft.toolInput.value, 'old-worker');
  assert.equal(draft.pathInput.value, 'F:\\AI\\old-skills');
  assert.match(el.querySelector('#library-source-result').textContent, /工作环境已切换/);
});

test('a source draft captured under a different root is not restored into the new workspace', async () => {
  const oldRoot = 'F:\\AI\\Workspace-A', newRoot = 'G:\\AI\\Workspace-B';
  const tasks = taskHarness();
  const page = makeLibrary({taskUI: tasks.taskUI, api: async () => inventoryAt(oldRoot, 'revision-A')});
  const first = makePage(); await page(first);
  await first.querySelector('#library-source-add').click();
  const rows = first.querySelector('#library-source-editor').querySelectorAll('[data-source-row]');
  editSourceRow(rows.at(-1), {tool: 'old-worker', path: 'F:\\AI\\old-skills'});
  const snapshot = library.capture(first, tasks.taskUI);
  assert.equal(snapshot.root, oldRoot);
  first.isConnected = false;

  const second = makePage();
  const newPage = makeLibrary({taskUI: tasks.taskUI, api: async () => inventoryAt(newRoot, 'revision-B', [
    {tool: 'new-worker', kind: 'skills_root', path: 'G:\\AI\\skills'},
  ])});
  await newPage(second, new URLSearchParams(), snapshot);
  assert.equal(second.dataset.libraryRoot, newRoot);
  assert.notEqual(second.dataset.librarySourceDirty, 'true');
  const rowsAfterRestore = second.querySelector('#library-source-editor').querySelectorAll('[data-source-row]');
  assert.equal(rowsAfterRestore.length, 1);
  assert.equal(rowsAfterRestore[0].querySelector('[data-source-tool]').value, 'new-worker');
  assert.equal(rowsAfterRestore[0].querySelector('[data-source-path]').value, 'G:\\AI\\skills');
});

test('an initially empty workspace root remains an explicit source owner after a nonempty rediscovery', async () => {
  const newRoot = 'G:\\AI\\Workspace-B';
  let discoveryCount = 0, posted;
  const page = makeLibrary({api: async (url, options) => {
    if (url === '/api/capabilities/discover') return ++discoveryCount === 1
      ? inventoryAt('', 'revision-empty') : inventoryAt(newRoot, 'revision-B');
    if (url === '/api/capabilities/sources') { posted = options.body; throw new Error('workspace is no longer empty'); }
    throw new Error(`unexpected API call: ${url}`);
  }});
  const el = makePage(); await page(el);
  assert.equal(el.dataset.librarySourceRoot, '');
  await el.querySelector('#library-source-add').click();
  const row = el.querySelector('#library-source-editor').querySelector('[data-source-row]');
  const draft = editSourceRow(row, {tool: 'local-agent', path: 'C:\\agents\\skills'});
  await el.querySelector('#library-refresh').click();
  assert.equal(el.dataset.libraryRoot, newRoot);
  assert.equal(el.dataset.librarySourceRoot, '');
  assert.equal(library.capture(el, taskHarness().taskUI).root, '');
  await el.querySelector('#library-source-save').click();
  assert.equal(posted._workspace_root, '');
  assert.equal(posted.revision, 'revision-B');
  assert.equal(draft.pathInput.value, 'C:\\agents\\skills');
});
