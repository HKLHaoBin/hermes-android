"""Executable contract for the Android job ledger state machine."""
from __future__ import annotations

import re
from pathlib import Path

STATES = frozenset({
    'queued', 'running', 'paused', 'succeeded', 'failed', 'cancelled',
})
TERMINAL = frozenset({'succeeded', 'cancelled'})
MAX_RETRIES = 3
TRANSITIONS = {
    'queued': frozenset({'running', 'paused', 'failed', 'cancelled'}),
    'running': frozenset({'paused', 'succeeded', 'failed', 'cancelled'}),
    'paused': frozenset({'queued', 'running', 'failed', 'cancelled'}),
    'failed': frozenset({'queued', 'cancelled'}),
    'succeeded': frozenset(),
    'cancelled': frozenset(),
}
def documented_fields() -> dict[str, str]:
    path = Path(__file__).resolve().parents[1] / 'docs/job-ledger.md'
    text = path.read_text(encoding='utf-8')
    table = text.split('| 字段 | 类型 | 约束 |', 1)[1].split('## State transitions', 1)[0]
    result = {}
    for line in table.splitlines():
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        if len(cells) >= 2 and cells[0] and cells[0] != '---':
            result[cells[0].strip('`')] = cells[1]
    return result


def documented_transitions() -> dict[str, frozenset[str]]:
    path = Path(__file__).resolve().parents[1] / 'docs/job-ledger.md'
    text = path.read_text(encoding='utf-8')
    block = text.split('```text', 1)[1].split('```', 1)[0]
    result = {}
    for line in block.splitlines():
        match = re.match(r'\s*(\w+)\s*->\s*(.+?)\s*$', line)
        if not match:
            continue
        old, destinations = match.groups()
        result[old] = frozenset() if '终态' in destinations else frozenset(
            item.strip() for item in destinations.split('|')
        )
    return result


DOCUMENTED_FIELDS = documented_fields()
DOCUMENTED_TRANSITIONS = documented_transitions()
REQUIRED_FIELDS = frozenset({
    'job_id', 'profile_id', 'state', 'created_at', 'updated_at', 'started_at',
    'finished_at', 'retry_count', 'last_error', 'runtime_id', 'session_id',
    'notification_id', 'worker_id', 'heartbeat_at',
})


def can_transition(old: str, new: str) -> bool:
    return old in STATES and new in TRANSITIONS[old]


def validate_record(record: dict) -> None:
    missing = REQUIRED_FIELDS - record.keys()
    if missing:
        raise ValueError(f'missing fields: {sorted(missing)}')
    if not isinstance(record['job_id'], str) or not record['job_id']:
        raise ValueError('job_id must be a non-empty string')
    if not isinstance(record['profile_id'], str) or not record['profile_id']:
        raise ValueError('profile_id must be a non-empty string')
    if record['state'] not in STATES:
        raise ValueError(f'unknown state: {record["state"]}')
    if type(record['created_at']) is not int or type(record['updated_at']) is not int:
        raise ValueError('timestamps must be integers')
    for field in ('started_at', 'finished_at', 'heartbeat_at'):
        if record[field] is not None and type(record[field]) is not int:
            raise ValueError(f'{field} must be an integer or null')
    for field in ('last_error', 'runtime_id', 'session_id', 'notification_id', 'worker_id'):
        if record[field] is not None and not isinstance(record[field], str):
            raise ValueError(f'{field} must be a string or null')
    if type(record['retry_count']) is not int or record['retry_count'] < 0 or record['retry_count'] > MAX_RETRIES:
        raise ValueError('retry_count must be between 0 and MAX_RETRIES')
    if record['updated_at'] < record['created_at']:
        raise ValueError('updated_at precedes created_at')
    if record['state'] in {'running', 'paused', 'succeeded'} and type(record['started_at']) is not int:
        raise ValueError('active or completed jobs require started_at')
    if record['state'] in TERMINAL and type(record['finished_at']) is not int:
        raise ValueError('terminal jobs require finished_at')
    if record['state'] not in TERMINAL and record['finished_at'] is not None:
        raise ValueError('non-terminal jobs cannot have finished_at')


