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
EVIDENCE_SCHEMA_VERSION = 1
EXPECTED_REPOSITORY = "HKLHaoBin/hermes-android"
EXPECTED_WORKFLOW = "Build APK"
EXPECTED_ARTIFACT_NAME = "apk"
EXPECTED_APPLICATION_ID = "io.github.hkhaobin.hermeswebui"
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
    "RD-MIGRATION-01",
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
    "RD-MEMORY-01",
    "RD-PROCESS-01",
    "RD-LARGE-01",
    "RD-UX-01",
    "RD-UX-02",
    "RD-UX-03",
    "RD-UX-04",
)


def _test_contract(expected: str, *evidence_kinds: str) -> dict[str, Any]:
    return {"expected": expected, "evidence_kinds": tuple(dict.fromkeys((*evidence_kinds, "json")))}


TEST_CONTRACT = {
    "RD-ID-01": _test_contract("Authorized physical device identity matches the assigned matrix slot.", "device-metadata", "json"),
    "RD-INSTALL-01": _test_contract("Exact signed APK installs without clearing application data.", "apk-install", "log"),
    "RD-INSTALL-02": _test_contract("Installed package reports the exact artifact application and version identity.", "apk-install", "json"),
    "RD-COLD-01": _test_contract("Cold start reaches the expected ready state.", "runtime-status", "log"),
    "RD-UPGRADE-01": _test_contract("Same-signer upgrade preserves application data and starts.", "apk-upgrade", "log"),
    "RD-MIGRATION-01": _test_contract("Existing application data migrates to the expected current schema without loss.", "migration", "json", "log"),
    "RD-ROLLBACK-01": _test_contract("Declared rollback path restores the previous known-good application state.", "rollback", "log"),
    "RD-RUNTIME-01": _test_contract("Runtime launches with the manifest payload and reaches ready state.", "runtime-status", "log"),
    "RD-RUNTIME-02": _test_contract("Hermes runtime and WebUI report the expected pinned revisions.", "runtime-status", "json"),
    "RD-RUNTIME-03": _test_contract("Capability diagnostics report the expected Android API and ABI.", "capabilities", "json"),
    "RD-RUNTIME-04": _test_contract("Runtime restart preserves the expected session and service state.", "runtime-status", "log"),
    "RD-RUNTIME-05": _test_contract("Unsupported capability is reported with an explicit bounded state.", "capabilities", "json"),
    "RD-JOB-01": _test_contract("An actual integrated Agent job completes with durable ledger state.", "job-ledger", "notification", "log"),
    "RD-JOB-02": _test_contract("A recovered job resumes or fails with bounded retry and notification state.", "job-ledger", "notification", "log"),
    "RD-WEB-01": _test_contract("WebView origin and feature isolation remain within the expected boundary.", "webview", "screenshot", "log"),
    "RD-WEB-02": _test_contract("WebView authentication and CSRF behavior match the expected policy.", "webview", "network", "log"),
    "RD-STREAM-01": _test_contract("Streaming response reaches the expected terminal state.", "stream", "log"),
    "RD-STREAM-02": _test_contract("Network loss and reconnect preserve the expected stream and session state.", "stream", "network", "log"),
    "RD-TERM-01": _test_contract("Terminal authentication and transport controls enforce the expected policy.", "terminal", "log"),
    "RD-SAF-01": _test_contract("Scoped-storage picker imports the selected file with an exact receipt.", "file-import", "json"),
    "RD-SAF-02": _test_contract("Unsupported or failed picker input produces the expected visible failure.", "file-import", "log"),
    "RD-SAF-03": _test_contract("Duplicate import handling preserves the expected file identity.", "file-import", "json"),
    "RD-SAF-04": _test_contract("Image import preserves the expected metadata and ownership.", "file-import", "json"),
    "RD-SAF-05": _test_contract("Large or non-image import remains within the expected storage boundary.", "file-import", "log"),
    "RD-BG-01": _test_contract("Locked-screen foreground operation preserves the expected job state.", "job-ledger", "notification", "log"),
    "RD-BG-02": _test_contract("Backgrounding preserves the expected service and job state.", "job-ledger", "notification", "log"),
    "RD-BG-03": _test_contract("Process death recovery produces the expected durable job state.", "job-ledger", "log"),
    "RD-BG-04": _test_contract("OEM battery policy and task removal produce the expected explicit outcome.", "job-ledger", "notification", "log"),
    "RD-MEMORY-01": _test_contract("Memory pressure preserves or recovers the expected runtime and job state.", "memory-pressure", "job-ledger", "log"),
    "RD-PROCESS-01": _test_contract("System process recreation restores the expected service and session state.", "process-recreation", "job-ledger", "log"),
    "RD-LARGE-01": _test_contract("Large-screen or foldable layout exposes every required surface at the expected usable size.", "large-screen", "layout", "screenshot"),
    "RD-UX-01": _test_contract("TalkBack exposes the expected labels and focus order.", "accessibility", "screenshot"),
    "RD-UX-02": _test_contract("The selected IME produces the expected input and focus behavior.", "ime", "screenshot"),
    "RD-UX-03": _test_contract("Rotation and font scale preserve the expected layout and interaction state.", "layout", "screenshot"),
    "RD-UX-04": _test_contract("Large-screen or split-screen layout preserves the expected usable surfaces.", "layout", "screenshot"),
}

SCENARIO_CONTRACT = {
    test_id: {
        "name": f"issue14:{test_id}",
        "assertions": (f"{test_id}:expected-state",),
    }
    for test_id in TEST_IDS
}
SCENARIO_CONTRACT.update(
    {
        "RD-MIGRATION-01": {"name": "migration", "assertions": ("data_migration", "schema_version")},
        "RD-MEMORY-01": {"name": "memory-pressure", "assertions": ("memory_pressure", "job_state")},
        "RD-PROCESS-01": {"name": "process-recreation", "assertions": ("process_recreation", "service_state")},
        "RD-LARGE-01": {"name": "large-screen", "assertions": ("capability_identity", "minimum_dimension_dp", "layout_surface")},
    }
)

