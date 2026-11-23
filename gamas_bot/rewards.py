"""Server-owned reward catalog and the rules that govern a redemption.

Reward points and processing credits are two different balances: points are
earned in Explore, seconds are consumed by the processing pipelines. Only this
module decides what a catalog entry costs and what it grants; nothing about a
redemption is taken from a client (a request names a code and nothing else).

Keeping the catalog here rather than in the HTTP layer is deliberate: the
redemption transaction has to validate the very same numbers it debits, and a
handler that passed in a cost would be trusting its caller.
"""

from __future__ import annotations

from typing import Any

#: The redeemable catalog. ``cost_points`` is an integer number of reward points;
#: ``credit_seconds`` is an integer number of processing seconds granted through
#: the ordinary credit ledger (``0`` means the entry grants no processing credit,
#: so no ledger row is ever written for it).
REWARD_CATALOG: list[dict[str, Any]] = [
    {"code": "theme_ocean", "category": "theme", "title": "تم اقیانوسی مینی‌اپ", "cost_points": 30, "credit_seconds": 0, "enabled": True, "expires_at": None, "max_per_user": 1},
    {"code": "badge_scholar", "category": "badge", "title": "نشان پژوهشگر برتر", "cost_points": 40, "credit_seconds": 0, "enabled": True, "expires_at": None, "max_per_user": 1},
    {"code": "credit_5m", "category": "credit_bonus", "title": "۵ دقیقه اعتبار گاماس", "cost_points": 50, "credit_seconds": 300, "enabled": True, "expires_at": None, "max_per_user": 5},
    {"code": "discount_10", "category": "discount", "title": "کد تخفیف ۱۰٪", "cost_points": 80, "credit_seconds": 0, "enabled": True, "expires_at": None, "max_per_user": 2},
]


def reward_item(code: str) -> dict[str, Any] | None:
    """The catalog entry for *code*, or ``None`` when it does not exist."""
    wanted = str(code or "").strip()
    if not wanted:
        return None
    return next((item for item in REWARD_CATALOG if str(item["code"]) == wanted), None)


def reward_cost_points(item: dict[str, Any]) -> int:
    """Integer point cost of one catalog entry (never client-supplied)."""
    cost = int(item["cost_points"])
    if cost <= 0:
        raise ValueError("reward cost must be positive")
    return cost


def reward_credit_seconds(item: dict[str, Any]) -> int:
    """Integer seconds an entry grants; ``0`` means it grants no credit."""
    return max(0, int(item.get("credit_seconds") or 0))


def reward_is_expired(item: dict[str, Any], *, now_iso: str) -> bool:
    """Whether a catalog entry's ``expires_at`` is in the past."""
    expires_at = item.get("expires_at")
    return bool(expires_at) and str(expires_at) < str(now_iso)
