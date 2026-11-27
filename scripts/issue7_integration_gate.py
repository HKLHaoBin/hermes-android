#!/usr/bin/env python3
"""Fail-closed admission and execution of the parent Issue #1 contracts.

Auto mode explicitly identifies an unmixed standalone Issue #7 checkout. Any
parent sentinel, source marker or parent documentation requires the complete
parent contract set. Integration owners can require it even with zero markers.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

REQUIRED = (
    "scripts/test_capabilities.py",
    "android/app/src/main/java/io/github/hermesandroid/JobLedger.kt",
    "scripts/test_job_ledger_contract.py",
    "scripts/test_job_ledger_source.py",
)
PARENT_DOCS = ("docs/capabilities.md", "docs/job-ledger.md")
PARENT_MARKERS = {
    "android/app/src/main/java/io/github/hermesandroid/HermesRuntime.kt": "capabilitiesJson(",
    "android/app/src/main/java/io/github/hermesandroid/HermesService.kt": "JobLedger(",
    "android/app/src/main/assets/ui/index.html": 'id="capabilities"',
    "android/app/src/main/assets/ui/app.js": "renderCapabilities(",
}


def validate_parent_surface(root: Path, require_parent: bool = False) -> bool:
    present = any((root / relative).exists() for relative in (*REQUIRED, *PARENT_DOCS))
    for relative, marker in PARENT_MARKERS.items():
        path = root / relative
        if path.is_file() and marker in path.read_text(encoding="utf-8"):
            present = True
    if not present and not require_parent:
        print("STANDALONE Issue #7: no parent surface; parent integration gate NOT passed.")
        return False
    missing = [name for name in REQUIRED if not (root / name).is_file()]
    if missing:
        raise ValueError("Missing parent contract (partial Issue #1 integration): " + ", ".join(missing))
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--require-parent", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if not validate_parent_surface(root, require_parent=args.require_parent):
            return 0
    except (OSError, ValueError) as error:
        print(f"::error::{error}")
        return 1
    env = os.environ.copy()
    env["HERMES_ISSUE7_INTEGRATION_ROOT"] = str(root)
    commands = [[sys.executable, "-m", "unittest", "scripts.test_issue7_integration_contract"]]
    commands.extend([sys.executable, str(root / name)] for name in (REQUIRED[0], REQUIRED[2], REQUIRED[3]))
    for command in commands:
        result = subprocess.run(command, cwd=root, env=env, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
