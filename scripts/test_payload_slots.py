"""TDD tests for the previous-good payload slot contract."""
from pathlib import Path
import tempfile
import unittest

from payload_update_contract import SlotStateStore


class PayloadSlotTests(unittest.TestCase):
    def test_failed_pending_activation_restores_previous_good(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SlotStateStore(Path(tmp))
            store.activate("old-payload")
            store.mark_healthy()
            store.activate("new-payload")
            self.assertEqual(store.snapshot()["active"], "new-payload")
            self.assertTrue(store.snapshot()["pending"])
            restored = store.rollback()
            self.assertEqual(restored, "old-payload")
            self.assertEqual(store.snapshot()["active"], "old-payload")
            self.assertFalse(store.snapshot()["pending"])
            self.assertEqual(store.snapshot()["previous_good"], "new-payload")

    def test_successful_activation_keeps_previous_good_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SlotStateStore(Path(tmp))
            store.activate("old")
            store.mark_healthy()
            store.activate("new")
            store.mark_healthy()
            before = store.snapshot()
            store.activate("new")
            self.assertEqual(store.snapshot(), before)
            self.assertEqual(before["previous_good"], "old")
            self.assertFalse(before["pending"])

    def test_first_pending_activation_without_previous_has_no_unsafe_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SlotStateStore(Path(tmp))
            store.activate("first")
            self.assertIsNone(store.rollback())
            self.assertIsNone(store.snapshot()["active"])
            self.assertFalse(store.snapshot()["pending"])


if __name__ == "__main__":
    unittest.main()
