"""TDD tests for deterministic receipts and detached RSA signatures."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from update_receipt import canonical_bytes, build_receipt, sign_receipt, verify_receipt


class UpdateReceiptTests(unittest.TestCase):
    def test_receipt_is_deterministic_and_redacts_no_runtime_secrets(self):
        manifest = {
            "format": 2,
            "payload_id": "payload-1",
            "runtime": {"source_commit": "a" * 40, "version": "1"},
            "webui": {"channel": "stable", "commit": "b" * 40},
            "compatibility": {"status": "verified"},
        }
        first = build_receipt(manifest, "c" * 64, "d" * 64)
        second = build_receipt(json.loads(json.dumps(manifest)), "c" * 64, "d" * 64)
        self.assertEqual(canonical_bytes(first), canonical_bytes(second))
        self.assertNotIn(b"token", canonical_bytes(first).lower())
        self.assertEqual(first["manifest_sha256"], "d" * 64)
        self.assertEqual(first["apk_sha256"], "c" * 64)

    def test_signature_verifies_and_modified_receipt_fails(self):
        openssl = shutil.which("openssl")
        if not openssl:
            self.skipTest("openssl is required for receipt signature contract")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = root / "key.pem"
            cert = root / "cert.pem"
            sig = root / "receipt.sig"
            subprocess.run([
                openssl, "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                "-out", str(key),
            ], check=True, capture_output=True)
            subprocess.run([
                openssl, "req", "-new", "-x509", "-key", str(key), "-out", str(cert),
                "-subj", "/CN=Hermes test receipt", "-days", "1",
            ], check=True, capture_output=True)
            receipt = build_receipt({"format": 2, "payload_id": "x"}, "a" * 64, "b" * 64)
            sign_receipt(receipt, key, sig)
            self.assertTrue(verify_receipt(receipt, sig, cert))
            receipt["payload_id"] = "tampered"
            self.assertFalse(verify_receipt(receipt, sig, cert))


if __name__ == "__main__":
    unittest.main()
