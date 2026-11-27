#!/usr/bin/env python3
"""Bidirectional structural/lexical schema parity; no hardware execution.

JSON Schema does not compare sibling values or inspect evidence files. The
runtime additionally enforces those contextual bindings, tested in the harness
suite. These tests exercise the same precondition/hash language in both paths.
"""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

import android_device_matrix as matrix
import test_android_device_matrix as fixtures
from test_android_device_matrix import receipt_fixture


ROOT = Path(__file__).resolve().parents[1]


class ReceiptSchemaParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads((ROOT / "docs/android-device-receipt.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema, format_checker=FormatChecker())

    def assert_both(self, receipt, accepted):
        schema_errors = list(self.validator.iter_errors(receipt))
        runtime_errors = matrix.validate_receipt(receipt)
        self.assertEqual(not schema_errors, accepted, [error.message for error in schema_errors])
        self.assertEqual(not runtime_errors, accepted, runtime_errors)

    def test_not_run_and_pass_metadata_are_accepted_by_both_paths(self):
        self.assert_both(receipt_fixture(), True)
        self.assert_both(fixtures.AndroidDeviceMatrixTests()._pass_receipt(), True)

    def test_every_enum_value_has_the_same_schema_and_runtime_language(self):
        for field, values in matrix._PRECONDITION_ENUMS.items():
            self.assertEqual(self.schema["$defs"]["preconditions"]["properties"][field]["properties"]["value"]["enum"], list(values))
            for value in values:
                with self.subTest(field=field, value=value):
                    receipt = receipt_fixture()
                    record = receipt["preconditions"][field]
                    record["value"] = value
                    if field == "screen_state":
                        record["evidence"] = "mWakefulness=Awake" if value == "interactive" else "mWakefulness=Asleep"
                    elif field != "install_mode":
                        record["evidence"] = value
                    if field == "notification_permission":
                        receipt["device"]["notification_permission"] = value
                    elif field == "battery_optimization":
                        receipt["device"]["battery"]["optimization"] = value
                    elif field == "network_mode":
                        receipt["device"]["network"]["mode"] = value
                    self.assert_both(receipt, True)

    def test_unknown_placeholder_or_missing_preconditions_are_rejected_by_both(self):
        invalid = ("unknown", "UNKNOWN", "n/a", "none", "unset", "fixture", "test-only", "operator-set-before-run", "NOT AVAILABLE", "", " ")
        for field in matrix._PRECONDITION_SOURCES:
            for value in invalid:
                with self.subTest(field=field, value=value):
                    receipt = receipt_fixture()
                    receipt["preconditions"][field]["value"] = value
                    self.assert_both(receipt, False)
            for missing in ("value", "source", "observed_at", "evidence"):
                receipt = receipt_fixture()
                del receipt["preconditions"][field][missing]
                self.assert_both(receipt, False)

    def test_all_hash_boundaries_reject_uppercase_whitespace_and_wrong_length(self):
        paths = (
            ("artifact", "apk", "sha256"), ("artifact", "apk", "signer_sha256"),
            ("artifact", "build", "head_sha"), ("artifact", "runtime", "sha256"),
            ("artifact", "runtime", "source_commit"), ("artifact", "webui_revision"),
            ("device", "serial_sha256"), ("tests", 0, "installed_apk", "sha256"),
        )
        for path in paths:
            original = receipt_fixture()
            target = original
            for key in path:
                target = target[key]
            assert isinstance(target, str)
            for value in ("A" * len(target), target + "\n", " " + target, target[:-1]):
                with self.subTest(path=path, value_kind=len(value)):
                    receipt = copy.deepcopy(original)
                    parent = receipt
                    for key in path[:-1]:
                        parent = parent[key]
                    parent[path[-1]] = value
                    self.assert_both(receipt, False)

    def test_sha_validation_does_not_normalize_caller_values(self):
        for length in (40, 64):
            for value, accepted in (("a" * length, True), ("A" * length, False), ("a" * length + "\n", False)):
                errors = []
                matrix._sha(value, "digest", errors, length=length)
                self.assertEqual(not errors, accepted)

    def test_precondition_and_path_regexes_are_shared_exactly(self):
        self.assertEqual(self.schema["$defs"]["known_precondition"]["pattern"], matrix._KNOWN_PRECONDITION_PATTERN)
        self.assertEqual(self.schema["$defs"]["installed_path"]["pattern"], matrix._INSTALLED_PATH.pattern)
        self.assertEqual(self.schema["$defs"]["evidence"]["properties"]["path"]["pattern"], matrix._EVIDENCE_PATH.pattern)

    def test_per_test_expected_observed_and_evidence_kinds_match_runtime(self):
        branches = self.schema["$defs"]["test_contract"]["oneOf"]
        self.assertEqual(len(branches), len(matrix.TEST_IDS))
        for branch, test_id in zip(branches, matrix.TEST_IDS):
            contract = matrix.TEST_CONTRACT[test_id]
            self.assertEqual(branch["properties"]["id"]["const"], test_id)
            self.assertEqual(branch["properties"]["expected"]["const"], contract["expected"])
            self.assertEqual(branch["then"]["properties"]["observed"]["const"], contract["expected"])
            self.assertEqual(branch["properties"]["evidence"]["items"]["properties"]["kind"]["enum"], list(contract["evidence_kinds"]))
        for test_id in ("RD-ID-01", "RD-MIGRATION-01", "RD-MEMORY-01", "RD-PROCESS-01", "RD-LARGE-01"):
            for field in ("expected", "observed", "kind"):
                with self.subTest(test_id=test_id, field=field):
                    receipt = fixtures.AndroidDeviceMatrixTests()._pass_receipt()
                    row = next(test for test in receipt["tests"] if test["id"] == test_id)
                    if field == "kind":
                        row["evidence"].append({**row["evidence"][0], "kind": "network"})
                    else:
                        row[field] = "arbitrary static result"
                    self.assert_both(receipt, False)

    def test_duplicate_test_ids_cannot_replace_a_required_test_in_either_path(self):
        receipt = receipt_fixture()
        receipt["tests"][1] = copy.deepcopy(receipt["tests"][0])
        receipt["tests"][1]["notes"] = "different row with a repeated ID"
        self.assert_both(receipt, False)

    def test_600dp_boundary_and_compact_phone_are_rejected_in_large_screen_slot(self):
        for minimum, accepted in ((599, False), (600, True), (800, True)):
            receipt = receipt_fixture()
            receipt["device"]["slot_id"] = "Large-screen"
            receipt["device"]["display"] = {"width": minimum * 2, "height": 2400, "density": 320}
            receipt["device"]["screen_capability"] = {
                "kind": "large-screen" if minimum >= 600 else "phone",
                "minimum_dimension_dp": minimum, "source": "adb:wm",
            }
            self.assert_both(receipt, accepted)

    def test_pass_requires_json_proof_descriptor_and_utc_timestamps_in_both_paths(self):
        for field, value in (("started_at", "2026-11-26T00:00:00+00:00"), ("finished_at", "2026-11-26T00:01:00Z\n")):
            receipt = fixtures.AndroidDeviceMatrixTests()._pass_receipt()
            receipt["tests"][0][field] = value
            self.assert_both(receipt, False)
        receipt = fixtures.AndroidDeviceMatrixTests()._pass_receipt()
        receipt["tests"][0]["evidence"][0]["kind"] = "device-metadata"
        self.assert_both(receipt, False)


if __name__ == "__main__":
    unittest.main()
