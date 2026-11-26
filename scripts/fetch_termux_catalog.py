#!/usr/bin/env python3
"""Build a verified Android ARM64 catalog from the Termux main repository.

CI runs this after fetching the locked source index. The generated catalog is
embedded in the signed APK; package installation later verifies each archive's
size and SHA-256 against this catalog. No floating "latest" package selection
is allowed.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

TARGET_ABI = "arm64-v8a"
TARGET_TRIPLE = "linux-arm64-bionic"
TARGET_LIBC = "bionic"
MIN_SDK = 24
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX40 = re.compile(r"^[0-9A-Fa-f]{40}$")


class CatalogBuildError(RuntimeError):
    """The signed source lock or package index cannot be used."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "hermes-android-toolchain/1"})
    last: OSError | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return response.read()
        except OSError as exc:
            last = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise CatalogBuildError(f"download failed: {url}: {last}") from last


def _verify_bytes(label: str, data: bytes, expected_sha256: str, expected_size: int | None = None) -> None:
    if expected_size is not None and len(data) != expected_size:
        raise CatalogBuildError(f"{label} size mismatch")
    if _sha256_bytes(data) != expected_sha256:
        raise CatalogBuildError(f"{label} SHA-256 mismatch")


def parse_packages(text: str) -> dict[str, dict[str, str]]:
    packages: dict[str, dict[str, str]] = {}
    current: dict[str, str] = {}
    last: str | None = None
    for line in text.splitlines():
        if not line.strip():
            if current.get("Package"):
                packages[current["Package"]] = current
            current, last = {}, None
            continue
        if line.startswith((" ", "\t")) and last:
            current[last] = current.get(last, "") + "\n" + line.strip()
            continue
        key, _, value = line.partition(":")
        current[key.strip()] = value.strip()
        last = key.strip()
    if current.get("Package"):
        packages[current["Package"]] = current
    return packages


def dependency_names(value: str | None) -> list[str]:
    if not value:
        return []
    result: list[str] = []
    for item in value.split(","):
        alternative = item.strip().split("|", 1)[0].strip()
        alternative = re.sub(r"\s*\([^)]*\)", "", alternative)
        alternative = re.sub(r":(?:any|native)$", "", alternative)
        alternative = re.sub(r"\s*\[[^]]*\]", "", alternative)
        if alternative and re.fullmatch(r"[A-Za-z0-9+.-]+", alternative):
            result.append(alternative)
    return result


def unconditional_names(value: str | None) -> list[str]:
    """Return only unconditional conflict names; versioned conflicts need comparison."""
    if not value:
        return []
    return dependency_names(",".join(item for item in value.split(",") if "(" not in item))


def resolve_dependency_closure(packages: dict[str, dict[str, str]], roots: list[str]) -> list[str]:
    result: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise CatalogBuildError(f"dependency cycle involving {name}")
        if name not in packages:
            raise CatalogBuildError(f"missing package in signed index: {name}")
        visiting.add(name)
        result.append(name)
        for dependency in dependency_names(packages[name].get("Depends")):
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for root in roots:
        visit(root)
    return result


def validate_source_lock(lock: dict[str, Any], observed: dict[str, Any]) -> None:
    repository = lock.get("repository")
    if not isinstance(repository, dict):
        raise CatalogBuildError("missing repository lock")
    if not observed.get("signature_verified"):
        raise CatalogBuildError("Termux InRelease signature is not verified")
    if observed.get("key_fingerprint", repository.get("key_fingerprint")).upper() != str(repository.get("key_fingerprint", "")).upper():
        raise CatalogBuildError("Termux signing key fingerprint mismatch")
    if observed.get("inrelease_sha256") != repository.get("inrelease_sha256"):
        raise CatalogBuildError("InRelease SHA-256 differs from lock")
    if observed.get("packages_index_sha256") != repository.get("packages_index_sha256"):
        raise CatalogBuildError("Packages index SHA-256 differs from lock")
    for key in ("inrelease_sha256", "packages_index_sha256"):
        if not isinstance(repository.get(key), str) or not _HEX64.fullmatch(repository[key]):
            raise CatalogBuildError(f"invalid locked {key}")
    if not isinstance(repository.get("key_fingerprint"), str) or not _HEX40.fullmatch(repository["key_fingerprint"]):
        raise CatalogBuildError("invalid locked key fingerprint")


