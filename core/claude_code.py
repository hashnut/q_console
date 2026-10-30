"""Claude Code local usage reader.

Source: ~/.claude/projects/<slug>/<session-uuid>.jsonl - one JSON object per
line. Assistant turns carry `message.usage`, which is the same shape the API
returns:

    {"input_tokens", "output_tokens", "cache_read_input_tokens",
     "cache_creation_input_tokens", "cache_creation": {"ephemeral_5m_...",
     "ephemeral_1h_..."}}

Two traps this file handles:

1. DUPLICATE LINES. The same (message.id, requestId) pair is written more than
   once - retries, and forked/resumed sessions that copy prior turns into a new
   file. Counting those twice inflates everything, so dedup is GLOBAL across
   files, not per file.
2. NO LOCAL RATE-LIMIT FEED. Claude Code does not persist "you are at X% of the
   5-hour window" anywhere on disk (checked: transcripts, ~/.claude.json). So we
   report what is actually knowable - tokens and a list-price cost equivalent -
   and gauge it against a budget the user owns. We never invent a plan number.
3. FABLE BILLING BOUNDARY. Fable is available through usage credits rather than
   the subscription usage window. Its local cost equivalent is therefore kept
   separate from the configurable 5-hour and seven-day plan proxies.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os

from . import config, store
from .util import parse_iso

# Public list price, USD per million tokens: (input, output, cache-read multiplier).
# Source: platform.claude.com/docs/en/about-claude/pricing (checked 2026-10-30).
# 5-minute cache writes bill at 1.25x input and 1-hour writes at 2x on every
# model; cache reads are 0.1x except where the third value says otherwise.
# Claude Haiku 5.5 is priced by prompt length - see HAIKU_5_5_TIERS.
PRICING = {
    "claude-fable-5-5": (10.0, 50.0, 0.10),  # not on the price page; Fable tier assumed
    "claude-fable-5-1": (10.0, 50.0, 0.025),
    "claude-mythos-5-1": (10.0, 50.0, 0.025),
    "claude-fable-5": (10.0, 50.0, 0.10),
    "claude-mythos-5": (10.0, 50.0, 0.10),
    "claude-opus-5-5": (4.0, 20.0, 0.05),
    "claude-opus-5": (5.0, 25.0, 0.10),
    "claude-opus-4-8": (5.0, 25.0, 0.10),
    "claude-opus-4-7": (5.0, 25.0, 0.10),
    "claude-opus-4-6": (5.0, 25.0, 0.10),
    "claude-sonnet-5-5": (2.0, 10.0, 0.05),
    "claude-sonnet-5": (2.0, 10.0, 0.10),
    "claude-sonnet-4-6": (3.0, 15.0, 0.10),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}
# (prompt-token ceiling, rates): a prompt counts input, cache reads and writes.
HAIKU_5_5_TIERS = ((100_000, (0.10, 0.50, 0.10)), (None, (0.50, 2.50, 0.10)))
FALLBACK_PRICE = (5.0, 25.0, 0.10)  # unknown model -> price it like an Opus tier

PRETTY = {
    "claude-fable-5-5": "Fable 5.5",
    "claude-fable-5-1": "Fable 5.1",
    "claude-mythos-5-1": "Mythos 5.1",
    "claude-fable-5": "Fable 5",
    "claude-mythos-5": "Mythos 5",
    "claude-opus-5-5": "Opus 5.5",
    "claude-opus-5": "Opus 5",
    "claude-opus-4-8": "Opus 4.8",
    "claude-opus-4-7": "Opus 4.7",
    "claude-opus-4-6": "Opus 4.6",
    "claude-sonnet-5-5": "Sonnet 5.5",
    "claude-sonnet-5": "Sonnet 5",
    "claude-sonnet-4-6": "Sonnet 4.6",
    "claude-haiku-5-5": "Haiku 5.5",
    "claude-haiku-4-5": "Haiku 4.5",
}


def is_fable(model: str) -> bool:
    """Fable is usage-credit work, not part of the Claude plan budget proxy."""
    return (model or "").startswith("claude-fable-")


def _rates_for(model: str, prompt_tokens: int = 0) -> tuple[float, float, float]:
    if model == "claude-haiku-5-5":
        for ceiling, rates in HAIKU_5_5_TIERS:
            if ceiling is None or prompt_tokens <= ceiling:
                return rates
    if model in PRICING:
        return PRICING[model]
    low = (model or "").lower()
    if "fable" in low or "mythos" in low:
        return (10.0, 50.0, 0.10)
    if "sonnet" in low:
        return (3.0, 15.0, 0.10)
    if "haiku" in low:
        return (1.0, 5.0, 0.10)
    return FALLBACK_PRICE


def price_of(model: str, inp, out, cache_read, cw5, cw1) -> float:
    p_in, p_out, read_mult = _rates_for(model, inp + cache_read + cw5 + cw1)
    return (
        inp * p_in
        + out * p_out
        + cache_read * p_in * read_mult
        + cw5 * p_in * 1.25
        + cw1 * p_in * 2.00
    ) / 1_000_000.0


def pretty_model(model: str) -> str:
    return PRETTY.get(model, model or "unknown")


def _key(message: dict, entry: dict) -> str:
    raw = "%s|%s" % (message.get("id") or "", entry.get("requestId") or "")
    if raw == "|":
        return ""
    return hashlib.blake2s(raw.encode("utf-8"), digest_size=8).hexdigest()


def _parse_file(path: str) -> list:
    """-> [[epoch_sec, model, in, out, cache_read, cw5, cw1, dedup_key], ...]"""
    rows = []
    try:
        handle = open(path, encoding="utf-8", errors="ignore")
    except OSError:
        return rows
    with handle:
        for line in handle:
            if '"usage"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("type") != "assistant":
                continue
            message = entry.get("message") or {}
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            model = message.get("model") or ""
            if not model or model == "<synthetic>":
                continue  # synthetic turns are local errors, not billed work
            when = parse_iso(entry.get("timestamp") or "")
            if when is None:
                continue
            creation = usage.get("cache_creation") or {}
            cw5 = creation.get("ephemeral_5m_input_tokens") or 0
            cw1 = creation.get("ephemeral_1h_input_tokens") or 0
            if not (cw5 or cw1):
                cw5 = usage.get("cache_creation_input_tokens") or 0
            rows.append([
                int(when.timestamp()),
                model,
                int(usage.get("input_tokens") or 0),
                int(usage.get("output_tokens") or 0),
                int(usage.get("cache_read_input_tokens") or 0),
                int(cw5),
                int(cw1),
                _key(message, entry),
            ])
    return rows


def collect(cfg: dict) -> dict:
    """Read every session file (incrementally) and return deduped records.

    Returns {"status", "note", "records": [dict...]} where each record is
    {ts, model, input, output, cache_read, cache_write, tokens, cost}.
    """
    root = config.expand(cfg.get("claude_projects_dir") or "~/.claude/projects")
    if not os.path.isdir(root):
        return {
            "status": "unavailable",
            "note": "세션 폴더 없음: %s" % root,
            "records": [],
            "root": root,
        }

    scanner = store.Scanner("claude")
    files = glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
    raw = []
    for path in files:
        sig = store.signature(path)
        if sig is None:
            continue
        cached = scanner.get(path, sig)
        if cached is None:
            cached = _parse_file(path)
            scanner.put(path, sig, cached)
        else:
            scanner.keep(path)
        raw.extend(cached)
    scanner.commit()

    seen = set()
    records = []
    for ts, model, inp, out, cread, cw5, cw1, key in raw:
        if key:
            if key in seen:
                continue
            seen.add(key)
        records.append({
            "ts": ts,
            "model": model,
            "input": inp,
            "output": out,
            "cache_read": cread,
            "cache_write": cw5 + cw1,
            "tokens": inp + out + cread + cw5 + cw1,
            "cost": price_of(model, inp, out, cread, cw5, cw1),
        })
    records.sort(key=lambda r: r["ts"])

    if not records:
        return {
            "status": "unavailable",
            "note": "세션은 있는데 usage 기록이 없음",
            "records": [],
            "root": root,
        }
    return {
        "status": "ok",
        "note": "%d개 세션 파일 · %d개 응답" % (len(files), len(records)),
        "records": records,
        "root": root,
    }
