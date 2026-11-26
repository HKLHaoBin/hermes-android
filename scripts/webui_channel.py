#!/usr/bin/env python3
"""Resolve WebUI channel tags to immutable commits."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_TAGS = {
    "stable": re.compile(r"^v(\d+)\.(\d+)\.(\d+)$"),
    "canary": re.compile(r"^exp-v(\d+)\.(\d+)\.(\d+)$"),
}
_REQUIRED_LOCK = {"format", "channel", "repository", "tag", "commit", "runtime", "compatibility"}


def _tag_key(channel: str, tag: str) -> tuple[int, int, int]:
    match = _TAGS[channel].fullmatch(tag)
    if not match:
        raise ValueError(f"tag {tag!r} is not an immutable {channel} tag")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _validate_selection(selection: dict, expected_channel: str | None = None) -> dict:
    if not isinstance(selection, dict):
        raise ValueError("WebUI selection must be an object")
    required = {"channel", "repository", "tag", "commit"}
    if set(selection) < required:
        raise ValueError("WebUI selection is missing identity fields")
    channel = selection["channel"]
    if channel not in _TAGS:
        raise ValueError("WebUI channel must be stable or canary")
    if expected_channel and expected_channel != channel:
        raise ValueError(f"selection channel differs: {channel} != {expected_channel}")
    _tag_key(channel, selection["tag"])
    commit = selection["commit"]
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise ValueError("WebUI selection commit must be a 40-hex immutable commit")
    if not isinstance(selection["repository"], str) or not selection["repository"]:
        raise ValueError("WebUI repository is required")
    return selection


def parse_refs(text: str) -> list[dict[str, str]]:
    direct: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) != 2 or not parts[1].startswith("refs/tags/"):
            continue
        raw_tag = parts[1][len("refs/tags/"):]
        peeled_ref = raw_tag.endswith("^{}")
        tag = raw_tag[:-3] if peeled_ref else raw_tag
        commit = parts[0].lower()
        if _COMMIT.fullmatch(commit):
            (peeled if peeled_ref else direct)[tag] = commit
    return [{"tag": tag, "commit": peeled.get(tag, commit)}
            for tag, commit in {**direct, **peeled}.items()]


def resolve_from_refs(text: str, channel: str, repository: str = "nesquena/hermes-webui") -> dict:
    if channel not in _TAGS:
        raise ValueError("WebUI channel must be stable or canary")
    candidates = []
    for ref in parse_refs(text):
        try:
            key = _tag_key(channel, ref["tag"])
        except ValueError:
            continue
        candidates.append((key, ref))
    if not candidates:
        raise ValueError(f"no immutable {channel} tag found")
    _, selected = max(candidates, key=lambda item: item[0])
    return {"format": 1, "channel": channel, "repository": repository,
            "tag": selected["tag"], "commit": selected["commit"]}


def load_lock(path: Path, requested_channel: str | None = None) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not _REQUIRED_LOCK.issubset(data):
        raise ValueError("WebUI lock is missing required fields")
    selection = _validate_selection(data, requested_channel)
    runtime = data["runtime"]
    if not isinstance(runtime, dict) or not runtime.get("channel") or not runtime.get("version"):
        raise ValueError("WebUI lock must bind an immutable runtime pair")
    if not isinstance(data["compatibility"], dict) or not data["compatibility"].get("contract"):
        raise ValueError("WebUI lock must include a compatibility contract")
    return selection


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    resolve = sub.add_parser("resolve")
    resolve.add_argument("--channel", choices=["stable", "canary", "locked"], required=True)
    resolve.add_argument("--lock", type=Path, required=True)
    resolve.add_argument("--refs", type=Path)
    resolve.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    if args.command == "resolve":
        if args.channel == "locked":
            result = load_lock(args.lock)
        else:
            if not args.refs:
                raise SystemExit("--refs is required for stable/canary resolution")
            result = resolve_from_refs(args.refs.read_text(encoding="utf-8"), args.channel)
        args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
