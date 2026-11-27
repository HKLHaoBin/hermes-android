#!/usr/bin/env python3
"""Verify every artifact-manifest field against downloaded build inputs."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from android_device_matrix import HarnessError, load_json, sha256_file, validate_artifact


_PACKAGE_BADGING = re.compile(
    r"^package:\s+name='(?P<application_id>[^']+)'\s+"
    r"versionCode='(?P<version_code>[0-9]+)'\s+"
    r"versionName='(?P<version_name>[^']*)'"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _read_text(path: Path, label: str) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise HarnessError(f"cannot read {label}: {path}: {exc}") from exc
    if not value:
        raise HarnessError(f"{label} is empty: {path}")
    return value


def parse_aapt_badging(output: str) -> dict[str, Any]:
    for line in output.splitlines():
        match = _PACKAGE_BADGING.match(line.strip())
        if match:
            return {
                "application_id": match.group("application_id"),
                "version_code": int(match.group("version_code")),
                "version_name": match.group("version_name"),
            }
    raise HarnessError("aapt dump badging did not expose package, versionCode and versionName")


def read_apk_metadata(aapt: Path, apk: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [str(aapt), "dump", "badging", str(apk)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise HarnessError(f"cannot execute aapt: {exc}") from exc
    if completed.returncode != 0:
        raise HarnessError(f"aapt dump badging failed: {(completed.stderr or completed.stdout).strip()}")
    return parse_aapt_badging(completed.stdout)


def _compare(errors: list[str], label: str, expected: Any, actual: Any) -> None:
    if expected != actual:
        errors.append(f"{label} mismatch: manifest={expected!r}, downloaded={actual!r}")


def verify_manifest(
    *,
    manifest_path: Path,
    apk_path: Path,
    payload_manifest_path: Path,
    payload_artifact_manifest_path: Path,
    payload_zip_path: Path,
    runtime_meta_path: Path,
    install_stamp_path: Path,
    webui_revision_path: Path,
    release_cert_path: Path,
    aapt_path: Path,
    signer_sha256: str,
    repository: str,
    workflow: str,
    artifact_name: str,
    run_id: str,
    head_sha: str,
) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    errors = validate_artifact(manifest)
    if errors:
        raise HarnessError("invalid artifact manifest:\n- " + "\n- ".join(errors))

    build = manifest["build"]
    _compare(errors, "build.repository", build["repository"], repository)
    _compare(errors, "build.workflow", build["workflow"], workflow)
    _compare(errors, "build.artifact_name", build["artifact_name"], artifact_name)
    _compare(errors, "build.run_id", build["run_id"], run_id)
    _compare(errors, "build.head_sha", build["head_sha"], head_sha)

    apk_manifest = manifest["apk"]
    apk_digest = sha256_file(apk_path)
    _compare(errors, "apk.filename", apk_manifest["filename"], apk_path.name)
    _compare(errors, "apk.sha256", apk_manifest["sha256"], apk_digest)
    _compare(errors, "apk.size_bytes", apk_manifest["size_bytes"], apk_path.stat().st_size)
    apk_metadata = read_apk_metadata(aapt_path, apk_path)
    for field in ("application_id", "version_name", "version_code"):
        _compare(errors, f"apk.{field}", apk_manifest[field], apk_metadata[field])
    signer = signer_sha256.lower().strip()
    if not _SHA256.fullmatch(signer):
        errors.append("downloaded APK signer SHA-256 is not a 64-character lowercase hex digest")
    _compare(errors, "apk.signer_sha256", apk_manifest["signer_sha256"], signer)
    _compare(errors, "release certificate", _read_text(release_cert_path, "release certificate").lower(), signer)

    runtime_meta = load_json(runtime_meta_path)
    install_stamp = load_json(install_stamp_path)
    payload = load_json(payload_manifest_path)
    if hashlib.sha256(payload_manifest_path.read_bytes()).hexdigest() != hashlib.sha256(payload_artifact_manifest_path.read_bytes()).hexdigest():
        errors.append("payload manifest in apk artifact differs from payload Actions artifact")
    runtime = manifest["runtime"]
    _compare(errors, "runtime.version", runtime["version"], runtime_meta.get("version"))
    _compare(errors, "runtime.sha256", runtime["sha256"], runtime_meta.get("sha256"))
    _compare(errors, "runtime.source_commit", runtime["source_commit"], install_stamp.get("commit"))
    _compare(errors, "runtime.payload_id", runtime["payload_id"], payload.get("payload_id"))
    upstream = payload.get("upstream", {})
    if isinstance(upstream, dict):
        upstream_deb = upstream.get("deb", {})
        upstream_stamp = upstream.get("stamp", {})
        _compare(errors, "payload.upstream.deb.version", runtime_meta.get("version"), upstream_deb.get("version"))
        _compare(errors, "payload.upstream.deb.sha256", runtime_meta.get("sha256"), upstream_deb.get("sha256"))
        _compare(errors, "payload.upstream.stamp.commit", install_stamp.get("commit"), upstream_stamp.get("commit"))
    webui_revision = _read_text(webui_revision_path, "WebUI revision")
    _compare(errors, "webui_revision", manifest["webui_revision"], webui_revision)

    if payload_zip_path.stat().st_size != payload.get("zip_bytes"):
        errors.append("payload zip size differs from payload manifest")
    if isinstance(runtime_meta.get("version"), str) and isinstance(runtime_meta.get("sha256"), str):
        expected_payload_id = (
            f"{runtime_meta['version']}-{runtime_meta['sha256'][:12]}-webui-"
            f"{sha256_file(payload_zip_path)[:16]}"
        )
        _compare(errors, "payload_id derived from runtime/WebUI payload", payload.get("payload_id"), expected_payload_id)

    if errors:
        raise HarnessError("artifact provenance mismatch:\n- " + "\n- ".join(errors))
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--payload-manifest", type=Path, required=True)
    parser.add_argument("--payload-artifact-manifest", type=Path, required=True)
    parser.add_argument("--payload-zip", type=Path, required=True)
    parser.add_argument("--runtime-meta", type=Path, required=True)
    parser.add_argument("--install-stamp", type=Path, required=True)
    parser.add_argument("--webui-revision-file", type=Path, required=True)
    parser.add_argument("--release-cert-file", type=Path, required=True)
    parser.add_argument("--aapt", type=Path, required=True)
    parser.add_argument("--signer-sha256", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--artifact-name", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--head-sha", required=True)
    args = parser.parse_args(argv)
    verify_manifest(
        manifest_path=args.manifest,
        apk_path=args.apk,
        payload_manifest_path=args.payload_manifest,
        payload_artifact_manifest_path=args.payload_artifact_manifest,
        payload_zip_path=args.payload_zip,
        runtime_meta_path=args.runtime_meta,
        install_stamp_path=args.install_stamp,
        webui_revision_path=args.webui_revision_file,
        release_cert_path=args.release_cert_file,
        aapt_path=args.aapt,
        signer_sha256=args.signer_sha256,
        repository=args.repository,
        workflow=args.workflow,
        artifact_name=args.artifact_name,
        run_id=args.run_id,
        head_sha=args.head_sha,
    )
    print(json.dumps({"verified": True, "manifest": str(args.manifest), "apk": str(args.apk)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
