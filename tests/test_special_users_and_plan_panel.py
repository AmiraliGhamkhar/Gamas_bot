"""Special (unlimited) users and the administrator plan panel.

These tests pin the two new administrator contracts:

* a special user keeps using the bot without their credit ever being touched —
  the database still records the processed media for the audit trail, and the
  flag itself is always granted with a reason and an audit entry;
* the plan panel can create, edit, disable and delete plans, and an
  administrator decision survives the canonical catalogue sync that runs on
  every start. Plans stay purchasable through the ordinary receipt flow.

Everything runs offline against a real SQLite database and the real handlers.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from support import make_settings, wav_bytes

from gamas_bot.billing import (
    FREE_PLAN_SECONDS,
    PLAN_5_SECONDS,
    canonical_plan_records,
    plan_catalog,
)
from gamas_bot.bot import StudyBot, _plan_edit_prompt
from gamas_bot.database import UNLIMITED_USAGE_REASON, Database


class _MessageEvent:
    """Admin-message double: records replies and knows who sent them."""

    def __init__(self, sender_id: int = 7, text: str = ""):
        self._sender_id = int(sender_id)
        self.raw_text = text
        self.is_private = True
        self.replies: list[str] = []
        self.responses: list[str] = []
        self.message = SimpleNamespace(media=None)

    async def get_sender(self):
        return SimpleNamespace(id=self._sender_id, username="owner", bot=False)

    async def reply(self, text: str = "", **_kwargs):
        self.replies.append(text)

    async def respond(self, text: str = "", **_kwargs):
        self.responses.append(text)


class _CallbackEvent(_MessageEvent):
    """Inline-button callback double driven by raw callback data."""

    def __init__(self, data: bytes, sender_id: int = 7):
        super().__init__(sender_id)
        self.data = data
        self.answers: list[tuple[str, bool]] = []
        self.edits: list[str] = []

    async def answer(self, text: str = "", alert: bool = False):
        self.answers.append((text, alert))

    async def edit(self, text: str, buttons=None, parse_mode=None):
        self.edits.append(text)

    @property
    def last_text(self) -> str:
        if self.edits:
            return self.edits[-1]
        if self.responses:
            return self.responses[-1]
        return ""


class _Harness(unittest.IsolatedAsyncioTestCase):
    """A real database, the real handlers, and an admin id of 7."""

    admin_id = 7

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(
            database_path=self.root / "panel.sqlite3",
            session_path=self.root / "session",
            temp_dir=self.root / "tmp",
            admin_ids=frozenset({self.admin_id}),
        )
        self.bot = StudyBot.__new__(StudyBot)
        self.bot.settings = self.settings
        self.bot.db = Database(self.settings.database_path)
        await self.bot.db.open()
        await self.bot.db.sync_plan_catalog(canonical_plan_records())
        self.bot._pending_admin_actions = {}
        self.bot._pending_credential_setup = {}
        self.bot.provider_health = SimpleNamespace(invalidate=lambda: None)
        self.bot.client = SimpleNamespace(
            send_message=self._record_message, send_file=self._record_message
        )
        self.sent_messages: list[tuple[int, str]] = []
        self.user = await self.bot.db.upsert_user(101, "student")

    async def _record_message(self, *args, **_kwargs):
        self.sent_messages.append((int(args[0]), str(args[1])))
        return SimpleNamespace(id=1)

    async def asyncTearDown(self):
        await self.bot.db.close()
        self.temp.cleanup()

    async def _new_submission(self, user_id: int, seconds: int, name: str = "clip.wav") -> int:
        return await self.bot.db.create_submission(
            user_id, name, seconds, name, "audio/wav"
        )


class SpecialUserDatabaseTests(_Harness):
    async def test_flag_requires_a_reason_and_is_audited_once(self):
        user_id = int(self.user["id"])
        with self.assertRaises(ValueError):
            await self.bot.db.set_user_unlimited(101, True, self.admin_id, "   ")
        self.assertFalse(await self.bot.db.is_unlimited_user(user_id))
        result = await self.bot.db.set_user_unlimited(101, True, self.admin_id, "همکار پشتیبانی")
        self.assertTrue(result["changed"])
        self.assertTrue(await self.bot.db.is_unlimited_user(user_id))
        # Asking again is a no-op, not a second grant.
        again = await self.bot.db.set_user_unlimited(101, True, self.admin_id, "تکرار")
        self.assertFalse(again["changed"])
        self.assertIsNone(
            await self.bot.db.set_user_unlimited(999, True, self.admin_id, "ناشناس")
        )
        actions = [row["action"] for row in await self.bot.db.admin_audit(limit=20)]
        self.assertEqual(actions.count("special_user_granted"), 1)

    async def test_special_media_is_recorded_but_never_charged(self):
        user_id = int(self.user["id"])
        await self.bot.db.set_user_unlimited(101, True, self.admin_id, "پشتیبانی")
        before = await self.bot.db.user_balance(user_id)
        submission = await self._new_submission(user_id, 10_000)
        result = await self.bot.db.reserve_usage(user_id, submission, 10_000)
        self.assertTrue(result["ok"])
        self.assertTrue(result["unlimited"])
        after = await self.bot.db.user_balance(user_id)
        self.assertEqual(after["available_seconds"], before["available_seconds"])
        reservation = await self.bot.db.usage_reservation(submission)
        self.assertEqual(reservation["status"], "consumed")
        self.assertEqual(reservation["consumed_seconds"], 10_000)
        self.assertEqual(reservation["reserved_seconds"], 0)
        ledger = await self.bot.db.usage_ledger(user_id)
        self.assertNotIn("reserve", [row["event_type"] for row in ledger])
        consume = [row for row in ledger if row["event_type"] == "consume"]
        self.assertEqual(len(consume), 1)
        self.assertEqual(consume[0]["consumed_seconds"], 10_000)
        self.assertIsNone(consume[0]["entitlement_id"])
        # Nothing is reserved, so nothing can be finalized or refunded twice.
        self.assertFalse(await self.bot.db.finalize_usage(submission, 10_000))
        self.assertEqual(await self.bot.db.release_usage(submission, "cleanup"), 0)
        self.assertEqual(
            (await self.bot.db.user_balance(user_id))["available_seconds"],
            before["available_seconds"],
        )

    async def test_special_reservation_is_idempotent_and_never_blocks(self):
        user_id = int(self.user["id"])
        # Spend the whole free hour first: a normal user would be denied now.
        used = await self._new_submission(user_id, FREE_PLAN_SECONDS)
        self.assertTrue(
            (await self.bot.db.reserve_usage(user_id, used, FREE_PLAN_SECONDS))["ok"]
        )
        self.assertTrue(await self.bot.db.finalize_usage(used, FREE_PLAN_SECONDS))
        self.assertEqual(
            (await self.bot.db.user_balance(user_id))["available_seconds"], 0
        )
        await self.bot.db.set_user_unlimited(101, True, self.admin_id, "پشتیبانی")
        submission = await self._new_submission(user_id, 8_000)
        first = await self.bot.db.reserve_usage(user_id, submission, 8_000)
        second = await self.bot.db.reserve_usage(user_id, submission, 8_000)
        self.assertTrue(first["ok"] and second["ok"])
        self.assertTrue(second["existing"])
        self.assertEqual(first["reservation_id"], second["reservation_id"])
        self.assertEqual(
            (await self.bot.db.user_balance(user_id))["available_seconds"], 0
        )

    async def test_revoked_user_is_billed_again(self):
        user_id = int(self.user["id"])
        await self.bot.db.set_user_unlimited(101, True, self.admin_id, "پشتیبانی")
        revoked = await self.bot.db.set_user_unlimited(
            101, False, self.admin_id, "پایان همکاری"
        )
        self.assertTrue(revoked["changed"])
        self.assertFalse(await self.bot.db.is_unlimited_user(user_id))
        submission = await self._new_submission(user_id, 10_000)
        result = await self.bot.db.reserve_usage(user_id, submission, 10_000)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "insufficient")
        self.assertEqual(
            (await self.bot.db.user_balance(user_id))["available_seconds"],
            FREE_PLAN_SECONDS,
        )
        actions = [row["action"] for row in await self.bot.db.admin_audit(limit=20)]
        self.assertIn("special_user_revoked", actions)

    async def test_unlimited_listing_reports_the_media_it_processed(self):
        await self.bot.db.set_user_unlimited(101, True, self.admin_id, "پشتیبانی")
        user_id = int(self.user["id"])
        for seconds in (100, 200):
            submission = await self._new_submission(user_id, seconds)
            await self.bot.db.reserve_usage(user_id, submission, seconds)
        listed = await self.bot.db.unlimited_users()
        self.assertEqual([row["telegram_id"] for row in listed], [101])
        self.assertEqual(listed[0]["unbilled_jobs"], 2)
        self.assertEqual(listed[0]["unlimited_reason"], "پشتیبانی")
        self.assertIsNotNone(listed[0]["unlimited_granted_at"])
        overview = await self.bot.db.admin_user_credit_overview(101)
        self.assertTrue(overview["is_unlimited"])
        self.assertEqual(overview["unlimited_reason"], "پشتیبانی")
        await self.bot.db.set_user_unlimited(101, False, self.admin_id, "حذف")
        self.assertEqual(await self.bot.db.unlimited_users(), [])

    async def test_user_summaries_and_stats_expose_the_special_flag(self):
        await self.bot.db.set_user_unlimited(101, True, self.admin_id, "پشتیبانی")
        summaries = await self.bot.db.user_summaries(limit=10)
        self.assertTrue(summaries[0]["is_unlimited"])
        self.assertEqual((await self.bot.db.stats())["unlimited_users"], 1)


class PlanPanelDatabaseTests(_Harness):
    async def test_admin_plan_is_purchasable_end_to_end(self):
        plan = await self.bot.db.create_plan(
            code="promo_2h",
            name="۲ ساعت ویژه",
            hours=2,
            price_toman=20_000,
            validity_days=30,
            admin_id=self.admin_id,
        )
        self.assertTrue(plan["is_custom"])
        self.assertEqual(plan["included_seconds"], 7_200)
        user_id = int(self.user["id"])
        request = await self.bot.db.create_payment_request(user_id, "promo_2h")
        self.assertEqual(request["amount_toman"], 20_000)
        accepted = await self.bot.db.submit_payment_receipt(
            int(request["id"]), user_id, str(self.root / "r.jpg"), 11
        )
        self.assertTrue(accepted)
        approval = await self.bot.db.approve_payment(int(request["id"]), self.admin_id)
        self.assertEqual(approval["granted_seconds"], 7_200)
        balance = await self.bot.db.user_balance(user_id)
        self.assertEqual(
            balance["available_seconds"], FREE_PLAN_SECONDS + 7_200
        )
        paid = next(row for row in balance["entitlements"] if row["source"] == "payment")
        self.assertEqual(paid["plan_code"], "promo_2h")
        self.assertIsNotNone(paid["expires_at"])

    async def test_lifetime_plan_grants_an_entitlement_without_expiry(self):
        await self.bot.db.create_plan(
            code="lifetime_promo",
            name="هدیهٔ بدون انقضا",
            hours=1,
            price_toman=10_000,
            validity_days=None,
            admin_id=self.admin_id,
        )
        user_id = int(self.user["id"])
        request = await self.bot.db.create_payment_request(user_id, "lifetime_promo")
        await self.bot.db.submit_payment_receipt(
            int(request["id"]), user_id, str(self.root / "r.jpg"), 12
        )
        await self.bot.db.approve_payment(int(request["id"]), self.admin_id)
        balance = await self.bot.db.user_balance(user_id)
        paid = next(row for row in balance["entitlements"] if row["source"] == "payment")
        self.assertIsNone(paid["expires_at"])
        self.assertEqual(paid["remaining_seconds"], 3_600)

    async def test_disabling_a_plan_hides_it_from_buyers_and_blocks_requests(self):
        plan = await self.bot.db.create_plan(
            code="promo_2h",
            name="۲ ساعت ویژه",
            hours=2,
            price_toman=20_000,
            validity_days=30,
            admin_id=self.admin_id,
        )
        self.assertTrue(
            await self.bot.db.set_plan_enabled(int(plan["id"]), False, self.admin_id)
        )
        purchasable = [
            row["code"] for row in await self.bot.db.list_plans(paid_only=True)
        ]
        self.assertNotIn("promo_2h", purchasable)
        with self.assertRaises(ValueError):
            await self.bot.db.create_payment_request(int(self.user["id"]), "promo_2h")
        # The row is kept for the panel and for its history.
        everything = {
            row["code"]: row for row in await self.bot.db.list_plans(include_disabled=True)
        }
        self.assertIn("promo_2h", everything)
        self.assertEqual(everything["promo_2h"]["enabled"], 0)
        self.assertTrue(
            await self.bot.db.set_plan_enabled(int(plan["id"]), True, self.admin_id)
        )
        self.assertIn(
            "promo_2h",
            [row["code"] for row in await self.bot.db.list_plans(paid_only=True)],
        )

    async def test_administrator_decisions_survive_the_catalogue_sync(self):
        # A disabled canonical plan must stay disabled across a restart.
        stored = {row["code"]: row for row in await self.bot.db.list_plans(include_disabled=True)}
        five_hour_id = int(stored["paid_5h_30d"]["id"])
        await self.bot.db.set_plan_enabled(five_hour_id, False, self.admin_id)
        # An edited canonical plan keeps the administrator's price.
        await self.bot.db.update_plan(
            int(stored["paid_25h_30d"]["id"]), {"price_toman": 120_000}, self.admin_id
        )
        custom = await self.bot.db.create_plan(
            code="promo_2h",
            name="۲ ساعت ویژه",
            hours=2,
            price_toman=20_000,
            validity_days=30,
            admin_id=self.admin_id,
        )
        await self.bot.db.sync_plan_catalog(canonical_plan_records())
        after = {
            row["code"]: row for row in await self.bot.db.list_plans(include_disabled=True)
        }
        self.assertEqual(after["paid_5h_30d"]["enabled"], 0)
        self.assertEqual(after["paid_5h_30d"]["price_toman"], 50_000)
        self.assertEqual(after["paid_25h_30d"]["price_toman"], 120_000)
        self.assertEqual(after["paid_25h_30d"]["is_custom"], 1)
        self.assertEqual(after["promo_2h"]["name"], "۲ ساعت ویژه")
        self.assertEqual(after["promo_2h"]["included_seconds"], 7_200)
        self.assertEqual(after["promo_2h"]["id"], custom["id"])
        # Untouched canonical plans still follow the configuration.
        self.assertEqual(after["paid_20h_30d"]["price_toman"], 130_000)
        self.assertEqual(after["paid_20h_30d"]["is_custom"], 0)

    async def test_free_and_default_plans_are_protected(self):
        stored = {row["code"]: row for row in await self.bot.db.list_plans(include_disabled=True)}
        free_id = int(stored["free_1h"]["id"])
        with self.assertRaises(ValueError):
            await self.bot.db.set_plan_enabled(free_id, False, self.admin_id)
        with self.assertRaises(ValueError):
            await self.bot.db.delete_plan(free_id, self.admin_id)
        with self.assertRaises(ValueError):
            await self.bot.db.update_plan(free_id, {"price_toman": 1_000}, self.admin_id)
        with self.assertRaises(ValueError):
            await self.bot.db.delete_plan(int(stored["paid_50h_30d"]["id"]), self.admin_id)
        # The free plan keeps working for brand-new users.
        newcomer = await self.bot.db.upsert_user(202, "newcomer")
        balance = await self.bot.db.user_balance(int(newcomer["id"]))
        self.assertEqual(balance["available_seconds"], FREE_PLAN_SECONDS)

    async def test_a_plan_with_history_is_disabled_not_deleted(self):
        plan = await self.bot.db.create_plan(
            code="promo_2h",
            name="۲ ساعت ویژه",
            hours=2,
            price_toman=20_000,
            validity_days=30,
            admin_id=self.admin_id,
        )
        request = await self.bot.db.create_payment_request(int(self.user["id"]), "promo_2h")
        await self.bot.db.submit_payment_receipt(
            int(request["id"]), int(self.user["id"]), str(self.root / "r.jpg"), 13
        )
        await self.bot.db.approve_payment(int(request["id"]), self.admin_id)
        with self.assertRaises(ValueError):
            await self.bot.db.delete_plan(int(plan["id"]), self.admin_id)
        # An unreferenced custom plan can be removed.
        harmless = await self.bot.db.create_plan(
            code="promo_3h",
            name="۳ ساعت ویژه",
            hours=3,
            price_toman=30_000,
            validity_days=30,
            admin_id=self.admin_id,
        )
        self.assertTrue(await self.bot.db.delete_plan(int(harmless["id"]), self.admin_id))
        self.assertIsNone(await self.bot.db.get_plan(int(harmless["id"])))

    async def test_create_plan_validates_every_field(self):
        base = {
            "code": "promo_2h",
            "name": "۲ ساعت ویژه",
            "hours": 2,
            "price_toman": 20_000,
            "validity_days": 30,
            "admin_id": self.admin_id,
        }
        created = await self.bot.db.create_plan(**base)
        self.assertIsNotNone(created["id"])
        for overrides, message in (
            ({"code": "Promo 2H"}, "کد"),
            ({"code": "promo_2h"}, "کد"),
            ({"name": "   "}, "نام"),
            ({"hours": 0}, "ساعت"),
            ({"hours": 5_000}, "ساعت"),
            ({"price_toman": 0}, "قیمت"),
            ({"validity_days": 0}, "اعتبار"),
            ({"validity_days": 4_000}, "اعتبار"),
        ):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(ValueError, message):
                await self.bot.db.create_plan(**{**base, **overrides})

    async def test_update_plan_validates_and_takes_ownership(self):
        plan = await self.bot.db.create_plan(
            code="promo_2h",
            name="۲ ساعت ویژه",
            hours=2,
            price_toman=20_000,
            validity_days=30,
            admin_id=self.admin_id,
        )
        with self.assertRaises(ValueError):
            await self.bot.db.update_plan(int(plan["id"]), {}, self.admin_id)
        with self.assertRaises(ValueError):
            await self.bot.db.update_plan(int(plan["id"]), {"price_toman": 0}, self.admin_id)
        updated = await self.bot.db.update_plan(
            int(plan["id"]),
            {"price_toman": 25_000, "validity_days": None, "name": "ویژهٔ جدید"},
            self.admin_id,
        )
        self.assertEqual(updated["price_toman"], 25_000)
        self.assertIsNone(updated["validity_days"])
        self.assertEqual(updated["name"], "ویژهٔ جدید")
        self.assertEqual(updated["included_seconds"], 7_200)
        self.assertIsNone(await self.bot.db.update_plan(9_999, {"price_toman": 1}, self.admin_id))

    async def test_plan_changes_are_audited(self):
        plan = await self.bot.db.create_plan(
            code="promo_2h",
            name="۲ ساعت ویژه",
            hours=2,
            price_toman=20_000,
            validity_days=30,
            admin_id=self.admin_id,
        )
        await self.bot.db.update_plan(int(plan["id"]), {"price_toman": 30_000}, self.admin_id)
        await self.bot.db.set_plan_enabled(int(plan["id"]), False, self.admin_id)
        await self.bot.db.set_plan_enabled(int(plan["id"]), True, self.admin_id)
        await self.bot.db.delete_plan(int(plan["id"]), self.admin_id)
        actions = [row["action"] for row in await self.bot.db.admin_audit(limit=20)]
        for expected in ("plan_created", "plan_updated", "plan_disabled", "plan_enabled", "plan_deleted"):
            self.assertIn(expected, actions)


class SpecialUserPanelTests(_Harness):
    async def _grant_through_the_panel(self, payload: str) -> _MessageEvent:
        self.bot._pending_admin_actions[self.admin_id] = "special_add"
        event = _MessageEvent(self.admin_id)
        await self.bot._handle_pending_admin_input(event, self.admin_id, "special_add", payload)
        return event

    async def test_the_panel_lists_adds_and_removes_special_users(self):
        empty = _CallbackEvent(b"admin:special")
        await self.bot._handle_callback(empty)
        self.assertIn("هنوز کاربری در این فهرست نیست", empty.last_text)

        prompt = _CallbackEvent(b"admin:special:add")
        await self.bot._handle_callback(prompt)
        self.assertEqual(self.bot._pending_admin_actions[self.admin_id], "special_add")
        self.assertIn("شناسه", prompt.last_text)

        granted = await self._grant_through_the_panel("101 | همکار پشتیبانی")
        self.assertTrue(await self.bot.db.is_unlimited_user(int(self.user["id"])))
        self.assertTrue(any("101" in reply for reply in granted.replies), granted.replies)
        self.assertNotIn(self.admin_id, self.bot._pending_admin_actions)

        listed = _CallbackEvent(b"admin:special")
        await self.bot._handle_callback(listed)
        self.assertIn("101", listed.last_text)
        self.assertIn("همکار پشتیبانی", listed.last_text)

        removed = _CallbackEvent(b"admin:special:remove:101")
        await self.bot._handle_callback(removed)
        self.assertFalse(await self.bot.db.is_unlimited_user(int(self.user["id"])))
        self.assertIn("هنوز کاربری در این فهرست نیست", removed.last_text)

    async def test_a_bad_payload_changes_nothing(self):
        for payload in ("بدون شناسه", "101", "101 |   ", "999999999 | دلیل"):
            with self.subTest(payload=payload):
                event = await self._grant_through_the_panel(payload)
                self.assertTrue(event.replies)
        self.assertFalse(await self.bot.db.is_unlimited_user(int(self.user["id"])))
        self.assertEqual(await self.bot.db.unlimited_users(), [])

    async def test_the_bot_never_bills_a_special_user(self):
        audio = self.root / "lecture.wav"
        audio.write_bytes(wav_bytes(3.0))
        user_id = int(self.user["id"])
        billed = await self.bot._reserve_submission_usage(
            await self._new_submission(user_id, 3), user_id, audio, None
        )
        self.assertEqual(billed, 3)
        await self.bot.db.set_user_unlimited(101, True, self.admin_id, "پشتیبانی")
        free_submission = await self._new_submission(user_id, 3)
        free = await self.bot._reserve_submission_usage(free_submission, user_id, audio, None)
        self.assertIsNone(free)
        balance = await self.bot.db.user_balance(user_id)
        # Only the first (billed) job was charged, and the special job is
        # recorded as unbilled rather than as a reservation.
        self.assertEqual(balance["available_seconds"], FREE_PLAN_SECONDS - 3)
        special_row = await self.bot.db.usage_reservation(free_submission)
        self.assertEqual(special_row["status"], "consumed")
        self.assertEqual(special_row["reason"], UNLIMITED_USAGE_REASON)

    async def test_the_balance_screen_announces_the_special_status(self):
        user_id = int(self.user["id"])
        ordinary = await self.bot._billing_balance_text(user_id)
        self.assertNotIn("نامحدود", ordinary)
        await self.bot.db.set_user_unlimited(101, True, self.admin_id, "پشتیبانی")
        special = await self.bot._billing_balance_text(user_id)
        self.assertIn("کاربر ویژه", special)
        self.assertIn("نامحدود", special)
        credit = self.bot._admin_credit_text(
            await self.bot.db.admin_user_credit_overview(101)
        )
        self.assertIn("نامحدود", credit)

    async def test_unlimited_command_grants_and_revokes(self):
        event = _MessageEvent(self.admin_id, "/unlimited 101 on همکار پشتیبانی")
        await self.bot._handle_admin_command(event, "/unlimited", event.raw_text)
        self.assertTrue(await self.bot.db.is_unlimited_user(int(self.user["id"])))
        event = _MessageEvent(self.admin_id, "/unlimited 101 off پایان همکاری")
        await self.bot._handle_admin_command(event, "/unlimited", event.raw_text)
        self.assertFalse(await self.bot.db.is_unlimited_user(int(self.user["id"])))
        bad = _MessageEvent(self.admin_id, "/unlimited 101")
        await self.bot._handle_admin_command(bad, "/unlimited", bad.raw_text)
        self.assertTrue(any("روش استفاده" in reply for reply in bad.replies))


class AdminPlanPanelTests(_Harness):
    async def _create_through_the_panel(self, payload: str) -> _MessageEvent:
        prompt = _CallbackEvent(b"admin:plan:new")
        await self.bot._handle_callback(prompt)
        self.assertEqual(self.bot._pending_admin_actions[self.admin_id], "plan_new")
        event = _MessageEvent(self.admin_id)
        await self.bot._handle_pending_admin_input(event, self.admin_id, "plan_new", payload)
        return event

    async def test_the_panel_creates_a_plan_from_the_prompt(self):
        event = await self._create_through_the_panel("promo_2h | ۲ ساعت ویژه | 2 | 20000 | 30")
        self.assertTrue(any("promo_2h" in reply for reply in event.replies), event.replies)
        stored = {row["code"]: row for row in await self.bot.db.list_plans()}
        self.assertIn("promo_2h", stored)
        self.assertEqual(stored["promo_2h"]["price_toman"], 20_000)
        self.assertEqual(stored["promo_2h"]["included_seconds"], 7_200)
        self.assertEqual(stored["promo_2h"]["is_custom"], 1)

    async def test_a_lifetime_plan_is_created_with_zero_days(self):
        await self._create_through_the_panel("life_1h | هدیه | 1 | 5000 | 0")
        stored = {row["code"]: row for row in await self.bot.db.list_plans()}
        self.assertIsNone(stored["life_1h"]["validity_days"])

    async def test_the_panel_rejects_malformed_input(self):
        for payload in (
            "promo_2h | ۲ ساعت ویژه | 2 | 20000",
            "2h | ۲ ساعت | 2 | 20000 | 30",
            "promo 2h | ۲ ساعت | 2 | 20000 | 30",
            "promo_2h | ۲ ساعت ویژه | 0 | 20000 | 30",
            "promo_2h | ۲ ساعت ویژه | 2 | 0 | 30",
            "promo_2h | ۲ ساعت ویژه | 2 | 20000 | سی",
        ):
            with self.subTest(payload=payload):
                event = await self._create_through_the_panel(payload)
                self.assertTrue(event.replies)
        self.assertEqual(
            [row["code"] for row in await self.bot.db.list_plans(include_disabled=True)],
            [row["code"] for row in canonical_plan_records()],
        )

    async def test_the_plan_code_is_normalised_to_lowercase(self):
        await self._create_through_the_panel("Promo_2H | ۲ ساعت ویژه | 2 | 20000 | 30")
        stored = {row["code"] for row in await self.bot.db.list_plans()}
        self.assertIn("promo_2h", stored)

    async def test_the_edit_prompt_updates_price_hours_and_validity(self):
        plan = await self.bot.db.create_plan(
            code="promo_2h",
            name="۲ ساعت ویژه",
            hours=2,
            price_toman=20_000,
            validity_days=30,
            admin_id=self.admin_id,
        )
        prompt = _CallbackEvent(f"admin:plan:edit:{plan['id']}".encode())
        await self.bot._handle_callback(prompt)
        self.assertEqual(
            self.bot._pending_admin_actions[self.admin_id], f"plan_edit:{plan['id']}"
        )
        self.assertIn("۲۰,۰۰۰ تومان", prompt.last_text)
        event = _MessageEvent(self.admin_id)
        await self.bot._handle_pending_admin_input(
            event, self.admin_id, f"plan_edit:{plan['id']}", "30000 | 4 | 0 | بستهٔ جامع"
        )
        self.assertTrue(event.replies)
        updated = await self.bot.db.get_plan(int(plan["id"]))
        self.assertEqual(updated["price_toman"], 30_000)
        self.assertEqual(updated["included_seconds"], 14_400)
        self.assertIsNone(updated["validity_days"])
        self.assertEqual(updated["name"], "بستهٔ جامع")

    async def test_edit_without_any_change_is_refused(self):
        plan = await self.bot.db.create_plan(
            code="promo_2h",
            name="۲ ساعت ویژه",
            hours=2,
            price_toman=20_000,
            validity_days=30,
            admin_id=self.admin_id,
        )
        event = _MessageEvent(self.admin_id)
        await self.bot._handle_pending_admin_input(
            event, self.admin_id, f"plan_edit:{plan['id']}", "- | - | - | -"
        )
        self.assertTrue(event.replies)
        unchanged = await self.bot.db.get_plan(int(plan["id"]))
        self.assertEqual(unchanged["price_toman"], 20_000)
        self.assertEqual(unchanged["validity_days"], 30)

    async def test_toggle_and_delete_buttons_drive_the_database(self):
        plan = await self.bot.db.create_plan(
            code="promo_2h",
            name="۲ ساعت ویژه",
            hours=2,
            price_toman=20_000,
            validity_days=30,
            admin_id=self.admin_id,
        )
        listing = _CallbackEvent(b"admin:plans")
        await self.bot._handle_callback(listing)
        self.assertIn("promo_2h", listing.last_text)
        self.assertIn("مدیریت طرح‌های فروش", listing.last_text)

        disabled = _CallbackEvent(f"admin:plan:toggle:{plan['id']}".encode())
        await self.bot._handle_callback(disabled)
        self.assertEqual((await self.bot.db.get_plan(int(plan["id"])))["enabled"], 0)
        self.assertNotIn(
            "promo_2h",
            [row["code"] for row in await self.bot.db.list_plans(paid_only=True)],
        )
        enabled = _CallbackEvent(f"admin:plan:toggle:{plan['id']}".encode())
        await self.bot._handle_callback(enabled)
        self.assertEqual((await self.bot.db.get_plan(int(plan["id"])))["enabled"], 1)

        deleted = _CallbackEvent(f"admin:plan:delete:{plan['id']}".encode())
        await self.bot._handle_callback(deleted)
        self.assertIsNone(await self.bot.db.get_plan(int(plan["id"])))
        self.assertTrue(
            any("حذف شد" in answer for answer, _alert in deleted.answers),
            deleted.answers,
        )
        # The refreshed panel no longer lists the deleted plan.
        self.assertNotIn("promo_2h", deleted.last_text)

    async def test_default_and_free_plans_cannot_be_deleted_from_the_panel(self):
        stored = {row["code"]: row for row in await self.bot.db.list_plans(include_disabled=True)}
        for code in ("free_1h", "paid_50h_30d"):
            event = _CallbackEvent(f"admin:plan:delete:{stored[code]['id']}".encode())
            await self.bot._handle_callback(event)
            self.assertTrue(event.answers)
            self.assertTrue(event.answers[-1][1], "the refusal must be an alert")
        self.assertIsNotNone(await self.bot.db.get_plan(int(stored["paid_50h_30d"]["id"])))

    async def test_the_buyer_screen_formats_a_lifetime_plan_without_none(self):
        await self.bot.db.create_plan(
            code="life_1h",
            name="هدیهٔ بدون انقضا",
            hours=1,
            price_toman=10_000,
            validity_days=None,
            admin_id=self.admin_id,
        )
        text, buttons = await self.bot._billing_plans()
        self.assertNotIn("None", text)
        self.assertIn("بدون انقضا", text)
        self.assertIn("۱۰ ساعت / ۳۰ روز", text)
        self.assertIn("۵۰,۰۰۰ تومان", text)
        flat = [button.text for row in buttons for button in row]
        self.assertTrue(any("هدیهٔ بدون انقضا" in label for label in flat), flat)
        self.assertTrue(any("۵ ساعت / ۳۰ روز" in label for label in flat), flat)


class CatalogueContractTests(unittest.IsolatedAsyncioTestCase):
    def test_the_purchasable_catalogue_has_the_five_promised_plans(self):
        records = {plan.code: plan for plan in plan_catalog()}
        self.assertEqual(
            [
                (records[code].name, records[code].included_seconds, records[code].price_toman)
                for code in ("paid_5h_30d", "paid_10h_30d", "paid_20h_30d")
            ],
            [
                ("۵ ساعت / ۳۰ روز", PLAN_5_SECONDS, 50_000),
                ("۱۰ ساعت / ۳۰ روز", 36_000, 75_000),
                ("۲۰ ساعت / ۳۰ روز", 72_000, 130_000),
            ],
        )

    async def test_plan_edit_prompt_shows_the_current_tariff(self):
        plan = {
            "code": "promo_2h",
            "name": "۲ ساعت ویژه",
            "included_seconds": 7_200,
            "price_toman": 20_000,
            "validity_days": None,
        }
        prompt = _plan_edit_prompt(plan)
        self.assertIn("promo_2h", prompt)
        self.assertIn("بدون انقضا", prompt)
        self.assertIn("۲۰,۰۰۰ تومان", prompt)
