'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function fixture() {
  const elements = Object.fromEntries(['androidImportStatus', 'androidImportInput', 'btnAttach', 'msg', 'composerBox', 'attachTray'].map(id => [id, {
    textContent: '', value: '', disabled: false, files: [], attributes: {},
    setAttribute(k, v) { this.attributes[k] = v; }, removeAttribute(k) { delete this.attributes[k]; },
    click() { this.clicked = true; }, dispatchEvent() { this.inputEvents = (this.inputEvents || 0) + 1; },
  }]));
  const observers = [];
  const calls = [];
  const ctx = {
    S: {session: {session_id: 'owner'}, currentDir: '.', pendingFiles: []},
    document: {getElementById: id => elements[id], body: {}},
    Event: class { constructor(type) { this.type = type; } },
    FormData: class { constructor() { this.fields = {}; } append(k, v) { this.fields[k] = v; } },
    MutationObserver: class { constructor(cb) { observers.push(cb); } observe() {} },
    showToast: (...args) => calls.push(['toast', ...args]),
    loadDir: async () => {},
    api: async (url, options) => {
      calls.push([url, options]);
      const file = options.body.fields.file;
      return {verified: true, path: '/private/workspace/' + file.name, filename: file.name, size: file.size, sha256: 'a'.repeat(64), zip_valid: true};
    },
  };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(__dirname, 'android_import.js'), 'utf8'), ctx);
  return {ctx, elements, observers, calls};
}

test('selecting a file uploads immediately, preserving ZIP and attaching verified path to composer without sending', async () => {
  const {ctx, elements, calls} = fixture();
  assert.equal(typeof elements.btnAttach.onclick, 'function');
  elements.btnAttach.onclick({preventDefault() {}, stopPropagation() {}});
  assert.equal(elements.btnAttach.attributes['aria-label'], '导入文件');
  assert.equal(elements.androidImportButton, undefined);
  for (const name of ['aria-haspopup', 'aria-controls', 'aria-expanded']) elements.btnAttach.setAttribute(name, 'stale-menu');
  ctx._syncAttachChoiceSemantics();
  assert.equal(elements.btnAttach.attributes['aria-haspopup'], undefined);
  assert.equal(elements.btnAttach.attributes['aria-controls'], undefined);
  assert.equal(elements.btnAttach.attributes['aria-expanded'], undefined);
  const input = elements.androidImportInput;
  assert.equal(input.clicked, true);
  input.files = [{name: '微信-会话.zip', size: 2312389}];
  await input.onchange();
  const upload = calls.find(c => c[0] === '/api/workspace/upload');
  assert.equal(upload[1].body.fields.android_import, '1');
  assert.equal(upload[1].body.fields.session_id, 'owner');
  assert.match(elements.androidImportStatus.textContent, /已导入并读回校验/);
  assert.match(elements.msg.value, /尚未导入其中的会话/);
  assert.match(elements.msg.value, /微信-会话.zip/);
  assert.equal(elements.msg.inputEvents, 1);
  assert.equal(elements.btnAttach.disabled, false);
});

test('no active session does not open the chooser', () => {
  const {ctx, elements, calls} = fixture();
  ctx.S.session = null;
  ctx.androidChooseImport();
  assert.equal(elements.androidImportInput.clicked, undefined);
  assert.equal(calls[0][0], 'toast');
});

test('cancel and zero-byte input do not report import success', async () => {
  const {ctx, elements, calls} = fixture();
  ctx.androidChooseImport();
  elements.androidImportInput.oncancel();
  assert.match(elements.androidImportStatus.textContent, /已取消/);
  await ctx.androidImportFiles([{name: 'bad.zip', size: 0}], 'owner');
  assert.match(elements.androidImportStatus.textContent, /导入失败/);
  assert.equal(calls.filter(c => c[0] === '/api/workspace/upload').length, 0);
  assert.equal(elements.msg.value, '');
});

test('missing verification receipt, size mismatch and network failure are visible failures', async () => {
  for (const response of [{path: '/tmp/a', size: 1}, {verified: true, size: 9, path: '/p', sha256: 'a'.repeat(64)}, null]) {
    const {ctx, elements} = fixture();
    ctx.api = async () => { if (!response) throw new Error('network offline'); return response; };
    await ctx.androidImportFiles([{name: 'a.zip', size: 1}], 'owner');
    assert.match(elements.androidImportStatus.textContent, /导入失败/);
    assert.equal(elements.msg.value, '');
    assert.equal(elements.btnAttach.disabled, false);
  }
});

test('session switched during upload never receives another session path', async () => {
  const {ctx, elements, observers} = fixture();
  let resolve;
  ctx.api = () => new Promise(r => { resolve = r; });
  const pending = ctx.androidImportFiles([{name: 'private.zip', size: 4}], 'owner');
  ctx.S.session = {session_id: 'other'};
  observers[0]();
  resolve({verified: true, size: 4, path: '/owner/private.zip', filename: 'private.zip', sha256: 'a'.repeat(64)});
  await pending;
  assert.equal(elements.msg.value, '');
  assert.equal(elements.androidImportStatus.textContent, '');
  ctx.S.session = {session_id: 'owner'};
  observers[0]();
  assert.match(elements.androidImportStatus.textContent, /owner\/private.zip/);
});

