#!/usr/bin/env python3
"""Create and verify deterministic signed release receipts without credentials."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from payload_identity import canonical_bytes, sha256_file


def build_receipt(manifest: dict[str, Any], apk_sha256: str, manifest_sha256: str,
                  source_revisions: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "format": 1,
        "manifest_sha256": manifest_sha256,
        "apk_sha256": apk_sha256,
        "payload_id": manifest.get("payload_id", ""),
        "runtime": manifest.get("runtime", {}),
        "webui": manifest.get("webui", {}),
        "compatibility": manifest.get("compatibility", {}),
        "source_revisions": source_revisions or {},
    }


def _read_receipt(value: Path | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, Path):
        return json.loads(value.read_text(encoding="utf-8"))
    return value


def sign_receipt(receipt: Path | dict[str, Any], private_key: Path, signature: Path) -> None:
    result = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(private_key)],
        input=canonical_bytes(_read_receipt(receipt)), capture_output=True, check=True,
    )
    signature.write_bytes(result.stdout)


def verify_receipt(receipt: Path | dict[str, Any], signature: Path, certificate: Path) -> bool:
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
        public_key = Path(tmp) / "public.pem"
        extracted = subprocess.run(
            ["openssl", "x509", "-pubkey", "-noout", "-in", str(certificate)],
            capture_output=True, check=False,
        )
        if extracted.returncode != 0:
            return False
        public_key.write_bytes(extracted.stdout)
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-verify", str(public_key), "-signature", str(signature)],
            input=canonical_bytes(_read_receipt(receipt)), capture_output=True, check=False,
        )
        return result.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--manifest", type=Path, required=True)
    create.add_argument("--apk", type=Path, required=True)
    create.add_argument("--out", type=Path, required=True)
    create.add_argument("--source-revisions", type=Path)
    sign = sub.add_parser("sign")
    sign.add_argument("--receipt", type=Path, required=True)
    sign.add_argument("--key", type=Path, required=True)
    sign.add_argument("--out", type=Path, required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--receipt", type=Path, required=True)
    verify.add_argument("--signature", type=Path, required=True)
    verify.add_argument("--cert", type=Path, required=True)
    args = ap.parse_args()
    if args.command == "create":
        manifest_bytes = args.manifest.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        source = json.loads(args.source_revisions.read_text(encoding="utf-8")) if args.source_revisions else {}
        receipt = build_receipt(manifest, sha256_file(args.apk), sha256_file(args.manifest), source)
        args.out.write_bytes(canonical_bytes(receipt))
        return 0
    if args.command == "sign":
        sign_receipt(args.receipt, args.key, args.out)
        return 0
    if args.command == "verify":
        return 0 if verify_receipt(args.receipt, args.signature, args.cert) else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
