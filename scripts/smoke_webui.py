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