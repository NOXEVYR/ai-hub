/* Software update entry. The web page never receives desktop control credentials. */
((root, factory) => {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else factory().mount(root);
})(globalThis, () => {
  'use strict';
  function createDraftGuard() {
    const edited = new Map();
    let revision = 0;
    const key = control => control?.dataset?.appDraftKey || control?.closest?.('[data-app-draft-scope]')?.dataset?.appDraftScope || control;
    return {
      edit: control => edited.set(key(control), ++revision),
      snapshot: controls => new Map(Array.from(controls, control => [key(control), edited.get(key(control))])),
      saved: (controls, snapshot) => {
        for (const control of controls) {
          const id = key(control);
          if (!snapshot || (snapshot.has(id) && snapshot.get(id) === edited.get(id))) edited.delete(id);
        }
      },
      savedSnapshot: snapshot => {
        for (const [id, version] of snapshot) {
          if (edited.get(id) === version) edited.delete(id);
        }
      },
      changed: snapshot => [...snapshot].some(([id, version]) => edited.get(id) !== version),
      rekey: (before, after) => {
        if (before && before !== after && edited.has(before)) {
          edited.set(after, Math.max(edited.get(before), edited.get(after) || 0));
          edited.delete(before);
        }
      },
      dirty: () => edited.size > 0,
    };
  }
  function describe(status) {
    const names = {idle:'尚无待安装版本', checking:'正在检查软件版本', available:'有新版本',
      downloading:'正在下载更新', ready:'更新已准备好', prepared:'准备安装', applying:'正在安装',
      succeeded:'更新已完成', failed:'更新未完成'};
    return `${names[status.state] || '暂时无法读取状态'} · 当前 ${String(status.current_version || '未知')}` +
      (status.latest_version ? ` · 最新 ${String(status.latest_version)}` : '');
  }
  function mount(win) {
    const doc = win.document, guard = createDraftGuard();
    win.aiHubHasUnsavedChanges = () => guard.dirty();
    const selector = 'input,textarea,select,[contenteditable],[data-app-draft-key]';
    const controls = container => [
      ...(container?.matches?.(selector) ? [container] : []),
      ...(container?.querySelectorAll?.(selector) || []),
    ];
    win.AIHubAppUpdate = {
      // A scope stores only its key and edit revision, never editor values or secrets.
      bind: (container, id, {transfer=false}={}) => {
        const control = {dataset:{appDraftKey:id}};
        if (transfer) guard.rekey(container?.dataset?.appDraftScope, id);
        if (container?.dataset) container.dataset.appDraftScope = id;
        return {
          snapshot: () => guard.snapshot([control]),
          saved: snapshot => guard.saved([control], snapshot),
          changed: snapshot => guard.changed(snapshot),
          edit: () => guard.edit(control),
          discard: () => guard.saved([control]),
        };
      },
      edit: control => guard.edit(control),
      edited: control => guard.edit(control),
      snapshotControls: list => guard.snapshot(list),
      savedControls: snapshot => guard.savedSnapshot(snapshot),
      snapshot: container => guard.snapshot(controls(container)),
      saved: (container, snapshot) => guard.saved(controls(container), snapshot),
    };
    // Keep edits from detached pages: navigation can retain an in-memory draft.
    // Installation stays blocked until the corresponding draft is saved.
    const record = event => {
      const el = event.target;
      if (!el?.matches?.('input,textarea,select,[contenteditable="true"]')) return;
      if (el.id === 'global-search' || el.type === 'search' || el.closest('[data-app-update]')) return;
      // Filters and navigation controls have no save action. Only explicitly
      // marked editors participate in the install guard.
      if (!el.dataset?.appDraftKey && !el.closest('[data-app-draft-scope]')) return;
      guard.edit(el);
    };
    doc.addEventListener('input', record, true);
    doc.addEventListener('change', record, true);
    const button = doc.getElementById('btn-app-update');
    if (!button) return;
    let dialog;
    button.addEventListener('click', async () => {
      if (win.chrome?.webview?.postMessage) {
        win.chrome.webview.postMessage('open-app-update');
        return;
      }
      if (dialog?.open) return;
      dialog = doc.createElement('dialog');
      dialog.className = 'app-update-dialog';
      dialog.setAttribute('data-app-update', '');
      dialog.innerHTML = '<h2>曜核软件更新</h2><p role="status">正在读取版本…</p>' +
        '<p>请在桌面版的托盘菜单中打开“软件更新”，可手动更新，或开启自动下载并在退出时安装。</p>' +
        '<p class="muted">软件更新与模型版本检查分别管理。当前更新通道：候选版。</p>' +
        '<form method="dialog"><button class="btn primary">关闭</button></form>';
      doc.body.appendChild(dialog);
      dialog.addEventListener('close', () => dialog.remove(), {once:true});
      dialog.showModal();
      try {
        const response = await win.fetch('/api/app-update/status', {cache:'no-store'});
        if (!response.ok) throw new Error('unavailable');
        const status = await response.json();
        if (dialog.isConnected) dialog.querySelector('[role="status"]').textContent = describe(status);
      } catch {
        if (dialog.isConnected) dialog.querySelector('[role="status"]').textContent = '暂时无法读取软件版本，请在桌面版重试。';
      }
    });
  }
  return {createDraftGuard, describe, mount};
});
