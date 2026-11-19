'use strict';
// Node component regressions: real pinned api and send() request expression,
// synthetic fetch only. No browser, Python native consumer or provider execution.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const staticRoot = process.env.ANDROID_PINNED_STATIC ||
  (process.env.HERMES_WEBUI_SOURCE && path.join(process.env.HERMES_WEBUI_SOURCE, 'static'));
assert.ok(staticRoot, 'Set HERMES_WEBUI_SOURCE or ANDROID_PINNED_STATIC to the pinned public WebUI source');
function pinned(name, hash) {
  const source = fs.readFileSync(path.join(staticRoot, name), 'utf8');
  assert.equal(crypto.createHash('sha256').update(source).digest('hex'), hash,
    `${name}: wrong pinned source; set ANDROID_PINNED_STATIC to unmodified 21426ee/static`);
  return source;
}
const workspace = pinned('workspace.js', 'd79559a4d2f836d5e45718ae3cfd126989476d1fc9ec157b07448919d143cbe9');
const messages = pinned('messages.js', '48ccd774384e72ae10de93bbb222afc9ffadc6706bc5c16174b26c2498c9ff93');
// Exact source-bound boundaries include the entire API helper and the entire
// first send() api invocation (not a reconstructed lookalike payload).
const apiEnd = workspace.indexOf('\nfunction recordClientSSEError(');
assert.ok(apiEnd > 0);
const callerStart = messages.indexOf("const startData=await api('/api/chat/start',");
const callerEnd = messages.indexOf('\n    _pendingMoaConfig=null;', callerStart);
assert.ok(callerStart > 0 && callerEnd > callerStart);
const caller = messages.slice(callerStart, callerEnd);
assert.ok(caller.endsWith('})});'));
function response(value = {stream_id: 'accepted'}, status = 200) {
  return {ok: status >= 200 && status < 300, status, statusText: 'synthetic',
    headers: {get: () => 'application/json'}, json: async () => value,
    text: async () => JSON.stringify(value)};
}
function fixture(loadImporter = false) {
  const calls = [];
  const elements = Object.fromEntries(['androidImportStatus', 'androidImportInput', 'btnAttach', 'msg', 'composerBox'].map(id => [id, {
    value: '', textContent: '', setAttribute() {}, removeAttribute() {}, dispatchEvent() {}, click() {},
  }]));
  const ctx = {URL, AbortController, setTimeout, clearTimeout, console,
    document: {baseURI: 'https://test.invalid/hermes/', getElementById: id => elements[id], body: {}},
    MutationObserver: class { observe() {} },
    Event: class {},
    FormData: class { constructor() { this.fields = {}; } append(key, value) { this.fields[key] = value; } },
    showToast() {}, loadDir: async () => {},
    location: {href: 'https://test.invalid/hermes/', pathname: '/hermes/', search: ''},
    S: {activeProfile: 'work', session: {session_id: 'owner', profile: 'work', workspace: '/ws'}},
    activeSid: 'owner', msgText: '', uploaded: [],
    _modelState: {model: 'vision-model', model_provider: 'provider'},
    _explicitPick: false, _pendingMoaConfig: null, _goalContinuationId: '',
  };
  ctx.window = ctx;
  let transport = async () => response();
  ctx.fetch = async (url, options) => { calls.push({url, options}); return transport(url, options); };
  vm.createContext(ctx);
  vm.runInContext(workspace.slice(0, apiEnd), ctx, {filename: 'pinned/workspace.js'});
  const bridge = path.join(__dirname, 'android_import_chat.js');
  if (fs.existsSync(bridge)) vm.runInContext(fs.readFileSync(bridge, 'utf8'), ctx, {filename: bridge});
  if (loadImporter) {
    const importer = path.join(__dirname, 'android_import.js');
    vm.runInContext(fs.readFileSync(importer, 'utf8'), ctx, {filename: importer});
  }
  return {ctx, calls, elements, transport(fn) { transport = fn; },
    remember(r, sid = 'owner', profile = 'work') {
      if (ctx.androidRememberImportedAttachment) return ctx.androidRememberImportedAttachment(sid, profile, r);
    },
    send(message, uploaded = []) {
      ctx.msgText = message; ctx.uploaded = uploaded;
      return vm.runInContext(`(async()=>{${caller}\nreturn startData;})()`, ctx);
    }};
}
const image = (suffix = 'photo.png') => ({verified: true, path: '/ws/' + suffix,
  filename: suffix, size: 67, sha256: 'a'.repeat(64), mime: 'image/png', is_image: true});
