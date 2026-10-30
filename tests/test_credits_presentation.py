import unittest
from unittest.mock import patch

from core import config, plan_usage, render, snapshot


class CodexAdditionalCreditTests(unittest.TestCase):
    def setUp(self):
        self.now = 1790856000
        self.usage = {
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {
                    "limit_window_seconds": 604800,
                    "used_percent": 98,
                    "reset_at": self.now + 450000,
                }
            },
            "rate_limit_reset_credits": {
                "available_count": 1,
                "applicable_available_count": 0,
            },
            "credits": {
                "has_credits": True,
                "unlimited": False,
                "overage_limit_reached": False,
                "balance": "61039.3291250000",
            },
        }
        self.details = {"credits": []}
        self.claude = {
            "status": "ok",
            "plan": "max",
            "all_models": {"used": 10},
            "reset_credits": {"available_count": 0, "applicable_available_count": 0},
        }

    def collect(self):
        with patch("core.plan_usage._load_json", return_value={
                "tokens": {"access_token": "test", "account_id": "test"}}), \
             patch("core.plan_usage._get_json", side_effect=[self.usage, self.details]):
            result = plan_usage.collect_codex(config.DEFAULTS)
        return result

    def build(self, codex):
        with patch("core.snapshot._read_current", return_value=(self.claude, codex)), \
             patch("core.snapshot.now_ms", return_value=self.now * 1000):
            return snapshot.build(config.DEFAULTS)

    def test_codex_additional_credits_parsed_and_displayed(self):
        codex = self.collect()
        self.assertTrue(codex["account_credits"]["has_credits"])
        self.assertAlmostEqual(codex["account_credits"]["balance"], 61039.329125)

        snap = self.build(codex)
        provider = snap["providers"][-1]
        self.assertEqual(provider["id"], "codex")
        self.assertIn("크레딧 61,039 남음", provider["credit_text"])

        # Check detail text
        self.assertIn("크레딧 61,039 남음", snap["detail_text"])

        # Check summary lines
        self.assertIn("크레딧 61,039 남음", snap["summary_lines"][-1])

        # Check card HTML
        for theme in ("surfacer", "phosphor", "mini"):
            html = render.render(snap, theme)
            self.assertIn("크레딧 <b>61,039</b> 남음", html)

        # Check overlay
        overlay = render.render_overlay(snap, {"overlay_items": ["codex", "codex-credit"]})
        self.assertIn("61.0k", overlay)
        self.assertIn("추가 크레딧", overlay)


class ClaudeApiCreditTests(unittest.TestCase):
    def setUp(self):
        self.now = 1790856000
        self.claude = {
            "status": "ok",
            "plan": "max",
            "all_models": {"used": 20},
            "reset_credits": None,
        }
        self.codex = {
            "status": "ok",
            "plan": "pro",
            "weekly": {"used": 10},
            "reset_credits": {"available_count": 0, "applicable_available_count": 0},
            "account_credits": {"has_credits": False, "balance": None},
        }

    @patch("core.api_key_usage.os.path.exists", return_value=False)
    def test_claude_card_shows_api_credits(self, exists_mock):
        cfg = dict(config.DEFAULTS, claude_api_credit_usd=200.0, claude_credit_expiry="2026-11-04")
        with patch("core.snapshot._read_current", return_value=(self.claude, self.codex)), \
             patch("core.snapshot.now_ms", return_value=self.now * 1000):
            snap = snapshot.build(cfg)

        claude_provider = snap["providers"][0]
        self.assertTrue(claude_provider["api_credits"]["has_credits"])
        self.assertEqual(claude_provider["api_credits"]["remaining"], 200.0)

        # In board/card view, API credits are displayed
        html = render.render(snap, "surfacer")
        self.assertIn("API 크레딧 <b>$200</b> 남음", html)
        self.assertIn("만료: 2026-11-04", html)


if __name__ == "__main__":
    unittest.main()
