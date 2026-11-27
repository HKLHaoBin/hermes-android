"""Integration-prep contract for Issue #7 against parent Issue #1 PR surfaces.

The test is strict when run against an integration checkout: the parent capability and JobLedger surfaces must both exist, and their source/workflow contracts must pass. The standalone contributor checkout uses `scripts/test_issue7_standalone_contract.py`; a partial parent surface is never treated as a skip.
"""
from pathlib import Path
import os
import unittest


ROOT = Path(os.environ.get("HERMES_ISSUE7_INTEGRATION_ROOT", Path(__file__).resolve().parents[1]))


def source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


class Issue7IntegrationContractTests(unittest.TestCase):
    def test_parent_contract_files_are_required(self):
        for relative in (
            "scripts/test_capabilities.py",
            "android/app/src/main/java/io/github/hermesandroid/JobLedger.kt",
            "scripts/test_job_ledger_contract.py",
            "scripts/test_job_ledger_source.py",
        ):
            with self.subTest(relative=relative):
                self.assertTrue((ROOT / relative).is_file(), f"Missing parent contract: {relative}")

    def test_issue7_identity_and_slot_contract_is_present(self):
        runtime = source("android/app/src/main/java/io/github/hermesandroid/HermesRuntime.kt")
        installer = source("android/app/src/main/java/io/github/hermesandroid/PayloadInstaller.kt")
        workflow = source(".github/workflows/build-apk.yml")
        compatibility_path = ROOT / "compatibility.json"
        self.assertTrue(compatibility_path.is_file(), "Issue #7 compatibility matrix is missing")
        compatibility = compatibility_path.read_text(encoding="utf-8")
        for needle in ('"runtime_slots": "files/hermes-agent-slots"', '"database": "databases/hermes_jobs.db"'):
            self.assertIn(needle, compatibility)
        for needle in ("runtimeVersion", "webuiCommit", "compatibilityStatus", "previousGoodSlot"):
            self.assertIn(needle, runtime)
        for needle in ("hermes-agent-slots", "previous_good", "recoverPending", "markHealthy"):
            self.assertIn(needle, installer)
        for needle in ("webui_channel", "update-receipt.json", "resolve_webui.py"):
            self.assertIn(needle, workflow)

    def test_capability_surface_is_preserved_when_parent_pr2_is_present(self):
        capability_test = ROOT / "scripts/test_capabilities.py"
        self.assertTrue(capability_test.is_file(), "parent PR #2 capability contract is missing")
        runtime = source("android/app/src/main/java/io/github/hermesandroid/HermesRuntime.kt")
        index = source("android/app/src/main/assets/ui/index.html")
        app = source("android/app/src/main/assets/ui/app.js")
        workflow = source(".github/workflows/build-apk.yml")
        for needle in ("androidApi", "abi", "capabilities", "capabilitiesJson"):
            self.assertIn(needle, runtime)
        self.assertIn('id="capabilities"', index)
        self.assertIn("renderCapabilities", app)
        self.assertIn("scripts/test_capabilities.py", workflow)

    def test_job_ledger_surface_is_preserved_when_parent_pr3_pr4_is_present(self):
        ledger_path = ROOT / "android/app/src/main/java/io/github/hermesandroid/JobLedger.kt"
        self.assertTrue(ledger_path.is_file(), "parent JobLedger surface is missing")
        ledger = ledger_path.read_text(encoding="utf-8")
        workflow = source(".github/workflows/build-apk.yml")
        for needle in ("expectedUpdatedAt", "updated_at = ?", "runtime_id TEXT", "profile_id TEXT"):
            self.assertIn(needle, ledger)
        self.assertIn("scripts/test_job_ledger_contract.py", workflow)
        self.assertIn("scripts/test_job_ledger_source.py", workflow)


if __name__ == "__main__":
    unittest.main()
