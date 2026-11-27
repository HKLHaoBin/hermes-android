#!/usr/bin/env python3
"""Android app-private tool catalog and transactional tool store.

The module deliberately does not execute anything from an installed package. It
validates the signed catalog, verifies artifact bytes, extracts into an isolated
slot, and commits a small JSON selection record atomically. User data lives
outside the store root and is never touched by these operations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import shutil
import stat
import tarfile
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

CATALOG_SCHEMA_VERSION = 1
INVENTORY_SCHEMA_VERSION = 1
TARGET_TRIPLE = "linux-arm64-bionic"
TARGET_ABI = "arm64-v8a"
TARGET_LIBC = "bionic"
TARGET_MIN_SDK = 24
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX40 = re.compile(r"^[0-9A-Fa-f]{40}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9+._-]*$")


class CatalogError(ValueError):
    """The catalog is malformed or not backed by a verified source lock."""


class PreflightError(RuntimeError):
    """A package cannot be installed on this host before any write begins."""


class TransactionError(RuntimeError):
    """An install transaction failed and its active selection was preserved."""


@dataclass(frozen=True)
class Artifact:
    url: str
    filename: str
    sha256: str
    size: int


@dataclass(frozen=True)
class PackageSpec:
    name: str
    version: str
    architecture: str
    min_sdk: int
    dependencies: tuple[str, ...]
    conflicts: tuple[str, ...]
    entrypoints: tuple[str, ...]
    path: tuple[str, ...]
    ld_library_path: tuple[str, ...]
    install_bytes: int
    artifact: Artifact


@dataclass(frozen=True)
class HostFacts:
    abi: str
    api: int
    libc: str
    root: Path
    free_bytes: int
    exec_probe: bool



def _require_safe_rel(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise CatalogError(f"invalid {field}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise CatalogError(f"unsafe {field}")
    return path.as_posix()


def _storage_version(version: str) -> str:
    """Keep package versions readable while making them valid on Windows and POSIX."""
    return "".join(
        character if character.isalnum() or character in ".-_+" else f"%{ord(character):02X}"
        for character in version
    )


def _atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            json.dump(data, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise TransactionError(f"cannot read state {path}: {exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_bytes(root: Path) -> int:
    if not root.exists():
        return 0
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _writable_probe(root: Path) -> Path:
    candidate = root if root.exists() else root.parent
    return candidate if candidate.exists() else Path.cwd()


class Catalog:
    def __init__(self, data: dict[str, Any], packages: dict[str, PackageSpec]):
        self._data = data
        self._packages = packages

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Catalog:
        if not isinstance(data, dict) or data.get("schema_version") != CATALOG_SCHEMA_VERSION:
            raise CatalogError("unsupported catalog schema")
        target = data.get("target")
        if not isinstance(target, dict):
            raise CatalogError("missing target")
        if target.get("triple") != TARGET_TRIPLE:
            raise CatalogError("unsupported target triple")
        if target.get("abi") != TARGET_ABI:
            raise CatalogError("unsupported ABI")
        if target.get("libc") != TARGET_LIBC:
            raise CatalogError("unsupported libc")
        if type(target.get("min_sdk")) is not int or target["min_sdk"] < TARGET_MIN_SDK:
            raise CatalogError("invalid minimum Android API")

        repository = data.get("repository")
        if not isinstance(repository, dict):
            raise CatalogError("missing repository lock")
        for key in ("inrelease_sha256", "packages_index_sha256"):
            if not isinstance(repository.get(key), str) or not _HEX64.fullmatch(repository[key]):
                raise CatalogError(f"invalid repository {key}")
        for key in ("inrelease_size", "packages_index_size"):
            if type(repository.get(key)) is not int or repository[key] <= 0:
                raise CatalogError(f"invalid repository {key}")
        fingerprint = repository.get("key_fingerprint")
        if not isinstance(fingerprint, str) or not _HEX40.fullmatch(fingerprint):
            raise CatalogError("invalid repository key fingerprint")
        signature = repository.get("signature")
        if not isinstance(signature, dict) or signature.get("type") != "openpgp-inrelease":
            raise CatalogError("missing repository signature")
        if signature.get("status") != "verified" or signature.get("verified_by") != "gpgv":
            raise CatalogError("repository signature is not verified")
        base_url = repository.get("base_url")
        base_parts = urlsplit(base_url) if isinstance(base_url, str) else None
        if (
            base_parts is None
            or base_parts.scheme != "https"
            or not base_parts.netloc
            or not base_parts.path.endswith("/")
            or ".." in PurePosixPath(base_parts.path).parts
        ):
            raise CatalogError("invalid repository base URL")
        keyring = repository.get("keyring_artifact")
        if not isinstance(keyring, dict):
            raise CatalogError("missing repository keyring artifact")
        keyring_filename = _require_safe_rel(keyring.get("filename", ""), "keyring filename")
        keyring_hash = keyring.get("sha256")
        keyring_size = keyring.get("size")
        keyring_url = keyring.get("url")
        keyring_parts = urlsplit(keyring_url) if isinstance(keyring_url, str) else None
        if (
            keyring_parts is None
            or keyring_parts.scheme != "https"
            or keyring_parts.netloc != base_parts.netloc
            or not keyring_parts.path.startswith(base_parts.path)
            or keyring_parts.path.rsplit("/", 1)[-1] != keyring_filename
            or not isinstance(keyring_hash, str)
            or not _HEX64.fullmatch(keyring_hash)
            or type(keyring_size) is not int
            or keyring_size <= 0
        ):
            raise CatalogError("invalid repository keyring artifact")

        raw_packages = data.get("packages")
        if not isinstance(raw_packages, list) or not raw_packages:
            raise CatalogError("catalog has no packages")
        packages: dict[str, PackageSpec] = {}
        for raw in raw_packages:
            if not isinstance(raw, dict):
                raise CatalogError("invalid package record")
            name = raw.get("name")
            if not isinstance(name, str) or not _NAME.fullmatch(name) or name in packages:
                raise CatalogError(f"invalid or duplicate package name: {name!r}")
            version = raw.get("version")
            if not isinstance(version, str) or not version or "/" in version or "\\" in version:
                raise CatalogError(f"invalid version for {name}")
            if raw.get("architecture") not in ("aarch64", "all"):
                raise CatalogError(f"unsupported package architecture for {name}")
            min_sdk = raw.get("min_sdk")
            if type(min_sdk) is not int or min_sdk < TARGET_MIN_SDK:
                raise CatalogError(f"invalid package minimum API for {name}")
            dependencies = raw.get("dependencies", [])
            if not isinstance(dependencies, list) or any(
                not isinstance(item, str) or not _NAME.fullmatch(item) for item in dependencies
            ):
                raise CatalogError(f"invalid dependencies for {name}")
            conflicts = raw.get("conflicts", [])
            if not isinstance(conflicts, list) or any(
                not isinstance(item, str) or not _NAME.fullmatch(item) for item in conflicts
            ):
                raise CatalogError(f"invalid conflicts for {name}")
            source = raw.get("source")
            if (
                not isinstance(source, dict)
                or not isinstance(source.get("package"), str)
                or not source["package"]
                or not isinstance(source.get("homepage", ""), str)
            ):
                raise CatalogError(f"invalid source provenance for {name}")
            license_name = raw.get("license")
            if not isinstance(license_name, str) or not license_name:
                raise CatalogError(f"invalid license provenance for {name}")
            entrypoints = raw.get("entrypoints", [])
            if not isinstance(entrypoints, list):
                raise CatalogError(f"invalid entrypoints for {name}")
            safe_entrypoints = tuple(_require_safe_rel(item, f"entrypoint for {name}") for item in entrypoints)
            exports = raw.get("exports", {})
            if not isinstance(exports, dict):
                raise CatalogError(f"invalid exports for {name}")

            export_paths = exports.get("path", [])
            export_ld_paths = exports.get("ld_library_path", [])
            if not isinstance(export_paths, list) or not isinstance(export_ld_paths, list):
                raise CatalogError(f"invalid exports for {name}")
            paths = tuple(_require_safe_rel(item, f"PATH export for {name}") for item in export_paths)
            ld_paths = tuple(_require_safe_rel(item, f"library export for {name}") for item in export_ld_paths)
            install_bytes = raw.get("install_bytes")
            if type(install_bytes) is not int or install_bytes <= 0:
                raise CatalogError(f"invalid install_bytes for {name}")
            artifact = raw.get("artifact")
            if not isinstance(artifact, dict):
                raise CatalogError(f"missing artifact for {name}")
            filename = _require_safe_rel(artifact.get("filename", ""), f"artifact filename for {name}")
            sha256 = artifact.get("sha256")
            if not isinstance(sha256, str) or not _HEX64.fullmatch(sha256):
                raise CatalogError(f"invalid artifact hash for {name}")
            size = artifact.get("size")
            if type(size) is not int or size <= 0:
                raise CatalogError(f"invalid artifact size for {name}")
            url = artifact.get("url")
            if not isinstance(url, str):
                raise CatalogError(f"artifact URL is outside the locked repository for {name}")
            url_parts = urlsplit(url)
            same_repo = (
                url_parts.scheme == "https"
                and url_parts.netloc == base_parts.netloc
                and url_parts.path.startswith(base_parts.path)
            )
            if not same_repo or url.endswith("/"):
                raise CatalogError(f"artifact URL is outside the locked repository for {name}")
            if url.rsplit("/", 1)[-1] != filename:
                raise CatalogError(f"artifact filename does not match URL for {name}")
            packages[name] = PackageSpec(
                name=name,
                version=version,
                architecture=raw["architecture"],
                min_sdk=min_sdk,
                dependencies=tuple(dependencies),
                conflicts=tuple(conflicts),
                entrypoints=safe_entrypoints,
                path=paths,
                ld_library_path=ld_paths,
                install_bytes=install_bytes,
                artifact=Artifact(url, filename, sha256, size),
            )
        missing = sorted({dep for package in packages.values() for dep in package.dependencies if dep not in packages})
        if missing:
            raise CatalogError(f"catalog has missing dependencies: {', '.join(missing)}")
        cycles = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(package_name: str) -> None:
            if package_name in visited:
                return
            if package_name in visiting:
                cycles.append(package_name)
                return
            visiting.add(package_name)
            for dependency in packages[package_name].dependencies:
                visit(dependency)
            visiting.remove(package_name)
            visited.add(package_name)

        for package_name in packages:
            visit(package_name)
        if cycles:
            raise CatalogError(f"catalog has dependency cycle: {cycles[0]}")
        return cls(data, packages)

    @classmethod
    def load(cls, path: Path) -> Catalog:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CatalogError(f"cannot load catalog {path}: {exc}") from exc
        return cls.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._data))

    def package(self, name: str) -> PackageSpec:
        try:
            return self._packages[name]
        except KeyError as exc:
            raise CatalogError(f"unknown package: {name}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._packages))

    @property
    def target(self) -> dict[str, Any]:
        return dict(self._data["target"])

    @property
    def repository(self) -> dict[str, Any]:
        return dict(self._data["repository"])


class ToolchainStore:
    """Immutable package entries with atomic active/previous selections."""

    _lock = threading.RLock()

    def __init__(self, root: Path, catalog: Catalog):
        self.root = Path(root).resolve()
        self.catalog = catalog
        self.entries = self.root / "entries"
        self.staging = self.root / ".staging"
        self.state_path = self.root / "state.json"
        self.facts_path = self.root / "facts.json"

    def _state(self) -> dict[str, Any]:
        state = _read_json(self.state_path, {"schema_version": INVENTORY_SCHEMA_VERSION, "active": {}, "previous": {}})
        if not isinstance(state, dict) or state.get("schema_version") != INVENTORY_SCHEMA_VERSION:
            raise TransactionError("invalid toolchain state schema")
        for key in ("active", "previous"):
            if not isinstance(state.get(key), dict):
                raise TransactionError(f"invalid toolchain state field: {key}")
        return state

    def _active_bytes(self, active: dict[str, Any]) -> int:
        return sum(_tree_bytes(self.root / item["entry"]) for item in active.values() if isinstance(item, dict))

    def _selection_bytes(self, *selections: dict[str, Any]) -> int:
        entries = {
            item["entry"]
            for selection in selections
            for item in selection.values()
            if isinstance(item, dict) and isinstance(item.get("entry"), str)
        }
        return sum(_tree_bytes(self.root / entry) for entry in entries)

    def _required_bytes(self, spec: PackageSpec, active: dict[str, Any], previous: dict[str, Any]) -> int:
        # Active and rollback selections can coexist with the download and staging tree.
        retained = self._selection_bytes(active, previous)
        return spec.artifact.size + spec.install_bytes + retained + 16 * 1024 * 1024

    def _selection_status(
        self,
        name: str,
        selected: Any,
        *,
        require_current_version: bool = True,
    ) -> tuple[bool, str]:
        if not isinstance(selected, dict):
            return False, "selection record is malformed"
        try:
            spec = self.catalog.package(name)
        except CatalogError:
            return False, "package is not present in the current catalog"
        version = selected.get("version")
        entry_name = selected.get("entry")
        if not isinstance(version, str) or not version:
            return False, "selection version is missing"
        if require_current_version and version != spec.version:
            return False, f"active version {version} is outdated; catalog requires {spec.version}"
        if not isinstance(entry_name, str) or not entry_name or "\\" in entry_name:
            return False, "selection entry is unsafe"
        try:
            entry = self._safe_path(self.root, entry_name)
            expected_root = (self.entries / name).resolve()
            if not entry.resolve().is_relative_to(expected_root):
                return False, "selection entry is outside the package slot"
        except (TransactionError, OSError, ValueError) as exc:
            return False, f"selection entry is unsafe: {exc}"
        if not entry.is_dir():
            return False, "active entry is missing"
        manifest_path = entry / ".hermes-package.json"
        try:
            manifest = _read_json(manifest_path, None)
        except TransactionError:
            return False, "package manifest is unreadable"
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            return False, "package manifest is missing or invalid"
        if manifest.get("name") != name or manifest.get("version") != version:
            return False, "package manifest does not match selection"
        manifest_hash = manifest.get("artifact_sha256")
        if not isinstance(manifest_hash, str) or not _HEX64.fullmatch(manifest_hash):
            return False, "package manifest hash is invalid"
        if require_current_version and manifest_hash != spec.artifact.sha256:
            return False, "package manifest hash does not match catalog"
        manifest_entrypoints = manifest.get("entrypoints")
        if not isinstance(manifest_entrypoints, list) or any(not isinstance(item, str) for item in manifest_entrypoints):
            return False, "package manifest entrypoints are invalid"
        if require_current_version and manifest_entrypoints != list(spec.entrypoints):
            return False, "package manifest entrypoints do not match catalog"
        for relative in manifest_entrypoints:
            try:
                path = self._safe_path(entry, relative)
            except TransactionError as exc:
                return False, str(exc)
            if not path.is_file():
                return False, f"declared entrypoint missing: {relative}"
            try:
                # Windows does not preserve POSIX executable mode bits; Android
                # runs on POSIX and enforces the bit during real activation.
                if os.name != "nt" and not (path.stat().st_mode & stat.S_IXUSR):
                    return False, f"declared entrypoint is not executable: {relative}"
            except OSError:
                return False, f"declared entrypoint cannot be inspected: {relative}"
        return True, ""

    def _acquire_transaction(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / ".transaction.lock"
        try:
            lock.mkdir()
        except FileExistsError as exc:
            owner = lock / "pid"
            stale = False
            try:
                pid = int(owner.read_text(encoding="ascii"))
                os.kill(pid, 0)
            except (OSError, ValueError):
                stale = True
            if stale and time.time() - lock.stat().st_mtime > 1:
                shutil.rmtree(lock, ignore_errors=True)
                lock.mkdir()
            else:
                raise TransactionError("another toolchain transaction is active") from exc
        try:
            (lock / "pid").write_text(str(os.getpid()), encoding="ascii")
        except OSError:
            shutil.rmtree(lock, ignore_errors=True)
            raise
        return lock

    @staticmethod
    def _release_transaction(lock: Path) -> None:
        shutil.rmtree(lock, ignore_errors=True)

    def preflight(self, name: str, host: HostFacts, *, abi: str | None = None) -> PackageSpec:
        spec = self.catalog.package(name)
        expected = self.catalog.target
        actual_abi = abi or host.abi
        if actual_abi != expected["abi"]:
            raise PreflightError(f"ABI mismatch: host={actual_abi}, package={expected['abi']}")
        if host.api < max(expected["min_sdk"], spec.min_sdk):
            raise PreflightError(f"Android API {host.api} is below package minimum")
        if host.libc != expected["libc"]:
            raise PreflightError(f"unsupported libc: {host.libc}")
        if not host.exec_probe:
            raise PreflightError("app-private executable probe failed")
        probe = _writable_probe(host.root)
        if not os.access(probe, os.W_OK):
            raise PreflightError("app-private tool root is not writable")
        state = self._state()
        active = state["active"]
        conflicts = [
            package_name
            for package_name in active
            if package_name in spec.conflicts or name in self.catalog.package(package_name).conflicts
        ]
        if conflicts:
            raise PreflightError(f"package conflicts with active package: {', '.join(sorted(conflicts))}")
        missing = [
            dep for dep in spec.dependencies
            if not self._selection_status(dep, active.get(dep))[0]
        ]
        if missing:
            raise PreflightError(f"dependency not installed: {', '.join(missing)}")
        required = self._required_bytes(spec, active, state["previous"])
        if host.free_bytes < required:
            raise PreflightError(f"insufficient space: need {required}, have {host.free_bytes}")
        return spec

    def _verify_artifact(self, spec: PackageSpec, archive: Path) -> None:
        try:
            size = archive.stat().st_size
        except OSError as exc:
            raise TransactionError(f"cannot read package archive: {exc}") from exc
        if size != spec.artifact.size:
            raise TransactionError(f"artifact size mismatch for {spec.name}")
        if _sha256(archive) != spec.artifact.sha256:
            raise TransactionError(f"artifact SHA-256 mismatch for {spec.name}")

    def _download_artifact(self, spec: PackageSpec, destination: Path) -> None:
        request = urllib.request.Request(
            spec.artifact.url,
            headers={"User-Agent": "hermes-android-toolchain/1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response, destination.open("wb") as output:
                if response.geturl() != spec.artifact.url:
                    raise TransactionError(f"artifact redirect differs from catalog for {spec.name}")
                total = 0
                digest = hashlib.sha256()
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > spec.artifact.size:
                        raise TransactionError(f"artifact is larger than catalog for {spec.name}")
                    digest.update(block)
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
        except TransactionError:
            destination.unlink(missing_ok=True)
            raise
        except OSError as exc:
            destination.unlink(missing_ok=True)
            raise TransactionError(f"artifact download failed for {spec.name}: {exc}") from exc
        if total != spec.artifact.size or digest.hexdigest() != spec.artifact.sha256:
            destination.unlink(missing_ok=True)
            raise TransactionError(f"downloaded artifact does not match catalog for {spec.name}")

    def _safe_path(self, root: Path, name: str) -> Path:
        if not isinstance(name, str) or "\\" in name:
            raise TransactionError("unsafe archive path")
        rel = PurePosixPath(name)
        if rel.is_absolute() or ".." in rel.parts:
            raise TransactionError(f"unsafe archive path: {name}")
        target = root.joinpath(*rel.parts)
        if not target.resolve().is_relative_to(root.resolve()):
            raise TransactionError(f"archive path escapes staging root: {name}")
        return target

    def _ensure_parent(self, root: Path, path: Path) -> None:
        relative = path.relative_to(root)
        current = root
        for part in relative.parts[:-1]:
            current /= part
            if current.is_symlink():
                raise TransactionError("archive parent traverses a symlink")
            current.mkdir(exist_ok=True)

    def _extract_tar(self, stream: Any, destination: Path) -> None:
        with tarfile.open(fileobj=stream, mode="r:*") as archive:
            members = archive.getmembers()
            for member in members:
                target = self._safe_path(destination, member.name)
                if member.isdir():
                    self._ensure_parent(destination, target)
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isreg():
                    self._ensure_parent(destination, target)
                    source = archive.extractfile(member)
                    if source is None:
                        raise TransactionError(f"missing archive member: {member.name}")
                    with target.open("wb") as output:
                        shutil.copyfileobj(source, output)
                    os.chmod(target, stat.S_IMODE(member.mode) | stat.S_IXUSR if member.mode & stat.S_IXUSR else stat.S_IMODE(member.mode))
                elif member.issym():
                    target_text = member.linkname
                    member_rel = PurePosixPath(member.name)
                    link_target = target_text
                    if target_text.startswith("/"):
                        absolute_target = PurePosixPath(target_text.lstrip("/"))
                        prefix = PurePosixPath("data/data/com.termux/files/usr")
                        if absolute_target.parts[:len(prefix.parts)] != prefix.parts:
                            raise TransactionError(f"unsafe symlink target: {member.name}")
                        link_target = posixpath.relpath(
                            absolute_target.as_posix(), member_rel.parent.as_posix()
                        )
                    resolved_target = PurePosixPath(
                        posixpath.normpath(str(member_rel.parent / link_target))
                    )
                    if resolved_target.is_absolute() or ".." in resolved_target.parts:
                        raise TransactionError(f"unsafe symlink target: {member.name}")
                    self._ensure_parent(destination, target)
                    target.unlink(missing_ok=True)
                    target.symlink_to(link_target)
                else:
                    raise TransactionError(f"unsupported archive member: {member.name}")

    def _extract_zip(self, archive: zipfile.ZipFile, destination: Path) -> None:
        for member in archive.infolist():
            target = self._safe_path(destination, member.filename)
            if member.is_dir():
                self._ensure_parent(destination, target)
                target.mkdir(parents=True, exist_ok=True)
                continue
            self._ensure_parent(destination, target)
            with archive.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            mode = (member.external_attr >> 16) & 0o777
            if mode:
                os.chmod(target, mode)

    def _extract_deb(self, archive: Path, destination: Path) -> None:
        data = archive.read_bytes()
        if not data.startswith(b"!<arch>\n"):
            raise TransactionError("not a Debian ar archive")
        position = 8
        data_member = None
        while position < len(data):
            header = data[position:position + 60]
            if len(header) != 60 or header[58:60] != b"`\n":
                raise TransactionError("malformed Debian ar archive")
            name = header[:16].decode("ascii", "replace").strip().rstrip("/")
            try:
                size = int(header[48:58].decode("ascii").strip())
            except ValueError as exc:
                raise TransactionError("malformed Debian ar member size") from exc
            member = data[position + 60:position + 60 + size]
            if name.startswith("data.tar"):
                data_member = member
                break
            position += 60 + size + size % 2
        if data_member is None:
            raise TransactionError("Debian package has no data archive")
        self._extract_tar(io_bytes(data_member), destination)

    def _extract(self, archive: Path, destination: Path) -> None:
        suffixes = "".join(archive.suffixes[-2:]).lower()
        if archive.name.endswith(".deb"):
            self._extract_deb(archive, destination)
        elif archive.suffix.lower() == ".zip":
            with zipfile.ZipFile(archive) as compressed:
                self._extract_zip(compressed, destination)
        elif suffixes in {".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst"} or archive.suffix.lower() == ".tar":
            with archive.open("rb") as stream:
                self._extract_tar(stream, destination)
        else:
            raise TransactionError(f"unsupported package archive: {archive.name}")
        self._normalize_termux_prefix(destination)

    def _normalize_termux_prefix(self, destination: Path) -> None:
        prefix = destination / "data" / "data" / "com.termux" / "files" / "usr"
        if not prefix.is_dir():
            return
        normalized = destination / ".normalized"
        if normalized.exists():
            shutil.rmtree(normalized)
        shutil.copytree(prefix, normalized, symlinks=True)
        for item in destination.iterdir():
            if item.name != ".normalized":
                if item.is_dir() and not item.is_symlink():
                    shutil.rmtree(item)
                else:
                    item.unlink()
        for item in normalized.iterdir():
            shutil.move(str(item), destination / item.name)
        normalized.rmdir()

    def _validate_entry(self, spec: PackageSpec, entry: Path) -> None:
        for relative in spec.entrypoints:
            path = self._safe_path(entry, relative)
            if not path.is_file():
                raise TransactionError(f"declared entrypoint missing: {spec.name}/{relative}")
            try:
                mode = path.stat().st_mode
                if not (mode & stat.S_IXUSR):
                    os.chmod(path, mode | stat.S_IXUSR)
            except OSError as exc:
                raise TransactionError(f"cannot make entrypoint executable: {relative}") from exc

    def _write_manifest(self, spec: PackageSpec, entry: Path) -> None:
        _atomic_json(entry / ".hermes-package.json", {
            "schema_version": 1,
            "name": spec.name,
            "version": spec.version,
            "artifact_sha256": spec.artifact.sha256,
            "target": self.catalog.target["triple"],
            "entrypoints": list(spec.entrypoints),
        })

    def _facts(self, active: dict[str, Any]) -> dict[str, Any]:
        rows = {}
        for name, selected in sorted(active.items()):
            spec = self.catalog.package(name)
            rows[name] = {
                "version": selected["version"],
                "entry": selected["entry"],
                "sha256": spec.artifact.sha256,
                "target": self.catalog.target["triple"],
                "path": [f"{selected['entry']}/{item}" for item in spec.path],
                "ld_library_path": [f"{selected['entry']}/{item}" for item in spec.ld_library_path],
                "dependencies": list(spec.dependencies),
                "conflicts": list(spec.conflicts),
            }
        return {"schema_version": INVENTORY_SCHEMA_VERSION, "packages": rows}

    def _commit(self, active: dict[str, Any], previous: dict[str, Any], operation: str) -> None:
        facts = self._facts(active)
        state = {
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "operation": operation,
            "active": active,
            "previous": previous,
            "facts": facts,
        }
        _atomic_json(self.root / "active.json", active)
        _atomic_json(self.root / "previous.json", previous)
        _atomic_json(self.facts_path, facts)
        # state.json is the authoritative commit point; materialized files may lag
        # after process death and are regenerated by the next operation.
        _atomic_json(self.state_path, state)

    def install(self, name: str, archive: Path | None, host: HostFacts) -> dict[str, Any]:
        with self._lock:
            transaction_lock = self._acquire_transaction()
            try:
                # The cross-process lock must cover preflight and commit. Otherwise
                # another writer can change dependencies/conflicts after preflight.
                spec = self.preflight(name, host)
                if archive is not None:
                    self._verify_artifact(spec, Path(archive))
                before = self._state()
                transaction = self.staging / uuid.uuid4().hex
                stage = transaction / "package"
                entry = self.entries / spec.name / _storage_version(spec.version)
                reserved = {
                    selected.get("entry")
                    for selection in (before["active"], before["previous"])
                    for selected in selection.values()
                    if isinstance(selected, dict)
                }
                if entry.exists() and entry.relative_to(self.root).as_posix() in reserved:
                    entry = self.entries / spec.name / f"{_storage_version(spec.version)}-{uuid.uuid4().hex[:12]}"
                try:
                    stage.mkdir(parents=True)
                    source = Path(archive) if archive is not None else transaction / spec.artifact.filename
                    if archive is None:
                        self._download_artifact(spec, source)
                    self._verify_artifact(spec, source)
                    self._extract(source, stage)
                    self._validate_entry(spec, stage)
                    self._write_manifest(spec, stage)
                    entry.parent.mkdir(parents=True, exist_ok=True)
                    if entry.exists():
                        shutil.rmtree(entry)
                    os.replace(stage, entry)
                    active = dict(before["active"])
                    active[spec.name] = {
                        "version": spec.version,
                        "entry": entry.relative_to(self.root).as_posix(),
                    }
                    self._commit(active, before["active"], "install")
                    return self.inventory(host)
                except (CatalogError, PreflightError, TransactionError):
                    raise
                except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError) as exc:
                    raise TransactionError(f"install failed for {name}: {exc}") from exc
                finally:
                    shutil.rmtree(transaction, ignore_errors=True)
            finally:
                self._release_transaction(transaction_lock)

    def update(self, name: str, archive: Path | None, host: HostFacts) -> dict[str, Any]:
        return self.install(name, archive, host)

    def install_closure(
        self,
        names: Iterable[str],
        host: HostFacts,
        archives: dict[str, Path] | None = None,
    ) -> dict[str, Any]:
        order: list[str] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(package_name: str) -> None:
            if package_name in visited:
                return
            if package_name in visiting:
                raise TransactionError(f"dependency cycle involving {package_name}")
            spec = self.catalog.package(package_name)
            visiting.add(package_name)
            for dependency in spec.dependencies:
                visit(dependency)
            visiting.remove(package_name)
            visited.add(package_name)
            order.append(package_name)

        requested = list(names)
        if not requested:
            raise TransactionError("at least one package is required")
        for package_name in requested:
            visit(package_name)
        result = self.inventory(host)
        for package_name in order:
            active = self._state()["active"].get(package_name)
            valid, _ = self._selection_status(package_name, active)
            if valid:
                continue
            archive = archives.get(package_name) if archives else None
            result = self.install(package_name, archive, host)
        return result

    def remove(self, name: str) -> dict[str, Any]:
        with self._lock:
            transaction_lock = self._acquire_transaction()
            try:
                before = self._state()
                if name not in before["active"]:
                    raise TransactionError(f"package is not active: {name}")
                active = dict(before["active"])
                dependents = [
                    package_name
                    for package_name in active
                    if package_name != name and name in self.catalog.package(package_name).dependencies
                ]
                if dependents:
                    raise TransactionError(f"package is required by active packages: {', '.join(sorted(dependents))}")
                remove = {name}
                changed = True
                while changed:
                    changed = False
                    for removed_name in tuple(remove):
                        for dependency in self.catalog.package(removed_name).dependencies:
                            if dependency not in active or dependency in remove:
                                continue
                            other_users = [
                                other for other in active
                                if other not in remove and dependency in self.catalog.package(other).dependencies
                            ]
                            if not other_users:
                                remove.add(dependency)
                                changed = True
                for package_name in remove:
                    active.pop(package_name, None)
                self._commit(active, before["active"], "remove")
                return {"active": active, "removed": sorted(remove)}
            finally:
                self._release_transaction(transaction_lock)

    def rollback(self) -> dict[str, Any]:
        with self._lock:
            transaction_lock = self._acquire_transaction()
            try:
                state = self._state()
                previous = state["previous"]
                if not previous:
                    raise TransactionError("no rollback selection is available")
                for name, selected in previous.items():
                    valid, reason = self._selection_status(name, selected, require_current_version=False)
                    if not valid:
                        raise TransactionError(f"rollback entry is invalid for {name}: {reason}")
                self._commit(previous, state["active"], "rollback")
                return {"active": previous, "previous": state["active"]}
            finally:
                self._release_transaction(transaction_lock)

    def inventory(self, host: HostFacts) -> dict[str, Any]:
        state = self._state()
        active = state["active"]
        rows = []
        for name in self.catalog.names():
            spec = self.catalog.package(name)
            reason = ""
            if name in active:
                valid, reason = self._selection_status(name, active[name])
                if valid:
                    status = "active"
                elif "outdated" in reason:
                    status = "outdated"
                else:
                    status = "corrupt"
            elif host.abi != self.catalog.target["abi"]:
                status, reason = "unsupported", "ABI mismatch"
            elif host.api < max(self.catalog.target["min_sdk"], spec.min_sdk):
                status, reason = "unsupported", "Android API below package minimum"
            elif host.libc != self.catalog.target["libc"]:
                status, reason = "unsupported", "libc mismatch"
            else:
                missing = [
                    dep for dep in spec.dependencies
                    if not self._selection_status(dep, active.get(dep))[0]
                ]
                conflicts = [
                    package_name
                    for package_name in active
                    if package_name in spec.conflicts or name in self.catalog.package(package_name).conflicts
                ]
                status = "blocked" if missing or conflicts else "available"
                if missing:
                    reason = f"dependency not installed: {', '.join(missing)}"
                elif conflicts:
                    reason = f"conflicts with active package: {', '.join(sorted(conflicts))}"
            rows.append({
                "name": name,
                "version": spec.version,
                "state": status,
                "reason": reason,
                "dependencies": list(spec.dependencies),
                "conflicts": list(spec.conflicts),
                "sha256": spec.artifact.sha256,
            })
        return {
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "catalog_schema_version": CATALOG_SCHEMA_VERSION,
            "target": self.catalog.target,
            "repository": self.catalog.repository,
            "root": str(self.root),
            "active": active,
            "previous": state["previous"],
            "packages": rows,
        }


def io_bytes(data: bytes):
    import io
    return io.BytesIO(data)


def _cli_catalog(path: str) -> Catalog:
    return Catalog.load(Path(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="android-toolchain")
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--abi", default=TARGET_ABI)
    parser.add_argument("--api", type=int, default=35)
    parser.add_argument("--libc", default=TARGET_LIBC)
    parser.add_argument("--free-bytes", type=int, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("inventory")
    preflight = sub.add_parser("preflight")
    preflight.add_argument("name")
    for command in ("install", "update"):
        command_parser = sub.add_parser(command)
        command_parser.add_argument("name")
        command_parser.add_argument("--archive", type=Path)
    closure = sub.add_parser("install-closure")
    closure.add_argument("names", nargs="+")
    remove = sub.add_parser("remove")
    remove.add_argument("name")
    sub.add_parser("rollback")
    args = parser.parse_args(argv)
    catalog = _cli_catalog(args.catalog)
    root = Path(args.root)
    store = ToolchainStore(root, catalog)
    probe = _writable_probe(root)
    free_bytes = args.free_bytes if args.free_bytes is not None else shutil.disk_usage(probe).free
    host = HostFacts(args.abi, args.api, args.libc, root, free_bytes, os.access(probe, os.X_OK))
    try:
        if args.command == "inventory":
            result = store.inventory(host)
        elif args.command == "preflight":
            spec = store.preflight(args.name, host)
            result = {
                "ok": True,
                "package": spec.name,
                "version": spec.version,
                "required_bytes": store._required_bytes(
                    spec, store._state()["active"], store._state()["previous"]
                ),
            }
        elif args.command in {"install", "update"}:
            result = getattr(store, args.command)(args.name, args.archive, host)
        elif args.command == "install-closure":
            result = store.install_closure(args.names, host)
        elif args.command == "remove":
            result = store.remove(args.name)
        else:
            result = store.rollback()
    except (CatalogError, PreflightError, TransactionError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
