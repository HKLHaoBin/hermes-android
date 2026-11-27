"""Standalone Issue #7 contract for the contributor branch.

The full integration contract is intentionally strict and runs only when the
parent Issue #1 capability/JobLedger surfaces are present.
"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class Issue7StandaloneContractTests(unittest.TestCase):
    def test_identity_slots_and_workflow_are_present(self):
        runtime = (ROOT / "android/app/src/main/java/io/github/hermesandroid/HermesRuntime.kt").read_text(encoding="utf-8")
        installer = (ROOT / "android/app/src/main/java/io/github/hermesandroid/PayloadInstaller.kt").read_text(encoding="utf-8")
        workflow = (ROOT / ".github/workflows/build-apk.yml").read_text(encoding="utf-8")
        compatibility = (ROOT / "compatibility.json").read_text(encoding="utf-8")
        for needle in ("runtimeVersion", "webuiCommit", "compatibilityStatus", "previousGoodSlot"):
            self.assertIn(needle, runtime)
        for needle in ("hermes-agent-slots", "previous_good", "recoverPending", "markHealthy"):
            self.assertIn(needle, installer)
        for needle in ('"runtime_slots": "files/hermes-agent-slots"', '"database": "databases/hermes_jobs.db"'):
            self.assertIn(needle, compatibility)
        for needle in ("webui_channel", "update-receipt.json", "resolve_webui.py"):
            self.assertIn(needle, workflow)


if __name__ == "__main__":
    unittest.main()