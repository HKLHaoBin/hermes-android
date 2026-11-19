/* Android immediate-import -> native chat attachment metadata. Load after
 * workspace.js (api declaration); no bytes, uploads, DOM or persistent storage.
 * Call androidRememberImportedAttachment(sid, originatingProfile, receipt) only
 * after the import hook has verified the receipt against the selected file.
 * The backend still validates image bytes before constructing image content.
 */
'use strict';
(function () {
  const receipts = new Map();
  const originalApi = api;
  const keyFor = (sid, profile) => JSON.stringify([sid, profile]);

  window.androidRememberImportedAttachment = function (sid, profile, receipt) {
    // Fail closed on absent ownership: a current profile at completion time
    // cannot substitute for the originating profile captured before upload.
    if (typeof sid !== 'string' || !sid || typeof profile !== 'string' || !profile ||
        !receipt || receipt.verified !== true || typeof receipt.path !== 'string' || !receipt.path ||
        !Number.isSafeInteger(receipt.size) || receipt.size <= 0 ||
        !/^[0-9a-f]{64}$/.test(receipt.sha256 || '') ||
        receipt.is_image !== true || typeof receipt.mime !== 'string' ||
        !receipt.mime.startsWith('image/')) return false;
    const key = keyFor(sid, profile);
    let owned = receipts.get(key);
    if (!owned) receipts.set(key, owned = new Map());
    // Copy metadata so caller mutation cannot change a remembered receipt.
    // Replacement has a new identity even when the same path is re-imported.
    owned.set(receipt.path, {path: receipt.path,
      name: typeof receipt.filename === 'string' ? receipt.filename : receipt.path.split(/[\\/]/).pop(),
      mime: receipt.mime, size: receipt.size, is_image: true});
    return true;
  };

  api = async function (path, opts) {
    if (path !== '/api/chat/start' || !opts || opts.method !== 'POST' || typeof opts.body !== 'string') {
      return originalApi.apply(this, arguments);
    }
    let payload;
    try { payload = JSON.parse(opts.body); } catch (_) { return originalApi.apply(this, arguments); }
    if (!payload || typeof payload.message !== 'string' || typeof payload.session_id !== 'string' ||
        typeof payload.profile !== 'string' ||
        (payload.attachments !== undefined && !Array.isArray(payload.attachments))) {
      return originalApi.apply(this, arguments);
    }
    const key = keyFor(payload.session_id, payload.profile);
    const owned = receipts.get(key);
    const captured = owned ? [...owned.values()].filter(att => payload.message.includes(JSON.stringify(att.path))) : [];
    if (!captured.length) return originalApi.apply(this, arguments);
    const attachments = [...(payload.attachments || [])];
    for (const att of captured) {
      const index = attachments.findIndex(existing => existing && existing.path === att.path);
      if (index < 0) attachments.push({...att});
      else attachments[index] = {...att, ...attachments[index]};
    }
    const result = await originalApi.call(this, path, {...opts,
      body: JSON.stringify({...payload, attachments})});
    // A 401 resolves undefined in the pinned helper. HTTP success alone also
    // does not prove turn admission; only stream_id acknowledges this turn.
    if (result && typeof result.stream_id === 'string' && result.stream_id) {
      for (const att of captured) {
        if (owned.get(att.path) === att) owned.delete(att.path);
      }
      if (!owned.size && receipts.get(key) === owned) receipts.delete(key);
    }
    return result;
  };
})();
