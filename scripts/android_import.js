/* Android-only immediate import. User-selected bytes, not raw shared-storage paths. */
'use strict';
let _androidImportBusy = false;
const _androidImportReceipts = new Map();

function androidImportSession() {
  return (typeof S !== 'undefined' && S.session && S.session.session_id) || '';
}

function androidImportStatus(sid, message) {
  _androidImportReceipts.set(sid, message);
  if (androidImportSession() === sid) {
    const panel = document.getElementById('androidImportStatus');
    if (panel) panel.textContent = message;
  }
}

function androidChooseImport() {
  const sid = androidImportSession();
  if (!sid) {
    showToast('请先新建或打开一个会话，再导入文件。', 4000, 'error');
    return;
  }
  if (_androidImportBusy) return;
  const input = document.getElementById('androidImportInput');
  if (!input) return;
  input.value = '';
  androidImportStatus(sid, '请选择文件；取消选择不会导入。无需开启“所有文件访问权限”。');
  input.oncancel = () => androidImportStatus(sid, '已取消选择，未导入文件。');
  input.onchange = async () => {
    const files = Array.from(input.files || []);
    if (!files.length) {
      androidImportStatus(sid, '未选择文件，未导入。');
      return;
    }
    await androidImportFiles(files, sid);
    input.value = '';
  };
  input.click();
}

async function androidImportFiles(files, sid) {
  if (_androidImportBusy || !sid) return;
  _androidImportBusy = true;
  const button = document.getElementById('androidImportButton');
  if (button) button.disabled = true;
  const lines = [];
  try {
    for (const file of files) {
      androidImportStatus(sid, [...lines, `正在导入：${file.name}（${file.size} 字节）…`].join('\n'));
      try {
        if (!file.size) throw new Error('文件为 0 字节或无法读取，请重新选择。');
        const form = new FormData();
        form.append('session_id', sid);
        form.append('path', '.');
        form.append('android_import', '1');
        form.append('file', file, file.name);
        const data = await api('/api/workspace/upload', {
          method: 'POST', body: form, headers: {}, timeoutMs: 120000, retries: 0,
        });
        if (!data || data.error) throw new Error((data && data.error) || '服务端没有返回导入回执。');
        if (!data.verified || data.size !== file.size || typeof data.path !== 'string' ||
            !data.path || !/^[0-9a-f]{64}$/.test(data.sha256 || '')) {
          throw new Error('服务端尚未确认文件读取及大小校验；请勿当作导入成功。');
        }
        lines.push(`已导入并读回校验：${data.filename}（${data.size} 字节）\n位置：${data.path}\nSHA-256：${data.sha256}`);
        if (data.zip_valid === false) lines.push('注意：文件已保存，但 ZIP 结构无效，尚不能作为会话压缩包解析。');
        // Bind the receipt to the session that initiated the upload. Never
        // insert another session's private path into the currently open chat.
        if (androidImportSession() === sid) {
          const composer = document.getElementById('msg');
          if (composer) {
            const note = `已导入本地文件：${JSON.stringify(data.path)}（${data.size} 字节，SHA-256：${data.sha256}）。请先读取文件，核对格式后再处理；目前尚未导入其中的会话。`;
            composer.value += (composer.value ? '\n' : '') + note;
            composer.dispatchEvent(new Event('input', {bubbles: true}));
          }
        }
      } catch (error) {
        const uncertain = error instanceof TypeError || error.timeout || error.name === 'TimeoutError';
        lines.push(`导入失败：${file.name}。${error.message || '文件读取或上传失败，请重新选择。'}${uncertain ? ' 未收到确认，文件可能已保存；请先检查工作区。本次不会自动重传。' : ''}`);
      }
      androidImportStatus(sid, lines.join('\n'));
    }
    if (androidImportSession() === sid && typeof loadDir === 'function') {
      // Directory refresh is cosmetic; do not turn a verified import into a
      // false upload failure if a separate panel refresh is unavailable.
      try { await loadDir(S.currentDir || '.'); } catch (_) { /* keep receipt */ }
    }
  } finally {
    _androidImportBusy = false;
    if (button) button.disabled = false;
  }
}

// Session navigation mutates the composer. Restore only the current session's
// receipt and never persist imported paths into cross-profile browser storage.
const _androidImportPanel = document.getElementById('androidImportStatus');
if (_androidImportPanel) {
  let lastSid = androidImportSession();
  const observer = new MutationObserver(() => {
    const sid = androidImportSession();
    if (sid !== lastSid) {
      lastSid = sid;
      _androidImportPanel.textContent = _androidImportReceipts.get(sid) || '';
    }
  });
  observer.observe(document.getElementById('composerBox') || document.body, {childList: true, subtree: true});
}
const _androidTray = document.getElementById('attachTray');
const _androidAttachmentHint = document.getElementById('androidAttachmentHint');
if (_androidTray && _androidAttachmentHint) {
  const syncAttachmentHint = () => {
    const count = (typeof S !== 'undefined' && Array.isArray(S.pendingFiles)) ? S.pendingFiles.length : 0;
    _androidAttachmentHint.textContent = count ? `已选择 ${count} 个附件，尚未发送。点击发送后才会上传；要立即保存文件，请使用“导入文件”。` : '';
  };
  new MutationObserver(syncAttachmentHint).observe(_androidTray, {childList: true, subtree: true});
  syncAttachmentHint();
}
