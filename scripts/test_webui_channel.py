"""TDD contract tests for immutable stable/canary WebUI selection."""
from pathlib import Path
import json
import unittest

from webui_channel import load_lock, resolve_from_refs

ROOT = Path(__file__).resolve().parents[1]


class WebUIChannelTests(unittest.TestCase):
    def test_stable_and_canary_resolve_only_immutable_tag_refs(self):
        refs = "\n".join([
            "1111111111111111111111111111111111111111\trefs/tags/v0.52.8",
            "2222222222222222222222222222222222222222\trefs/tags/v0.52.9",
            "3333333333333333333333333333333333333333\trefs/tags/exp-v0.52.10",
            "4444444444444444444444444444444444444444\trefs/tags/exp-v0.52.11",
            "5555555555555555555555555555555555555555\trefs/heads/master",
        ])
        stable = resolve_from_refs(refs, "stable")
        canary = resolve_from_refs(refs, "canary")
        self.assertEqual((stable["tag"], stable["commit"]), ("v0.52.9", "2" * 40))
        self.assertEqual((canary["tag"], canary["commit"]), ("exp-v0.52.11", "4" * 40))
        self.assertEqual(stable["channel"], "stable")
        self.assertEqual(canary["channel"], "canary")

    def test_branch_refs_and_wrong_channel_tags_fail_closed(self):
        refs = "\n".join([
            "a" * 40 + "\trefs/heads/master",
            "b" * 40 + "\trefs/tags/exp-v0.52.2",
        ])
        with self.assertRaisesRegex(ValueError, "stable"):
            resolve_from_refs(refs, "stable")
        with self.assertRaisesRegex(ValueError, "immutable canary tag"):
            resolve_from_refs("c" * 40 + "\trefs/heads/canary\n", "canary")

    def test_annotated_tags_use_peeled_commit_not_tag_object(self):
        refs = "\n".join([
            "1" * 40 + "\trefs/tags/v0.52.12",
            "2" * 40 + "\trefs/tags/v0.52.12^{}",
        ])
        selected = resolve_from_refs(refs, "stable")
        self.assertEqual(selected["commit"], "2" * 40)

    def test_reviewed_lock_is_literal_and_has_pair_metadata(self):
        lock = load_lock(ROOT / "webui.lock.json")
        self.assertEqual(lock["channel"], "canary")
        self.assertEqual(lock["tag"], "exp-v0.52.558")
        self.assertEqual(lock["commit"], "21426ee7ffcd620f5e9d53401ee21c4c6dbb9c9d")
        self.assertEqual(lock["repository"], "nesquena/hermes-webui")
        self.assertIn("runtime", lock)
        self.assertIn("compatibility", lock)

    def test_lock_does_not_contain_credentials(self):
        text = (ROOT / "webui.lock.json").read_text(encoding="utf-8").lower()
        for forbidden in ("api_key", "authorization", "cookie", "password", "token", "secret"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
