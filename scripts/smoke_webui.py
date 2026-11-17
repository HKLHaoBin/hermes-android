"""Probe actual WebUI auth and assets on Android without model credentials."""

import os
from pathlib import Path
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
for path, expected in [('/', b'Hermes'), ('/api/sessions', b'sessions')]:
    with opener.open(Request(root + path, headers={'Cookie': cookie}), timeout=10) as response:
        assert response.status == 200 and response.geturl() == root + path
        assert expected in response.read(), path
print('SMOKE_OK')