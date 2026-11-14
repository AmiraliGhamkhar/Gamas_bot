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

from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction

from .progress import to_persian_digits

#: Canonical plan codes. ``free_1h`` is the lifetime free grant.
FREE_PLAN_CODE = "free_1h"
PLAN_3_CODE = "paid_3h_30d"
PLAN_6_CODE = "paid_6h_30d"
PLAN_10_CODE = "paid_10h_30d"
PLAN_20_CODE = "paid_20h_30d"
PLAN_26_CODE = "paid_26h_30d"
PLAN_30_CODE = "paid_30h_30d"

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

#: One exact usage-conversion unit; it is not a purchase or price unit.
CREDITS_PER_UNIT = 100_000
SECONDS_PER_UNIT = 10_800
HOURS_PER_UNIT = SECONDS_PER_UNIT // SECONDS_PER_HOUR
#: Credits per second of processed audio (100 000 / 10 800 s).
CREDITS_PER_SECOND = Fraction(CREDITS_PER_UNIT, SECONDS_PER_UNIT)
#: Exact rational credits per hour (100 000 / 3). Never use this as a float
#: in accounting; conversion helpers below round with integer arithmetic.
CREDITS_PER_HOUR = Fraction(CREDITS_PER_UNIT, HOURS_PER_UNIT)


