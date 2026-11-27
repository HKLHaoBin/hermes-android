"""Focused source contracts for the reviewed Android rollback operations.

These inspect production ordering without compiling/executing Kotlin. The
portable state tests complement them; neither is an Android runtime result.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
JAVA = ROOT / "android/app/src/main/java/io/github/hermesandroid"


def method(source: str, name: str) -> str:
    source = source.replace("\r\n", "\n")
    start = re.search(r"(?m)^    (?:private )?fun " + re.escape(name) + r"\(", source)
    if start is None:
        raise AssertionError(f"missing production method {name}")
    following = re.search(r"(?m)^    (?:private )?fun \w+\(", source[start.end():])
    end = start.end() + following.start() if following else len(source)
    return source[start.start():end]


class ReviewedSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = (JAVA / "HermesService.kt").read_text(encoding="utf-8")
        cls.installer = (JAVA / "PayloadInstaller.kt").read_text(encoding="utf-8")

    def test_recovery_requires_quiescence_before_state_change(self):
        recovery = method(self.installer, "recoverPending")
        rollback = method(self.installer, "rollback")
        self.assertIn("beforeDiscard: (String) -> Unit", recovery)
        self.assertIn("rollback(beforeDiscard)", recovery)
        self.assertLess(rollback.index("beforeDiscard("), rollback.index("writeState("))
        service = method(self.service, "runRuntime")
        self.assertIn("installer.recoverPending(::quiescePayload)", service)

    def test_manifest_selection_persists_quarantine_and_never_installs_old_manifest_from_new_zip(self):
        selection = method(self.installer, "selectedManifest")
        self.assertIn("rejectedPayloadId()", selection)
        self.assertIn("installedManifest()", selection)
        self.assertIn("needsInstall(restored)", selection)
        self.assertIn("throw IOException", selection)
        healthy = method(self.installer, "markHealthy")
        self.assertNotIn('|| state.optStringOrNull("rejected")', healthy)
        service = method(self.service, "runRuntime")
        self.assertIn("installer.selectedManifest(includedManifest)", service)

    def test_all_activation_failures_cleanup_before_rollback_and_restart_fallback(self):
        service = method(self.service, "runRuntime")
        failure = service[service.index("} catch (t: Throwable)"):]
        self.assertLess(failure.index("stopOwnedProcess(attemptedRoot)"), failure.index("installer.rollback("))
        self.assertIn("installer.rollback(::quiescePayload)", failure)
        self.assertIn("return runRuntime()", failure)
        self.assertIn("if (!stopping && restored != null && restoredManifest != null)", failure)
        cleanup = method(self.service, "stopOwnedProcess")
        self.assertLess(cleanup.index("quiescePayload("), cleanup.index("proc?.waitFor()"))
        self.assertLess(cleanup.index("proc?.waitFor()"), cleanup.index("process = null"))
        self.assertNotIn("stopping = true", cleanup)
        self.assertNotIn("installer.rollback", cleanup)

    def test_healthy_state_durable_before_running_publication(self):
        service = method(self.service, "runRuntime")
        self.assertLess(service.index("installer.markHealthy()"), service.index("phase = HermesRuntime.Phase.RUNNING"))
        self.assertIn('restoredManifest ?: org.json.JSONObject()', service)

    def test_gc_runs_before_space_check_and_after_every_health_check(self):
        service = method(self.service, "runRuntime")
        self.assertLess(service.index("installer.pruneStaleSlots()"), service.index("if (installer.needsInstall(manifest))"))
        healthy = method(self.installer, "markHealthy")
        self.assertRegex(healthy, r"\n        }\n        pruneSlots\(\)")
        prune = method(self.installer, "pruneSlots")
        self.assertIn('state.optStringOrNull("active")', prune)
        self.assertIn('state.optStringOrNull("previous_good")', prune)
        self.assertIn("FileTools.exists(child)", prune)

    def test_method_extraction_is_bounded_and_newline_independent(self):
        fixture = '    private fun a() { wanted() }\n\n    fun b() { forbidden() }\n'
        for text in (fixture, fixture.replace('\n', '\r\n')):
            self.assertNotIn('forbidden', method(text, 'a'))
        with self.assertRaisesRegex(AssertionError, 'missing production method'):
            method(fixture, 'missing')


if __name__ == "__main__":
    unittest.main()
