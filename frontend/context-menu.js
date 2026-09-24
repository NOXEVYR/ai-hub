/* Shared model-row context menu. No filesystem actions outside existing APIs. */
(function (root, factory) {
  const value = factory();
  if (typeof module === 'object' && module.exports) module.exports = value;
  else root.AIHubContextMenu = value;
})(typeof window !== 'undefined' ? window : globalThis, function () {
  'use strict';

  function targetModel(target) {
    if (!target?.closest || target.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"])')) return null;
    const link = target.closest('.model-link[data-id]') || target.closest('tr, .flat-row')?.querySelector('.model-link[data-id]');
    const id = Number(link?.dataset.id);
    return Number.isSafeInteger(id) && id > 0 ? {id, element: link} : null;
  }

  function position(x, y, width, height, viewportWidth, viewportHeight) {
    return {left: Math.max(8, Math.min(x, viewportWidth - width - 8)),
      top: Math.max(8, Math.min(y, viewportHeight - height - 8))};
  }

  function actions(id, {api, openDetails, clipboard, toast}) {
    const copy = async field => {
      const model = await api(`/api/model/${id}`);
      if (typeof model[field] !== 'string' || !model[field]) throw new Error('该模型尚未记录此信息');
      if (!clipboard?.writeText) throw new Error('当前窗口未允许复制，请从详情中选中后复制');
      await clipboard.writeText(model[field]);
      toast(field === 'path' ? '完整路径已复制' : '文件名已复制', 'ok');
    };
    return [
      {label: '打开所在文件夹', run: async () => {await api(`/api/model/${id}/reveal`, {body: {}}); toast('已在资源管理器中定位', 'ok');}},
      {label: '复制完整路径', run: () => copy('path')},
      {label: '复制文件名', run: () => copy('filename')},
      {label: '查看详情', run: () => openDetails(id)},
    ];
  }

  function install({document: doc, window: win, api, openDetails, clipboard, toast}) {
    let menu = null, origin = null;
    const listeners = [];
    function listen(node, name, callback, options) {
      node.addEventListener(name, callback, options);
      listeners.push(() => node.removeEventListener(name, callback, options));
    }
    function close(restore = false) {
      if (!menu) return;
      const returnFocus = restore && menu.contains(doc.activeElement);
      menu.remove(); menu = null;
      if (returnFocus && origin?.isConnected) origin.focus();
      origin = null;
    }
    function show(event, model, keyboard = false) {
      event.preventDefault();
      close(); origin = model.element;
      menu = doc.createElement('div');
      menu.className = 'model-context-menu';
      menu.setAttribute('role', 'menu');
      menu.setAttribute('aria-label', '模型文件操作');
      const heading = doc.createElement('div');
      heading.className = 'model-context-heading';
      heading.textContent = '模型文件'; menu.appendChild(heading);
      for (const action of actions(model.id, {api, openDetails, clipboard, toast})) {
        const button = doc.createElement('button');
        button.type = 'button'; button.setAttribute('role', 'menuitem');
        button.textContent = action.label;
        button.onclick = e => {
          e.preventDefault(); e.stopPropagation(); close(true);
          Promise.resolve().then(action.run).catch(error => toast(error.message || '操作未完成', 'err'));
        };
        menu.appendChild(button);
      }
      doc.body.appendChild(menu);
      const bounds = model.element.getBoundingClientRect();
      const box = menu.getBoundingClientRect();
      const point = position(keyboard ? bounds.left : event.clientX, keyboard ? bounds.bottom : event.clientY,
        box.width, box.height, win.innerWidth, win.innerHeight);
      menu.style.left = point.left + 'px'; menu.style.top = point.top + 'px';
      menu.querySelector('button').focus();
    }
    listen(doc, 'contextmenu', event => {
      if (menu?.contains(event.target)) {event.preventDefault(); return;}
      const model = targetModel(event.target);
      if (model) show(event, model, event.clientX === 0 && event.clientY === 0);
      else close();
    });
    listen(doc, 'pointerdown', event => {if (menu && !menu.contains(event.target)) close();}, true);
    listen(doc, 'keydown', event => {
      if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
        const model = targetModel(event.target);
        if (model) show(event, model, true);
        return;
      }
      if (!menu) return;
      if (event.key === 'Escape') {event.preventDefault(); event.stopPropagation(); close(true); return;}
      if (event.key === 'Tab') {close(true); return;}
      if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const buttons = [...menu.querySelectorAll('button')];
      const current = buttons.indexOf(doc.activeElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
        : (current + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next].focus();
    }, true);
    listen(doc, 'scroll', event => {if (!menu?.contains(event.target)) close();}, true);
    for (const name of ['resize', 'blur', 'hashchange', 'popstate']) listen(win, name, () => close());
    const page = doc.querySelector('#page');
    const observer = page && win.MutationObserver ? new win.MutationObserver(() => close()) : null;
    observer?.observe(page, {childList: true, subtree: true});
    return {close, destroy() {close(); listeners.forEach(remove => remove()); observer?.disconnect();}};
  }
  return {targetModel, position, actions, install};
});