const note = r => `已导入本地文件：${JSON.stringify(r.path)}（${r.size} 字节）。`;
const body = f => JSON.parse(f.calls.at(-1).options.body);
const hasImages = f => body(f).attachments?.some(a => a && a.is_image === true) || false;

// Additional component acceptance coverage of ownership, admission and transport.
test('receipt requires verified image metadata and explicit originating ownership', async () => {
  const f = fixture();
  for (const changes of [{verified: false}, {size: 0}, {size: 1.2}, {sha256: 'bad'},
    {path: ''}, {is_image: false}, {mime: 'application/zip'}, {mime: undefined}]) {
    const r = {...image(), ...changes};
    assert.equal(f.remember(r), false);
    await f.send(note(image()));
    assert.equal(hasImages(f), false);
  }
  const g = fixture();
  assert.equal(g.ctx.androidRememberImportedAttachment('owner', undefined, image()), false);
  assert.equal(g.ctx.androidRememberImportedAttachment('', 'work', image()), false);
  assert.equal(g.ctx.androidRememberImportedAttachment('owner', '', image()), false);
});

test('unrelated text, bare paths and changed/partial paths do not attach; matching note still works later', async () => {
  const f = fixture(), r = image();
  f.remember(r);
  for (const msg of ['hello', r.path, JSON.stringify(r.path + '.old'), JSON.stringify(r.path.slice(0, -1))]) {
    await f.send(msg);
    assert.equal(hasImages(f), false);
  }
  await f.send(note(r));
  assert.equal(hasImages(f), true);
  await f.send(note(r));
  assert.equal(hasImages(f), false, 'accepted receipt must not follow later turns');
});

test('session and profile payload ownership, not live UI state, selects receipts', async () => {
  const f = fixture(), r = image();
  f.remember(r);
  f.ctx.activeSid = 'other';
  await f.send(note(r));
  assert.equal(hasImages(f), false);
  f.ctx.activeSid = 'owner'; f.ctx.S.activeProfile = 'private';
  await f.send(note(r));
  assert.equal(hasImages(f), false);
  f.ctx.S.activeProfile = 'work';
  await f.send(note(r));
  assert.equal(hasImages(f), true);
});

test('same session id/path in another profile has independent receipt lifetime', async () => {
  const f = fixture(), r = image();
  f.remember(r); f.remember({...r, mime: 'image/jpeg'}, 'owner', 'private');
  await f.send(note(r));
  assert.equal(body(f).attachments[0].mime, 'image/png');
  f.ctx.S.activeProfile = 'private';
  await f.send(note(r));
  assert.equal(body(f).attachments[0].mime, 'image/jpeg');
});

test('existing attachment metadata merges without duplicates or mutation', async () => {
  const f = fixture(), r = image();
  f.remember(r); f.remember(r);
  const uploaded = [{path: '/ws/existing.pdf', mime: 'application/pdf'}, {path: r.path, name: 'keep-name', extra: 1}];
  const before = JSON.stringify(uploaded);
  await f.send(note(r), uploaded);
  assert.equal(body(f).attachments.length, 2);
  assert.equal(body(f).attachments[1].name, 'keep-name');
  assert.equal(body(f).attachments[1].mime, 'image/png');
  assert.equal(body(f).attachments[1].extra, 1);
  assert.equal(JSON.stringify(uploaded), before);
});

test('only explicitly referenced imported images are selected; ZIP stays text-only', async () => {
  const f = fixture(), a = image('a.png'), b = image('b.png');
  f.remember(a); f.remember(b);
  assert.equal(f.remember({...image('archive.zip'), mime: 'application/zip', is_image: false}), false);
  await f.send(note(a));
  assert.deepEqual(body(f).attachments.map(a => a.path), [a.path]);
  await f.send(note(b));
  assert.deepEqual(body(f).attachments.map(a => a.path), [b.path]);
});

