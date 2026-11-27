"""TDD tests for canonical payload identity helpers."""
from pathlib import Path
import tempfile
import unittest

from payload_identity import canonical_bytes, sha256_tree, build_payload_id


class PayloadIdentityTests(unittest.TestCase):
    def test_tree_hash_is_order_independent_but_content_sensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "b").write_text("two", encoding="utf-8")
            (root / "a").write_text("one", encoding="utf-8")
            first = sha256_tree(root)
            (root / "a").write_text("changed", encoding="utf-8")
            second = sha256_tree(root)
            self.assertNotEqual(first, second)
            self.assertEqual(len(first), 64)

    def test_payload_id_is_full_digest_of_identity(self):
        identity = {
            "runtime": {"version": "r1", "source_commit": "a" * 40},
            "webui": {"channel": "stable", "tag": "v1", "commit": "b" * 40},
            "compatibility": {"contract": "c1", "status": "verified"},
            "zip_sha256": "d" * 64,
        }
        value = build_payload_id(identity)
        self.assertEqual(len(value), 64)
        self.assertEqual(value, build_payload_id(identity))
        self.assertNotIn("api_key", canonical_bytes(identity).decode())


if __name__ == "__main__":
    unittest.main()
