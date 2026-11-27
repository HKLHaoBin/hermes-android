#!/usr/bin/env python3
"""Portable model of the Android active/previous-good payload state contract."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Callable


class SlotStateStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = root / "active.json"

    def snapshot(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {
                "format": 1,
                "generation": 0,
                "active": None,
                "previous_good": None,
                "pending": False,
                "rejected": None,
            }
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return data

    def _write(self, data: dict[str, Any]) -> None:
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        temp.replace(self.path)

    def activate(self, slot_id: str) -> dict[str, Any]:
        if not slot_id or "/" in slot_id or "\\" in slot_id or slot_id in {".", ".."}:
            raise ValueError("unsafe slot id")
        current = self.snapshot()
        if current.get("rejected") == slot_id:
            raise ValueError("refusing rejected payload")
        if current["active"] == slot_id:
            return current
        if current["pending"]:
            raise ValueError("recover pending activation before replacing it")
        self.create_slot(slot_id)
        next_state = {
            "format": 1,
            "generation": int(current.get("generation", 0)) + 1,
            "active": slot_id,
            "previous_good": current.get("active"),
            "pending": True,
            "rejected": None,
        }
        self._write(next_state)
        return next_state

    def mark_healthy(self) -> dict[str, Any]:
        current = self.snapshot()
        if current.get("pending"):
            current["pending"] = False
            current["rejected"] = None
            self._write(current)
        self.prune_slots()
        return current

    def rollback(self, before_discard: Callable[[Path], None] | None = None) -> str | None:
        current = self.snapshot()
        if not current.get("pending"):
            return current.get("active")
        failed = current.get("active")
        previous = current.get("previous_good")
        if previous == failed or not (self.root / "slots" / str(previous)).is_dir():
            previous = None
        if failed:
            failed_path = self.root / "slots" / failed
            if before_discard:
                before_discard(failed_path)
        current["active"] = previous
        current["previous_good"] = None
        current["pending"] = False
        current["rejected"] = failed
        current["generation"] = int(current.get("generation", 0)) + 1
        self._write(current)
        if failed:
            path = self.root / "slots" / failed
            if path.exists():
                shutil.rmtree(path)
        self.prune_slots()
        return previous

    def recover_pending(self, before_discard: Callable[[Path], None] | None = None) -> str | None:
        return self.rollback(before_discard) if self.snapshot().get("pending") else self.snapshot().get("active")

    def select_payload(self, included_payload_id: str) -> str | None:
        current = self.snapshot()
        if current.get("rejected") == included_payload_id:
            return current.get("active")
        return included_payload_id

    def create_slot(self, slot_id: str) -> None:
        (self.root / "slots" / slot_id).mkdir(parents=True, exist_ok=True)

    def slot_ids(self) -> set[str]:
        slots = self.root / "slots"
        if not slots.is_dir():
            return set()
        return {path.name for path in slots.iterdir() if path.is_dir()}

    def prune_slots(self) -> None:
        current = self.snapshot()
        keep = {current.get("active"), current.get("previous_good")} - {None}
        slots = self.root / "slots"
        if not slots.is_dir():
            return
        for path in slots.iterdir():
            if path.name not in keep:
                if path.is_symlink() or not path.is_dir():
                    path.unlink()
                else:
                    shutil.rmtree(path)