test('invalid ZIP is distinguished from successful file copy', async () => {
  const {ctx, elements} = fixture();
  ctx.api = async () => ({verified: true, size: 4, path: '/a.zip', filename: 'a.zip', sha256: 'b'.repeat(64), zip_valid: false});
  await ctx.androidImportFiles([{name: 'a.zip', size: 4}], 'owner');
  assert.match(elements.androidImportStatus.textContent, /ZIP 结构无效/);
});

test('non-idempotent import never retries after a saved request loses its response (real pinned API helper)', {skip: !process.env.HERMES_WEBUI_SOURCE}, async () => {
  const {ctx, elements} = fixture();
  const workspace = fs.readFileSync(path.join(process.env.HERMES_WEBUI_SOURCE, 'static', 'workspace.js'), 'utf8');
  const boundary = '\nfunction recordClientSSEError(';
  assert.equal(workspace.split(boundary).length, 2, 'Pinned API helper boundary changed');
  ctx.document.baseURI = 'http://127.0.0.1:8765/';
  Object.assign(ctx, {URL, setTimeout, clearTimeout, AbortController, TypeError});
  let savedRequests = 0;
  ctx.fetch = async () => {
    savedRequests += 1;
    // This synthetic transport models saving the request and losing the reply.
    // The real pinned helper owns retry classification; no fake api retry loop.
    throw new TypeError('response lost after server saved bytes');
  };
  vm.runInContext(workspace.split(boundary)[0], ctx);
  await ctx.androidImportFiles([{name: 'once.zip', size: 4}], 'owner');
  assert.equal(savedRequests, 1, 'One file selection must not silently create repeated copies');
  assert.match(elements.androidImportStatus.textContent, /导入失败/);
  assert.match(elements.androidImportStatus.textContent, /文件可能已保存/);
  assert.equal(elements.msg.value, '');
});

test('partial batch failure does not hide successful files', async () => {
  const {ctx, elements} = fixture();
  await ctx.androidImportFiles([{name: 'empty.zip', size: 0}, {name: 'ok.txt', size: 1}], 'owner');
  assert.match(elements.androidImportStatus.textContent, /导入失败：empty.zip/);
  assert.match(elements.androidImportStatus.textContent, /已导入并读回校验：ok.txt/);
});

test('paste and drop addFiles use the same immediate import, not pending-send attachments', async () => {
  const {ctx, elements, calls} = fixture();
  assert.equal(typeof ctx.addFiles, 'function');
  await ctx.addFiles([{name: 'pasted.png', size: 12}]);
  assert.equal(calls.filter(c => c[0] === '/api/workspace/upload').length, 1);
  assert.equal(ctx.S.pendingFiles.length, 0);
  assert.match(elements.msg.value, /pasted.png/);
});

test('new imports preserve existing pending drafts without appending more pending files', async () => {
  const {ctx} = fixture();
  const oldDraft = {name: 'legacy-draft.txt', size: 5};
  ctx.S.pendingFiles.push(oldDraft);
  await ctx.addFiles([{name: 'new.txt', size: 6}]);
  assert.equal(ctx.S.pendingFiles.length, 1);
  assert.equal(ctx.S.pendingFiles[0], oldDraft);
});

test('paste during an active import is visibly rejected without silently queueing another upload', async () => {
  const {ctx, calls} = fixture();
  let resolve;
  ctx.api = () => new Promise(r => { resolve = r; });
  const importing = ctx.addFiles([{name: 'first.txt', size: 4}]);
  await ctx.addFiles([{name: 'second.txt', size: 4}]);
  assert.ok(calls.some(c => c[0] === 'toast' && /完成后重新选择/.test(c[1])));
  assert.equal(ctx.S.pendingFiles.length, 0);
  resolve({verified: true, size: 4, path: '/first.txt', filename: 'first.txt', sha256: 'a'.repeat(64)});
  await importing;
});

test('profile switch during upload keeps image metadata with original owner and hides foreign receipt', async () => {
  const {ctx, elements, observers} = fixture();
  ctx.S.activeProfile = 'original';
  let resolve;
  const remembered = [];
  ctx.androidRememberImportedAttachment = (...args) => { remembered.push(args); return true; };
  ctx.api = () => new Promise(r => { resolve = r; });
  const pending = ctx.androidImportFiles([{name: 'private.png', size: 4}], 'owner');
  ctx.S.activeProfile = 'different';
  observers[0]();
  resolve({verified: true, size: 4, path: '/original/private.png', filename: 'private.png', sha256: 'a'.repeat(64), is_image: true, mime: 'image/png'});
  await pending;
  assert.equal(remembered[0][1], 'original');
  assert.equal(elements.msg.value, '');
  assert.equal(elements.androidImportStatus.textContent, '');
  ctx.S.activeProfile = 'original';
  observers[0]();
  assert.match(elements.androidImportStatus.textContent, /已导入并读回校验/);
});

test('changing owner while system picker is open does not upload into a different profile', async () => {
  const {ctx, elements, calls} = fixture();
  ctx.androidChooseImport();
  elements.androidImportInput.files = [{name: 'selected.png', size: 4}];
  ctx.S.activeProfile = 'different';
  await elements.androidImportInput.onchange();
  assert.equal(calls.filter(c => c[0] === '/api/workspace/upload').length, 0);
  assert.ok(calls.some(c => c[0] === 'toast' && /已切换/.test(c[1])));
});
