"""Generate overlay QA inputs without live requests or real credentials.

Usage: python tools/make_overlay_fixtures.py release/session-qa
"""
import datetime as dt
import itertools
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import config, plan_usage, render, snapshot


def main(directory):
    home = Path(directory)
    home.mkdir(parents=True, exist_ok=True)
    now = int(dt.datetime(2026, 10, 1, 21, tzinfo=dt.timezone(dt.timedelta(hours=9))).timestamp())
    claude = {"status": "ok", "note": "QA fixture", "reset_credits": {
        "available_count": 1, "applicable_available_count": 0},
        **plan_usage.extract_claude({"limits": [
            {"kind": "weekly_all", "percent": 29, "resets_at": "2026-10-02T12:00:00Z"},
            {"kind": "session", "percent": 2, "resets_at": "2026-10-01T16:20:00Z"},
            {"kind": "weekly_scoped", "percent": 0, "resets_at": "2026-10-02T12:00:00Z",
             "scope": {"model": {"display_name": "Fable"}}},
        ]}, "max")}
    codex = {"status": "ok", "weekly": {"used": 7, "resets_at": now + 3 * 86400},
             "reset_credits": {"available_count": 2, "applicable_available_count": 0}}
    with patch("core.snapshot._read_current", return_value=(claude, codex)), \
         patch("core.snapshot.now_ms", return_value=now * 1000):
        snap = snapshot.build(config.DEFAULTS)
        (home / "example.html").write_text(render.render_overlay(snap, {
            "overlay_items": ["claude-code", "codex", "clock"]}), encoding="utf-8")
        claude["all_models"]["used"] = claude["session"]["used"] = 100
        snap = snapshot.build(config.DEFAULTS)
    for count in range(1, 5):
        for items in itertools.combinations(config.DEFAULTS["overlay_items"], count):
            (home / ("_".join(items) + ".html")).write_text(
                render.render_overlay(snap, {"overlay_items": list(items)}), encoding="utf-8")
    print("Wrote example and all 15 item selections to", home)


if __name__ == "__main__":
    main(sys.argv[1])
