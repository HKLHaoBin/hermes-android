"""Static tests for data-directory separation and no live WebUI-only updater."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class Issue7StaticTests(unittest.TestCase):
    def test_runtime_env_keeps_user_data_outside_payload_slots(self):
        source = (ROOT / "android/app/src/main/java/io/github/hermesandroid/RuntimeEnv.kt").read_text(encoding="utf-8")
        self.assertIn('File(context.filesDir, "home")', source)
        self.assertIn('File(context.filesDir, "logs")', source)
        self.assertNotIn("hermes-agent-slots", source)

    def test_workflow_never_uses_mutable_master_for_android_webui(self):
        source = (ROOT / ".github/workflows/build-apk.yml").read_text(encoding="utf-8")
        self.assertNotIn("ref: master", source)
        self.assertNotIn("ref: main", source)
        self.assertIn("persist-credentials: false", source)


if __name__ == "__main__":
    unittest.main()
