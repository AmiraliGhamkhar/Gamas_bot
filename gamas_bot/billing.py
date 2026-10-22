"""Canonical usage plans and display helpers for integer-second billing.

Accounting is done in **integer seconds** only; hours exist for the user-facing
copy and for the environment configuration knobs. The catalogue in this module
is the single source of truth used by the database seed/upsert, by the Telegram
plan screen and by the tests, so a price or duration can never diverge between
the promise and the code.

An operator may still override any tariff value through the environment (see
:data:`gamas_bot.config.PLAN_ENV_FIELDS`); the resolved values are what the
database rows, the UI and the entitlements use. Plans an administrator creates
or edits from the in-bot panel live in the same ``plans`` table, but they are
marked ``is_custom`` there and are deliberately outside this catalogue.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .progress import to_persian_digits

#: Canonical plan codes. ``free_1h`` is the lifetime free grant.
FREE_PLAN_CODE = "free_1h"
PLAN_5_CODE = "paid_5h_30d"
PLAN_10_CODE = "paid_10h_30d"
PLAN_20_CODE = "paid_20h_30d"
PLAN_25_CODE = "paid_25h_30d"
PLAN_50_CODE = "paid_50h_30d"

# Canonical catalogue. These are defaults: an operator may override the tariff
# through the ``FREE_PLAN_HOURS`` / ``PLAN_*`` environment variables, and the
# resolved values are what the database rows, the UI and the entitlements use.
FREE_PLAN_HOURS = 1

#: User-facing name of a paid plan; both numbers are rendered as Persian digits.
PAID_PLAN_NAME = "{hours} ساعت / {days} روز"

#: The one free grant; never sold, never renewed by ``/start``.
FREE_PLAN_NAME = "رایگان مادام‌العمر"

SECONDS_PER_HOUR = 3_600


@dataclass(frozen=True, slots=True)
class PlanSpec:
    """One canonical purchasable plan and the configuration keys behind it.

    ``key`` is the :meth:`gamas_bot.config.Settings.plan_values` prefix
    (``plan_5_hours`` / ``plan_5_price_toman`` / ``plan_5_validity_days``).
    """

    code: str
    key: str
    hours: int
    price_toman: int
    validity_days: int
    sort_order: int


#: The purchasable catalogue, cheapest first. The free plan is not listed: it
#: is granted by the database, never sold, and therefore has no price keys.
PAID_PLAN_SPECS: tuple[PlanSpec, ...] = (
    PlanSpec(PLAN_5_CODE, "plan_5", 5, 50_000, 30, 1),
    PlanSpec(PLAN_10_CODE, "plan_10", 10, 75_000, 30, 2),
    PlanSpec(PLAN_20_CODE, "plan_20", 20, 130_000, 30, 3),
    PlanSpec(PLAN_25_CODE, "plan_25", 25, 150_000, 30, 4),
    PlanSpec(PLAN_50_CODE, "plan_50", 50, 250_000, 30, 5),
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
    free_seconds = _resolved(values, "free_plan_hours", FREE_PLAN_HOURS) * SECONDS_PER_HOUR
    plans: list[Plan] = [
        Plan(FREE_PLAN_CODE, FREE_PLAN_NAME, free_seconds, 0, None, 0, True),
    ]
    for spec in PAID_PLAN_SPECS:
        hours = _resolved(values, f"{spec.key}_hours", spec.hours)
        days = _resolved(values, f"{spec.key}_validity_days", spec.validity_days)
        plans.append(
            Plan(
                spec.code,
                PAID_PLAN_NAME.format(
                    hours=to_persian_digits(hours), days=to_persian_digits(days)
                ),
                hours * SECONDS_PER_HOUR,
                _resolved(values, f"{spec.key}_price_toman", spec.price_toman),
                days,
                spec.sort_order,
            )
        )
    return tuple(plans)


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
    grouped = f"{int(amount):,}".replace(",", "٬")
    return f"{to_persian_digits(grouped)} تومان"


def format_validity(days: int | None) -> str:
    """Render a plan's validity the way the plan screens show it."""
    if days is None:
        return "بدون انقضا"
    return to_persian_digits(f"{int(days)} روز")
