import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from core import config, opencodex_usage, snapshot
from core import __main__ as app

NOW = 1793414262


def payload():
    return {"reports": [{"provider": "anthropic", "source": "anthropic:oauth-usage", "quota": {
        "weeklyPercent": 6, "weeklyResetAt": (NOW + 500000) * 1000,
        "fiveHourPercent": 4, "fiveHourResetAt": (NOW + 11000) * 1000,
        "updatedAt": (NOW - 5) * 1000,
        "customWindows": [{"label": "Fable", "scope": "model", "percent": 0,
                           "resetAt": (NOW + 500000) * 1000}]}}]}


def credentials():
    return {"anthropic": {"activeAccountId": "synthetic", "selectionRevision": 1, "accounts": [
        {"id": "synthetic", "loginId": "login-one", "credential": {
            "accountId": "fixture-owner", "access": "secret-access", "refresh": "secret-refresh", "plan": "max_20x"}}]}}


class OpenCodexUsageTests(unittest.TestCase):
    def test_live_windows_use_usage_timestamp_and_milliseconds(self):
        value = opencodex_usage.extract(payload(), NOW)
        self.assertEqual([value[k]["used"] for k in ("all_models", "session", "fable")], [6, 4, 0])
        self.assertEqual(value["session"]["resets_at"], NOW + 11000)
        self.assertEqual(value["measured_at"], NOW - 5)

    def test_unknown_expired_and_invalid_windows_are_not_zero(self):
        for number in (None, True, -1, 101, float("nan"), "6"):
            data = payload()
            data["reports"][0]["quota"]["weeklyPercent"] = number
            self.assertIsNone(opencodex_usage.extract(data, NOW)["all_models"]["used"])
        data = payload()
        data["reports"][0]["quota"]["fiveHourResetAt"] = (NOW - 1) * 1000
        self.assertIsNone(opencodex_usage.extract(data, NOW)["session"]["used"])
        data["reports"][0]["quota"]["weeklyPercent"] = 100
        self.assertEqual(opencodex_usage.extract(data, NOW)["all_models"]["used"], 100)

    def test_old_future_aggregate_and_other_provider_reports_rejected(self):
        for change in (lambda q: q.update(updatedAt=(NOW - 121) * 1000),
                       lambda q: q.update(updatedAt=(NOW + 31) * 1000)):
            data = payload(); change(data["reports"][0]["quota"])
            with self.assertRaises(ValueError): opencodex_usage.extract(data, NOW)
        for key, value in (("provider", "anthropic-apikey"), ("source", "unverified"), ("aggregation", {"kind": "pool"})):
            data = payload(); data["reports"][0][key] = value
            with self.assertRaises(ValueError): opencodex_usage.extract(data, NOW)

    def test_same_account_read_never_writes_or_exports_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "auth.json"); path.write_text(json.dumps(credentials()), encoding="utf-8")
            before = path.read_bytes()
            with patch("core.opencodex_usage._quota", return_value=payload()):
                value = opencodex_usage.collect({"opencodex_home": directory}, NOW)
            self.assertEqual(value["status"], "ok")
            self.assertFalse(value["allow_carry"])
            self.assertEqual(path.read_bytes(), before)
            self.assertNotIn("secret-", json.dumps(value))

    def test_account_switch_and_login_rotation_during_read_drop_values(self):
        for change in (lambda d: d["anthropic"].update(selectionRevision=2),
                       lambda d: d["anthropic"]["accounts"][0].update(loginId="new-login")):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory, "auth.json"); path.write_text(json.dumps(credentials()), encoding="utf-8")
                def switched(*args):
                    data = credentials(); change(data)
                    path.write_text(json.dumps(data), encoding="utf-8")
                    return payload()
                with patch("core.opencodex_usage._quota", side_effect=switched):
                    value = opencodex_usage.collect({"opencodex_home": directory}, NOW)
                self.assertEqual(value["status"], "unavailable")
                self.assertIsNone(value["all_models"]["used"])

    def test_cli_uses_owner_cache_without_force_refresh_or_login(self):
        result = subprocess.CompletedProcess([], 0, json.dumps(payload()).encode())
        with patch("core.opencodex_usage.shutil.which", return_value="node.exe"), \
             patch("core.opencodex_usage._cli_entry", return_value="ocx.mjs"), \
             patch("core.opencodex_usage.subprocess.run", return_value=result) as run:
            opencodex_usage._quota({}, Path("fixture-home"))
        self.assertEqual(run.call_args.args[0], ["node.exe", "ocx.mjs", "provider", "quota", "--json"])
        self.assertEqual(run.call_args.kwargs["env"]["OPENCODEX_HOME"], "fixture-home")
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_selected_owner_never_tries_native_expired_grant(self):
        with patch("core.snapshot.opencodex_usage.collect", return_value={"status": "ok"}) as read, \
             patch("core.snapshot.plan_usage.collect_claude") as native:
            snapshot._read_claude({"claude_usage_source": "opencodex"}, NOW)
        read.assert_called_once(); native.assert_not_called()

    def test_failed_owner_never_reuses_native_or_previous_account_snapshot(self):
        good = {**opencodex_usage.extract(payload(), NOW), "source": "opencodex", "allow_carry": False, "status": "ok"}
        failed = {"source": "opencodex", "allow_carry": False, "status": "unavailable"}
        with patch("core.snapshot.api_key_usage.claude_api_credit_status", return_value={}), \
             patch("core.snapshot.now_ms", return_value=NOW * 1000):
            with patch("core.snapshot._read_current", return_value=(good, {}, {})):
                previous = snapshot.build(config.DEFAULTS)
            self.assertEqual(previous["providers"][0]["limits"][0]["measured_at"], NOW - 5)
            self.assertEqual(previous["providers"][0]["limits"][1]["measured_at"], NOW - 5)
            with patch("core.snapshot._read_current", return_value=(failed, {}, {})):
                current = snapshot.build(config.DEFAULTS, previous=copy.deepcopy(previous))
        self.assertIsNone(current["providers"][0]["limits"][0]["used"])
        self.assertIsNone(current["providers"][0]["limits"][1]["used"])
        self.assertIsNone(current["providers"][1]["limits"][0]["used"])

    def test_source_cli_selection_persists_without_network(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(config, "APP_HOME", directory), \
             patch.object(config, "CONFIG_PATH", str(Path(directory, "config.json"))), \
             patch.object(app, "emit"), patch.object(app, "refresh") as refresh:
            self.assertEqual(app.main(["--set-claude-source", "opencodex"]), 0)
            self.assertEqual(config.load()["claude_usage_source"], "opencodex")
            self.assertEqual(app.main(["--set-claude-source", "invalid"]), 2)
            refresh.assert_not_called()
