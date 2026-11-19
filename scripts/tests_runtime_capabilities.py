"""Source-bound prompt regressions; no Agent import, profile writes or model calls.

Usage: python scripts/tests_runtime_capabilities.py PATH_TO_PAYLOAD_ROOT
The root contains app/agent/prompt_builder.py, exactly as shipped in payload.zip.
Only the actual environment builder is executed; its unrelated host probes are
stubbed. This is component coverage, not handset or real-model acceptance.
"""
import ast
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from android_runtime_fixes import patch_environment_guidance


ROOT = Path(sys.argv.pop(1)) if len(sys.argv) > 1 else None
HEADING = 'Android app runtime capabilities'


class RuntimeCapabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if ROOT is None:
            raise RuntimeError('Pass the extracted payload root as the first argument')
        path = ROOT / 'app/agent/prompt_builder.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name == 'build_environment_hints']
        if len(tree.body) != 1:
            raise AssertionError('Expected exactly one build_environment_hints function')
        cls.code = compile(tree, str(path), 'exec')

    def render(self, android: str | None = '1', backend='local', embedder='EMBEDDER_NOTE'):
        ns = {
            'os': os,
            '_tenv_read': lambda key: backend,
            '_REMOTE_TERMINAL_BACKENDS': {'docker', 'ssh'},
            '_plugin_backend_is_remote': lambda value: value == 'custom-cloud',
            '_remote_backend_hint': lambda value: 'REMOTE:' + value,
            '_local_host_hints': lambda: ['LOCAL_HOST'],
            '_bot_screen_hint': lambda: 'BOT_SCREEN',
            'is_wsl': lambda: False,
            '_embedder_environment_hint': lambda: embedder,
        }
        with patch.dict(os.environ, {}, clear=False):
            if android is None:
                os.environ.pop('HERMES_ANDROID_APP', None)
            else:
                os.environ['HERMES_ANDROID_APP'] = android
            exec(self.code, ns)
            return ns['build_environment_hints']()

    def test_missing_or_duplicate_build_anchor_fails_without_writes(self):
        anchor = ('    hints += [WSL_ENVIRONMENT_HINT] if is_wsl() else []\n'
                  '    return "\\n\\n".join(h for h in (*hints, _embedder_environment_hint()) if h)')
        for source in ('# changed upstream\n', anchor + '\n' + anchor):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temp:
                stage = Path(temp)
                target = stage / 'app/agent/prompt_builder.py'
                target.parent.mkdir(parents=True)
                target.write_text(source, encoding='utf-8')
                before = target.read_bytes()
                with self.assertRaisesRegex(RuntimeError, 'anchor changed'):
                    patch_environment_guidance(stage)
                self.assertEqual(target.read_bytes(), before)

    def test_android_storage_and_portable_shell_guidance(self):
        text = self.render()
        self.assertIn(HEADING, text)
        for phrase in ('directory enumeration', 'os.stat', 'open/read', 'EACCES',
                       'does not prove absence', 'system file picker', 'app-private',
                       'verify', 'os.scandir', 'st_mtime', 'stderr', 'exit status',
                       '-printf', '-mmin', '-mtime', '-newermt', '-1 hour'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase.lower(), text.lower())

    def test_unavailable_tools_are_not_install_promises(self):
        text = self.render()
        for phrase in ('browser_exec', 'Chromium', 'configured remote', 'CDP',
                       'WebView', 'do not retry', 'do not guess', 'sudo',
                       'chmod', 'Shizuku', 'pkg', 'HERMES_HOME'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase.lower(), text.lower())

    def test_not_android_preserves_upstream_prompt(self):
        for flag in (None, ''):
            with self.subTest(flag=flag):
                self.assertEqual(self.render(android=flag),
                                 'LOCAL_HOST\n\nBOT_SCREEN\n\nEMBEDDER_NOTE')

    def test_android_preserves_embedder_and_host_hints(self):
        text = self.render()
        self.assertTrue(text.startswith('LOCAL_HOST\n\nBOT_SCREEN\n\n'))
        self.assertTrue(text.endswith('EMBEDDER_NOTE'))
        self.assertEqual(text.count(HEADING), 1)
        self.assertNotIn('C:\\Users', text)
        self.assertNotIn('com.termux/files/home', text)

    def test_remote_terminal_keeps_android_host_distinct(self):
        for backend in ('docker', 'ssh', 'custom-cloud'):
            with self.subTest(backend=backend):
                text = self.render(backend=backend)
                self.assertIn(HEADING, text)
                self.assertTrue(text.startswith('REMOTE:' + backend))
                self.assertNotIn('LOCAL_HOST', text)
                self.assertNotIn('BOT_SCREEN', text)
                self.assertIn('Android host', text)
                self.assertIn('remote terminal', text)
                self.assertTrue(text.endswith('EMBEDDER_NOTE'))


if __name__ == '__main__':
    unittest.main()
