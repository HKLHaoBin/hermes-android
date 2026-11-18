"""Exercise the real bundled Dashboard alongside the bundled WebUI."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

root, port = Path(sys.argv[1]), sys.argv[2]
origin = f'http://127.0.0.1:{port}'
opener = build_opener(ProxyHandler({}))


def denied(path, headers=None):
    try:
        with opener.open(Request(origin + path, headers=headers or {}), timeout=5):
            raise AssertionError('Dashboard unexpectedly allowed request')
    except HTTPError as error:
        assert error.code == 401, error.code


with tempfile.TemporaryDirectory() as directory:
    ready = Path(directory) / 'dashboard.ready'
    env = os.environ.copy()
    env.pop('HERMES_DASHBOARD_SESSION_TOKEN', None)
    env.update(HERMES_ANDROID_DASHBOARD_READY=str(ready), HERMES_ANDROID_DASHBOARD_PORT=port)
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(
            [sys.executable, '-P', str(root / 'webui/android_dashboard.py')],
            env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 90
            while not ready.is_file() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.25)
            assert ready.is_file() and process.poll() is None, f'Dashboard not ready; exit={process.poll()}'
            cookie = ready.read_text(encoding='ascii').split(';', 1)[0]
            denied('/')
            denied('/api/config')
            denied('/', {'Cookie': cookie, 'Origin': 'https://example.invalid'})
            with opener.open(Request(origin + '/', headers={'Cookie': cookie}), timeout=10) as response:
                assert response.status == 200 and b'Hermes' in response.read()
            print('DASHBOARD_OK')
        except Exception:
            # Disposable CI home, no provider credentials. Never print the ready file or headers.
            log.seek(0)
            print(log.read(16384).decode(errors='replace'), file=sys.stderr)
            raise
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()