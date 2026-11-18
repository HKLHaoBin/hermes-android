"""Build-time compatibility patches for the Android bundle, not live installs."""
from pathlib import Path


def replace_once(path, old, new):
    text = path.read_text(encoding='utf-8')
    if text.count(old) != 1:
        raise RuntimeError(f'Android compatibility anchor changed in {path.name}')
    text = text.replace(old, new)
    compile(text, str(path), 'exec')
    path.write_text(text, encoding='utf-8')


def patch_payload(stage):
    app = stage / 'app'
    replace_once(app / 'tools/code_kernel.py',
        '        sock_tmpdir = socket_safe_tmpdir()\n        rpc_endpoint = kernel.sock_path = os.path.join(sock_tmpdir, f"hermes_rpc_{uuid.uuid4().hex}.sock")',
        '''        filename = f"hermes_rpc_{uuid.uuid4().hex}.sock"
        if os.environ.get("HERMES_ANDROID_APP"):
            import tempfile
            sock_tmpdir = tempfile.gettempdir()
            # Keep the socket inside the app sandbox, never a shared /tmp.
            if len(os.fsencode(os.path.join(sock_tmpdir, filename))) >= 108:
                filename = f"h-{uuid.uuid4().hex[:20]}.sock"
        else:
            sock_tmpdir = socket_safe_tmpdir()
        rpc_endpoint = kernel.sock_path = os.path.join(sock_tmpdir, filename)''')
    replace_once(app / 'tools/environments/local.py',
        'super().__init__(cwd=_resolve_local_initial_cwd(cwd), timeout=timeout, env=env, persist_env=persistent)',
        '''super().__init__(cwd=_resolve_local_initial_cwd(cwd), timeout=timeout, env=env, persist_env=persistent)
        if os.environ.get("HERMES_ANDROID_APP") and _find_bash() == "/system/bin/sh":
            # Android sh has no Bash PIPESTATUS array. Preserve the actual shell exit code.
            self._pipe_marker = ""''')
    streaming = stage / 'webui/api/streaming.py'
    replace_once(streaming, '    return getattr(session_db, "_conn", None) is not None',
        '''    from hermes_state import _SessionDB
    if isinstance(session_db, _SessionDB):
        return not session_db._closed and session_db._store is not None
    return getattr(session_db, "_conn", None) is not None''')
    replace_once(streaming, '                        if _cached_agent_matches_session(_cached_agent, session_id):',
        '''                        # Some runtimes make the Agent and its context engine borrow
                        # the SessionDB's handle at construction. Rebuild rather than
                        # replacing only the wrapper under a closed borrowed handle.
                        _cached_db = getattr(_cached_agent, '_session_db', None)
                        _borrowed_closed = (
                            getattr(_cached_agent, 'persistence', None) is not None
                            and not _session_db_is_open(_cached_db)
                        )
                        if _cached_agent_matches_session(_cached_agent, session_id) and not _borrowed_closed:''')
