"""Canonical usage plans, the credit currency, and display helpers.

Accounting is done in **integer seconds** only; the *credit* is the user-facing
unit. The business ratio is exact and stated once: **100 000 credits are three
hours of audio**, i.e. one hour costs :data:`CREDITS_PER_HOUR` (33 334) credits
and one second costs exactly 100/3 credits. The ledger never stores a fraction,
so :func:`seconds_to_credits` rounds up and :func:`credits_to_seconds` rounds
down — the user is never charged more time than the credits they hold.

That ratio is the single definition the ledger, the Telegram plan screen, the
mini app and the tests share; no screen may invent its own conversion.

The catalogue in this module is the single source of truth used by the database
seed/upsert, by the Telegram plan screen, the wallet and the tests, so a price
or duration can never diverge between the promise and the code.

An operator may still override any tariff value through the environment (see
:data:`gamas_bot.config.PLAN_ENV_FIELDS`); the resolved values are what the
database rows, the UI and the entitlements use. Plans an administrator creates
or edits from the in-bot panel live in the same ``plans`` table, but they are
marked ``is_custom`` there and are deliberately outside this catalogue.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from .progress import to_persian_digits

#: Canonical plan codes. ``free_1h`` is the lifetime free grant.
FREE_PLAN_CODE = "free_1h"
PLAN_3_CODE = "paid_3h_30d"
PLAN_9_CODE = "paid_9h_30d"
PLAN_12_CODE = "paid_12h_30d"
PLAN_24_CODE = "paid_24h_30d"
PLAN_36_CODE = "paid_36h_30d"
PLAN_60_CODE = "paid_60h_30d"

# Canonical catalogue. These are defaults: an operator may override the tariff
# through the ``FREE_PLAN_HOURS`` / ``PLAN_*`` environment variables, and the
# resolved values are what the database rows, the UI and the entitlements use.
FREE_PLAN_HOURS = 1

#: User-facing name of a paid plan. The credit amount is derived from the hours
#: at render time, so a tariff override can never desync the two numbers.
PAID_PLAN_NAME = "{hours} ساعت / {days} روز"

#: The one free grant; never sold, never renewed by ``/start``.
FREE_PLAN_NAME = "رایگان مادام‌العمر"

SECONDS_PER_HOUR = 3_600

# ---------------------------------------------------------------------------
# The credit currency
# ---------------------------------------------------------------------------

#: Minimum purchase unit, and the unit the tariff is quoted in.
CREDIT_PACKAGE_SIZE = 100_000
#: Hours of audio one purchase unit pays for.
HOURS_PER_PACKAGE = 3
#: Canonical selling price of one purchase unit, in toman
#: (100 000 credits = 3 hours = 60 000 T, i.e. 20 000 T per hour).
PRICE_TOMAN_PER_PACKAGE = 60_000
#: Credits per second of processed audio (100 000 / 10 800 s).
CREDITS_PER_SECOND = CREDIT_PACKAGE_SIZE / (HOURS_PER_PACKAGE * SECONDS_PER_HOUR)
#: Credits per hour of processed audio (100 000 / 3 = 33 333.33).
CREDITS_PER_HOUR = CREDIT_PACKAGE_SIZE / HOURS_PER_PACKAGE


def seconds_to_credits(seconds: int | float | None) -> int:
    """Convert ledger seconds to credits, rounding up.

    Rounding up keeps a fractional credit from ever becoming free audio: the
    rate is 100/3 credits per second, so most measurements are fractional.
    """
    if seconds is None:
        return 0
    value = float(seconds)
    if not math.isfinite(value):
        return 0
    return max(0, math.ceil(value * CREDITS_PER_SECOND))


def credits_to_seconds(credits: int | float | None) -> int:
    """Convert credits to whole ledger seconds, rounding down.

    The inverse of :func:`seconds_to_credits` for every amount a plan can sell:
    the pair is explicit about its rounding so a balance is never overstated.
    """
    if credits is None:
        return 0
    value = float(credits)
    if not math.isfinite(value):
        return 0
    return max(0, math.floor(value / CREDITS_PER_SECOND))


def credits_per_hour(hours: int | float) -> int:
    """Credits covering ``hours`` of audio, rounded up to the whole credit."""
    return int(math.ceil(float(hours) * CREDITS_PER_HOUR))


def seconds_per_package(credits: int) -> int:
    """Ledger seconds a credit amount pays for, at the canonical rate."""
    return credits_to_seconds(credits)


def plan_seconds(hours: int) -> int:
    """Ledger seconds a plan's hours are worth.

    Intentional approximation of one second per plan hour: the credit display
    is derived from :func:`seconds_to_credits`, so a plan always renders as the
    credits it actually grants (3 h → 100 000). Deriving the seconds from the
    credits instead would drift (10 800 s rents 33 334 credits per hour, not
    33 333) and the two numbers would stop agreeing across the whole catalogue.
    """
    return int(hours) * SECONDS_PER_HOUR


def format_credits(credits: int | float | None) -> str:
    """Render a credit amount with Persian digits and a thin thousands mark.

    ``None`` renders «نامشخص»: an unknown balance must never look like zero.
    The number is grouped with the Persian thousands separator so a six-digit
    balance stays readable in the wallet and the mini app.
    """
    if credits is None:
        return "نامشخص"
    total = max(0, int(credits))
    grouped = f"{total:,}".replace(",", "٬")
    return f"{to_persian_digits(grouped)} اعتبار"


def format_seconds_as_credits(seconds: int | float | None) -> str:
    """:func:`format_credits` for a ledger amount in seconds."""
    return format_credits(seconds_to_credits(seconds))


@dataclass(frozen=True, slots=True)
class PlanSpec:
    """One canonical purchasable plan and the configuration keys behind it.

    ``key`` is the :meth:`gamas_bot.config.Settings.plan_values` prefix
    (``plan_3_hours`` / ``plan_3_price_toman`` / ``plan_3_validity_days``).
    """

    code: str
    key: str
    hours: int
    price_toman: int
    validity_days: int
    sort_order: int


#: The purchasable catalogue, cheapest first. The free plan is not listed: it
#: is granted by the database, never sold, and therefore has no price keys.
#: Prices are proportional to the credited hours at the canonical
#: :data:`PRICE_TOMAN_PER_PACKAGE` rate (20 000 T per hour), so a longer plan is
#: a convenience bundle and never a hidden discount or surcharge.
PAID_PLAN_SPECS: tuple[PlanSpec, ...] = (
    PlanSpec(PLAN_3_CODE, "plan_3", 3, 60_000, 30, 1),
    PlanSpec(PLAN_9_CODE, "plan_9", 9, 180_000, 30, 2),
    PlanSpec(PLAN_12_CODE, "plan_12", 12, 240_000, 30, 3),
    PlanSpec(PLAN_24_CODE, "plan_24", 24, 480_000, 30, 4),
    PlanSpec(PLAN_36_CODE, "plan_36", 36, 720_000, 30, 5),
    PlanSpec(PLAN_60_CODE, "plan_60", 60, 1_200_000, 30, 6),
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

    @property
    def included_credits(self) -> int:
        """What the user is promised, in the currency they think in.

        A free grant keeps its whole hours (1 h → 100 000 credits) so the free
        and paid screens quote the same conversion.
        """
        if self.is_free:
            return self.included_hours * CREDIT_PACKAGE_SIZE // HOURS_PER_PACKAGE
        return seconds_to_credits(self.included_seconds)

    @property
    def included_hours(self) -> int:
        return int(round(self.included_seconds / SECONDS_PER_HOUR))


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
                plan_seconds(hours),
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


def format_usage(seconds: int | float | None) -> str:
    """Render a measured duration the way a lecture is described.

    «۱ ساعت و ۳۰ دقیقه» for the wallet and the mini app; a billing screen that
    states only «۵۴۰۰ اعتبار» leaves the user to do the arithmetic.
    """
    return format_duration(seconds)


def format_plan_row(plan: Plan) -> str:
    """One plan line shared by the Telegram screen and the mini app.

    The credit amount leads, because that is what the user spends; the hours it
    covers follow in parentheses so the tariff stays transparent.
    """
    return (
        f"{format_credits(plan.included_credits)}"
        f" ({to_persian_digits(plan.included_hours)} ساعت)"
        f" — {format_toman(plan.price_toman)}"
    )


def format_credit_duration(seconds: int | float | None) -> str:
    """A measured length in both units: «۱۲ دقیقه ≈ ۷۲٬۰۰۰ اعتبار».

    The duration is what the user recognises; the credit amount is what it will
    cost. Showing only one of the two is what makes a tariff feel arbitrary.
    """
    if seconds is None:
        return "نامشخص"
    return f"{format_duration(seconds)} ≈ {format_seconds_as_credits(seconds)}"


def format_credits_number(credits: int | float | None) -> str:
    """The bare grouped credit number, in Persian digits, without the unit.

    Buttons need this: a Telegram inline button label must stay short, so the
    unit word lives in the surrounding template instead of every label.
    """
    if credits is None:
        return "نامشخص"
    total = max(0, int(credits))
    return to_persian_digits(f"{total:,}".replace(",", "٬"))


def format_credits_with_hours(credits: int, hours: int | float) -> str:
    """«۱٬۰۸۰٬۰۰۰ اعتبار (۳ ساعت)» — what a plan promises, in both units."""
    whole = int(hours) if float(hours).is_integer() else hours
    return f"{format_credits(credits)} ({to_persian_digits(whole)} ساعت)"
