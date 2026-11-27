"""Source-bound contract checks for Issue #7 integration points."""
from pathlib import Path
import json
import unittest

ROOT = Path(__file__).resolve().parents[1]


class Issue7SourceContractTests(unittest.TestCase):
    def test_payload_manifest_and_identity_contract_are_wired(self):
        source = (ROOT / "scripts/relocate_payload.py").read_text(encoding="utf-8")
        for needle in (
            "--webui-identity", "--compatibility", '"format": 2',
            '"webui"', '"compatibility"', '"zip_sha256"', 'sha256_tree',
            '"payload"', '"payload_id"',
        ):
            self.assertIn(needle, source)

    def test_workflow_uses_immutable_webui_selection_and_receipt(self):
        source = (ROOT / ".github/workflows/build-apk.yml").read_text(encoding="utf-8")
        for needle in (
            "webui_channel", "resolve_webui.py", "steps.webui.outputs.commit",
            "update-receipt.json", "update-receipt.sig", "payload-manifest.json",
        ):
            self.assertIn(needle, source)

    def test_parent_integration_contract_fails_closed_on_partial_surface(self):
        workflow = (ROOT / ".github/workflows/build-apk.yml").read_text(encoding="utf-8")
        self.assertIn("python3 scripts/issue7_integration_gate.py", workflow)
        self.assertIn("scripts.test_issue7_parent_gate", workflow)
        strict = (ROOT / "scripts/test_issue7_integration_contract.py").read_text(encoding="utf-8")
        self.assertNotIn("self.skipTest(", strict)

    def test_android_installer_has_slots_previous_good_and_atomic_state(self):
        source = (ROOT / "android/app/src/main/java/io/github/hermesandroid/PayloadInstaller.kt").read_text(encoding="utf-8")
        for needle in (
            "hermes-agent-slots", "active.json", "previous_good", "pending",
            "rollback", "markHealthy", "recoverPending", "FileOutputStream",
            "renameTo", "zip_sha256", "rejectedPayloadId", "rejected", "pruneSlots", "pruneStaleSlots", "beforeDiscard(slotDir(it).absolutePath)",
        ):
            self.assertIn(needle, source)
        self.assertIn("failed?.takeIf { it != restored && it != retained }?.let { FileTools.deleteTree(slotDir(it)) }", source)
        self.assertIn('"reused_previous_good"', source)
        self.assertIn("rejected = failed", source)

    def test_runtime_status_exposes_pair_identity_and_compatibility(self):
        runtime = (ROOT / "android/app/src/main/java/io/github/hermesandroid/HermesRuntime.kt").read_text(encoding="utf-8")
        service = (ROOT / "android/app/src/main/java/io/github/hermesandroid/HermesService.kt").read_text(encoding="utf-8")
        for needle in (
            "runtimeVersion", "runtimeCommit", "webuiChannel", "webuiTag",
            "webuiCommit", "compatibilityStatus", "previousGoodSlot",
        ):
            self.assertIn(needle, runtime)
        for needle in ("recoverPending", "markHealthy", "rollback", "compatibility"):
            self.assertIn(needle, service)

    def test_service_restarts_restored_slot_and_stops_before_rollback(self):
        service = (ROOT / "android/app/src/main/java/io/github/hermesandroid/HermesService.kt").read_text(encoding="utf-8")
        for needle in ("installer.recoverPending(::quiescePayload)", "return runRuntime()", "stopOwnedProcess(attemptedRoot)", "proc?.waitFor()", "installer.pruneStaleSlots()"):
            self.assertIn(needle, service)

    def test_compatibility_file_is_non_secret(self):
        data = json.loads((ROOT / "compatibility.json").read_text(encoding="utf-8"))
        self.assertEqual(data["contract"], "android-hermes-runtime-webui-v1")
        self.assertEqual(data["status"], "verified")
        self.assertEqual(data["abis"], ["arm64-v8a"])
        raw = json.dumps(data).lower()
        for forbidden in ("api_key", "authorization", "cookie", "password", "token", "secret"):
            self.assertNotIn(forbidden, raw)


if __name__ == "__main__":
    unittest.main()
