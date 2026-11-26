#!/usr/bin/env python3
"""Issue #14 Android device matrix and release-gate harness.

This module is deliberately read-only with respect to Android devices.  The
capture command only reads ADB metadata; it never installs an APK, changes
package-verifier settings, changes permissions, or changes battery policy.

A receipt is valid evidence only when it contains exact device, WebView, APK,
runtime, WebUI, and test identity.  The gate command never creates missing
receipts or devices: absent/blocked/not-run rows keep the release blocked.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
RECEIPT_KIND = "android-device-acceptance"
ARTIFACT_KIND = "android-build-artifact"
REDACTION_POLICY_VERSION = "1"
STATUSES = ("PASS", "FAIL", "BLOCKED", "NOT RUN")
FAILURE_CLASSES = (
    "app",
    "service",
    "job",
    "notification",
    "webview",
    "network",
    "oem",
    "artifact",
    "device",
    "operator",
    "unknown",
)

TEST_IDS = (
    "RD-ID-01",
    "RD-INSTALL-01",
    "RD-INSTALL-02",
    "RD-COLD-01",
    "RD-UPGRADE-01",
    "RD-ROLLBACK-01",
    "RD-RUNTIME-01",
    "RD-RUNTIME-02",
    "RD-RUNTIME-03",
    "RD-RUNTIME-04",
    "RD-RUNTIME-05",
    "RD-JOB-01",
    "RD-JOB-02",
    "RD-WEB-01",
    "RD-WEB-02",
    "RD-STREAM-01",
    "RD-STREAM-02",
    "RD-TERM-01",
    "RD-SAF-01",
    "RD-SAF-02",
    "RD-SAF-03",
    "RD-SAF-04",
    "RD-SAF-05",
    "RD-BG-01",
    "RD-BG-02",
    "RD-BG-03",
    "RD-BG-04",
    "RD-UX-01",
    "RD-UX-02",
    "RD-UX-03",
    "RD-UX-04",
)

# These are capability slots, not devices.  No model, serial, or result is
# stored here; an authorized run must provide those values in a receipt.
REQUIRED_SLOTS = (
    {"slot_id": "A13-Google", "api_level": 33, "oem": "Google", "abi": "arm64-v8a", "required": True, "tier": "M0"},
    {"slot_id": "A14-Samsung", "api_level": 34, "oem": "Samsung", "abi": "arm64-v8a", "required": True, "tier": "M0"},
    {"slot_id": "A15-Xiaomi", "api_level": 35, "oem": "Xiaomi", "abi": "arm64-v8a", "required": True, "tier": "M0"},
    {"slot_id": "A16-Google", "api_level": 36, "oem": "Google", "abi": "arm64-v8a", "required": True, "tier": "M0"},
    {"slot_id": "Large-screen", "api_level": 0, "oem": "any", "abi": "arm64-v8a", "required": False, "tier": "M1"},
    {"slot_id": "OEM-stress", "api_level": 0, "oem": "any", "abi": "arm64-v8a", "required": False, "tier": "M1"},
)

DEFAULT_MATRIX_PATH = Path(__file__).resolve().parents[1] / "docs" / "android-device-matrix.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_RUN_ID = re.compile(r"^[0-9]+$")
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_SECRET_KEY = re.compile(
    r"(?:^|_)(?:api[_-]?key|password|secret|token|cookie|authorization|"
    r"private[_-]?key|access[_-]?token|refresh[_-]?token)(?:_|$)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?:bearer\s+[A-Za-z0-9._~+/=-]+|"
    r"(?:api[_-]?key|password|authorization|cookie|access[_-]?token|refresh[_-]?token)\s*[:=])|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    re.IGNORECASE,
)
_SAFE_REDACTION_KEYS = {
    "authorization_ref",
    "raw_serial_stored",
    "secrets_stored",
    "patterns_checked",
    "policy_version",
}

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_BLOCKED = 2
EXIT_USAGE = 64


class HarnessError(Exception):
    """Invalid input or an unusable evidence artifact."""


class BlockedError(HarnessError):
    """A required authorized prerequisite is absent."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _non_empty(value: Any, name: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{name} must be a non-empty string")


def _sha(value: Any, name: str, errors: list[str], *, length: int = 64) -> None:
    pattern = _SHA256 if length == 64 else _SHA1
    if not isinstance(value, str) or not pattern.fullmatch(value.lower()):
        errors.append(f"{name} must be lowercase hexadecimal SHA-{length * 4}")


