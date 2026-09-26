/* In-app context menus for navigation and existing, explicitly identified assets. */
(function (root, factory) {
  const value = factory();
  if (typeof module === 'object' && module.exports) module.exports = value;
  else root.AIHubContextMenu = value;
})(typeof window !== 'undefined' ? window : globalThis, function () {
  'use strict';

  const EDITABLE = 'input, textarea, select, option, [contenteditable]:not([contenteditable="false"])';
  const INTERACTIVE = 'a[href], button, summary, [role="button"], [role="link"]';

  function elementOf(target) {
    return target?.closest ? target : target?.parentElement || null;
  }

  function validId(value) {
    const id = Number(value);
    return Number.isSafeInteger(id) && id > 0 ? id : null;
  }

  function targetModel(target) {
    const element = elementOf(target);
    if (!element?.closest || element.closest(EDITABLE)) return null;
    const direct = element.closest('.model-link[data-id], [data-model-id]');
    const row = element.closest('tr, .flat-row, [data-model-card]');
    const link = direct || row?.querySelector('.model-link[data-id], [data-model-id]');
    const id = validId(link?.dataset?.id ?? link?.dataset?.modelId);
    return id ? { id, element: link } : null;
  }

  function targetWorkflow(target) {
    const element = elementOf(target);
    if (!element?.closest || element.closest(EDITABLE)) return null;
    const row = element.closest('tr, .flat-row, [data-workflow-card]');
    const marked = element.closest('[data-workflow]') || row?.querySelector('[data-workflow]');
    const path = marked?.dataset?.workflow;
    if (typeof path !== 'string' || !path.trim()) return null;
    const copyPath = marked.dataset.workflowCopy;
    return { path, copyPath: typeof copyPath === 'string' && copyPath.trim() ? copyPath : null,
      element: marked };
  }

  function targetPath(target) {
    const element = elementOf(target);
    if (!element?.closest || element.closest(EDITABLE)) return null;
    const marked = element.closest('[data-context-path], [data-path]');
    const path = marked?.dataset?.contextPath ?? marked?.dataset?.path;
    return typeof path === 'string' && path.trim() ? { path, element: marked } : null;
  }

  function targetWorkcenterDocument(target) {
    const element = elementOf(target);
    if (!element?.closest || element.closest(EDITABLE)) return null;
    const marked = element.closest('[data-wc-document]');
    const id = marked?.dataset?.wcDocument;
    if (typeof id !== 'string' || !id.trim()) return null;
    const owner = marked.closest('[data-wc-root]');
    const root = owner?.dataset?.wcRoot;
    const path = marked.dataset?.contextPath;
    return { id, root: typeof root === 'string' ? root : null,
      path: typeof path === 'string' && path.trim() ? path : null, element: marked };
  }

  function targetSkill(target) {
    const element = elementOf(target);
    if (!element?.closest || element.closest(EDITABLE)) return null;
    const marked = element.closest('[data-skill-path]');
    const path = marked?.dataset?.skillPath;
    return typeof path === 'string' && path.trim() ? { path, element: marked } : null;
  }

  function position(x, y, width, height, viewportWidth, viewportHeight) {
    return { left: Math.max(8, Math.min(x, viewportWidth - width - 8)),
      top: Math.max(8, Math.min(y, viewportHeight - height - 8)) };
  }

  function copyValue(value, { clipboard, toast }, label = '路径已复制') {
    return async () => {
      if (typeof value !== 'string' || !value.trim()) throw new Error('没有可复制的路径');
      if (!clipboard?.writeText) throw new Error('当前窗口未允许复制，请在详情中选中后复制');
      await clipboard.writeText(value);
      toast(label, 'ok');
    };
  }

  function actions(id, { api, openDetails, clipboard, toast }) {
    const copy = async field => {
      const model = await api(`/api/model/${id}`);
      if (typeof model[field] !== 'string' || !model[field]) throw new Error('该模型尚未记录此信息');
      if (!clipboard?.writeText) throw new Error('当前窗口未允许复制，请从详情中选中后复制');
      await clipboard.writeText(model[field]);
      toast(field === 'path' ? '完整路径已复制' : '文件名已复制', 'ok');
    };
    return [
      { label: '打开所在文件夹', run: async () => {
        await api(`/api/model/${id}/reveal`, { body: {} });
        toast('已在资源管理器中定位', 'ok');
      } },
      { label: '复制完整路径', run: () => copy('path') },
      { label: '复制文件名', run: () => copy('filename') },
      ...(typeof openDetails === 'function' ? [{ label: '查看详情', run: () => openDetails(id) }] : []),
    ];
  }

  function workflowActions(workflow, { clipboard, toast, openWorkflowFolder, openWorkflowDetails }) {
    return [
      ...(typeof openWorkflowFolder === 'function' ? [{ label: '打开所在文件夹', run: async () => {
        await openWorkflowFolder(workflow.path);
        toast('已在资源管理器中定位', 'ok');
      } }] : []),
      { label: '复制完整路径', run: copyValue(workflow.path, { clipboard, toast }, '工作流路径已复制') },
      ...(workflow.copyPath ? [{ label: '复制修正版路径', run: copyValue(workflow.copyPath, { clipboard, toast }, '修正版路径已复制') }] : []),
      ...(typeof openWorkflowDetails === 'function' ? [{ label: '查看依赖详情', run: () => openWorkflowDetails(workflow.path) }] : []),
    ];
  }

  function pathActions(path, { clipboard, toast, openPathFolder }) {
    return [
      ...(typeof openPathFolder === 'function' ? [{ label: '打开所在文件夹', run: async () => {
        await openPathFolder(path);
        toast('已在资源管理器中定位', 'ok');
      } }] : []),
      { label: '复制路径', run: copyValue(path, { clipboard, toast }) },
    ];
  }

  function workcenterDocumentActions(document, { clipboard, toast, openWorkcenterDocument }) {
    return [
      ...(typeof openWorkcenterDocument === 'function' && document.root ? [{ label: '打开所在文件夹', run: async () => {
        await openWorkcenterDocument(document.id, document.root);
        toast('已在资源管理器中定位', 'ok');
      } }] : []),
      ...(document.path ? [{ label: '复制路径', run: copyValue(document.path, { clipboard, toast }) }] : []),
    ];
  }

  function skillActions(path, { clipboard, toast, openSkillFolder }) {
    return [
      ...(typeof openSkillFolder === 'function' ? [{ label: '打开所在文件夹', run: async () => {
        await openSkillFolder(path);
        toast('已在资源管理器中定位', 'ok');
      } }] : []),
      { label: '复制路径', run: copyValue(path, { clipboard, toast }, 'Skill 路径已复制') },
    ];
  }

  function navigationActions(navigation) {
    if (!navigation) return [];
    const status = navigation.status?.() || {};
    return [
      ...(typeof navigation.back === 'function' ? [{ label: '返回上一页', disabled: status.canBack !== true, run: () => navigation.back() }] : []),
      ...(typeof navigation.forward === 'function' ? [{ label: '前进到下一页', disabled: status.canForward !== true, run: () => navigation.forward() }] : []),
      ...(typeof navigation.refresh === 'function' ? [{ label: '刷新当前页', run: () => navigation.refresh() }] : []),
    ];
  }

  function hasSelection(doc, win) {
    const selection = doc.getSelection?.() || win.getSelection?.();
    return !!selection && !selection.isCollapsed && String(selection).length > 0;
  }

  function shouldKeepNative(event, doc, win) {
    const element = elementOf(event.target);
    if (element?.closest?.(EDITABLE) || hasSelection(doc, win)) return true;
    return false;
  }

  function install({ document: doc, window: win, api, openDetails, clipboard, toast,
    navigation, openWorkflowFolder, openWorkflowDetails, openPathFolder, openSkillFolder,
    openWorkcenterDocument }) {
    let menu = null, origin = null, suppressContextTarget = null, suppressContextUntil = 0;
    const listeners = [];
    function listen(node, name, callback, options) {
      node.addEventListener(name, callback, options);
      listeners.push(() => node.removeEventListener(name, callback, options));
    }
    function close(restore = false) {
      if (!menu) return;
      const returnFocus = restore && menu.contains(doc.activeElement);
      menu.remove(); menu = null;
      if (returnFocus && origin?.isConnected && typeof origin.focus === 'function') origin.focus();
      origin = null;
    }
    function actionsFor(event) {
      const model = targetModel(event.target);
      if (model) return { title: '模型文件操作', anchor: model.element,
        actions: actions(model.id, { api, openDetails, clipboard, toast }) };
      const workcenterDocument = targetWorkcenterDocument(event.target);
      if (workcenterDocument) return { title: '工作中心文档操作', anchor: workcenterDocument.element,
        actions: workcenterDocumentActions(workcenterDocument, { clipboard, toast, openWorkcenterDocument }) };
      const workflow = targetWorkflow(event.target);
      if (workflow) return { title: '工作流操作', anchor: workflow.element,
        actions: workflowActions(workflow, { clipboard, toast, openWorkflowFolder, openWorkflowDetails }) };
      const skill = targetSkill(event.target);
      if (skill) return { title: 'Skill 文件操作', anchor: skill.element,
        actions: skillActions(skill.path, { clipboard, toast, openSkillFolder }) };
      const path = targetPath(event.target);
      if (path) return { title: '文件路径操作', anchor: path.element,
        actions: pathActions(path.path, { clipboard, toast, openPathFolder }) };
      const element = elementOf(event.target);
      if (element?.closest?.(INTERACTIVE)) return null;
      if (!element?.closest?.('#app')) return null;
      return { title: '页面操作', anchor: element, actions: navigationActions(navigation) };
    }
    function show(event, spec, keyboard = false) {
      if (!spec?.actions.length) return false;
      event.preventDefault();
      close();
      const active = doc.activeElement;
      origin = active && active !== doc.body && active.isConnected ? active : spec.anchor;
      menu = doc.createElement('div');
      menu.className = 'model-context-menu';
      menu.setAttribute('role', 'menu');
      menu.setAttribute('aria-label', spec.title);
      const heading = doc.createElement('div');
      heading.className = 'model-context-heading';
      heading.textContent = spec.title;
      menu.appendChild(heading);
      for (const action of spec.actions) {
        const button = doc.createElement('button');
        button.type = 'button';
        button.disabled = action.disabled === true;
        button.setAttribute('role', 'menuitem');
        button.textContent = action.label;
        button.onclick = e => {
          e.preventDefault(); e.stopPropagation();
          if (button.disabled) return;
          close(true);
          Promise.resolve().then(action.run).catch(error => toast(error?.message || '操作未完成', 'err'));
        };
        menu.appendChild(button);
      }
      doc.body.appendChild(menu);
      const bounds = spec.anchor?.getBoundingClientRect?.() || { left: 8, bottom: 8 };
      const box = menu.getBoundingClientRect();
      const point = position(keyboard ? (bounds.left || 8) : event.clientX,
        keyboard ? (bounds.bottom || bounds.top || 8) : event.clientY,
        box.width, box.height, win.innerWidth, win.innerHeight);
      menu.style.left = point.left + 'px'; menu.style.top = point.top + 'px';
      menu.querySelector('button:not([disabled])')?.focus();
      return true;
    }
    function handleContextMenu(event) {
      if (menu?.contains(event.target)) { event.preventDefault(); return; }
      if (suppressContextTarget === event.target && Date.now() < suppressContextUntil) {
        event.preventDefault(); suppressContextTarget = null; return;
      }
      if (shouldKeepNative(event, doc, win)) { close(); return; }
      const spec = actionsFor(event);
      if (spec) show(event, spec, event.clientX === 0 && event.clientY === 0);
      else close();
    }
    function handleKeydown(event) {
      if (event.key === 'ContextMenu' || event.key === 'Apps' || (event.shiftKey && event.key === 'F10')) {
        if (shouldKeepNative(event, doc, win)) return;
        const spec = actionsFor(event);
        if (spec && show(event, spec, true)) {
          event.stopPropagation();
          suppressContextTarget = event.target;
          suppressContextUntil = Date.now() + 500;
        }
        return;
      }
      if (!menu) return;
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(true); return; }
      if (event.key === 'Tab') { close(); return; }
      if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const buttons = [...menu.querySelectorAll('button:not([disabled])')];
      if (!buttons.length) return;
      const current = buttons.indexOf(doc.activeElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
        : (current + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next].focus();
    }
    listen(doc, 'contextmenu', handleContextMenu);
    listen(doc, 'pointerdown', event => { if (menu && !menu.contains(event.target)) close(); }, true);
    listen(doc, 'focusin', event => { if (menu && !menu.contains(event.target)) close(); }, true);
    listen(doc, 'keydown', handleKeydown, true);
    listen(doc, 'scroll', event => { if (!menu?.contains(event.target)) close(); }, true);
    for (const name of ['resize', 'blur', 'hashchange', 'popstate']) listen(win, name, () => close());
    const page = doc.querySelector('#page');
    const observer = page && win.MutationObserver ? new win.MutationObserver(() => close()) : null;
    observer?.observe(page, { childList: true, subtree: true });
    return { close, destroy() { close(); listeners.forEach(remove => remove()); observer?.disconnect(); } };
  }
  return { targetModel, targetWorkflow, targetPath, targetWorkcenterDocument, targetSkill, position, actions, workflowActions,
    pathActions, workcenterDocumentActions, skillActions, navigationActions, install };
});
