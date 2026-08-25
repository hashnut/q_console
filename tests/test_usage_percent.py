import datetime as dt
import os
import unittest
from unittest.mock import patch

from core import api_key_usage, bootstrap, config, plan_usage, render, snapshot
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


class StaleCarryForwardTests(unittest.TestCase):
    """An expired Claude Code token answers 401. The strip should keep showing
    the last real percentage instead of dropping to "--"."""

    def setUp(self):
        self.now = 1788138151
        self.ok_claude = {
            "status": "ok", "note": "Claude 계정 Usage 실측",
            "all_models": {"used": 16.0, "resets_at": self.now + 4 * 86400},
            "fable": {"used": 19.0, "resets_at": self.now + 4 * 86400},
        }
        self.failed_claude = {
            "status": "unavailable", "note": "Claude 사용률 확인 실패: HTTP 401",
            "all_models": {"used": None, "resets_at": None},
            "fable": {"used": None, "resets_at": None},
        }
        self.codex = {
            "status": "ok", "note": "Codex 계정 Usage 실측",
            "weekly": {"used": 9.0, "resets_at": self.now + 7 * 86400},
            "plan": "pro",
        }

    def _build(self, claude, previous=None, at=None):
        with patch("core.snapshot.now_ms", return_value=(at or self.now) * 1000),              patch("core.snapshot._read_current", return_value=(claude, self.codex)):
            return snapshot.build({"warning_used_percent": 80}, previous=previous)

    def test_failed_read_keeps_the_previous_percentage_marked_stale(self):
        good = self._build(self.ok_claude)
        snap = self._build(self.failed_claude, previous=good,
                           at=self.now + 1800)

        claude = snap["providers"][0]["limits"][0]
        self.assertEqual(claude["used"], 16.0)
        self.assertTrue(claude["stale"])
        self.assertEqual(claude["measured_at"], self.now)
        self.assertEqual(snap["providers"][1]["limits"][0]["used"], 19.0)
        self.assertEqual(snap["providers"][0]["status"], "stale")
        self.assertIn("HTTP 401", snap["providers"][0]["note"])
        self.assertIn("16%*", snap["hover_line"])

        html = render.render_overlay(snap)
        self.assertIn(">16%</span>", html)
        self.assertIn("class='st'", html)
        self.assertNotIn(">--</span>", html)

    def test_recovered_read_replaces_the_carried_value(self):
        stale = self._build(self.failed_claude,
                            previous=self._build(self.ok_claude))
        fresh = dict(self.ok_claude,
                     all_models={"used": 21.0,
                                 "resets_at": self.now + 4 * 86400})
        snap = self._build(fresh, previous=stale, at=self.now + 3600)

        limit = snap["providers"][0]["limits"][0]
        self.assertEqual(limit["used"], 21.0)
        self.assertFalse(limit["stale"])
        self.assertEqual(snap["providers"][0]["status"], "ok")

    def test_carried_value_is_dropped_once_its_window_has_reset(self):
        good = self._build(self.ok_claude)
        snap = self._build(self.failed_claude, previous=good,
                           at=self.now + 4 * 86400 + 60)
        self.assertIsNone(snap["providers"][0]["limits"][0]["used"])
        self.assertFalse(snap["providers"][0]["limits"][0]["stale"])

    def test_carried_value_expires_after_the_max_age(self):
        good = self._build(self.ok_claude)
        # Well inside the weekly window, but older than stale_max_age_sec.
        snap = self._build(self.failed_claude, previous=good,
                           at=self.now + 25 * 3600)
        self.assertIsNone(snap["providers"][0]["limits"][0]["used"])

    def test_a_plan_percentage_is_never_carried_into_budget_mode(self):
        """The two percentages measure different things; swapping one for the
        other would put a plan number under a budget label."""
        good = self._build(self.ok_claude)
        failed_api = {"status": "unavailable", "mode": "api_key",
                      "note": "API 키 모드 · 세션 로그 없음",
                      "all_models": {"used": None, "resets_at": None},
                      "fable": {"used": None, "resets_at": None}}
        snap = self._build(failed_api, previous=good, at=self.now + 1800)
        self.assertIsNone(snap["providers"][0]["limits"][0]["used"])

    def test_first_ever_run_with_no_cache_still_reports_unknown(self):
        snap = self._build(self.failed_claude, previous=None)
        self.assertIsNone(snap["providers"][0]["limits"][0]["used"])
        self.assertIn("--", snap["detail_text"])