def _entrypoints(name: str) -> list[str]:
    known = {
        "bash": ["bin/bash"],
        "git": ["bin/git"],
        "curl": ["bin/curl"],
        "wget": ["bin/wget"],
        "tar": ["bin/tar"],
        "unzip": ["bin/unzip"],
        "coreutils": ["bin/ls"],
        "findutils": ["bin/find"],
        "termux-tools": ["bin/termux-info"],
    }
    return known.get(name, [])


def build_catalog(packages: dict[str, dict[str, str]], source: dict[str, Any], roots: list[str]) -> dict[str, Any]:
    closure = resolve_dependency_closure(packages, roots)
    repository = {
        "name": source["name"],
        "base_url": source["base_url"],
        "suite": source["suite"],
        "component": source["component"],
        "architecture": source["architecture"],
        "inrelease_sha256": source["inrelease_sha256"],
        "inrelease_size": source.get("inrelease_size"),
        "packages_index_sha256": source["packages_index_sha256"],
        "packages_index_size": source.get("packages_index_size"),
        "key_fingerprint": source["key_fingerprint"].upper(),
        "keyring_artifact": source.get("keyring_artifact", {}),
        "signature": {
            "type": "openpgp-inrelease",
            "status": "verified",
            "verified_by": "gpgv",
        },
    }
    records = []
    for name in closure:
        row = packages[name]
        filename = row.get("Filename", "")
        if not filename.startswith("pool/") or ".." in Path(filename).parts:
            raise CatalogBuildError(f"unsafe package filename: {filename}")
        try:
            size = int(row["Size"])
            installed_bytes = int(row.get("Installed-Size", "0")) * 1024
        except ValueError as exc:
            raise CatalogBuildError(f"invalid size for package {name}") from exc
        if size <= 0:
            raise CatalogBuildError(f"invalid artifact size for package {name}")
        if installed_bytes <= 0:
            installed_bytes = size
        sha256 = row.get("SHA256", "")
        if not _HEX64.fullmatch(sha256):
            raise CatalogBuildError(f"invalid package hash for {name}")
        dependencies = dependency_names(row.get("Depends"))
        records.append({
            "name": name,
            "version": row.get("Version", ""),
            "architecture": row.get("Architecture", ""),
            "min_sdk": MIN_SDK,
            "dependencies": dependencies,
            "conflicts": unconditional_names(row.get("Conflicts")),
            "source": {
                "package": row.get("Source", name),
                "homepage": row.get("Homepage", ""),
            },
            "license": row.get("License", "unknown"),
            "entrypoints": _entrypoints(name),
            "exports": {"path": ["bin"], "ld_library_path": ["lib"]},
            "install_bytes": installed_bytes,
            "artifact": {
                "url": source["base_url"] + filename,
                "filename": filename.rsplit("/", 1)[-1],
                "sha256": sha256,
                "size": size,
            },
        })
    return {
        "schema_version": 1,
        "catalog_id": f"termux-main-aarch64-{source['inrelease_sha256'][:12]}",
        "target": {
            "triple": TARGET_TRIPLE,
            "abi": TARGET_ABI,
            "min_sdk": MIN_SDK,
            "libc": TARGET_LIBC,
        },
        "repository": repository,
        "packages": records,
    }


def _parse_release_packages_hash(inrelease: bytes) -> str:
    text = inrelease.decode("utf-8", "replace")
    in_sha256 = False
    for line in text.splitlines():
        if line == "SHA256:":
            in_sha256 = True
            continue
        if in_sha256 and line and not line.startswith(" "):
            break
        parts = line.split()
        if in_sha256 and len(parts) == 3 and parts[2] == "main/binary-aarch64/Packages.gz":
            return parts[0]
    raise CatalogBuildError("InRelease has no aarch64 Packages.gz hash")


