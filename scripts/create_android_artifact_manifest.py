#!/usr/bin/env python3
"""Create the Actions APK artifact identity receipt from verified build outputs."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from android_device_matrix import HarnessError, _write_json_atomic, build_artifact_manifest, load_json


def _read_text(path: Path, name: str) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise HarnessError(f"cannot read {name}: {path}: {exc}") from exc
    if not value:
        raise HarnessError(f"{name} is empty: {path}")
    return value


def create_manifest(args: argparse.Namespace) -> dict:
    runtime_meta = load_json(args.runtime_meta)
    install_stamp = load_json(args.install_stamp)
    payload = load_json(args.payload_manifest)
    runtime_version = runtime_meta.get("version")
    runtime_sha256 = runtime_meta.get("sha256")
    runtime_source_commit = install_stamp.get("commit")
    payload_id = payload.get("payload_id")
    if not isinstance(runtime_version, str) or not runtime_version:
        raise HarnessError("runtime meta is missing version")
    if not isinstance(runtime_sha256, str) or not runtime_sha256:
        raise HarnessError("runtime meta is missing sha256")
    if not isinstance(runtime_source_commit, str) or not runtime_source_commit:
        raise HarnessError("install stamp is missing commit")
    if not isinstance(payload_id, str) or not payload_id:
        raise HarnessError("payload manifest is missing payload_id")

    upstream = payload.get("upstream")
    if isinstance(upstream, dict):
        upstream_deb = upstream.get("deb")
        upstream_stamp = upstream.get("stamp")
        if isinstance(upstream_deb, dict) and upstream_deb.get("sha256") not in {None, runtime_sha256}:
            raise HarnessError("payload upstream runtime SHA-256 differs from runtime meta")
        if isinstance(upstream_stamp, dict) and upstream_stamp.get("commit") not in {None, runtime_source_commit}:
            raise HarnessError("payload upstream source commit differs from install stamp")

    return build_artifact_manifest(
        args.apk,
        application_id=args.application_id,
        version_name=args.version_name,
        version_code=args.version_code,
        signer_sha256=_read_text(args.signer_file, "signer SHA-256"),
        build_run_id=args.run_id,
        head_sha=args.head_sha,
        runtime_version=runtime_version,
        runtime_sha256=runtime_sha256,
        runtime_source_commit=runtime_source_commit,
        payload_id=payload_id,
        webui_revision=_read_text(args.webui_revision_file, "WebUI revision"),
        repository=args.repository,
        artifact_name=args.artifact_name,
        workflow=args.workflow,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-meta", type=Path, required=True)
    parser.add_argument("--install-stamp", type=Path, required=True)
    parser.add_argument("--payload-manifest", type=Path, required=True)
    parser.add_argument("--webui-revision-file", type=Path, required=True)
    parser.add_argument("--signer-file", type=Path, required=True)
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--version-name", required=True)
    parser.add_argument("--version-code", type=int, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--artifact-name", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--head-sha", required=True)
    args = parser.parse_args(argv)
    manifest = create_manifest(args)
    _write_json_atomic(args.output, manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
