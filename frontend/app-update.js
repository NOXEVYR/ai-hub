/* Software update entry. The web page never receives desktop control credentials. */
((root, factory) => {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else factory().mount(root);
})(globalThis, () => {
  'use strict';
  function createDraftGuard() {
    const edited = new Set();
    return {
      edit: control => edited.add(control),
      saved: controls => { for (const control of controls) edited.delete(control); },
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
    win.AIHubAppUpdate = {saved: container => guard.saved(container.querySelectorAll('input,textarea,select,[contenteditable]'))};
    // Keep edits from detached pages: navigation can retain an in-memory draft.
    // This deliberately errs on the side of a save confirmation in the native dialog.
    const record = event => {
      const el = event.target;
      if (!el?.matches?.('input,textarea,select,[contenteditable="true"]')) return;
      if (el.id === 'global-search' || el.type === 'search' || el.closest('[data-app-update]')) return;
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
