"""Stage explicit, verified Android file imports into the pinned public WebUI."""
from pathlib import Path
import shutil


def replace_once(path, old, new):
    text = path.read_text(encoding='utf-8')
    if text.count(old) != 1:
        raise RuntimeError(f'Android import anchor changed: {path.name}')
    path.write_text(text.replace(old, new), encoding='utf-8')


def stage_import(root):
    root = Path(root)
    here = Path(__file__).resolve().parent
    shutil.copyfile(here / 'android_import.js', root / 'static/android_import.js')
    shutil.copyfile(here / 'android_import_chat.js', root / 'static/android_import_chat.js')
    shutil.copyfile(here / 'android_import_api.py', root / 'api/android_import.py')
    index = root / 'static/index.html'
    replace_once(index,
        '        <div class="attach-tray" id="attachTray"></div>',
        '''        <div id="androidImportStatus" role="status" aria-live="polite" style="white-space:pre-wrap;overflow-wrap:anywhere;max-height:140px;overflow:auto;font-size:12px;padding:0 10px"></div>
        <input type="file" id="androidImportInput" class="file-input-visually-hidden" multiple accept="*/*">
        <div class="attach-tray" id="attachTray"></div>''')
    replace_once(index,
        '<script src="static/outline.js?v=__WEBUI_VERSION__" defer></script>',
        '<script src="static/outline.js?v=__WEBUI_VERSION__" defer></script>\n'
        '<script src="static/android_import_chat.js?v=__WEBUI_VERSION__" defer></script>\n'
        '<script src="static/android_import.js?v=__WEBUI_VERSION__" defer></script>')
    upload = root / 'api/upload.py'
    replace_once(upload,
        "        subpath = fields.get('path', '')",
        "        subpath = fields.get('path', '')\n"
        "        android_import = fields.get('android_import') == '1'")
    replace_once(upload,
        "            safe_name = _sanitize_upload_name(filename)\n            dest = safe_resolve_ws(target_dir, safe_name)",
        "            if android_import and not file_bytes:\n"
        "                return j(handler, {'error': '选中的文件没有可读取的内容（0 字节），请重新选择。'}, status=400)\n"
        "            safe_name = _sanitize_upload_name(filename)\n            dest = safe_resolve_ws(target_dir, safe_name)")
    replace_once(upload,
        '            # For archives, optionally extract into the target directory.',
        '''            if android_import:
                # Explicit import keeps the original archive. A successful stat or
                # HTTP response alone is not a successful byte-level import.
                from api.android_import import verified_import_receipt
                receipt = verified_import_receipt(workspace, dest, file_bytes)
                results.append(receipt)
                print(f'[android-import] saved_verified bytes={receipt["size"]} sha256={receipt["sha256"]}', flush=True)
                continue

            # For archives, optionally extract into the target directory.''')
    compile(upload.read_text(encoding='utf-8'), str(upload), 'exec')