def _timestamp(value: Any, name: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not _RFC3339.fullmatch(value):
        errors.append(f"{name} must be an RFC3339 UTC timestamp ending in Z")
        return
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        errors.append(f"{name} is not a valid timestamp")


def _redaction_findings(value: Any, path: str = "$") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            key_lower = key_text.lower()
            if key_lower == "serial":
                findings.append(f"redaction: {path}.{key_text}: raw serial is forbidden; store serial_sha256 and serial_suffix")
            elif key_lower not in _SAFE_REDACTION_KEYS and _SECRET_KEY.search(key_lower):
                findings.append(f"redaction: {path}.{key_text}: secret-like field name is forbidden")
            findings.extend(_redaction_findings(child, f"{path}.{key_text}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(_redaction_findings(child, f"{path}[{index}]"))
    elif isinstance(value, str) and _SECRET_VALUE.search(value):
        findings.append(f"redaction: {path}: secret-like value is forbidden")
    return findings


def _relative_evidence_path(path: str) -> bool:
    candidate = Path(path)
    return not candidate.is_absolute() and ".." not in candidate.parts


def _evidence_errors(test: dict[str, Any], *, receipt_root: Path | None = None) -> list[str]:
    errors: list[str] = []
    evidence = test.get("evidence")
    if not isinstance(evidence, list):
        return [f"{test.get('id', '<unknown>')}.evidence must be an array"]
    for index, item in enumerate(evidence):
        prefix = f"{test.get('id', '<unknown>')}.evidence[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        _non_empty(item.get("path"), f"{prefix}.path", errors)
        if isinstance(item.get("path"), str) and not _relative_evidence_path(item["path"]):
            errors.append(f"{prefix}.path must be relative and cannot contain ..")
        _sha(item.get("sha256"), f"{prefix}.sha256", errors)
        _non_empty(item.get("kind"), f"{prefix}.kind", errors)
        if receipt_root is not None and isinstance(item.get("path"), str) and _relative_evidence_path(item["path"]):
            evidence_path = receipt_root / item["path"]
            if not evidence_path.is_file():
                errors.append(f"{prefix}.path does not exist: {item['path']}")
            elif sha256_file(evidence_path) != item.get("sha256"):
                errors.append(f"{prefix}.sha256 does not match file: {item['path']}")
    return errors


def aggregate_status(statuses: Iterable[str]) -> str:
    values = list(statuses)
    if any(status == "FAIL" for status in values):
        return "FAIL"
    if any(status == "BLOCKED" for status in values):
        return "BLOCKED"
    if not values or any(status != "PASS" for status in values):
        return "NOT RUN"
    return "PASS"


def validate_artifact(artifact: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(artifact, dict):
        return ["artifact must be an object"]
    if artifact.get("artifact_kind") != ARTIFACT_KIND:
        errors.append(f"artifact_kind must be {ARTIFACT_KIND}")
    if artifact.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"artifact schema_version must be {SCHEMA_VERSION}")
    apk = artifact.get("apk")
    build = artifact.get("build")
    runtime = artifact.get("runtime")
    for name, value in (("artifact.apk", apk), ("artifact.build", build), ("artifact.runtime", runtime)):
        if not isinstance(value, dict):
            errors.append(f"{name} must be an object")
    if isinstance(apk, dict):
        for field in ("filename", "application_id", "version_name"):
            _non_empty(apk.get(field), f"artifact.apk.{field}", errors)
        _sha(apk.get("sha256"), "artifact.apk.sha256", errors)
        _sha(apk.get("signer_sha256"), "artifact.apk.signer_sha256", errors)
        if not isinstance(apk.get("size_bytes"), int) or apk["size_bytes"] <= 0:
            errors.append("artifact.apk.size_bytes must be a positive integer")
        if not isinstance(apk.get("version_code"), int) or apk["version_code"] <= 0:
            errors.append("artifact.apk.version_code must be a positive integer")
    if isinstance(build, dict):
        for field in ("repository", "workflow"):
            _non_empty(build.get(field), f"artifact.build.{field}", errors)
        if not isinstance(build.get("run_id"), str) or not _RUN_ID.fullmatch(build["run_id"]):
            errors.append("artifact.build.run_id must contain only digits")
        _sha(build.get("head_sha"), "artifact.build.head_sha", errors, length=40)
    if isinstance(runtime, dict):
        for field in ("version", "payload_id"):
            _non_empty(runtime.get(field), f"artifact.runtime.{field}", errors)
        _sha(runtime.get("sha256"), "artifact.runtime.sha256", errors)
        _sha(runtime.get("source_commit"), "artifact.runtime.source_commit", errors, length=40)
    _sha(artifact.get("webui_revision"), "artifact.webui_revision", errors, length=40)
    return errors


def validate_device(device: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(device, dict):
        return ["device must be an object"]
    for field in (
        "slot_id",
        "device_id",
        "manufacturer",
        "model",
        "oem",
        "oem_skin",
        "abi",
        "android_release",
        "build_fingerprint",
        "security_patch_level",
        "serial_sha256",
        "serial_suffix",
        "authorization_ref",
    ):
        _non_empty(device.get(field), f"device.{field}", errors)
    if device.get("abi") != "arm64-v8a":
        errors.append("device.abi must be arm64-v8a")
    if not isinstance(device.get("api_level"), int) or device["api_level"] not in {33, 34, 35, 36}:
        errors.append("device.api_level must be one of 33, 34, 35, 36")
    _sha(device.get("serial_sha256"), "device.serial_sha256", errors)
    if not isinstance(device.get("authorized"), bool) or not device.get("authorized"):
        errors.append("device.authorized must be true for a hardware receipt")
    webview = device.get("webview")
    if not isinstance(webview, dict):
        errors.append("device.webview must be an object")
    else:
        for field in ("package", "version", "provider"):
            _non_empty(webview.get(field), f"device.webview.{field}", errors)
    display = device.get("display")
    if not isinstance(display, dict):
        errors.append("device.display must be an object")
    else:
        for field in ("width", "height", "density"):
            if not isinstance(display.get(field), int) or display[field] <= 0:
                errors.append(f"device.display.{field} must be a positive integer")
    input_info = device.get("input")
    if not isinstance(input_info, dict):
        errors.append("device.input must be an object")
    else:
        _non_empty(input_info.get("ime"), "device.input.ime", errors)
        if not isinstance(input_info.get("talkback"), bool):
            errors.append("device.input.talkback must be boolean")
    battery = device.get("battery")
    if not isinstance(battery, dict):
        errors.append("device.battery must be an object")
    else:
        if battery.get("optimization") not in {"optimized", "exempt", "unknown"}:
            errors.append("device.battery.optimization must be optimized, exempt, or unknown")
        _non_empty(battery.get("oem_policy"), "device.battery.oem_policy", errors)
    if device.get("notification_permission") not in {"allow", "deny", "default", "unknown"}:
        errors.append("device.notification_permission has an invalid value")
    network = device.get("network")
    if not isinstance(network, dict):
        errors.append("device.network must be an object")
    else:
        _non_empty(network.get("mode"), "device.network.mode", errors)
        _non_empty(network.get("backend_id"), "device.network.backend_id", errors)
    return errors


def _validate_test(test: Any, expected_id: str, *, receipt_root: Path | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(test, dict):
        return [f"tests[{expected_id}] must be an object"]
    if test.get("id") != expected_id:
        errors.append(f"test id mismatch: expected {expected_id}")
    status = test.get("status")
    if status not in STATUSES:
        errors.append(f"{expected_id}.status must be one of {STATUSES}")
        return errors
    _non_empty(test.get("observed"), f"{expected_id}.observed", errors) if status in {"PASS", "FAIL"} else None
    if status in {"FAIL", "BLOCKED", "NOT RUN"}:
        _non_empty(test.get("reason"), f"{expected_id}.reason", errors)
    if status in {"FAIL", "BLOCKED"} and test.get("failure_class") not in FAILURE_CLASSES:
        errors.append(f"{expected_id}.failure_class is required for {status}")
    if status == "PASS" and test.get("failure_class") not in {None, ""}:
        errors.append(f"{expected_id}.failure_class must be empty for PASS")
    if test.get("redaction_checked") is not True:
        errors.append(f"{expected_id}.redaction_checked must be true")
    errors.extend(_evidence_errors(test, receipt_root=receipt_root))
    if status in {"PASS", "FAIL"} and not test.get("evidence"):
        errors.append(f"{expected_id}.evidence is required for {status}")
    if status in {"PASS", "FAIL"}:
        for field in ("started_at", "finished_at"):
            _timestamp(test.get(field), f"{expected_id}.{field}", errors)
    return errors


def validate_receipt(
    receipt: Any,
    matrix: dict[str, Any] | None = None,
    *,
    receipt_root: Path | None = None,
    check_evidence: bool = False,
) -> list[str]:
    errors = _redaction_findings(receipt)
    if not isinstance(receipt, dict):
        return ["receipt must be an object"] + errors
    if receipt.get("receipt_kind") != RECEIPT_KIND:
        errors.append(f"receipt_kind must be {RECEIPT_KIND}")
    if receipt.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    for field in ("receipt_id",):
        _non_empty(receipt.get(field), field, errors)
    _timestamp(receipt.get("generated_at"), "generated_at", errors)
    operator = receipt.get("operator")
    if not isinstance(operator, dict):
        errors.append("operator must be an object")
    else:
        _non_empty(operator.get("id"), "operator.id", errors)
        _non_empty(operator.get("authorization_ref"), "operator.authorization_ref", errors)
    errors.extend(validate_device(receipt.get("device")))
    errors.extend(validate_artifact(receipt.get("artifact")))
    preconditions = receipt.get("preconditions")
    if not isinstance(preconditions, dict):
        errors.append("preconditions must be an object")
    else:
        for field in ("install_mode", "notification_permission", "battery_optimization", "network_mode", "screen_state", "backend_id"):
            _non_empty(preconditions.get(field), f"preconditions.{field}", errors)
    redaction = receipt.get("redaction")
    if not isinstance(redaction, dict):
        errors.append("redaction must be an object")
    else:
        if redaction.get("policy_version") != REDACTION_POLICY_VERSION:
            errors.append(f"redaction.policy_version must be {REDACTION_POLICY_VERSION}")
        for field in ("raw_serial_stored", "secrets_stored", "patterns_checked"):
            if redaction.get(field) is not False and field != "patterns_checked":
                errors.append(f"redaction.{field} must be false")
        if redaction.get("patterns_checked") is not True:
            errors.append("redaction.patterns_checked must be true")
    tests = receipt.get("tests")
    if not isinstance(tests, list):
        return errors + ["tests must be an array"]
    expected_ids = tuple(matrix.get("test_ids", TEST_IDS)) if matrix else TEST_IDS
    by_id: dict[str, Any] = {}
    for index, test in enumerate(tests):
        if not isinstance(test, dict):
            errors.append(f"tests[{index}] must be an object")
            continue
        test_id = test.get("id")
        if not isinstance(test_id, str):
            errors.append(f"tests[{index}].id must be a string")
            continue
        if test_id in by_id:
            errors.append(f"duplicate test id: {test_id}")
        by_id[test_id] = test
    missing = [test_id for test_id in expected_ids if test_id not in by_id]
    extra = [test_id for test_id in by_id if test_id not in expected_ids]
    errors.extend(f"missing test id: {test_id}" for test_id in missing)
    errors.extend(f"unknown test id: {test_id}" for test_id in extra)
    for test_id in expected_ids:
        if test_id in by_id:
            errors.extend(_validate_test(by_id[test_id], test_id, receipt_root=receipt_root if check_evidence else None))
    statuses = [by_id[test_id].get("status") for test_id in expected_ids if test_id in by_id]
    computed = aggregate_status(statuses)
    if receipt.get("aggregate_status") != computed:
        errors.append(f"aggregate_status must be {computed}, got {receipt.get('aggregate_status')}")
    if receipt.get("aggregate_status") not in STATUSES:
        errors.append(f"aggregate_status must be one of {STATUSES}")
    return errors


def validate_matrix(matrix: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(matrix, dict):
        return ["matrix must be an object"]
    if matrix.get("matrix_kind") != "android-device-acceptance-matrix":
        errors.append("matrix_kind is invalid")
    if matrix.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"matrix schema_version must be {SCHEMA_VERSION}")
    test_ids = matrix.get("test_ids")
    if test_ids != list(TEST_IDS):
        errors.append("matrix test_ids must exactly match the harness test catalog")
    slots = matrix.get("slots")
    if not isinstance(slots, list):
        return errors + ["matrix slots must be an array"]
    seen: set[str] = set()
    for index, slot in enumerate(slots):
        if not isinstance(slot, dict):
            errors.append(f"slots[{index}] must be an object")
            continue
        slot_id = slot.get("slot_id")
        if not isinstance(slot_id, str) or not slot_id:
            errors.append(f"slots[{index}].slot_id must be non-empty")
        elif slot_id in seen:
            errors.append(f"duplicate matrix slot: {slot_id}")
        else:
            seen.add(slot_id)
        if not isinstance(slot.get("api_level"), int) or slot["api_level"] not in {0, 33, 34, 35, 36}:
            errors.append(f"slots[{index}].api_level must be 0 or 33..36")
        if slot.get("abi") != "arm64-v8a":
            errors.append(f"slots[{index}].abi must be arm64-v8a")
        if not isinstance(slot.get("oem"), str) or not slot["oem"]:
            errors.append(f"slots[{index}].oem must be non-empty")
        if not isinstance(slot.get("required"), bool):
            errors.append(f"slots[{index}].required must be boolean")
        if slot.get("tier") not in {"M0", "M1"}:
            errors.append(f"slots[{index}].tier must be M0 or M1")
        forbidden = {"model", "serial", "build_fingerprint", "result", "status"} & set(slot)
        errors.extend(f"slots[{index}] must not contain device/result field {field}" for field in sorted(forbidden))
    expected_slots = {slot["slot_id"] for slot in REQUIRED_SLOTS}
    if seen != expected_slots:
        errors.append(f"matrix slots must be exactly {sorted(expected_slots)}")
    required_api = {slot["api_level"] for slot in slots if slot.get("required")}
    if required_api != {33, 34, 35, 36}:
        errors.append("required matrix slots must cover API 33, 34, 35, and 36")
    return errors


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise HarnessError(f"JSON root must be an object: {path}")
    return value


def load_matrix(path: Path = DEFAULT_MATRIX_PATH) -> dict[str, Any]:
    value = load_json(path)
    errors = validate_matrix(value)
    if errors:
        raise HarnessError("invalid matrix:\n- " + "\n- ".join(errors))
    return value


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def build_artifact_manifest(
    apk_path: Path,
    *,
    application_id: str,
    version_name: str,
    version_code: int,
    signer_sha256: str,
    build_run_id: str,
    head_sha: str,
    runtime_version: str,
    runtime_sha256: str,
    runtime_source_commit: str,
    payload_id: str,
    webui_revision: str,
    repository: str = "HKLHaoBin/hermes-android",
    workflow: str = "Build APK",
) -> dict[str, Any]:
    if not apk_path.is_file():
        raise HarnessError(f"APK does not exist: {apk_path}")
    digest = sha256_file(apk_path)
    sidecar = Path(str(apk_path) + ".sha256")
    if sidecar.is_file():
        expected = sidecar.read_text(encoding="utf-8").strip().split()[0] if sidecar.read_text(encoding="utf-8").strip() else ""
        if expected.lower() != digest:
            raise ValueError(f"APK SHA-256 sidecar does not match {apk_path}")
    manifest = {
        "artifact_kind": ARTIFACT_KIND,
        "schema_version": SCHEMA_VERSION,
        "apk": {
            "filename": apk_path.name,
            "sha256": digest,
            "size_bytes": apk_path.stat().st_size,
            "application_id": application_id,
            "version_name": version_name,
            "version_code": version_code,
            "signer_sha256": signer_sha256.lower(),
        },
        "build": {
            "repository": repository,
            "workflow": workflow,
            "run_id": build_run_id,
            "head_sha": head_sha.lower(),
        },
        "runtime": {
            "version": runtime_version,
            "sha256": runtime_sha256.lower(),
            "source_commit": runtime_source_commit.lower(),
            "payload_id": payload_id,
        },
        "webui_revision": webui_revision.lower(),
    }
    errors = validate_artifact(manifest)
    if errors:
        raise HarnessError("invalid artifact manifest:\n- " + "\n- ".join(errors))
    return manifest


def _adb_command(adb: str, serial: str | None, *args: str) -> list[str]:
    command = [adb]
    if serial:
        command.extend(["-s", serial])
    command.extend(args)
    return command


def _run_adb(adb: str, serial: str | None, *args: str) -> str:
    command = _adb_command(adb, serial, *args)
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        raise BlockedError(f"cannot execute ADB: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise BlockedError(f"ADB command failed ({' '.join(args)}): {detail[-1] if detail else completed.returncode}")
    return completed.stdout


def list_adb_devices(adb: str = "adb") -> list[dict[str, str]]:
    try:
        completed = subprocess.run([adb, "devices"], check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        raise BlockedError(f"cannot execute ADB: {exc}") from exc
    if completed.returncode != 0:
        raise BlockedError(f"adb devices failed: {(completed.stderr or completed.stdout).strip()}")
    devices: list[dict[str, str]] = []
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = re.split(r"\s+", line)
        if len(parts) >= 2:
            devices.append({"serial": parts[0], "state": parts[1]})
    return devices


def _parse_getprop(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"\[([^]]+)\]: \[([^]]*)\]", line.strip())
        if match:
            values[match.group(1)] = match.group(2)
    return values


def _first_abi(value: str) -> str:
    return value.split(",", 1)[0].strip()


def _parse_display(text: str) -> tuple[int, int]:
    match = re.search(r"(\d+)x(\d+)", text)
    if not match:
        raise BlockedError("cannot capture exact device display size")
    return int(match.group(1)), int(match.group(2))


def _parse_density(text: str) -> int:
    match = re.search(r"(\d+)", text)
    if not match:
        raise BlockedError("cannot capture exact device density")
    return int(match.group(1))


def _parse_webview(text: str) -> dict[str, str]:
    packages = re.findall(r"\b[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\b", text, re.IGNORECASE)
    versions = re.findall(r"\b\d+(?:\.\d+){2,}(?:[-+][A-Za-z0-9._-]+)?\b", text)
    package = next((item for item in packages if item.startswith(("com.", "org.", "io."))), "")
    version = versions[0] if versions else ""
    if not package or not version:
        raise BlockedError("cannot capture exact Android System WebView package and version")
    return {"package": package, "version": version, "provider": package}


def _normalize_oem(manufacturer: str) -> str:
    lower = manufacturer.lower()
    if "samsung" in lower:
        return "Samsung"
    if any(name in lower for name in ("xiaomi", "redmi", "poco")):
        return "Xiaomi"
    if "google" in lower:
        return "Google"
    return manufacturer.strip() or "unknown"


def _oem_skin(props: dict[str, str]) -> str:
    for key in (
        "ro.build.version.oneui",
        "ro.miui.ui.version.name",
        "ro.build.version.oplusrom",
        "ro.build.version.emui",
    ):
        value = props.get(key, "").strip()
        if value:
            return f"{key}={value}"
    return props.get("ro.build.display.id", "").strip() or props.get("ro.build.version.incremental", "unknown")


def _notification_permission(adb: str, serial: str, package: str) -> str:
    try:
        raw = _run_adb(adb, serial, "shell", "cmd", "appops", "get", package, "POST_NOTIFICATION").lower()
    except BlockedError:
        return "unknown"
    if "allow" in raw:
        return "allow"
    if "deny" in raw or "ignore" in raw:
        return "deny"
    if "default" in raw:
        return "default"
    return "unknown"


def _battery_optimization(adb: str, serial: str, package: str) -> tuple[str, str]:
    try:
        raw = _run_adb(adb, serial, "shell", "dumpsys", "deviceidle")
    except BlockedError:
        return "unknown", "unknown"
    if package in raw:
        return "exempt", "reported-by-deviceidle"
    return "unknown", "reported-by-deviceidle"


def capture_receipt(
    *,
    artifact: dict[str, Any],
    output: Path,
    slot_id: str,
    serial: str | None,
    adb: str,
    operator_id: str,
    authorization_ref: str,
    backend_id: str,
    matrix: dict[str, Any] | None = None,
) -> dict[str, Any]:
    artifact_errors = validate_artifact(artifact)
    if artifact_errors:
        raise HarnessError("invalid artifact manifest:\n- " + "\n- ".join(artifact_errors))
    if not operator_id.strip() or not authorization_ref.strip():
        raise HarnessError("operator_id and authorization_ref are required")
    devices = [device for device in list_adb_devices(adb) if device["state"] == "device"]
    if not devices:
        raise BlockedError("no authorized ADB device is connected; hardware execution remains blocked")
    if serial is None:
        if len(devices) != 1:
            raise BlockedError("multiple authorized ADB devices are connected; pass --serial explicitly")
        serial = devices[0]["serial"]
    elif not any(device["serial"] == serial for device in devices):
        raise BlockedError(f"requested serial is not an authorized online ADB device: {serial}")

    props = _parse_getprop(_run_adb(adb, serial, "shell", "getprop"))
    abi = _first_abi(props.get("ro.product.cpu.abilist", ""))
    api_level_text = props.get("ro.build.version.sdk", "")
    if not api_level_text.isdigit():
        raise BlockedError("cannot capture exact ro.build.version.sdk")
    api_level = int(api_level_text)
    if abi != "arm64-v8a" or api_level not in {33, 34, 35, 36}:
        raise BlockedError(f"device is outside Issue #14 M0 API/ABI scope: abi={abi!r}, api={api_level}")
    manufacturer = props.get("ro.product.manufacturer", "")
    oem = _normalize_oem(manufacturer)
    matrix_data = matrix or load_matrix()
    slot = next((item for item in matrix_data["slots"] if item["slot_id"] == slot_id), None)
    if slot is None:
        raise HarnessError(f"unknown matrix slot: {slot_id}")
    if slot["api_level"] not in {0, api_level} or slot["oem"] not in {"any", oem}:
        raise BlockedError(f"device identity does not match slot {slot_id}: oem={oem}, api={api_level}")

    width, height = _parse_display(_run_adb(adb, serial, "shell", "wm", "size"))
    density = _parse_density(_run_adb(adb, serial, "shell", "wm", "density"))
    webview_raw = ""
    try:
        webview_raw = _run_adb(adb, serial, "shell", "cmd", "webviewupdate", "get-current-webview-package")
    except BlockedError:
        webview_raw = _run_adb(adb, serial, "shell", "dumpsys", "webviewupdate")
    webview = _parse_webview(webview_raw)
    package = artifact["apk"]["application_id"]
    serial_digest = hashlib.sha256(serial.encode("utf-8")).hexdigest()
    enabled_services = _run_adb(adb, serial, "shell", "settings", "get", "secure", "enabled_accessibility_services")
    default_ime = _run_adb(adb, serial, "shell", "settings", "get", "secure", "default_input_method").strip() or "unknown"
    battery_optimization, oem_policy = _battery_optimization(adb, serial, package)
    notification_permission = _notification_permission(adb, serial, package)
    airplane = _run_adb(adb, serial, "shell", "settings", "get", "global", "airplane_mode_on").strip()
    network_mode = "airplane" if airplane == "1" else "connected-unknown"
    receipt = {
        "receipt_kind": RECEIPT_KIND,
        "schema_version": SCHEMA_VERSION,
        "receipt_id": f"{slot_id}-{serial_digest[:12]}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        "generated_at": utc_now(),
        "aggregate_status": "NOT RUN",
        "operator": {"id": operator_id, "authorization_ref": authorization_ref},
        "device": {
            "slot_id": slot_id,
            "device_id": f"{oem}-{props.get('ro.product.model', 'unknown')}",
            "manufacturer": manufacturer,
            "model": props.get("ro.product.model", "unknown"),
            "oem": oem,
            "oem_skin": _oem_skin(props),
            "abi": abi,
            "android_release": props.get("ro.build.version.release", "unknown"),
            "api_level": api_level,
            "build_fingerprint": props.get("ro.build.fingerprint", "unknown"),
            "security_patch_level": props.get("ro.build.version.security_patch", "unknown"),
            "serial_sha256": serial_digest,
            "serial_suffix": serial[-4:] if len(serial) >= 4 else serial,
            "authorized": True,
            "authorization_ref": authorization_ref,
            "webview": webview,
            "display": {"width": width, "height": height, "density": density},
            "input": {
                "ime": default_ime,
                "talkback": "talkback" in enabled_services.lower(),
            },
            "battery": {"optimization": battery_optimization, "oem_policy": oem_policy},
            "notification_permission": notification_permission,
            "network": {"mode": network_mode, "backend_id": backend_id},
        },
        "artifact": artifact,
        "preconditions": {
            "install_mode": "operator-set-before-run",
            "notification_permission": notification_permission,
            "battery_optimization": battery_optimization,
            "network_mode": network_mode,
            "screen_state": "operator-set-before-run",
            "backend_id": backend_id,
        },
        "tests": [
            {
                "id": test_id,
                "status": "NOT RUN",
                "observed": "",
                "reason": "Not executed; record only after the authorized test step runs.",
                "failure_class": None,
                "evidence": [],
                "redaction_checked": True,
            }
            for test_id in TEST_IDS
        ],
        "redaction": {
            "policy_version": REDACTION_POLICY_VERSION,
            "raw_serial_stored": False,
            "secrets_stored": False,
            "patterns_checked": True,
        },
    }
    errors = validate_receipt(receipt, matrix_data)
    if errors:
        raise HarnessError("captured receipt failed validation:\n- " + "\n- ".join(errors))
    _write_json_atomic(output, receipt)
    return receipt


def record_test(
    receipt_path: Path,
    test_id: str,
    *,
    status: str,
    observed: str,
    reason: str,
    failure_class: str | None,
    evidence_paths: list[Path],
) -> dict[str, Any]:
    if test_id not in TEST_IDS:
        raise HarnessError(f"unknown test id: {test_id}")
    if status not in STATUSES:
        raise HarnessError(f"invalid status: {status}")
    receipt = load_json(receipt_path)
    if receipt.get("device", {}).get("authorized") is not True:
        raise BlockedError("cannot record a test without an authorized hardware receipt")
    root = receipt_path.parent.resolve()
    evidence: list[dict[str, str]] = []
    for path in evidence_paths:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:
            raise HarnessError(f"evidence must be under the receipt directory: {path}") from exc
        if not resolved.is_file():
            raise HarnessError(f"evidence does not exist: {path}")
        evidence.append({"path": relative.as_posix(), "sha256": sha256_file(resolved), "kind": resolved.suffix.lstrip(".") or "file"})
    test = next((item for item in receipt.get("tests", []) if item.get("id") == test_id), None)
    if test is None:
        raise HarnessError(f"receipt has no test id: {test_id}")
    test.update(
        {
            "status": status,
            "observed": observed,
            "reason": reason,
            "failure_class": failure_class,
            "evidence": evidence,
            "redaction_checked": True,
        }
    )
    if status in {"PASS", "FAIL"}:
        test["started_at"] = test.get("started_at", utc_now())
        test["finished_at"] = utc_now()
    else:
        test.pop("started_at", None)
        test.pop("finished_at", None)
    receipt["aggregate_status"] = aggregate_status(item.get("status") for item in receipt["tests"])
    errors = validate_receipt(receipt, receipt_root=root, check_evidence=True)
    if errors:
        raise HarnessError("updated receipt failed validation:\n- " + "\n- ".join(errors))
    _write_json_atomic(receipt_path, receipt)
    return receipt


def evaluate_gate(receipts: list[dict[str, Any]], matrix: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    slot_reports: list[dict[str, Any]] = []
    valid_receipts: dict[str, dict[str, Any]] = {}
    has_fail = False
    for index, receipt in enumerate(receipts):
        errors = validate_receipt(receipt, matrix)
        if errors:
            reasons.extend(f"receipt[{index}]: {error}" for error in errors)
            continue
        slot_id = receipt["device"]["slot_id"]
        if slot_id in valid_receipts:
            reasons.append(f"duplicate receipt for slot: {slot_id}")
            continue
        valid_receipts[slot_id] = receipt
        if receipt["aggregate_status"] == "FAIL":
            has_fail = True
    for slot in matrix["slots"]:
        if not slot["required"]:
            continue
        receipt = valid_receipts.get(slot["slot_id"])
        if receipt is None:
            reasons.append(f"missing required receipt: {slot['slot_id']}")
            slot_reports.append({"slot_id": slot["slot_id"], "status": "BLOCKED", "reason": "missing receipt"})
            continue
        device = receipt["device"]
        mismatch = []
        if device["api_level"] != slot["api_level"]:
            mismatch.append(f"api {device['api_level']} != {slot['api_level']}")
        if device["oem"] != slot["oem"]:
            mismatch.append(f"oem {device['oem']} != {slot['oem']}")
        if device["abi"] != slot["abi"]:
            mismatch.append(f"abi {device['abi']} != {slot['abi']}")
        if mismatch:
            reasons.append(f"slot {slot['slot_id']} identity mismatch: {', '.join(mismatch)}")
        if receipt["aggregate_status"] != "PASS":
            reasons.append(f"slot {slot['slot_id']} aggregate status is {receipt['aggregate_status']}")
        slot_reports.append({"slot_id": slot["slot_id"], "status": receipt["aggregate_status"], "receipt_id": receipt["receipt_id"]})
    if has_fail:
        status = "FAIL"
    elif reasons:
        status = "BLOCKED"
    else:
        status = "PASS"
    return {
        "gate_kind": "android-device-release-gate",
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "status": status,
        "eligible": status == "PASS",
        "reasons": reasons,
        "slots": slot_reports,
    }


def _command_artifact(args: argparse.Namespace) -> int:
    manifest = build_artifact_manifest(
        Path(args.apk),
        application_id=args.application_id,
        version_name=args.version_name,
        version_code=args.version_code,
        signer_sha256=args.signer_sha256,
        build_run_id=args.build_run_id,
        head_sha=args.head_sha,
        runtime_version=args.runtime_version,
        runtime_sha256=args.runtime_sha256,
        runtime_source_commit=args.runtime_source_commit,
        payload_id=args.payload_id,
        webui_revision=args.webui_revision,
    )
    _write_json_atomic(Path(args.output), manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return EXIT_PASS


def _command_validate_matrix(args: argparse.Namespace) -> int:
    value = load_json(Path(args.matrix))
    errors = validate_matrix(value)
    if errors:
        print(json.dumps({"valid": False, "errors": errors}, ensure_ascii=False, indent=2))
        return EXIT_FAIL
    print(json.dumps({"valid": True, "matrix": str(Path(args.matrix))}, ensure_ascii=False, indent=2))
    return EXIT_PASS


def _command_validate_receipt(args: argparse.Namespace) -> int:
    path = Path(args.receipt)
    receipt = load_json(path)
    matrix = load_matrix(Path(args.matrix))
    errors = validate_receipt(receipt, matrix, receipt_root=path.parent, check_evidence=args.check_evidence)
    print(json.dumps({"valid": not errors, "errors": errors, "receipt": str(path)}, ensure_ascii=False, indent=2))
    return EXIT_PASS if not errors else EXIT_FAIL


def _command_capture(args: argparse.Namespace) -> int:
    artifact = load_json(Path(args.artifact_manifest))
    matrix = load_matrix(Path(args.matrix))
    capture_receipt(
        artifact=artifact,
        output=Path(args.output),
        slot_id=args.slot_id,
        serial=args.serial,
        adb=args.adb,
        operator_id=args.operator_id,
        authorization_ref=args.authorization_ref,
        backend_id=args.backend_id,
        matrix=matrix,
    )
    print(json.dumps({"created": args.output, "status": "NOT RUN"}, ensure_ascii=False, indent=2))
    return EXIT_PASS


def _command_record(args: argparse.Namespace) -> int:
    receipt = record_test(
        Path(args.receipt),
        args.test_id,
        status=args.status,
        observed=args.observed,
        reason=args.reason,
        failure_class=args.failure_class,
        evidence_paths=[Path(item) for item in args.evidence],
    )
    print(json.dumps({"updated": args.receipt, "aggregate_status": receipt["aggregate_status"]}, ensure_ascii=False, indent=2))
    return EXIT_PASS


def _command_gate(args: argparse.Namespace) -> int:
    matrix = load_matrix(Path(args.matrix))
    root = Path(args.receipts_dir)
    receipts: list[dict[str, Any]] = []
    if root.is_dir():
        for path in sorted(root.glob("*.json")):
            value = load_json(path)
            if value.get("receipt_kind") == RECEIPT_KIND:
                receipts.append(value)
    result = evaluate_gate(receipts, matrix)
    if args.output:
        _write_json_atomic(Path(args.output), result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if result["status"] == "PASS":
        return EXIT_PASS
    if result["status"] == "FAIL":
        return EXIT_FAIL
    return EXIT_BLOCKED


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    validate_matrix_parser = sub.add_parser("validate-matrix")
    validate_matrix_parser.add_argument("matrix")
    validate_matrix_parser.set_defaults(handler=_command_validate_matrix)

    validate_receipt_parser = sub.add_parser("validate-receipt")
    validate_receipt_parser.add_argument("--receipt", required=True)
    validate_receipt_parser.add_argument("--matrix", default=str(DEFAULT_MATRIX_PATH))
    validate_receipt_parser.add_argument("--check-evidence", action="store_true")
    validate_receipt_parser.set_defaults(handler=_command_validate_receipt)

    artifact_parser = sub.add_parser("artifact-manifest")
    artifact_parser.add_argument("--apk", required=True)
    artifact_parser.add_argument("--output", required=True)
    artifact_parser.add_argument("--application-id", required=True)
    artifact_parser.add_argument("--version-name", required=True)
    artifact_parser.add_argument("--version-code", required=True, type=int)
    artifact_parser.add_argument("--signer-sha256", required=True)
    artifact_parser.add_argument("--build-run-id", required=True)
    artifact_parser.add_argument("--head-sha", required=True)
    artifact_parser.add_argument("--runtime-version", required=True)
    artifact_parser.add_argument("--runtime-sha256", required=True)
    artifact_parser.add_argument("--runtime-source-commit", required=True)
    artifact_parser.add_argument("--payload-id", required=True)
    artifact_parser.add_argument("--webui-revision", required=True)
    artifact_parser.set_defaults(handler=_command_artifact)

    capture_parser = sub.add_parser("capture", help="capture exact metadata from an authorized ADB device only")
    capture_parser.add_argument("--artifact-manifest", required=True)
    capture_parser.add_argument("--output", required=True)
    capture_parser.add_argument("--slot-id", required=True)
    capture_parser.add_argument("--operator-id", required=True)
    capture_parser.add_argument("--authorization-ref", required=True)
    capture_parser.add_argument("--backend-id", required=True)
    capture_parser.add_argument("--serial")
    capture_parser.add_argument("--adb", default="adb")
    capture_parser.add_argument("--matrix", default=str(DEFAULT_MATRIX_PATH))
    capture_parser.set_defaults(handler=_command_capture)

    record_parser = sub.add_parser("record", help="record one executed test and hash its evidence")
    record_parser.add_argument("--receipt", required=True)
    record_parser.add_argument("--test-id", required=True, choices=TEST_IDS)
    record_parser.add_argument("--status", required=True, choices=STATUSES)
    record_parser.add_argument("--observed", default="")
    record_parser.add_argument("--reason", default="")
    record_parser.add_argument("--failure-class", choices=FAILURE_CLASSES)
    record_parser.add_argument("--evidence", action="append", default=[])
    record_parser.set_defaults(handler=_command_record)

    gate_parser = sub.add_parser("gate")
    gate_parser.add_argument("--receipts-dir", required=True)
    gate_parser.add_argument("--matrix", default=str(DEFAULT_MATRIX_PATH))
    gate_parser.add_argument("--output")
    gate_parser.set_defaults(handler=_command_gate)

    list_parser = sub.add_parser("list-tests")
    list_parser.set_defaults(handler=lambda args: (print("\n".join(TEST_IDS)) or EXIT_PASS))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except BlockedError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return EXIT_BLOCKED
    except (HarnessError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
