"""Canonical usage plans and display helpers for integer-second billing.

Accounting is done in **integer seconds** only; hours exist for the user-facing
copy and for the environment configuration knobs. The catalogue in this module
is the single source of truth used by the database seed/upsert, by the Telegram
plan screen and by the tests, so a price or duration can never diverge between
the promise and the code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .progress import to_persian_digits

#: Canonical plan codes. ``free_1h`` is the lifetime free grant; the legacy
#: ``free_lifetime_1h`` code is still recognised so a database created before
#: migration 004 keeps working without granting anything twice.
FREE_PLAN_CODE = "free_1h"
LEGACY_FREE_PLAN_CODE = "free_lifetime_1h"
PLAN_25_CODE = "paid_25h_30d"
PLAN_50_CODE = "paid_50h_30d"

#: Every accepted spelling of the free plan, newest first.
FREE_PLAN_CODES = (FREE_PLAN_CODE, LEGACY_FREE_PLAN_CODE)

# Canonical catalogue. These are defaults: an operator may override the tariff
# through the ``FREE_PLAN_HOURS`` / ``PLAN_*`` environment variables, and the
# resolved values are what the database rows, the UI and the entitlements use.
FREE_PLAN_HOURS = 1
PLAN_25_HOURS = 25
PLAN_25_PRICE_TOMAN = 150_000
PLAN_25_VALIDITY_DAYS = 30
PLAN_50_HOURS = 50
PLAN_50_PRICE_TOMAN = 250_000
PLAN_50_VALIDITY_DAYS = 30

FREE_PLAN_SECONDS = FREE_PLAN_HOURS * 3_600
PLAN_25_SECONDS = PLAN_25_HOURS * 3_600
PLAN_50_SECONDS = PLAN_50_HOURS * 3_600

#: Keys understood by :func:`plan_catalog` when a deployment overrides values.
PLAN_VALUE_KEYS = (
    "free_plan_hours",
    "plan_25_hours",
    "plan_25_price_toman",
    "plan_25_validity_days",
    "plan_50_hours",
    "plan_50_price_toman",
    "plan_50_validity_days",
)


@dataclass(frozen=True, slots=True)
class Plan:
    code: str
    name: str
    included_seconds: int
    price_toman: int
    validity_days: int | None
    sort_order: int
    is_free: bool = False

    def as_record(self) -> dict[str, int | str | None]:
        return {
            "code": self.code,
            "name": self.name,
            "included_seconds": self.included_seconds,
            "price_toman": self.price_toman,
            "validity_days": self.validity_days,
            "sort_order": self.sort_order,
            "is_free": int(self.is_free),
        }


def _resolved(values: Mapping[str, int] | None, key: str, default: int) -> int:
    """Read one integer override, ignoring anything non-positive."""
    if not values:
        return default
    try:
        candidate = int(values.get(key, default))
    except (TypeError, ValueError):
        return default
    return candidate if candidate > 0 else default


def plan_catalog(values: Mapping[str, int] | None = None) -> tuple[Plan, ...]:
    """Return the one canonical catalogue used by DB seeding and the UI.

    ``values`` accepts the resolved settings mapping (see
    :meth:`gamas_bot.config.Settings.plan_values`). Without it the canonical
    business defaults are returned.
    """
    free_seconds = _resolved(values, "free_plan_hours", FREE_PLAN_HOURS) * 3_600
    hours_25 = _resolved(values, "plan_25_hours", PLAN_25_HOURS)
    hours_50 = _resolved(values, "plan_50_hours", PLAN_50_HOURS)
    return (
        Plan(
            FREE_PLAN_CODE,
            "رایگان مادام‌العمر",
            free_seconds,
            0,
            None,
            0,
            True,
        ),
        Plan(
            PLAN_25_CODE,
            f"{to_persian_digits(hours_25)} ساعت / "
            f"{to_persian_digits(_resolved(values, 'plan_25_validity_days', PLAN_25_VALIDITY_DAYS))} روز",
            hours_25 * 3_600,
            _resolved(values, "plan_25_price_toman", PLAN_25_PRICE_TOMAN),
            _resolved(values, "plan_25_validity_days", PLAN_25_VALIDITY_DAYS),
            1,
        ),
        Plan(
            PLAN_50_CODE,
            f"{to_persian_digits(hours_50)} ساعت / "
            f"{to_persian_digits(_resolved(values, 'plan_50_validity_days', PLAN_50_VALIDITY_DAYS))} روز",
            hours_50 * 3_600,
            _resolved(values, "plan_50_price_toman", PLAN_50_PRICE_TOMAN),
            _resolved(values, "plan_50_validity_days", PLAN_50_VALIDITY_DAYS),
            2,
        ),
    )


def format_duration(seconds: int | float | None) -> str:
    """Format billable time without rounding up or losing integer precision."""
    if seconds is None:
        return "نامشخص"
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours and minutes:
        value = f"{hours} ساعت و {minutes} دقیقه"
    elif hours:
        value = f"{hours} ساعت"
    elif minutes and secs:
        value = f"{minutes} دقیقه و {secs} ثانیه"
    elif minutes:
        value = f"{minutes} دقیقه"
    else:
        value = f"{secs} ثانیه"
    return to_persian_digits(value)


def format_toman(amount: int) -> str:
    return f"{to_persian_digits(f'{int(amount):,}')} تومان"
