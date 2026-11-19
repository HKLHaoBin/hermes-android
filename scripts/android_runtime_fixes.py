"""Build-time compatibility patches for the Android bundle, not live installs."""
from pathlib import Path


def replace_once(path, old, new):
    text = path.read_text(encoding='utf-8')
    if text.count(old) != 1:
        raise RuntimeError(f'Android compatibility anchor changed in {path.name}')
    text = text.replace(old, new)
    compile(text, str(path), 'exec')
    path.write_text(text, encoding='utf-8')


def patch_environment_guidance(stage):
    # Pinned Android payload f7e2898a: inject into the normal environment block,
    # never into a user's profile/config/memory or a cached conversation.
    replace_once(stage / 'app/agent/prompt_builder.py',
        '    hints += [WSL_ENVIRONMENT_HINT] if is_wsl() else []\n'
        '    return "\\n\\n".join(h for h in (*hints, _embedder_environment_hint()) if h)',
        '''    hints += [WSL_ENVIRONMENT_HINT] if is_wsl() else []
    if os.environ.get("HERMES_ANDROID_APP"):
        hints.append(
            "Android app runtime capabilities (Android host, not a configured remote terminal):\\n"
            "- Shared-storage access: distinguish directory enumeration, exact-path os.stat, "
            "open/read of bytes, and writing the app-private destination. A successful stat "
            "does not prove readable bytes; EACCES on open is a read-permission failure, "
            "not proof the file is absent or corrupt. An empty/filtered listing does not prove absence. "
            "App-private files and a tested writable working directory remain usable.\\n"
            "- For inaccessible shared-storage bytes, prefer the existing app import/upload UI "
            "with the system file picker. A URI grant does not grant shell-path access. "
            "Verify server-received bytes, size and readback in app-private storage before "
            "claiming import succeeded; picker selection alone is not delivery to the agent. "
            "Use the active HERMES_HOME and actual workdir; do not hardcode another profile's paths.\\n"
            "- Shell portability: inspect the installed command's help and preserve stderr and exit status. "
            "Android toybox find may support -printf, -mmin and -mtime; do not declare them "
            "universally unsupported. Date parsing for -newermt '-1 hour' may fail even when "
            "the option exists. Prefer Python os.scandir and os.stat().st_mtime with an explicit "
            "numeric time cutoff for portable time filters, reporting enumeration/stat errors "
            "rather than suppressing them as empty results.\\n"
            "- A listed tool is not proof its backend/dependencies work. browser_exec needs "
            "an actual browser backend; the app WebView is not an automation Chromium. "
            "If a call reports missing Chromium, report that concrete limitation and do not retry "
            "unchanged or promise an installation. Do not infer that a configured remote "
            "browser or CDP endpoint is unavailable merely because local Chromium is absent; "
            "keep configured remote backends usable. Use available web search/extraction for "
            "public pages when suitable, without claiming browser-only work was done.\\n"
            "- Do not guess sudo, chmod, Shizuku, pkg/apt installs, permission toggles or "
            "privilege workarounds. Inspect actual capabilities and manifest/permission evidence "
            "before recommending changes; do not alter device security or install software "
            "without authorization. State blockers honestly and use verified available tools."
        )
    return "\\n\\n".join(h for h in (*hints, _embedder_environment_hint()) if h)''')


def patch_payload(stage):
    patch_environment_guidance(stage)
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