def _ar_members(data: bytes) -> list[tuple[str, bytes]]:
    if not data.startswith(b"!<arch>\n"):
        raise CatalogBuildError("Termux keyring artifact is not an ar archive")
    members = []
    position = 8
    while position < len(data):
        header = data[position:position + 60]
        if len(header) != 60 or header[58:60] != b"`\n":
            raise CatalogBuildError("malformed Termux keyring ar archive")
        name = header[:16].decode("ascii", "replace").strip().rstrip("/")
        try:
            size = int(header[48:58].decode("ascii").strip())
        except ValueError as exc:
            raise CatalogBuildError("malformed Termux keyring member size") from exc
        body = data[position + 60:position + 60 + size]
        members.append((name, body))
        position += 60 + size + size % 2
    return members


def _extract_termux_keyring(deb: bytes, destination: Path) -> Path:
    data_tar = next((body for name, body in _ar_members(deb) if name.startswith("data.tar")), None)
    if data_tar is None:
        raise CatalogBuildError("Termux keyring has no data archive")
    with tarfile.open(fileobj=io_bytes(data_tar), mode="r:*") as archive:
        member = next((item for item in archive.getmembers() if item.isfile() and item.name.endswith("termux-autobuilds.gpg")), None)
        if member is None:
            raise CatalogBuildError("Termux keyring has no termux-autobuilds key")
        keyring = destination / "termux-autobuilds.gpg"
        source = archive.extractfile(member)
        if source is None:
            raise CatalogBuildError("cannot read Termux keyring member")
        keyring.write_bytes(source.read())
        return keyring


def _gpg_path(path: Path) -> str:
    if os.name == "nt":
        value = str(path).replace("\\", "/")
        if len(value) > 2 and value[1] == ":":
            return "/" + value[0].lower() + value[2:]
        return value
    return str(path)


def verify_source(lock: dict[str, Any], work: Path, gpgv: str = "gpgv", gpg: str = "gpg") -> tuple[bytes, bytes, dict[str, Any]]:
    repository = lock["repository"]
    inrelease = _fetch(repository["inrelease_url"])
    packages_gz = _fetch(repository["packages_index_url"])
    keyring_deb = _fetch(repository["keyring_artifact"]["url"])
    _verify_bytes("InRelease", inrelease, repository["inrelease_sha256"], repository["inrelease_size"])
    _verify_bytes("Packages.gz", packages_gz, repository["packages_index_sha256"], repository["packages_index_size"])
    _verify_bytes("Termux keyring", keyring_deb, repository["keyring_artifact"]["sha256"], repository["keyring_artifact"]["size"])
    if _parse_release_packages_hash(inrelease) != repository["packages_index_sha256"]:
        raise CatalogBuildError("InRelease Packages.gz hash differs from lock")
    keyring = _extract_termux_keyring(keyring_deb, work)
    fingerprint = subprocess.run(
        [gpg, "--batch", "--with-colons", "--show-keys", _gpg_path(keyring)],
        check=True, capture_output=True, text=True,
    ).stdout
    if repository["key_fingerprint"].upper() not in fingerprint.upper():
        raise CatalogBuildError("Termux keyring fingerprint differs from lock")
    (work / "InRelease").write_bytes(inrelease)
    subprocess.run(
        [gpgv, "--keyring", _gpg_path(keyring), _gpg_path(work / "InRelease")],
        check=True, capture_output=True, text=True,
    )
    observed = {
        "signature_verified": True,
        "key_fingerprint": repository["key_fingerprint"],
        "inrelease_sha256": _sha256_bytes(inrelease),
        "packages_index_sha256": _sha256_bytes(packages_gz),
    }
    validate_source_lock(lock, observed)
    return inrelease, gzip.decompress(packages_gz), observed


def io_bytes(data: bytes):
    import io
    return io.BytesIO(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--gpgv", default="gpgv")
    parser.add_argument("--gpg", default="gpg")
    args = parser.parse_args(argv)
    lock = json.loads(args.lock.read_text(encoding="utf-8"))
    try:
        with tempfile.TemporaryDirectory(prefix="termux-catalog-", dir=os.environ.get("TMPDIR")) as directory:
            work = Path(directory)
            _, packages_bytes, observed = verify_source(lock, work, args.gpgv, args.gpg)
            source = dict(lock["repository"], **observed)
            catalog = build_catalog(parse_packages(packages_bytes.decode("utf-8")), source, lock["roots"])
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (CatalogBuildError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"termux catalog: {exc}")
        return 2
    print(f"termux catalog: wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
