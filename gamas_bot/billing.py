"""Canonical usage plans and display helpers for integer-second billing."""

from __future__ import annotations

from dataclasses import dataclass

from .progress import to_persian_digits

FREE_PLAN_CODE = "free_lifetime_1h"
PLAN_25_CODE = "paid_25h_30d"
PLAN_50_CODE = "paid_50h_30d"

# Canonical catalogue. These are deliberately not environment overrides:
# existing databases, migrations and new grants must agree on the promised plans.
FREE_PLAN_SECONDS = 3_600
PLAN_25_SECONDS = 90_000
PLAN_25_PRICE_TOMAN = 150_000
PLAN_25_VALIDITY_DAYS = 30
PLAN_50_SECONDS = 180_000
PLAN_50_PRICE_TOMAN = 250_000
PLAN_50_VALIDITY_DAYS = 30


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


def plan_catalog() -> tuple[Plan, ...]:
    """Return the one canonical catalogue used by DB seeding and the UI."""
    return (
        Plan(
            FREE_PLAN_CODE,
            "رایگان مادام‌العمر",
            FREE_PLAN_SECONDS,
            0,
            None,
            0,
            True,
        ),
        Plan(
            PLAN_25_CODE,
            "۲۵ ساعت / ۳۰ روز",
            PLAN_25_SECONDS,
            PLAN_25_PRICE_TOMAN,
            PLAN_25_VALIDITY_DAYS,
            1,
        ),
        Plan(
            PLAN_50_CODE,
            "۵۰ ساعت / ۳۰ روز",
            PLAN_50_SECONDS,
            PLAN_50_PRICE_TOMAN,
            PLAN_50_VALIDITY_DAYS,
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