class ApiKeyModeTests(unittest.TestCase):
    """Someone on an API key has no plan percentage - the gauge is this
    month's spend against a budget they set."""

    def setUp(self):
        self.now = int(dt.datetime(2026, 8, 25, 10, 0).timestamp())
        self.month_start = int(dt.datetime(2026, 8, 1).timestamp())
        self.cfg = dict(config.DEFAULTS, claude_api_budget_usd=100.0,
                        codex_api_budget_tokens=1_000_000)

    def test_subscription_login_beats_a_stray_api_key(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-api-x"}),              patch("core.api_key_usage._oauth_token", return_value="tok"):
            self.assertEqual(
                api_key_usage.claude_auth_mode(self.cfg)["mode"], "subscription")

    def test_api_key_in_env_is_detected_when_not_signed_in(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-api-x"}),              patch("core.api_key_usage._oauth_token", return_value=None):
            mode = api_key_usage.claude_auth_mode(self.cfg)
        self.assertEqual(mode["mode"], "api_key")
        self.assertEqual(mode["source"], "ANTHROPIC_API_KEY")

    def test_no_login_and_no_key_is_reported_as_neither(self):
        with patch.dict(os.environ, {}, clear=True),              patch("core.api_key_usage._oauth_token", return_value=None),              patch("core.api_key_usage._settings_env", return_value={}):
            self.assertEqual(
                api_key_usage.claude_auth_mode(self.cfg)["mode"], "none")

    def test_usage_mode_setting_can_force_the_budget_gauge(self):
        cfg = dict(self.cfg, usage_mode="api_key")
        with patch("core.api_key_usage._oauth_token", return_value="tok"):
            self.assertEqual(api_key_usage.claude_auth_mode(cfg)["mode"],
                             "api_key")

    def test_month_window_starts_on_the_first_and_ends_next_month(self):
        start, end = api_key_usage.month_window(self.now)
        self.assertEqual(start, self.month_start)
        self.assertEqual(end, int(dt.datetime(2026, 9, 1).timestamp()))

    def test_local_cost_is_gauged_against_the_budget_with_fable_split(self):
        records = [
            # Before this month - must not count toward the month's spend.
            {"ts": self.month_start - 3600, "model": "claude-opus-5",
             "cost": 50.0},
            {"ts": self.month_start + 60, "model": "claude-opus-5",
             "cost": 15.0},
            {"ts": self.now - 60, "model": "claude-fable-5", "cost": 10.0},
        ]
        with patch("core.claude_code.collect",
                   return_value={"status": "ok", "note": "", "records": records}):
            result = api_key_usage.collect_claude_api(
                self.cfg, self.now, {"mode": "api_key", "admin_key": ""})

        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["all_models"]["amount"], 25.0)
        self.assertAlmostEqual(result["all_models"]["used"], 25.0)
        self.assertAlmostEqual(result["fable"]["amount"], 10.0)
        self.assertAlmostEqual(result["fable"]["used"], 10.0)
        self.assertEqual(result["all_models"]["unit"], "usd")
        self.assertFalse(result["all_models"]["billed"])

    def test_admin_cost_report_amounts_are_cents(self):
        page = {"data": [{"results": [{"amount": "123.45", "currency": "USD"},
                                      {"amount": "76.55", "currency": "USD"}]}],
                "has_more": False, "next_page": None}
        with patch("core.api_key_usage._get_json", return_value=page) as get:
            usd = api_key_usage.admin_cost_usd("sk-ant-admin01-x", 0, 1)
        self.assertAlmostEqual(usd, 2.0)  # 200 cents, not $200
        headers = get.call_args[0][1]
        self.assertEqual(headers["x-api-key"], "sk-ant-admin01-x")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")

    def test_admin_failure_falls_back_to_local_logs(self):
        with patch("core.api_key_usage.admin_cost_usd",
                   side_effect=RuntimeError("HTTP 404")),              patch("core.claude_code.collect", return_value={
                 "status": "ok", "note": "",
                 "records": [{"ts": self.now, "model": "claude-opus-5",
                              "cost": 4.0}]}):
            result = api_key_usage.collect_claude_api(
                self.cfg, self.now,
                {"mode": "api_key", "admin_key": "sk-ant-admin01-x"})
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["all_models"]["amount"], 4.0)
        self.assertIn("로컬 로그로 대체", result["all_models"] and result["note"])

    def test_unset_budget_shows_no_percentage_rather_than_a_fake_one(self):
        cfg = dict(self.cfg, claude_api_budget_usd=0)
        with patch("core.claude_code.collect", return_value={
                "status": "ok", "note": "",
                "records": [{"ts": self.now, "model": "claude-opus-5",
                             "cost": 4.0}]}):
            result = api_key_usage.collect_claude_api(
                cfg, self.now, {"mode": "api_key", "admin_key": ""})
        self.assertIsNone(result["all_models"]["used"])
        self.assertIn("예산 미설정", result["note"])

    def test_codex_api_mode_counts_this_months_tokens(self):
        buckets = {
            (self.month_start - 7200) // 3600: [500_000, 0, 0, 0, 1],
            (self.now - 3600) // 3600: [250_000, 0, 0, 0, 1],
        }
        with patch("core.codex.collect", return_value={
                "status": "ok", "note": "", "buckets": buckets}):
            result = api_key_usage.collect_codex_api(self.cfg, self.now)
        self.assertEqual(result["weekly"]["amount"], 250_000)
        self.assertAlmostEqual(result["weekly"]["used"], 25.0)
        self.assertEqual(result["weekly"]["unit"], "tokens")

    def test_snapshot_renders_api_mode_as_budget_not_measured(self):
        claude = api_key_usage._claude_api_result(
            25.0, 10.0, 100.0, self.now + 86400, "API 키 모드", False)
        codex = {"status": "ok", "note": "API 키 모드",
                 "weekly": {"used": 25.0, "resets_at": self.now + 86400,
                            "amount": 250_000, "budget": 1_000_000,
                            "unit": "tokens", "billed": False},
                 "plan": "api"}
        with patch("core.snapshot.now_ms", return_value=self.now * 1000),              patch("core.snapshot._read_current", return_value=(claude, codex)):
            snap = snapshot.build(self.cfg)

        limit = snap["providers"][0]["limits"][0]
        self.assertEqual(limit["used"], 25.0)
        self.assertFalse(limit["measured"])
        self.assertEqual(limit["primary_text"], "$25.0")
        self.assertIn("예산 $100", limit["sub"])
        html = render.render(snap, "surfacer")
        self.assertIn("예산 기준", html)
        self.assertIn("$25.0", html)
        self.assertIn("250K", render.render(snap, "surfacer"))


