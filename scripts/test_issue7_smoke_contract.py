"""Contract/fixture checks for the versioned payload smoke installer."""
from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


class PayloadSmokeContractTests(unittest.TestCase):
    def test_installer_fixture_writes_versioned_slot_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "payload.zip"
            manifest = root / "payload.json"
            dest = root / "files" / "hermes-agent-slots" / "payload-a"
            state = root / "files" / "hermes-agent.active.json"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("bin/hermes", "#!/system/bin/sh\n")
            manifest.write_text(json.dumps({
                "format": 2,
                "payload_id": "payload-a",
                "placeholders": {"root": "@@ROOT@@", "prefix": "@@PREFIX@@"},
                "rewrite": [],
                "symlinks": [],
                "executables": ["bin/hermes"],
            }), encoding="utf-8")
            subprocess.run([
                "python3", str(ROOT / "scripts/install_payload.py"),
                "--zip", str(archive),
                "--manifest", str(manifest),
                "--dest", str(dest),
                "--final-root", "/files/hermes-agent-slots/payload-a",
                "--slot-root", str(root / "files" / "hermes-agent-slots"),
                "--state", str(state),
            ], check=True, capture_output=True)
            data = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(data["active"], "payload-a")
            self.assertFalse(data["pending"])
            self.assertTrue((dest / ".payload_manifest.json").is_file())

    def test_smoke_uses_versioned_slot_not_legacy_root(self):
        installer = (ROOT / "scripts/install_payload.py").read_text(encoding="utf-8")
        workflow = (ROOT / ".github/workflows/build-apk.yml").read_text(encoding="utf-8")
        smoke = (ROOT / "scripts/smoke.sh").read_text(encoding="utf-8")
        for needle in ("--slot-root", "--state", "hermes-agent.active.json", "hermes-agent-slots"):
            self.assertIn(needle, installer + workflow + smoke)
        self.assertIn("--simulate-health-failure", workflow)
        self.assertIn('s["rejected"]', workflow)
        self.assertIn('sed -n', smoke)
        self.assertIn("hermes-agent-slots/$SLOT", smoke)

    def test_versioned_smoke_state_rejects_failed_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            slot_root = root / "files" / "hermes-agent-slots"
            state = root / "files" / "hermes-agent.active.json"

            def make_payload(payload_id: str, invalid: bool = False):
                archive = root / f"{payload_id}.zip"
                manifest = root / f"{payload_id}.json"
                with zipfile.ZipFile(archive, "w") as zf:
                    zf.writestr("../escape" if invalid else "bin/hermes", "payload")
                manifest.write_text(json.dumps({
                    "format": 2,
                    "payload_id": payload_id,
                    "placeholders": {"root": "@@ROOT@@", "prefix": "@@PREFIX@@"},
                    "rewrite": [],
                    "symlinks": [],
                    "executables": [],
                }), encoding="utf-8")
                return archive, manifest

            def install(payload_id: str, invalid: bool = False):
                archive, manifest = make_payload(payload_id, invalid)
                return subprocess.run([
                    "python3", str(ROOT / "scripts/install_payload.py"),
                    "--zip", str(archive), "--manifest", str(manifest),
                    "--dest", str(slot_root / payload_id),
                    "--final-root", f"/files/hermes-agent-slots/{payload_id}",
                    "--slot-root", str(slot_root), "--state", str(state),
                ], capture_output=True, text=True)

            self.assertEqual(install("payload-a").returncode, 0)
            self.assertEqual(install("payload-b").returncode, 0)
            failed = install("payload-c", invalid=True)
            self.assertNotEqual(failed.returncode, 0)
            data = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(data["active"], "payload-b")
            self.assertEqual(data["previous_good"], "payload-a")
            self.assertIsNone(data["rejected"])
            self.assertFalse((slot_root / "payload-c").exists())

    def test_versioned_smoke_can_probe_health_failure_and_restore_active_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            slot_root = root / "files" / "hermes-agent-slots"
            state = root / "files" / "hermes-agent.active.json"
            archive = root / "payload.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("bin/hermes", "payload")

            def manifest(payload_id: str) -> Path:
                path = root / f"{payload_id}.json"
                path.write_text(json.dumps({
                    "format": 2,
                    "payload_id": payload_id,
                    "placeholders": {"root": "@@ROOT@@", "prefix": "@@PREFIX@@"},
                    "rewrite": [], "symlinks": [], "executables": [],
                }), encoding="utf-8")
                return path

            def run(payload_id: str, *extra: str):
                return subprocess.run([
                    "python3", str(ROOT / "scripts/install_payload.py"),
                    "--zip", str(archive), "--manifest", str(manifest(payload_id)),
                    "--dest", str(slot_root / payload_id),
                    "--final-root", f"/files/hermes-agent-slots/{payload_id}",
                    "--slot-root", str(slot_root), "--state", str(state), *extra,
                ], capture_output=True, text=True)

            self.assertEqual(run("payload-good").returncode, 0)
            self.assertEqual(run("payload-probe", "--simulate-health-failure").returncode, 0)
            data = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(data["active"], "payload-good")
            self.assertEqual(data["rejected"], "payload-probe")
            self.assertFalse(data["pending"])
            self.assertFalse((slot_root / "payload-probe").exists())


if __name__ == "__main__":
    unittest.main()