for (const outcome of ['401', '500', 'undefined', 'no-stream', 'empty-stream', 'error']) {
  test(`receipt retained after ${outcome}, then consumed only after stream admission`, async () => {
    const f = fixture(), r = image(); f.remember(r);
    f.transport(async () => {
      if (outcome === 'error') throw new Error('transport failed');
      if (outcome === 'undefined') return response(undefined);
      if (outcome === 'no-stream') return response({ok: true});
      if (outcome === 'empty-stream') return response({stream_id: ''});
      return response({error: 'failure'}, Number(outcome));
    });
    // Explicit undefined needs a JSON response whose json() resolves undefined.
    if (outcome === 'undefined') f.transport(async () => ({...response(), json: async () => undefined}));
    if (outcome === '500' || outcome === 'error') await assert.rejects(f.send(note(r)));
    else await f.send(note(r));
    assert.equal(hasImages(f), true);
    assert.equal(f.calls.length, 1, 'bridge introduces no transport request/retry');
    f.transport(async () => response());
    await f.send(note(r)); assert.equal(hasImages(f), true);
    await f.send(note(r)); assert.equal(hasImages(f), false);
  });
}

test('in-flight completion consumes only captured identities, not newly imported receipts', async () => {
  const f = fixture(), a = image('a.png'), b = image('b.png');
  f.remember(a);
  let resolve;
  f.transport(() => new Promise(r => { resolve = r; }));
  const pending = f.send(note(a));
  assert.equal(typeof resolve, 'function');
  f.remember({...a, mime: 'image/jpeg'}); // same-path replacement must survive
  f.remember(b);
  // Also move the UI: acceptance cleanup remains scoped to captured owner.
  f.ctx.S.activeProfile = 'private'; f.ctx.activeSid = 'other';
  resolve(response()); await pending;
  f.ctx.S.activeProfile = 'work'; f.ctx.activeSid = 'owner';
  f.transport(async () => response());
  await f.send(note(a) + note(b));
  assert.equal(body(f).attachments.length, 2);
  assert.equal(body(f).attachments[0].mime, 'image/jpeg');
  await f.send(note(a) + note(b)); assert.equal(hasImages(f), false);
});

test('remembered metadata is copied, and fresh page contexts never inherit receipts', async () => {
  const f = fixture(), r = image(); f.remember(r);
  const original = {...r}; r.path = '/elsewhere'; r.mime = 'image/jpeg';
  await f.send(note(original));
  assert.equal(body(f).attachments[0].path, original.path);
  assert.equal(body(f).attachments[0].mime, 'image/png');
  const fresh = fixture(); await fresh.send(note(original));
  assert.equal(hasImages(fresh), false);
});

test('non-chat calls, malformed bodies and options retain pinned helper behavior', async () => {
  const f = fixture(); f.remember(image());
  const signal = new AbortController().signal;
  const options = {method: 'POST', body: 'raw', headers: {'X-Test': 'keep'},
    credentials: 'omit', signal, timeoutMs: 0, retries: 0, redirect401: false, cache: 'no-store'};
  await f.ctx.api('/api/workspace/upload', options);
  assert.equal(f.calls[0].options.body, 'raw');
  assert.equal(f.calls[0].options.headers, options.headers);
  assert.equal(f.calls[0].options.signal, signal);
  assert.equal(f.calls[0].options.credentials, 'omit');
  assert.equal(f.calls[0].options.cache, 'no-store');
  assert.equal('timeoutMs' in f.calls[0].options, false); // actual pinned helper strips controls
  assert.equal(options.timeoutMs, 0);
  for (const raw of ['not json', 'null', '{}', JSON.stringify({session_id: 'owner', profile: 'work', message: note(image()), attachments: {bad: true}})]) {
    await f.ctx.api('/api/chat/start', {...options, body: raw});
    assert.equal(f.calls.at(-1).options.body, raw);
  }
  await f.ctx.api('/api/chat/start', {...options, body: JSON.stringify({session_id: 'owner', profile: 'work', message: note(image())})});
  assert.equal(hasImages(f), true);
  assert.equal(f.calls.at(-1).options.headers, options.headers);
  assert.equal(f.calls.at(-1).options.signal, signal);
  assert.equal(f.calls.length, 6);
});

