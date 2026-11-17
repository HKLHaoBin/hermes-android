"""Start the pinned WebUI and transfer its session over app-private storage."""

import os
from pathlib import Path
import site
import sys

site.addsitedir(os.environ['HERMES_SITE'])
sys.path.insert(0, str(Path(__file__).resolve().parent))

import server
from api.auth import _auth_cookie_header, create_session, is_auth_enabled
from api.config import verify_hermes_imports


class AndroidServer(server.QuietHTTPServer):
    def serve_forever(self, *args, **kwargs):
        if self.server_address[0] != '127.0.0.1' or not is_auth_enabled():
            raise RuntimeError('Android WebUI requires loopback and authentication')
        if not verify_hermes_imports()[0]:
            raise RuntimeError('Android Hermes Agent imports failed')
        ready = Path(os.environ.pop('HERMES_ANDROID_READY'))
        temporary = ready.with_suffix('.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='ascii') as output:
            output.write(_auth_cookie_header(create_session()))
        temporary.replace(ready)
        return super().serve_forever(*args, **kwargs)


server.QuietHTTPServer = AndroidServer
server.main()