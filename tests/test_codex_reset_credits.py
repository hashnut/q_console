import datetime as dt
import unittest
from unittest.mock import patch

from core import config, plan_usage, render, snapshot


class CodexResetCreditTests(unittest.TestCase):
    def setUp(self):
        self.now = 1790856000
        self.usage = {
            "plan_type": "pro",
            "rate_limit": {"primary_window": {
                "limit_window_seconds": 604800, "used_percent": 7,
                "reset_at": self.now + 604800}},
            "rate_limit_reset_credits": {
                "available_count": 2, "applicable_available_count": 0},
        }
        self.details = {"credits": [
            {"status": "available", "expires_at": dt.datetime.fromtimestamp(
                self.now + days * 86400, dt.timezone.utc).isoformat()}
            for days in (22, 29)
        ]}
        self.claude = {"status": "ok", "plan": "max", "all_models": {"used": 10},
                       "reset_credits": {"available_count": 2,
                                         "applicable_available_count": 1}}

    def collect(self, detail=None):
        responses = [self.usage, self.details if detail is None else detail]
        with patch("core.plan_usage._load_json", return_value={
                "tokens": {"access_token": "test", "account_id": "test"}}), \
             patch("core.plan_usage._get_json", side_effect=responses) as get:
            result = plan_usage.collect_codex(config.DEFAULTS)
        return result, get

    def build(self, codex):
        with patch("core.snapshot._read_current", return_value=(self.claude, codex)), \
             patch("core.snapshot.now_ms", return_value=self.now * 1000):
            return snapshot.build(config.DEFAULTS)

    def test_web_available_count_matches_every_view_even_when_not_applicable(self):
        codex, get = self.collect()
        snap = self.build(codex)
        provider = snap["providers"][-1]
        self.assertEqual(snapshot.reset_credit_text(provider["reset_credits"]), "리셋권 2개")
        self.assertEqual(provider["reset_credits"]["applicable_available_count"], 0)
        self.assertEqual(provider["reset_credits"]["available_count"], 2)
        tip = provider["reset_credit_tip"]
        self.assertIn("22d 0h 남음", tip)
        self.assertIn("29d 0h 남음", tip)
        for theme in ("surfacer", "phosphor", "mini"):
            html = render.render(snap, theme)
            self.assertIn("리셋권 2개", html)
            self.assertIn(tip, html)
        overlay = render.render_overlay(snap, {"overlay_items": ["codex"]})
        self.assertIn("리셋권 2개", overlay)
        self.assertIn(tip, overlay)
        self.assertIn("리셋권 2개", snap["summary_lines"][-1])
        self.assertIn("리셋권 2개", snap["detail_text"])
        self.assertEqual(snapshot.reset_credit_text(snap["providers"][0]["reset_credits"]),
                         "리셋권 1개")
        self.assertNotIn("display_count", codex["reset_credits"])
        self.assertEqual([call.args[0] for call in get.call_args_list],
                         [plan_usage.CODEX_USAGE_URL, plan_usage.CODEX_RESET_CREDITS_URL])

    def test_zero_or_unknown_available_count_never_uses_applicable_count(self):
        for available, expected in ((0, "리셋권 0개"), (None, "리셋권 --")):
            with self.subTest(available=available):
                self.usage["rate_limit_reset_credits"].update(
                    available_count=available, applicable_available_count=2)
                codex, get = self.collect()
                provider = self.build(codex)["providers"][-1]
                self.assertEqual(snapshot.reset_credit_text(provider["reset_credits"]), expected)
                self.assertEqual(provider["reset_credit_tip"], "")
                self.assertEqual(get.call_count, 1)

    def test_expiry_failure_preserves_available_count_and_weekly_usage(self):
        codex, _ = self.collect(RuntimeError("detail unavailable"))
        provider = self.build(codex)["providers"][-1]
        self.assertEqual(codex["status"], "ok")
        self.assertEqual(codex["weekly"]["used"], 7)
        self.assertEqual(snapshot.reset_credit_text(provider["reset_credits"]), "리셋권 2개")
        self.assertEqual(provider["reset_credit_tip"], "리셋권 만료 시각 미확인")

    def test_usage_failure_shows_unknown_count(self):
        with patch("core.plan_usage._load_json", return_value={
                "tokens": {"access_token": "test", "account_id": "test"}}), \
             patch("core.plan_usage._get_json", side_effect=RuntimeError("offline")):
            codex = plan_usage.collect_codex(config.DEFAULTS)
        provider = self.build(codex)["providers"][-1]
        self.assertEqual(snapshot.reset_credit_text(provider["reset_credits"]), "리셋권 --")
        self.assertEqual(provider["reset_credit_tip"], "")


if __name__ == "__main__":
    unittest.main()
