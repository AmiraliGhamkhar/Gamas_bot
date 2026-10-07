"""Canonical usage plans and display helpers for integer-second billing."""

from __future__ import annotations

from dataclasses import dataclass

from .progress import to_persian_digits

FREE_PLAN_CODE = "free_lifetime_1h"
PLAN_25_CODE = "paid_25h_30d"
PLAN_50_CODE = "paid_50h_30d"

# Default catalogue. Deployments may resize plans through FREE_PLAN_HOURS and
# PLAN_25_* / PLAN_50_* (whole hours only); codes are stable identifiers.
# Entitlements are immutable snapshots, so a change only affects new grants.
FREE_PLAN_SECONDS = 3_600
PLAN_25_SECONDS = 90_000
PLAN_25_PRICE_TOMAN = 150_000
PLAN_25_VALIDITY_DAYS = 30
PLAN_50_SECONDS = 180_000
PLAN_50_PRICE_TOMAN = 250_000
PLAN_50_VALIDITY_DAYS = 30
SECONDS_PER_HOUR = 3_600


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


def plan_catalog(settings=None) -> tuple[Plan, ...]:
    """Return the one catalogue used by DB seeding and the UI.

    ``settings`` (a :class:`~gamas_bot.config.Settings`) supplies the
    environment-configured hours, prices and validity; without it the
    built-in defaults apply.
    """
    free_seconds = _hours(settings, "free_plan_hours", FREE_PLAN_SECONDS)
    p25 = _hours(settings, "plan_25_hours", PLAN_25_SECONDS)
    p50 = _hours(settings, "plan_50_hours", PLAN_50_SECONDS)
    p25_days = int(getattr(settings, "plan_25_validity_days", PLAN_25_VALIDITY_DAYS))
    p50_days = int(getattr(settings, "plan_50_validity_days", PLAN_50_VALIDITY_DAYS))
    return (
        Plan(FREE_PLAN_CODE, "رایگان (یک‌بار برای همیشه)", free_seconds, 0, None, 0, True),
        Plan(
            PLAN_25_CODE,
            _plan_name(p25, p25_days),
            p25,
            int(getattr(settings, "plan_25_price_toman", PLAN_25_PRICE_TOMAN)),
            p25_days,
            1,
        ),
        Plan(
            PLAN_50_CODE,
            _plan_name(p50, p50_days),
            p50,
            int(getattr(settings, "plan_50_price_toman", PLAN_50_PRICE_TOMAN)),
            p50_days,
            2,
        ),
    )


def _hours(settings, attribute: str, default_seconds: int) -> int:
    if settings is None:
        return default_seconds
    hours = getattr(settings, attribute)
    if isinstance(hours, bool) or not isinstance(hours, int) or hours <= 0:
        raise ValueError(f"{attribute} must be a positive whole number of hours")
    return hours * SECONDS_PER_HOUR


def _plan_name(seconds: int, days: int) -> str:
    return to_persian_digits(f"{seconds // SECONDS_PER_HOUR} ساعت / {days} روز")


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
