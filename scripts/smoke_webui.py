"""Probe actual WebUI auth and assets on Android without model credentials."""

import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

port = int(sys.argv[1])
root = f'http://127.0.0.1:{port}'
ready = Path(os.environ['TMPDIR']) / f'webui-{port}.ready'
opener = build_opener(ProxyHandler({}))
for _ in range(120):
    if ready.is_file():
        break
    time.sleep(0.5)
assert ready.is_file(), 'WebUI did not initialize'
cookie = ready.read_text().split(';', 1)[0]
ready.unlink()
with opener.open(root + '/health', timeout=10) as response:
    assert response.status == 200
try:
    opener.open(root + '/api/sessions', timeout=10)
except HTTPError as error:
    assert error.code == 401, error.code
else:
    raise AssertionError('Unauthenticated session list unexpectedly accessible')
page = ''
for path, expected in [('/', b'Hermes'), ('/api/sessions', b'sessions')]:
    with opener.open(Request(root + path, headers={'Cookie': cookie}), timeout=10) as response:
        assert response.status == 200 and response.geturl() == root + path
        body = response.read()
        assert expected in body, path
        if path == '/':
            page = body.decode()
print('SMOKE_OK')

assert re.search(r'<div class="composer-left">(?:(?!</div>).)*<button[^>]*id="androidComposerTerminal"[^>]*onclick="cmdTerminal\(\)"', page, re.S), 'Terminal entry missing from composer toolbar'

assets = re.findall(r'(static/vendor/android-terminal/[^"\s]+)', page)
assert len(assets) == 4, 'Expected four local terminal assets'
for asset in assets:
    with opener.open(root + '/' + asset, timeout=10) as response:
        assert response.status == 200 and len(response.read()) > 300, asset

csrf_match = re.search(r'csrfToken:"([^"]+)"', page)
assert csrf_match is not None, 'Authenticated WebUI did not supply a CSRF token'
csrf = csrf_match[1]


def post(path, body, auth=True, token=True):
    headers = {'Content-Type': 'application/json', 'Origin': root}
    if auth:
        headers['Cookie'] = cookie
    if token:
        headers['X-Hermes-CSRF-Token'] = csrf
    req = Request(root + '/api/' + path, json.dumps(body).encode(), headers)
    with opener.open(req, timeout=20) as response:
        return json.load(response)


session = post('session/new', {'worktree': False})['session']['session_id']
params = {'session_id': session}
for auth, token, status in [(False, False, 401), (True, False, 403)]:
    try:
        post('terminal/start', params, auth, token)
    except HTTPError as error:
        assert error.code == status, error.code
    else:
        raise AssertionError('Untrusted terminal start allowed')
assert post('terminal/start', params)['running']
try:
    req = Request(root + f'/api/terminal/output?session_id={session}', headers={'Cookie': cookie})
    with opener.open(req, timeout=40) as response:
        assert response.headers.get_content_type() == 'text/event-stream'
        post('terminal/input', params | {'data': 'hermes --version; printf "ANDROID_TERM_%s\\n" "$?"\r'})
        received = ''
        for _ in range(150):
            line = response.readline().decode()
            if not line:
                break
            if line.startswith('data: '):
                received += json.loads(line[6:]).get('text', '')
                if re.search(r'^ANDROID_TERM_0\r?$', received, re.M):
                    break
        assert re.search(r'^ANDROID_TERM_0\r?$', received, re.M), repr(received[-1200:])
finally:
    post('terminal/close', params)
print('TERMINAL_OK')

# Exercise the actual authenticated multipart route and saved bytes, not a
# mocked receipt. The test ZIP is generated here; no user conversations/keys.
import hashlib
import io
import zipfile


def import_file(name, data, auth=True, token=True):
    boundary = 'HermesAndroidImportSmokeBoundary'
    chunks = []
    for field, value in [('session_id', session), ('path', '.'), ('android_import', '1')]:
        chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n{value}\r\n').encode())
    chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode())
    chunks.extend([data, f'\r\n--{boundary}--\r\n'.encode()])
    headers = {'Content-Type': f'multipart/form-data; boundary={boundary}', 'Origin': root}
    if auth:
        headers['Cookie'] = cookie
    if token:
        headers['X-Hermes-CSRF-Token'] = csrf
    with opener.open(Request(root + '/api/workspace/upload', b''.join(chunks), headers), timeout=30) as response:
        return json.load(response)


assert page.count('id="btnAttach"') == 1 and 'static/android_import.js' in page
assert 'id="androidImportButton"' not in page, 'Duplicate import button still present'
assert 'id="androidAttachmentHint"' not in page, 'Obsolete pending-send hint still present'
with opener.open(Request(root + '/static/android_import.js', headers={'Cookie': cookie}), timeout=10) as response:
    assert b'androidImportFiles' in response.read()
archive = io.BytesIO()
with zipfile.ZipFile(archive, 'w') as output:
    output.writestr('conversation.json', '{"messages": [{"role": "user", "content": "test fixture"}]}')
contents = archive.getvalue()
for auth, token, status in [(False, False, 401), (True, False, 403)]:
    try:
        import_file('blocked.zip', contents, auth, token)
    except HTTPError as error:
        assert error.code == status, error.code
    else:
        raise AssertionError('Untrusted import allowed')
first = import_file('会话导入.zip', contents)
second = import_file('会话导入.zip', contents)
for receipt in [first, second]:
    assert receipt['verified'] is True and receipt['extracted'] is False
    assert receipt['size'] == len(contents) and receipt['zip_valid'] is True
    saved = Path(receipt['path'])
    assert saved.read_bytes() == contents
    assert hashlib.sha256(saved.read_bytes()).hexdigest() == receipt['sha256']
    assert not (saved.parent / 'conversation.json').exists(), 'Import unexpectedly extracted archive'
assert first['path'] != second['path'], 'Duplicate import overwrote original'
assert Path(first['path']).read_bytes() == contents
try:
    import_file('empty.zip', b'')
except HTTPError as error:
    assert error.code == 400, error.code
else:
    raise AssertionError('Zero-byte import accepted')
bad_zip = import_file('invalid.zip', b'not a zip')
assert bad_zip['verified'] is True and bad_zip['zip_valid'] is False
assert Path(bad_zip['path']).read_bytes() == b'not a zip'

# Use a real tiny PNG and the shipped native multimodal consumer. This is
# import-to-image-content coverage without a billed model request.
import base64
import struct
import zlib


def png_chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)


png = b'\x89PNG\r\n\x1a\n' + png_chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
png += png_chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00')) + png_chunk(b'IEND', b'')
image = import_file('导入图片.png', png)
assert image['verified'] is True and image['is_image'] is True and image['mime'] == 'image/png'
assert Path(image['path']).read_bytes() == png
runtime = Path(sys.argv[2])
sys.path.insert(0, str(runtime / 'webui'))
from api.streaming import _build_native_multimodal_message
parts = _build_native_multimodal_message('', 'Describe the imported image', [image], str(Path(image['path']).parent))
assert isinstance(parts, list), 'Imported image was reduced to a text-only path'
urls = [p['image_url']['url'] for p in parts if p.get('type') == 'image_url']
assert len(urls) == 1 and urls[0].startswith('data:image/png;base64,')
assert base64.b64decode(urls[0].split(',', 1)[1]) == png
print('ANDROID_IMPORT_IMAGE_OK')
print('ANDROID_IMPORT_OK')