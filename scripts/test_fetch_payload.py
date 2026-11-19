"""Pure package-selection/lock regressions; no network, apt or APK execution."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from fetch_payload import load_package_lock, select_package

LOCK = Path(__file__).resolve().parents[1] / 'runtime-package.lock.json'


class RuntimePackageLockTests(unittest.TestCase):
    def setUp(self):
        self.lock = load_package_lock(LOCK)

    def indexed(self):
        lock = self.lock
        return {'Package': lock['package'], 'Architecture': lock['architecture'],
                'Version': lock['version'], 'Filename': lock['filename'],
                'SHA256': lock['sha256'], 'Size': str(lock['size'])}

    def test_reviewed_lock_loads_literal_version_digest_and_source(self):
        self.assertEqual(self.lock['version'], '0.23.2~canary.20261118T064620Z-1')
        self.assertEqual(self.lock['sha256'], '47d65ab4fceb5de40ed010bc94ce73ff0ae2b411f8a3587472e909d391e7e793')
        self.assertEqual(self.lock['source_commit'], 'f7e2898aaefc8f967dbf7345e3999a65672fe60e')

    def test_rotated_index_uses_only_reviewed_old_official_pool_identity(self):
        newer = dict(self.indexed(), Version='0.23.2~canary.new')
        pkg, archived = select_package([newer], self.lock['version'], 'canary', self.lock)
        self.assertTrue(archived)
        self.assertEqual(pkg, self.indexed())
        self.assertNotEqual(pkg['Version'], newer['Version'])

    def test_matching_current_signed_index_does_not_need_archive_fallback(self):
        pkg, archived = select_package([self.indexed()], self.lock['version'], 'canary', self.lock)
        self.assertFalse(archived)
        self.assertEqual(pkg['SHA256'], self.lock['sha256'])

    def test_same_version_changed_index_identity_fails_closed(self):
        for field, value in [('SHA256', '0' * 64), ('Size', '1'), ('Filename', 'pool/h/other.deb')]:
            with self.subTest(field=field):
                pkg = dict(self.indexed(), **{field: value})
                with self.assertRaisesRegex(ValueError, 'identity differs'):
                    select_package([pkg], self.lock['version'], 'canary', self.lock)

    def test_no_lock_never_falls_back_to_guessed_package(self):
        with self.assertRaisesRegex(ValueError, 'not found'):
            select_package([], self.lock['version'], 'canary')

    def test_requested_version_or_channel_cannot_override_lock(self):
        for version, channel in [('', 'canary'), ('latest', 'canary'), (self.lock['version'], 'stable')]:
            with self.subTest(version=version, channel=channel):
                with self.assertRaisesRegex(ValueError, 'differs'):
                    select_package([], version, channel, self.lock)

    def test_unlocked_single_index_selection_preserves_existing_behavior(self):
        pkg, archived = select_package([self.indexed()], self.lock['version'], 'canary')
        self.assertEqual(pkg, self.indexed())
        self.assertFalse(archived)

    def test_malformed_lock_rejected_before_download(self):
        changes = [
            {'filename': 'https://untrusted.invalid/runtime.deb'},
            {'filename': 'pool/h/../runtime.deb'}, {'version': '../version'},
            {'package': 'other'}, {'architecture': 'x86_64'}, {'channel': 'other'},
            {'sha256': 'bad'}, {'source_commit': 'bad'}, {'size': True},
            {'size': 0}, {'size': -1}, {'size': 2**31}, {'size': '184902680'},
            {'extra': 'unexpected'},
        ]
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            p = Path(tmp) / 'lock.json'
            for change in changes:
                with self.subTest(change=change):
                    data = copy.deepcopy(self.lock)
                    data.update(change)
                    p.write_text(json.dumps(data), encoding='utf-8')
                    with self.assertRaises(ValueError):
                        load_package_lock(p)


if __name__ == '__main__':
    unittest.main()
