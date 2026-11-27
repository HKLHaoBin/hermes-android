"""Bundle the pinned WebUI's terminal assets and Android runtime paths."""

import base64
import hashlib
import re
import sys
from pathlib import Path
from urllib.request import urlopen

from stage_android_import import stage_import

root = Path(sys.argv[1])
index = root / 'static/index.html'
html = index.read_text(encoding='utf-8')
pattern = re.compile(
    r'(?P<url>https://cdn\.jsdelivr\.net/npm/xterm(?:-addon-fit|-addon-web-links)?@[^"\s]+)'
    r'" integrity="sha384-(?P<hash>[A-Za-z0-9+/=]+)"'
)


def download(url):
    with urlopen(url, timeout=30) as response:
        return response.read(2_000_001)


def vendor(match):
    url = match['url']
    package, path = url.split('/npm/', 1)[1].split('/', 1)
    local = root / 'static/vendor/android-terminal' / package / path
    data = download(url)
    if base64.b64encode(hashlib.sha384(data).digest()).decode() != match['hash']:
        raise SystemExit(f'Unexpected hash for {package}/{path}')
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(data)
    license_path = root / 'static/vendor/android-terminal' / package / 'LICENSE'
    if not license_path.exists():
        license_bytes = download(f'https://cdn.jsdelivr.net/npm/{package}/LICENSE')
        if b'MIT License' not in license_bytes and b'Permission is hereby granted' not in license_bytes:
            raise SystemExit(f'Unexpected license for {package}')
        license_path.write_bytes(license_bytes)
    return f'{local.relative_to(root).as_posix()}" integrity="sha384-{match["hash"]}"'


html, count = pattern.subn(vendor, html)
if count != 4:
    raise SystemExit(f'Expected four pinned terminal assets, got {count}')
anchor_button = '            <button type="button" class="icon-btn has-tooltip" id="btnSavedPrompts"'
if html.count(anchor_button) != 1:
    raise SystemExit('Composer toolbar insertion point changed')
html = html.replace(anchor_button, '''            <button type="button" class="icon-btn has-tooltip" id="androidComposerTerminal" data-tooltip="Terminal" data-i18n-title="terminal_title" data-i18n-aria-label="terminal_title" aria-label="Terminal" onclick="cmdTerminal()">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m4 6 6 6-6 6m9 0h7"/></svg>
            </button>
''' + anchor_button)
index.write_text(html, encoding='utf-8')

terminal = root / 'api/terminal.py'
source = terminal.read_text(encoding='utf-8')
anchor = '        env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}'
if source.count(anchor) != 1:
    raise SystemExit('Embedded terminal environment contract changed')
source = source.replace(anchor, '        # Bundle paths only; do not inherit server passwords or provider keys.\n'
    '        _SAFE_ENV_KEYS.update({"HERMES_HOME", "PREFIX", "PATH", "LD_LIBRARY_PATH", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "HERMES_ANDROID_TOOLCHAIN_ROOT", "HERMES_ANDROID_TOOLCHAIN_CATALOG", "HERMES_ANDROID_TOOLCHAIN_SCRIPT"})\n' + anchor)
terminal.write_text(source, encoding='utf-8')
stage_import(root)