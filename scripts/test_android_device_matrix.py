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
        "operator": {"id": "test-only", "authorization_ref": "test-only"},
        "device": {
            "slot_id": "A13-Google",
            "device_id": "test-only-device",
            "manufacturer": "test-only",
            "model": "test-only",
            "oem": "Google",
            "oem_skin": "test-only",
            "abi": "arm64-v8a",
            "android_release": "13",
            "api_level": 33,
            "build_fingerprint": "test-only/fingerprint",
            "security_patch_level": "2026-01-01",
            "serial_sha256": "1" * 64,
            "serial_suffix": "1234",
            "authorized": True,
            "authorization_ref": "test-only",
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
    def test_receipt_schema_document_is_valid_and_has_no_raw_serial_field(self):
        schema_path = ROOT / "docs/android-device-receipt.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["receipt_kind"]["const"], matrix.RECEIPT_KIND)
        self.assertNotIn("serial", schema["$defs"]["device"]["properties"])
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
                reason="",
                failure_class=None,
                evidence_paths=[evidence],
            )
            self.assertEqual(updated["tests"][0]["status"], "PASS")
            self.assertEqual(updated["aggregate_status"], "NOT RUN")
            self.assertEqual(updated["tests"][0]["evidence"][0]["sha256"], hashlib.sha256(evidence.read_bytes()).hexdigest())
            self.assertEqual(matrix.validate_receipt(updated), [])


if __name__ == "__main__":
    unittest.main()