def _ceil_div(numerator: int, denominator: int) -> int:
    """Integer ceiling division for non-negative integers."""
    return -(-numerator // denominator)


def _positive_fraction(value: object) -> Fraction | None:
    """Return an exact positive rational without first coercing to ``float``.

    Durations usually arrive as integer seconds, but media probes and public
    helpers may carry decimal strings or Python floats.  ``str(float)`` retains
    the caller-visible decimal value rather than importing the binary floating
    point approximation into an accounting calculation.  Non-finite floats and
    malformed values are rejected before any arithmetic happens.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        # ``nan`` / infinities have no meaningful rational representation.
        if value != value or value in (float("inf"), float("-inf")):
            return None
    try:
        fraction = Fraction(value) if isinstance(value, int) else Fraction(str(value))
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return fraction if fraction > 0 else None


def seconds_to_credits(seconds: int | float | None) -> int:
    """Convert ledger seconds to credits, rounding up with exact arithmetic.

    ``credits = ceil(seconds * CREDITS_PER_UNIT / SECONDS_PER_UNIT)``.  No
    floating-point value participates in the multiplication or rounding.
    """
    fraction = _positive_fraction(seconds)
    if fraction is None:
        return 0
    numerator = fraction.numerator * CREDITS_PER_UNIT
    denominator = fraction.denominator * SECONDS_PER_UNIT
    return _ceil_div(numerator, denominator)


def credits_to_seconds(credits: int | float | None) -> int:
    """Convert credits to whole ledger seconds, rounding down exactly."""
    fraction = _positive_fraction(credits)
    if fraction is None:
        return 0
    # A fractional input can only arise at an API boundary; floor it before the
    # integer ledger conversion so a user is never promised extra audio.
    whole_credits = fraction.numerator // fraction.denominator
    return (whole_credits * SECONDS_PER_UNIT) // CREDITS_PER_UNIT


def credits_per_hour(hours: int | float) -> int:
    """Credits covering ``hours`` of audio, rounded up with exact rationals."""
    fraction = _positive_fraction(hours)
    if fraction is None:
        return 0
    numerator = fraction.numerator * CREDITS_PER_UNIT
    denominator = fraction.denominator * HOURS_PER_UNIT
    return _ceil_div(numerator, denominator)


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
    """One canonical plan and its configuration keys.

    ``key`` is the :meth:`gamas_bot.config.Settings.plan_values` prefix
    (for example ``plan_3_hours``). Original and sale prices are explicit,
    independent amounts.
    """

    code: str
    key: str
    hours: int
    original_price_toman: int
    price_toman: int
    validity_days: int
    sort_order: int


#: Explicit six-plan commercial catalog, ordered for display. List and sale
#: prices are intentionally independent; do not derive one from hours or from
#: the credit conversion ratio. No 50-hour tier is represented until both
#: missing prices are confirmed.
PAID_PLAN_SPECS: tuple[PlanSpec, ...] = (
    PlanSpec(PLAN_3_CODE, "plan_3", 3, 160_000, 79_000, 30, 1),
    PlanSpec(PLAN_6_CODE, "plan_6", 6, 200_000, 99_000, 30, 2),
    PlanSpec(PLAN_10_CODE, "plan_10", 10, 220_000, 109_000, 30, 3),
    PlanSpec(PLAN_20_CODE, "plan_20", 20, 360_000, 179_000, 30, 4),
    PlanSpec(PLAN_26_CODE, "plan_26", 26, 460_000, 229_000, 30, 5),
    PlanSpec(PLAN_30_CODE, "plan_30", 30, 600_000, 299_000, 30, 6),
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
    original_price_toman: int | None = None
    purchase_available: bool = True

    @property
    def included_credits(self) -> int:
        """Convert the plan's granted seconds to credits, rounding up safely."""
        return seconds_to_credits(self.included_seconds)

    @property
    def included_hours(self) -> int:
        return int(round(self.included_seconds / SECONDS_PER_HOUR))

    @property
    def discount_percent(self) -> float:
        """The explicit list-to-sale discount, or zero for plans without one."""
        original = self.original_price_toman
        if original is None or original <= 0 or self.price_toman <= 0:
            return 0.0
        return max(0.0, (original - self.price_toman) * 100 / original)


def _resolved(values: Mapping[str, int] | None, key: str, default: int) -> int:
    """Read one integer override, ignoring invalid non-positive values."""
    if not values:
        return default
    try:
        candidate = int(values.get(key, default))
    except (TypeError, ValueError):
        return default
    return candidate if candidate > 0 else default


def plan_catalog(values: Mapping[str, int] | None = None) -> tuple[Plan, ...]:
    """Return the authoritative catalog used by startup sync and the wallet.

    ``values`` accepts the resolved settings mapping (see
    :meth:`gamas_bot.config.Settings.plan_values`). The free grant is present
    for display and account logic but is never synchronized as a purchase plan.
    """
    free_seconds = _resolved(values, "free_plan_hours", FREE_PLAN_HOURS) * SECONDS_PER_HOUR
    plans: list[Plan] = [
        Plan(
            FREE_PLAN_CODE,
            FREE_PLAN_NAME,
            free_seconds,
            0,
            None,
            0,
            is_free=True,
            original_price_toman=None,
            purchase_available=False,
        ),
    ]
    for spec in PAID_PLAN_SPECS:
        hours = _resolved(values, f"{spec.key}_hours", spec.hours)
        days = _resolved(values, f"{spec.key}_validity_days", spec.validity_days)
        original_price = _resolved(
            values, f"{spec.key}_original_price_toman", spec.original_price_toman
        )
        price = _resolved(values, f"{spec.key}_price_toman", spec.price_toman)
        available = original_price > 0 and price > 0
        plans.append(
            Plan(
                spec.code,
                PAID_PLAN_NAME.format(
                    hours=to_persian_digits(hours), days=to_persian_digits(days)
                ),
                plan_seconds(hours),
                price,
                days,
                spec.sort_order,
                original_price_toman=original_price or None,
                purchase_available=available,
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


def format_plan_price(sale_price: int, original_price: int | None = None) -> str:
    """Render the list price and final sale price without implying a tariff rule."""
    sale = format_toman(sale_price)
    if original_price is not None and original_price > sale_price > 0:
        return (
            f"قیمت اصلی: {format_toman(original_price)} | "
            f"قیمت نهایی: {sale}"
        )
    return f"مبلغ: {sale}"


def format_validity(days: int | None) -> str:
    """Render a plan's validity the way the plan screens show it."""
    if days is None:
        return "بدون انقضا"
    return to_persian_digits(f"{int(days)} روز")


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
    # Always an int: a plan of "۲٫۵ ساعت" does not exist, and the credit table
    # is expressed in whole hours (3, 9, 12, 24, 36, 60).
    whole_hours = int(round(float(hours)))
    return f"{format_credits(credits)} ({to_persian_digits(whole_hours)} ساعت)"
