"""Source-bound Android integration contracts for Issue #5."""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KOTLIN = ROOT / "android/app/src/main/java/io/github/hermesandroid"


class AndroidToolchainSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = (KOTLIN / "HermesRuntime.kt").read_text(encoding="utf-8")
        cls.asset_server = (KOTLIN / "AssetServer.kt").read_text(encoding="utf-8")
        cls.runtime_env = (KOTLIN / "RuntimeEnv.kt").read_text(encoding="utf-8")
        cls.relocator = (ROOT / "scripts/relocate_payload.py").read_text(encoding="utf-8")
        cls.smoke_env = (ROOT / "scripts/smoke_env.py").read_text(encoding="utf-8")
        cls.smoke = (ROOT / "scripts/smoke.sh").read_text(encoding="utf-8")
        cls.workflow = (ROOT / ".github/workflows/build-apk.yml").read_text(encoding="utf-8")
        cls.catalog = json.loads(
            (ROOT / "android/app/src/main/assets/toolchain/catalog.json").read_text(encoding="utf-8")
        )
        cls.lock = json.loads(
            (ROOT / "toolchain/termux-catalog.lock.json").read_text(encoding="utf-8")
        )

    def test_catalog_is_locked_for_termux_arm64_and_verified(self):
        self.assertEqual(self.catalog["schema_version"], 1)
        self.assertEqual(self.catalog["target"]["abi"], "arm64-v8a")
        self.assertEqual(self.catalog["target"]["triple"], "linux-arm64-bionic")
        self.assertEqual(self.catalog["repository"]["signature"]["status"], "verified")
        self.assertEqual(self.catalog["repository"]["signature"]["verified_by"], "gpgv")
        self.assertIn("keyring_artifact", self.catalog["repository"])
        self.assertTrue(all(item.get("source", {}).get("package") for item in self.catalog["packages"]))
        self.assertTrue(all(item.get("license") for item in self.catalog["packages"]))
        names = {item["name"] for item in self.catalog["packages"]}
        self.assertTrue({"bash", "git", "curl", "wget", "tar", "unzip"} <= names)

    def test_issue5_requested_tools_are_locked_with_declared_entrypoints(self):
        requested = {
            "ripgrep": "bin/rg",
            "ffmpeg": "bin/ffmpeg",
            "python": "bin/python",
            "nodejs": "bin/node",
            "npm": "bin/npm",
            "uv": "bin/uv",
        }
        catalog_by_name = {item["name"]: item for item in self.catalog["packages"]}
        self.assertTrue(set(requested) <= set(self.lock["roots"]))
        self.assertTrue(set(requested) <= set(catalog_by_name))
        for name, entrypoint in requested.items():
            package = catalog_by_name[name]
            self.assertEqual(package["entrypoints"], [entrypoint])
            self.assertRegex(package["artifact"]["sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(package["artifact"]["size"] > 0)
            self.assertEqual(package["source"]["package"], name)

    def test_catalog_and_source_lock_have_one_repository_identity(self):
        catalog_repo = self.catalog["repository"]
        lock_repo = self.lock["repository"]
        for field in ("inrelease_sha256", "packages_index_sha256", "key_fingerprint"):
            self.assertEqual(catalog_repo[field].lower(), lock_repo[field].lower())
        self.assertEqual(
            catalog_repo["keyring_artifact"]["sha256"],
            lock_repo["keyring_artifact"]["sha256"],
        )

    def test_runtime_status_exposes_versioned_toolchain_inventory(self):
        self.assertIn("ToolchainManager.inventoryJson", self.runtime)
        self.assertIn('put("androidApi",', self.runtime)
        self.assertIn('put("abi",', self.runtime)
        self.assertIn('put("schemaVersion",', self.runtime)
        self.assertIn('put("toolchain",', self.runtime)
        self.assertIn('put("capabilitySchemaVersion",', self.runtime)
        self.assertIn('put("capabilities", capabilitiesJson())', self.runtime)
        self.assertIn('fun statusJson(includeToken: Boolean)', self.runtime)
        capability_ids = re.findall(r'capability\(this,\s*"([a-z_]+)"', self.runtime)
        self.assertEqual(len(capability_ids), 13)
        self.assertNotIn("toolchain", capability_ids)

    def test_asset_server_has_explicit_toolchain_query(self):
        self.assertIn('"toolchain.json"', self.asset_server)
        self.assertIn("ToolchainManager.inventoryJson", self.asset_server)
        self.assertIn("HermesRuntime.statusJson(context, includeToken = true)", self.asset_server)

    def test_runtime_environment_consumes_active_tool_exports(self):
        self.assertIn("ToolchainManager.pathEntries", self.runtime_env)
        self.assertIn("HERMES_ANDROID_TOOLCHAIN_ROOT", self.runtime_env)
        self.assertIn("HERMES_ANDROID_TOOLCHAIN_CATALOG", self.runtime_env)

    def test_native_inventory_validates_authoritative_selection(self):
        manager = (KOTLIN / "ToolchainManager.kt").read_text(encoding="utf-8")
        self.assertIn("selectionStatus", manager)
        self.assertIn(".hermes-package.json", manager)
        self.assertIn("artifact_sha256", manager)
        self.assertIn("entrypoints", manager)
        self.assertIn('SelectionStatus("outdated"', manager)
        self.assertIn("activeTreeValid", manager)
        self.assertIn('optJSONObject("exports")', manager)
        self.assertIn('status.state != "active"', manager)
        self.assertIn("readState", manager)
        self.assertIn("validState", manager)
        self.assertNotIn('readObject(File(root(context), "active.json"))', manager)
        self.assertNotIn('readObject(File(root(context), "previous.json"))', manager)

    def test_relocator_stages_toolchain_script_catalog_and_manifest_paths(self):
        self.assertIn("toolchain_catalog", self.relocator)
        self.assertIn("toolchain_script", self.relocator)
        self.assertIn("android_toolchain.py", self.relocator)

    def test_container_smoke_exercises_locked_dependency_closure(self):
        self.assertIn("install-closure", self.smoke)
        self.assertIn("TOOLCHAIN_INSTALL_OK", self.smoke)
        for tool in ("ripgrep", "ffmpeg", "python", "nodejs", "npm", "uv"):
            self.assertIn(tool, self.smoke)
        self.assertIn("installed-bash", self.smoke)
        self.assertIn("TOOLCHAIN_ACTIVE_OK", self.smoke)
        self.assertIn("check_tool_path", self.smoke)
        self.assertIn("env --shell", self.smoke)

    def test_smoke_environment_refreshes_from_installed_toolchain_state(self):
        self.assertIn("refresh_toolchain_env", self.smoke_env)
        self.assertIn("env --shell", self.smoke_env)
        self.assertIn("HERMES_ANDROID_TOOLCHAIN_PATH", self.smoke_env)
        self.assertIn("HERMES_ANDROID_TOOLCHAIN_LD_LIBRARY_PATH", self.smoke_env)

    def test_workflow_preserves_job_ledger_and_adds_toolchain_contracts(self):
        self.assertIn("scripts/test_job_ledger_contract.py", self.workflow)
        self.assertIn("scripts/test_job_ledger_source.py", self.workflow)
        self.assertIn("scripts/test_android_toolchain.py", self.workflow)
        self.assertIn("scripts/test_termux_catalog.py", self.workflow)


if __name__ == "__main__":
    unittest.main()
