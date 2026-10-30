import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core import api_key_usage, config, console_credit, render, snapshot

ORG = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


class ConsoleCreditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(config, "APP_HOME", self.tmp.name)
        p.start()
        self.addCleanup(p.stop)
        self.cfg = dict(config.DEFAULTS, claude_console_org_id=ORG,
                        claude_credit_anchor_usd=999, claude_credit_anchor_at=1)
        self.now = 1793340000

    def response(self, amount=12957):
        return {"status": "ok", "org_id": ORG, "org_name": "Test API organization",
                "received_at": self.now,
                "payload": {"amount": amount, "balance_credits": 129,
                            "currency": "USD", "balance": {
                                "credits": {"amount_minor": amount, "exponent": 2}},
                            "promo_tranches": [{"remaining_amount_minor_units": amount - 1,
                                                "granted_amount_minor_units": 20000}],
                            "next_expires_at": "2026-11-04T00:00:00Z"}}

    def test_authoritative_balance_beats_rounded_field_and_grant(self):
        result = console_credit.parse_balance(self.response()["payload"])
        self.assertEqual(result["remaining"], 129.57)
        self.assertEqual(result["total"], 200)
        self.assertEqual(result["expiry"], "2026-11-04T00:00:00Z")

    def test_minor_units_exponent_and_real_zero(self):
        for exponent, amount, expected in ((0, 129, 129), (2, 0, 0), (4, 123456, 12.3456)):
            result = console_credit.parse_balance({"balance": {"credits": {
                "amount_minor": amount, "exponent": exponent}}})
            self.assertEqual(result["remaining"], expected)

    def test_missing_or_nonfinite_balance_is_not_zero(self):
        for payload in ({}, {"amount": None}, {"amount": "NaN"}, {"amount": True},
                        {"amount": 0, "currency": "EUR"},
                        {"balance": {"credits": {"amount_minor": 120, "exponent": -1}}}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                console_credit.parse_balance(payload)

    def test_default_path_never_uses_anchor_or_local_spend(self):
        with patch.object(console_credit, "_run_host", return_value=self.response()) as host, \
                patch.object(api_key_usage, "_local_api_spend", side_effect=AssertionError("local estimate used")):
            result = api_key_usage.claude_api_credit_status(self.cfg, self.now)
        host.assert_called_once_with(ORG)
        self.assertEqual(result["remaining"], 129.57)
        self.assertEqual(result["source"], "console")
        self.assertEqual(result["status"], "ok")

    def test_remote_usage_updates_without_any_local_logs(self):
        with patch.object(console_credit, "_run_host", side_effect=[self.response(), self.response(12004)]):
            first = console_credit.collect(self.cfg, self.now)
            second = console_credit.collect(self.cfg, self.now + 61)
        self.assertEqual(first["remaining"], 129.57)
        self.assertEqual(second["remaining"], 120.04)

    def test_refresh_interval_avoids_duplicate_requests(self):
        with patch.object(console_credit, "_run_host", return_value=self.response()) as host:
            console_credit.collect(self.cfg, self.now)
            result = console_credit.collect(self.cfg, self.now + 20)
        self.assertEqual(host.call_count, 1)
        self.assertEqual(result["remaining"], 129.57)

    def test_auth_failure_keeps_last_measurement_and_marks_stale(self):
        console_credit.collect(self.cfg, self.now, response=self.response())
        with patch.object(console_credit, "_run_host", return_value={"error": "auth"}):
            stale = console_credit.collect(self.cfg, self.now + 61)
        self.assertEqual(stale["remaining"], 129.57)
        self.assertEqual(stale["measured_at"], self.now)
        self.assertEqual(stale["status"], "stale")
        self.assertIn("세션 만료", snapshot.claude_api_credit_tip(stale))
        self.assertIn("*", snapshot.claude_api_credit_text(stale))
        provider = {"id": "claude-code", "api_credits": stale}
        html = render.render_overlay({"providers": [provider]}, {"overlay_items": ["claude-api"]})
        self.assertIn("$129.57*", html)
        self.assertIn("모든 PC 사용분 포함", html)
        self.assertIn("$129.57*", render.claude_api_credits_html(provider))

    def test_failure_without_measurement_is_unknown_not_estimate(self):
        with patch.object(console_credit, "_run_host", return_value={"error": "auth"}):
            result = console_credit.collect(self.cfg, self.now)
        self.assertIsNone(result["remaining"])
        self.assertIn("--", render.claude_api_credits_html({"api_credits": result}))
        self.assertNotIn("999", snapshot.claude_api_credit_text(result))

    def test_other_organization_cannot_reuse_previous_balance(self):
        console_credit.collect(self.cfg, self.now, response=self.response())
        cfg = dict(self.cfg, claude_console_org_id=OTHER)
        result = console_credit.collect(cfg, self.now + 1, response={"error": "auth"})
        self.assertIsNone(result["remaining"])
        self.assertEqual(result["organization_id"], OTHER)

    def test_mismatched_response_and_malformed_success_are_errors(self):
        for response in (dict(self.response(), org_id=OTHER), dict(self.response(), payload={})):
            result = console_credit.collect(self.cfg, self.now, response=response)
            self.assertIsNone(result["remaining"])
            self.assertEqual(result["status"], "unavailable")

    def test_too_old_measurement_is_not_kept(self):
        console_credit.collect(self.cfg, self.now, response=self.response())
        result = console_credit.collect(self.cfg, self.now + 86401, response={"error": "timeout"})
        self.assertIsNone(result["remaining"])

    def test_retry_backoff_does_not_extend_measurement_max_age(self):
        console_credit.collect(self.cfg, self.now, response=self.response())
        stale = console_credit.collect(self.cfg, self.now + 86399, response={"error": "auth"})
        self.assertEqual(stale["status"], "stale")
        with patch.object(console_credit, "_run_host", side_effect=AssertionError("backoff ignored")):
            expired = console_credit.collect(self.cfg, self.now + 86401)
        self.assertIsNone(expired["remaining"])
        self.assertEqual(expired["status"], "unavailable")

    def test_cache_contains_only_public_measurement_fields(self):
        response = dict(self.response(), cookies="secret-cookie", access_token="secret-token")
        console_credit.collect(self.cfg, self.now, response=response)
        cached = Path(self.tmp.name, console_credit.BALANCE_FILE).read_text(encoding="utf-8")
        self.assertNotIn("secret", cached)
        self.assertNotIn("cookies", cached)


if __name__ == "__main__":
    unittest.main()
