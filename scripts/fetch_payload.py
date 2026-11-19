#!/usr/bin/env python3
"""Download and verify the official Hermes Agent Android (Termux) runtime package.

Nous Research publishes a signed APT repository with a self-contained
``hermes-agent`` .deb for aarch64 Android: bionic CPython 3.14, every native
wheel prebuilt for ``android_24_arm64_v8a``, Node.js, ripgrep, ffmpeg and the
Hermes source tree. This script:

1. downloads the repository key and checks its fingerprint against the one
   published in the upstream docs,
2. verifies ``InRelease`` with that key (gpgv),
3. checks the ``Packages`` index against the signed Release hashes,
4. downloads the chosen ``hermes-agent`` .deb and checks its SHA-256,
5. extracts it with ``dpkg-deb -x``.

Usage:
    fetch_payload.py --channel stable --out build/deb [--version X.Y.Z]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

BASE = "https://hermes-assets.nousresearch.com/releases/termux"
# From website/docs/getting-started/termux.md in NousResearch/hermes-agent.
KEY_FINGERPRINT = "C572B5FDD1A29CCFA9A912B6840B0848E139156D"
ARCH = "aarch64"
COMPONENT = "main"
PACKAGE = "hermes-agent"


def log(msg: str) -> None:
    print(f"[fetch] {msg}", flush=True)


def fetch(url: str, dest: Path | None = None) -> bytes:
    log(f"GET {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-android-build/1"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        if dest is None:
            return resp.read()
        with open(dest, "wb") as fh:
            shutil.copyfileobj(resp, fh, length=1 << 20)
        return b""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_key(key_path: Path, gnupg_home: Path) -> Path:
    """Import the key into an isolated keyring and pin its fingerprint."""
    env = {**os.environ, "GNUPGHOME": str(gnupg_home)}
    out = subprocess.run(
        ["gpg", "--batch", "--with-colons", "--show-keys", "--with-fingerprint", str(key_path)],
        check=True, capture_output=True, text=True, env=env,
    ).stdout
    fprs = [line.split(":")[9] for line in out.splitlines() if line.startswith("fpr:")]
    if not fprs or fprs[0].upper() != KEY_FINGERPRINT:
        raise SystemExit(f"repository key fingerprint mismatch: got {fprs[:1]}, want {KEY_FINGERPRINT}")
    keyring = gnupg_home / "hermes.gpg"
    subprocess.run(["gpg", "--batch", "--dearmor", "--output", str(keyring), str(key_path)],
                   check=True, env=env)
    log(f"repository key fingerprint OK ({KEY_FINGERPRINT})")
    return keyring


def verified_release(inrelease: Path, keyring: Path, gnupg_home: Path) -> str:
    env = {**os.environ, "GNUPGHOME": str(gnupg_home)}
    plain = gnupg_home / "Release"
    subprocess.run(["gpgv", "--keyring", str(keyring), "--output", str(plain), str(inrelease)],
                   check=True, env=env)
    log("InRelease signature OK")
    return plain.read_text(encoding="utf-8")


def release_hashes(release: str) -> dict[str, tuple[str, int]]:
    hashes: dict[str, tuple[str, int]] = {}
    in_sha = False
    for line in release.splitlines():
        if line.startswith("SHA256:"):
            in_sha = True
            continue
        if in_sha:
            if not line.startswith(" "):
                break
            digest, size, name = line.split()
            hashes[name] = (digest, int(size))
    if not hashes:
        raise SystemExit("Release has no SHA256 section")
    return hashes


def parse_packages(text: str) -> list[dict[str, str]]:
    stanzas, cur, last = [], {}, None
    for line in text.splitlines():
        if not line.strip():
            if cur:
                stanzas.append(cur)
            cur, last = {}, None
            continue
        if line.startswith((" ", "\t")) and last:
            cur[last] += "\n" + line.strip()
            continue
        key, _, value = line.partition(":")
        cur[key.strip()] = value.strip()
        last = key.strip()
    if cur:
        stanzas.append(cur)
    return stanzas


def newest(candidates: list[dict[str, str]]) -> dict[str, str]:
    best = candidates[0]
    for cand in candidates[1:]:
        newer = subprocess.run(
            ["dpkg", "--compare-versions", cand["Version"], "gt", best["Version"]]
        ).returncode == 0
        if newer:
            best = cand
    return best


def load_package_lock(path: Path) -> dict:
    """Read a reviewed immutable package identity, not a user-supplied URL."""
    data = json.loads(path.read_text(encoding="utf-8"))
    required = {"package", "architecture", "channel", "version", "filename", "sha256", "size", "source_commit"}
    if not isinstance(data, dict) or set(data) != required:
        raise ValueError("Invalid runtime package lock fields")
    if data["package"] != PACKAGE or data["architecture"] != ARCH or data["channel"] not in {"stable", "canary"}:
        raise ValueError("Wrong package, architecture or channel in runtime lock")
    version = data["version"]
    if not isinstance(version, str) or not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+~_-]*", version):
        raise ValueError("Invalid pinned package version")
    if data["filename"] != f"pool/h/{PACKAGE}_{version}_{ARCH}.deb":
        raise ValueError("Pinned package path does not match its literal identity")
    for field, length in [("sha256", 64), ("source_commit", 40)]:
        value = data[field]
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{" + str(length) + r"}", value):
            raise ValueError(f"Invalid pinned {field}")
    if type(data["size"]) is not int or not 0 < data["size"] < 2**31:
        raise ValueError("Invalid pinned package size")
    return data


def select_package(stanzas, version, channel, package_lock=None):
    candidates = [s for s in stanzas if s.get("Package") == PACKAGE and
                  (not version or s.get("Version") == version)]
    if package_lock is not None:
        if not version or version != package_lock["version"] or channel != package_lock["channel"]:
            raise ValueError("Requested runtime differs from the reviewed package lock")
        pinned = {"Package": PACKAGE, "Architecture": ARCH, "Version": version,
                  "Filename": package_lock["filename"], "SHA256": package_lock["sha256"],
                  "Size": str(package_lock["size"])}
        # A new index must never silently replace a same-version pinned binary.
        for candidate in candidates:
            if any(candidate.get(k) != pinned[k] for k in ("Filename", "SHA256", "Size")):
                raise ValueError("Signed index package identity differs from reviewed lock")
        return pinned, not bool(candidates)
    if not candidates:
        raise ValueError(f"{PACKAGE} {version or ''} not found in hermes-{channel}")
    return (candidates[0] if len(candidates) == 1 else newest(candidates)), False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--channel", default="stable", choices=["stable", "canary"])
    ap.add_argument("--version", default="", help="exact Debian version to pin (default: newest)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--package-lock", type=Path, help="reviewed immutable package identity, including archived pool fallback")
    args = ap.parse_args()
    package_lock = load_package_lock(args.package_lock) if args.package_lock else None

    suite = f"hermes-{args.channel}"
    repo = f"{BASE}/{args.channel}"
    out = args.out.resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    with tempfile.TemporaryDirectory(prefix="hermes-gpg-") as tmp:
        gnupg_home = Path(tmp)
        os.chmod(gnupg_home, 0o700)
        key_path = gnupg_home / "key.asc"
        fetch(f"{repo}/key.asc", key_path)
        keyring = verify_key(key_path, gnupg_home)

        inrelease = gnupg_home / "InRelease"
        fetch(f"{repo}/dists/{suite}/InRelease", inrelease)
        hashes = release_hashes(verified_release(inrelease, keyring, gnupg_home))

    rel_gz = f"{COMPONENT}/binary-{ARCH}/Packages.gz"
    rel_plain = f"{COMPONENT}/binary-{ARCH}/Packages"
    index = rel_gz if rel_gz in hashes else rel_plain if rel_plain in hashes else ""
    if not index:
        raise SystemExit(f"no Packages index for {ARCH} in Release")
    raw = fetch(f"{repo}/dists/{suite}/{index}")
    want, size = hashes[index]
    if hashlib.sha256(raw).hexdigest() != want or len(raw) != size:
        raise SystemExit("Packages index does not match the signed Release")
    text_bytes = gzip.decompress(raw) if index.endswith(".gz") else raw
    log("Packages index hash OK")

    pkg, archived = select_package(parse_packages(text_bytes.decode("utf-8")), args.version, args.channel, package_lock)
    if archived:
        log("Current signed index rotated out this version; using reviewed SHA-256 lock from the same official pool")
    log(f"selected {PACKAGE} {pkg['Version']} ({pkg['Filename']})")

    deb = out / Path(pkg["Filename"]).name
    fetch(f"{repo}/{pkg['Filename']}", deb)
    got = sha256_file(deb)
    if got != pkg["SHA256"].lower() or deb.stat().st_size != int(pkg["Size"]):
        raise SystemExit(f".deb hash/size mismatch: {got}")
    log(".deb SHA-256 OK")

    root = out / "root"
    subprocess.run(["dpkg-deb", "-x", str(deb), str(root)], check=True)
    if package_lock:
        stamp = root / "data/data/com.termux/files/usr/lib/hermes-agent/app/install-stamp.json"
        if json.loads(stamp.read_text(encoding="utf-8")).get("commit") != package_lock["source_commit"]:
            raise ValueError("Extracted runtime commit differs from reviewed package lock")
    meta = {"channel": args.channel, "version": pkg["Version"], "filename": pkg["Filename"],
            "sha256": got, "size": int(pkg["Size"])}
    (out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    deb.unlink()
    log(f"extracted to {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
