"""CLI contract for producing a signed receipt artifact."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class UpdateReceiptCliTests(unittest.TestCase):
    def test_workflow_receipt_checksum_survives_artifact_root_download(self):
        root = Path(__file__).resolve().parents[1]
        build = (root / ".github/workflows/build-apk.yml").read_text(encoding="utf-8")
        publish = (root / ".github/workflows/publish-apk.yml").read_text(encoding="utf-8")
        producers = [line.strip() for line in build.splitlines()
                     if "sha256sum" in line and "update-receipt.json.sha256" in line]
        consumers = [line.strip() for line in publish.splitlines()
                     if "sha256sum --check" in line and "update-receipt.json.sha256" in line]
        self.assertEqual(len(producers), 1)
        self.assertEqual(len(consumers), 1)
        bash = shutil.which("bash")
        assert bash is not None, "bash is required for the workflow checksum contract"
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            dist = work / "dist"
            dist.mkdir()
            # A receipt fixture, not a built APK or a live release artifact.
            (dist / "update-receipt.json").write_text('{"format":1}\n', encoding="utf-8")
            subprocess.run([bash, "-ec", producers[0]], cwd=work, check=True, capture_output=True)
            checked = subprocess.run([bash, "-ec", consumers[0]], cwd=dist, capture_output=True)
            self.assertEqual(checked.returncode, 0, checked.stderr.decode("utf-8", errors="replace"))
            checksum = (dist / "update-receipt.json.sha256").read_text(encoding="utf-8")
            # GNU checksum files may carry a '*' binary-mode marker on Windows.
            self.assertEqual(checksum.strip().split()[-1].removeprefix("*"), "update-receipt.json")

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
