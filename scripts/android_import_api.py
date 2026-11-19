"""Byte-level import receipt; no archive extraction and no URI-to-path guessing."""
import hashlib
import io
import os
import zipfile


def verified_import_receipt(workspace, dest, expected):
    from api.workspace import open_anchored_fd
    size = 0
    digest = hashlib.sha256()
    # Reopen the exact saved file under the trusted workspace, without following
    # a symlink raced into its path. Never accept stat() as a read test.
    with os.fdopen(open_anchored_fd(workspace, dest, want_dir=False), 'rb') as saved:
        for chunk in iter(lambda: saved.read(65536), b''):
            size += len(chunk)
            digest.update(chunk)
    sha256 = digest.hexdigest()
    if size != len(expected) or sha256 != hashlib.sha256(expected).hexdigest():
        raise ValueError('导入后校验不一致，请重新选择文件。')
    receipt = {
        'filename': dest.name,
        'path': str(dest),
        'size': size,
        'sha256': sha256,
        'verified': True,
        'extracted': False,
        'is_image': False,
    }
    if dest.suffix.lower() == '.zip':
        # Only structural validation, not decompression of untrusted archives.
        # Do not claim this validates the conversation format or every ZIP CRC.
        receipt['zip_valid'] = zipfile.is_zipfile(io.BytesIO(expected))
    return receipt
