"""Contract and transaction tests for the Android app-private tool store."""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from android_toolchain import (
    Catalog,
    CatalogError,
    HostFacts,
    PreflightError,
    ToolchainStore,
    TransactionError,
)


class AndroidToolchainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.root = Path(self.temp.name)
        self.user = self.root / "home"
        self.user.mkdir()
        self.user_state = self.user / "profile.json"
        self.user_state.write_text('{"keep": true}\n', encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, name: str, version: str, body: str = "ok") -> tuple[Path, str, int]:
        path = self.root / f"{name}-{version}.zip"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("bin/" + name, f"#!/system/bin/sh\necho {body}\n")
            archive.writestr("share/" + name + ".txt", body)
        data = path.read_bytes()
        return path, hashlib.sha256(data).hexdigest(), len(data)

    class Response:
        def __init__(self, data: bytes, url: str):
            self.stream = __import__("io").BytesIO(data)
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.stream.close()

        def geturl(self):
            return self.url

        def read(self, size=-1):
            return self.stream.read(size)

    def catalog(self, tool_artifacts: dict[str, tuple[str, int]], *, tool_version="1.0.0") -> Catalog:
        packages = [
            {
                "name": "libfoo",
                "version": "1.0.0",
                "architecture": "aarch64",
                "min_sdk": 24,
                "dependencies": [],
                "source": {"package": "libfoo", "homepage": "https://packages.invalid/libfoo"},
                "license": "MIT",
                "entrypoints": [],
                "exports": {"path": ["lib"], "ld_library_path": ["lib"]},
                "install_bytes": 80,
                "artifact": {
                    "url": "https://packages.invalid/libfoo_1.0.0.zip",
                    "filename": "libfoo_1.0.0.zip",
                    "sha256": tool_artifacts["libfoo"][0],
                    "size": tool_artifacts["libfoo"][1],
                },
            },
            {
                "name": "tool",
                "version": tool_version,
                "architecture": "aarch64",
                "min_sdk": 24,
                "dependencies": ["libfoo"],
                "source": {"package": "tool", "homepage": "https://packages.invalid/tool"},
                "license": "MIT",
                "entrypoints": ["bin/tool"],
                "exports": {"path": ["bin"], "ld_library_path": []},
                "install_bytes": 120,
                "artifact": {
                    "url": f"https://packages.invalid/tool_{tool_version}.zip",
                    "filename": f"tool_{tool_version}.zip",
                    "sha256": tool_artifacts["tool"][0],
                    "size": tool_artifacts["tool"][1],
                },
            },
        ]
        return Catalog.from_dict({
            "schema_version": 1,
            "catalog_id": "test-catalog",
            "target": {
                "triple": "linux-arm64-bionic",
                "abi": "arm64-v8a",
                "min_sdk": 24,
                "libc": "bionic",
            },
            "repository": {
                "name": "termux-main",
                "base_url": "https://packages.invalid/",
                "suite": "stable",
                "component": "main",
                "architecture": "aarch64",
                "inrelease_sha256": "a" * 64,
                "inrelease_size": 1,
                "packages_index_sha256": "b" * 64,
                "packages_index_size": 1,
                "key_fingerprint": "c" * 40,
                "keyring_artifact": {
                    "url": "https://packages.invalid/keyring.deb",
                    "filename": "keyring.deb",
                    "sha256": "d" * 64,
                    "size": 1,
                },
                "signature": {
                    "type": "openpgp-inrelease",
                    "status": "verified",
                    "verified_by": "gpgv",
                },
            },
            "packages": packages,
        })

    def host(self, free_bytes=100_000_000) -> HostFacts:
        return HostFacts(
            abi="arm64-v8a",
            api=35,
            libc="bionic",
            root=self.root / "tools",
            free_bytes=free_bytes,
            exec_probe=True,
        )

    def test_catalog_rejects_unsigned_or_wrong_target(self):
        _lib, lib_hash, lib_size = self.archive("libfoo", "1.0.0")
        _tool, tool_hash, tool_size = self.archive("tool", "1.0.0")
        catalog = self.catalog({
            "libfoo": (lib_hash, lib_size),
            "tool": (tool_hash, tool_size),
        })
        doc = catalog.to_dict()
        doc["repository"]["signature"]["status"] = "missing"
        with self.assertRaisesRegex(CatalogError, "signature"):
            Catalog.from_dict(doc)

        doc = catalog.to_dict()
        doc["target"]["abi"] = "armeabi-v7a"
        with self.assertRaisesRegex(CatalogError, "ABI"):
            Catalog.from_dict(doc)

        doc = catalog.to_dict()
        doc["packages"][0]["artifact"]["url"] = "https://packages.invalid.evil/libfoo_1.0.0.zip"
        with self.assertRaisesRegex(CatalogError, "outside"):
            Catalog.from_dict(doc)

        doc = catalog.to_dict()
        doc["packages"][0].pop("source")
        with self.assertRaisesRegex(CatalogError, "source"):
            Catalog.from_dict(doc)

        doc = catalog.to_dict()
        doc["repository"].pop("keyring_artifact")
        with self.assertRaisesRegex(CatalogError, "keyring"):
            Catalog.from_dict(doc)

    def test_preflight_rejects_abi_dependency_and_space_before_write(self):
        _lib, lib_hash, lib_size = self.archive("libfoo", "1.0.0")
        _tool, tool_hash, tool_size = self.archive("tool", "1.0.0")
        catalog = self.catalog({
            "libfoo": (lib_hash, lib_size),
            "tool": (tool_hash, tool_size),
        })
        store = ToolchainStore(self.root / "tools", catalog)
        with self.assertRaisesRegex(PreflightError, "ABI"):
            store.preflight("tool", self.host(), abi="armeabi-v7a")
        with self.assertRaisesRegex(PreflightError, "dependency"):
            store.preflight("tool", self.host())
        with self.assertRaisesRegex(PreflightError, "space"):
            store.preflight("libfoo", self.host(free_bytes=1))
        self.assertFalse((self.root / "tools").exists())

    def test_preflight_rejects_catalog_conflict_before_write(self):
        lib, lib_hash, lib_size = self.archive("libfoo", "1.0.0")
        _tool, tool_hash, tool_size = self.archive("tool", "1.0.0")
        doc = self.catalog({
            "libfoo": (lib_hash, lib_size),
            "tool": (tool_hash, tool_size),
        }).to_dict()
        doc["packages"][1]["conflicts"] = ["libfoo"]
        catalog = Catalog.from_dict(doc)
        store = ToolchainStore(self.root / "tools", catalog)
        store.install("libfoo", lib, self.host())
        with self.assertRaisesRegex(PreflightError, "conflicts"):
            store.preflight("tool", self.host())

    def test_install_update_and_rollback_preserve_user_data(self):
        lib, lib_hash, lib_size = self.archive("libfoo", "1.0.0")
        tool_v1, tool_hash_v1, tool_size_v1 = self.archive("tool", "1.0.0", "v1")
        catalog = self.catalog({
            "libfoo": (lib_hash, lib_size),
            "tool": (tool_hash_v1, tool_size_v1),
        })
        store = ToolchainStore(self.root / "tools", catalog)
        store.install("libfoo", lib, self.host())
        store.install("tool", tool_v1, self.host())
        first = store.inventory(self.host())
        self.assertEqual(first["active"]["tool"]["version"], "1.0.0")
        self.assertEqual(self.user_state.read_text(encoding="utf-8"), '{"keep": true}\n')

        tool_v2, tool_hash_v2, tool_size_v2 = self.archive("tool", "2.0.0", "v2")
        updated_doc = catalog.to_dict()
        updated_doc["packages"][1]["version"] = "2.0.0"
        updated_doc["packages"][1]["artifact"].update({
            "url": "https://packages.invalid/tool_2.0.0.zip",
            "filename": "tool_2.0.0.zip",
            "sha256": tool_hash_v2,
            "size": tool_size_v2,
        })
        store = ToolchainStore(self.root / "tools", Catalog.from_dict(updated_doc))
        store.install("tool", tool_v2, self.host())
        self.assertEqual(store.inventory(self.host())["active"]["tool"]["version"], "2.0.0")
        self.assertTrue((self.root / "tools" / "entries" / "tool" / "1.0.0").is_dir())
        store.rollback()
        self.assertEqual(store.inventory(self.host())["active"]["tool"]["version"], "1.0.0")
        self.assertEqual(self.user_state.read_text(encoding="utf-8"), '{"keep": true}\n')

    def test_remove_cascades_unused_dependencies_but_keeps_shared_dependency(self):
        lib, lib_hash, lib_size = self.archive("libfoo", "1.0.0")
        tool, tool_hash, tool_size = self.archive("tool", "1.0.0")
        catalog = self.catalog({
            "libfoo": (lib_hash, lib_size),
            "tool": (tool_hash, tool_size),
        })
        store = ToolchainStore(self.root / "tools", catalog)
        store.install("libfoo", lib, self.host())
        store.install("tool", tool, self.host())
        store.remove("tool")
        inventory = store.inventory(self.host())
        self.assertEqual(inventory["active"], {})
        self.assertTrue((self.root / "tools" / "entries" / "libfoo" / "1.0.0").is_dir())
        self.assertTrue((self.root / "tools" / "entries" / "tool" / "1.0.0").is_dir())

    def test_reinstall_does_not_destroy_active_or_previous_entry(self):
        lib, lib_hash, lib_size = self.archive("libfoo", "1.0.0")
        _tool, tool_hash, tool_size = self.archive("tool", "1.0.0")
        catalog = self.catalog({
            "libfoo": (lib_hash, lib_size),
            "tool": (tool_hash, tool_size),
        })
        store = ToolchainStore(self.root / "tools", catalog)
        store.install("libfoo", lib, self.host())
        first = store.inventory(self.host())["active"]["libfoo"]["entry"]
        store.install("libfoo", lib, self.host())
        current = store.inventory(self.host())
        previous = current["previous"]["libfoo"]["entry"]
        self.assertNotEqual(current["active"]["libfoo"]["entry"], previous)
        self.assertTrue((self.root / "tools" / previous).is_dir())
        self.assertTrue((self.root / "tools" / first).is_dir())

    def test_hash_or_archive_failure_never_changes_active_state(self):
        lib, lib_hash, lib_size = self.archive("libfoo", "1.0.0")
        _tool, tool_hash, tool_size = self.archive("tool", "1.0.0")
        catalog = self.catalog({
            "libfoo": (lib_hash, lib_size),
            "tool": (tool_hash, tool_size),
        })
        store = ToolchainStore(self.root / "tools", catalog)
        store.install("libfoo", lib, self.host())
        bad = self.root / "bad.zip"
        bad.write_bytes(b"not an archive")
        with self.assertRaises((TransactionError, ValueError)):
            store.install("tool", bad, self.host())
        self.assertEqual(
            store.inventory(self.host())["active"],
            {"libfoo": {"entry": "entries/libfoo/1.0.0", "version": "1.0.0"}},
        )
        self.assertEqual(self.user_state.read_text(encoding="utf-8"), '{"keep": true}\n')

    def test_install_without_archive_downloads_only_the_locked_artifact(self):
        lib, lib_hash, lib_size = self.archive("libfoo", "1.0.0")
        _tool, tool_hash, tool_size = self.archive("tool", "1.0.0")
        catalog = self.catalog({
            "libfoo": (lib_hash, lib_size),
            "tool": (tool_hash, tool_size),
        })
        store = ToolchainStore(self.root / "tools", catalog)
        with patch("android_toolchain.urllib.request.urlopen", return_value=self.Response(
            lib.read_bytes(), "https://packages.invalid/libfoo_1.0.0.zip"
        )):
            store.install("libfoo", None, self.host())
        self.assertEqual(store.inventory(self.host())["active"]["libfoo"]["version"], "1.0.0")

    def test_install_closure_orders_dependencies_before_requested_tool(self):
        lib, lib_hash, lib_size = self.archive("libfoo", "1.0.0")
        tool, tool_hash, tool_size = self.archive("tool", "1.0.0")
        catalog = self.catalog({
            "libfoo": (lib_hash, lib_size),
            "tool": (tool_hash, tool_size),
        })
        store = ToolchainStore(self.root / "tools", catalog)
        result = store.install_closure(
            ["tool"], self.host(), {"libfoo": lib, "tool": tool}
        )
        self.assertEqual(result["active"]["libfoo"]["version"], "1.0.0")
        self.assertEqual(result["active"]["tool"]["version"], "1.0.0")

    def test_inventory_marks_old_active_version_and_install_closure_refreshes_it(self):
        lib, lib_hash, lib_size = self.archive("libfoo", "1.0.0")
        _tool, tool_hash, tool_size = self.archive("tool", "1.0.0")
        catalog = self.catalog({
            "libfoo": (lib_hash, lib_size),
            "tool": (tool_hash, tool_size),
        })
        store = ToolchainStore(self.root / "tools", catalog)
        store.install("libfoo", lib, self.host())

        updated_doc = catalog.to_dict()
        updated_doc["packages"][0]["version"] = "2.0.0"
        updated_catalog = Catalog.from_dict(updated_doc)
        updated_store = ToolchainStore(self.root / "tools", updated_catalog)
        row = next(item for item in updated_store.inventory(self.host())["packages"] if item["name"] == "libfoo")
        self.assertEqual(row["state"], "outdated")
        refreshed = updated_store.install_closure(["libfoo"], self.host(), {"libfoo": lib})
        self.assertEqual(refreshed["active"]["libfoo"]["version"], "2.0.0")

    def test_inventory_marks_deleted_entrypoint_and_install_closure_reinstalls_it(self):
        lib, lib_hash, lib_size = self.archive("libfoo", "1.0.0")
        tool, tool_hash, tool_size = self.archive("tool", "1.0.0")
        catalog = self.catalog({
            "libfoo": (lib_hash, lib_size),
            "tool": (tool_hash, tool_size),
        })
        store = ToolchainStore(self.root / "tools", catalog)
        store.install("libfoo", lib, self.host())
        store.install("tool", tool, self.host())
        entry = Path(self.root / "tools" / store.inventory(self.host())["active"]["tool"]["entry"])
        (entry / "bin/tool").unlink()
        row = next(item for item in store.inventory(self.host())["packages"] if item["name"] == "tool")
        self.assertEqual(row["state"], "corrupt")
        refreshed = store.install_closure(["tool"], self.host(), {"tool": tool})
        self.assertEqual(refreshed["active"]["tool"]["version"], "1.0.0")
        self.assertEqual(next(item for item in refreshed["packages"] if item["name"] == "tool")["state"], "active")

    def test_install_acquires_cross_process_lock_before_preflight(self):
        lib, lib_hash, lib_size = self.archive("libfoo", "1.0.0")
        _tool, tool_hash, tool_size = self.archive("tool", "1.0.0")
        catalog = self.catalog({
            "libfoo": (lib_hash, lib_size),
            "tool": (tool_hash, tool_size),
        })
        store = ToolchainStore(self.root / "tools", catalog)
        events = []
        acquire = store._acquire_transaction
        preflight = store.preflight

        def observed_acquire():
            lock = acquire()
            events.append("lock")
            return lock

        def observed_preflight(*args, **kwargs):
            events.append("preflight")
            self.assertTrue((store.root / ".transaction.lock").is_dir())
            return preflight(*args, **kwargs)

        with patch.object(store, "_acquire_transaction", side_effect=observed_acquire), patch.object(
            store, "preflight", side_effect=observed_preflight
        ):
            store.install("libfoo", lib, self.host())
        self.assertEqual(events[:2], ["lock", "preflight"])

    def test_inventory_is_versioned_queryable_and_truthful(self):
        _lib, lib_hash, lib_size = self.archive("libfoo", "1.0.0")
        _tool, tool_hash, tool_size = self.archive("tool", "1.0.0")
        catalog = self.catalog({
            "libfoo": (lib_hash, lib_size),
            "tool": (tool_hash, tool_size),
        })
        store = ToolchainStore(self.root / "tools", catalog)
        inventory = store.inventory(self.host())
        self.assertEqual(inventory["schema_version"], 1)
        self.assertEqual(inventory["target"]["abi"], "arm64-v8a")
        states = {row["name"]: row["state"] for row in inventory["packages"]}
        self.assertEqual(states["libfoo"], "available")
        self.assertEqual(states["tool"], "blocked")
        self.assertIn("dependency", inventory["packages"][1]["reason"])

    def test_inventory_reports_missing_active_entry_as_corrupt(self):
        lib, lib_hash, lib_size = self.archive("libfoo", "1.0.0")
        _tool, tool_hash, tool_size = self.archive("tool", "1.0.0")
        catalog = self.catalog({
            "libfoo": (lib_hash, lib_size),
            "tool": (tool_hash, tool_size),
        })
        store = ToolchainStore(self.root / "tools", catalog)
        store.install("libfoo", lib, self.host())
        shutil.rmtree(self.root / "tools" / "entries" / "libfoo" / "1.0.0")
        row = next(item for item in store.inventory(self.host())["packages"] if item["name"] == "libfoo")
        self.assertEqual(row["state"], "corrupt")

    def test_path_traversal_archive_is_rejected_without_execution(self):
        lib, lib_hash, lib_size = self.archive("libfoo", "1.0.0")
        _tool, tool_hash, tool_size = self.archive("tool", "1.0.0")
        catalog = self.catalog({
            "libfoo": (lib_hash, lib_size),
            "tool": (tool_hash, tool_size),
        })
        malicious = self.root / "malicious.zip"
        with zipfile.ZipFile(malicious, "w") as archive:
            archive.writestr("../../outside", "do not write")
        spec = catalog.package("tool")
        doc = catalog.to_dict()
        doc["packages"][1]["artifact"]["sha256"] = hashlib.sha256(malicious.read_bytes()).hexdigest()
        doc["packages"][1]["artifact"]["size"] = malicious.stat().st_size
        catalog = Catalog.from_dict(doc)
        store = ToolchainStore(self.root / "tools", catalog)
        store.install("libfoo", lib, self.host())
        with self.assertRaises(TransactionError):
            store.install("tool", malicious, self.host())
        self.assertFalse((self.root / "outside").exists())
        self.assertIsNotNone(spec)

    def test_tar_symlink_must_resolve_inside_staging_root(self):
        _lib, lib_hash, lib_size = self.archive("libfoo", "1.0.0")
        _tool, tool_hash, tool_size = self.archive("tool", "1.0.0")
        catalog = self.catalog({
            "libfoo": (lib_hash, lib_size),
            "tool": (tool_hash, tool_size),
        })
        store = ToolchainStore(self.root / "tools", catalog)

        def tar_bytes(linkname):
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode="w") as archive:
                member = tarfile.TarInfo("data/data/com.termux/files/usr/share/doc/copyright")
                member.type = tarfile.SYMTYPE
                member.linkname = linkname
                archive.addfile(member)
            return io.BytesIO(output.getvalue())

        destination = self.root / "stage"
        destination.mkdir()
        store._extract_tar(tar_bytes("../../LICENSES/LGPL-2.1.txt"), destination)
        self.assertTrue((destination / "data/data/com.termux/files/usr/share/doc/copyright").is_symlink())

        store._extract_tar(
            tar_bytes("/data/data/com.termux/files/usr/share/doc/LICENSES/LGPL-2.1.txt"),
            destination,
        )
        self.assertTrue((destination / "data/data/com.termux/files/usr/share/doc/copyright").is_symlink())

        with self.assertRaises(TransactionError):
            store._extract_tar(tar_bytes("../../../../../../../../../../outside"), destination)
        with self.assertRaises(TransactionError):
            store._extract_tar(tar_bytes("/system/bin/sh"), destination)
        self.assertFalse((self.root / "outside").exists())


if __name__ == "__main__":
    unittest.main()
