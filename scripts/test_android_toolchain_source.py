"""Source-bound Android integration contracts for Issue #5."""
import json
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
        cls.smoke = (ROOT / "scripts/smoke.sh").read_text(encoding="utf-8")
        cls.catalog = json.loads(
            (ROOT / "android/app/src/main/assets/toolchain/catalog.json").read_text(encoding="utf-8")
        )

    def test_catalog_is_locked_for_termux_arm64_and_verified(self):
        self.assertEqual(self.catalog["schema_version"], 1)
        self.assertEqual(self.catalog["target"]["abi"], "arm64-v8a")
        self.assertEqual(self.catalog["target"]["triple"], "linux-arm64-bionic")
        self.assertEqual(self.catalog["repository"]["signature"]["status"], "verified")
        self.assertEqual(self.catalog["repository"]["signature"]["verified_by"], "gpgv")
        names = {item["name"] for item in self.catalog["packages"]}
        self.assertTrue({"bash", "git", "curl", "wget", "tar", "unzip"} <= names)

    def test_runtime_status_exposes_versioned_toolchain_inventory(self):
        self.assertIn("ToolchainManager.inventoryJson", self.runtime)
        self.assertIn('put("androidApi",', self.runtime)
        self.assertIn('put("abi",', self.runtime)
        self.assertIn('put("schemaVersion",', self.runtime)
        self.assertIn('put("toolchain",', self.runtime)
        self.assertIn('put("capabilitySchemaVersion",', self.runtime)

    def test_asset_server_has_explicit_toolchain_query(self):
        self.assertIn('"toolchain.json"', self.asset_server)
        self.assertIn("ToolchainManager.inventoryJson", self.asset_server)

    def test_runtime_environment_consumes_active_tool_exports(self):
        self.assertIn("ToolchainManager.pathEntries", self.runtime_env)
        self.assertIn("HERMES_ANDROID_TOOLCHAIN_ROOT", self.runtime_env)
        self.assertIn("HERMES_ANDROID_TOOLCHAIN_CATALOG", self.runtime_env)

    def test_relocator_stages_toolchain_script_catalog_and_manifest_paths(self):
        self.assertIn("toolchain_catalog", self.relocator)
        self.assertIn("toolchain_script", self.relocator)
        self.assertIn("android_toolchain.py", self.relocator)

    def test_container_smoke_exercises_locked_dependency_closure(self):
        self.assertIn("install-closure", self.smoke)
        self.assertIn("TOOLCHAIN_INSTALL_OK", self.smoke)


if __name__ == "__main__":
    unittest.main()
