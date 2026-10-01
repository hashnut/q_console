import datetime as dt
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from core import config, plan_usage, render, snapshot


class ClaudeSessionRecoveryTests(unittest.TestCase):
    def test_zero_session_and_legacy_alias_have_their_own_reset(self):
        for data in (
            {"limits": [{"kind": "session", "percent": 0, "resets_at": "2026-10-01T16:20:00Z"}]},
            {"limits": [{"kind": "five_hour", "percent": 0, "resets_at": "2026-10-01T16:20:00Z"}]},
            {"five_hour": {"utilization": 0, "resets_at": "2026-10-01T16:20:00Z"}},
        ):
            with self.subTest(data=data):
                session = plan_usage.extract_claude(data)["session"]
                self.assertEqual(session["used"], 0)
                self.assertEqual(session["resets_at"], plan_usage._iso_epoch("2026-10-01T16:20:00Z"))

    def test_old_weekly_only_cache_keeps_unknown_session_visible(self):
        now = 1790850000
        previous = {"providers": [{"id": "claude-code", "plan": "max", "limits": [{
            "key": "week", "used": 29, "measured_at": now - 60, "resets_at": now + 86400}]}]}
        claude = {"status": "unavailable", "note": "HTTP 429", "usage_retry": {"after": now + 300},
                  **plan_usage.extract_claude({}, "max")}
        with patch("core.snapshot._read_current", return_value=(claude, {})), \
             patch("core.snapshot.now_ms", return_value=now * 1000):
            snap = snapshot.build(config.DEFAULTS, previous=previous)
        week, session = snap["providers"][0]["limits"]
        self.assertEqual(week["used"], 29)
        self.assertTrue(week["stale"])
        self.assertIsNone(session["used"])
        html = render.render_overlay(snap, {"overlay_items": ["claude-code"]})
        self.assertIn(">Claude 주간</span>", html)
        self.assertIn(">5h</span>", html)
        self.assertIn("현재 세션 · HTTP 429", html)
        self.assertIn("(조회 대기)", html)

    def test_api_key_and_enterprise_spend_do_not_show_subscription_windows(self):
        for claude in (
            {"mode": "api_key", "all_models": {"used": 2, "unit": "usd", "amount": 2, "budget": 100}},
            {"plan": "enterprise", **plan_usage.extract_claude({}, "enterprise")},
        ):
            with self.subTest(claude=claude), \
                 patch("core.snapshot._read_current", return_value=(claude, {})):
                snap = snapshot.build(config.DEFAULTS)
            self.assertEqual(len(snap["providers"][0]["limits"]), 1)
            html = render.render_overlay(snap, {"overlay_items": ["claude-code"]})
            self.assertNotIn(">5h</span>", html)
            self.assertNotIn("Claude 주간", html)


class ClaudeRetryTests(unittest.TestCase):
    def setUp(self):
        self.now = 1790850000
        self.credentials = {"claudeAiOauth": {"accessToken": "test-only-token", "subscriptionType": "max"}}
        self.usage = {"five_hour": {"utilization": 2, "resets_at": "2026-10-01T16:20:00Z"}}

    def collect(self, get, retry=None, now=None, credentials=None):
        with patch("core.plan_usage._load_json", return_value=credentials or self.credentials), \
             patch("core.plan_usage._claude_cli_version", return_value="2.1.286"), \
             patch("core.plan_usage._get_json", get):
            return plan_usage.collect_claude({}, retry=retry, now=self.now if now is None else now)

    def test_workers_wait_then_recover_and_clear_backoff(self):
        with patch("core.plan_usage._get_json", side_effect=plan_usage.UsageHTTPError(429)) as get:
            failed = self.collect(get)
            retry = failed["usage_retry"]
            self.assertEqual(retry["after"], self.now + 300)
            waiting = self.collect(get, retry=retry, now=self.now + 60)
            self.assertEqual(waiting["usage_retry"], retry)
            get.assert_called_once()
            failed_again = self.collect(get, retry=retry, now=self.now + 300)
            self.assertEqual(failed_again["usage_retry"]["after"], self.now + 300 + 600)
        with patch("core.plan_usage._get_json", return_value=self.usage) as get:
            good = self.collect(get, retry=retry, now=self.now + 300)
            self.assertEqual(good["session"]["used"], 2)
            self.assertNotIn("usage_retry", good)
        self.assertNotIn("test-only-token", json.dumps(failed))

    def test_server_retry_after_and_changed_login(self):
        with patch("core.plan_usage._get_json", side_effect=plan_usage.UsageHTTPError(429, 3600)) as get:
            retry = self.collect(get)["usage_retry"]
            self.assertEqual(retry["after"], self.now + 3600)
        changed = {"claudeAiOauth": {"accessToken": "changed-test-token", "subscriptionType": "max"}}
        with patch("core.plan_usage._get_json", return_value=self.usage) as get:
            good = self.collect(get, retry=retry, now=self.now + 60, credentials=changed)
            get.assert_called_once()
            self.assertEqual(good["status"], "ok")

    def test_snapshot_persists_retry_and_next_worker_uses_it(self):
        with patch("core.plan_usage._get_json", side_effect=plan_usage.UsageHTTPError(429)) as get:
            failed = self.collect(get)
        with patch("core.snapshot._read_current", return_value=(failed, {})), \
             patch("core.snapshot.now_ms", return_value=self.now * 1000):
            previous = snapshot.build(config.DEFAULTS)
        self.assertEqual(previous["providers"][0]["usage_retry"], failed["usage_retry"])
        with patch("core.snapshot._read_claude", return_value=failed) as read_claude, \
             patch("core.snapshot._read_codex", return_value={}):
            snapshot._read_current(config.DEFAULTS, self.now + 60, previous)
        read_claude.assert_called_once_with(config.DEFAULTS, self.now + 60, failed["usage_retry"])

    def test_http_error_reads_retry_header_without_exposing_body(self):
        error = urllib.error.HTTPError("https://example.test", 429, "rate limited",
                                       {"Retry-After": "600"}, io.BytesIO(b"private response"))
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(plan_usage.UsageHTTPError) as raised:
                plan_usage._get_json("https://example.test", {})
        self.assertEqual(raised.exception.retry_after, 600)
        self.assertEqual(str(raised.exception), "HTTP 429")
        with patch("core.plan_usage.time.time", return_value=self.now):
            date = dt.datetime.fromtimestamp(self.now + 900, dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
            self.assertEqual(plan_usage._retry_after(date), 900)
        self.assertIsNone(plan_usage._retry_after("invalid"))


if __name__ == "__main__":
    unittest.main()