test('pinned retry policy remains caller-controlled; bridge never adds retries', async () => {
  for (const retries of [0, 2]) {
    const f = fixture(), r = image(); f.remember(r);
    f.transport(async () => { throw vm.runInContext('new TypeError("offline")', f.ctx); });
    const options = {method: 'POST', body: JSON.stringify({session_id: 'owner', profile: 'work', message: note(r)}),
      retries, retryDelayMs: 0, timeoutMs: 0};
    await assert.rejects(f.ctx.api('/api/chat/start', options), /offline/);
    assert.equal(f.calls.length, retries + 1);
    assert.ok(f.calls.every(c => JSON.parse(c.options.body).attachments.length === 1));
    f.transport(async () => response());
    await f.send(note(r)); assert.equal(hasImages(f), true);
  }
});

test('pinned timeout and upstream abort semantics preserve receipts for retry', async () => {
  const f = fixture(), r = image(); f.remember(r);
  let fetchSignal;
  f.transport(async (_url, options) => {
    fetchSignal = options.signal;
    return new Promise(() => {});
  });
  const upstream = new AbortController();
  await assert.rejects(f.ctx.api('/api/chat/start', {method: 'POST',
    body: JSON.stringify({session_id: 'owner', profile: 'work', message: note(r)}),
    signal: upstream.signal, timeoutMs: 1, timeoutToast: false, retries: 0}),
  error => error.name === 'TimeoutError');
  assert.equal(fetchSignal.aborted, true);
  assert.equal(upstream.signal.aborted, false);
  assert.equal(f.calls.length, 1);
  f.transport(async () => response());
  await f.send(note(r)); assert.equal(hasImages(f), true);
});

test('missing payload profile fails closed rather than taking the current UI profile', async () => {
  const f = fixture(), r = image(); f.remember(r);
  await f.ctx.api('/api/chat/start', {method: 'POST', body: JSON.stringify({session_id: 'owner', message: note(r)})});
  assert.equal(hasImages(f), false);
  await f.send(note(r)); assert.equal(hasImages(f), true);
});

test('STD-012-1: pinned send request carries imported image metadata without a second upload', async () => {
  const f = fixture(), r = image('图像 "quoted".png');
  f.remember(r);
  await f.send(note(r));
  assert.equal(body(f).attachments?.length, 1, 'native image attachment metadata must survive immediate import');
  assert.deepEqual(body(f).attachments[0], {path: r.path, name: r.filename, mime: r.mime, size: r.size, is_image: true});
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].url, 'https://test.invalid/hermes/api/chat/start');
  assert.equal(body(f).model, 'vision-model');
  assert.equal(body(f).profile, 'work');
  assert.equal(body(f).goal_continuation_id, '');
});

test('actual immediate importer remembers verified PNG and pinned chat request carries it with no second upload', async () => {
  const f = fixture(true), r = image('actual-import.png');
  f.transport(async url => url.endsWith('/api/workspace/upload') ? response(r) : response());
  await f.ctx.addFiles([{name: r.filename, size: r.size}]);
  assert.equal(f.calls.length, 1);
  assert.match(f.elements.androidImportStatus.textContent, /已导入并读回校验/);
  await f.send(f.elements.msg.value);
  assert.equal(f.calls.length, 2);
  assert.ok(f.calls[0].url.endsWith('/api/workspace/upload'));
  assert.ok(f.calls[1].url.endsWith('/api/chat/start'));
  assert.equal(body(f).attachments[0].path, r.path);
  assert.equal(body(f).attachments[0].mime, 'image/png');
  assert.equal(body(f).attachments[0].size, r.size);
  assert.equal(body(f).attachments[0].is_image, true);
});