class WebView2BootstrapTests(unittest.TestCase):
    """First launch installs the one dependency q_console cannot ship - once,
    only when it is missing, and only after verifying what it downloaded."""

    def test_present_runtime_is_never_reinstalled(self):
        with patch("core.bootstrap.runtime_version", return_value="151.0.1.2"),              patch("core.bootstrap._download") as download:
            result = bootstrap.ensure_webview2()
        self.assertEqual(result["status"], "present")
        download.assert_not_called()

    def test_a_previous_attempt_is_not_repeated_on_every_launch(self):
        with patch("core.bootstrap.runtime_version", return_value=None),              patch("core.bootstrap.read_marker",
                   return_value={"attempted": True, "at": "어제",
                                 "result": "실패: 네트워크"}),              patch("core.bootstrap._download") as download:
            result = bootstrap.ensure_webview2()
        self.assertEqual(result["status"], "skipped")
        download.assert_not_called()

    def test_forced_retry_runs_even_after_a_failed_attempt(self):
        with patch("core.bootstrap.runtime_version", side_effect=[None, "1.2.3"]),              patch("core.bootstrap.read_marker", return_value={"attempted": True}),              patch("core.bootstrap._write_marker"),              patch("core.bootstrap._download", return_value="https://x.microsoft.com/a.exe"),              patch("core.bootstrap._authenticode_ok", return_value=True),              patch("core.bootstrap.subprocess.run") as run,              patch("core.bootstrap.os.remove"):
            result = bootstrap.ensure_webview2(force=True)
        self.assertEqual(result["status"], "installed")
        self.assertIn("/silent", run.call_args[0][0])
        self.assertIn("/install", run.call_args[0][0])

    def test_an_unsigned_download_is_never_executed(self):
        with patch("core.bootstrap.runtime_version", return_value=None),              patch("core.bootstrap.read_marker", return_value={}),              patch("core.bootstrap._write_marker"),              patch("core.bootstrap._download", return_value="https://x.microsoft.com/a.exe"),              patch("core.bootstrap._authenticode_ok", return_value=False),              patch("core.bootstrap.subprocess.run") as run,              patch("core.bootstrap.os.remove"):
            result = bootstrap.ensure_webview2()
        self.assertEqual(result["status"], "failed")
        self.assertIn("서명", result["detail"])
        run.assert_not_called()

    def test_only_microsoft_https_hosts_are_accepted(self):
        self.assertTrue(bootstrap._host_allowed(
            "https://msedge.sf.dl.delivery.mp.microsoft.com/f/setup.exe"))
        self.assertFalse(bootstrap._host_allowed(
            "http://msedge.sf.dl.delivery.mp.microsoft.com/f/setup.exe"))
        self.assertFalse(bootstrap._host_allowed(
            "https://microsoft.com.attacker.example/setup.exe"))
        self.assertFalse(bootstrap._host_allowed("https://example.com/setup.exe"))


if __name__ == "__main__":
    unittest.main()
