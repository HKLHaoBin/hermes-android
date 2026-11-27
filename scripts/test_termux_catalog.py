"""Offline tests for the signed Termux ARM64 catalog builder."""
from __future__ import annotations

import json
import unittest

from fetch_termux_catalog import (
    CatalogBuildError,
    build_catalog,
    dependency_names,
    parse_packages,
    resolve_dependency_closure,
    unconditional_names,
    validate_source_lock,
)

PACKAGES = """\
Package: bash
Version: 5.3.20
Architecture: aarch64
Filename: pool/main/b/bash/bash_5.3.20_aarch64.deb
Size: 100
Installed-Size: 300
SHA256: {bash_hash}
Depends: libandroid-support, termux-tools\n
Package: libandroid-support
Version: 29-1
Architecture: aarch64
Filename: pool/main/liba/libandroid-support/libandroid-support_29-1_aarch64.deb
Size: 50
Installed-Size: 120
SHA256: {lib_hash}\n
Package: termux-tools
Version: 1.0
Architecture: aarch64
Filename: pool/main/t/termux-tools/termux-tools_1.0_aarch64.deb
Size: 80
Installed-Size: 180
SHA256: {tools_hash}
Depends: libandroid-support\n
"""


class TermuxCatalogTests(unittest.TestCase):
    def setUp(self):
        self.hashes = {
            "bash": "1" * 64,
            "lib": "2" * 64,
            "tools": "3" * 64,
        }
        self.packages = parse_packages(PACKAGES.format(
            bash_hash=self.hashes["bash"],
            lib_hash=self.hashes["lib"],
            tools_hash=self.hashes["tools"],
        ))
        self.lock = {
            "schema_version": 1,
            "repository": {
                "name": "termux-main",
                "base_url": "https://packages.termux.dev/apt/termux-main/",
                "suite": "stable",
                "component": "main",
                "architecture": "aarch64",
                "inrelease_sha256": "a" * 64,
                "packages_index_sha256": "b" * 64,
                "key_fingerprint": "c" * 40,
            },
            "roots": ["bash"],
        }

    def test_dependency_parser_strips_versions_and_alternatives(self):
        self.assertEqual(
            dependency_names("libfoo (>= 1), bar | baz, qux:any"),
            ["libfoo", "bar", "qux"],
        )

    def test_conflict_parser_does_not_turn_versioned_conflicts_into_unconditional_blocks(self):
        self.assertEqual(unconditional_names("foo (<< 3), bar"), ["bar"])

    def test_closure_is_deterministic_and_rejects_missing_dependency(self):
        closure = resolve_dependency_closure(self.packages, ["bash"])
        self.assertEqual(closure, ["bash", "libandroid-support", "termux-tools"])
        with self.assertRaisesRegex(CatalogBuildError, "missing"):
            resolve_dependency_closure(self.packages, ["missing"])

    def test_source_lock_requires_verified_signature_and_exact_digests(self):
        validate_source_lock(self.lock, self.lock["repository"] | {"signature_verified": True})
        with self.assertRaisesRegex(CatalogBuildError, "signature"):
            validate_source_lock(self.lock, self.lock["repository"] | {"signature_verified": False})
        bad = dict(self.lock["repository"], packages_index_sha256="0" * 64)
        with self.assertRaisesRegex(CatalogBuildError, "Packages"):
            validate_source_lock(self.lock, bad | {"signature_verified": True})

    def test_build_catalog_records_signature_source_and_exact_artifacts(self):
        source = self.lock["repository"] | {
            "signature_verified": True,
            "inrelease_sha256": self.lock["repository"]["inrelease_sha256"],
            "packages_index_sha256": self.lock["repository"]["packages_index_sha256"],
        }
        catalog = build_catalog(self.packages, source, self.lock["roots"])
        self.assertEqual(catalog["schema_version"], 1)
        self.assertEqual(catalog["target"]["abi"], "arm64-v8a")
        self.assertEqual(catalog["repository"]["signature"]["status"], "verified")
        names = [item["name"] for item in catalog["packages"]]
        self.assertEqual(names, ["bash", "libandroid-support", "termux-tools"])
        bash = catalog["packages"][0]
        self.assertEqual(bash["artifact"]["sha256"], self.hashes["bash"])
        self.assertEqual(bash["artifact"]["size"], 100)
        self.assertEqual(bash["install_bytes"], 300 * 1024)
        json.dumps(catalog)


if __name__ == "__main__":
    unittest.main()
