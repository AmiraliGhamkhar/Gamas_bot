"""The credit currency: one ratio, one catalogue, no screen-level arithmetic."""

from __future__ import annotations

import unittest

from gamas_bot.billing import (
    CREDITS_PER_HOUR,
    CREDITS_PER_SECOND,
    CREDIT_PACKAGE_SIZE,
    HOURS_PER_PACKAGE,
    PRICE_TOMAN_PER_PACKAGE,
    SECONDS_PER_HOUR,
    credits_per_hour,
    credits_to_seconds,
    format_credits,
    format_credits_number,
    format_credits_with_hours,
    format_credit_duration,
    format_seconds_as_credits,
    plan_catalog,
    seconds_to_credits,
)
from gamas_bot.config import PLAN_ENV_FIELDS


class CurrencyTests(unittest.TestCase):
    def test_the_business_ratio_is_exact(self):
        # 100,000 credits are three hours of audio.
        self.assertEqual(CREDIT_PACKAGE_SIZE, 100_000)
        self.assertEqual(HOURS_PER_PACKAGE, 3)
        self.assertAlmostEqual(CREDITS_PER_HOUR, 100_000 / 3)
        self.assertAlmostEqual(CREDITS_PER_SECOND * SECONDS_PER_HOUR, CREDITS_PER_HOUR)
        self.assertEqual(credits_per_hour(3), CREDIT_PACKAGE_SIZE)
        self.assertEqual(credits_per_hour(9), 300_000)

    def test_price_per_package_matches_the_agreed_tariff(self):
        # 100,000 credits cost 65,000 toman, i.e. 65,000 / 3 toman per hour.
        self.assertEqual(PRICE_TOMAN_PER_PACKAGE, 65_000)
        self.assertEqual(PRICE_TOMAN_PER_PACKAGE / HOURS_PER_PACKAGE, 65_000 / 3)

    def test_seconds_and_credits_round_in_the_safe_direction(self):
        self.assertEqual(seconds_to_credits(3600), 33_334)
        self.assertEqual(seconds_to_credits(10_800), 100_000)
        self.assertEqual(seconds_to_credits(1), 10)
        # A partial second costs a whole credit; credits never buy more time.
        self.assertEqual(seconds_to_credits(0.01), 1)
        self.assertEqual(credits_to_seconds(100_000), 10_800)
        self.assertEqual(credits_to_seconds(99), 10)
        # The pair never overstates a balance.
        self.assertGreaterEqual(
            seconds_to_credits(credits_to_seconds(100_000)), 100_000
        )
        self.assertEqual(seconds_to_credits(None), 0)
        self.assertEqual(credits_to_seconds(None), 0)

    def test_unknown_balance_is_not_zero(self):
        self.assertEqual(format_credits(None), "نامشخص")
        self.assertEqual(format_credits(0), "۰ اعتبار")

    def test_display_helpers_are_persian_and_grouped(self):
        self.assertEqual(format_credits(100_000), "۱۰۰٬۰۰۰ اعتبار")
        self.assertEqual(format_credits_number(100_000), "۱۰۰٬۰۰۰")
        self.assertEqual(format_seconds_as_credits(10_800), "۱۰۰٬۰۰۰ اعتبار")
        self.assertEqual(
            format_credits_with_hours(100_000, 3), "۱۰۰٬۰۰۰ اعتبار (۳ ساعت)"
        )

    def test_a_measured_file_shows_both_units(self):
        rendered = format_credit_duration(10_800)
        self.assertIn("۳ ساعت", rendered)
        self.assertIn("۱۰۰٬۰۰۰ اعتبار", rendered)


class CatalogueTests(unittest.TestCase):
    def test_every_canonical_plan_is_a_whole_number_of_packages(self):
        for plan in plan_catalog():
            if plan.is_free:
                continue
            self.assertEqual(
                plan.included_credits % CREDIT_PACKAGE_SIZE,
                0,
                f"{plan.code} is not a whole purchase unit",
            )

    def test_the_hourly_price_is_uniform_across_the_catalogue(self):
        for plan in plan_catalog():
            if plan.is_free:
                continue
            per_hour = plan.price_toman / plan.included_hours
            self.assertEqual(
                per_hour,
                PRICE_TOMAN_PER_PACKAGE / HOURS_PER_PACKAGE,
                f"{plan.code} breaks the uniform hourly price",
            )

    def test_the_six_agreed_plans_are_the_catalogue(self):
        hours = [plan.included_hours for plan in plan_catalog() if not plan.is_free]
        self.assertEqual(hours, [3, 9, 12, 24, 36, 60])

    def test_the_three_hour_plan_is_the_tariff_anchor(self):
        plan = next(p for p in plan_catalog() if p.code == "paid_3h_30d")
        self.assertEqual(plan.included_credits, 100_000)
        self.assertEqual(plan.price_toman, 65_000)
        self.assertEqual(plan.included_seconds, 3 * SECONDS_PER_HOUR)

    def test_a_tariff_override_reaches_the_catalogue(self):
        values = {attribute: default for _n, attribute, default in PLAN_ENV_FIELDS}
        values["plan_3_hours"] = 6
        values["plan_3_price_toman"] = 120_000
        plan = next(p for p in plan_catalog(values) if p.code == "paid_3h_30d")
        self.assertEqual((plan.included_hours, plan.price_toman), (6, 120_000))
        self.assertEqual(plan.included_credits, 6 * CREDITS_PER_HOUR)

    def test_a_non_positive_override_falls_back_to_the_default(self):
        plan = next(
            p for p in plan_catalog({"plan_3_price_toman": 0}) if p.code == "paid_3h_30d"
        )
        self.assertEqual(plan.price_toman, PRICE_TOMAN_PER_PACKAGE)


if __name__ == "__main__":
    unittest.main()
