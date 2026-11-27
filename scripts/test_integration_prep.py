"""Source/contract checks for integration of PR #2, #3, #4 and Issue #10."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
JAVA = ROOT / "android/app/src/main/java/io/github/hermesandroid"
RUNTIME = (JAVA / "HermesRuntime.kt").read_text(encoding="utf-8")
LEDGER = (JAVA / "JobLedger.kt").read_text(encoding="utf-8")
COORDINATOR = (JAVA / "AgentJobCoordinator.kt").read_text(encoding="utf-8")
SERVICE = (JAVA / "HermesService.kt").read_text(encoding="utf-8")
INDEX = (ROOT / "android/app/src/main/assets/ui/index.html").read_text(encoding="utf-8")
UI = (ROOT / "android/app/src/main/assets/ui/app.js").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github/workflows/build-apk.yml").read_text(encoding="utf-8")
DOCS = (ROOT / "docs/integration-prep.md").read_text(encoding="utf-8") if (ROOT / "docs/integration-prep.md").is_file() else ""


class IntegrationPrepTests(unittest.TestCase):
    def test_capability_and_job_status_share_one_runtime_snapshot(self):
        self.assertIn("runtimeJobId", RUNTIME)
        self.assertIn("runtimeJobState", RUNTIME)
        self.assertIn("agentJobs", RUNTIME)
        self.assertIn("capabilitiesJson", RUNTIME)
        self.assertIn('put("capabilities", capabilitiesJson())', RUNTIME)
        self.assertIn('put("runtimeJobId", s.runtimeJobId)', RUNTIME)
        self.assertIn('put("agentJobs", s.agentJobs)', RUNTIME)

    def test_job_ledger_is_the_only_persistent_job_store(self):
        self.assertEqual(LEDGER.count("class JobLedger("), 1)
        self.assertIn("private const val DATABASE_VERSION = 2", LEDGER)
        self.assertIn("worker_id TEXT", LEDGER)
        self.assertIn("heartbeat_at INTEGER", LEDGER)
        self.assertIn("CREATE TABLE IF NOT EXISTS job_schedules", LEDGER)
        self.assertIn("JobLedger", COORDINATOR)
        self.assertIn("RuntimeJobLedger", SERVICE)
        self.assertIn('const val AGENT_PROFILE = "default"', COORDINATOR)
        self.assertIn('private const val RUNTIME_PROFILE = "android-runtime"', SERVICE)

    def test_staging_contracts_are_all_in_ci(self):
        self.assertIn("scripts/test_capabilities.py", WORKFLOW)
        self.assertIn("scripts/test_job_ledger_contract.py", WORKFLOW)
        self.assertIn("scripts/test_job_ledger_source.py", WORKFLOW)
        self.assertIn("scripts/test_issue10_scheduler.py", WORKFLOW)
        self.assertIn("integration-prep", DOCS)

    def test_ui_keeps_capability_and_agent_job_surfaces_separate(self):
        self.assertEqual(INDEX.count('id="capabilities"'), 1)
        self.assertIn('data-kind="agentJobs"', INDEX)
        self.assertIn("status.capabilities", UI)
        self.assertIn("jobs.json", UI)
        self.assertIn("job-diagnostics.json", UI)


if __name__ == "__main__":
    unittest.main()
