"""Test fail-closed parent admission independently of any parent implementation."""
from contextlib import redirect_stdout
from io import StringIO
from itertools import combinations
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import issue7_integration_gate as gate


class ParentGateTests(unittest.TestCase):
    REQUIRED = (
        "scripts/test_capabilities.py",
        "android/app/src/main/java/io/github/hermesandroid/JobLedger.kt",
        "scripts/test_job_ledger_contract.py",
        "scripts/test_job_ledger_source.py",
    )

    def make_files(self, root, paths):
        for name in paths:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# fixture only\n", encoding="utf-8")

    def test_complete_parent_contract_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_files(root, self.REQUIRED)
            self.assertTrue(gate.validate_parent_surface(root))

    def test_every_nonempty_incomplete_combination_is_rejected(self):
        for count in (1, 2, 3):
            for present in combinations(self.REQUIRED, count):
                with self.subTest(present=present), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self.make_files(root, present)
                    with self.assertRaisesRegex(ValueError, "Missing parent contract"):
                        gate.validate_parent_surface(root)

    def test_required_mode_rejects_even_a_completely_missing_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "Missing parent contract"):
                gate.validate_parent_surface(Path(tmp), require_parent=True)

    def test_auto_mode_labels_a_genuinely_standalone_checkout(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(StringIO()) as output:
            self.assertFalse(gate.validate_parent_surface(Path(tmp)))
            self.assertIn("STANDALONE", output.getvalue())

    def test_parent_source_without_test_sentinels_is_not_standalone(self):
        markers = {
            "docs/capabilities.md": "# parent fixture\n",
            "docs/job-ledger.md": "# parent fixture\n",
            "android/app/src/main/java/io/github/hermesandroid/HermesRuntime.kt": '    private fun capabilitiesJson() = TODO()\n',
            "android/app/src/main/java/io/github/hermesandroid/HermesService.kt": '    val ledger = JobLedger(this)\n',
            "android/app/src/main/assets/ui/index.html": '<div id="capabilities"></div>\n',
        }
        for name, content in markers.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make_files(root, (name,))
                (root / name).write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Missing parent contract"):
                    gate.validate_parent_surface(root)

    def test_missing_files_never_invoke_parent_test_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_files(root, (self.REQUIRED[2],))
            with patch.object(gate.subprocess, "run") as run, redirect_stdout(StringIO()):
                self.assertNotEqual(gate.main(["--root", str(root)]), 0)
                run.assert_not_called()

    def test_complete_gate_runs_strict_and_all_parent_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_files(root, self.REQUIRED)
            with patch.object(gate.subprocess, "run") as run:
                run.return_value.returncode = 0
                self.assertEqual(gate.main(["--root", str(root), "--require-parent"]), 0)
                commands = [c.args[0] for c in run.call_args_list]
                self.assertEqual(len(commands), 4)
                self.assertIn("scripts.test_issue7_integration_contract", commands[0])
                self.assertEqual([c[-1] for c in commands[1:]], [str(root / p) for p in (self.REQUIRED[0], self.REQUIRED[2], self.REQUIRED[3])])
                for call in run.call_args_list:
                    self.assertEqual(call.kwargs["cwd"], root)

    def test_parent_test_failure_stops_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_files(root, self.REQUIRED)
            with patch.object(gate.subprocess, "run") as run:
                run.return_value.returncode = 17
                self.assertEqual(gate.main(["--root", str(root)]), 17)
                self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
