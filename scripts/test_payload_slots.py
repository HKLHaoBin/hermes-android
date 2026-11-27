"""TDD tests for the previous-good payload slot contract."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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
            self.assertIsNone(store.snapshot()["previous_good"])
            self.assertEqual(store.snapshot()["rejected"], "new-payload")

    def test_rejected_payload_is_not_selected_again_until_a_new_identity_arrives(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SlotStateStore(Path(tmp))
            store.activate("old")
            store.mark_healthy()
            store.activate("bad")
            store.rollback()
            store.mark_healthy()
            self.assertEqual(store.select_payload("bad"), "old")
            self.assertEqual(store.select_payload("new"), "new")

    def test_recovery_callback_runs_before_failed_slot_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SlotStateStore(Path(tmp))
            store.activate("old")
            store.mark_healthy()
            store.activate("bad")
            observed = []

            store.recover_pending(lambda failed: observed.append(failed.exists()))

            self.assertEqual(observed, [True])
            self.assertFalse((Path(tmp) / "slots" / "bad").exists())

    def test_quiescence_failure_keeps_pending_state_and_both_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SlotStateStore(Path(tmp))
            store.activate("old")
            store.mark_healthy()
            store.activate("bad")
            before = store.snapshot()

            def cannot_stop(_):
                raise OSError("runtime still alive")

            with self.assertRaises(OSError):
                store.recover_pending(cannot_stop)
            self.assertEqual(store.snapshot(), before)
            self.assertEqual(store.slot_ids(), {"old", "bad"})

    def test_failed_health_commit_keeps_pending_fallback_for_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SlotStateStore(Path(tmp))
            store.activate("old")
            store.mark_healthy()
            store.activate("bad")
            before = store.snapshot()
            with patch.object(store, "_write", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.mark_healthy()
            self.assertEqual(store.snapshot(), before)
            self.assertEqual(store.recover_pending(), "old")
            self.assertEqual(store.select_payload("bad"), "old")

    def test_repeated_updates_have_only_active_and_one_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SlotStateStore(Path(tmp))
            previous = None
            for candidate in ("one", "two", "three", "four"):
                store.activate(candidate)
                store.mark_healthy()
                expected = {candidate} | ({previous} if previous else set())
                self.assertEqual(store.slot_ids(), expected)
                previous = candidate

    def test_ordinary_health_check_also_prunes_orphans_and_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SlotStateStore(Path(tmp))
            store.activate("active")
            store.mark_healthy()
            store.create_slot("orphan")
            store.create_slot(".staging-interrupted")
            store.mark_healthy()
            self.assertEqual(store.slot_ids(), {"active"})

    def test_direct_activation_cannot_bypass_the_rejected_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SlotStateStore(Path(tmp))
            store.activate("old")
            store.mark_healthy()
            store.activate("bad")
            store.rollback()
            with self.assertRaisesRegex(ValueError, "rejected"):
                store.activate("bad")
            self.assertEqual(store.slot_ids(), {"old"})

    def test_quarantine_survives_restart_and_restored_health_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SlotStateStore(Path(tmp))
            store.activate("old")
            store.mark_healthy()
            store.activate("bad")
            store.recover_pending()
            restarted = SlotStateStore(Path(tmp))
            restarted.mark_healthy()
            self.assertEqual(restarted.snapshot()["rejected"], "bad")
            self.assertEqual(restarted.select_payload("bad"), "old")
            restarted.activate("new")
            restarted.mark_healthy()
            self.assertEqual(restarted.snapshot()["previous_good"], "old")
            self.assertEqual(restarted.slot_ids(), {"old", "new"})

    def test_prune_keeps_only_active_and_previous_good_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SlotStateStore(Path(tmp))
            for slot in ("old", "older", "oldest", "new"):
                store.create_slot(slot)
            store.activate("old")
            store.mark_healthy()
            store.activate("new")
            store.mark_healthy()
            store.prune_slots()
            self.assertEqual(store.slot_ids(), {"old", "new"})

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

    def test_user_cancellation_rolls_back_without_quarantining_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SlotStateStore(Path(tmp))
            store.activate("old")
            store.mark_healthy()
            store.activate("candidate")
            self.assertEqual(store.rollback(reject_failed=False), "old")
            self.assertIsNone(store.snapshot()["rejected"])
            store.activate("candidate")

    def test_reusing_previous_good_preserves_it_when_activation_is_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SlotStateStore(Path(tmp))
            store.create_slot("active")
            store.create_slot("retained")
            store._write({
                "format": 1,
                "generation": 4,
                "active": "active",
                "previous_good": "retained",
                "pending": False,
                "rejected": None,
                "reused_previous_good": False,
            })
            store.activate("retained", reuse_previous_good=True)
            self.assertEqual(store.rollback(reject_failed=False), "active")
            self.assertEqual(store.snapshot()["previous_good"], "retained")
            self.assertEqual(store.slot_ids(), {"active", "retained"})


if __name__ == "__main__":
    unittest.main()