# These are capability slots, not devices.  No model, serial, or result is
# stored here; an authorized run must provide those values in a receipt.
REQUIRED_SLOTS = (
    {"slot_id": "A13-Google", "api_level": 33, "oem": "Google", "abi": "arm64-v8a", "required": True, "tier": "M0"},
    {"slot_id": "A14-Samsung", "api_level": 34, "oem": "Samsung", "abi": "arm64-v8a", "required": True, "tier": "M0"},
    {"slot_id": "A15-Xiaomi", "api_level": 35, "oem": "Xiaomi", "abi": "arm64-v8a", "required": True, "tier": "M0"},
    {"slot_id": "A16-Google", "api_level": 36, "oem": "Google", "abi": "arm64-v8a", "required": True, "tier": "M0"},
    {"slot_id": "Large-screen", "api_level": 0, "oem": "any", "abi": "arm64-v8a", "required": True, "tier": "M1"},
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
    r"[\"']?(?:api[_-]?key|password|authorization|cookie|token|access[_-]?token|refresh[_-]?token)[\"']?\s*[:=]\s*[\"']?[^\"'\s,}]+)|"
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
_UNKNOWN_LITERALS = {
    "",
    "unknown",
    "n/a",
    "na",
    "none",
    "null",
    "<unknown>",
    "unset",
    "not available",
    "test-only",
    "fixture",
    "mock",
}
_EMULATOR_TRANSPORTS = {"emulator", "test", "mock", "fixture"}
_PRECONDITION_ENUMS = {
    "install_mode": ("installed",),
    "notification_permission": ("allow", "deny", "default"),
    "battery_optimization": ("optimized", "exempt"),
    "network_mode": ("connected", "disconnected", "airplane"),
    "screen_state": ("interactive", "non-interactive"),
}
_PRECONDITION_SOURCES = {
    "install_mode": "adb:pm-path",
    "notification_permission": "adb:appops",
    "battery_optimization": "adb:deviceidle",
    "network_mode": "adb:connectivity",
    "screen_state": "adb:power",
    "backend_id": "operator-authorization",
}
_RAW_SERIAL_VALUE = re.compile(
    r"(?<![A-Za-z0-9_])(?:raw[_ .-]?)?(?:device[_ .-]?)?serial(?:[_ .-]?(?:number|no))?[\"']?\s*[:=]\s*[\"']?[^\"'\s,}]+"
    r"|\badb\s+-s\s+\S+|^\s*\S+\s+(?:device|unauthorized|offline)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_INSTALLED_PATH = re.compile(r"^(?!.*(?:^|/)\.\.(?:/|$))/(?:data/app|product/app|system/app|system/priv-app)/[^\r\n]+\.apk(?![\s\S])")
_EVIDENCE_PATH = re.compile(r"^(?!/)(?![A-Za-z]:)(?!.*(?:^|/)\.\.(?:/|$))[^\\\r\n]+(?![\s\S])")
_KNOWN_PRECONDITION_PATTERN = (
    r"^(?!.*[uU][nN][kK][nN][oO][wW][nN])"
    r"(?!.*(?:^|[-_\s])(?:[nN]/?[aA]|[nN][oO][nN][eE]|[nN][uU][lL][lL]|[uU][nN][sS][eE][tT]|"
    r"[fF][iI][xX][tT][uU][rR][eE]|[mM][oO][cC][kK]|[tT][eE][sS][tT][-_ ]?[oO][nN][lL][yY]|"
    r"[nN][oO][tT][-_ ]+[aA][vV][aA][iI][lL][aA][bB][lL][eE]|"
    r"[oO][pP][eE][rR][aA][tT][oO][rR]-[sS][eE][tT]-[bB][eE][fF][oO][rR][eE]-[rR][uU][nN])(?:$|[-_\s]))"
    r"(?=\S)(?=.*\S$).+(?![\s\S])"
)

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


def _known_identity(value: Any, name: str, errors: list[str]) -> None:
    _non_empty(value, name, errors)
    if isinstance(value, str) and value.strip().lower() in _UNKNOWN_LITERALS:
        errors.append(f"{name} must contain a captured identity, not {value!r}")


def _known_precondition(value: Any, name: str, errors: list[str]) -> None:
    _non_empty(value, name, errors)
    if isinstance(value, str) and not re.fullmatch(_KNOWN_PRECONDITION_PATTERN, value):
        errors.append(f"{name} must be known before a release PASS")


def _parse_installed_package_path(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            path = line.removeprefix("package:").strip()
            if path and path.startswith("/") and ".." not in Path(path).parts:
                return path
    raise BlockedError("cannot capture the installed package APK path")


def _parse_installed_package_metadata(text: str, package: str) -> tuple[str, int]:
    if f"Package [{package}]" not in text:
        raise BlockedError(f"dumpsys package did not identify installed package {package}")
    version_code = re.search(r"\bversionCode=(\d+)\b", text)
    version_name = re.search(r"\bversionName=([^\s]+)", text)
    if not version_code or not version_name or not version_name.group(1).strip():
        raise BlockedError(f"dumpsys package did not expose exact version metadata for {package}")
    return version_name.group(1), int(version_code.group(1))


def _installed_apk_metadata(adb: str, serial: str, package: str) -> dict[str, Any]:
    package_path = _parse_installed_package_path(_run_adb(adb, serial, "shell", "pm", "path", package))
    package_dump = _run_adb(adb, serial, "shell", "dumpsys", "package", package)
    version_name, version_code = _parse_installed_package_metadata(package_dump, package)
    digest_output = _run_adb(adb, serial, "shell", "sha256sum", package_path)
    digest_match = re.search(r"\b([0-9a-fA-F]{64})\b", digest_output)
    if not digest_match:
        raise BlockedError("cannot capture the installed APK SHA-256")
    size_output = _run_adb(adb, serial, "shell", "stat", "-c", "%s", package_path)
    size_match = re.search(r"\b([0-9]+)\b", size_output)
    if not size_match or int(size_match.group(1)) <= 0:
        raise BlockedError("cannot capture the installed APK size")
    return {
        "package_path": package_path,
        "sha256": digest_match.group(1).lower(),
        "size_bytes": int(size_match.group(1)),
        "application_id": package,
        "version_name": version_name,
        "version_code": version_code,
    }


def _object_shape(
    value: Any,
    name: str,
    *,
    required: set[str],
    allowed: set[str],
    errors: list[str],
) -> bool:
    if not isinstance(value, dict):
        errors.append(f"{name} must be an object")
        return False
    errors.extend(f"{name} is missing required field {field}" for field in sorted(required - set(value)))
    errors.extend(f"{name} contains undeclared field {field}" for field in sorted(set(value) - allowed))
    return True


def _sha(value: Any, name: str, errors: list[str], *, length: int = 64) -> None:
    pattern = _SHA256 if length == 64 else _SHA1
    if not isinstance(value, str) or not pattern.fullmatch(value):
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
            if key_lower.replace("-", "_").replace(".", "_") in {"serial", "raw_serial", "device_serial", "serial_number", "serialno", "ro_serialno", "ro_boot_serialno"}:
                findings.append(f"redaction: {path}.{key_text}: raw serial is forbidden; store serial_sha256 and serial_suffix")
            elif key_lower not in _SAFE_REDACTION_KEYS and _SECRET_KEY.search(key_lower):
                findings.append(f"redaction: {path}.{key_text}: secret-like field name is forbidden")
            findings.extend(_redaction_findings(child, f"{path}.{key_text}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(_redaction_findings(child, f"{path}[{index}]"))
    elif isinstance(value, str):
        if _SECRET_VALUE.search(value):
            findings.append(f"redaction: {path}: secret-like value is forbidden")
        if _RAW_SERIAL_VALUE.search(value):
            findings.append(f"redaction: {path}: raw serial assignment/ADB output is forbidden")
    return findings


def _evidence_content_findings(data: bytes, path: str, serial_sha256: str | None = None) -> list[str]:
    text = data.decode("utf-8", errors="replace")
    findings = _redaction_findings(text, path)
    if serial_sha256 and any(
        hashlib.sha256(token.encode("utf-8")).hexdigest() == serial_sha256
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9._:-]{3,127}", text)
    ):
        findings.append(f"redaction: {path}: unredacted device serial matches receipt identity")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return findings
    return findings + _redaction_findings(parsed, path)


def _relative_evidence_path(path: str) -> bool:
    return _EVIDENCE_PATH.fullmatch(path) is not None


def _evidence_errors(
    test: dict[str, Any],
    *,
    allowed_kinds: tuple[str, ...],
    receipt_root: Path | None = None,
    receipt: dict[str, Any] | None = None,
) -> list[str]:
    errors: list[str] = []
    evidence = test.get("evidence")
    if not isinstance(evidence, list):
        return [f"{test.get('id', '<unknown>')}.evidence must be an array"]
    for index, item in enumerate(evidence):
        prefix = f"{test.get('id', '<unknown>')}.evidence[{index}]"
        if not _object_shape(
            item,
            prefix,
            required={"path", "sha256", "kind"},
            allowed={"path", "sha256", "kind"},
            errors=errors,
        ):
            continue
        _non_empty(item.get("path"), f"{prefix}.path", errors)
        if isinstance(item.get("path"), str) and not _relative_evidence_path(item["path"]):
            errors.append(f"{prefix}.path must be relative and cannot contain ..")
        _sha(item.get("sha256"), f"{prefix}.sha256", errors)
        _non_empty(item.get("kind"), f"{prefix}.kind", errors)
        if item.get("kind") not in allowed_kinds:
            errors.append(f"{prefix}.kind must be one of {allowed_kinds} for {test.get('id')}")
        if receipt_root is not None and isinstance(item.get("path"), str) and _relative_evidence_path(item["path"]):
            evidence_path = receipt_root / item["path"]
            if not evidence_path.resolve().is_relative_to(receipt_root.resolve()):
                errors.append(f"{prefix}.path must stay under the receipt directory after resolving links")
            elif not evidence_path.is_file():
                errors.append(f"{prefix}.path does not exist: {item['path']}")
            else:
                data = evidence_path.read_bytes()
                if not data:
                    errors.append(f"{prefix}.path must reference non-empty evidence: {item['path']}")
                if sha256_file(evidence_path) != item.get("sha256"):
                    errors.append(f"{prefix}.sha256 does not match file: {item['path']}")
                serial_sha256 = receipt.get("device", {}).get("serial_sha256") if receipt else None
                errors.extend(_evidence_content_findings(data, f"{prefix}.bytes", serial_sha256))
                if item.get("kind") == "json" and receipt is not None:
                    errors.extend(_structured_evidence_errors(data, f"{prefix}.bytes", item=item, test=test, receipt=receipt))
    if test.get("status") == "PASS" and not any(isinstance(item, dict) and item.get("kind") == "json" for item in evidence):
        errors.append(f"{test.get('id', '<unknown>')}.PASS requires a structured JSON evidence proof")
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


def artifact_identity(artifact: dict[str, Any]) -> tuple[Any, ...]:
    apk = artifact["apk"]
    build = artifact["build"]
    runtime = artifact["runtime"]
    return (
        apk["filename"],
        apk["sha256"],
        apk["size_bytes"],
        apk["application_id"],
        apk["signer_sha256"],
        apk["version_name"],
        apk["version_code"],
        build["repository"],
        build["workflow"],
        build["artifact_name"],
        build["run_id"],
        build["head_sha"],
        runtime["version"],
        runtime["sha256"],
        runtime["source_commit"],
        runtime["payload_id"],
        artifact["webui_revision"],
    )


def _validate_apk(value: Any, name: str, errors: list[str]) -> bool:
    if not _object_shape(
        value,
        name,
        required={"filename", "sha256", "size_bytes", "application_id", "version_name", "version_code", "signer_sha256"},
        allowed={"filename", "sha256", "size_bytes", "application_id", "version_name", "version_code", "signer_sha256"},
        errors=errors,
    ):
        return False
    for field in ("filename", "application_id", "version_name"):
        _non_empty(value.get(field), f"{name}.{field}", errors)
    _sha(value.get("sha256"), f"{name}.sha256", errors)
    _sha(value.get("signer_sha256"), f"{name}.signer_sha256", errors)
    if type(value.get("size_bytes")) is not int or value["size_bytes"] <= 0:
        errors.append(f"{name}.size_bytes must be a positive integer")
    if type(value.get("version_code")) is not int or value["version_code"] <= 0:
        errors.append(f"{name}.version_code must be a positive integer")
    return True


def _validate_installed_apk(value: Any, name: str, errors: list[str]) -> bool:
    if not _object_shape(
        value,
        name,
        required={"package_path", "sha256", "size_bytes", "application_id", "version_name", "version_code"},
        allowed={"package_path", "sha256", "size_bytes", "application_id", "version_name", "version_code"},
        errors=errors,
    ):
        return False
    package_path = value.get("package_path")
    _non_empty(package_path, f"{name}.package_path", errors)
    if isinstance(package_path, str):
        if ".." in package_path.split("/") or not _INSTALLED_PATH.fullmatch(package_path):
            errors.append(f"{name}.package_path must be an installed Android package APK path")
    _sha(value.get("sha256"), f"{name}.sha256", errors)
    for field in ("application_id", "version_name"):
        _non_empty(value.get(field), f"{name}.{field}", errors)
    if type(value.get("size_bytes")) is not int or value["size_bytes"] <= 0:
        errors.append(f"{name}.size_bytes must be a positive integer")
    if type(value.get("version_code")) is not int or value["version_code"] <= 0:
        errors.append(f"{name}.version_code must be a positive integer")
    return True


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _device_identity_digest(device: dict[str, Any]) -> str:
    return _stable_digest(device)


def _artifact_identity_digest(artifact: dict[str, Any]) -> str:
    return _stable_digest(artifact_identity(artifact))


def _is_large_screen_capability(capability: Any) -> bool:
    if not isinstance(capability, dict):
        return False
    minimum = capability.get("minimum_dimension_dp")
    return (
        type(minimum) is int and minimum >= 600
        and capability.get("kind") == "large-screen"
        and capability.get("source") == "adb:wm"
    )


def _validate_screen_capability(value: Any, display: dict[str, Any], name: str, errors: list[str]) -> bool:
    required = {"kind", "minimum_dimension_dp", "source"}
    if not _object_shape(value, name, required=required, allowed=required, errors=errors):
        return False
    if value.get("kind") not in ("phone", "large-screen"):
        errors.append(f"{name}.kind must be phone or large-screen")
    minimum = value.get("minimum_dimension_dp")
    if type(minimum) is not int or minimum <= 0:
        errors.append(f"{name}.minimum_dimension_dp must be a positive integer")
    dimensions = [display.get(field) for field in ("width", "height", "density")]
    if all(type(number) is int and number > 0 for number in dimensions):
        measured = min(display["width"], display["height"]) * 160 // display["density"]
        if minimum != measured:
            errors.append(f"{name}.minimum_dimension_dp does not match the measured display/density")
        if value.get("kind") != ("large-screen" if measured >= 600 else "phone"):
            errors.append(f"{name}.kind does not match the measured 600dp large-screen predicate")
    if value.get("source") != "adb:wm":
        errors.append(f"{name}.source must be adb:wm")
    return True


def _validate_precondition_record(value: Any, name: str, field: str, errors: list[str]) -> bool:
    required = {"value", "source", "observed_at", "evidence"}
    if not _object_shape(value, name, required=required, allowed=required, errors=errors):
        return False
    allowed_values = _PRECONDITION_ENUMS.get(field)
    if allowed_values is not None:
        if value.get("value") not in allowed_values:
            errors.append(f"{name}.value must be one of {allowed_values}")
    else:
        _known_precondition(value.get("value"), f"{name}.value", errors)
    if value.get("source") != _PRECONDITION_SOURCES[field]:
        errors.append(f"{name}.source must be {_PRECONDITION_SOURCES[field]}")
    _timestamp(value.get("observed_at"), f"{name}.observed_at", errors)
    _non_empty(value.get("evidence"), f"{name}.evidence", errors)
    if field == "install_mode":
        if not isinstance(value.get("evidence"), str) or not _INSTALLED_PATH.fullmatch(value["evidence"]):
            errors.append(f"{name}.evidence must be an observed installed APK path")
    elif field == "screen_state":
        states = {"mWakefulness=Awake": "interactive", "mWakefulness=Asleep": "non-interactive",
                  "mWakefulness=Dozing": "non-interactive", "mWakefulness=Dreaming": "interactive"}
        if not isinstance(value.get("evidence"), str) or states.get(value["evidence"]) != value.get("value"):
            errors.append(f"{name}.evidence must substantiate the observed screen state")
    elif value.get("evidence") != value.get("value"):
        errors.append(f"{name}.evidence must match the captured value")
    return True


def _scenario_measurement_errors(test_id: str, measurements: Any, receipt: dict[str, Any], started: Any, finished: Any) -> list[str]:
    errors: list[str] = []
    required_by_test = {
        "RD-MIGRATION-01": {"schema_before", "schema_after", "records_before", "records_after", "data_before_sha256", "data_after_sha256"},
        "RD-MEMORY-01": {"pressure_event", "pressure_at", "job_id", "ledger_preserved", "terminal_state"},
        "RD-PROCESS-01": {"pid_before", "pid_after", "recreated_at", "session_before", "session_after", "service_state"},
        "RD-LARGE-01": {"minimum_dimension_dp", "surfaces"},
    }
    required = required_by_test.get(test_id, {"observed_state"})
    if not _object_shape(measurements, f"{test_id}.measurements", required=required, allowed=required, errors=errors):
        return errors
    if set(measurements) != required:
        return errors
    if test_id == "RD-MIGRATION-01":
        for field in ("schema_before", "schema_after", "records_before", "records_after"):
            if type(measurements[field]) is not int or measurements[field] <= 0:
                errors.append(f"{test_id}.measurements.{field} must be a positive integer")
        if not errors and (measurements["schema_after"] <= measurements["schema_before"] or measurements["records_before"] != measurements["records_after"]):
            errors.append(f"{test_id}.measurements must show a schema advance and preserved records")
        for field in ("data_before_sha256", "data_after_sha256"):
            _sha(measurements[field], f"{test_id}.measurements.{field}", errors)
        if measurements["data_before_sha256"] != measurements["data_after_sha256"]:
            errors.append(f"{test_id}.measurements must preserve the migrated data digest")
    elif test_id == "RD-MEMORY-01":
        if measurements["pressure_event"] not in ("trim-memory-running-critical", "low-memory-kill"):
            errors.append(f"{test_id}.measurements requires an actual memory-pressure event")
        _known_precondition(measurements["job_id"], f"{test_id}.measurements.job_id", errors)
        if measurements["ledger_preserved"] is not True or measurements["terminal_state"] not in ("running", "succeeded"):
            errors.append(f"{test_id}.measurements must show preserved ledger/job state")
    elif test_id == "RD-PROCESS-01":
        for field in ("pid_before", "pid_after"):
            if type(measurements[field]) is not int or measurements[field] <= 0:
                errors.append(f"{test_id}.measurements.{field} must be a positive integer")
        if measurements["pid_before"] == measurements["pid_after"]:
            errors.append(f"{test_id}.measurements must show different process identities")
        for field in ("session_before", "session_after"):
            _known_precondition(measurements[field], f"{test_id}.measurements.{field}", errors)
        if measurements["session_before"] != measurements["session_after"] or measurements["service_state"] != "ready":
            errors.append(f"{test_id}.measurements must show restored session/service state")
    elif test_id == "RD-LARGE-01":
        capability = receipt.get("device", {}).get("screen_capability")
        if not _is_large_screen_capability(capability):
            errors.append("RD-LARGE-01 requires measured large-screen capability in the tested posture")
        if type(measurements["minimum_dimension_dp"]) is not int or not isinstance(capability, dict) or measurements["minimum_dimension_dp"] != capability.get("minimum_dimension_dp"):
            errors.append("RD-LARGE-01 measurements must bind the exact measured capability dimension")
        if not isinstance(measurements["surfaces"], dict) or set(measurements["surfaces"]) != {"messages", "composer", "tabs", "console-frame"} or any(value is not True for value in measurements["surfaces"].values()):
            errors.append("RD-LARGE-01 measurements must show all required usable layout surfaces")
    elif measurements["observed_state"] != TEST_CONTRACT[test_id]["expected"]:
        errors.append(f"{test_id}.measurements.observed_state must match the test-specific expected state")
    for field in ("pressure_at", "recreated_at"):
        if field in measurements:
            _timestamp(measurements[field], f"{test_id}.measurements.{field}", errors)
            if isinstance(measurements[field], str) and isinstance(started, str) and isinstance(finished, str):
                try:
                    if not datetime.fromisoformat(started.replace("Z", "+00:00")) <= datetime.fromisoformat(measurements[field].replace("Z", "+00:00")) <= datetime.fromisoformat(finished.replace("Z", "+00:00")):
                        errors.append(f"{test_id}.measurements.{field} must be inside the test interval")
                except (ValueError, TypeError):
                    pass
    return errors


def _structured_evidence_errors(
    data: bytes,
    path: str,
    *,
    item: dict[str, Any],
    test: dict[str, Any],
    receipt: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    try:
        envelope = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return [f"{test.get('id')}.evidence must contain a structured JSON proof envelope: {path}"]
    if not isinstance(envelope, dict):
        return [f"{test.get('id')}.evidence JSON proof envelope must be an object: {path}"]
    required = {
        "evidence_schema_version",
        "kind",
        "test_id",
        "receipt_id",
        "expected",
        "observed",
        "device",
        "artifact",
        "scenario",
        "assertions",
        "measurements",
        "preconditions_sha256",
        "started_at",
        "finished_at",
    }
    if test.get("id") == "RD-LARGE-01":
        required.add("capability")
    allowed = required
    errors.extend(f"{test.get('id')}.structured_evidence missing field {field}" for field in sorted(required - set(envelope)))
    errors.extend(f"{test.get('id')}.structured_evidence contains undeclared field {field}" for field in sorted(set(envelope) - allowed))
    if errors:
        return errors
    if type(envelope.get("evidence_schema_version")) is not int or envelope.get("evidence_schema_version") != EVIDENCE_SCHEMA_VERSION:
        errors.append(f"{test.get('id')}.structured_evidence.evidence_schema_version is invalid")
    if envelope.get("kind") != item.get("kind") or envelope.get("kind") != "json":
        errors.append(f"{test.get('id')}.structured_evidence.kind must be json and match the receipt evidence item")
    if envelope.get("test_id") != test.get("id"):
        errors.append(f"{test.get('id')}.structured_evidence.test_id must match the receipt test")
    if envelope.get("receipt_id") != receipt.get("receipt_id"):
        errors.append(f"{test.get('id')}.structured_evidence.receipt_id must match the receipt")
    if envelope.get("expected") != test.get("expected") or envelope.get("observed") != test.get("observed"):
        errors.append(f"{test.get('id')}.structured_evidence expected/observed must match the receipt test")
    for field in ("started_at", "finished_at"):
        _timestamp(envelope.get(field), f"{test.get('id')}.structured_evidence.{field}", errors)
        if envelope.get(field) != test.get(field):
            errors.append(f"{test.get('id')}.structured_evidence.{field} must match the receipt test")
    if envelope.get("preconditions_sha256") != _stable_digest(receipt.get("preconditions")):
        errors.append(f"{test.get('id')}.structured_evidence.preconditions_sha256 is not bound to the captured preconditions")
    device = receipt.get("device")
    evidence_device = envelope.get("device")
    if not isinstance(device, dict) or not isinstance(evidence_device, dict):
        errors.append(f"{test.get('id')}.structured_evidence.device must be an object")
    else:
        expected_device = {
            "slot_id": device.get("slot_id"),
            "device_id": device.get("device_id"),
            "serial_sha256": device.get("serial_sha256"),
            "identity_sha256": _device_identity_digest(device),
        }
        if evidence_device != expected_device:
            errors.append(f"{test.get('id')}.structured_evidence.device is not bound to the receipt device")
    artifact = receipt.get("artifact")
    evidence_artifact = envelope.get("artifact")
    if not isinstance(artifact, dict) or not isinstance(evidence_artifact, dict):
        errors.append(f"{test.get('id')}.structured_evidence.artifact must be an object")
    elif not validate_artifact(artifact):
        expected_artifact = {
            "filename": artifact.get("apk", {}).get("filename"),
            "sha256": artifact.get("apk", {}).get("sha256"),
            "identity_sha256": _artifact_identity_digest(artifact),
        }
        if evidence_artifact != expected_artifact:
            errors.append(f"{test.get('id')}.structured_evidence.artifact is not bound to the receipt artifact")
    scenario_contract = SCENARIO_CONTRACT[test["id"]]
    scenario = envelope.get("scenario")
    if not isinstance(scenario, dict) or set(scenario) != {"name"}:
        errors.append(f"{test.get('id')}.structured_evidence.scenario must contain only name")
    elif scenario.get("name") != scenario_contract["name"]:
        errors.append(f"{test.get('id')}.structured_evidence.scenario.name is not test-specific")
    assertions = envelope.get("assertions")
    if not isinstance(assertions, list):
        errors.append(f"{test.get('id')}.structured_evidence.assertions must be an array")
    else:
        expected_names = set(scenario_contract["assertions"])
        actual_names = [entry.get("name") for entry in assertions if isinstance(entry, dict)]
        if not all(isinstance(name, str) for name in actual_names) or sorted(str(name) for name in actual_names) != sorted(expected_names):
            errors.append(f"{test.get('id')}.structured_evidence.assertions must equal the scenario contract")
        for entry in assertions:
            if not isinstance(entry, dict) or set(entry) != {"name", "expected", "observed", "passed"}:
                errors.append(f"{test.get('id')}.structured_evidence assertions have an invalid shape")
                continue
            if entry["expected"] != TEST_CONTRACT[test["id"]]["expected"] or (test.get("status") == "PASS" and entry["observed"] != entry["expected"]):
                errors.append(f"{test.get('id')}.structured_evidence assertion values must substantiate the fixed test contract")
            if not isinstance(entry["passed"], bool):
                errors.append(f"{test.get('id')}.structured_evidence assertion passed must be boolean")
            elif test.get("status") == "PASS" and entry["passed"] is not True:
                errors.append(f"{test.get('id')}.structured_evidence PASS assertions must pass")
    if test.get("status") == "PASS":
        errors.extend(_scenario_measurement_errors(test["id"], envelope.get("measurements"), receipt, envelope.get("started_at"), envelope.get("finished_at")))
    if test.get("id") == "RD-LARGE-01":
        if not isinstance(device, dict) or envelope.get("capability") != device.get("screen_capability"):
            errors.append("RD-LARGE-01 structured evidence capability does not match the receipt device")
    return errors


def validate_artifact(artifact: Any) -> list[str]:
    errors: list[str] = []
    if not _object_shape(
        artifact,
        "artifact",
        required={"artifact_kind", "schema_version", "apk", "build", "runtime", "webui_revision"},
        allowed={"artifact_kind", "schema_version", "apk", "build", "runtime", "webui_revision"},
        errors=errors,
    ):
        return errors
    if artifact.get("artifact_kind") != ARTIFACT_KIND:
        errors.append(f"artifact_kind must be {ARTIFACT_KIND}")
    if type(artifact.get("schema_version")) is not int or artifact.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"artifact schema_version must be {SCHEMA_VERSION}")
    apk = artifact.get("apk")
    build = artifact.get("build")
    runtime = artifact.get("runtime")
    _validate_apk(apk, "artifact.apk", errors)
    if _object_shape(
        build,
        "artifact.build",
        required={"repository", "workflow", "artifact_name", "run_id", "head_sha"},
        allowed={"repository", "workflow", "artifact_name", "run_id", "head_sha"},
        errors=errors,
    ):
        for field in ("repository", "workflow", "artifact_name"):
            _known_identity(build.get(field), f"artifact.build.{field}", errors)
        if isinstance(build.get("repository"), str) and not re.fullmatch(r"[^/\s]+/[^/\s]+", build["repository"]):
            errors.append("artifact.build.repository must be an owner/repository value")
        if not isinstance(build.get("run_id"), str) or not _RUN_ID.fullmatch(build["run_id"]):
            errors.append("artifact.build.run_id must contain only digits")
        _sha(build.get("head_sha"), "artifact.build.head_sha", errors, length=40)
    if _object_shape(
        runtime,
        "artifact.runtime",
        required={"version", "sha256", "source_commit", "payload_id"},
        allowed={"version", "sha256", "source_commit", "payload_id"},
        errors=errors,
    ):
        for field in ("version", "payload_id"):
            _known_identity(runtime.get(field), f"artifact.runtime.{field}", errors)
        _sha(runtime.get("sha256"), "artifact.runtime.sha256", errors)
        _sha(runtime.get("source_commit"), "artifact.runtime.source_commit", errors, length=40)
    _sha(artifact.get("webui_revision"), "artifact.webui_revision", errors, length=40)
    return errors


def validate_release_artifact(artifact: Any) -> list[str]:
    errors = validate_artifact(artifact)
    if errors or not isinstance(artifact, dict):
        return errors
    build = artifact.get("build")
    apk = artifact.get("apk")
    if isinstance(build, dict):
        if build.get("repository") != EXPECTED_REPOSITORY:
            errors.append(f"artifact.build.repository must be {EXPECTED_REPOSITORY}")
        if build.get("workflow") != EXPECTED_WORKFLOW:
            errors.append(f"artifact.build.workflow must be {EXPECTED_WORKFLOW}")
        if build.get("artifact_name") != EXPECTED_ARTIFACT_NAME:
            errors.append(f"artifact.build.artifact_name must be {EXPECTED_ARTIFACT_NAME}")
    if isinstance(apk, dict):
        filename = apk.get("filename")
        if not isinstance(filename, str) or not re.fullmatch(r"hermes-android-[^/\\]+\.apk", filename):
            errors.append("artifact.apk.filename must be the expected hermes-android APK file")
        if apk.get("application_id") != EXPECTED_APPLICATION_ID:
            errors.append(f"artifact.apk.application_id must be {EXPECTED_APPLICATION_ID}")
    return errors


def validate_device(device: Any) -> list[str]:
    errors: list[str] = []
    required = {
        "slot_id",
        "device_id",
        "manufacturer",
        "model",
        "oem",
        "oem_skin",
        "abi",
        "android_release",
        "api_level",
        "build_fingerprint",
        "security_patch_level",
        "serial_sha256",
        "serial_suffix",
        "authorized",
        "authorization_ref",
        "is_emulator",
        "transport",
        "webview",
        "display",
        "screen_capability",
        "input",
        "battery",
        "notification_permission",
        "network",
    }
    if not _object_shape(device, "device", required=required, allowed=required, errors=errors):
        return errors
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
        "authorization_ref",
    ):
        _known_identity(device.get(field), f"device.{field}", errors)
    if device.get("abi") != "arm64-v8a":
        errors.append("device.abi must be arm64-v8a")
    if type(device.get("api_level")) is not int or device["api_level"] not in {33, 34, 35, 36}:
        errors.append("device.api_level must be one of 33, 34, 35, 36")
    _sha(device.get("serial_sha256"), "device.serial_sha256", errors)
    _non_empty(device.get("serial_suffix"), "device.serial_suffix", errors)
    if isinstance(device.get("serial_suffix"), str) and len(device["serial_suffix"]) > 8:
        errors.append("device.serial_suffix must be at most 8 characters")
    if not isinstance(device.get("authorized"), bool) or not device.get("authorized"):
        errors.append("device.authorized must be true for a hardware receipt")
    if device.get("is_emulator") is not False:
        errors.append("device.is_emulator must be false for real hardware")
    transport = device.get("transport")
    if transport not in {"usb", "tcpip"}:
        errors.append("device.transport must be usb or tcpip")
    elif transport.lower() in _EMULATOR_TRANSPORTS or "emulator" in transport.lower():
        errors.append("device.transport identifies an emulator/test endpoint")
    webview = device.get("webview")
    if _object_shape(
        webview,
        "device.webview",
        required={"package", "version", "provider"},
        allowed={"package", "version", "provider"},
        errors=errors,
    ):
        for field in ("package", "version", "provider"):
            _known_identity(webview.get(field), f"device.webview.{field}", errors)
    display = device.get("display")
    if _object_shape(
        display,
        "device.display",
        required={"width", "height", "density"},
        allowed={"width", "height", "density"},
        errors=errors,
    ):
        for field in ("width", "height", "density"):
            if type(display.get(field)) is not int or display[field] <= 0:
                errors.append(f"device.display.{field} must be a positive integer")
    if isinstance(display, dict):
        capability = device.get("screen_capability")
        _validate_screen_capability(capability, display, "device.screen_capability", errors)
        if device.get("slot_id") == "Large-screen" and not _is_large_screen_capability(capability):
            errors.append("device assigned to Large-screen must satisfy the measurable large-screen/foldable predicate")
    input_info = device.get("input")
    if _object_shape(
        input_info,
        "device.input",
        required={"ime", "talkback"},
        allowed={"ime", "talkback"},
        errors=errors,
    ):
        _known_identity(input_info.get("ime"), "device.input.ime", errors)
        if not isinstance(input_info.get("talkback"), bool):
            errors.append("device.input.talkback must be boolean")
    battery = device.get("battery")
    if _object_shape(
        battery,
        "device.battery",
        required={"optimization", "oem_policy"},
        allowed={"optimization", "oem_policy"},
        errors=errors,
    ):
        if battery.get("optimization") not in {"optimized", "exempt"}:
            errors.append("device.battery.optimization must be optimized or exempt; unknown blocks release")
        _known_precondition(battery.get("oem_policy"), "device.battery.oem_policy", errors)
    if device.get("notification_permission") not in {"allow", "deny", "default"}:
        errors.append("device.notification_permission has an invalid value")
    network = device.get("network")
    if _object_shape(
        network,
        "device.network",
        required={"mode", "backend_id"},
        allowed={"mode", "backend_id"},
        errors=errors,
    ):
        if network.get("mode") not in _PRECONDITION_ENUMS["network_mode"]:
            errors.append("device.network.mode must be connected, disconnected or airplane")
        _known_precondition(network.get("backend_id"), "device.network.backend_id", errors)
    return errors


def _validate_test(
    test: Any,
    expected_id: str,
    *,
    artifact_apk: dict[str, Any] | None,
    receipt_root: Path | None = None,
    receipt: dict[str, Any] | None = None,
) -> list[str]:
    errors: list[str] = []
    required = {
        "id",
        "status",
        "expected",
        "observed",
        "reason",
        "failure_class",
        "installed_apk",
        "evidence",
        "redaction_checked",
    }
    if not _object_shape(test, f"tests[{expected_id}]", required=required, allowed=required | {"started_at", "finished_at"}, errors=errors):
        return errors
    if test.get("id") != expected_id:
        errors.append(f"test id mismatch: expected {expected_id}")
    contract = TEST_CONTRACT[expected_id]
    if test.get("expected") != contract["expected"]:
        errors.append(f"{expected_id}.expected must match the fixed test contract")
    status = test.get("status")
    if status not in STATUSES:
        errors.append(f"{expected_id}.status must be one of {STATUSES}")
        return errors
    if not isinstance(test.get("observed"), str):
        errors.append(f"{expected_id}.observed must be a string")
    if test.get("status") == "NOT RUN" and test.get("observed") != "":
        errors.append(f"{expected_id}.observed must be empty for NOT RUN")
    if status in {"PASS", "FAIL"}:
        _non_empty(test.get("observed"), f"{expected_id}.observed", errors)
    if status == "PASS" and test.get("observed") != contract["expected"]:
        errors.append(f"{expected_id}.observed must equal the fixed expected state for PASS")
    _non_empty(test.get("reason"), f"{expected_id}.reason", errors)
    failure_class = test.get("failure_class")
    if failure_class is not None and not isinstance(failure_class, str):
        errors.append(f"{expected_id}.failure_class must be a string or null")
    if failure_class is not None and failure_class not in FAILURE_CLASSES:
        errors.append(f"{expected_id}.failure_class has an invalid value")
    if status in {"FAIL", "BLOCKED"} and failure_class not in FAILURE_CLASSES:
        errors.append(f"{expected_id}.failure_class is required for {status}")
    if status == "PASS" and failure_class is not None:
        errors.append(f"{expected_id}.failure_class must be empty for PASS")
    installed_apk = test.get("installed_apk")
    _validate_installed_apk(installed_apk, f"{expected_id}.installed_apk", errors)
    if artifact_apk is not None and isinstance(installed_apk, dict):
        for field in ("sha256", "size_bytes", "application_id", "version_name", "version_code"):
            if installed_apk.get(field) != artifact_apk.get(field):
                errors.append(f"{expected_id}.installed_apk.{field} must match receipt.artifact.apk")
    if not isinstance(test.get("redaction_checked"), bool) or test.get("redaction_checked") is not True:
        errors.append(f"{expected_id}.redaction_checked must be true")
    errors.extend(_evidence_errors(test, allowed_kinds=contract["evidence_kinds"], receipt_root=receipt_root, receipt=receipt))
    if status in {"PASS", "FAIL"} and not test.get("evidence"):
        errors.append(f"{expected_id}.evidence is required for {status}")
    if status == "NOT RUN" and test.get("evidence"):
        errors.append(f"{expected_id}.evidence must be empty for NOT RUN")
    for field in ("started_at", "finished_at"):
        if field in test:
            _timestamp(test.get(field), f"{expected_id}.{field}", errors)
        elif status in {"PASS", "FAIL"}:
            _timestamp(test.get(field), f"{expected_id}.{field}", errors)
    if status in {"PASS", "FAIL"} and isinstance(test.get("started_at"), str) and isinstance(test.get("finished_at"), str):
        try:
            if datetime.fromisoformat(test["started_at"].replace("Z", "+00:00")) > datetime.fromisoformat(test["finished_at"].replace("Z", "+00:00")):
                errors.append(f"{expected_id} test timestamps must be in chronological order")
        except (ValueError, TypeError):
            pass
    return errors


def validate_receipt(
    receipt: Any,
    matrix: dict[str, Any] | None = None,
    *,
    receipt_root: Path | None = None,
    check_evidence: bool = False,
) -> list[str]:
    errors = _redaction_findings(receipt)
    required = {
        "receipt_kind",
        "schema_version",
        "receipt_id",
        "generated_at",
        "aggregate_status",
        "operator",
        "device",
        "artifact",
        "preconditions",
        "tests",
        "redaction",
    }
    if not _object_shape(receipt, "receipt", required=required, allowed=required, errors=errors):
        return errors
    if receipt.get("receipt_kind") != RECEIPT_KIND:
        errors.append(f"receipt_kind must be {RECEIPT_KIND}")
    if type(receipt.get("schema_version")) is not int or receipt.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    for field in ("receipt_id",):
        _non_empty(receipt.get(field), field, errors)
    _timestamp(receipt.get("generated_at"), "generated_at", errors)
    operator = receipt.get("operator")
    if _object_shape(
        operator,
        "operator",
        required={"id", "authorization_ref"},
        allowed={"id", "authorization_ref"},
        errors=errors,
    ):
        _non_empty(operator.get("id"), "operator.id", errors)
        _known_identity(operator.get("authorization_ref"), "operator.authorization_ref", errors)
    errors.extend(validate_device(receipt.get("device")))
    errors.extend(validate_artifact(receipt.get("artifact")))
    preconditions = receipt.get("preconditions")
    if _object_shape(
        preconditions,
        "preconditions",
        required={"install_mode", "notification_permission", "battery_optimization", "network_mode", "screen_state", "backend_id"},
        allowed={"install_mode", "notification_permission", "battery_optimization", "network_mode", "screen_state", "backend_id"},
        errors=errors,
    ):
        for field in ("install_mode", "notification_permission", "battery_optimization", "network_mode", "screen_state", "backend_id"):
            _validate_precondition_record(preconditions.get(field), f"preconditions.{field}", field, errors)
        device = receipt.get("device")
        if isinstance(device, dict):
            captured_values = {
                "notification_permission": device.get("notification_permission"),
                "battery_optimization": device.get("battery", {}).get("optimization") if isinstance(device.get("battery"), dict) else None,
                "network_mode": device.get("network", {}).get("mode") if isinstance(device.get("network"), dict) else None,
                "backend_id": device.get("network", {}).get("backend_id") if isinstance(device.get("network"), dict) else None,
            }
            for field, captured_value in captured_values.items():
                record = preconditions.get(field)
                if isinstance(record, dict) and record.get("value") != captured_value:
                    errors.append(f"preconditions.{field}.value must match captured device metadata")
    redaction = receipt.get("redaction")
    if _object_shape(
        redaction,
        "redaction",
        required={"policy_version", "raw_serial_stored", "secrets_stored", "patterns_checked"},
        allowed={"policy_version", "raw_serial_stored", "secrets_stored", "patterns_checked"},
        errors=errors,
    ):
        if not isinstance(redaction.get("policy_version"), str) or redaction.get("policy_version") != REDACTION_POLICY_VERSION:
            errors.append(f"redaction.policy_version must be {REDACTION_POLICY_VERSION}")
        for field in ("raw_serial_stored", "secrets_stored", "patterns_checked"):
            if not isinstance(redaction.get(field), bool):
                errors.append(f"redaction.{field} must be boolean")
            elif redaction.get(field) is not False and field != "patterns_checked":
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
            artifact_apk = None
            if isinstance(receipt.get("artifact"), dict) and isinstance(receipt["artifact"].get("apk"), dict):
                artifact_apk = receipt["artifact"]["apk"]
            errors.extend(
                _validate_test(
                    by_id[test_id],
                    test_id,
                    artifact_apk=artifact_apk,
                    receipt_root=receipt_root if check_evidence else None,
                    receipt=receipt,
                )
            )
            installed = by_id[test_id].get("installed_apk")
            install_precondition = preconditions.get("install_mode") if isinstance(preconditions, dict) else None
            if isinstance(installed, dict) and isinstance(install_precondition, dict) and installed.get("package_path") != install_precondition.get("evidence"):
                errors.append(f"{test_id}.installed_apk.package_path must match the captured install precondition evidence")
    statuses = [by_id[test_id].get("status") for test_id in expected_ids if test_id in by_id]
    computed = aggregate_status(statuses)
    if receipt.get("aggregate_status") != computed:
        errors.append(f"aggregate_status must be {computed}, got {receipt.get('aggregate_status')}")
    if not isinstance(receipt.get("aggregate_status"), str) or receipt.get("aggregate_status") not in STATUSES:
        errors.append(f"aggregate_status must be one of {STATUSES}")
    return errors


def validate_matrix(matrix: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(matrix, dict):
        return ["matrix must be an object"]
    if matrix.get("matrix_kind") != "android-device-acceptance-matrix":
        errors.append("matrix_kind is invalid")
    if type(matrix.get("schema_version")) is not int or matrix.get("schema_version") != SCHEMA_VERSION:
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
        if type(slot.get("api_level")) is not int or slot["api_level"] not in {0, 33, 34, 35, 36}:
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
    expected_by_id = {slot["slot_id"]: slot for slot in REQUIRED_SLOTS}
    for slot in slots:
        expected = expected_by_id.get(slot.get("slot_id"))
        if expected is None:
            continue
        for field in ("api_level", "oem", "abi", "tier", "required"):
            if slot.get(field) != expected[field]:
                errors.append(
                    f"matrix slot {slot['slot_id']}.{field} must be {expected[field]!r}, got {slot.get(field)!r}"
                )
    required_api = {slot["api_level"] for slot in slots if slot.get("required") and slot.get("api_level")}
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


def _resolve_repository(
    repository: str | None,
    owner: str | None,
    repo: str | None,
) -> str:
    if repository and (owner or repo):
        raise ValueError("pass either --repository or --owner/--repo, not both")
    if repository:
        value = repository.strip()
    elif owner and repo:
        value = f"{owner.strip()}/{repo.strip()}"
    else:
        raise ValueError("explicit repository provenance is required")
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", value):
        raise ValueError("repository provenance must be an owner/repository value")
    return value


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
    repository: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
    artifact_name: str = "apk",
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
            "signer_sha256": signer_sha256,
        },
        "build": {
            "repository": _resolve_repository(repository, owner, repo),
            "workflow": workflow,
            "artifact_name": artifact_name,
            "run_id": build_run_id,
            "head_sha": head_sha,
        },
        "runtime": {
            "version": runtime_version,
            "sha256": runtime_sha256,
            "source_commit": runtime_source_commit,
            "payload_id": payload_id,
        },
        "webui_revision": webui_revision,
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
    matches = re.findall(r"(?:Physical|Override) size:\s*(\d+)x(\d+)", text)
    if not matches:
        raise BlockedError("cannot capture exact device display size")
    return int(matches[-1][0]), int(matches[-1][1])


def _parse_density(text: str) -> int:
    matches = re.findall(r"(?:Physical|Override) density:\s*(\d+)", text)
    if not matches or int(matches[-1]) <= 0:
        raise BlockedError("cannot capture exact device density")
    return int(matches[-1])


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


def _is_emulator_serial(serial: str) -> bool:
    lower = serial.lower()
    return lower.startswith(("emulator-", "test-", "mock-", "fixture-"))


def _transport_for_serial(serial: str) -> str:
    if re.fullmatch(r"[^:]+:\d+", serial):
        return "tcpip"
    return "usb"


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


def _network_mode(adb: str, serial: str) -> str:
    try:
        raw = _run_adb(adb, serial, "shell", "dumpsys", "connectivity")
    except BlockedError:
        return "unknown"
    upper = raw.upper()
    if "DISCONNECTED" in upper:
        return "disconnected"
    if "CONNECTED" in upper:
        return "connected"
    return "unknown"


def _screen_state(adb: str, serial: str) -> tuple[str, str]:
    raw = _run_adb(adb, serial, "shell", "dumpsys", "power")
    match = re.search(r"\bmWakefulness=(Awake|Asleep|Dozing|Dreaming)\b", raw)
    if not match:
        raise BlockedError("cannot capture exact screen wakefulness")
    return ("interactive" if match.group(1) in ("Awake", "Dreaming") else "non-interactive", match.group(0))


def _screen_capability(width: int, height: int, density: int) -> dict[str, Any]:
    minimum_dp = min(width, height) * 160 // density
    return {
        "kind": "large-screen" if minimum_dp >= 600 else "phone",
        "minimum_dimension_dp": minimum_dp,
        "source": "adb:wm",
    }


def _precondition_record(value: str, field: str, evidence: str | None = None) -> dict[str, str]:
    return {"value": value, "source": _PRECONDITION_SOURCES[field], "observed_at": utc_now(), "evidence": value if evidence is None else evidence}


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
    artifact_errors = validate_release_artifact(artifact)
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
    assert serial is not None
    if _is_emulator_serial(serial):
        raise BlockedError(f"ADB serial identifies an emulator/test endpoint: {serial}")

    props = _parse_getprop(_run_adb(adb, serial, "shell", "getprop"))
    qemu_values = [props.get(key, "").strip().lower() for key in ("ro.kernel.qemu", "ro.boot.qemu", "ro.boot.emulator")]
    if any(value in {"1", "true", "yes"} for value in qemu_values):
        raise BlockedError("device properties identify an emulator/test endpoint")
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
    captured_fields = {
        "manufacturer": manufacturer,
        "model": props.get("ro.product.model", ""),
        "android_release": props.get("ro.build.version.release", ""),
        "build_fingerprint": props.get("ro.build.fingerprint", ""),
        "security_patch_level": props.get("ro.build.version.security_patch", ""),
        "oem_skin": _oem_skin(props),
    }
    for field, value in captured_fields.items():
        if not isinstance(value, str) or value.strip().lower() in _UNKNOWN_LITERALS:
            raise BlockedError(f"cannot capture exact device identity field: {field}")

    width, height = _parse_display(_run_adb(adb, serial, "shell", "wm", "size"))
    density = _parse_density(_run_adb(adb, serial, "shell", "wm", "density"))
    webview_raw = ""
    try:
        webview_raw = _run_adb(adb, serial, "shell", "cmd", "webviewupdate", "get-current-webview-package")
    except BlockedError:
        webview_raw = _run_adb(adb, serial, "shell", "dumpsys", "webviewupdate")
    webview = _parse_webview(webview_raw)
    package = artifact["apk"]["application_id"]
    installed_apk = _installed_apk_metadata(adb, serial, package)
    serial_digest = hashlib.sha256(serial.encode("utf-8")).hexdigest()
    enabled_services = _run_adb(adb, serial, "shell", "settings", "get", "secure", "enabled_accessibility_services")
    default_ime = _run_adb(adb, serial, "shell", "settings", "get", "secure", "default_input_method").strip() or "unknown"
    battery_optimization, oem_policy = _battery_optimization(adb, serial, package)
    notification_permission = _notification_permission(adb, serial, package)
    airplane = _run_adb(adb, serial, "shell", "settings", "get", "global", "airplane_mode_on").strip()
    network_mode = "airplane" if airplane == "1" else _network_mode(adb, serial)
    screen_state, screen_evidence = _screen_state(adb, serial)
    screen_capability = _screen_capability(width, height, density)
    if slot_id == "Large-screen" and not _is_large_screen_capability(screen_capability):
        raise BlockedError("Large-screen capture requires a measured minimum display dimension of at least 600dp")
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
            "is_emulator": False,
            "transport": _transport_for_serial(serial),
            "webview": webview,
            "display": {"width": width, "height": height, "density": density},
            "screen_capability": screen_capability,
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
            "install_mode": _precondition_record("installed", "install_mode", installed_apk["package_path"]),
            "notification_permission": _precondition_record(notification_permission, "notification_permission"),
            "battery_optimization": _precondition_record(battery_optimization, "battery_optimization"),
            "network_mode": _precondition_record(network_mode, "network_mode"),
            "screen_state": _precondition_record(screen_state, "screen_state", screen_evidence),
            "backend_id": _precondition_record(backend_id, "backend_id"),
        },
        "tests": [
            {
                "id": test_id,
                "status": "NOT RUN",
                "expected": TEST_CONTRACT[test_id]["expected"],
                "observed": "",
                "reason": "Not executed; record only after the authorized test step runs.",
                "failure_class": None,
                "installed_apk": dict(installed_apk),
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
        raise BlockedError("device capture has unknown or incomplete release preconditions:\n- " + "\n- ".join(errors))
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
    evidence_kinds: list[str] | None = None,
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
    evidence_redaction_errors: list[str] = []
    if evidence_kinds is not None and len(evidence_kinds) != len(evidence_paths):
        raise HarnessError("evidence-kind count must match evidence count")
    kinds = evidence_kinds or [path.suffix.lstrip(".") or "file" for path in evidence_paths]
    allowed_kinds = TEST_CONTRACT[test_id]["evidence_kinds"]
    invalid_kinds: list[str] = []
    for path, kind in zip(evidence_paths, kinds):
        if kind not in allowed_kinds:
            invalid_kinds.append(kind)
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:
            raise HarnessError(f"evidence must be under the receipt directory: {path}") from exc
        if not resolved.is_file():
            raise HarnessError(f"evidence does not exist: {path}")
        data = resolved.read_bytes()
        evidence_redaction_errors.extend(_evidence_content_findings(data, str(resolved), receipt.get("device", {}).get("serial_sha256")))
        evidence.append({
            "path": relative.as_posix(),
            "sha256": hashlib.sha256(data).hexdigest(),
            "kind": kind,
        })
    if evidence_redaction_errors:
        raise HarnessError("redaction: evidence contains secret-like content:\n- " + "\n- ".join(evidence_redaction_errors))
    if invalid_kinds:
        raise HarnessError(f"evidence kinds {invalid_kinds!r} are not allowed for {test_id}; expected one of {allowed_kinds}")
    test = next((item for item in receipt.get("tests", []) if item.get("id") == test_id), None)
    if test is None:
        raise HarnessError(f"receipt has no test id: {test_id}")
    test.update(
        {
            "status": status,
            "expected": TEST_CONTRACT[test_id]["expected"],
            "observed": observed,
            "reason": reason,
            "failure_class": failure_class,
            "evidence": evidence,
            "redaction_checked": not evidence_redaction_errors,
        }
    )
    if status == "PASS":
        proofs = [path for path, kind in zip(evidence_paths, kinds) if kind == "json"]
        if not proofs:
            raise HarnessError(f"{test_id}.PASS requires a structured JSON evidence proof")
        proof = load_json(proofs[0])
        for field in ("started_at", "finished_at"):
            test[field] = proof.get(field)
    elif status == "FAIL":
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


def evaluate_gate(
    receipts: list[dict[str, Any]],
    matrix: dict[str, Any],
    *,
    receipt_roots: list[Path] | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    slot_reports: list[dict[str, Any]] = []
    valid_receipts: dict[str, dict[str, Any]] = {}
    has_fail = False
    if receipt_roots is None or len(receipt_roots) != len(receipts):
        reasons.append("receipt evidence roots are required for release-gate validation")
        receipt_roots = [Path("")] * len(receipts)
    for index, receipt in enumerate(receipts):
        root = receipt_roots[index]
        if not root or not root.is_dir():
            errors = ["receipt evidence root is missing or is not a directory"]
        else:
            errors = validate_receipt(receipt, matrix, receipt_root=root, check_evidence=True)
            errors.extend(validate_release_artifact(receipt.get("artifact")))
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
        if slot["api_level"] and device["api_level"] != slot["api_level"]:
            mismatch.append(f"api {device['api_level']} != {slot['api_level']}")
        if slot["oem"] != "any" and device["oem"] != slot["oem"]:
            mismatch.append(f"oem {device['oem']} != {slot['oem']}")
        if device["abi"] != slot["abi"]:
            mismatch.append(f"abi {device['abi']} != {slot['abi']}")
        if mismatch:
            reasons.append(f"slot {slot['slot_id']} identity mismatch: {', '.join(mismatch)}")
        if receipt["aggregate_status"] != "PASS":
            reasons.append(f"slot {slot['slot_id']} aggregate status is {receipt['aggregate_status']}")
        slot_reports.append({"slot_id": slot["slot_id"], "status": receipt["aggregate_status"], "receipt_id": receipt["receipt_id"]})
    required_receipts = [
        valid_receipts[slot["slot_id"]]
        for slot in matrix["slots"]
        if slot["required"] and slot["slot_id"] in valid_receipts
    ]
    if required_receipts:
        baseline = artifact_identity(required_receipts[0]["artifact"])
        for receipt in required_receipts[1:]:
            if artifact_identity(receipt["artifact"]) != baseline:
                reasons.append(
                    "required device receipts have mixed artifact identity; "
                    f"slot {receipt['device']['slot_id']} does not match the first required receipt"
                )
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
        repository=args.repository,
        owner=args.owner,
        repo=args.repo,
        artifact_name=args.artifact_name,
        workflow=args.workflow,
    )
    _write_json_atomic(Path(args.output), manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return EXIT_PASS


def _command_validate_artifact(args: argparse.Namespace) -> int:
    path = Path(args.artifact)
    artifact = load_json(path)
    errors = validate_artifact(artifact)
    print(json.dumps({"valid": not errors, "errors": errors, "artifact": str(path)}, ensure_ascii=False, indent=2))
    return EXIT_PASS if not errors else EXIT_FAIL


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
        evidence_kinds=args.evidence_kind or None,
    )
    print(json.dumps({"updated": args.receipt, "aggregate_status": receipt["aggregate_status"]}, ensure_ascii=False, indent=2))
    return EXIT_PASS


def _command_gate(args: argparse.Namespace) -> int:
    matrix = load_matrix(Path(args.matrix))
    root = Path(args.receipts_dir)
    receipts: list[dict[str, Any]] = []
    receipt_roots: list[Path] = []
    if root.is_dir():
        for path in sorted(root.glob("*.json")):
            value = load_json(path)
            if value.get("receipt_kind") == RECEIPT_KIND:
                receipts.append(value)
                receipt_roots.append(path.parent)
    result = evaluate_gate(receipts, matrix, receipt_roots=receipt_roots)
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
    artifact_parser.add_argument("--artifact-name", default="apk")
    artifact_parser.add_argument("--workflow", default="Build APK")
    provenance = artifact_parser.add_mutually_exclusive_group()
    provenance.add_argument("--repository", help="explicit owner/repository provenance")
    provenance.add_argument("--owner", help="repository owner; pair with --repo")
    artifact_parser.add_argument("--repo", help="repository name; pair with --owner")
    artifact_parser.set_defaults(handler=_command_artifact)

    validate_artifact_parser = sub.add_parser("validate-artifact")
    validate_artifact_parser.add_argument("--artifact", required=True)
    validate_artifact_parser.set_defaults(handler=_command_validate_artifact)

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
    record_parser.add_argument("--evidence-kind", action="append", default=[])
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
