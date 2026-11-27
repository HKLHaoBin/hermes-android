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
        cls.kotlin_build = (ROOT / "android/build.gradle.kts").read_text(encoding="utf-8")

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
        self.assertIn("var generationMatched = false", teardown)
        self.assertNotIn("if (generation != activeTeardownGeneration) continue", teardown)
        self.assertIn("continue", teardown)
        self.assertIn("launchLocked()", teardown)
        self.assertIn("stopSelfResult(teardownStartId)", teardown)
        stop = method(self.service, "stopRuntime")
        self.assertIn("generation: Long", stop)
        self.assertIn("if (generation != activeTeardownGeneration) return", stop)
        self.assertLess(stop.index("if (generation != activeTeardownGeneration) return"), stop.index("ProcUtil.stopTree"))
        self.assertNotIn("stopSelf()", self.service)
        self.assertIn("worker?.join()", stop)
        self.assertIn("ProcessOwner", self.service)
        self.assertIn("ownerIsCurrentLocked", self.service)
        self.assertIn("proc?.isAliveCompat() == true", stop)
        self.assertNotIn("if (target > 0) ProcUtil.stopTree", stop)
        auto = method(self.service, "scheduleAutoRestart")
        self.assertIn("failedWorker.join()", auto)
        self.assertIn("autoRestartOwnerGeneration", auto)
        self.assertIn("latestCommand == Command.START", auto)
        self.assertIn("worker === failedWorker", auto)
        self.assertNotIn("pendingStart = true", auto)
        self.assertIn('id("org.jetbrains.kotlin.android") version "2.0.21"', self.kotlin_build)

    def test_workflow_keeps_contract_command_continuation_intact(self):
        continuation = "scripts.test_issue7_smoke_contract " + "\\" + "\n" + "            scripts.test_issue7_parent_gate"
        self.assertIn(
            continuation,
            self.workflow,
        )

    def test_retained_slots_have_complete_integrity_validation(self):
        reuse = method(self.installer, "canReuseSlot")
        self.assertIn("validateSlot(candidate, manifest)", reuse)
        self.assertIn(".payload_integrity.json", self.installer)
        self.assertIn("slotContentDigest", self.installer)
        self.assertIn("manifest_sha256", self.installer)
        self.assertIn("content_sha256", self.installer)
        self.assertIn("validateManifestEntries", self.installer)
        self.assertIn("MODE_MASK", self.installer)
        self.assertIn("EXECUTE_BITS", self.installer)
        self.assertIn("entry.mode.toString(8)", self.installer)
        self.assertIn("out += ContentEntry(relative, 'D', current, mode = mode)", self.installer)
        self.assertIn("hasExecuteBit", self.installer)
        self.assertIn("validateSymlinkTarget", self.installer)
        self.assertIn('arrayOf("python", "repo", "site")', self.installer)
        self.assertNotIn("candidate.exists()", self.installer)

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
        self.assertIn("existingMode", self.installer)
        self.assertIn("Os.chmod(temp.absolutePath, it)", self.installer)
        self.assertIn("FileTools.isSymlink", self.installer)
        self.assertNotIn(".writeText(", rewrite)
        self.assertNotIn(".writeBytes(", rewrite)
        self.assertNotIn(".readBytes(", rewrite)
        self.assertNotIn("JSONObject(manifest.toString()).apply", finish)
        self.assertIn("if (legacyManifest == null) {", migration)
        self.assertIn("app/payload-manifest.json", self.installer)
        self.assertIn("app/payload_manifest.json", self.installer)
        self.assertIn("payload_manifest", self.installer)
        self.assertIn("NO_TRUSTED_MANIFEST", self.installer)
        self.assertIn("legacy-status", self.installer)
        runtime = (JAVA / "HermesRuntime.kt").read_text(encoding="utf-8")
        self.assertIn("legacyMigrationStatus", runtime)
        self.assertIn('put("legacyMigrationStatus", s.legacyMigrationStatus)', runtime)
        self.assertIn('"legacy-no-fallback"', self.service)

    def test_activation_validates_current_active_and_candidate_again(self):
        install = method(self.installer, "install")
        activate = method(self.installer, "activateExisting")
        self.assertIn("val activeId = current", install)
        self.assertIn("quarantineInvalidActive(activeId", install)
        self.assertIn("!validateSlot(candidate, manifest)", install)
        self.assertIn("activateExisting(payloadId, current, manifest", install)
        self.assertIn("refusing to activate unverified candidate", activate)
        self.assertIn("refusing to retain unverified active slot", activate)
        prune = method(self.installer, "pruneSlots")
        self.assertIn("val validActive", prune)
        self.assertIn("val validPrevious", prune)
        self.assertIn("quiescence callback required", prune)


if __name__ == "__main__":
    unittest.main()
