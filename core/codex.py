"""Codex CLI local usage reader.

Source: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl. Two payloads matter:

  turn_context  -> which model the turn ran on
  token_count   -> {"info": {"total_token_usage": {...cumulative...},
                             "last_token_usage": {...this turn...}},
                    "rate_limits": {"primary": {"used_percent", "window_minutes",
                                                "resets_at"}, "plan_type", ...}}

`rate_limits` is the one place on this machine where a REAL subscription number
lives, so it is what the Codex gauge shows. Everything else (tokens) is counted
locally.

Why last_token_usage and not the cumulative total: the cumulative counter RESETS
whenever the thread compacts. Differencing it across a reset re-adds the whole
session (measured: 51.6B "tokens" over 35 days, ~6x the truth). Summing the
per-turn field, skipping repeats of an unchanged value, gives 8.4B - which is
consistent with the session lengths on disk.
"""

from __future__ import annotations

import glob
import json
import os
import time

from . import config, store
from .util import parse_iso

FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")

# Only files touched inside this window are parsed. Anything older cannot land
# in a 30-day dashboard, and skipping them keeps a cold pass near one second.
MAX_AGE_DAYS = 45


def _parse_file(path: str) -> dict:
    """-> {"buckets": {epoch_hour: [total, fresh_in, cached, out, turns]},
           "model": str, "rl": [ts, rate_limits], "last_ts": int}"""
    buckets = {}
    model = None
    last_rl = None
    last_ts = 0
    previous = None
    try:
        handle = open(path, encoding="utf-8", errors="ignore")
    except OSError:
        return {"buckets": {}, "model": None, "rl": None, "last_ts": 0}
    with handle:
        for line in handle:
            if '"token_count"' not in line and '"turn_context"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            payload = entry.get("payload") or {}
            if entry.get("type") == "turn_context":
                model = payload.get("model") or model
                continue
            if payload.get("type") != "token_count":
                continue
            when = parse_iso(entry.get("timestamp") or "")
            if when is None:
                continue
            stamp = int(when.timestamp())
            last_ts = max(last_ts, stamp)

            limits = payload.get("rate_limits")
            if isinstance(limits, dict):
                if last_rl is None or stamp >= last_rl[0]:
                    last_rl = [stamp, limits]

            usage = (payload.get("info") or {}).get("last_token_usage") or {}
            current = tuple(int(usage.get(f) or 0) for f in FIELDS)
            if current == previous or current == (0, 0, 0, 0):
                continue  # a repeat of the same turn, not new spend
            previous = current
            inp, cached, out, total = current
            slot = str(stamp // 3600)
            bucket = buckets.get(slot)
            if bucket is None:
                bucket = buckets[slot] = [0, 0, 0, 0, 0]
            bucket[0] += total
            bucket[1] += max(0, inp - cached)   # fresh (uncached) input
            bucket[2] += cached
            bucket[3] += out
            bucket[4] += 1
    return {"buckets": buckets, "model": model, "rl": last_rl, "last_ts": last_ts}


def collect(cfg: dict) -> dict:
    """-> {"status", "note", "buckets", "models", "rate_limits", "limit_seen_ms"}

    `buckets` is {epoch_hour_int: [total, fresh_in, cached, out, turns]} merged
    across every session; `models` is {model: total_tokens}.
    """
    root = config.expand(cfg.get("codex_sessions_dir") or "~/.codex/sessions")
    if not os.path.isdir(root):
        return {
            "status": "unavailable",
            "note": "세션 폴더 없음: %s" % root,
            "buckets": {}, "models": {}, "rate_limits": None,
            "limit_seen_ms": None, "root": root, "files": 0,
        }

    scanner = store.Scanner("codex")
    cutoff = time.time() - MAX_AGE_DAYS * 86400
    merged = {}
    models = {}
    best_rl = None
    files = 0
    for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        sig = store.signature(path)
        if sig is None or sig[1] < cutoff:
            continue
        files += 1
        data = scanner.get(path, sig)
        if data is None:
            data = _parse_file(path)
            scanner.put(path, sig, data)
        else:
            scanner.keep(path)
        total_here = 0
        for slot, values in (data.get("buckets") or {}).items():
            slot = int(slot)
            row = merged.get(slot)
            if row is None:
                row = merged[slot] = [0, 0, 0, 0, 0]
            for i in range(5):
                row[i] += values[i]
            total_here += values[0]
        name = data.get("model") or "unknown"
        models[name] = models.get(name, 0) + total_here
        limits = data.get("rl")
        if limits and (best_rl is None or limits[0] > best_rl[0]):
            best_rl = limits
    scanner.commit()

    if not merged:
        return {
            "status": "unavailable",
            "note": "최근 %d일 안에 Codex 세션 없음" % MAX_AGE_DAYS,
            "buckets": {}, "models": {}, "rate_limits": None,
            "limit_seen_ms": None, "root": root, "files": files,
        }
    return {
        "status": "ok",
        "note": "%d개 롤아웃 파일" % files,
        "buckets": merged,
        "models": models,
        "rate_limits": best_rl[1] if best_rl else None,
        "limit_seen_ms": best_rl[0] * 1000 if best_rl else None,
        "root": root,
        "files": files,
    }
