"""Focused source contracts for the maintainer findings in this repair stage."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
JAVA = ROOT / "android/app/src/main/java/io/github/hermesandroid"


def method(source: str, name: str) -> str:
    source = source.replace("\r\n", "\n")
    start = re.search(r"(?m)^    (?:(?:private|override) )?fun " + re.escape(name) + r"\(", source)
    if start is None:
        raise AssertionError(f"missing method {name}")
    following = re.search(r"(?m)^    (?:private )?fun \w+\(", source[start.end():])
    end = start.end() + following.start() if following else len(source)
    return source[start.start():end]


class SecurityRepairContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = (JAVA / "HermesService.kt").read_text(encoding="utf-8")
        cls.installer = (JAVA / "PayloadInstaller.kt").read_text(encoding="utf-8")
        cls.workflow = (ROOT / ".github/workflows/build-apk.yml").read_text(encoding="utf-8")

    def test_parent_surfaces_and_gates_are_restored(self):
        for relative in (
            "scripts/test_capabilities.py",
            "android/app/src/main/java/io/github/hermesandroid/JobLedger.kt",
            "scripts/test_job_ledger_contract.py",
            "scripts/test_job_ledger_source.py",
            "docs/job-ledger.md",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)
        for needle in (
            "scripts.test_capabilities",
            "scripts.test_job_ledger_contract",
            "scripts.test_job_ledger_source",
            "python3 scripts/issue7_integration_gate.py",
        ):
            self.assertIn(needle, self.workflow)
        runtime = (JAVA / "HermesRuntime.kt").read_text(encoding="utf-8")
        ui = (ROOT / "android/app/src/main/assets/ui/index.html").read_text(encoding="utf-8")
        app = (ROOT / "android/app/src/main/assets/ui/app.js").read_text(encoding="utf-8")
        self.assertIn('put("capabilities", capabilitiesJson())', runtime)
        self.assertIn('id="capabilities"', ui)
        self.assertIn("renderCapabilities", app)

    def test_service_serializes_commands_and_fences_teardown(self):
        start = method(self.service, "onStartCommand")
        self.assertIn("synchronized(lifecycleLock)", start)
        self.assertIn("commandGeneration += 1L", start)
        self.assertIn("pendingStart = true", start)
        self.assertIn("else if (worker?.isAlive != true)", start)
        self.assertIn("requestTeardownLocked(startId, restart = false)", start)
        self.assertIn("requestTeardownLocked(startId, restart = true)", start)
        teardown = method(self.service, "teardownLoop")
        self.assertIn("if (generation != activeTeardownGeneration) continue", teardown)
        self.assertIn("launchLocked()", teardown)
        self.assertIn("stopSelfResult(teardownStartId)", teardown)
        stop = method(self.service, "stopRuntime")
        self.assertIn("generation: Long", stop)
        self.assertIn("if (generation != activeTeardownGeneration) return", stop)
        self.assertLess(stop.index("if (generation != activeTeardownGeneration) return"), stop.index("ProcUtil.stopTree"))
        self.assertNotIn("stopSelf()", self.service)
        self.assertIn("worker?.join()", stop)

    def test_retained_slots_have_complete_integrity_validation(self):
        reuse = method(self.installer, "canReuseSlot")
        self.assertIn("validateSlot(candidate, manifest)", reuse)
        self.assertIn(".payload_integrity.json", self.installer)
        self.assertIn("slotContentDigest", self.installer)
        self.assertIn("manifest_sha256", self.installer)
        self.assertIn("content_sha256", self.installer)
        self.assertIn("validateManifestEntries", self.installer)

    def test_corrupt_active_payload_is_quarantined_before_staging(self):
        install = method(self.installer, "install")
        self.assertIn("quarantineInvalidActive", install)
        self.assertIn("beforeDiscard", install)

    def test_legacy_migration_uses_no_follow_metadata_and_proven_manifest(self):
        migration = method(self.installer, "migrateLegacy")
        finish = method(self.installer, "finishLegacyMigration")
        rewrite = method(self.installer, "rewriteLegacyPaths")
        self.assertIn("legacyManifestOrNull", migration + finish)
        self.assertIn("readNoFollowText", self.installer)
        self.assertIn("readNoFollowBytes", rewrite)
        self.assertIn("writeAtomicNoFollow", self.installer)
        self.assertIn("FileTools.isSymlink", self.installer)
        self.assertNotIn(".writeText(", rewrite)
        self.assertNotIn(".writeBytes(", rewrite)
        self.assertNotIn(".readBytes(", rewrite)
        self.assertNotIn("JSONObject(manifest.toString()).apply", finish)
        self.assertIn("if (legacyManifest == null) return", migration)


if __name__ == "__main__":
    unittest.main()
