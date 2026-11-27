#!/usr/bin/env python3
"""TDD coverage for the Issue #14 device matrix harness.

The valid receipts used here are synthetic in-memory validation fixtures only. No
fixture is a device result, and the tests never create a hardware receipt.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
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


def installed_apk_fixture(artifact=None) -> dict:
    apk = (artifact or artifact_fixture())["apk"]
    return {
        "package_path": "/data/app/~~captured/base.apk",
        "sha256": apk["sha256"],
        "size_bytes": apk["size_bytes"],
        "application_id": apk["application_id"],
        "version_name": apk["version_name"],
        "version_code": apk["version_code"],
    }


def precondition_fixture(field: str, value: str, evidence: str | None = None) -> dict:
    return {
        "value": value, "source": matrix._PRECONDITION_SOURCES[field],
        "observed_at": "2026-11-26T00:00:00Z", "evidence": value if evidence is None else evidence,
    }


def proof_fixture(receipt, test):
    test_id = test["id"]
    expected = matrix.TEST_CONTRACT[test_id]["expected"]
    measurements = {
        "RD-MIGRATION-01": {"schema_before": 1, "schema_after": 2, "records_before": 3, "records_after": 3,
                            "data_before_sha256": "4" * 64, "data_after_sha256": "4" * 64},
        "RD-MEMORY-01": {"pressure_event": "trim-memory-running-critical", "pressure_at": "2026-11-26T00:00:30Z",
                         "job_id": "job-123", "ledger_preserved": True, "terminal_state": "running"},
        "RD-PROCESS-01": {"pid_before": 101, "pid_after": 102, "recreated_at": "2026-11-26T00:00:30Z",
                          "session_before": "session-123", "session_after": "session-123", "service_state": "ready"},
        "RD-LARGE-01": {"minimum_dimension_dp": receipt["device"]["screen_capability"]["minimum_dimension_dp"],
                        "surfaces": {"messages": True, "composer": True, "tabs": True, "console-frame": True}},
    }.get(test_id, {"observed_state": expected})
    result = {
        "evidence_schema_version": matrix.EVIDENCE_SCHEMA_VERSION, "kind": "json", "test_id": test_id,
        "receipt_id": receipt["receipt_id"], "expected": expected, "observed": expected,
        "device": {"slot_id": receipt["device"]["slot_id"], "device_id": receipt["device"]["device_id"],
                   "serial_sha256": receipt["device"]["serial_sha256"], "identity_sha256": matrix._device_identity_digest(receipt["device"])},
        "artifact": {"filename": receipt["artifact"]["apk"]["filename"], "sha256": receipt["artifact"]["apk"]["sha256"],
                     "identity_sha256": matrix._artifact_identity_digest(receipt["artifact"])},
        "preconditions_sha256": matrix._stable_digest(receipt["preconditions"]),
        "scenario": {"name": matrix.SCENARIO_CONTRACT[test_id]["name"]},
        "assertions": [{"name": name, "expected": expected, "observed": expected, "passed": True}
                       for name in matrix.SCENARIO_CONTRACT[test_id]["assertions"]],
        "measurements": measurements,
        "started_at": "2026-11-26T00:00:00Z", "finished_at": "2026-11-26T00:01:00Z",
    }
    if test_id == "RD-LARGE-01":
        result["capability"] = copy.deepcopy(receipt["device"]["screen_capability"])
    return result


def receipt_fixture() -> dict:
    tests = [
        {
            "id": test_id,
            "status": "NOT RUN",
            "expected": matrix.TEST_CONTRACT[test_id]["expected"],
            "observed": "",
            "reason": "Not executed: no authorized hardware run in this fixture.",
            "failure_class": None,
            "installed_apk": installed_apk_fixture(),
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
        "operator": {"id": "operator-01", "authorization_ref": "unit-test-authorization"},
        "device": {
            "slot_id": "A13-Google",
            "device_id": "pixel-serial-redacted",
            "manufacturer": "Google",
            "model": "Pixel 8",
            "oem": "Google",
            "oem_skin": "AOSP-2026",
            "abi": "arm64-v8a",
            "android_release": "13",
            "api_level": 33,
            "build_fingerprint": "google/pixel/pixel8:13/2026/1234567:user/release-keys",
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
            "screen_capability": {"kind": "phone", "minimum_dimension_dp": 411, "source": "adb:wm"},
            "input": {"ime": "com.google.android.inputmethod.latin", "talkback": False},
            "battery": {"optimization": "optimized", "oem_policy": "reported-by-deviceidle"},
            "notification_permission": "default",
            "network": {"mode": "connected", "backend_id": "controlled-lab"},
        },
        "artifact": artifact_fixture(),
        "preconditions": {
            "install_mode": precondition_fixture("install_mode", "installed", installed_apk_fixture()["package_path"]),
            "notification_permission": precondition_fixture("notification_permission", "default"),
            "battery_optimization": precondition_fixture("battery_optimization", "optimized"),
            "network_mode": precondition_fixture("network_mode", "connected"),
            "screen_state": precondition_fixture("screen_state", "interactive", "mWakefulness=Awake"),
            "backend_id": precondition_fixture("backend_id", "controlled-lab"),
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
        receipt["device"]["display"] = {"width": 1600, "height": 2560, "density": 320}
        receipt["device"]["screen_capability"] = {"kind": "large-screen", "minimum_dimension_dp": 800, "source": "adb:wm"}
        receipt["artifact"] = copy.deepcopy(artifact or artifact_fixture())
        for test in receipt["tests"]:
            test.update(
                status="PASS",
                expected=matrix.TEST_CONTRACT[test["id"]]["expected"],
                observed=matrix.TEST_CONTRACT[test["id"]]["expected"],
                reason="Expected state observed with clean evidence.",
                failure_class=None,
                installed_apk=installed_apk_fixture(receipt["artifact"]),
                evidence=[{"path": f"evidence/{test['id']}.log", "sha256": "a" * 64, "kind": "json"}],
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
            evidence_path.write_text(json.dumps(proof_fixture(receipt, test)), encoding="utf-8")
            test["evidence"] = [{
                "path": evidence_path.name,
                "sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
                "kind": "json",
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
        self.assertIn("expected", schema["$defs"]["test"]["required"])
        self.assertIn("installed_apk", schema["$defs"]["test"]["required"])
        self.assertEqual(schema["properties"]["tests"]["minItems"], len(matrix.TEST_IDS))
        self.assertIn("package_path", schema["$defs"]["test"]["properties"]["installed_apk"]["required"])
        self.assertIn("allOf", schema["$defs"]["test"])
        self.assertTrue(any("started_at" in branch.get("then", {}).get("required", []) for branch in schema["$defs"]["test"]["allOf"]))
        self.assertNotIn("unknown", schema["$defs"]["device"]["properties"]["battery"]["properties"]["optimization"]["enum"])
        self.assertNotIn("unknown", schema["$defs"]["device"]["properties"]["notification_permission"]["enum"])
        self.assertEqual(schema["$defs"]["test"]["properties"]["reason"]["minLength"], 1)
        self.assertEqual(schema["$defs"]["redaction"]["properties"]["raw_serial_stored"]["const"], False)

    def test_repository_matrix_has_required_api_slots_and_large_screen_gate(self):
        matrix_data = matrix.load_matrix(matrix.DEFAULT_MATRIX_PATH)
        self.assertEqual(matrix.validate_matrix(matrix_data), [])
        required_scenarios = {"RD-MIGRATION-01", "RD-MEMORY-01", "RD-PROCESS-01", "RD-LARGE-01"}
        self.assertTrue(required_scenarios.issubset(matrix.TEST_IDS))
        self.assertTrue(required_scenarios.issubset(matrix_data["test_ids"]))
        required = [slot for slot in matrix_data["slots"] if slot["required"]]
        self.assertEqual({slot["api_level"] for slot in required if slot["api_level"]}, {33, 34, 35, 36})
        self.assertTrue(next(slot for slot in required if slot["slot_id"] == "Large-screen")["required"])
        for slot in required:
            self.assertNotIn("model", slot)
            self.assertNotIn("serial", slot)

    def test_required_scenarios_reject_unbound_static_json_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "A13-Google"
            receipt = self._materialize_pass_receipt(root)
            test = receipt["tests"][0]
            evidence = root / "RD-ID-01.json"
            evidence.write_text('{"phase":"running"}\n', encoding="utf-8")
            test["evidence"] = [{
                "path": evidence.name,
                "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                "kind": "json",
            }]
            errors = matrix.validate_receipt(receipt, receipt_root=root, check_evidence=True)
            self.assertTrue(any("structured" in error or "bound" in error or "scenario" in error for error in errors))

    def test_large_screen_slot_requires_measurable_capability_identity(self):
        receipt = self._pass_receipt("Large-screen")
        receipt["device"]["display"] = {"width": 1080, "height": 2400, "density": 420}
        receipt["device"]["screen_capability"] = {"kind": "phone", "minimum_dimension_dp": 411, "source": "adb:wm"}
        errors = matrix.validate_receipt(receipt)
        self.assertTrue(any("large-screen" in error or "screen" in error for error in errors))

    def test_preconditions_require_captured_enum_values_not_operator_reminders(self):
        receipt = receipt_fixture()
        receipt["preconditions"]["install_mode"] = "operator-set-before-run"
        receipt["preconditions"]["screen_state"] = "operator-set-before-run"
        errors = matrix.validate_receipt(receipt)
        self.assertTrue(any("install_mode" in error or "screen_state" in error for error in errors))

    def test_structured_json_evidence_rejects_raw_serial_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "A13-Google"
            receipt = self._materialize_pass_receipt(root)
            evidence = root / "RD-ID-01.json"
            evidence.write_text('{"serial":"raw-device-serial"}\n', encoding="utf-8")
            receipt["tests"][0]["evidence"] = [{
                "path": evidence.name,
                "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                "kind": "json",
            }]
            errors = matrix.validate_receipt(receipt, receipt_root=root, check_evidence=True)
            self.assertTrue(any("serial" in error.lower() or "redaction" in error.lower() for error in errors))

    def test_schema_and_runtime_both_reject_uppercase_hashes_and_placeholder_preconditions(self):
        receipt = receipt_fixture()
        receipt["artifact"]["apk"]["sha256"] = "A" * 64
        receipt["preconditions"]["install_mode"] = "operator-set-before-run"
        errors = matrix.validate_receipt(receipt)
        self.assertTrue(any("sha256" in error for error in errors))
        schema = json.loads((ROOT / "docs/android-device-receipt.schema.json").read_text(encoding="utf-8"))
        self.assertIsNone(re.fullmatch(schema["$defs"]["sha256"]["pattern"], "A" * 64))
        precondition_schema = schema["$defs"]["preconditions"]["properties"]["install_mode"]["properties"]["value"]
        self.assertIn("enum", precondition_schema)
        self.assertNotIn("operator-set-before-run", precondition_schema["enum"])

    def test_proof_envelopes_bind_every_required_scenario_to_exact_receipt_device_and_artifact(self):
        mutations = (
            ("test_id", "RD-ID-01"), ("receipt_id", "another-run"),
            ("device.device_id", "another-device"), ("device.identity_sha256", "0" * 64),
            ("artifact.sha256", "0" * 64), ("artifact.identity_sha256", "0" * 64),
            ("started_at", "2026-11-25T00:00:00Z"), ("preconditions_sha256", "0" * 64),
            ("scenario.name", "unrelated-scenario"), ("measurements", {}),
        )
        for test_id in ("RD-MIGRATION-01", "RD-MEMORY-01", "RD-PROCESS-01", "RD-LARGE-01"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                baseline = self._materialize_pass_receipt(root, "Large-screen")
                self.assertEqual(matrix.validate_receipt(baseline, receipt_root=root, check_evidence=True), [])
                for path, value in mutations:
                    with self.subTest(test_id=test_id, path=path):
                        receipt = copy.deepcopy(baseline)
                        test = next(item for item in receipt["tests"] if item["id"] == test_id)
                        proof = proof_fixture(receipt, test)
                        parent = proof
                        keys = path.split(".")
                        for key in keys[:-1]:
                            parent = parent[key]
                        parent[keys[-1]] = value
                        evidence = root / test["evidence"][0]["path"]
                        evidence.write_text(json.dumps(proof), encoding="utf-8")
                        test["evidence"][0]["sha256"] = matrix.sha256_file(evidence)
                        errors = matrix.validate_receipt(receipt, receipt_root=root, check_evidence=True)
                        self.assertTrue(any(test_id in error for error in errors), errors)

    def test_scenario_measurements_reject_static_success_flags_and_invalid_lifecycle_observations(self):
        mutations = {
            "RD-MIGRATION-01": {"schema_after": 1, "records_after": 2, "data_after_sha256": "5" * 64},
            "RD-MEMORY-01": {"pressure_event": "passed", "pressure_at": "2026-11-25T00:00:00Z", "ledger_preserved": False},
            "RD-PROCESS-01": {"pid_after": 101, "session_after": "different-session", "recreated_at": "2026-11-25T00:00:00Z"},
            "RD-LARGE-01": {"minimum_dimension_dp": 411, "surfaces": {"messages": True}},
        }
        for test_id, changes in mutations.items():
            receipt = self._pass_receipt("Large-screen")
            test = next(item for item in receipt["tests"] if item["id"] == test_id)
            for field, value in changes.items():
                with self.subTest(test_id=test_id, field=field):
                    proof = proof_fixture(receipt, test)
                    proof["measurements"][field] = value
                    errors = matrix._structured_evidence_errors(json.dumps(proof).encode(), "proof.json", item={"kind": "json"}, test=test, receipt=receipt)
                    self.assertTrue(any(test_id in error for error in errors), errors)
            proof = proof_fixture(receipt, test)
            proof["assertions"][0]["passed"] = False
            self.assertTrue(matrix._structured_evidence_errors(json.dumps(proof).encode(), "proof.json", item={"kind": "json"}, test=test, receipt=receipt))

    def test_large_screen_capability_is_recomputed_not_trusted_and_proof_must_match(self):
        receipt = self._pass_receipt("Large-screen")
        receipt["device"]["display"] = {"width": 1080, "height": 2400, "density": 420}
        self.assertTrue(any("measured" in error for error in matrix.validate_receipt(receipt)))
        self.assertEqual(matrix._screen_capability(1080, 2400, 420)["kind"], "phone")
        self.assertEqual(matrix._screen_capability(1440, 3200, 560)["kind"], "phone")
        receipt = self._pass_receipt("Large-screen")
        test = next(item for item in receipt["tests"] if item["id"] == "RD-LARGE-01")
        proof = proof_fixture(receipt, test)
        proof["capability"]["minimum_dimension_dp"] = 411
        self.assertTrue(matrix._structured_evidence_errors(json.dumps(proof).encode(), "proof.json", item={"kind": "json"}, test=test, receipt=receipt))

    def test_raw_serial_policy_applies_to_json_nested_fields_text_and_actual_serial_tokens(self):
        forbidden = (
            b'{"serial":"raw-device-serial"}', b'{"nested":[{"serial":"raw-device-serial"}]}',
            b'{"\\u0073erial":"raw-device-serial"}', b'serial=raw-device-serial',
            b'ro.serialno=raw-device-serial', b'adb -s raw-device-serial shell getprop',
            b'raw-device-serial\tdevice\n',
        )
        for data in forbidden:
            with self.subTest(data_kind=data[:20]):
                self.assertTrue(matrix._evidence_content_findings(data, "evidence"))
        digest = hashlib.sha256(b"ABCD12345678").hexdigest()
        self.assertTrue(matrix._evidence_content_findings(b"Observed ABCD12345678 in a log.", "evidence", digest))
        self.assertEqual(matrix._evidence_content_findings(b'{"serial_sha256":"aabb","serial_suffix":"1234"}', "evidence"), [])

    def test_record_rejects_unbound_proof_without_overwriting_the_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt_path = root / "receipt.json"
            original = json.dumps(receipt_fixture())
            receipt_path.write_text(original, encoding="utf-8")
            evidence = root / "unbound.json"
            evidence.write_text('{"passed":true}', encoding="utf-8")
            with self.assertRaisesRegex(matrix.HarnessError, "structured"):
                matrix.record_test(receipt_path, "RD-ID-01", status="PASS", observed=matrix.TEST_CONTRACT["RD-ID-01"]["expected"],
                                   reason="Claimed result", failure_class=None, evidence_paths=[evidence])
            self.assertEqual(receipt_path.read_text(encoding="utf-8"), original)

    def test_display_parsers_use_the_effective_override_measurements(self):
        self.assertEqual(matrix._parse_display("Physical size: 2400x3200\nOverride size: 1080x2400\n"), (1080, 2400))
        self.assertEqual(matrix._parse_density("Physical density: 320\nOverride density: 420\n"), 420)

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

    def test_standalone_capture_and_gate_require_canonical_actions_artifact_identity(self):
        artifact = artifact_fixture()
        artifact["build"]["repository"] = "OtherOwner/other-repo"
        artifact["build"]["workflow"] = "Other workflow"
        artifact["build"]["artifact_name"] = "other"
        artifact["apk"]["filename"] = "other.apk"
        self.assertTrue(matrix.validate_artifact(artifact) == [])
        self.assertTrue(matrix.validate_release_artifact(artifact))
        output = Path(tempfile.mkdtemp()) / "receipt.json"
        with patch.object(matrix, "list_adb_devices", return_value=[]):
            with self.assertRaises(matrix.HarnessError):
                matrix.capture_receipt(
                    artifact=artifact,
                    output=output,
                    slot_id="A13-Google",
                    serial=None,
                    adb="adb",
                    operator_id="operator",
                    authorization_ref="auth-record",
                    backend_id="controlled",
                )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "A13-Google"
            receipt = self._materialize_pass_receipt(root)
            receipt["artifact"] = artifact
            result = matrix.evaluate_gate([receipt], matrix.load_matrix(matrix.DEFAULT_MATRIX_PATH), receipt_roots=[root])
            self.assertEqual(result["status"], "BLOCKED")
            self.assertFalse(result["eligible"])

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

    def test_optional_failures_are_reported_without_blocking_required_release_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipts = []
            roots = []
            for slot in (item for item in matrix.REQUIRED_SLOTS if item["required"]):
                slot_root = root / slot["slot_id"]
                receipts.append(self._materialize_pass_receipt(slot_root, slot["slot_id"]))
                roots.append(slot_root)
            optional_root = root / "OEM-stress"
            optional = self._materialize_pass_receipt(optional_root, "OEM-stress")
            failed = optional["tests"][0]
            failed.update(
                status="FAIL",
                observed="Observed optional OEM stress failure.",
                reason="Optional OEM stress scenario did not meet its observation.",
                failure_class="oem",
            )
            proof_path = optional_root / failed["evidence"][0]["path"]
            proof = proof_fixture(optional, failed)
            proof["observed"] = failed["observed"]
            proof_path.write_text(json.dumps(proof), encoding="utf-8")
            failed["evidence"][0]["sha256"] = hashlib.sha256(proof_path.read_bytes()).hexdigest()
            optional["aggregate_status"] = "FAIL"
            self.assertEqual(matrix.validate_receipt(optional), [])
            receipts.append(optional)
            roots.append(optional_root)

            result = matrix.evaluate_gate(receipts, matrix.load_matrix(matrix.DEFAULT_MATRIX_PATH), receipt_roots=roots)

            self.assertEqual(result["status"], "PASS")
            self.assertTrue(result["eligible"])
            self.assertEqual(result["optional_failures"][0]["slot_id"], "OEM-stress")
            self.assertEqual(result["optional_failures"][0]["status"], "FAIL")

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

    def test_receipt_metadata_rejects_serial_digest_matches_and_short_serial_suffix(self):
        raw_serial = "raw-device-serial"
        receipt = receipt_fixture()
        receipt["device"]["serial_sha256"] = hashlib.sha256(raw_serial.encode("utf-8")).hexdigest()
        receipt["device"]["device_id"] = raw_serial
        errors = matrix.validate_receipt(receipt)
        self.assertTrue(any("serial" in error.lower() for error in errors), errors)

        short_serial = "ABCD1234"
        receipt = receipt_fixture()
        receipt["device"]["serial_sha256"] = hashlib.sha256(short_serial.encode("utf-8")).hexdigest()
        receipt["device"]["serial_suffix"] = short_serial
        errors = matrix.validate_receipt(receipt)
        self.assertTrue(any("serial" in error.lower() for error in errors), errors)

        output = Path(tempfile.mkdtemp()) / "short-serial.json"
        with patch.object(matrix, "list_adb_devices", return_value=[{"serial": short_serial, "state": "device"}]), \
             patch.object(matrix, "_run_adb", side_effect=AssertionError("short serial must be blocked before metadata capture")):
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

    def test_receipt_requires_expected_state_evidence_kind_and_exact_installed_apk(self):
        receipt = self._pass_receipt()
        self.assertTrue(hasattr(matrix, "TEST_CONTRACT"))
        for test in receipt["tests"]:
            contract = matrix.TEST_CONTRACT[test["id"]]
            test["expected"] = contract["expected"]
            test["installed_apk"] = installed_apk_fixture(receipt["artifact"])
            test["evidence"][0]["kind"] = "json"
        self.assertEqual(matrix.validate_receipt(receipt), [])

        receipt["tests"][0]["expected"] = "arbitrary operator expectation"
        self.assertTrue(any("expected" in error for error in matrix.validate_receipt(receipt)))
        receipt = self._pass_receipt()
        for test in receipt["tests"]:
            contract = matrix.TEST_CONTRACT[test["id"]]
            test["expected"] = contract["expected"]
            test["installed_apk"] = installed_apk_fixture(receipt["artifact"])
            test["evidence"][0]["kind"] = "json"
        receipt["tests"][0]["evidence"][0]["kind"] = "arbitrary-text"
        self.assertTrue(any("evidence" in error for error in matrix.validate_receipt(receipt)))
        receipt["tests"][0]["evidence"][0]["kind"] = matrix.TEST_CONTRACT["RD-ID-01"]["evidence_kinds"][0]
        receipt["tests"][0]["installed_apk"]["sha256"] = "0" * 64
        self.assertTrue(any("installed_apk" in error or "artifact" in error for error in matrix.validate_receipt(receipt)))
        receipt = self._pass_receipt()
        receipt["tests"][0]["installed_apk"] = copy.deepcopy(receipt["artifact"]["apk"])
        self.assertTrue(any("installed_apk" in error or "package_path" in error for error in matrix.validate_receipt(receipt)))

    def test_runtime_validator_matches_schema_types_transport_and_suffix_contract(self):
        receipt = receipt_fixture()
        receipt["device"]["display"]["width"] = True
        self.assertTrue(any("display.width" in error for error in matrix.validate_receipt(receipt)))
        receipt = receipt_fixture()
        receipt["device"]["transport"] = "bluetooth"
        self.assertTrue(any("transport" in error for error in matrix.validate_receipt(receipt)))
        receipt = receipt_fixture()
        receipt["device"]["serial_suffix"] = "123456789"
        self.assertTrue(any("serial_suffix" in error for error in matrix.validate_receipt(receipt)))

    def test_unknown_battery_notification_and_oem_preconditions_are_not_release_valid(self):
        receipt = self._pass_receipt()
        receipt["device"]["battery"]["optimization"] = "unknown"
        receipt["device"]["battery"]["oem_policy"] = "unknown"
        receipt["device"]["notification_permission"] = "unknown"
        receipt["preconditions"]["battery_optimization"] = "unknown"
        receipt["preconditions"]["notification_permission"] = "unknown"
        errors = matrix.validate_receipt(receipt)
        self.assertTrue(any("unknown" in error.lower() or "precondition" in error.lower() for error in errors))

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
            evidence=[{"path": "evidence/status.json", "sha256": "a" * 64, "kind": "json"}],
            started_at="2026-11-26T00:00:00Z",
            finished_at="2026-11-26T00:01:00Z",
        )
        receipt["aggregate_status"] = "PASS"
        errors = matrix.validate_receipt(receipt)
        self.assertTrue(any("aggregate_status" in error or "observed" in error for error in errors))
        receipt["aggregate_status"] = "NOT RUN"
        test["status"] = "NOT RUN"
        test["observed"] = ""
        test["evidence"] = []
        test["reason"] = "Not executed."
        test.pop("started_at", None)
        test.pop("finished_at", None)
        self.assertEqual(matrix.validate_receipt(receipt), [])

    def test_pass_rejects_zero_byte_evidence_and_arbitrary_observed_text(self):
        receipt = receipt_fixture()
        test = receipt["tests"][0]
        test.update(
            status="PASS",
            observed="operator says it passed",
            reason="Observed state was reported.",
            evidence=[{"path": "RD-ID-01.log", "sha256": "a" * 64, "kind": "log"}],
            started_at="2026-11-26T00:00:00Z",
            finished_at="2026-11-26T00:01:00Z",
        )
        receipt["aggregate_status"] = "NOT RUN"
        self.assertTrue(any("observed" in error for error in matrix.validate_receipt(receipt)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "A13-Google"
            materialized = self._materialize_pass_receipt(root)
            empty = root / "RD-ID-01.log"
            empty.write_bytes(b"")
            materialized["tests"][0]["evidence"][0]["sha256"] = hashlib.sha256(b"").hexdigest()
            result = matrix.evaluate_gate([materialized], matrix.load_matrix(matrix.DEFAULT_MATRIX_PATH), receipt_roots=[root])
            self.assertEqual(result["status"], "BLOCKED")
            self.assertTrue(any("non-empty" in reason for reason in result["reasons"]))

    def test_fail_and_blocked_require_failure_class_and_reason(self):
        receipt = receipt_fixture()
        test = receipt["tests"][0]
        test.update(
            status="FAIL",
            observed="Mismatch",
            reason="",
            failure_class=None,
            evidence=[{"path": "evidence/status.json", "sha256": "a" * 64, "kind": "json"}],
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
        receipt = receipt_fixture()
        receipt["tests"][0]["observed"] = '{"password":"secret-value"}'
        errors = matrix.validate_receipt(receipt)
        self.assertTrue(any("redaction" in error.lower() for error in errors))

    def test_precondition_placeholders_are_blocked(self):
        for value in ("n/a", "none", "unset", "fixture", "test-only"):
            with self.subTest(value=value):
                receipt = receipt_fixture()
                receipt["preconditions"]["network_mode"] = value
                errors = matrix.validate_receipt(receipt)
                self.assertTrue(any("network_mode" in error for error in errors))

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

    def test_battery_capture_uses_authoritative_exemption_query_and_blocks_unknown(self):
        package = "io.github.hkhaobin.hermeswebui"
        calls = []

        def run_deviceidle(_adb, _serial, *args):
            calls.append(args)
            if args == ("shell", "dumpsys", "deviceidle"):
                return "DeviceIdleController state: active\n"
            if args == ("shell", "dumpsys", "deviceidle", "whitelist"):
                return "Whitelist user apps:\n  com.example.other\n"
            raise AssertionError(args)

        with patch.object(matrix, "_run_adb", side_effect=run_deviceidle):
            optimized, optimized_evidence = matrix._battery_optimization("adb", "serial", package)
        self.assertEqual(optimized, "optimized")
        self.assertIn("whitelist", optimized_evidence)
        self.assertIn(("shell", "dumpsys", "deviceidle", "whitelist"), calls)

        calls.clear()

        def run_exempt(_adb, _serial, *args):
            calls.append(args)
            if args == ("shell", "dumpsys", "deviceidle"):
                return f"Whitelist user apps:\n  {package}\n"
            raise AssertionError(args)

        with patch.object(matrix, "_run_adb", side_effect=run_exempt):
            exempt, exempt_evidence = matrix._battery_optimization("adb", "serial", package)
        self.assertEqual(exempt, "exempt")
        self.assertIn("deviceidle", exempt_evidence)
        self.assertEqual(calls, [("shell", "dumpsys", "deviceidle")])

        def run_query_failure(_adb, _serial, *args):
            if args == ("shell", "dumpsys", "deviceidle"):
                return "DeviceIdleController state: active\n"
            raise matrix.BlockedError("authoritative whitelist query failed")

        with patch.object(matrix, "_run_adb", side_effect=run_query_failure):
            unknown, unknown_evidence = matrix._battery_optimization("adb", "serial", package)
        self.assertEqual(unknown, "unknown")
        self.assertIn("unknown", unknown_evidence)

        serial = "R58M1234567"
        props = "\n".join(
            [
                "[ro.product.cpu.abilist]: [arm64-v8a,armeabi-v7a]",
                "[ro.build.version.sdk]: [33]",
                "[ro.product.manufacturer]: [Google]",
                "[ro.product.model]: [Pixel 8]",
                "[ro.build.version.release]: [13]",
                "[ro.build.fingerprint]: [google/pixel/pixel8:13/2026/1234567:user/release-keys]",
                "[ro.build.version.security_patch]: [2026-01-01]",
                "[ro.build.display.id]: [AOSP-2026]",
            ]
        )

        def run_capture(_adb, selected_serial, *args):
            self.assertEqual(selected_serial, serial)
            if args == ("shell", "getprop"):
                return props
            if args == ("shell", "wm", "size"):
                return "Physical size: 1080x2400\n"
            if args == ("shell", "wm", "density"):
                return "Physical density: 420\n"
            if args == ("shell", "cmd", "webviewupdate", "get-current-webview-package"):
                return "com.test.webview 1.2.3\n"
            if args == ("shell", "settings", "get", "secure", "enabled_accessibility_services"):
                return "\n"
            if args == ("shell", "settings", "get", "secure", "default_input_method"):
                return "com.google.android.inputmethod.latin\n"
            if args == ("shell", "settings", "get", "global", "airplane_mode_on"):
                return "0\n"
            raise AssertionError(args)

        for battery_value in ("optimized", "exempt"):
            with self.subTest(battery_value=battery_value), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "receipt.json"
                with patch.object(matrix, "list_adb_devices", return_value=[{"serial": serial, "state": "device"}]), \
                     patch.object(matrix, "_run_adb", side_effect=run_capture), \
                     patch.object(matrix, "_parse_webview", return_value={"package": "com.test.webview", "version": "1.2.3", "provider": "com.test.webview"}), \
                     patch.object(matrix, "_installed_apk_metadata", return_value=installed_apk_fixture()), \
                     patch.object(matrix, "_notification_permission", return_value="default"), \
                     patch.object(matrix, "_battery_optimization", return_value=(battery_value, f"reported-{battery_value}")), \
                     patch.object(matrix, "_network_mode", return_value="connected"), \
                     patch.object(matrix, "_screen_state", return_value=("interactive", "mWakefulness=Awake")):
                    receipt = matrix.capture_receipt(
                        artifact=artifact_fixture(),
                        output=output,
                        slot_id="A13-Google",
                        serial=None,
                        adb="adb",
                        operator_id="operator",
                        authorization_ref="auth-record",
                        backend_id="controlled",
                    )
                self.assertEqual(receipt["device"]["battery"]["optimization"], battery_value)
                self.assertEqual(receipt["preconditions"]["battery_optimization"]["value"], battery_value)
                self.assertTrue(output.exists())

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "unknown-battery.json"
            with patch.object(matrix, "list_adb_devices", return_value=[{"serial": serial, "state": "device"}]), \
                 patch.object(matrix, "_run_adb", side_effect=run_capture), \
                 patch.object(matrix, "_parse_webview", return_value={"package": "com.test.webview", "version": "1.2.3", "provider": "com.test.webview"}), \
                 patch.object(matrix, "_installed_apk_metadata", return_value=installed_apk_fixture()), \
                 patch.object(matrix, "_notification_permission", return_value="default"), \
                 patch.object(matrix, "_battery_optimization", return_value=("unknown", "unknown")), \
                 patch.object(matrix, "_network_mode", return_value="connected"), \
                 patch.object(matrix, "_screen_state", return_value=("interactive", "mWakefulness=Awake")):
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

    def test_record_test_hashes_relative_evidence_and_updates_aggregate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt_path = root / "receipt.json"
            receipt_path.write_text(json.dumps(receipt_fixture()), encoding="utf-8")
            evidence = root / "status.json"
            fixture = receipt_fixture()
            evidence.write_text(json.dumps(proof_fixture(fixture, fixture["tests"][0])), encoding="utf-8")
            updated = matrix.record_test(
                receipt_path,
                "RD-ID-01",
                status="PASS",
                observed=matrix.TEST_CONTRACT["RD-ID-01"]["expected"],
                reason="Artifact identity matched.",
                failure_class=None,
                evidence_paths=[evidence],
            )
            self.assertEqual(updated["tests"][0]["status"], "PASS")
            self.assertEqual(updated["aggregate_status"], "NOT RUN")
            self.assertEqual(updated["tests"][0]["evidence"][0]["sha256"], hashlib.sha256(evidence.read_bytes()).hexdigest())
            self.assertEqual(matrix.validate_receipt(updated), [])

    def test_record_test_preserves_actual_installed_apk_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt_path = root / "receipt.json"
            receipt = receipt_fixture()
            original = copy.deepcopy(receipt["tests"][0]["installed_apk"])
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            evidence = root / "status.json"
            evidence.write_text(json.dumps(proof_fixture(receipt, receipt["tests"][0])), encoding="utf-8")
            updated = matrix.record_test(
                receipt_path,
                "RD-ID-01",
                status="PASS",
                observed=matrix.TEST_CONTRACT["RD-ID-01"]["expected"],
                reason="Installed package metadata matched the artifact.",
                failure_class=None,
                evidence_paths=[evidence],
                evidence_kinds=["json"],
            )
            self.assertEqual(updated["tests"][0]["installed_apk"], original)
            self.assertNotIn("filename", updated["tests"][0]["installed_apk"])


if __name__ == "__main__":
    unittest.main()
