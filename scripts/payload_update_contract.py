#!/usr/bin/env python3
"""Portable model of the Android active/previous-good payload state contract."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class SlotStateStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = root / "active.json"

    def snapshot(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"format": 1, "generation": 0, "active": None, "previous_good": None, "pending": False}
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
        if current["active"] == slot_id and not current["pending"]:
            return current
        next_state = {
            "format": 1,
            "generation": int(current.get("generation", 0)) + 1,
            "active": slot_id,
            "previous_good": current.get("active"),
            "pending": True,
        }
        self._write(next_state)
        return next_state

    def mark_healthy(self) -> dict[str, Any]:
        current = self.snapshot()
        current["pending"] = False
        self._write(current)
        return current

    def rollback(self) -> str | None:
        current = self.snapshot()
        if not current.get("pending"):
            return current.get("active")
        failed = current.get("active")
        previous = current.get("previous_good")
        current["active"] = previous
        current["previous_good"] = failed if previous else None
        current["pending"] = False
        current["generation"] = int(current.get("generation", 0)) + 1
        self._write(current)
        return previous

    def recover_pending(self) -> str | None:
        return self.rollback() if self.snapshot().get("pending") else self.snapshot().get("active")