if __name__ == '__main__':
    import unittest

    class JobLedgerContractTests(unittest.TestCase):
        def test_implementation_matches_documented_transition_table(self):
            self.assertEqual(TRANSITIONS, DOCUMENTED_TRANSITIONS)
            for old in STATES:
                for new in STATES:
                    with self.subTest(old=old, new=new):
                        self.assertEqual(can_transition(old, new), new in DOCUMENTED_TRANSITIONS[old])

        def test_implementation_matches_documented_schema(self):
            self.assertEqual(REQUIRED_FIELDS, frozenset(DOCUMENTED_FIELDS))
            expected_types = {
                'job_id': 'string', 'profile_id': 'string', 'state': 'enum',
                'created_at': 'integer', 'updated_at': 'integer',
                'started_at': 'integer/null', 'finished_at': 'integer/null',
                'retry_count': 'integer', 'last_error': 'string/null',
                'runtime_id': 'string/null', 'session_id': 'string/null',
                'notification_id': 'string/null', 'worker_id': 'string/null',
                'heartbeat_at': 'integer/null',
            }
            self.assertEqual(DOCUMENTED_FIELDS, expected_types)

        def test_record_requires_identity_and_timestamps(self):
            with self.assertRaisesRegex(ValueError, 'missing fields'):
                validate_record({'state': 'queued'})

        def test_record_rejects_invalid_state_and_time(self):
            base = {
                'job_id': 'j1', 'profile_id': 'p1', 'state': 'queued',
                'created_at': 10, 'updated_at': 10, 'started_at': None,
                'finished_at': None, 'retry_count': 0, 'last_error': None,
                'runtime_id': None, 'session_id': None, 'notification_id': None, 'worker_id': None, 'heartbeat_at': None,
            }
            invalid = dict(base, state='unknown')
            with self.assertRaisesRegex(ValueError, 'unknown state'):
                validate_record(invalid)
            invalid = dict(base, updated_at=9)
            with self.assertRaisesRegex(ValueError, 'precedes'):
                validate_record(invalid)

        def test_record_rejects_wrong_identity_timestamp_and_bool_types(self):
            base = {
                'job_id': 'j1', 'profile_id': 'p1', 'state': 'queued',
                'created_at': 10, 'updated_at': 10, 'started_at': None,
                'finished_at': None, 'retry_count': 0, 'last_error': None,
                'runtime_id': None, 'session_id': None, 'notification_id': None, 'worker_id': None, 'heartbeat_at': None,
            }
            cases = [
                (dict(base, job_id=None), 'job_id'),
                (dict(base, profile_id=3), 'profile_id'),
                (dict(base, created_at='10'), 'timestamps'),
                (dict(base, started_at='10'), 'started_at'),
                (dict(base, finished_at='10'), 'finished_at'),
                (dict(base, last_error=3), 'last_error'),
                (dict(base, runtime_id=3), 'runtime_id'),
                (dict(base, session_id=3), 'session_id'),
                (dict(base, notification_id=3), 'notification_id'),
                (dict(base, worker_id=3), 'worker_id'),
                (dict(base, heartbeat_at='10'), 'heartbeat_at'),
                (dict(base, retry_count=True), 'retry_count'),
                (dict(base, retry_count=MAX_RETRIES + 1), 'retry_count'),
            ]
            for record, message in cases:
                with self.subTest(message=message):
                    with self.assertRaisesRegex(ValueError, message):
                        validate_record(record)

        def test_record_enforces_lifecycle_timestamps(self):
            base = {
                'job_id': 'j1', 'profile_id': 'p1', 'state': 'queued',
                'created_at': 10, 'updated_at': 10, 'started_at': None,
                'finished_at': None, 'retry_count': 0, 'last_error': None,
                'runtime_id': None, 'session_id': None, 'notification_id': None, 'worker_id': None, 'heartbeat_at': None,
            }
            with self.assertRaisesRegex(ValueError, 'started_at'):
                validate_record(dict(base, state='running'))
            with self.assertRaisesRegex(ValueError, 'started_at'):
                validate_record(dict(base, state='succeeded'))
            with self.assertRaisesRegex(ValueError, 'finished_at'):
                validate_record(dict(base, state='succeeded', started_at=11))
            validate_record(dict(base, state='running', started_at=11))
            validate_record(dict(base, state='failed', last_error='任务执行失败'))
            validate_record(dict(base, state='cancelled', finished_at=12))
            validate_record(dict(base, state='succeeded', started_at=11, finished_at=12))
            with self.assertRaisesRegex(ValueError, 'non-terminal jobs'):
                validate_record(dict(base, state='running', started_at=11, finished_at=12))

        def test_record_requires_each_documented_field(self):
            base = {
                'job_id': 'j1', 'profile_id': 'p1', 'state': 'queued',
                'created_at': 10, 'updated_at': 10, 'started_at': None,
                'finished_at': None, 'retry_count': 0, 'last_error': None,
                'runtime_id': None, 'session_id': None, 'notification_id': None, 'worker_id': None, 'heartbeat_at': None,
            }
            for field in REQUIRED_FIELDS:
                with self.subTest(field=field):
                    record = dict(base)
                    del record[field]
                    with self.assertRaisesRegex(ValueError, 'missing fields'):
                        validate_record(record)

    unittest.main()
