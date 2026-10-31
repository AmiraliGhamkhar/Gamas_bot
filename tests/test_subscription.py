"""Tests for the mandatory channel subscription gate.

The gate is verified offline: a resolver is injected into
``bot.subscription._resolver`` so no test reaches the network, exactly like
``tests/test_bot.py`` wires ``bot.client`` to a fake.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase, mock
from unittest.mock import AsyncMock

import aiohttp

from gamas_bot import db as db_module
from gamas_bot.bot import GamasBot
from gamas_bot.config import check, from_env
from gamas_bot.subscription import (
    ACTIVE_STATUSES,
    DENIED_STATUSES,
    STATE_MEMBER,
    STATE_NOT_MEMBER,
    STATE_UNKNOWN,
    SUBSCRIPTION_PREFIX,
    SubscriptionApiError,
    SubscriptionService,
)
from tests.support import FakeJobEvent, FakeTelegramClient, make_settings

USER_ID = 777001

#: Substrings that appear only in the corresponding rendered screen.
JOIN_PROMPT_MARKER = "کانال گاماس بپیوندید"
CONFIRMED_MARKER = "تأیید شد"
UNAVAILABLE_MARKER = "در دسترس نیست"


class _CallbackQuery:
    def __init__(self, sender_id: int):
        self.sender_id = sender_id
        self.message = None


class _OriginalUpdate:
    def __init__(self, sender_id: int):
        self.user_id = sender_id
        self.event = _CallbackQuery(sender_id)


class CallbackEvent:
    """Minimal Telethon CallbackQuery double that records every outcome."""

    is_private = True

    def __init__(self, sender_id: int, data: bytes):
        self.sender_id = sender_id
        self.chat_id = sender_id
        self.user_id = sender_id
        self.data = data
        self.answers: list[tuple] = []
        self.edits: list[tuple] = []
        self.responses: list[tuple] = []
        self.original_update = _OriginalUpdate(sender_id)
        self.client = FakeTelegramClient()

    async def answer(self, text=None, *, alert=False):
        self.answers.append((text, alert))

    async def edit(self, text, buttons=None):
        self.edits.append((text, buttons))

    async def respond(self, text="", buttons=None, **_kwargs):
        self.responses.append((text, buttons))


class SubscriptionTestCase(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.settings = make_settings(
            db_path=root / "subscription.db",
            temp_dir=root / "tmp",
            subscription_required=True,
            subscription_channel="@gamas_AI",
            # Raw-only: no note API, so no test ever reaches the network.
            note_api_key="",
            note_api_base_url="",
        )
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self.db = db_module.Database(self.settings.db_path)
        await self.db.connect()
        await self.db.sync_plans(
            [
                {
                    "code": "paid_5h_30d",
                    "hours": 5,
                    "price": 50000,
                    "validity_days": 30,
                },
            ]
        )
        self.bot = GamasBot(self.settings)
        self.bot.db = self.db
        # The gate records its finding on the same connected database the bot
        # uses, so point the service at it too.
        self.bot.subscription.db = self.db
        self.bot.client = FakeTelegramClient()

    async def asyncTearDown(self):
        await self.bot.shutdown()
        await self.db.close()
        self._tmp.cleanup()

    async def use_resolver(self, outcome) -> None:
        """Make the membership lookup return ``outcome`` (or raise it)."""

        async def resolver(user_id):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        self.bot.subscription._resolver = resolver


class StatusMappingTests(TestCase):
    def test_active_statuses_map_to_member(self):
        for status in ACTIVE_STATUSES:
            self.assertEqual(SubscriptionService._map_status(status), STATE_MEMBER)

    def test_denied_statuses_map_to_not_member(self):
        for status in DENIED_STATUSES:
            self.assertEqual(SubscriptionService._map_status(status), STATE_NOT_MEMBER)

    def test_restricted_counts_as_member(self):
        # A restricted member can still read the channel, so they stay allowed.
        self.assertEqual(SubscriptionService._map_status("restricted"), STATE_MEMBER)

    def test_unknown_status_is_unknown(self):
        for status in ("bogus", "", None):
            self.assertEqual(SubscriptionService._map_status(status), STATE_UNKNOWN)


class MembershipTests(SubscriptionTestCase):
    async def test_member(self):
        await self.use_resolver("member")
        result = await self.bot.subscription.check_membership(USER_ID)
        self.assertEqual(result.state, STATE_MEMBER)
        self.assertEqual(result.status, "member")

    async def test_left_is_not_member(self):
        await self.use_resolver("left")
        result = await self.bot.subscription.check_membership(USER_ID)
        self.assertEqual(result.state, STATE_NOT_MEMBER)

    async def test_kicked_is_not_member(self):
        await self.use_resolver("kicked")
        result = await self.bot.subscription.check_membership(USER_ID)
        self.assertEqual(result.state, STATE_NOT_MEMBER)

    async def test_infra_error_is_unknown_and_never_denied(self):
        await self.use_resolver(SubscriptionApiError(0, "network down"))
        result = await self.bot.subscription.check_membership(USER_ID)
        self.assertEqual(result.state, STATE_UNKNOWN)

    async def test_forbidden_is_unknown(self):
        await self.use_resolver(SubscriptionApiError(403, "Forbidden"))
        result = await self.bot.subscription.check_membership(USER_ID)
        self.assertEqual(result.state, STATE_UNKNOWN)

    async def test_malformed_status_is_unknown(self):
        await self.use_resolver("banned")
        result = await self.bot.subscription.check_membership(USER_ID)
        self.assertEqual(result.state, STATE_UNKNOWN)

    async def test_confirmed_non_participant_400_is_denied(self):
        await self.use_resolver(SubscriptionApiError(400, "not a participant"))
        result = await self.bot.subscription.check_membership(USER_ID)
        self.assertEqual(result.state, STATE_NOT_MEMBER)

    async def test_cache_is_used_for_confirmed_members(self):
        self.bot.subscription.settings = replace(
            self.bot.subscription.settings, subscription_cache_seconds=300.0
        )
        calls = []

        async def resolver(user_id):
            calls.append(user_id)
            return "member"

        self.bot.subscription._resolver = resolver
        first = await self.bot.subscription.check_membership(USER_ID)
        second = await self.bot.subscription.check_membership(USER_ID)
        self.assertEqual((first.state, second.state), (STATE_MEMBER, STATE_MEMBER))
        self.assertEqual(len(calls), 1)

    async def test_failure_is_not_cached(self):
        self.bot.subscription.settings = replace(
            self.bot.subscription.settings, subscription_cache_seconds=300.0
        )
        calls = []

        async def resolver(user_id):
            calls.append(user_id)
            raise SubscriptionApiError(0, "down")

        self.bot.subscription._resolver = resolver
        first = await self.bot.subscription.check_membership(USER_ID)
        second = await self.bot.subscription.check_membership(USER_ID)
        self.assertEqual((first.state, second.state), (STATE_UNKNOWN, STATE_UNKNOWN))
        self.assertEqual(len(calls), 2)

    async def test_in_flight_requests_are_deduplicated(self):
        calls = []

        async def resolver(user_id):
            calls.append(user_id)
            await asyncio.sleep(0.05)
            return "member"

        self.bot.subscription._resolver = resolver
        results = await asyncio.gather(
            self.bot.subscription.check_membership(USER_ID),
            self.bot.subscription.check_membership(USER_ID),
        )
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(result.state == STATE_MEMBER for result in results))

    async def test_network_error_retries_once(self):
        calls = []

        async def resolver(user_id):
            calls.append(user_id)
            raise aiohttp.ClientError("boom")

        self.bot.subscription._resolver = resolver
        with mock.patch("gamas_bot.subscription.asyncio.sleep", new=AsyncMock()):
            result = await self.bot.subscription.check_membership(USER_ID)
        self.assertEqual(result.state, STATE_UNKNOWN)
        self.assertEqual(len(calls), 2)

    async def test_429_is_retried(self):
        calls = []

        async def resolver(user_id):
            calls.append(user_id)
            if len(calls) == 1:
                raise SubscriptionApiError(429, "Too Many Requests", retry_after=0.01)
            return "member"

        self.bot.subscription._resolver = resolver
        with mock.patch("gamas_bot.subscription.asyncio.sleep", new=AsyncMock()):
            result = await self.bot.subscription.check_membership(USER_ID)
        self.assertEqual(result.state, STATE_MEMBER)
        self.assertEqual(len(calls), 2)


class GateTests(SubscriptionTestCase):
    async def test_non_member_is_denied_with_join_prompt(self):
        await self.use_resolver("left")
        event = FakeJobEvent(chat_id=USER_ID)
        self.assertFalse(await self.bot.subscription.require_access(event))
        self.assertTrue(any(JOIN_PROMPT_MARKER in text for text in event.replies))

    async def test_member_is_allowed_without_prompt(self):
        await self.use_resolver("member")
        event = FakeJobEvent(chat_id=USER_ID)
        self.assertTrue(await self.bot.subscription.require_access(event))
        self.assertFalse(any(JOIN_PROMPT_MARKER in text for text in event.replies))

    async def test_require_subscription_decision_only_form(self):
        await self.use_resolver("left")
        self.assertFalse(await self.bot.subscription.require_subscription(USER_ID))
        await self.use_resolver("member")
        self.assertTrue(await self.bot.subscription.require_subscription(USER_ID))
        await self.use_resolver(SubscriptionApiError(0, "down"))
        self.assertTrue(await self.bot.subscription.require_subscription(USER_ID))
        self.bot.subscription.settings = replace(
            self.bot.subscription.settings, admin_ids={USER_ID}
        )
        self.assertTrue(await self.bot.subscription.require_subscription(USER_ID))

    async def test_admin_is_exempt(self):
        await self.use_resolver("left")
        self.bot.subscription.settings = replace(
            self.bot.subscription.settings, admin_ids={USER_ID}
        )
        event = FakeJobEvent(chat_id=USER_ID)
        self.assertTrue(await self.bot.subscription.require_access(event))
        self.assertFalse(any(JOIN_PROMPT_MARKER in text for text in event.replies))

    async def test_unknown_fails_open(self):
        await self.use_resolver(SubscriptionApiError(0, "network down"))
        event = FakeJobEvent(chat_id=USER_ID)
        self.assertTrue(await self.bot.subscription.require_access(event))
        self.assertTrue(any(UNAVAILABLE_MARKER in text for text in event.replies))

    async def test_unknown_alerts_admins_throttled(self):
        await self.use_resolver(SubscriptionApiError(403, "bot is not an administrator"))
        notify = AsyncMock()
        self.bot._notify_admins = notify
        event = FakeJobEvent(chat_id=USER_ID)
        self.assertTrue(await self.bot.subscription.require_access(event))
        self.assertTrue(await self.bot.subscription.require_access(event))
        self.assertEqual(notify.await_count, 1)

    async def test_first_admin_alert_is_delivered_on_a_freshly_booted_host(self):
        """``0.0`` is a real ``time.monotonic()`` value, not "never".

        Audit F-2: ``_last_admin_alert`` started at ``0.0`` and was compared
        with the boot-relative monotonic clock, so on a host whose uptime is
        below ``ADMIN_ALERT_INTERVAL_SECONDS`` the first — and most important —
        alert about a broken channel configuration was silently swallowed.
        """
        await self.use_resolver(SubscriptionApiError(403, "bot is not an administrator"))
        notify = AsyncMock()
        self.bot._notify_admins = notify
        clock = mock.Mock()
        clock.monotonic.return_value = 5.0  # a host booted five seconds ago
        with mock.patch("gamas_bot.subscription.time", clock):
            event = FakeJobEvent(chat_id=USER_ID)
            self.assertTrue(await self.bot.subscription.require_access(event))
        self.assertEqual(notify.await_count, 1)

    async def test_admin_alerts_stay_throttled_within_the_interval(self):
        notify = AsyncMock()
        self.bot._notify_admins = notify
        clock = mock.Mock()
        clock.monotonic.side_effect = [5.0, 6.0, 400.0]
        with mock.patch("gamas_bot.subscription.time", clock):
            await self.bot.subscription._alert_admins("first")
            await self.bot.subscription._alert_admins("second")
            await self.bot.subscription._alert_admins("third")
        self.assertEqual(notify.await_count, 2)

    async def test_disabled_gate_allows(self):
        await self.use_resolver("left")
        self.bot.subscription.settings = replace(
            self.bot.subscription.settings, subscription_required=False
        )
        event = FakeJobEvent(chat_id=USER_ID)
        self.assertTrue(await self.bot.subscription.require_access(event))
        self.assertFalse(any(JOIN_PROMPT_MARKER in text for text in event.replies))

    async def test_unconfigured_gate_allows(self):
        await self.use_resolver("left")
        self.bot.subscription.settings = replace(
            self.bot.subscription.settings, subscription_channel=""
        )
        event = FakeJobEvent(chat_id=USER_ID)
        self.assertTrue(await self.bot.subscription.require_access(event))
        self.assertFalse(any(JOIN_PROMPT_MARKER in text for text in event.replies))


class InflightSharingTests(SubscriptionTestCase):
    """Cancellation safety of the one-request-per-user sharing.

    Audit F-1: ``check_membership`` shared a bare ``asyncio.Future`` between the
    owner of the request and every concurrent waiter. Two defects followed:
    a cancelled owner never resolved the future, so a waiter awaited it forever,
    and a cancelled waiter cancelled the future its task was awaiting, which
    killed the verification for the owner and for unrelated co-waiters.
    """

    def _blocking_resolver(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def resolver(_user_id):
            started.set()
            await release.wait()
            return "member"

        self.bot.subscription._resolver = resolver
        return started, release

    async def test_cancelled_owner_releases_waiters_without_hanging(self):
        started, release = self._blocking_resolver()
        owner = asyncio.create_task(self.bot.subscription.check_membership(USER_ID))
        await started.wait()
        waiter = asyncio.create_task(self.bot.subscription.check_membership(USER_ID))
        await asyncio.sleep(0.05)
        self.assertIn(USER_ID, self.bot.subscription._inflight)

        owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owner
        # The waiter must finish instead of waiting on a future nobody resolves.
        result = await asyncio.wait_for(waiter, 2.0)
        self.assertEqual(result.state, STATE_UNKNOWN)
        self.assertEqual(result.detail, "cancelled")
        self.assertFalse(result.is_denied, "a cancelled check is never a denial")
        release.set()

    async def test_cancelled_waiter_does_not_cancel_the_shared_check(self):
        started, release = self._blocking_resolver()
        owner = asyncio.create_task(self.bot.subscription.check_membership(USER_ID))
        await started.wait()
        first = asyncio.create_task(self.bot.subscription.check_membership(USER_ID))
        await asyncio.sleep(0.02)
        second = asyncio.create_task(self.bot.subscription.check_membership(USER_ID))
        await asyncio.sleep(0.02)

        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        shared = self.bot.subscription._inflight.get(USER_ID)
        self.assertIsNotNone(shared)
        self.assertFalse(shared.cancelled(), "a waiter cancelled the shared request")

        release.set()
        owner_result = await asyncio.wait_for(owner, 2.0)
        second_result = await asyncio.wait_for(second, 2.0)
        self.assertEqual(owner_result.state, STATE_MEMBER)
        self.assertEqual(second_result.state, STATE_MEMBER)

    async def test_waiter_gets_the_same_result_as_the_owner(self):
        started, release = self._blocking_resolver()
        calls: list[int] = []

        async def counting_resolver(user_id):
            calls.append(user_id)
            started.set()
            await release.wait()
            return "member"

        self.bot.subscription._resolver = counting_resolver
        owner = asyncio.create_task(self.bot.subscription.check_membership(USER_ID))
        await started.wait()
        waiter = asyncio.create_task(self.bot.subscription.check_membership(USER_ID))
        await asyncio.sleep(0.02)
        release.set()
        self.assertEqual((await asyncio.wait_for(owner, 2.0)).state, STATE_MEMBER)
        self.assertEqual((await asyncio.wait_for(waiter, 2.0)).state, STATE_MEMBER)
        self.assertEqual(calls, [USER_ID], "concurrent checks must share one request")


class CallbackHandlerTests(SubscriptionTestCase):
    async def test_check_callback_for_non_member_prompts(self):
        await self.use_resolver("left")
        event = CallbackEvent(USER_ID, b"sub:check")
        await self.bot.subscription.handle_callback(event, USER_ID, SUBSCRIPTION_PREFIX + "check")
        texts = [text for text, _ in event.edits] + [text for text, _ in event.responses]
        self.assertTrue(any(JOIN_PROMPT_MARKER in text for text in texts))

    async def test_check_callback_for_member_confirms(self):
        await self.use_resolver("member")
        event = CallbackEvent(USER_ID, b"sub:check")
        await self.bot.subscription.handle_callback(event, USER_ID, SUBSCRIPTION_PREFIX + "check")
        texts = [text for text, _ in event.edits] + [text for text, _ in event.responses]
        self.assertTrue(any(CONFIRMED_MARKER in text for text in texts))

    async def test_check_callback_for_unknown_reports_unavailable(self):
        await self.use_resolver(SubscriptionApiError(0, "down"))
        event = CallbackEvent(USER_ID, b"sub:check")
        await self.bot.subscription.handle_callback(event, USER_ID, SUBSCRIPTION_PREFIX + "check")
        texts = [text for text, _ in event.edits] + [text for text, _ in event.responses]
        self.assertTrue(any(UNAVAILABLE_MARKER in text for text in texts))

    async def test_other_callback_prefix_is_ignored(self):
        event = CallbackEvent(USER_ID, b"something:else")
        await self.bot.subscription.handle_callback(event, USER_ID, "something:else")
        self.assertEqual(event.edits, [])
        self.assertEqual(event.responses, [])


class MessageGateIntegrationTests(SubscriptionTestCase):
    async def test_message_gate_blocks_non_member(self):
        await self.use_resolver("left")
        event = FakeJobEvent(chat_id=USER_ID)
        event.message.message = "hello"
        await self.bot._handle_message(event)
        self.assertTrue(any(JOIN_PROMPT_MARKER in text for text in event.replies))

    async def test_message_gate_allows_member(self):
        await self.use_resolver("member")
        event = FakeJobEvent(chat_id=USER_ID)
        event.message.message = "hello"
        await self.bot._handle_message(event)
        self.assertFalse(any(JOIN_PROMPT_MARKER in text for text in event.replies))

    async def test_message_gate_allows_admin(self):
        await self.use_resolver("left")
        self.bot.subscription.settings = replace(
            self.bot.subscription.settings, admin_ids={USER_ID}
        )
        event = FakeJobEvent(chat_id=USER_ID)
        event.message.message = "hello"
        await self.bot._handle_message(event)
        self.assertFalse(any(JOIN_PROMPT_MARKER in text for text in event.replies))


class CallbackGateIntegrationTests(SubscriptionTestCase):
    async def test_subscription_callback_is_routed_before_the_gate(self):
        await self.use_resolver("left")
        event = CallbackEvent(USER_ID, b"sub:check")
        handler = AsyncMock()
        self.bot.subscription.handle_callback = handler
        await self.bot._dispatch_callback(event)
        handler.assert_awaited_once_with(event, USER_ID, SUBSCRIPTION_PREFIX + "check")
        # The gate must not prompt; the subscription handler owns the reply.
        self.assertEqual(event.edits, [])

    async def test_other_callback_is_gated(self):
        await self.use_resolver("left")
        event = CallbackEvent(USER_ID, b"menu:main")
        await self.bot._dispatch_callback(event)
        self.assertTrue(any(JOIN_PROMPT_MARKER in text for text, _ in event.edits))


class MigrationTests(SubscriptionTestCase):
    async def test_set_subscription_state_round_trip(self):
        await self.db.ensure_user(USER_ID)
        await self.db.set_subscription_state(
            USER_ID, "member", verified_at="2026-10-30T00:00:00+00:00"
        )
        identity = await self.db.user_identity(USER_ID)
        self.assertEqual(identity["subscription_status"], "member")
        self.assertEqual(identity["subscription_verified_at"], "2026-10-30T00:00:00+00:00")

        await self.db.set_subscription_state(USER_ID, "left")
        identity = await self.db.user_identity(USER_ID)
        self.assertEqual(identity["subscription_status"], "left")
        self.assertIsNone(identity["subscription_verified_at"])

    async def test_identities_for_exposes_subscription_columns(self):
        await self.db.ensure_user(USER_ID)
        await self.db.set_subscription_state(USER_ID, "member")
        identities = await self.db.identities_for([USER_ID])
        self.assertIn("subscription_status", identities[USER_ID])
        self.assertIn("subscription_verified_at", identities[USER_ID])


class ConfigTests(TestCase):
    def test_from_env_reads_subscription_keys(self):
        with mock.patch.dict(
            os.environ,
            {
                "SUBSCRIPTION_CHANNEL": "-100123",
                "SUBSCRIPTION_REQUIRED": "false",
                "SUBSCRIPTION_CACHE_SECONDS": "45",
                "SUBSCRIPTION_TIMEOUT_SECONDS": "9",
                "TELEGRAM_API_BASE_URL": "https://example.test",
            },
            clear=True,
        ):
            settings = from_env(env_file="/nonexistent")
        self.assertEqual(settings.subscription_channel, "-100123")
        self.assertFalse(settings.subscription_required)
        self.assertEqual(settings.subscription_cache_seconds, 45.0)
        self.assertEqual(settings.subscription_timeout_seconds, 9.0)
        self.assertEqual(settings.telegram_api_base_url, "https://example.test")

    def test_channel_url_for_public_channel(self):
        with mock.patch.dict(os.environ, {"SUBSCRIPTION_CHANNEL": "@gamas_AI"}, clear=True):
            settings = from_env(env_file="/nonexistent")
        self.assertEqual(settings.subscription_channel_url, "https://t.me/gamas_AI")

    def test_channel_url_for_numeric_channel(self):
        with mock.patch.dict(os.environ, {"SUBSCRIPTION_CHANNEL": "-1001234567890"}, clear=True):
            settings = from_env(env_file="/nonexistent")
        self.assertEqual(settings.subscription_channel_url, "https://t.me/c/1001234567890")

    def test_check_accepts_valid_channel(self):
        with mock.patch.dict(
            os.environ,
            {"SUBSCRIPTION_REQUIRED": "true", "SUBSCRIPTION_CHANNEL": "@gamas_AI"},
            clear=True,
        ):
            settings = from_env(env_file="/nonexistent")
        warnings = check(settings)
        self.assertFalse(any("SUBSCRIPTION_CHANNEL" in warning for warning in warnings))

    def test_check_warns_when_required_but_unconfigured(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = from_env(env_file="/nonexistent")
        settings = replace(settings, subscription_channel="")
        warnings = check(settings)
        self.assertTrue(any("SUBSCRIPTION_CHANNEL" in warning for warning in warnings))

    def test_check_warns_on_invalid_channel(self):
        with mock.patch.dict(
            os.environ,
            {"SUBSCRIPTION_REQUIRED": "true", "SUBSCRIPTION_CHANNEL": "bad channel!"},
            clear=True,
        ):
            settings = from_env(env_file="/nonexistent")
        warnings = check(settings)
        self.assertTrue(any("SUBSCRIPTION_CHANNEL" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
