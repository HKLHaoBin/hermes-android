"""Static contract checks for the Android JobLedger source.

Kotlin compilation and Android runtime behavior are verified by the Actions build;
these checks keep the critical SQL/state-machine safeguards visible in the source.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'android/app/src/main/java/io/github/hermesandroid/JobLedger.kt'


class JobLedgerSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE.read_text(encoding='utf-8')

    def test_schema_contains_all_contract_columns(self):
        for column in (
            'job_id TEXT PRIMARY KEY', 'profile_id TEXT NOT NULL', 'state TEXT NOT NULL',
            'created_at INTEGER NOT NULL', 'updated_at INTEGER NOT NULL',
            'started_at INTEGER', 'finished_at INTEGER', 'retry_count INTEGER NOT NULL',
            'last_error TEXT', 'runtime_id TEXT', 'session_id TEXT', 'notification_id TEXT',
            'worker_id TEXT', 'heartbeat_at INTEGER',
        ):
            with self.subTest(column=column):
                self.assertIn(column, self.source)

    def test_concurrency_and_idempotency_guards_are_present(self):
        self.assertIn('SQLiteDatabase.CONFLICT_IGNORE', self.source)
        self.assertGreaterEqual(self.source.count('db.beginTransaction()'), 3)
        self.assertGreaterEqual(self.source.count('updated_at = ?'), 3)
        self.assertIn('private fun findVersion(', self.source)
        self.assertIn('fun heartbeat(', self.source)
        self.assertIn('fun setRuntimeId(', self.source)
        self.assertIn('CREATE TABLE IF NOT EXISTS job_schedules', self.source)
        self.assertIn('fun advanceSchedule(', self.source)

    def test_transition_requires_caller_revision_token(self):
        start = self.source.index('fun transition(')
        end = self.source.index('    /** Resume a paused/failed job', start)
        transition = self.source[start:end]
        self.assertIn('expectedUpdatedAt: Long', transition)
        self.assertIn('findVersion(profileId, jobId, expected, expectedUpdatedAt)', transition)
        self.assertIn('expectedUpdatedAt.toString()', transition)

    def test_recovery_and_retry_guards_are_present(self):
        self.assertIn('private const val MAX_RETRIES = 3', self.source)
        self.assertIn('if (current.retryCount >= MAX_RETRIES)', self.source)
        self.assertIn('重试次数已达上限', self.source)
        self.assertIn('Recovery must go through resume()', self.source)
        self.assertIn('putNull("finished_at")', self.source)
        self.assertNotIn('putNull("started_at")', self.source)
        self.assertIn('private fun safeError(error: String?): String?', self.source)
        self.assertIn('putNullable("last_error", safeError(error))', self.source)


if __name__ == '__main__':
    unittest.main()
