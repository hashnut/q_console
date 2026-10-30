import copy
import json
import subprocess
import unittest
from unittest.mock import patch

from core import config, gemini, render, snapshot


def report():
    return {"status": "SUCCESS", "num_turns": 0,
            "command": {"name": "usage", "data": {"groups": [
                {"name": "Gemini Models", "buckets": [
                    {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": .9,
                     "reset_time": "2026-11-04T23:38:47Z"},
                    {"id": "gemini-5h", "window": "5h", "remaining_fraction": 1,
                     "reset_time": "2026-10-30T16:46:19Z"}]},
                {"name": "Claude and GPT models", "buckets": [
                    {"id": "3p-weekly", "remaining_fraction": 0}]},
                {"name": "Gemini 4 Argon", "buckets": [
                    {"id": "gemini-argon-wtu-weekly", "window": "weekly",
                     "remaining_fraction": 0, "reset_time": "2026-11-02T00:52:16Z"}]}]}}}


class GeminiTests(unittest.TestCase):
    def test_reads_separate_gemini_windows_without_claude_gpt(self):
        limits = gemini.extract_usage(report())
        self.assertEqual([x["window_key"] for x in limits],
                         ["gemini-weekly", "gemini-5h", "gemini-argon-wtu-weekly"])
        self.assertEqual([x["used"] for x in limits], [10, 0, 100])
        self.assertTrue(all(x["resets_at"] for x in limits))

    def test_disabled_missing_and_invalid_fractions_are_not_zero_usage(self):
        for value in (None, True, float("nan"), float("inf"), -.1, 1.1, "0.9"):
            with self.subTest(value=value):
                data = report()
                bucket = data["command"]["data"]["groups"][0]["buckets"][0]
                bucket["remaining_fraction"] = value
                self.assertNotIn("gemini-weekly", [x["window_key"] for x in gemini.extract_usage(data)])
        data = report()
        data["command"]["data"]["groups"][0]["buckets"][1]["disabled"] = True
        self.assertNotIn("gemini-5h", [x["window_key"] for x in gemini.extract_usage(data)])
        for buckets in (None, 5, {}):
            data = report()
            data["command"]["data"]["groups"][0]["buckets"] = buckets
            self.assertEqual(len(gemini.extract_usage(data)), 1)

    def test_unknown_reset_stays_unknown(self):
        data = report()
        data["command"]["data"]["groups"][0]["buckets"][0]["reset_time"] = "bad"
        self.assertIsNone(gemini.extract_usage(data)[0]["resets_at"])

    def test_requires_usage_report_not_a_model_response(self):
        for data in ({"status": "SUCCESS", "response": "90%"},
                     {"status": "SUCCESS", "command": ["usage"]},
                     {**report(), "num_turns": 1},
                     {"status": "ERROR", "command": {"name": "usage"}},
                     {"status": "SUCCESS", "command": {"name": "usage", "data": {"groups": []}}}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                gemini.extract_usage(data)

    def test_cli_is_read_only_private_and_auto_update_disabled(self):
        result = subprocess.CompletedProcess([], 0, json.dumps(report()).encode())
        version = subprocess.CompletedProcess([], 0, b"1.4.10\n")
        with patch("core.gemini.subprocess.run", side_effect=[version, result]) as run:
            self.assertEqual(gemini._run_usage("test-agy.exe")["num_turns"], 0)
        args, kwargs = run.call_args
        self.assertEqual(args[0][:5], ["test-agy.exe", "-p", "/usage", "--output-format", "json"])
        self.assertEqual(kwargs["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"], "true")
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertNotEqual(kwargs["cwd"], str(__file__))
        self.assertLessEqual(kwargs["timeout"], 30)

    def test_old_cli_never_receives_a_usage_prompt(self):
        for version in (b"1.1.10", b"unknown"):
            with patch("core.gemini.subprocess.run", return_value=subprocess.CompletedProcess([], 0, version)) as run:
                with self.assertRaises(RuntimeError):
                    gemini._run_usage("test-agy.exe")
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[0], ["test-agy.exe", "--version"])

    def test_timeout_and_bad_json_are_safe_unknown(self):
        for error in (subprocess.TimeoutExpired("agy", 30), ValueError("private-token"), OSError()):
            with patch("core.gemini.Path.is_file", return_value=True), \
                 patch("core.gemini._run_usage", side_effect=error):
                result = gemini.collect(config.DEFAULTS)
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["limits"], [])
            self.assertNotIn("private-token", result["note"])

    def build(self, raw, previous=None):
        with patch("core.snapshot._read_current", return_value=({}, {}, raw)), \
             patch("core.snapshot.api_key_usage.claude_api_credit_status", return_value={}):
            return snapshot.build(config.DEFAULTS, previous)

    def test_failed_unidentified_account_does_not_reuse_old_quota(self):
        good = self.build({"status": "ok", "limits": gemini.extract_usage(report())})
        failed = self.build({"status": "unavailable", "limits": []}, copy.deepcopy(good))
        provider = failed["providers"][-1]
        self.assertEqual(provider["id"], "gemini")
        self.assertIsNone(provider["limits"][0]["used"])
        self.assertFalse(provider["limits"][0]["stale"])

    def test_codex_then_gemini_with_windows_and_independent_toggle(self):
        snap = self.build({"status": "ok", "limits": gemini.extract_usage(report())})
        html = render.render_overlay(snap, {"overlay_items": ["codex", "gemini"]})
        self.assertLess(html.index(">Codex</span>"), html.index(">Gemini 주간</span>"))
        self.assertIn(">5h</span>", html)
        self.assertIn(">Argon</span>", html)
        hidden = render.render_overlay(snap, {"overlay_items": ["codex"]})
        self.assertNotIn("Gemini", hidden)
        for theme in ("surfacer", "phosphor", "mini"):
            self.assertIn("Gemini", render.render(snap, theme))
        self.assertIn("Gemini 10% / 5h 0% / Argon 주간 100%", snap["summary_lines"][-1])
