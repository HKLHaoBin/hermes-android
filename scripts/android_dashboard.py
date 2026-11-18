"""Launch the official Dashboard behind an Android-app-only loopback gate."""

from hmac import compare_digest
from http.cookies import CookieError, SimpleCookie
import os
from pathlib import Path
import secrets
import site
import sys

site.addsitedir(os.environ['HERMES_SITE'])

from hermes_cli import web_server
from hermes_cli.main import main

ready = Path(os.environ.pop('HERMES_ANDROID_DASHBOARD_READY'))
port = int(os.environ.pop('HERMES_ANDROID_DASHBOARD_PORT'))
secret = secrets.token_urlsafe(32)
origin = f'http://127.0.0.1:{port}'.encode()
host = origin.removeprefix(b'http://')
original_build = web_server._build_uvicorn_server
original_started = web_server._on_server_started


class AppGate:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        kind = scope['type']
        if kind not in ('http', 'websocket'):
            return await self.inner(scope, receive, send)
        headers = dict(scope['headers'])
        try:
            cookies = SimpleCookie(headers.get(b'cookie', b'').decode('ascii'))
            value = cookies['hermes_android_dashboard'].value.encode('ascii')
        except (CookieError, KeyError, UnicodeError):
            value = b''
        incoming_origin = headers.get(b'origin')
        origin_ok = incoming_origin == origin or (incoming_origin is None and kind == 'http')
        if headers.get(b'host') == host and origin_ok and compare_digest(value, secret.encode()):
            return await self.inner(scope, receive, send)
        if kind == 'websocket':
            return await send({'type': 'websocket.close', 'code': 1008})
        await send({'type': 'http.response.start', 'status': 401, 'headers': [(b'cache-control', b'no-store')]})
        await send({'type': 'http.response.body', 'body': b'Unauthorized'})


def guarded_server(bind_host, bind_port, **kwargs):
    if (bind_host, bind_port) != ('127.0.0.1', port):
        raise RuntimeError('Unexpected Android Dashboard bind')
    config, server = original_build(bind_host, bind_port, **kwargs)
    config.app = AppGate(config.app)
    config.proxy_headers = False
    return config, server


def publish_ready(*args, **kwargs):
    original_started(*args, **kwargs)
    temporary = ready.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='ascii') as output:
        output.write(f'hermes_android_dashboard={secret}; Path=/; HttpOnly; SameSite=Strict')
    temporary.replace(ready)


web_server._build_uvicorn_server = guarded_server
web_server._on_server_started = publish_ready
sys.argv[:] = ['hermes', 'dashboard', '--host', '127.0.0.1', '--port', str(port), '--no-open', '--build-mode', 'skip']
sys.exit(main())