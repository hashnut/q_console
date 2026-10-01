from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from core import plan_usage


class ClaudeOAuthRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "credentials.json"
        self.now = 1790850000
        self.credentials = {"other_client_field": {"preserve": True}, "claudeAiOauth": {
            "accessToken": "expired-test-access", "refreshToken": "test-refresh",
            "expiresAt": (self.now - 60) * 1000, "subscriptionType": "max",
            "rateLimitTier": "keep-tier", "scopes": ["user:profile", "user:inference"]}}
        self.write(self.credentials)
        self.cfg = {"claude_credentials_file": str(self.path), "claude_cli_version": "2.1.286"}
        self.renewed = {"access_token": "fresh-test-access", "refresh_token": "rotated-test-refresh",
                        "expires_in": 28800, "refresh_token_expires_in": 86400,
                        "scope": "user:profile user:inference"}
        self.usage = {"limits": [{"kind": "session", "percent": 5, "resets_at": "2026-10-02T00:20:00Z"},
                                 {"kind": "weekly_all", "percent": 30, "resets_at": "2026-10-02T12:00:00Z"}]}

    def write(self, credentials):
        self.path.write_text(json.dumps(credentials), encoding="utf-8")

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_expired_bearer_is_refreshed_before_old_usage_cooldown(self):
        old_scope = hashlib.sha256(b"expired-test-access").hexdigest()
        retry = {"after": self.now + 1800, "failures": 1, "auth_scope": old_scope}
        with patch("core.plan_usage._post_json", return_value=self.renewed) as post, \
             patch("core.plan_usage._get_json", return_value=self.usage) as get:
            result = plan_usage.collect_claude(self.cfg, now=self.now, retry=retry)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["session"]["used"], 5)
        self.assertEqual(result["all_models"]["used"], 30)
        self.assertEqual(get.call_args.args[1]["Authorization"], "Bearer fresh-test-access")
        self.assertEqual(post.call_args.args[0], plan_usage.CLAUDE_TOKEN_URL)
        self.assertEqual(post.call_args.args[2]["scope"], "user:profile user:inference")
        stored = self.read()
        self.assertEqual(stored["other_client_field"], {"preserve": True})
        self.assertEqual(stored["claudeAiOauth"]["rateLimitTier"], "keep-tier")
        self.assertEqual(stored["claudeAiOauth"]["refreshToken"], "rotated-test-refresh")
        self.assertEqual(stored["claudeAiOauth"]["expiresAt"], (self.now + 28800) * 1000)
        for secret in ("fresh-test-access", "test-refresh", "rotated-test-refresh"):
            self.assertNotIn(secret, json.dumps(result))

    def test_valid_bearer_does_not_refresh_and_401_retries_only_once(self):
        self.credentials["claudeAiOauth"]["expiresAt"] = (self.now + 3600) * 1000
        self.write(self.credentials)
        with patch("core.plan_usage._post_json", return_value=self.renewed) as post, \
             patch("core.plan_usage._get_json", return_value=self.usage):
            self.assertEqual(plan_usage.collect_claude(self.cfg, now=self.now)["status"], "ok")
            post.assert_not_called()
        with patch("core.plan_usage._post_json", return_value=self.renewed) as post, \
             patch("core.plan_usage._get_json", side_effect=[plan_usage.UsageHTTPError(401), self.usage]) as get:
            self.assertEqual(plan_usage.collect_claude(self.cfg, now=self.now)["status"], "ok")
            post.assert_called_once()
            self.assertEqual(get.call_count, 2)
        with patch("core.plan_usage._post_json", return_value=self.renewed) as post, \
             patch("core.plan_usage._get_json", side_effect=plan_usage.UsageHTTPError(401)) as get:
            self.assertEqual(plan_usage.collect_claude(self.cfg, now=self.now)["status"], "unavailable")
            post.assert_called_once()
            self.assertEqual(get.call_count, 2)

    def test_refresh_rate_limit_waits_across_workers_without_reposting_token(self):
        with patch("core.plan_usage._post_json", side_effect=plan_usage.UsageHTTPError(429, 600)) as post, \
             patch("core.plan_usage._get_json") as get:
            failed = plan_usage.collect_claude(self.cfg, now=self.now)
            retry = failed["usage_retry"]
            self.assertEqual(retry["phase"], "refresh")
            self.assertEqual(retry["after"], self.now + 600)
            waiting = plan_usage.collect_claude(self.cfg, now=self.now + 60, retry=retry)
            self.assertEqual(waiting["usage_retry"], retry)
            post.assert_called_once()
            get.assert_not_called()
        self.assertEqual(self.read(), self.credentials)

    def test_failed_or_malformed_refresh_keeps_credentials_intact(self):
        for response in ({"access_token": "test", "expires_in": False}, {"expires_in": 100}):
            with self.subTest(response=response), \
                 patch("core.plan_usage._post_json", return_value=response), \
                 patch("core.plan_usage._get_json") as get:
                result = plan_usage.collect_claude(self.cfg, now=self.now)
            self.assertEqual(result["status"], "unavailable")
            get.assert_not_called()
            self.assertEqual(self.read(), self.credentials)

    def test_native_client_account_change_is_not_overwritten(self):
        changed = copy.deepcopy(self.credentials)
        changed["claudeAiOauth"].update(accessToken="native-client-access", refreshToken="native-client-refresh",
                                       expiresAt=(self.now + 3600) * 1000)

        def refresh(*args):
            self.write(changed)
            return self.renewed

        with patch("core.plan_usage._post_json", side_effect=refresh), \
             patch("core.plan_usage._get_json", return_value=self.usage) as get:
            plan_usage.collect_claude(self.cfg, now=self.now)
        self.assertEqual(self.read(), changed)
        self.assertEqual(get.call_args.args[1]["Authorization"], "Bearer native-client-access")

    def test_metadata_changed_by_native_client_is_preserved(self):
        def refresh(*args):
            changed = self.read()
            changed["new_native_field"] = True
            changed["claudeAiOauth"]["native_metadata"] = "preserve"
            self.write(changed)
            return self.renewed

        with patch("core.plan_usage._post_json", side_effect=refresh):
            plan_usage._refresh_claude_credentials(str(self.path), self.credentials, self.now, "2.1.286")
        self.assertTrue(self.read()["new_native_field"])
        self.assertEqual(self.read()["claudeAiOauth"]["native_metadata"], "preserve")

    def test_concurrent_workers_refresh_once_and_reuse_the_winner(self):
        def refresh(*args):
            time.sleep(0.1)
            return self.renewed

        with patch("core.plan_usage._post_json", side_effect=refresh) as post, ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(plan_usage._refresh_claude_credentials, str(self.path), self.credentials,
                                   self.now, "2.1.286") for _ in range(2)]
            results = [f.result() for f in futures]
        post.assert_called_once()
        self.assertTrue(all(r["claudeAiOauth"]["accessToken"] == "fresh-test-access" for r in results))


if __name__ == "__main__":
    unittest.main()
