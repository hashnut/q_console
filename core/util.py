"""Shared helpers: time, formatting, tone bands.

Everything here is stdlib-only on purpose - the whole point of this rewrite is
that it runs off the local Python install with no packages to fetch.
"""

from __future__ import annotations

import datetime as _dt

KST = None  # local timezone is whatever the box says; we never hardcode one.


def now_ms() -> int:
    return int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)


def parse_iso(ts: str):
    """'2026-08-21T07:12:12.110Z' -> aware datetime in LOCAL time.

    Both Claude Code and Codex write RFC3339 with a Z suffix. Anything we cannot
    parse returns None and the caller drops the record rather than guessing.
    """
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        d = _dt.datetime.fromisoformat(ts)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone()


def fmt_dur(seconds: float) -> str:
    """Countdown text. Always coarse: nobody schedules work off seconds."""
    if seconds is None:
        return "--"
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def fmt_age(ms: float) -> str:
    if not ms:
        return ""
    sec = max(0.0, (now_ms() - ms) / 1000.0)
    if sec < 60:
        return f"{int(sec)}s"
    if sec < 3600:
        return f"{int(sec // 60)}m"
    if sec < 86400:
        return f"{sec / 3600:.1f}h".replace(".0h", "h")
    return f"{sec / 86400:.1f}d".replace(".0d", "d")


def fmt_tokens(n: float) -> str:
    if n is None:
        return "--"
    n = float(n)
    if n >= 1e9:
        return f"{n / 1e9:.1f}B"
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}K"
    return f"{int(n)}"


def fmt_usd(v: float) -> str:
    if v is None:
        return "--"
    if v >= 100:
        return f"${v:,.0f}"
    if v >= 10:
        return f"${v:.1f}"
    return f"${v:.2f}"


def tone_for(used_percent, warn: int = 80):
    """Dashboard tone band. None -> unknown, never a fake 0."""
    if used_percent is None:
        return "unknown"
    u = float(used_percent)
    if u >= 95:
        return "blocked"
    if u >= warn:
        return "warning"
    if u >= warn * 0.6:
        return "caution"
    return "relaxed"


def clamp_pct(v):
    if v is None:
        return None
    return max(0.0, min(100.0, float(v)))
