import datetime as dt
import unittest
from unittest.mock import patch

from core import config, plan_usage, render, snapshot


def grant(**fields):
    value = {
        "id": "launch", "resets_total": 1, "resets_left": 1,
        "ends_at": "2026-10-22T16:00:00+00:00",
        "clears": ["five_hour", "seven_day", "seven_day_overage_included"],
        "paused": False, "usable_now": True,
    }
    value.update(fields)
    return value


class ClaudeResetCreditTests(unittest.TestCase):
    def setUp(self):
        self.now = int(dt.datetime(2026, 10, 1, 16).timestamp())
        self.usage = {
            "limits": [
                {"kind": "weekly_all", "percent": 19, "resets_at": "2026-10-02T11:59:59+00:00"},
                {"kind": "session", "percent": 52, "resets_at": "2026-10-01T11:19:59+00:00"},
            ],
            "cedar_ember": {"eligible": True, "ineligible_reason": None, "grants": [grant()]},
        }
        self.codex = {"status": "ok", "weekly": {"used": 2, "resets_at": self.now + 86400},
                      "plan": "pro", "reset_credits": {
                          "available_count": 0, "applicable_available_count": 0}}

    def build(self, data=None):
        claude = {"status": "ok", **plan_usage.extract_claude(data or self.usage, "max"),
                  "reset_credits": plan_usage.extract_claude_reset_credits(data or self.usage)}
        with patch("core.snapshot._read_current", return_value=(claude, self.codex)), \
             patch("core.snapshot.now_ms", return_value=self.now * 1000):
            return snapshot.build(config.DEFAULTS)

    def test_grants_become_owned_and_usable_counts_with_typed_expiries(self):
        five_hour = grant(id="five", ends_at="2026-10-05T00:00:00+00:00",
                          clears=["five_hour"], resets_left=2, usable_now=False)
        paused = grant(id="paused", paused=True, ends_at=None)
        used_up = grant(id="spent", resets_left=0)
        self.usage["cedar_ember"]["grants"] = [grant(), five_hour, paused, used_up, "bad"]
        credits = plan_usage.extract_claude_reset_credits(self.usage)
        self.assertEqual(credits["available_count"], 4)
        self.assertEqual(credits["applicable_available_count"], 1)
        five_at = plan_usage._iso_epoch("2026-10-05T00:00:00+00:00")
        full_at = plan_usage._iso_epoch("2026-10-22T16:00:00+00:00")
        self.assertEqual(credits["expires_at"], [five_at, five_at, full_at])
        self.assertEqual(credits["expiry_labels"], ["5시간 리셋", "5시간 리셋", "전체 리셋"])

    def test_unreadable_grants_are_unknown_and_unoffered_accounts_hide_them(self):
        unknown = {"available_count": None, "applicable_available_count": None}
        self.assertEqual(plan_usage.extract_claude_reset_credits({}), unknown)
        for reason in ("surface", "cli_version"):
            self.assertEqual(plan_usage.extract_claude_reset_credits(
                {"cedar_ember": {"eligible": False, "ineligible_reason": reason}}), unknown)
        self.assertIsNone(plan_usage.extract_claude_reset_credits(
            {"cedar_ember": {"eligible": False, "ineligible_reason": "plan"}}))
        empty = plan_usage.extract_claude_reset_credits(
            {"cedar_ember": {"eligible": True, "grants": []}})
        self.assertEqual((empty["available_count"], empty["applicable_available_count"]), (0, 0))

    def test_every_view_shows_claude_reset_credits_with_expiry_tooltip(self):
        snap = self.build()
        claude = snap["providers"][0]
        expiry = dt.datetime.fromtimestamp(
            plan_usage._iso_epoch("2026-10-22T16:00:00+00:00")).strftime("%m-%d %H:%M")
        self.assertTrue(claude["reset_credit_tip"].startswith("리셋권 만료 · 전체 리셋 %s (" % expiry))
        self.assertIn("리셋권 1개 · 사용 가능 1개", snap["summary_lines"][0])
        for theme in ("surfacer", "phosphor", "mini"):
            self.assertIn(claude["reset_credit_tip"], render.render(snap, theme))
        overlay = render.render_overlay(snap, {"overlay_items": ["claude-code"]})
        self.assertIn("리셋권 1개 · 사용 가능 1개", overlay)
        self.assertIn(claude["reset_credit_tip"], overlay)
        # The credits close the Claude segment, after its five-hour window.
        self.assertLess(overlay.index("52%"), overlay.index("리셋권 1개"))
        self.assertNotIn("리셋권", render.render_overlay(snap, {"overlay_items": ["codex"]})
                         .replace("리셋권 0개", ""))

    def test_usage_read_identifies_as_claude_code_and_carries_grants(self):
        seen = {}

        def fake_get(url, headers):
            seen.update(url=url, agent=headers["User-Agent"])
            return self.usage

        with patch("core.plan_usage._load_json", return_value={"claudeAiOauth": {
                     "accessToken": "token", "subscriptionType": "max"}}), \
             patch("core.plan_usage._claude_cli_version", return_value="2.1.286"), \
             patch("core.plan_usage._get_json", side_effect=fake_get):
            result = plan_usage.collect_claude({})
        self.assertEqual(seen["url"], plan_usage.CLAUDE_USAGE_URL + "?cedar_ember=1")
        self.assertEqual(seen["agent"], "claude-cli/2.1.286 (external, cli)")
        self.assertEqual(result["all_models"]["used"], 19)
        self.assertEqual(result["reset_credits"]["available_count"], 1)


if __name__ == "__main__":
    unittest.main()
