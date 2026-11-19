/* Android-only immediate import. User-selected bytes, not raw shared-storage paths. */
'use strict';
let _androidImportBusy = false;
const _androidImportReceipts = new Map();

function androidImportSession() {
  return (typeof S !== 'undefined' && S.session && S.session.session_id) || '';
}

function androidImportProfile() {
  return String((typeof S !== 'undefined' && (S.activeProfile || (S.session && S.session.profile))) || 'default');
}

function androidImportStatus(sid, message, profile = androidImportProfile()) {
  _androidImportReceipts.set(JSON.stringify([sid, profile]), message);
  if (androidImportSession() === sid && androidImportProfile() === profile) {
    const panel = document.getElementById('androidImportStatus');
    if (panel) panel.textContent = message;
  }
}

function androidChooseImport() {
  const sid = androidImportSession();
  const ownerProfile = androidImportProfile();
  if (!sid) {
    showToast('请先新建或打开一个会话，再导入文件。', 4000, 'error');
    return;
  }
  if (_androidImportBusy) return;
  const input = document.getElementById('androidImportInput');
  if (!input) return;
  input.value = '';
  androidImportStatus(sid, '请选择文件；取消选择不会导入。无需开启“所有文件访问权限”。', ownerProfile);
  input.oncancel = () => androidImportStatus(sid, '已取消选择，未导入文件。', ownerProfile);
  input.onchange = async () => {
    const files = Array.from(input.files || []);
    if (!files.length) {
      androidImportStatus(sid, '未选择文件，未导入。', ownerProfile);
      return;
    }
    if (androidImportProfile() !== ownerProfile || androidImportSession() !== sid) {
      showToast('会话或配置已切换，请在当前会话重新选择文件。', 4000, 'error');
      return;
    }
    await androidImportFiles(files, sid, ownerProfile);
    input.value = '';
  };
  input.click();
}

async function androidImportFiles(files, sid, ownerProfile = androidImportProfile()) {
  if (_androidImportBusy || !sid) return;
  _androidImportBusy = true;
  const button = document.getElementById('btnAttach');
  if (button) button.disabled = true;
  const lines = [];
  try {
    for (const file of files) {
      androidImportStatus(sid, [...lines, `正在导入：${file.name}（${file.size} 字节）…`].join('\n'), ownerProfile);
      try {
        if (androidImportProfile() !== ownerProfile) throw new Error('配置已切换，剩余文件未上传；请重新选择。');
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
        if (data.is_image) {
          if (typeof androidRememberImportedAttachment !== 'function') {
            throw new Error('文件已保存，但图片消息通道尚未就绪，请刷新后重试。');
          }
          if (androidRememberImportedAttachment(sid, ownerProfile, data) !== true) {
            throw new Error('文件已保存，但图片消息信息未通过校验，尚不能确认图片可随消息发送。');
          }
        }
        lines.push(`已导入并读回校验：${data.filename}（${data.size} 字节）\n位置：${data.path}\nSHA-256：${data.sha256}`);
        if (data.zip_valid === false) lines.push('注意：文件已保存，但 ZIP 结构无效，尚不能作为会话压缩包解析。');
        // Bind the receipt to the session that initiated the upload. Never
        // insert another session's private path into the currently open chat.
        if (androidImportSession() === sid && androidImportProfile() === ownerProfile) {
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
      androidImportStatus(sid, lines.join('\n'), ownerProfile);
    }
    if (androidImportSession() === sid && androidImportProfile() === ownerProfile && typeof loadDir === 'function') {
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
  let lastOwner = JSON.stringify([androidImportSession(), androidImportProfile()]);
  const observer = new MutationObserver(() => {
    const owner = JSON.stringify([androidImportSession(), androidImportProfile()]);
    if (owner !== lastOwner) {
      lastOwner = owner;
      _androidImportPanel.textContent = _androidImportReceipts.get(owner) || '';
    }
  });
  observer.observe(document.getElementById('composerBox') || document.body, {childList: true, subtree: true});
}
// Keep the original paperclip icon and position, but replace its old pending
// attachment behavior. This script runs after boot.js; no second toolbar button.
function _syncAttachChoiceSemantics() {
  const attach = document.getElementById('btnAttach');
  if (attach) ['aria-haspopup', 'aria-controls', 'aria-expanded'].forEach(name => attach.removeAttribute(name));
}

const _androidAttachButton = document.getElementById('btnAttach');
if (_androidAttachButton) {
  _syncAttachChoiceSemantics();
  _androidAttachButton.removeAttribute('data-i18n-title');
  _androidAttachButton.removeAttribute('data-i18n-aria-label');
  _androidAttachButton.setAttribute('aria-label', '导入文件');
  _androidAttachButton.setAttribute('data-tooltip', '导入文件（选完立即保存）');
  _androidAttachButton.setAttribute('title', '导入文件（选完立即保存）');
  _androidAttachButton.onclick = event => {
    event.preventDefault();
    event.stopPropagation();
    androidChooseImport();
  };
}

// Paste/drop/camera callbacks in the pinned WebUI resolve addFiles dynamically.
// Route new selections through the same immediate import instead of adding a
// second pending-send upload path. Existing drafts are not silently deleted.
function addFiles(files) {
  const sid = androidImportSession();
  if (!sid) {
    showToast('请先新建或打开一个会话，再导入文件。', 4000, 'error');
    return Promise.resolve();
  }
  if (_androidImportBusy) {
    showToast('正在导入文件，请完成后重新选择或粘贴。', 4000, 'error');
    return Promise.resolve();
  }
  return androidImportFiles(Array.from(files || []), sid);
}
