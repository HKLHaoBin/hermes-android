"""Static contract tests for Issue #10 durable Agent jobs.

Android/Kotlin execution is validated by the Actions build; these checks keep the
cross-component scheduler, ledger, notification, and UI seams explicit locally.
"""
from __future__ import annotations

import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
JAVA = ROOT / "android/app/src/main/java/io/github/hermesandroid"
LEDGER = (JAVA / "JobLedger.kt").read_text(encoding="utf-8")
SERVICE = (JAVA / "HermesService.kt").read_text(encoding="utf-8")
RUNTIME = (JAVA / "HermesRuntime.kt").read_text(encoding="utf-8")
APP = (JAVA / "HermesApp.kt").read_text(encoding="utf-8")
MANIFEST = (ROOT / "android/app/src/main/AndroidManifest.xml").read_text(encoding="utf-8")
ASSET_SERVER = (JAVA / "AssetServer.kt").read_text(encoding="utf-8")
ACTIVITY = (JAVA / "MainActivity.kt").read_text(encoding="utf-8")
INDEX = (ROOT / "android/app/src/main/assets/ui/index.html").read_text(encoding="utf-8")
UI = (ROOT / "android/app/src/main/assets/ui/app.js").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github/workflows/build-apk.yml").read_text(encoding="utf-8")


class Issue10SchedulerContractTests(unittest.TestCase):
    def test_ledger_persists_worker_heartbeat_and_schedule_metadata(self):
        self.assertIn("private const val DATABASE_VERSION = 2", LEDGER)
        self.assertIn("worker_id TEXT", LEDGER)
        self.assertIn("heartbeat_at INTEGER", LEDGER)
        self.assertIn("CREATE TABLE IF NOT EXISTS job_schedules", LEDGER)
        self.assertIn("fun heartbeat(", LEDGER)
        self.assertIn("fun dueSchedules(", LEDGER)
        self.assertIn("fun advanceSchedule(", LEDGER)
        self.assertIn("fun scheduleForJob(", LEDGER)

    def test_scheduler_components_are_real_and_service_owned(self):
        for name in (
            "AgentJobCoordinator.kt",
            "AgentJobClient.kt",
            "AgentJobPayloadStore.kt",
            "AgentJobNotification.kt",
            "AgentJobAlarmReceiver.kt",
            "AgentJobBootReceiver.kt",
        ):
            self.assertTrue((JAVA / name).is_file(), name)
        coordinator = (JAVA / "AgentJobCoordinator.kt").read_text(encoding="utf-8") if (JAVA / "AgentJobCoordinator.kt").is_file() else ""
        client = (JAVA / "AgentJobClient.kt").read_text(encoding="utf-8") if (JAVA / "AgentJobClient.kt").is_file() else ""
        self.assertIn("object AgentJobCoordinator", coordinator)
        self.assertIn("workerId", coordinator)
        self.assertIn("jobLedger.heartbeat", coordinator)
        self.assertIn("JobState.SUCCEEDED", coordinator)
        self.assertIn("JobState.PAUSED", coordinator)
        self.assertIn("JobState.CANCELLED", coordinator)
        self.assertIn("/api/chat/start", client)
        self.assertIn("/api/session?session_id=", client)
        self.assertIn("/api/chat/cancel?stream_id=", client)
        self.assertIn("X-Hermes-CSRF-Token", client)

    def test_runtime_and_agent_jobs_are_separate(self):
        self.assertIn('private const val RUNTIME_PROFILE = "android-runtime"', SERVICE)
        self.assertIn('const val AGENT_PROFILE = "default"', (JAVA / "AgentJobCoordinator.kt").read_text(encoding="utf-8"))
        self.assertIn("runtimeJobId", RUNTIME)
        self.assertIn("runtimeJobState", RUNTIME)
        self.assertIn("agentJobs", RUNTIME)
        self.assertIn("appVisible", RUNTIME)
        self.assertIn("hermesAgentJob", UI)
        self.assertIn("AgentJobCoordinator.onRuntimeReady", SERVICE)
        self.assertIn("AgentJobCoordinator.onRuntimeUnavailable", SERVICE)
        self.assertIn("AgentJobCoordinator.onServiceDestroyed", SERVICE)

    def test_foreground_service_and_process_recovery_hooks_exist(self):
        self.assertIn("ACTION_RUN_JOBS", SERVICE)
        self.assertIn("ACTION_RESUME_JOB", SERVICE)
        self.assertIn("ACTION_PAUSE_JOB", SERVICE)
        self.assertIn("ACTION_CANCEL_JOB", SERVICE)
        self.assertIn("startForeground", SERVICE)
        self.assertIn("AgentJobCoordinator.recoverAfterProcessDeath", SERVICE)
        self.assertIn("RECEIVE_BOOT_COMPLETED", MANIFEST)
        self.assertIn("AgentJobBootReceiver", MANIFEST)
        self.assertIn("AgentJobAlarmReceiver", MANIFEST)
        boot = (JAVA / "AgentJobBootReceiver.kt").read_text(encoding="utf-8")
        self.assertIn("ACTION_BOOT_COMPLETED", boot)
        self.assertIn("ACTION_MY_PACKAGE_REPLACED", boot)

    def test_task_notifications_and_diagnostics_are_exposed(self):
        notification = (JAVA / "AgentJobNotification.kt").read_text(encoding="utf-8") if (JAVA / "AgentJobNotification.kt").is_file() else ""
        self.assertIn("ACTION_RESUME_JOB", notification)
        self.assertIn("ACTION_PAUSE_JOB", notification)
        self.assertIn("ACTION_CANCEL_JOB", notification)
        self.assertIn("notificationId", notification)
        self.assertIn("areNotificationsEnabled", notification)
        self.assertIn("batteryOptimized", notification)
        self.assertIn('"jobs.json"', ASSET_SERVER)
        self.assertIn("jobDiagnosticsJson", ASSET_SERVER)
        self.assertIn("agentJobs", INDEX)
        self.assertIn("jobs.json", UI)
        self.assertIn("resumeAgentJob", ACTIVITY)
        self.assertIn("cancelAgentJob", ACTIVITY)
        self.assertIn("EXTRA_JOB_ID", ACTIVITY)
        self.assertIn("requestNotificationPermission", ACTIVITY)
        self.assertLess(UI.index("activateAgentJob"), UI.index("gw.request('prompt.submit'"))

    def test_ci_runs_issue10_static_contract(self):
        self.assertIn("Issue #10 scheduler contract regressions", WORKFLOW)
        self.assertIn("scripts/test_issue10_scheduler.py", WORKFLOW)


if __name__ == "__main__":
    unittest.main()
