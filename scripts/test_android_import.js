'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function fixture() {
  const elements = Object.fromEntries(['androidImportStatus', 'androidImportInput', 'androidImportButton', 'msg', 'composerBox', 'attachTray', 'androidAttachmentHint'].map(id => [id, {
    textContent: '', value: '', disabled: false, files: [], click() { this.clicked = true; }, dispatchEvent() { this.inputEvents = (this.inputEvents || 0) + 1; },
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
  ctx.androidChooseImport();
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
  assert.equal(elements.androidImportButton.disabled, false);
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
    assert.equal(elements.androidImportButton.disabled, false);
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

test('partial batch failure does not hide the successful file and pending attachment hint is explicit', async () => {
  const {ctx, elements, observers} = fixture();
  await ctx.androidImportFiles([{name: 'empty.zip', size: 0}, {name: 'ok.txt', size: 1}], 'owner');
  assert.match(elements.androidImportStatus.textContent, /导入失败：empty.zip/);
  assert.match(elements.androidImportStatus.textContent, /已导入并读回校验：ok.txt/);
  ctx.S.pendingFiles = [{name: 'pending.zip'}];
  observers[1]();
  assert.match(elements.androidAttachmentHint.textContent, /尚未发送/);
  ctx.S.pendingFiles = [];
  observers[1]();
  assert.equal(elements.androidAttachmentHint.textContent, '');
});
