"""Run the Android compatibility regression checks on the bundled interpreter."""
import ast
import logging
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace

from agent.agent_init_session import stand_in_interface
from hermes_state import SessionDB
from tools.code_kernel import _bind_rpc_socket
from tools.environments.local import LocalEnvironment

root = Path(sys.argv[1])
# Reuse exactly the shipped helpers, with real state.db handles, no model calls.
path = root / 'webui/api/streaming.py'
names = {'_session_db_is_open', '_adopt_session_db_for_cached_agent'}
tree = ast.parse(path.read_text(encoding='utf-8'))
tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
ns = {'logger': logging.getLogger(__name__)}
exec(compile(tree, str(path), 'exec'), ns)
old, fresh = SessionDB(), SessionDB()
try:
    agent = SimpleNamespace(_session_db=old, persistence=stand_in_interface(old))
    assert agent.persistence is not None
    assert ns['_adopt_session_db_for_cached_agent'](agent, fresh) is old
    assert ns['_session_db_is_open'](old) and not ns['_session_db_is_open'](fresh)
    assert agent.persistence.coordination is not None
finally:
    old.close()
    fresh.close()

kernel = SimpleNamespace(sock_path=None, server_sock=None)
try:
    endpoint = _bind_rpc_socket(kernel)
    assert Path(endpoint).parent == Path(tempfile.gettempdir())
    assert Path(endpoint).stat().st_mode & 0o777 == 0o600
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(endpoint)
        conn, _ = kernel.server_sock.accept()
        conn.close()
finally:
    if kernel.server_sock is not None:
        kernel.server_sock.close()
    if kernel.sock_path:
        Path(kernel.sock_path).unlink(missing_ok=True)

shell = LocalEnvironment(cwd=os.environ['HOME'])
try:
    shell.init_session()
    for cmd, code in [('printf ANDROID_SHELL_OK', 0), ('false', 1)]:
        result = shell.execute(cmd)
        assert result['returncode'] == code and 'syntax error' not in result['output'], result
        if code == 0:
            assert 'ANDROID_SHELL_OK' in result['output'], result
finally:
    shell.cleanup()
print('ANDROID_RUNTIME_FIXES_OK')
