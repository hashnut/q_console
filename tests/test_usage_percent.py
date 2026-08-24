import datetime as dt
import os
import unittest
from unittest.mock import patch

from core import config, plan_usage, render, snapshot
from core.__main__ import detached_child_environment


class UsageExtractionTests(unittest.TestCase):
    def test_claude_all_models_and_fable_are_separate_percentages(self):
        data = {
            "limits": [
                {"kind": "weekly_all", "percent": 3,
                 "resets_at": "2026-08-28T12:00:00+00:00"},
                {"kind": "weekly_scoped", "percent": 0,
                 "resets_at": "2026-08-28T12:00:00+00:00",
                 "scope": {"model": {"display_name": "Fable"}}},
            ]
        }
        result = plan_usage.extract_claude(data)
        self.assertEqual(result["all_models"]["used"], 3.0)
        self.assertEqual(result["fable"]["used"], 0.0)

    def test_codex_uses_current_weekly_account_window(self):
        data = {
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 0,
                    "limit_window_seconds": 604800,
                    "reset_at": 1788138151,
                },
                "secondary_window": None,
            },
        }
        result = plan_usage.extract_codex(data)
        self.assertEqual(result["weekly"]["used"], 0.0)
        self.assertEqual(result["weekly"]["resets_at"], 1788138151)
        self.assertEqual(result["plan"], "pro")

    @patch("core.plan_usage._get_json", side_effect=RuntimeError("offline"))
    @patch("core.plan_usage._load_json")
    def test_failed_live_request_returns_unknown_not_stale(self, load, get_json):
        load.return_value = {"tokens": {
            "access_token": "redacted", "account_id": "redacted"}}
        result = plan_usage.collect_codex({"codex_auth_file": "unused"})
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["weekly"]["used"])


class UsagePresentationTests(unittest.TestCase):
    @patch.dict(os.environ, {
        "_PYI_ARCHIVE_FILE": "q_console.exe",
        "_PYI_PARENT_PROCESS_LEVEL": "1",
        "Q_CONSOLE_KEEP": "yes",
    })
    def test_persistent_tray_does_not_inherit_pyinstaller_parent_markers(self):
        env = detached_child_environment()
        self.assertNotIn("_PYI_ARCHIVE_FILE", env)
        self.assertNotIn("_PYI_PARENT_PROCESS_LEVEL", env)
        self.assertEqual(env["Q_CONSOLE_KEEP"], "yes")

    def test_first_launch_defaults_to_overlay(self):
        self.assertTrue(config.DEFAULTS["overlay_mode"])

    def setUp(self):
        self.now = int(dt.datetime(2026, 8, 24, 10, 30).timestamp())
        self.claude = {
            "status": "ok",
            "note": "fixture",
            "all_models": {"used": 3.0, "resets_at": self.now + 4 * 86400},
            "fable": {"used": 0.0, "resets_at": self.now + 4 * 86400},
        }
        self.codex = {
            "status": "ok",
            "note": "fixture",
            "weekly": {"used": 0.0, "resets_at": self.now + 7 * 86400},
            "plan": "pro",
        }

    @patch("core.snapshot.now_ms")
    @patch("core.snapshot._read_current")
    def test_snapshot_contains_only_three_measured_percentages(self, read, now_ms):
        read.return_value = self.claude, self.codex
        now_ms.return_value = self.now * 1000
        snap = snapshot.build({"warning_used_percent": 80})

        self.assertEqual([p["id"] for p in snap["providers"]],
                         ["claude-code", "fable", "codex"])
        self.assertEqual([p["limits"][0]["used"] for p in snap["providers"]],
                         [3.0, 0.0, 0.0])
        self.assertTrue(all(p["limits"][0]["measured"] for p in snap["providers"]))
        self.assertNotIn("$", snap["detail_text"])

    @patch("core.snapshot.now_ms")
    @patch("core.snapshot._read_current")
    def test_overlay_shows_three_percentages_and_chatgpt_mark(self, read, now_ms):
        read.return_value = self.claude, self.codex
        now_ms.return_value = self.now * 1000
        snap = snapshot.build({"warning_used_percent": 80})
        html = render.render_overlay(snap)

        self.assertIn(">Claude</span>", html)
        self.assertIn(">Fable</span>", html)
        self.assertIn(">Codex</span>", html)
        self.assertIn(">3.0%</span>", html)
        self.assertGreaterEqual(html.count(">0.0%</span>"), 2)
        self.assertGreaterEqual(html.count("(4d 0h)"), 2)
        self.assertIn("(7d 0h)", html)
        self.assertIn("aria-label='ChatGPT'", html)
        self.assertIn("M304.246 294.611", html)
        self.assertIn("postMessage('q_console:drag-start')", html)
        self.assertIn("postMessage('q_console:drag-move')", html)
        self.assertIn("cursor:move", html)
        self.assertNotIn("$", html)
        self.assertNotIn("5h", html)


if __name__ == "__main__":
    unittest.main()
