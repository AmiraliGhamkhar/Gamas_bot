"""Server-side validation of Telegram Mini App ``initData``.

Never trust a frontend-supplied ``user_id``; always derive the authenticated
Telegram user ID from the HMAC-SHA256-verified ``initData`` payload.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qsl


class InitDataError(ValueError):
    """Raised when Telegram Mini App ``initData`` is missing, malformed, stale, or invalid."""


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = 86_400,
    now: float | None = None,
) -> dict[str, Any]:
    """Verify Telegram WebApp ``initData`` using HMAC-SHA256 and return parsed user info.

    Returns ``{"user_id": int, "user": dict, "auth_date": int}``.
    """
    raw = (init_data or "").strip()
    token = (bot_token or "").strip()
    if not raw or not token:
        raise InitDataError("Missing initData or bot token")

    pairs = parse_qsl(raw, keep_blank_values=True)
    data: dict[str, str] = {}
    received_hash = ""
    for k, v in pairs:
        if k == "hash":
            received_hash = v
        else:
            data[k] = v

    if not received_hash:
        raise InitDataError("Missing initData hash")

    data_check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret_key = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        raise InitDataError("Invalid initData signature")

    try:
        auth_date = int(data.get("auth_date", "0"))
    except ValueError as exc:
        raise InitDataError("Invalid auth_date") from exc
    if auth_date <= 0:
        raise InitDataError("Missing auth_date")

    current = time.time() if now is None else float(now)
    if max_age_seconds > 0 and (current - auth_date) > max_age_seconds:
        raise InitDataError("Expired initData")

    user_raw = data.get("user")
    if not user_raw:
        raise InitDataError("Missing user in initData")
    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError as exc:
        raise InitDataError("Invalid user JSON in initData") from exc
    if not isinstance(user, dict) or "id" not in user:
        raise InitDataError("Missing user.id in initData")

    return {
        "user_id": int(user["id"]),
        "user": user,
        "auth_date": auth_date,
    }
