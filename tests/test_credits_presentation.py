import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import api_key_usage, config, plan_usage, render, snapshot
from core import __main__ as app


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

        # In board/card view, API credits are displayed with 2 decimals
        html = render.render(snap, "surfacer")
        self.assertIn("API 크레딧 <b>$200.00</b> 남음", html)
        self.assertIn("만료: 2026-11-04", html)

    def test_opencodex_incremental_scan_and_exact_cents_presentation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ocx_usage = Path(tmpdir) / "usage.jsonl"
            scan_cache = Path(tmpdir) / "scan-cache.json"
            # Write an anthropic-apikey row followed by >120 KB of non-Anthropic / OAuth rows
            row_api = (
                '{"timestamp": %d, "provider": "anthropic-apikey", "model": "claude-sonnet-5-5", '
                '"requestedModel": "anthropic-apikey/claude-sonnet-5-5", "accountLogLabel": "k4349", '
                '"usage": {"inputTokens": 100000, "outputTokens": 10000, '
                '"cacheReadInputTokens": 80000, "cacheCreationInputTokens": 10000, '
                '"cacheCreation1hInputTokens": 10000}}\n'
                % ((self.now - 300) * 1000)
            )
            # An OAuth subscription row that must NOT be counted as API key spend
            row_oauth = (
                '{"timestamp": %d, "provider": "anthropic", "model": "claude-sonnet-5-5", '
                '"requestedModel": "anthropic/claude-sonnet-5-5", '
                '"usage": {"inputTokens": 500000, "outputTokens": 50000}}\n'
                % ((self.now - 200) * 1000)
            )
            # OpenAI filler rows exceeding 100 KB so a naive tail-seek would lose row_api
            row_openai = (
                '{"timestamp": %d, "provider": "openai", "model": "gpt-5", '
                '"usage": {"inputTokens": 1000, "outputTokens": 500}, "pad": "%s"}\n'
                % ((self.now - 100) * 1000, "x" * 2000)
            )
            ocx_usage.write_text(row_api + row_oauth + (row_openai * 65), encoding="utf-8")
            self.assertGreater(ocx_usage.stat().st_size, 120_000)

            cfg = dict(
                config.DEFAULTS,
                claude_api_credit_usd=200.0,
                claude_api_base_spent_usd=3.0,
                claude_credit_expiry="2026-11-04",
            )
            with patch.object(config, "SCAN_PATH", str(scan_cache)), \
                 patch("core.api_key_usage.os.path.expanduser",
                       side_effect=lambda p: str(ocx_usage) if p.endswith("usage.jsonl") else str(Path(tmpdir) / "none")), \
                 patch("core.snapshot._read_current", return_value=(self.claude, self.codex)), \
                 patch("core.snapshot.now_ms", return_value=self.now * 1000):
                snap = snapshot.build(cfg)

            cred = snap["providers"][0]["api_credits"]
            # row_api cost: uncached_in=10k ($0.03), out=10k ($0.15), cread=80k ($0.024), cw1h=10k ($0.06) = $0.264
            # plus base_spent=$3.00 -> $3.264 spent, $196.736 remaining
            self.assertAlmostEqual(cred["spent"], 3.264, places=4)
            self.assertAlmostEqual(cred["remaining"], 196.736, places=4)

            html = render.render(snap, "surfacer")
            self.assertIn("API 크레딧 <b>$196.74</b> 남음 ($3.26 사용)", html)

            overlay = render.render_overlay(snap, {"overlay_items": ["claude-code", "claude-api"]})
            self.assertIn(">$196.74</span>", overlay)
            self.assertIn("이번 달 $3.26 사용", overlay)

    def test_set_overlay_items_accepts_claude_api_and_codex_credit(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(config, "APP_HOME", directory), \
                 patch.object(config, "CONFIG_PATH", str(Path(directory) / "config.json")), \
                 patch.object(config, "OVERLAY_PATH", str(Path(directory) / "overlay.html")), \
                 patch.object(app, "load_cached_snapshot", return_value={"providers": []}):
                rc = app.main(["--set-overlay-items", "claude-code,claude-api,codex,codex-credit,clock"])
                self.assertEqual(rc, 0)
                self.assertEqual(
                    config.load()["overlay_items"],
                    ["claude-code", "claude-api", "codex", "codex-credit", "clock"],
                )


if __name__ == "__main__":
    unittest.main()
