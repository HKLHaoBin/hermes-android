#!/usr/bin/env python3
"""Install payload.zip the way the app's PayloadInstaller.kt does (CI smoke test only).

Extracts into --dest, but resolves placeholders against --final-root: the path the
tree will have when it is used (inside the Android test container).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import zipfile
from pathlib import Path


def write_state(path: Path, *, active: str | None, previous_good: str | None,
                pending: bool, generation: int, rejected: str | None = None) -> None:
    value = {
        "format": 1,
        "generation": generation,
        "active": active,
        "previous_good": previous_good,
        "pending": pending,
        "rejected": rejected,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(temp, path)


def remove_tree(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", type=Path, required=True)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--dest", type=Path, required=True)
    ap.add_argument("--final-root", required=True)
    ap.add_argument("--slot-root", type=Path)
    ap.add_argument("--state", type=Path)
    ap.add_argument("--simulate-health-failure", action="store_true")
    args = ap.parse_args()
    if (args.slot_root is None) != (args.state is None):
        ap.error("--slot-root and --state must be supplied together")

    m = json.loads(args.manifest.read_text(encoding="utf-8"))
    payload_id = m["payload_id"]
    expected_zip_sha256 = (m.get("payload") or {}).get("zip_sha256")
    if expected_zip_sha256:
        digest = hashlib.sha256()
        with args.zip.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        digest = digest.hexdigest()
        if digest != expected_zip_sha256:
            raise SystemExit(f"payload zip SHA-256 mismatch: {digest}")
    state_path = args.state
    slot_root = args.slot_root
    previous = None
    generation = 0
    if slot_root is not None and state_path is not None:
        expected_dest = (slot_root / payload_id).resolve()
        if args.dest.resolve() != expected_dest:
            raise SystemExit("versioned smoke destination must be slot-root/payload_id")
        slot_root.mkdir(parents=True, exist_ok=True)
        if state_path.is_file():
            old = json.loads(state_path.read_text(encoding="utf-8"))
            generation = int(old.get("generation", 0))
            previous = old.get("active")
            if previous == payload_id:
                previous = old.get("previous_good")

    dest = args.dest
    activated = False
    committed = False
    try:
        remove_tree(dest)
        dest.mkdir(parents=True)
        with zipfile.ZipFile(args.zip) as zf:
            for name in zf.namelist():
                if name.startswith("/") or ".." in name.split("/"):
                    raise SystemExit(f"unsafe entry {name}")
            zf.extractall(dest)

        ph = m["placeholders"]
        final = args.final_root
        prefix = f"{final}/{ph.get('prefix_dir', 'prefix')}"

        def resolve(value: str) -> str:
            return value.replace(ph["root"], final).replace(ph["prefix"], prefix)

        for rel in m["rewrite"]:
            f = dest / rel
            if f.is_file():
                text = f.read_bytes().decode("latin-1")
                f.write_bytes(resolve(text).encode("latin-1"))
        for rel, target in m["symlinks"]:
            link = dest / rel
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.is_symlink() or link.exists():
                link.unlink()
            os.symlink(resolve(target), link)
        for rel in m["executables"]:
            f = dest / rel
            if f.is_file():
                f.chmod(0o755)
        (dest / ".payload_id").write_text(payload_id, encoding="utf-8")
        (dest / ".payload_manifest.json").write_text(
            json.dumps(m, sort_keys=True) + "\n", encoding="utf-8"
        )

        if state_path is not None and slot_root is not None:
            write_state(state_path, active=payload_id, previous_good=previous,
                        pending=True, generation=generation + 1)
            activated = True

        if args.simulate_health_failure:
            if state_path is None or slot_root is None:
                raise SystemExit("--simulate-health-failure requires versioned slot state")
            remove_tree(dest)
            write_state(state_path, active=previous, previous_good=None,
                        pending=False, generation=generation + 2, rejected=payload_id)
            activated = False
            committed = True
            print(f"simulated health rollback {payload_id} -> {previous or 'none'}")
            return 0

        if state_path is not None and slot_root is not None:
            write_state(state_path, active=payload_id, previous_good=previous,
                        pending=False, generation=generation + 1)
            activated = False
            committed = True
            keep = {payload_id}
            if previous:
                keep.add(previous)
            for child in slot_root.iterdir():
                if child.name not in keep:
                    remove_tree(child)
        else:
            committed = True

        print(f"installed {payload_id}: {len(m['rewrite'])} rewritten, "
              f"{len(m['symlinks'])} links, {len(m['executables'])} executables")
        return 0
    except BaseException:
        if committed:
            raise
        if state_path is not None and slot_root is not None and activated:
            remove_tree(dest)
            write_state(state_path, active=previous, previous_good=None,
                        pending=False, generation=generation + 2, rejected=payload_id)
        elif not activated:
            remove_tree(dest)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
