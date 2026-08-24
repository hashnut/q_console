"""Incremental scan cache.

A full pass over 443 Codex rollouts costs ~10 s. The tray refreshes every 30
minutes and on every click, so re-parsing everything each time would burn CPU
for data that cannot have changed: a session file that is closed never changes
again. We key each file's parsed result on (size, mtime) and only re-read the
ones whose signature moved.

The cache is a plain JSON file in AppHome. If it is corrupt or from an older
layout we throw it away and do a cold pass - it is derived data, never a source.
"""

from __future__ import annotations

import json
import os

from . import config

VERSION = 3


def _load_all() -> dict:
    try:
        with open(config.SCAN_PATH, encoding="utf-8") as fh:
            blob = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(blob, dict) or blob.get("version") != VERSION:
        return {}
    entries = blob.get("entries")
    return entries if isinstance(entries, dict) else {}


def _save_all(entries: dict) -> None:
    try:
        config.write_atomic(
            config.SCAN_PATH,
            json.dumps({"version": VERSION, "entries": entries}, separators=(",", ":")),
        )
    except OSError:
        pass  # a cache we cannot write just means the next pass is cold


class Scanner:
    """Per-namespace view of the shared cache ('claude' / 'codex')."""

    def __init__(self, namespace: str):
        self.namespace = namespace
        self._all = _load_all()
        self._mine = self._all.get(namespace) or {}
        self._next = {}

    def get(self, path: str, sig):
        entry = self._mine.get(path)
        if entry and entry.get("sig") == sig:
            return entry.get("data")
        return None

    def put(self, path: str, sig, data) -> None:
        self._next[path] = {"sig": sig, "data": data}

    def keep(self, path: str) -> None:
        """Carry an unchanged entry forward without re-parsing."""
        entry = self._mine.get(path)
        if entry is not None:
            self._next[path] = entry

    def commit(self) -> None:
        # Files that vanished simply fall out: _next only holds what we saw.
        self._all[self.namespace] = self._next
        _save_all(self._all)


def signature(path: str):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [int(st.st_size), int(st.st_mtime)]
