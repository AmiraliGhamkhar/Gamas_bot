"""Mandatory Telegram channel subscription enforcement (Gamas Bot v4).

The bot grants access to its functional features only to users who are
*currently* members of the configured mandatory channel (default
``@gamas_AI``).

Design rules
------------

* **Fresh check on every protected interaction.** A previously successful
  verification is never trusted: membership is re-queried through the Telegram
  Bot API ``getChatMember`` method each time. The database only records the
  last observed status for observability, never as the enforcement source.
* **Confirmed non-membership only.** ``left`` and ``kicked`` are the only
  statuses that deny access. ``member``, ``administrator``, ``creator`` and
  ``restricted`` all count as a current member.
* **Fail open on infrastructure errors.** A timeout, network failure,
  malformed response or a lost administrator permission can never be reported
  as "not a member": that would lock paying users out because of an outage.
  Such failures are logged and (for permission errors, at most once per
  interval) raised to the administrators, while the interaction proceeds.
* **Administrators are exempt.** They are operators, not customers, and the
  channel requirement is a user-facing product rule. Their existing
  ``ADMIN_IDS`` authorization is untouched; no administrator id is hardcoded.
* **One in-flight check per user.** Simultaneous verifications share a single
  Telegram request instead of racing, and that sharing is cancellation-safe: a
  cancelled interaction can neither cancel a sibling's check nor leave a waiter
  awaiting a future nobody will resolve (see ``check_membership``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

import aiohttp
from telethon import Button

logger = logging.getLogger(__name__)

#: Callback prefix owned by this module (kept out of the bot's dispatch chain).
SUBSCRIPTION_PREFIX = "sub:"

#: Telegram ``ChatMember.status`` values that count as a current member.
ACTIVE_STATUSES = frozenset({"creator", "administrator", "member", "restricted"})
#: Statuses that prove the user is not a member and must be denied.
DENIED_STATUSES = frozenset({"left", "kicked"})

#: Result states returned by :meth:`SubscriptionService.check_membership`.
STATE_MEMBER = "member"
STATE_NOT_MEMBER = "not_member"
STATE_UNKNOWN = "unknown"

#: How often a permission/configuration failure is raised to the administrators.
ADMIN_ALERT_INTERVAL_SECONDS = 300.0
#: Cap applied to a Telegram ``retry_after`` so a flood wait cannot stall a worker.
MAX_FLOOD_WAIT_SECONDS = 60.0
#: A malformed ``retry_after`` must not become an unbounded sleep.
MAX_NETWORK_RETRY_DELAY_SECONDS = 1.0

#: Rendered for a user who is confirmed not to be a member. ``{channel_url}``
#: is the configured channel's public link, so a numeric channel id works too.
SUBSCRIPTION_REQUIRED_TEMPLATE = (
    "🔒 استفاده از ربات گاماس فقط برای اعضای کانال رسمی آن مجاز است.\n\n"
    "برای دسترسی به خدمات ربات، ابتدا به کانال گاماس بپیوندید:\n"
    "{channel_url}\n\n"
    "پس از عضویت، دکمهٔ «🔍 بررسی عضویت» را بزنید."
)
#: Rendered after a failed re-verification.
SUBSCRIPTION_STILL_REQUIRED_TEXT = (
    "🤔 عضویت شما در کانال گاماس هنوز تأیید نشده است.\n"
    "برای ادامه، ابتدا به کانال بپیوندید و سپس «🔍 بررسی عضویت» را بزنید."
)
#: Rendered after a successful re-verification.
SUBSCRIPTION_CONFIRMED_TEXT = (
    "✅ عضویت شما در کانال گاماس تأیید شد.\n"
    "اکنون می‌توانید از ربات استفاده کنید."
)
#: Rendered when the check could not be completed (infrastructure/permission).
#: It deliberately does not claim the user is (or is not) a member.
SUBSCRIPTION_UNAVAILABLE_TEXT = (
    "⚠️ در حال حاضر توانایی بررسی عضویت موقتاً در دسترس نیست.\n"
    "یک لحظه دیگر دوباره امتحان کنید."
)

#: The join action (opens the channel) plus the re-verification action. Built
#: per render so the button opens the configured channel's real link.
def _join_buttons(channel_url: str) -> list[list]:
    return [
        [
            Button.url("🌟 عضویت در کانال گاماس", channel_url),
            Button.inline("🔍 بررسی عضویت", b"sub:check"),
        ]
    ]

#: An async member-status resolver: ``user_id -> raw Telegram status``.
MemberResolver = Callable[[int], Awaitable[str]]


class SubscriptionApiError(Exception):
    """Telegram answered ``ok:false``; carries the Bot API error fields."""

    def __init__(self, code: int, description: str = "", retry_after: float | None = None):
        super().__init__(f"{code}: {description}")
        self.code = int(code)
        self.description = str(description)
        self.retry_after = retry_after


@dataclass(frozen=True)
class MembershipResult:
    """The outcome of one membership check."""

    state: str
    #: The raw Telegram ``ChatMember.status`` when one was known.
    status: str | None = None
    #: A short diagnostic for logs and the ``unknown`` detail path.
    detail: str = ""

    @property
    def is_member(self) -> bool:
        return self.state == STATE_MEMBER

    @property
    def is_denied(self) -> bool:
        return self.state == STATE_NOT_MEMBER

    @property
    def is_unknown(self) -> bool:
        return self.state == STATE_UNKNOWN


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SubscriptionService:
    """Centralized membership verification for one mandatory channel."""

    def __init__(
        self,
        bot,
        settings,
        db=None,
        *,
        resolver: MemberResolver | None = None,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.db = db if db is not None else bot.db
        self._resolver = resolver
        self._session: aiohttp.ClientSession | None = None
        #: user_id -> (raw status, monotonic timestamp) for the short-lived cache.
        self._cache: dict[int, tuple[str, float]] = {}
        #: user_id -> future, so concurrent checks share one request.
        self._inflight: dict[int, asyncio.Future[MembershipResult]] = {}
        self._channel_resolved = False
        #: Monotonic timestamp of the last administrator alert, or ``None``
        #: when none has been sent yet. ``time.monotonic()`` has an arbitrary
        #: epoch (system uptime), so ``0.0`` cannot mean "never": on a freshly
        #: booted host it is less than the throttle interval and the very
        #: first, most important alert would be swallowed.
        self._last_admin_alert: float | None = None

    # ------------------------------------------------------------------ config
    @property
    def configured(self) -> bool:
        return bool((self.settings.subscription_channel or "").strip())

    @property
    def required(self) -> bool:
        return bool(self.settings.subscription_required)

    @property
    def channel_ref(self) -> str:
        return (self.settings.subscription_channel or "").strip()

    @property
    def channel_url(self) -> str:
        return self.settings.subscription_channel_url

    def _is_admin(self, user_id: int | None) -> bool:
        return bool(user_id) and user_id in self.settings.admin_ids

    # --------------------------------------------------------------- transport
    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=float(self.settings.subscription_timeout_seconds)
                )
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:  # noqa: BLE001 - shutdown must never raise
                logger.debug("subscription HTTP session close failed", exc_info=True)
        self._session = None

    async def _http_member_status(self, user_id: int) -> str:
        """Call the Bot API ``getChatMember`` and return the raw status."""
        token = (self.settings.bot_token or "").strip()
        if not token:
            raise SubscriptionApiError(0, "TELEGRAM_BOT_TOKEN is not set")
        session = await self._ensure_session()
        url = f"{self.settings.telegram_api_base_url.rstrip('/')}/bot{token}/getChatMember"
        payload = {"chat_id": self.channel_ref, "user_id": int(user_id)}
        async with session.post(url, json=payload) as response:
            text = await response.text()
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise SubscriptionApiError(0, f"invalid JSON response: {text[:200]}") from exc
        if not isinstance(data, dict) or not data.get("ok"):
            code = int(data.get("error_code") or 0) if isinstance(data, dict) else 0
            description = str(data.get("description") or "") if isinstance(data, dict) else text[:200]
            retry_after = None
            if isinstance(data, dict):
                parameters = data.get("parameters") or {}
                if isinstance(parameters, dict):
                    raw_retry_after = parameters.get("retry_after")
                    if raw_retry_after is not None:
                        try:
                            retry_after = float(raw_retry_after)
                        except (TypeError, ValueError):
                            retry_after = None
            raise SubscriptionApiError(code, description, retry_after)
        result = data.get("result")
        if not isinstance(result, dict):
            raise SubscriptionApiError(0, "getChatMember returned no result")
        return str(result.get("status") or "")

    async def _resolve_status(self, user_id: int) -> str:
        if self._resolver is not None:
            return await self._resolver(user_id)
        return await self._http_member_status(user_id)

    # --------------------------------------------------------------- checking
    @staticmethod
    def _map_status(status: str) -> str:
        status = (status or "").strip().lower()
        if status in ACTIVE_STATUSES:
            return STATE_MEMBER
        if status in DENIED_STATUSES:
            return STATE_NOT_MEMBER
        return STATE_UNKNOWN

    def _is_non_participant_error(self, exc: SubscriptionApiError) -> bool:
        text = f"{exc.code} {exc.description}".lower()
        return (
            exc.code == 400
            and any(
                marker in text
                for marker in ("not a participant", "not a member", "user not found")
            )
        )

    async def check_membership(
        self,
        user_id: int,
        *,
        force: bool = False,
        allow_cache: bool = True,
    ) -> MembershipResult:
        """Return the current membership of *user_id*.

        Concurrent checks for the same user share one request. A short-lived
        cache (only when ``SUBSCRIPTION_CACHE_SECONDS`` is positive) dedupes
        repeated checks; a failed verification is never cached.
        """
        if (
            allow_cache
            and not force
            and self.settings.subscription_cache_seconds > 0
        ):
            cached = self._cache.get(user_id)
            if cached is not None and (time.monotonic() - cached[1]) < float(
                self.settings.subscription_cache_seconds
            ):
                return MembershipResult(self._map_status(cached[0]), cached[0], "cache")

        existing = self._inflight.get(user_id)
        if existing is not None:
            # Shield the shared request: cancelling a task cancels the future
            # it is awaiting, so a plain ``await existing`` would let one
            # cancelled interaction cancel the verification for the owner and
            # for every other waiter of the same user.
            return await asyncio.shield(existing)

        loop = asyncio.get_running_loop()
        future: asyncio.Future[MembershipResult] = loop.create_future()
        self._inflight[user_id] = future
        try:
            result = await self._check_uncached(user_id)
        except asyncio.CancelledError:
            # The shared request died with this task. Release the waiters with
            # an inconclusive result — never a denial — so no interaction can
            # wait forever on a future that nothing will resolve (fail open).
            if not future.done():
                future.set_result(MembershipResult(STATE_UNKNOWN, None, "cancelled"))
            raise
        finally:
            self._inflight.pop(user_id, None)

        if allow_cache and not force:
            if result.state == STATE_MEMBER and result.status:
                self._cache[user_id] = (result.status, time.monotonic())
            else:
                # A failed or inconclusive check must not be reused.
                self._cache.pop(user_id, None)
        try:
            future.set_result(result)
        except asyncio.InvalidStateError:  # pragma: no cover - concurrent cancel
            pass
        return result

    async def _check_uncached(self, user_id: int) -> MembershipResult:
        last_error: str = ""
        detail = ""
        for attempt in range(2):
            try:
                status = await self._resolve_status(user_id)
            except SubscriptionApiError as exc:
                if exc.code == 429 and exc.retry_after and attempt == 0:
                    delay = min(float(exc.retry_after), MAX_FLOOD_WAIT_SECONDS)
                    logger.warning(
                        "subscription flood wait user=%s retry_after=%s", user_id, delay
                    )
                    await asyncio.sleep(delay)
                    continue
                if self._is_non_participant_error(exc):
                    return MembershipResult(STATE_NOT_MEMBER, "left", str(exc))
                if exc.code == 403:
                    detail = "bot-not-admin"
                elif exc.code == 400 and "chat" in exc.description.lower():
                    detail = "channel-unavailable"
                else:
                    detail = f"api-{exc.code}"
                last_error = str(exc)
                if exc.code == 429 and attempt == 0:
                    continue
                break
            except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == 0:
                    await asyncio.sleep(MAX_NETWORK_RETRY_DELAY_SECONDS)
                    continue
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never fail closed
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "subscription check raised user=%s (%s)", user_id, last_error,
                    exc_info=True,
                )
                break
            mapped = self._map_status(status)
            if mapped == STATE_UNKNOWN:
                return MembershipResult(STATE_UNKNOWN, status or None, f"status:{status!r}")
            return MembershipResult(mapped, status or None, "")
        # Infrastructure/permission failure: fail open, never deny.
        logger.warning(
            "subscription check unavailable user=%s detail=%s", user_id, detail or last_error
        )
        return MembershipResult(STATE_UNKNOWN, None, detail or last_error or "unavailable")

    async def _record(self, user_id: int, result: MembershipResult) -> None:
        if result.state == STATE_MEMBER:
            status = "member"
            verified_at = _now_iso()
        else:
            status = result.status or result.state
            verified_at = None
        try:
            await self.db.set_subscription_state(user_id, status, verified_at=verified_at)
        except Exception:  # noqa: BLE001 - observability must never break a request
            logger.debug("could not record subscription state user=%s", user_id, exc_info=True)

    # ---------------------------------------------------------------- access
    async def _gate(
        self, user_id: int | None, *, force: bool = False
    ) -> MembershipResult | None:
        """Resolve one user's gate decision (no rendering).

        Returns ``None`` when the gate does not apply (administrator, or the
        gate is disabled/unconfigured), otherwise the membership result.
        Confirmed memberships and denials are recorded; an inconclusive check
        alerts the administrators when the cause is a permission problem.
        """
        if user_id is None:
            return None
        if self._is_admin(user_id):
            return None
        if not self.required or not self.configured:
            return None
        result = await self.check_membership(user_id, force=force)
        if result.is_member or result.is_denied:
            await self._record(user_id, result)
            return result
        # Unknown: infrastructure or permission. Never claim non-membership.
        if "bot-not-admin" in result.detail or "channel-unavailable" in result.detail:
            await self._alert_admins(
                "⚠️ بررسی عضویت کانال گاماس ناموفق است.\n"
                f"دلیل: {result.detail}\n"
                "ربات باید administrator کانال باشد؛ "
                "راهنمای تنظیم در docs/TELEGRAM_SUBSCRIPTION.md را ببینید."
            )
        return result

    async def require_subscription(
        self, user_id: int | None, *, force: bool = False
    ) -> bool:
        """Return ``True`` when *user_id* may use protected features.

        The decision-only form of the gate: administrators and a disabled or
        unconfigured gate allow, a confirmed non-member is denied, and an
        inconclusive check allows (fail open). Rendering happens in
        :meth:`require_access`.
        """
        result = await self._gate(user_id, force=force)
        return result is None or not result.is_denied

    async def require_access(self, event, *, force: bool = False) -> bool:
        """Gate one interaction; return ``True`` when the user may proceed.

        On denial the join prompt is already rendered; on an inconclusive check
        the interaction proceeds (fail open) and the "check unavailable" screen
        is shown instead of a denial.
        """
        user_id = getattr(event, "sender_id", None)
        if user_id is None:
            return True
        result = await self._gate(user_id, force=force)
        if result is None or result.is_member:
            return True
        if result.is_denied:
            await self._show_join_required(event)
            return False
        await self._show_unavailable(event)
        return True

    def _join_prompt(self) -> tuple[str, list[list]]:
        return (
            SUBSCRIPTION_REQUIRED_TEMPLATE.format(channel_url=self.channel_url),
            _join_buttons(self.channel_url),
        )

    async def _show_join_required(self, event) -> None:
        await self.bot._edit_or_reply(event, *self._join_prompt())

    async def _show_unavailable(self, event) -> None:
        await self.bot._edit_or_reply(
            event,
            SUBSCRIPTION_UNAVAILABLE_TEXT,
            [[Button.inline("🔍 بررسی عضویت", b"sub:check")]],
        )

    # ------------------------------------------------------------- callbacks
    async def handle_callback(self, event, user_id: int, data: str) -> None:
        """Handle ``sub:check`` (and the compatibility ``sub:join``)."""
        if data == f"{SUBSCRIPTION_PREFIX}check":
            await self.handle_check(event, user_id)
            return
        if data == f"{SUBSCRIPTION_PREFIX}join":
            # Kept for callers that render a callback join button; the shipped
            # prompt opens the channel with a URL button instead.
            await self._show_join_required(event)
            return
        await self.bot._ack(event)

    async def handle_check(self, event, user_id: int) -> None:
        """Fresh membership check triggered by the user."""
        result = await self.check_membership(user_id, force=True, allow_cache=False)
        if result.is_member:
            await self._record(user_id, result)
            await self.bot._ack(event)
            await self.bot._edit_or_reply(
                event,
                SUBSCRIPTION_CONFIRMED_TEXT,
                [[Button.inline("↩️ منوی اصلی", b"menu:main")]],
            )
            return
        if result.is_denied:
            await self._record(user_id, result)
            await self.bot._ack(event, SUBSCRIPTION_STILL_REQUIRED_TEXT)
            await self._show_join_required(event)
            return
        # Inconclusive: never tell the user they are not a member.
        await self.bot._ack(event)
        await self._show_unavailable(event)

    # -------------------------------------------------------- operational
    async def _alert_admins(self, text: str) -> None:
        now = time.monotonic()
        last = self._last_admin_alert
        if last is not None and now - last < ADMIN_ALERT_INTERVAL_SECONDS:
            return
        self._last_admin_alert = now
        try:
            await self.bot._notify_admins(text)
        except Exception:  # noqa: BLE001
            logger.debug("could not alert admins about subscription", exc_info=True)

    async def startup_self_check(self) -> bool:
        """Verify the bot is an administrator of the mandatory channel.

        Returns ``True`` when the channel is configured and the bot is an
        administrator. Failures are logged and raised to the administrators;
        they never prevent the bot from starting.
        """
        if not self.required or not self.configured:
            return True
        try:
            me = await self.bot.client.get_me()
        except Exception as exc:  # noqa: BLE001
            logger.warning("subscription self-check could not read bot identity: %s", exc)
            return False
        try:
            status = await self._http_member_status(int(getattr(me, "id", 0) or 0))
        except SubscriptionApiError as exc:
            logger.error(
                "subscription self-check failed (bot may not be channel admin): %s", exc
            )
            await self._alert_admins(
                "⚠️ بررسی خودکار کانال گاماس ناموفق بود.\n"
                f"دلیل: {exc}\n"
                "ربات باید administrator کانال @gamas_AI باشد."
            )
            return False
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as exc:
            logger.warning("subscription self-check unavailable: %s", exc)
            return False
        if status in {"administrator", "creator"}:
            logger.info("subscription self-check passed channel=%s", self.channel_ref)
            return True
        logger.error(
            "subscription self-check: bot status in channel is %r; "
            "the bot must be an administrator to verify members",
            status,
        )
        await self._alert_admins(
            "⚠️ ربات در کانال گاماس administrator نیست؛ بررسی عضویت کاربران "
            f"ممکن نیست (وضعیت: {status!r}). ربات را administrator کانال کنید."
        )
        return False
