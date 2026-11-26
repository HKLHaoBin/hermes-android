#!/usr/bin/env python3
"""Canonical identity and deterministic tree hashing for Android payloads."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_tree(root: Path) -> str:
    """Hash names, file types, modes and bytes without mtimes or absolute paths."""
    digest = hashlib.sha256()
    if not root.is_dir():
        raise ValueError(f"identity tree is not a directory: {root}")
    entries = sorted((p for p in root.rglob("*") if p.is_file() or p.is_symlink()),
                     key=lambda p: p.relative_to(root).as_posix())
    for path in entries:
        rel = path.relative_to(root).as_posix().encode("utf-8")
        if path.is_symlink():
            digest.update(b"L\0" + rel + b"\0" + os.readlink(path).encode("utf-8") + b"\n")
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        size = path.stat().st_size
        digest.update(f"F\0{mode:o}\0{size}\0".encode("ascii") + rel + b"\0")
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        digest.update(b"\n")
    return digest.hexdigest()


def sha256_files(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.name):
        digest.update(b"F\0" + path.name.encode("utf-8") + b"\0")
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        digest.update(b"\n")
    return digest.hexdigest()


def build_payload_id(identity: dict[str, Any]) -> str:
    return sha256_bytes(canonical_bytes(identity))


def build_pair_identity(meta: dict[str, Any], stamp: dict[str, Any], webui: dict[str, Any],
                        compatibility: dict[str, Any], zip_sha256: str, staged_webui_sha256: str | None) -> dict[str, Any]:
    return {
        "runtime": {
            "channel": meta.get("channel", ""),
            "version": meta.get("version", ""),
            "deb_sha256": meta.get("sha256", ""),
            "source_commit": stamp.get("commit", ""),
        },
        "webui": {
            "channel": webui.get("channel", ""),
            "repository": webui.get("repository", ""),
            "tag": webui.get("tag", ""),
            "commit": webui.get("commit", ""),
            "staged_sha256": staged_webui_sha256 or "",
            "android_patch_set": webui.get("android_patch_set", "issue-7-safe-updates-v1"),
        },
        "compatibility": compatibility,
        "zip_sha256": zip_sha256,
    }
