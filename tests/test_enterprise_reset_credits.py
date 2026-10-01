import copy
import datetime as dt
import unittest
from unittest.mock import patch

from core import config, plan_usage, render, snapshot


class EnterpriseResetCreditTests(unittest.TestCase):
    def setUp(self):
        self.now = int(dt.datetime(2026, 9, 30, 10).timestamp())
        self.enterprise = {
            "limits": [], "seven_day": None,
            "spend": {
                "used": {"amount_minor": 7095, "currency": "USD", "exponent": 2},
                "limit": {"amount_minor": 25000, "currency": "USD", "exponent": 2},
                "percent": 28,
            },
            "extra_usage": {
                "is_enabled": True, "used_credits": 7095.0,
                "monthly_limit": 25000, "utilization": 28.38,
                "currency": "USD", "decimal_places": 2,
            },
        }
        self.codex = {
            "status": "ok", "weekly": {"used": 2, "resets_at": self.now + 604800},
            "plan": "pro", "reset_credits": {
                "available_count": 1, "applicable_available_count": 0,
            },
        }

    def build(self, data=None, previous=None, now=None, failed=False):
        claude = {"status": "unavailable" if failed else "ok",
                  **plan_usage.extract_claude(self.enterprise if data is None else data, "enterprise")}
        with patch("core.snapshot._read_current", return_value=(claude, self.codex)), \
             patch("core.snapshot.now_ms", return_value=(self.now if now is None else now) * 1000):
            return snapshot.build(config.DEFAULTS, previous=previous)

    def test_enterprise_minor_units_are_converted_to_actual_monthly_money(self):
        result = plan_usage.extract_claude(self.enterprise, "enterprise")
        value = result["all_models"]
        self.assertAlmostEqual(value["used"], 28.38)
        self.assertEqual(value["amount"], 70.95)
        self.assertEqual(value["budget"], 250)
        self.assertEqual(value["window_key"], "month")
        self.assertIsNone(value["resets_at"])
        self.assertIsNone(result["fable"]["used"])

    def test_legacy_enterprise_extra_usage_and_zero_spend(self):
        for amount, expected in ((7095, 28.38), (0, 0)):
            result = plan_usage.extract_claude({"extra_usage": {
                "used_credits": amount, "monthly_limit": 25000,
                "currency": "USD", "decimal_places": 2,
            }}, "enterprise")
            self.assertAlmostEqual(result["all_models"]["used"], expected)
            self.assertEqual(result["all_models"]["amount"], amount / 100)

    def test_enterprise_without_a_spending_cap_keeps_money_without_fake_percent(self):
        data = copy.deepcopy(self.enterprise)
        data["spend"]["limit"] = None
        data["spend"]["percent"] = 0
        data["extra_usage"]["monthly_limit"] = None
        snap = self.build(data)
        limit = snap["providers"][0]["limits"][0]
        self.assertIsNone(limit["used"])
        self.assertIsNone(limit["budget"])
        self.assertEqual(limit["primary_text"], "$70.95")
        for theme in ("surfacer", "phosphor", "mini"):
            self.assertIn("$70.95", render.render(snap, theme))
        self.assertIn("$70.95", render.render_overlay(snap))

    def test_weekly_enterprise_seat_preserves_its_weekly_limit(self):
        result = plan_usage.extract_claude({"seven_day": {"utilization": 12},
                                         "extra_usage": self.enterprise["extra_usage"]}, "enterprise")
        self.assertEqual(result["all_models"]["used"], 12)
        self.assertNotIn("account_spend", result["all_models"])

    def test_different_currencies_do_not_form_a_percentage(self):
        data = copy.deepcopy(self.enterprise)
        data["spend"]["limit"]["currency"] = "EUR"
        value = plan_usage.extract_claude(data, "enterprise")["all_models"]
        self.assertIsNone(value["budget"])
        self.assertIsNone(value["used"])

    def test_reset_credits_distinguish_owned_from_currently_applicable(self):
        result = plan_usage.extract_codex({"rate_limit_reset_credits": {
            "available_count": 1, "applicable_available_count": 0,
        }})
        self.assertEqual(result["reset_credits"], self.codex["reset_credits"])
        for bad in (None, -1, True, 1.5, "bad", float("nan")):
            result = plan_usage.extract_codex({"rate_limit_reset_credits": {"available_count": bad}})
            self.assertIsNone(result["reset_credits"]["available_count"])
        self.assertIsNone(plan_usage.extract_codex({})["reset_credits"]["available_count"])

    def test_every_view_shows_enterprise_money_and_reset_credit_counts(self):
        snap = self.build()
        limit = snap["providers"][0]["limits"][0]
        self.assertTrue(limit["measured"])
        self.assertEqual(limit["key"], "month")
        for theme in ("surfacer", "phosphor", "mini"):
            html = render.render(snap, theme)
            self.assertIn("$70.95", html)
            self.assertIn("$250.00", html)
            self.assertIn("리셋권 1개", html)
            self.assertIn("사용 가능 0개", html)
        html = render.render_overlay(snap)
        self.assertIn("Enterprise", html)
        self.assertIn("$70.95", html)
        self.assertIn("리셋권 1개", html)
        self.assertIn("사용 가능 0개", html)
        self.assertIn("$70.95", snap["detail_text"])
        self.assertNotIn("API 키 모드", snap["detail_text"])
        self.assertNotIn("롤링", render.render(snap, "mini"))

    def test_failed_enterprise_read_carries_money_but_never_past_a_new_month(self):
        good = self.build()
        carried = self.build({}, previous=good, now=self.now + 600, failed=True)
        limit = carried["providers"][0]["limits"][0]
        self.assertTrue(limit["stale"])
        self.assertTrue(limit["measured"])
        self.assertEqual(limit["amount"], 70.95)
        next_month = int(dt.datetime(2026, 10, 1, 0, 1).timestamp())
        expired = self.build({}, previous=good, now=next_month, failed=True)
        self.assertIsNone(expired["providers"][0]["limits"][0]["amount"])

    def test_enterprise_does_not_reuse_previous_personal_fable_percentage(self):
        prior = self.build()
        prior["providers"][1]["plan"] = "max"
        prior["providers"][1]["limits"][0].update(
            used=35, measured_at=self.now, resets_at=self.now + 604800)
        snap = self.build(previous=prior)
        self.assertIsNone(snap["providers"][1]["limits"][0]["used"])

    def test_reset_credit_expiries_keep_only_available_credits_soonest_first(self):
        data = {"credits": [
            {"status": "available", "expires_at": "2026-10-29T18:53:16.605377Z"},
            {"status": "redeemed", "expires_at": "2026-10-02T00:00:00Z"},
            {"status": "available", "expires_at": "2026-10-05T04:00:00Z"},
            {"status": "available", "expires_at": None},
            "bad",
        ]}
        self.assertEqual(plan_usage.extract_reset_credit_expiries(data), [
            plan_usage._iso_epoch("2026-10-05T04:00:00Z"),
            plan_usage._iso_epoch("2026-10-29T18:53:16Z"),
        ])
        self.assertEqual(plan_usage.extract_reset_credit_expiries({}), [])

    def test_every_view_shows_reset_credit_expiry_as_tooltip(self):
        self.codex["reset_credits"]["expires_at"] = [self.now + 2 * 86400 + 3 * 3600]
        snap = self.build()
        expiry = dt.datetime.fromtimestamp(self.now + 2 * 86400 + 3 * 3600).strftime("%m-%d %H:%M")
        tip = "리셋권 만료 · %s (2d 3h 남음)" % expiry
        self.assertEqual(snap["providers"][-1]["reset_credit_tip"], tip)
        for theme in ("surfacer", "phosphor", "mini"):
            self.assertIn("title='%s'" % tip, render.render(snap, theme))
        self.assertIn("title='%s'" % tip, render.render_overlay(snap))
        self.assertIn(tip, snap["detail_text"])

    def test_reset_credit_tooltip_states_unknown_or_missing_expiry(self):
        self.assertEqual(snapshot.reset_credit_tooltip(
            {"available_count": 1, "expires_at": None}, self.now), "리셋권 만료 시각 미확인")
        self.assertEqual(snapshot.reset_credit_tooltip(
            {"available_count": 1, "expires_at": []}, self.now), "리셋권 만료 시각 미제공")
        self.assertEqual(snapshot.reset_credit_tooltip({"available_count": 0}, self.now), "")
        self.codex["reset_credits"]["available_count"] = 0
        html = render.render_overlay(self.build())
        self.assertNotIn("만료", html)

    def test_hiding_codex_hides_its_reset_credits(self):
        html = render.render_overlay(self.build(), {"overlay_items": ["claude-code"]})
        self.assertNotIn("리셋권", html)
        self.assertNotIn("aria-label='ChatGPT'", html)

    def test_mini_can_render_an_unavailable_provider_without_limits(self):
        snap = self.build()
        snap["providers"][0]["limits"] = []
        self.assertIn("기록없음", render.render(snap, "mini"))


if __name__ == "__main__":
    unittest.main()
