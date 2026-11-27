"""Regression checks for the Android capability declaration and diagnostics UI."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'android/app/src/main/java/io/github/hermesandroid/HermesRuntime.kt'
INDEX = ROOT / 'android/app/src/main/assets/ui/index.html'
APP = ROOT / 'android/app/src/main/assets/ui/app.js'

EXPECTED = {
    'webui', 'runtime', 'cli_tui', 'terminal', 'skills', 'plugins', 'mcp',
    'browser', 'computer_use', 'background_service', 'job_queue',
    'backup_restore', 'real_device_validation',
}
VALID_STATES = {'supported', 'partial', 'experimental', 'unavailable'}


class CapabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = RUNTIME.read_text(encoding='utf-8')
        cls.index = INDEX.read_text(encoding='utf-8')
        cls.app = APP.read_text(encoding='utf-8')

    def test_runtime_declares_each_expected_capability_once(self):
        found = re.findall(r'capability\(this,\s*"([a-z_]+)",\s*"([a-z]+)"', self.runtime)
        ids = {item_id for item_id, _ in found}
        self.assertEqual(ids, EXPECTED)
        self.assertEqual(len(found), len(EXPECTED))
        self.assertTrue({state for _, state in found} <= VALID_STATES)

    def test_status_json_exposes_capabilities(self):
        self.assertIn('put("capabilities", capabilitiesJson())', self.runtime)
        self.assertIn('put("androidApi", android.os.Build.VERSION.SDK_INT)', self.runtime)
        self.assertIn('put("abi", android.os.Build.SUPPORTED_ABIS.firstOrNull() ?: "unknown")', self.runtime)
        self.assertIn("fetch('/__android/status.json'", self.app)
        self.assertIn('status.capabilities || []', self.app)

    def test_ui_has_single_capability_container_and_all_labels(self):
        self.assertEqual(self.index.count('id="capabilities"'), 1)
        for capability_id in EXPECTED:
            self.assertIn(f'{capability_id}:', self.app)
        self.assertIn('capabilityStates', self.app)
        self.assertIn('cap-row ', self.app)


if __name__ == '__main__':
    unittest.main()
