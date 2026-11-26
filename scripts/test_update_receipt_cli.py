"""CLI contract for producing a signed receipt artifact."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class UpdateReceiptCliTests(unittest.TestCase):
    def test_create_and_sign_cli_round_trip(self):
        openssl = shutil.which("openssl")
        if not openssl:
            self.skipTest("openssl is required")
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            manifest = d / "payload.json"
            apk = d / "app.apk"
            key = d / "key.pem"
            cert = d / "cert.pem"
            receipt = d / "receipt.json"
            signature = d / "receipt.sig"
            manifest.write_text(json.dumps({"format": 2, "payload_id": "p"}), encoding="utf-8")
            apk.write_bytes(b"apk")
            subprocess.run([openssl, "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(key)], check=True, capture_output=True)
            subprocess.run([openssl, "req", "-new", "-x509", "-key", str(key), "-out", str(cert), "-subj", "/CN=receipt", "-days", "1"], check=True, capture_output=True)
            subprocess.run(["python3", str(root / "scripts/update_receipt.py"), "create", "--manifest", str(manifest), "--apk", str(apk), "--out", str(receipt)], check=True)
            subprocess.run(["python3", str(root / "scripts/update_receipt.py"), "sign", "--receipt", str(receipt), "--key", str(key), "--out", str(signature)], check=True)
            result = subprocess.run(["python3", str(root / "scripts/update_receipt.py"), "verify", "--receipt", str(receipt), "--signature", str(signature), "--cert", str(cert)], check=False)
            self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
