#!/usr/bin/env python3
"""TDD coverage for the Issue #14 device matrix harness.

The valid receipts used here are synthetic in-memory validation fixtures only. No
fixture is a device result, and the tests never create a hardware receipt.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import android_device_matrix as matrix



def artifact_fixture() -> dict:
    return {
        "artifact_kind": matrix.ARTIFACT_KIND,
        "schema_version": matrix.SCHEMA_VERSION,
        "apk": {
            "filename": "hermes-android-test.apk",
            "sha256": "a" * 64,
            "size_bytes": 123,
            "application_id": "io.github.hkhaobin.hermeswebui",
            "version_name": "0.1.2",
            "version_code": 99,
            "signer_sha256": "b" * 64,
        },
        "build": {
            "repository": "HKLHaoBin/hermes-android",
            "workflow": "Build APK",
            "artifact_name": "apk",
            "run_id": "12345",
            "head_sha": "c" * 40,
        },
        "runtime": {
            "version": "0.23.2~canary.test-1",
            "sha256": "d" * 64,
            "source_commit": "e" * 40,
            "payload_id": "payload-test",
        },
        "webui_revision": "f" * 40,
    }



def receipt_fixture() -> dict:
    tests = [
        {
            "id": test_id,
            "status": "NOT RUN",
            "observed": "",
            "reason": "Not executed: no authorized hardware run in this fixture.",
            "failure_class": None,
            "evidence": [],
            "redaction_checked": True,
        }
        for test_id in matrix.TEST_IDS
    ]
    return {
        "receipt_kind": matrix.RECEIPT_KIND,
        "schema_version": matrix.SCHEMA_VERSION,
        "receipt_id": "fixture-receipt",
        "generated_at": "2026-11-26T00:00:00Z",
        "aggregate_status": "NOT RUN",
        "operator": {"id": "test-only", "authorization_ref": "unit-test-authorization"},
        "device": {
            "slot_id": "A13-Google",
            "device_id": "fixture-device",
            "manufacturer": "Google",
            "model": "Pixel-test",
            "oem": "Google",
            "oem_skin": "AOSP-test",
            "abi": "arm64-v8a",
            "android_release": "13",
            "api_level": 33,
            "build_fingerprint": "google/test/test:13/fixture/1234567:user/release-keys",
            "security_patch_level": "2026-01-01",
            "serial_sha256": "1" * 64,
            "serial_suffix": "1234",
            "authorized": True,
            "authorization_ref": "unit-test-authorization",
            "is_emulator": False,
            "transport": "usb",
            "webview": {
                "package": "com.test.webview",
                "version": "1.2.3",
                "provider": "com.test.webview",
            },
            "display": {"width": 1080, "height": 2400, "density": 420},
            "input": {"ime": "test-only-ime", "talkback": False},
            "battery": {"optimization": "unknown", "oem_policy": "test-only"},
            "notification_permission": "unknown",
            "network": {"mode": "controlled", "backend_id": "test-only"},
        },
        "artifact": artifact_fixture(),
        "preconditions": {
            "install_mode": "clean",
            "notification_permission": "unknown",
            "battery_optimization": "unknown",
            "network_mode": "controlled",
            "screen_state": "unlocked",
            "backend_id": "test-only",
        },
        "tests": tests,
        "redaction": {
            "policy_version": matrix.REDACTION_POLICY_VERSION,
            "raw_serial_stored": False,
            "secrets_stored": False,
            "patterns_checked": True,
        },
    }


class AndroidDeviceMatrixTests(unittest.TestCase):
    def _pass_receipt(self, slot_id="A13-Google", artifact=None):
        receipt = receipt_fixture()
        slot = next(item for item in matrix.REQUIRED_SLOTS if item["slot_id"] == slot_id)
        receipt["device"]["slot_id"] = slot_id
        receipt["device"]["api_level"] = slot["api_level"] or 33
        receipt["device"]["oem"] = "Google" if slot["oem"] == "any" else slot["oem"]
        receipt["artifact"] = copy.deepcopy(artifact or artifact_fixture())
        for test in receipt["tests"]:
            test.update(
                status="PASS",
                observed="Expected state observed.",
                reason="Expected state observed with clean evidence.",
                failure_class=None,
                evidence=[{"path": f"evidence/{test['id']}.log", "sha256": "a" * 64, "kind": "log"}],
                started_at="2026-11-26T00:00:00Z",
                finished_at="2026-11-26T00:01:00Z",
            )
        receipt["aggregate_status"] = "PASS"
        return receipt

    def _materialize_pass_receipt(self, root, slot_id="A13-Google", artifact=None):
        receipt = self._pass_receipt(slot_id, artifact)
        root.mkdir(parents=True, exist_ok=True)
        for test in receipt["tests"]:
            evidence_path = root / f"{test['id']}.log"
            evidence_path.write_text("clean hardware evidence\n", encoding="utf-8")
            test["evidence"] = [{
                "path": evidence_path.name,
                "sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
                "kind": "log",
            }]
        return receipt

    def test_receipt_schema_document_is_valid_and_has_no_raw_serial_field(self):
        schema_path = ROOT / "docs/android-device-receipt.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["receipt_kind"]["const"], matrix.RECEIPT_KIND)
        self.assertNotIn("serial", schema["$defs"]["device"]["properties"])
        self.assertIn("is_emulator", schema["$defs"]["device"]["required"])
        self.assertIn("transport", schema["$defs"]["device"]["required"])
        self.assertIn("artifact_name", schema["$defs"]["artifact"]["properties"]["build"]["required"])
        self.assertEqual(schema["$defs"]["test"]["properties"]["reason"]["minLength"], 1)
        self.assertEqual(schema["$defs"]["redaction"]["properties"]["raw_serial_stored"]["const"], False)

    def test_repository_matrix_has_four_required_api_slots_and_no_devices(self):
        matrix_data = matrix.load_matrix(matrix.DEFAULT_MATRIX_PATH)
        self.assertEqual(matrix.validate_matrix(matrix_data), [])
        required = [slot for slot in matrix_data["slots"] if slot["required"]]
        self.assertEqual({slot["api_level"] for slot in required}, {33, 34, 35, 36})
        for slot in required:
            self.assertNotIn("model", slot)
            self.assertNotIn("serial", slot)

    def test_harness_has_no_device_mutation_commands(self):
        source = (ROOT / "scripts/android_device_matrix.py").read_text(encoding="utf-8")
        self.assertNotIn('"install"', source)
        self.assertNotIn('"settings", "put"', source)
        self.assertNotIn("package_verifier_enable", source)
        self.assertNotIn('"pm", "grant"', source)

    def test_gate_checks_evidence_files_and_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "A13-Google"
            receipt = self._materialize_pass_receipt(root)
            evidence = root / "RD-ID-01.log"
            evidence.write_text("tampered hardware evidence\n", encoding="utf-8")
            matrix_data = matrix.load_matrix(matrix.DEFAULT_MATRIX_PATH)
            result = matrix.evaluate_gate([receipt], matrix_data, receipt_roots=[root])
            self.assertEqual(result["status"], "BLOCKED")
            self.assertFalse(result["eligible"])

    def test_gate_rejects_mixed_artifact_identity_across_required_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipts = []
            roots = []
            for index, slot in enumerate(item for item in matrix.REQUIRED_SLOTS if item["required"]):
                slot_root = root / slot["slot_id"]
                artifact = artifact_fixture()
                if index == 3:
                    artifact["apk"]["sha256"] = "9" * 64
                receipts.append(self._materialize_pass_receipt(slot_root, slot["slot_id"], artifact))
                roots.append(slot_root)
            result = matrix.evaluate_gate(receipts, matrix.load_matrix(matrix.DEFAULT_MATRIX_PATH), receipt_roots=roots)
            self.assertEqual(result["status"], "BLOCKED")
            self.assertFalse(result["eligible"])
            self.assertTrue(any("artifact identity" in reason for reason in result["reasons"]))

    def test_gate_accepts_required_slots_only_with_one_artifact_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipts = []
            roots = []
            for slot in (item for item in matrix.REQUIRED_SLOTS if item["required"]):
                slot_root = root / slot["slot_id"]
                receipts.append(self._materialize_pass_receipt(slot_root, slot["slot_id"]))
                roots.append(slot_root)
            result = matrix.evaluate_gate(receipts, matrix.load_matrix(matrix.DEFAULT_MATRIX_PATH), receipt_roots=roots)
            self.assertEqual(result["status"], "PASS")
            self.assertTrue(result["eligible"])

    def test_record_test_scans_evidence_bytes_before_setting_redaction_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt_path = root / "receipt.json"
            receipt_path.write_text(json.dumps(receipt_fixture()), encoding="utf-8")
            evidence = root / "logcat.txt"
            evidence.write_text("Authorization: Bearer secret-value\n", encoding="utf-8")
            with self.assertRaisesRegex(matrix.HarnessError, "redaction"):
                matrix.record_test(
                    receipt_path,
                    "RD-ID-01",
                    status="PASS",
                    observed="Evidence was collected.",
                    reason="Evidence was collected.",
                    failure_class=None,
                    evidence_paths=[evidence],
                )
            self.assertEqual(json.loads(receipt_path.read_text(encoding="utf-8"))["aggregate_status"], "NOT RUN")

    def test_validate_device_rejects_emulator_identity(self):
        receipt = receipt_fixture()
        receipt["device"]["is_emulator"] = True
        receipt["device"]["transport"] = "emulator"
        errors = matrix.validate_receipt(receipt)
        self.assertTrue(any("emulator" in error.lower() for error in errors))

    def test_capture_rejects_emulator_before_metadata_capture(self):
        output = Path(tempfile.mkdtemp()) / "receipt.json"
        with patch.object(matrix, "list_adb_devices", return_value=[{"serial": "emulator-5554", "state": "device"}]), \
             patch.object(matrix, "_run_adb", side_effect=AssertionError("emulator must be rejected before metadata capture")):
            with self.assertRaises(matrix.BlockedError):
                matrix.capture_receipt(
                    artifact=artifact_fixture(),
                    output=output,
                    slot_id="A13-Google",
                    serial=None,
                    adb="adb",
                    operator_id="operator",
                    authorization_ref="auth-record",
                    backend_id="controlled",
                )
        self.assertFalse(output.exists())

    def test_validate_device_rejects_missing_or_unknown_identity_fields(self):
        for field in ("model", "android_release", "build_fingerprint", "security_patch_level", "oem_skin"):
            with self.subTest(field=field):
                receipt = receipt_fixture()
                receipt["device"][field] = "unknown"
                self.assertTrue(matrix.validate_receipt(receipt), field)
                receipt["device"].pop(field)
                self.assertTrue(matrix.validate_receipt(receipt), field)

    def test_receipt_validator_enforces_required_fields_and_rejects_extra_fields(self):
        receipt = self._pass_receipt()
        receipt["tests"][0].pop("reason")
        self.assertTrue(any("reason" in error for error in matrix.validate_receipt(receipt)))
        receipt = self._pass_receipt()
        receipt["tests"][0].pop("failure_class")
        self.assertTrue(any("failure_class" in error for error in matrix.validate_receipt(receipt)))
        receipt = self._pass_receipt()
        receipt["tests"][0]["undeclared"] = True
        self.assertTrue(any("undeclared" in error or "additional" in error for error in matrix.validate_receipt(receipt)))
        receipt = self._pass_receipt()
        receipt["undeclared"] = True
        self.assertTrue(any("undeclared" in error or "additional" in error for error in matrix.validate_receipt(receipt)))

    def test_artifact_manifest_requires_explicit_repository_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            apk = Path(tmp) / "candidate.apk"
            apk.write_bytes(b"test-only APK bytes")
            with self.assertRaisesRegex(ValueError, "repository"):
                matrix.build_artifact_manifest(
                    apk,
                    application_id="io.github.hkhaobin.hermeswebui",
                    version_name="0.1.2",
                    version_code=99,
                    signer_sha256="b" * 64,
                    build_run_id="12345",
                    head_sha="c" * 40,
                    runtime_version="runtime-test",
                    runtime_sha256="d" * 64,
                    runtime_source_commit="e" * 40,
                    payload_id="payload-test",
                    webui_revision="f" * 40,
                )
            manifest = matrix.build_artifact_manifest(
                apk,
                application_id="io.github.hkhaobin.hermeswebui",
                version_name="0.1.2",
                version_code=99,
                signer_sha256="b" * 64,
                build_run_id="12345",
                head_sha="c" * 40,
                runtime_version="runtime-test",
                runtime_sha256="d" * 64,
                runtime_source_commit="e" * 40,
                payload_id="payload-test",
                webui_revision="f" * 40,
                owner="ExampleOwner",
                repo="example-repo",
            )
            self.assertEqual(manifest["build"]["repository"], "ExampleOwner/example-repo")

    def test_artifact_manifest_cli_accepts_explicit_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            apk = root / "candidate.apk"
            output = root / "artifact.json"
            apk.write_bytes(b"test-only APK bytes")
            result = matrix.main([
                "artifact-manifest",
                "--apk", str(apk),
                "--output", str(output),
                "--application-id", "io.github.hkhaobin.hermeswebui",
                "--version-name", "0.1.2",
                "--version-code", "99",
                "--signer-sha256", "b" * 64,
                "--build-run-id", "12345",
                "--head-sha", "c" * 40,
                "--runtime-version", "runtime-test",
                "--runtime-sha256", "d" * 64,
                "--runtime-source-commit", "e" * 40,
                "--payload-id", "payload-test",
                "--webui-revision", "f" * 40,
                "--repository", "ExampleOwner/example-repo",
            ])
            self.assertEqual(result, matrix.EXIT_PASS)
            self.assertEqual(matrix.load_json(output)["build"]["repository"], "ExampleOwner/example-repo")
            self.assertEqual(matrix.load_json(output)["build"]["artifact_name"], "apk")

    def test_validate_artifact_cli_accepts_manifest_and_rejects_missing_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            apk = root / "candidate.apk"
            output = root / "artifact.json"
            apk.write_bytes(b"test-only APK bytes")
            manifest = matrix.build_artifact_manifest(
                apk,
                application_id="io.github.hkhaobin.hermeswebui",
                version_name="0.1.2",
                version_code=99,
                signer_sha256="b" * 64,
                build_run_id="12345",
                head_sha="c" * 40,
                runtime_version="runtime-test",
                runtime_sha256="d" * 64,
                runtime_source_commit="e" * 40,
                payload_id="payload-test",
                webui_revision="f" * 40,
                repository="ExampleOwner/example-repo",
            )
            output.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(matrix.main(["validate-artifact", "--artifact", str(output)]), matrix.EXIT_PASS)
            manifest["build"].pop("artifact_name")
            output.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(matrix.main(["validate-artifact", "--artifact", str(output)]), matrix.EXIT_FAIL)

    def test_validate_matrix_checks_each_required_slot_contract(self):
        matrix_data = matrix.load_matrix(matrix.DEFAULT_MATRIX_PATH)
        expected = next(item for item in matrix.REQUIRED_SLOTS if item["slot_id"] == "A13-Google")
        for field, value in {
            "api_level": 34,
            "oem": "Samsung",
            "abi": "x86_64",
            "tier": "M1",
            "required": False,
        }.items():
            with self.subTest(field=field):
                changed = copy.deepcopy(matrix_data)
                slot = next(item for item in changed["slots"] if item["slot_id"] == expected["slot_id"])
                slot[field] = value
                self.assertTrue(matrix.validate_matrix(changed), field)

    def test_not_run_receipt_is_valid_but_not_release_eligible(self):
        receipt = receipt_fixture()
        self.assertEqual(matrix.validate_receipt(receipt), [])
        result = matrix.evaluate_gate([receipt], matrix.load_matrix(matrix.DEFAULT_MATRIX_PATH))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["eligible"])

    def test_pass_requires_observed_text_and_evidence(self):
        receipt = receipt_fixture()
        test = receipt["tests"][0]
        test.update(
            status="PASS",
            observed="Observed expected state.",
            reason="",
            evidence=[{"path": "evidence/status.json", "sha256": "a" * 64, "kind": "status"}],
            started_at="2026-11-26T00:00:00Z",
            finished_at="2026-11-26T00:01:00Z",
        )
        receipt["aggregate_status"] = "PASS"
        errors = matrix.validate_receipt(receipt)
        self.assertTrue(any("aggregate_status" in error for error in errors))
        receipt["aggregate_status"] = "NOT RUN"
        test["status"] = "NOT RUN"
        test["observed"] = ""
        test["evidence"] = []
        test["reason"] = "Not executed."
        test.pop("started_at", None)
        test.pop("finished_at", None)
        self.assertEqual(matrix.validate_receipt(receipt), [])

    def test_fail_and_blocked_require_failure_class_and_reason(self):
        receipt = receipt_fixture()
        test = receipt["tests"][0]
        test.update(
            status="FAIL",
            observed="Mismatch",
            reason="",
            failure_class=None,
            evidence=[{"path": "evidence/status.json", "sha256": "a" * 64, "kind": "status"}],
            started_at="2026-11-26T00:00:00Z",
            finished_at="2026-11-26T00:01:00Z",
        )
        receipt["aggregate_status"] = "FAIL"
        errors = matrix.validate_receipt(receipt)
        self.assertTrue(any("failure_class" in error for error in errors))
        test["failure_class"] = "app"
        self.assertTrue(any("reason" in error for error in matrix.validate_receipt(receipt)))
        test["reason"] = "Expected state was not observed."
        self.assertEqual(matrix.validate_receipt(receipt), [])

    def test_secret_redaction_rejects_raw_tokens_and_serials(self):
        receipt = receipt_fixture()
        receipt["tests"][0]["observed"] = "Authorization: Bearer should-never-be-stored"
        errors = matrix.validate_receipt(receipt)
        self.assertTrue(any("redaction" in error.lower() for error in errors))
        receipt = receipt_fixture()
        receipt["device"]["serial"] = "raw-serial"
        errors = matrix.validate_receipt(receipt)
        self.assertTrue(any("serial" in error.lower() for error in errors))

    def test_artifact_manifest_hashes_apk_and_rejects_bad_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            apk = Path(tmp) / "candidate.apk"
            payload = b"test-only APK bytes"
            apk.write_bytes(payload)
            manifest = matrix.build_artifact_manifest(
                apk,
                application_id="io.github.hkhaobin.hermeswebui",
                version_name="0.1.2",
                version_code=99,
                signer_sha256="b" * 64,
                build_run_id="12345",
                head_sha="c" * 40,
                runtime_version="runtime-test",
                runtime_sha256="d" * 64,
                runtime_source_commit="e" * 40,
                payload_id="payload-test",
                webui_revision="f" * 40,
                repository="ExampleOwner/example-repo",
            )
            self.assertEqual(manifest["apk"]["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual(matrix.validate_artifact(manifest), [])
            (Path(str(apk) + ".sha256")).write_text("0" * 64 + "  candidate.apk\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "sidecar"):
                matrix.build_artifact_manifest(
                    apk,
                    application_id="io.github.hkhaobin.hermeswebui",
                    version_name="0.1.2",
                    version_code=99,
                    signer_sha256="b" * 64,
                    build_run_id="12345",
                    head_sha="c" * 40,
                    runtime_version="runtime-test",
                    runtime_sha256="d" * 64,
                    runtime_source_commit="e" * 40,
                    payload_id="payload-test",
                    webui_revision="f" * 40,
                    repository="ExampleOwner/example-repo",
                )

    def test_capture_is_blocked_without_authorized_adb_device_and_writes_nothing(self):
        receipt = Path(tempfile.mkdtemp()) / "receipt.json"
        with patch.object(matrix, "list_adb_devices", return_value=[]):
            with self.assertRaises(matrix.BlockedError):
                matrix.capture_receipt(
                    artifact=artifact_fixture(),
                    output=receipt,
                    slot_id="A13-Google",
                    serial=None,
                    adb="adb",
                    operator_id="operator",
                    authorization_ref="auth-record",
                    backend_id="controlled",
                )
        self.assertFalse(receipt.exists())

    def test_record_test_hashes_relative_evidence_and_updates_aggregate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt_path = root / "receipt.json"
            receipt_path.write_text(json.dumps(receipt_fixture()), encoding="utf-8")
            evidence = root / "status.json"
            evidence.write_text('{"phase":"running"}\n', encoding="utf-8")
            updated = matrix.record_test(
                receipt_path,
                "RD-ID-01",
                status="PASS",
                observed="Artifact identity matched.",
                reason="Artifact identity matched.",
                failure_class=None,
                evidence_paths=[evidence],
            )
            self.assertEqual(updated["tests"][0]["status"], "PASS")
            self.assertEqual(updated["aggregate_status"], "NOT RUN")
            self.assertEqual(updated["tests"][0]["evidence"][0]["sha256"], hashlib.sha256(evidence.read_bytes()).hexdigest())
            self.assertEqual(matrix.validate_receipt(updated), [])


if __name__ == "__main__":
    unittest.main()
