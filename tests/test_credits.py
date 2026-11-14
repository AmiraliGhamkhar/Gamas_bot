"""The credit currency: one ratio, one catalogue, no screen-level arithmetic."""

from __future__ import annotations

import unittest

from gamas_bot.billing import (
    CREDITS_PER_HOUR,
    CREDITS_PER_SECOND,
    CREDITS_PER_UNIT,
    HOURS_PER_UNIT,
    SECONDS_PER_HOUR,
    format_plan_price,
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
    def test_the_usage_conversion_unit_is_exact(self):
        # 100,000 credits are three hours of audio; this is not a price rule.
        self.assertEqual(CREDITS_PER_UNIT, 100_000)
        self.assertEqual(HOURS_PER_UNIT, 3)
        self.assertAlmostEqual(CREDITS_PER_HOUR, 100_000 / 3)
        self.assertAlmostEqual(CREDITS_PER_SECOND * SECONDS_PER_HOUR, CREDITS_PER_HOUR)
        self.assertEqual(credits_per_hour(3), CREDITS_PER_UNIT)
        self.assertEqual(credits_per_hour(9), 300_000)

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
    def test_exact_six_plan_catalog_and_original_sale_prices(self):
        paid = [plan for plan in plan_catalog() if not plan.is_free]
        self.assertEqual(
            [
                (
                    plan.included_hours,
                    plan.original_price_toman,
                    plan.price_toman,
                    plan.validity_days,
                )
                for plan in paid
            ],
            [
                (3, 160_000, 79_000, 30),
                (6, 200_000, 99_000, 30),
                (10, 220_000, 109_000, 30),
                (20, 360_000, 179_000, 30),
                (26, 460_000, 229_000, 30),
                (30, 600_000, 299_000, 30),
            ],
        )
        self.assertNotIn("paid_60h_30d", {plan.code for plan in paid})
        self.assertNotIn("paid_50h_30d", {plan.code for plan in paid})

    def test_free_plan_uses_the_same_ceil_conversion_as_paid_plans(self):
        free = next(plan for plan in plan_catalog() if plan.is_free)
        self.assertEqual(free.included_seconds, 3600)
        self.assertEqual(free.included_credits, 33_334)

    def test_credit_amounts_are_derived_from_seconds_not_plan_prices(self):
        paid = [plan for plan in plan_catalog() if not plan.is_free]
        self.assertEqual(
            [plan.included_credits for plan in paid],
            [100_000, 200_000, 333_334, 666_667, 866_667, 1_000_000],
        )
        self.assertEqual(paid[0].included_seconds, 3 * SECONDS_PER_HOUR)

    def test_original_and_sale_price_overrides_reach_the_catalogue(self):
        values = {attribute: default for _n, attribute, default in PLAN_ENV_FIELDS}
        values["plan_3_hours"] = 6
        values["plan_3_original_price_toman"] = 210_000
        values["plan_3_price_toman"] = 99_000
        plan = next(p for p in plan_catalog(values) if p.code == "paid_3h_30d")
        self.assertEqual(
            (plan.included_hours, plan.original_price_toman, plan.price_toman),
            (6, 210_000, 99_000),
        )
        self.assertEqual(plan.included_credits, 6 * CREDITS_PER_HOUR)

    def test_non_positive_override_falls_back_to_explicit_default(self):
        plan = next(
            p for p in plan_catalog({"plan_3_price_toman": 0}) if p.code == "paid_3h_30d"
        )
        self.assertEqual(plan.price_toman, 79_000)

    def test_wallet_price_label_uses_both_prices_when_discounted(self):
        self.assertEqual(
            format_plan_price(79_000, 160_000),
            "قیمت اصلی: ۱۶۰٬۰۰۰ تومان | قیمت نهایی: ۷۹٬۰۰۰ تومان",
        )
        self.assertEqual(format_plan_price(50_000, 50_000), "مبلغ: ۵۰٬۰۰۰ تومان")


if __name__ == "__main__":
    unittest.main()
