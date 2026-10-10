"""Admin/user panel presentation contracts (pagination, labels, acknowledgements).

The admin panels are the only way an operator can see what the platform is
doing, so their failure modes matter as much as the router's:

* a long list must be *paginated*, never silently cut — and paging must not
  trigger a provider request, must keep the provider/task context, and must
  survive a stale or malformed callback;
* availability, deprecation and billing state must stay visible on every page,
  and an unknown free-status must never be rendered as "free";
* every reason a provider was skipped must be readable in Persian *and* keep
  its machine code;
* an expensive read-only tap is acknowledged before the database work and
  answered exactly once.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

from gamas_bot import admin_ai as admin_ai_module
from gamas_bot.admin_ai import AIPanels, SERVICE
from gamas_bot.admin_stt import SttPanels
from gamas_bot.ai.models import ModelRegistry
from gamas_bot.ai.registry import PROVIDER_REGISTRY
from gamas_bot.ai.routing import ProviderRouter
from gamas_bot.ai.usage import AIUsageTracker
from gamas_bot.bot import StudyBot
from gamas_bot.database import Database
from gamas_bot.provider_credentials import ProviderCredentialManager
from gamas_bot.provider_health import ProviderHealthChecker
from gamas_bot.stt_platform.models import STTModelInfo
from gamas_bot.stt_platform.registry import STT_PROVIDER_REGISTRY

from support import make_settings


def _button_data(button) -> bytes:
    """Callback payload of a Telethon inline button (any 1.x layout)."""
    data = getattr(button, "data", None)
    if data:
        return bytes(data)
    inner = getattr(button, "type", None)
    data = getattr(inner, "data", None)
    return bytes(data) if data else b""


def _callback_event(telegram_id: int, data: str):
    return SimpleNamespace(
        is_private=True,
        data=data.encode("utf-8"),
        get_sender=AsyncMock(
            return_value=SimpleNamespace(id=telegram_id, username="admin", bot=False)
        ),
        answer=AsyncMock(),
        edit=AsyncMock(),
        reply=AsyncMock(),
        respond=AsyncMock(),
        message=SimpleNamespace(delete=AsyncMock(), media=None),
    )


class PanelCase(unittest.IsolatedAsyncioTestCase):
    """A StudyBot wired to a real (temporary) database and the real router."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = Database(root / "panels.sqlite3")
        await self.db.open()
        self.settings = make_settings(
            database_path=root / "panels.sqlite3",
            session_path=root / "session",
            temp_dir=root / "tmp",
            admin_ids=frozenset({7}),
            provider_credentials_encryption_key=Fernet.generate_key().decode("ascii"),
        )
        self.bot = StudyBot.__new__(StudyBot)
        self.bot.settings = self.settings
        self.bot.db = self.db
        self.bot.credential_manager = ProviderCredentialManager(self.db, self.settings)
        self.bot._pending_admin_actions = {}
        self.bot._pending_credential_setup = {}
        self.bot.ai_tracker = AIUsageTracker(self.db)
        self.bot.model_registry = ModelRegistry(self.db, self.settings)
        self.bot.provider_router = ProviderRouter(
            self.db,
            self.settings,
            self.bot.credential_manager,
            self.bot.ai_tracker,
            self.bot.model_registry,
        )
        self.bot.provider_health = ProviderHealthChecker(
            self.db, self.settings, self.bot.credential_manager
        )
        self.bot.ai_panels = AIPanels(self.bot)
        self.bot.stt_panels = SttPanels(self.bot)
        self.bot._edit_callback = AsyncMock()

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def _callback(self, data: str):
        event = _callback_event(7, data)
        await self.bot._handle_callback(event)
        return event

    @property
    def last_call(self):
        return self.bot._edit_callback.call_args

    @property
    def last_text(self) -> str:
        call = self.last_call
        return str(call.args[1] if call and len(call.args) > 1 else "")

    @property
    def last_buttons(self) -> list:
        call = self.last_call
        return list(call.args[2] if call and len(call.args) > 2 else [])

    def flat_buttons(self) -> list[tuple[str, bytes]]:
        return [
            (str(button), _button_data(button))
            for row in self.last_buttons
            for button in row
        ]

    def payloads(self) -> list[bytes]:
        return [payload for _label, payload in self.flat_buttons()]


class CatalogPaginationTests(PanelCase):
    async def _seed_catalog(self, provider: str = "gemini", count: int = 25) -> None:
        rows = [
            {
                "model": f"model-{index:02d}",
                "free_status": "free_permanent" if index % 2 else "unknown",
                "deprecated": 1 if index == 24 else 0,
                "deprecation_date": "2026-09-01" if index == 24 else None,
            }
            for index in range(count)
        ]
        await self.db.ai_models_upsert_discovery(provider, rows)

    async def test_pages_walk_the_whole_catalog(self):
        await self._seed_catalog()
        await self._callback("admin:ai:md:gemini:0")
        first = self.last_text
        self.assertIn("model-00", first)
        self.assertIn("model-09", first)
        self.assertNotIn("model-10", first)
        self.assertIn("صفحهٔ 1 از 3", first)
        await self._callback("admin:ai:md:gemini:2")
        last = self.last_text
        self.assertIn("model-24", last)
        self.assertIn("صفحهٔ 3 از 3", last)
        self.assertNotIn("model-00", last)

    async def test_short_catalog_is_not_labeled_as_paged(self):
        await self._seed_catalog(count=3)
        await self._callback("admin:ai:md:gemini:0")
        self.assertNotIn("صفحهٔ", self.last_text)
        self.assertIn("model-02", self.last_text)

    async def test_empty_catalog_offers_the_sync_instead_of_an_error(self):
        await self._callback("admin:ai:md:gemini:0")
        self.assertIn("هنوز مدلی ثبت نشده", self.last_text)
        self.assertTrue(any(b"mdsync:gemini" in payload for payload in self.payloads()))

    async def test_malformed_or_stale_page_tokens_degrade_safely(self):
        await self._seed_catalog()
        await self._callback("admin:ai:md:gemini:not-a-number")
        self.assertIn("model-00", self.last_text)
        await self._callback("admin:ai:md:gemini:9999")
        self.assertIn("model-24", self.last_text)
        self.bot._edit_callback.reset_mock()
        await self._callback("admin:ai:md:unknownprovider:1")
        self.assertEqual(self.last_text, "")
        self.assertTrue(self.last_call is None or "نامعتبر" in str(self.last_call))

    async def test_page_rendering_never_touches_a_provider(self):
        await self._seed_catalog()

        def _no_session(*_args, **_kwargs):
            raise AssertionError("a catalog redraw must not open a provider session")

        original = admin_ai_module.aiohttp.ClientSession
        admin_ai_module.aiohttp.ClientSession = _no_session
        try:
            for page in (0, 1, 2):
                await self._callback(f"admin:ai:md:gemini:{page}")
        finally:
            admin_ai_module.aiohttp.ClientSession = original
        self.assertIn("model-20", self.last_text)

    async def test_row_actions_stay_on_their_page_and_inside_the_callback_limit(self):
        await self._seed_catalog()
        await self._callback("admin:ai:md:gemini:1")
        payloads = self.payloads()
        for payload in payloads:
            self.assertLessEqual(len(payload), 64, payload)
        self.assertTrue(
            any(payload.startswith(b"admin:ai:mdep:") and payload.endswith(b":1") for payload in payloads),
            payloads,
        )
        self.assertTrue(
            any(payload.startswith(b"admin:ai:mpaid:") and payload.endswith(b":1") for payload in payloads),
            payloads,
        )
        self.assertTrue(any(payload == b"admin:ai:md:gemini:0" for payload in payloads))

    async def test_unknown_free_status_is_never_shown_as_free(self):
        await self._seed_catalog(count=2)  # model-00 -> unknown, model-01 -> permanent
        await self._callback("admin:ai:md:gemini:0")
        text = self.last_text
        self.assertIn("رایگان تلقی نمی‌شود", text)
        self.assertIn("رایگان دائمی", text)
        for line in text.splitlines():
            if "model-00" in line:
                self.assertNotIn("رایگان 🆓", line)
                self.assertIn("⚠️", line)

    async def test_promotional_and_paid_markers_survive_paging(self):
        await self.db.ai_models_upsert_discovery(
            "groq",
            [
                {"model": "promo-model", "free_status": "free_promotional", "free_until": "2026-11-01T00:00:00Z"},
                {"model": "paid-model", "free_status": "paid"},
            ],
        )
        await self._callback("admin:ai:md:groq:0")
        text = self.last_text
        self.assertIn("رایگان موقت", text)
        self.assertIn("تا 2026-11-01", text)
        self.assertIn("پولی 💳", text)

    async def test_deprecating_a_model_records_the_date_and_keeps_the_row(self):
        await self._seed_catalog(count=12)
        await self._callback("admin:ai:md:gemini:1")
        row_id = None
        for payload in self.payloads():
            if payload.startswith(b"admin:ai:mdep:"):
                row_id = int(payload.split(b":")[3])
                break
        self.assertIsNotNone(row_id)
        await self._callback(f"admin:ai:mdep:{row_id}:1")
        rows = await self.db.ai_models_list("gemini", include_unavailable=True)
        marked = next(row for row in rows if row["id"] == row_id)
        self.assertEqual(int(marked["deprecated"]), 1)
        self.assertTrue(marked["deprecation_date"])
        self.assertEqual(len(rows), 12)  # never deleted
        # and the redraw returns to the page the button came from
        self.assertIn("model-11", self.last_text)

    async def test_action_on_a_vanished_row_fails_safely(self):
        await self._seed_catalog(count=2)
        await self._callback("admin:ai:mdep:999999:0")
        self.assertTrue(self.last_call.args[1] if self.last_call else True)
        rows = await self.db.ai_models_list("gemini", include_unavailable=True)
        self.assertTrue(all(not int(row["deprecated"]) for row in rows))


class PlanAndRoutePanelTests(PanelCase):
    async def test_every_skip_reason_is_translated_and_keeps_its_code(self):
        await self.db.ai_routes_replace(
            "notes",
            "chunk_structuring",
            [
                {"provider": "cohere", "enabled": 0, "free_only": 1},
                {"provider": "nara", "enabled": 1, "free_only": 1},
            ],
            admin_id=None,
        )
        await self._callback("admin:ai:plan")
        text = self.last_text
        self.assertIn("route_disabled", text)
        self.assertRegex(text, r"cohere[^\n]*[\u0600-\u06FF]")

    async def test_the_translation_table_covers_every_code_the_router_can_emit(self):
        source = Path("gamas_bot/ai/routing.py").read_text(encoding="utf-8")
        codes = set(
            re.findall(r'skipped\.append\(\s*\([^,()]+,\s*"([a-z_]+)"\s*\)\)', source)
        )
        self.assertTrue(codes, "no skip codes found in the router")
        missing = codes - set(admin_ai_module._SKIP_REASON_FA)
        self.assertFalse(missing, f"untranslated plan skip codes: {sorted(missing)}")

    async def test_route_panel_lists_every_addable_provider(self):
        await self.db.ai_routes_replace(
            "notes", "chunk_structuring", [{"provider": "nara", "enabled": 1, "free_only": 1}],
            admin_id=None,
        )
        await self._callback("admin:ai:rt:chunk_structuring")
        payloads = self.payloads()
        radd = [payload for payload in payloads if payload.startswith(b"admin:ai:rtadd:")]
        # Far more than four providers accept a route row; the old panel offered
        # the first four and left the admin believing the rest did not exist.
        self.assertGreater(len(radd), 5)
        for payload in radd:
            self.assertLessEqual(len(payload), 64, payload)
        # The panel keeps the legacy short form for the default task, so the
        # reset control is matched by prefix rather than by an exact token.
        self.assertTrue(
            any(payload.startswith(b"admin:ai:rtsd") for payload in payloads), payloads
        )

    async def test_reset_to_default_order_only_permutes_the_stored_rows(self):
        from gamas_bot.ai.routing import DEFAULT_NOTE_ORDER

        await self.db.ai_routes_replace(
            "notes",
            "chunk_structuring",
            [
                {"provider": slug, "enabled": 1, "free_only": 1}
                for slug in ("groq", "nara", "openrouter")
            ],
            admin_id=None,
        )
        await self._callback("admin:ai:rtsd:chunk_structuring")
        rows = await self.db.ai_routes_list("notes", "chunk_structuring")
        order = [row["provider"] for row in rows]
        self.assertEqual(order, [slug for slug in DEFAULT_NOTE_ORDER if slug in set(order)])
        self.assertEqual(sorted(order), ["groq", "nara", "openrouter"])
        self.assertTrue(all(int(row["free_only"]) == 1 for row in rows))
        # an explicit admin action is attributed, so the automatic upgrade
        # cannot reorder this route again later
        self.assertTrue(all(row["updated_by_admin_id"] == 7 for row in rows))


class AcknowledgementTests(PanelCase):
    async def test_expensive_read_is_acked_before_the_database_work(self):
        order: list[str] = []
        real_stats = self.db.stats

        async def stats():
            order.append("db.stats")
            return await real_stats()

        self.db.stats = stats
        event = _callback_event(7, "admin:stats")
        event.answer = AsyncMock(side_effect=lambda *a, **k: order.append("answer"))
        await self.bot._handle_callback(event)
        self.assertEqual(order, ["answer", "db.stats"])

    async def test_credit_screen_is_acked_before_its_reads(self) -> None:
        order: list[str] = []
        real = self.db.admin_user_credit_overview

        async def overview(telegram_id, *, recent_rows: int = 10, usage_rows: int = 8):
            order.append("db.admin_user_credit_overview")
            return await real(telegram_id, recent_rows=recent_rows, usage_rows=usage_rows)

        self.db.admin_user_credit_overview = overview  # type: ignore[method-assign]
        event = _callback_event(7, "admin:credits:101")
        event.answer = AsyncMock(side_effect=lambda *a, **k: order.append("answer"))
        await self.bot._handle_callback(event)
        self.assertEqual(order, ["answer", "db.admin_user_credit_overview"])

    async def test_a_callback_is_answered_at_most_once(self):
        for data in ("admin:stats", "admin:users", "admin:users:0", "admin:ai"):
            with self.subTest(data=data):
                event = _callback_event(7, data)
                await self.bot._handle_callback(event)
                self.assertLessEqual(event.answer.await_count, 1)

    async def test_non_admin_is_rejected_without_rendering(self):
        event = _callback_event(999, "admin:users")
        await self.bot._handle_callback(event)
        self.bot._edit_callback.assert_not_called()
        self.assertTrue(event.answer.await_args.kwargs.get("alert"))


class UserListTests(PanelCase):
    async def _add_users(self, count: int) -> list[int]:
        ids = []
        for index in range(count):
            telegram_id = 1000 + index
            await self.db.upsert_user(telegram_id, username=f"user{index}")
            ids.append(telegram_id)
        return ids

    async def test_user_list_pages_instead_of_stopping_at_fifty(self):
        ids = set(await self._add_users(60))
        size = self.bot.USERS_PAGE_SIZE
        await self._callback("admin:users")
        # The callback itself registers the sender, so the total is read after
        # the first render rather than assumed.
        total = int((await self.db.stats())["users"])
        pages = -(-total // size)
        self.assertGreater(pages, 1)
        self.assertIn(f"صفحهٔ 1 از {pages} ({total} کاربر)", self.last_text)
        self.assertEqual(len(re.findall(r"شناسه: ", self.last_text)), size)
        self.assertIn(b"admin:users:1", self.payloads())
        reachable = set()
        for page in range(pages):
            await self._callback(f"admin:users:{page}")
            with self.subTest(page=page):
                self.assertIn(f"صفحهٔ {page + 1} از {pages}", self.last_text)
                reachable.update(
                    int(match) for match in re.findall(r"شناسه: (\d+)", self.last_text)
                )
                expected = size if page < pages - 1 else total - page * size
                self.assertEqual(len(re.findall(r"شناسه: ", self.last_text)), expected)
        # Every account is reachable somewhere across the pages: nothing is cut.
        self.assertTrue(ids <= reachable)
        # the last page offers no "next" button
        self.assertNotIn(f"admin:users:{pages}".encode("ascii"), self.payloads())

    async def test_malformed_page_token_shows_the_first_page(self):
        await self._add_users(3)
        await self._callback("admin:users:garbage")
        self.assertIn("شناسه: ", self.last_text)
        self.assertEqual(len(re.findall(r"شناسه: ", self.last_text)), 4)  # 3 + the admin
        self.assertNotIn("از ", self.last_text.splitlines()[0])  # single page: no indicator


class CredentialPanelTests(PanelCase):
    async def _add_keys(self, count: int, *, service: str = "notes") -> list[int]:
        ids = []
        for index in range(count):
            provider = ("groq" if index % 2 else "gemini") if service == "notes" else "deepgram"
            ids.append(
                await self.bot.credential_manager.add_credential(
                    service=service,
                    provider=provider,
                    label=f"key-{index}",
                    secret=f"gsk-secret-value-{index:04d}",
                    admin_id=7,
                )
            )
        return ids

    async def test_credential_list_pages_and_keeps_its_filter(self):
        # Each row costs a line and a three-button row; Telegram stops at 100
        # buttons, so an uncapped list could not be sent at all past ~33 keys.
        await self._add_keys(15)
        await self._callback("admin:ai:keys")
        self.assertIn("صفحهٔ 1 از 2", self.last_text)
        self.assertEqual(self.last_text.count("• #"), self.bot.ai_panels.KEYS_PAGE_SIZE)
        self.assertIn(b"admin:ai:keys:all:1", self.payloads())
        await self._callback("admin:ai:keys:all:1")
        self.assertIn("صفحهٔ 2 از 2", self.last_text)
        self.assertEqual(self.last_text.count("• #"), 3)
        for payload in self.payloads():
            self.assertLessEqual(len(payload), 64, payload)
        self.assertNotIn("gsk-secret-value", self.last_text)
        self.assertIn("••••", self.last_text)  # masked, always

    async def test_credential_filter_starts_back_at_the_first_page(self):
        await self._add_keys(3, service="stt")
        await self._add_keys(14, service="notes")
        await self._callback("admin:ai:keys:stt")
        self.assertNotIn("صفحهٔ", self.last_text)
        self.assertEqual(self.last_text.count("• #"), 3)
        # The notes filter does start a new page window, it does not leak the
        # other service's rows into this one.
        await self._callback("admin:ai:keys:notes")
        self.assertEqual(self.last_text.count("• #"), self.bot.ai_panels.KEYS_PAGE_SIZE)
        self.assertIn("صفحهٔ 1 از 2", self.last_text)
        self.assertEqual(len(re.findall(r"• #\d+ ", self.last_text)), 12)

    async def test_malformed_keys_page_shows_the_first_page(self):
        await self._add_keys(13)
        await self._callback("admin:ai:keys:all:garbage")
        self.assertIn("صفحهٔ 1 از 2", self.last_text)


    async def test_operations_credential_screen_pages_too(self):
        # The ops digest had the same 100-button ceiling problem as the AI key
        # panel: ~20 stored keys and the screen stopped rendering.
        await self._add_keys(13, service="notes")
        await self._add_keys(12, service="stt")
        await self._callback("admin:credentials")
        self.assertIn("صفحهٔ 1 از 3", self.last_text)
        self.assertEqual(self.last_text.count("• #"), self.bot.CREDENTIALS_PAGE_SIZE)
        self.assertIn(b"admin:credentials:1", self.payloads())
        await self._callback("admin:credentials:2")
        self.assertIn("صفحهٔ 3 از 3", self.last_text)
        self.assertEqual(self.last_text.count("• #"), 1)
        for payload in self.payloads():
            self.assertLessEqual(len(payload), 64, payload)
        self.assertNotIn("gsk-secret-value", self.last_text)
        await self._callback("admin:credentials:bad-token")
        self.assertIn("صفحهٔ 1 از 3", self.last_text)


class QuotaOverviewTests(PanelCase):
    """The combined quota screen stays sendable and says what it folded away."""

    async def _add_budgets(self, provider: str, count: int) -> None:
        for index in range(count):
            await self.db.stt_quota_set_budget(
                provider,
                f"scope{index}",
                f"quota{index}",
                limit=100.0,
                remaining=50.0,
                reset_at=None,
                admin_id=7,
            )

    def _delete_rows(self) -> list[tuple[str, bytes]]:
        return [item for item in self.flat_buttons() if "✕ حذف سقف" in item[0]]

    async def test_overview_bounds_budgets_and_links_the_rest(self) -> None:
        extra = SttPanels.QUOTA_OVERVIEW_BUDGETS + 2
        await self._add_budgets("groq", extra)
        await self._callback("admin:stt:qt")
        self.assertEqual(len(self._delete_rows()), SttPanels.QUOTA_OVERVIEW_BUDGETS)
        self.assertIn(
            f"{extra - SttPanels.QUOTA_OVERVIEW_BUDGETS} سقف دیگر برای همین ارائه‌دهنده"
            " ثبت شده است",
            self.last_text,
        )
        self.assertIn(b"admin:stt:qt:groq", self.payloads())
        # The provider screen still lists and can delete every budget.
        await self._callback("admin:stt:qt:groq")
        self.assertEqual(len(self._delete_rows()), extra)
        joined = b" ".join(self.payloads())
        for index in range(extra):
            self.assertIn(f"admin:stt:qdx:groq:{index}:".encode("ascii"), joined)

    async def test_delete_tokens_survive_long_admin_typed_identities(self) -> None:
        # The budget prompt accepts scope/quota_type as free text up to 60 chars
        # each; the delete callback used to carry both and blew the 64-byte limit.
        long_scope = "a" * 60
        long_type = "b" * 60
        await self.db.stt_quota_set_budget(
            "groq", long_scope, long_type, limit=100.0, remaining=40.0, reset_at=None, admin_id=7
        )
        await self._callback("admin:stt:qt:groq")
        token = next(
            item for item in self.payloads() if item.startswith(b"admin:stt:qdx:groq:")
        )
        self.assertLessEqual(len(token), 64, token)
        await self._callback(token.decode("ascii"))
        rows = await self.db.stt_quota_budgets("groq")
        self.assertNotIn(
            (long_scope, long_type), {(row["account_scope"], row["quota_type"]) for row in rows}
        )

    async def test_a_stale_fingerprint_refuses_the_write(self) -> None:
        await self.db.stt_quota_set_budget(
            "groq", "provider", "rpd", limit=100.0, remaining=40.0, reset_at=None, admin_id=7
        )
        await self._callback("admin:stt:qt:groq")
        token = next(
            item for item in self.payloads() if item.startswith(b"admin:stt:qdx:groq:")
        ).decode("ascii")
        _admin, _stt, _action, provider, position, _fp = token.split(":")
        await self._callback(f"admin:stt:qdx:{provider}:{position}:{'0' * 8}")
        self.assertEqual(len(await self.db.stt_quota_budgets("groq")), 1)

    async def test_the_literal_identity_token_still_works(self) -> None:
        await self.db.stt_quota_set_budget(
            "groq", "provider", "rpd", limit=100.0, remaining=40.0, reset_at=None, admin_id=7
        )
        await self._callback("admin:stt:qdel:groq:provider:rpd")
        self.assertEqual(await self.db.stt_quota_budgets("groq"), [])

    async def test_keyboard_stays_inside_telegrams_limit(self) -> None:
        for provider in ("groq", "deepgram", "speechmatics", "openai_compatible"):
            await self._add_budgets(provider, 12)
        pages = -(-len(STT_PROVIDER_REGISTRY) // SttPanels.QUOTA_PROVIDERS_PAGE_SIZE)
        seen = 0
        for page in range(pages):
            await self._callback("admin:stt:qt" if page == 0 else f"admin:stt:qtp:{page}")
            self.assertLessEqual(len(self.flat_buttons()), 100)
            self.assertLessEqual(len(self.last_text), 4096)
            seen += len(self._delete_rows())
        # Every provider that has budgets is reachable, each showing its page of
        # them, so none of the registered budgets is hidden from the panel.
        self.assertEqual(seen, 4 * SttPanels.QUOTA_OVERVIEW_BUDGETS)


class RouteUsabilityAnnotationTests(PanelCase):
    """A route row never reads as usable when the planner cannot use it."""

    async def _route(self, *providers: str) -> None:
        await self.db.ai_routes_replace(
            "notes",
            "chunk_structuring",
            [{"provider": slug, "enabled": 1, "free_only": 1} for slug in providers],
            admin_id=None,
        )
        await self._callback("admin:ai:rt")

    async def test_blocked_rows_carry_the_gate_reason_and_the_machine_code(self) -> None:
        await self._route("zai", "nara")
        text = self.last_text
        # A provider blocked by a policy gate and a provider with no ready key are
        # both 'saved but unusable'; neither may be shown as a plain healthy row.
        self.assertRegex(text, r"zai[^\n]*\n\s*⚠️[^\n]*terms_of_use_blocked")
        self.assertRegex(text, r"nara[^\n]*\n\s*⚠️[^\n]*no_credential")
        self.assertIn("⚠️ یعنی ردیف در مسیر ذخیره شده ولی همین حالا قابل استفاده نیست", text)

    async def test_the_warning_disappears_once_the_key_is_attested(self) -> None:
        await self._route("nara")
        self.assertIn("no_credential", self.last_text)
        credential_id = await self.bot.credential_manager.add_credential(
            service="notes",
            provider="nara",
            label="primary",
            secret="nara-secret-value-0001",
            admin_id=7,
        )
        # An unattested key is still not billable-safe, so the row stays flagged.
        await self._callback("admin:ai:rt")
        self.assertIn("no_credential", self.last_text)
        await self.bot.credential_manager.set_billing_attestation(credential_id, "free", 7)
        await self._callback("admin:ai:rt")
        self.assertNotIn("no_credential", self.last_text)
        self.assertIn("1. ", self.last_text)


class AiLogTotalsTests(PanelCase):
    """The AI log page and its totals must answer the same question."""

    async def _insert(
        self,
        count: int,
        *,
        provider: str = "groq",
        http_status: int | None = None,
        error_class: str | None = None,
        job_id: str | None = None,
        event: str = "note_request_failed",
    ) -> None:
        for _ in range(count):
            await self.db.ai_event_insert(
                {
                    "level": "error" if http_status or error_class else "info",
                    "event": event,
                    "service": "notes",
                    "provider": provider,
                    "http_status": http_status,
                    "error_class": error_class,
                    "job_id": job_id,
                }
            )

    async def test_count_mirrors_every_page_filter(self) -> None:
        for status, error in ((404, "http_4xx"), (429, "rate_limited"), (500, "http_5xx")):
            await self._insert(2, http_status=status, error_class=error, job_id=f"j{status}")
        self.assertEqual(await self.db.ai_event_count(provider="groq"), 6)
        # The "< 100 means status class" convention is the page query's, not new.
        self.assertEqual(await self.db.ai_event_count(http_status=4), 4)
        self.assertEqual(await self.db.ai_event_count(http_status=5), 2)
        self.assertEqual(await self.db.ai_event_count(http_status=429), 2)
        self.assertEqual(await self.db.ai_event_count(error_class="rate_limited"), 2)
        self.assertEqual(await self.db.ai_event_count(job_id="j500"), 2)
        self.assertEqual(await self.db.ai_event_count(since="2999-01-01T00:00:00+00:00"), 0)
        self.assertEqual(
            await self.db.ai_event_count(http_status=4, provider="groq"),
            len(await self.db.ai_events_list(limit=200, provider="groq", http_status=4)),
        )

    async def test_header_states_the_total_and_pages(self) -> None:
        await self._insert(40)
        await self._callback("admin:ai:lg")
        size = AIPanels.LOGS_PAGE_SIZE
        pages = -(-40 // size)
        self.assertIn(f"صفحهٔ 1 از {pages} (40 رویداد)", self.last_text)
        # Following the rendered control keeps the filter and advances the page.
        for expected in range(2, pages + 1):
            payload = next(
                item[1] for item in self.flat_buttons() if "بعدی" in item[0]
            ).decode("ascii")
            await self._callback(payload)
            self.assertIn(f"صفحهٔ {expected} از {pages}", self.last_text)
            self.assertIn("note_request_failed", self.last_text)
        self.assertFalse([item for item in self.flat_buttons() if "بعدی" in item[0]])

    async def test_stale_page_token_clamps_to_the_last_page(self) -> None:
        await self._insert(20)
        await self._callback("admin:ai:lgp:9900:-")
        pages = -(-20 // AIPanels.LOGS_PAGE_SIZE)
        self.assertIn(f"صفحهٔ {pages} از {pages} (20 رویداد)", self.last_text)
        self.assertIn("note_request_failed", self.last_text)

    async def test_empty_filter_says_so_without_a_page_indicator(self) -> None:
        await self._insert(3, provider="gemini")
        await self._callback("admin:ai:lg:nomatchprovider")
        self.assertIn("رویدادی با این فیلتر ثبت نشده است", self.last_text)
        self.assertNotIn("صفحهٔ", self.last_text)


class CredentialWizardTests(PanelCase):
    """The key-setup wizard must stay sendable and resolve what it rendered."""

    async def test_model_step_payloads_fit_telegrams_callback_limit(self) -> None:
        for slug in STT_PROVIDER_REGISTRY:
            await self._callback(f"admin:ai:wiz:pv:stt:{slug}")
            for _label, payload in self.flat_buttons():
                self.assertLessEqual(len(payload), 64, f"{slug}: {payload!r}")

    async def test_the_long_model_id_that_used_to_break_the_step(self) -> None:
        # This exact payload was 65 bytes, i.e. Telegram rejected the whole step.
        legacy = "admin:ai:wiz:mdl:stt:gemini_transcribe:gemini-3.5-transcribe-live".encode()
        self.assertGreater(len(legacy), 64)
        await self._callback("admin:ai:wiz:pv:stt:gemini_transcribe")
        self.assertNotIn(legacy, self.payloads())
        self.assertTrue(any(b"wiz:mdl:stt:gemini_transcribe:" in item for item in self.payloads()))

    async def test_an_index_resolves_to_the_rendered_model(self) -> None:
        await self._callback("admin:ai:wiz:pv:stt:groq")
        models = self.bot.ai_panels._wizard_stt_models("groq")
        self.assertGreater(len(models), 1)
        payload = next(
            item
            for item in self.payloads()
            if item.startswith(b"admin:ai:wiz:mdl:stt:groq:") and item.endswith(b":1")
        )
        await self._callback(payload.decode("ascii"))
        self.assertIn(f"مدل انتخاب‌شده: {models[1]}", self.last_text)

    async def test_a_stale_index_fails_without_touching_the_pending_setup(self) -> None:
        await self._callback("admin:ai:wiz:pv:stt:groq")
        self.bot._pending_credential_setup.clear()
        event = await self._callback("admin:ai:wiz:mdl:stt:groq:99")
        self.assertIn("این مدل دیگر در فهرست نیست", str(event.answer.call_args))
        self.assertNotIn(7, self.bot._pending_credential_setup)

    async def test_the_step_says_when_it_leaves_known_models_out(self) -> None:
        self.bot.ai_panels.WIZARD_MODEL_CHOICES = 2
        await self._callback("admin:ai:wiz:pv:stt:speechmatics")
        self.assertIn("(2 مدل شناخته‌شدهٔ دیگر", self.last_text)
        self.assertLessEqual(len(self.flat_buttons()), 100)


class SttCatalogCallbackTests(PanelCase):
    """Provider-supplied model ids must never decide whether a page is sendable."""

    LONG_MODEL = "2025-01-01-google-us-en-wideband-reflow-extended-diagnostic-v3"

    async def _seed(self, provider: str, models: list[str]) -> None:
        await self.db.stt_models_upsert(
            [
                {
                    "provider": provider,
                    "model": model,
                    "display_name": model,
                    "status": "available",
                    "available": 1,
                    "deprecated": 0,
                    "source": "live_sync",
                }
                for model in models
            ]
        )

    async def test_every_payload_fits_even_with_a_pathological_model_id(self) -> None:
        # The old token embedded the model id, so one long id from a provider sync
        # made the entire catalog page unsendable (Telegram caps callback_data at
        # 64 bytes) — with no way for the admin to page past it.
        self.assertGreater(len(f"admin:stt:mdep:google_cloud_stt:{self.LONG_MODEL}".encode()), 64)
        await self._seed("google_cloud_stt", [self.LONG_MODEL, "second-model", "third-model"])
        await self._callback("admin:stt:md:google_cloud_stt:0")
        payloads = self.payloads()
        self.assertTrue(any(b"mdep" in item for item in payloads), payloads)
        for item in payloads:
            self.assertLessEqual(len(item), 64, item)

    async def test_the_action_touches_the_row_that_was_rendered(self) -> None:
        await self._seed("google_cloud_stt", [self.LONG_MODEL])
        await self._callback("admin:stt:md:google_cloud_stt:0")
        payload = next(item for item in self.payloads() if b"mdep" in item).decode("ascii")
        self.assertIn(":", payload.split("mdep")[1])
        await self._callback(payload)
        rows = {row["model"]: row for row in await self.db.stt_models_list("google_cloud_stt")}
        self.assertEqual(int(rows[self.LONG_MODEL]["deprecated"]), 1)
        self.assertTrue(rows[self.LONG_MODEL]["deprecation_date"])

    async def test_a_stale_position_fails_without_writing(self) -> None:
        await self._seed("google_cloud_stt", [self.LONG_MODEL])
        before = [dict(row) for row in await self.db.stt_models_list("google_cloud_stt")]
        event = await self._callback("admin:stt:mdep:google_cloud_stt:0:99")
        self.assertIn("این مدل دیگر در فهرست نیست", str(event.answer.call_args))
        after = [dict(row) for row in await self.db.stt_models_list("google_cloud_stt")]
        self.assertEqual([row["deprecated"] for row in before], [row["deprecated"] for row in after])

    async def test_a_malformed_position_fails_without_writing(self) -> None:
        await self._seed("google_cloud_stt", [self.LONG_MODEL])
        await self._callback("admin:stt:mdep:google_cloud_stt:0:not-a-number")
        rows = {row["model"]: row for row in await self.db.stt_models_list("google_cloud_stt")}
        self.assertEqual(int(rows[self.LONG_MODEL]["deprecated"]), 0)


class BillingPanelTests(PanelCase):
    """Finance panels count the backlog; they never report a page as a total."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from gamas_bot.billing import plan_catalog

        await self.db.sync_plan_catalog(
            [plan.as_record() for plan in plan_catalog(self.settings.plan_values)]
        )

    async def _pending(self, count: int) -> list[int]:
        """Create ``count`` receipts that really are waiting for review.

        A request only joins the admin queue once the user has submitted its
        receipt, so the helper walks the same two steps as the user flow.
        """
        ids: list[int] = []
        for index in range(count):
            user = await self.db.upsert_user(9000 + index, f"buyer{index}")
            request = await self.db.create_payment_request(int(user["id"]), "paid_25h_30d")
            payment_id = int(request["id"])
            await self.db.submit_payment_receipt(
                payment_id, int(user["id"]), f"/nonexistent/receipt-{index}.jpg", 500 + index
            )
            ids.append(payment_id)
        return ids

    async def test_pending_page_states_the_total_and_navigates(self) -> None:
        await self._pending(25)
        first = await self._callback("admin:payments")
        self.assertIn("صفحهٔ 1 از 2 — 25 پرداخت در انتظار بررسی", str(first.answer.call_args))
        self.assertEqual(first.respond.await_count, 20)  # one page, not the whole queue
        self.assertIn(b"admin:payments:1", self.payloads())
        self.assertIn("صفحهٔ 1 از 2", self.last_text)
        second = await self._callback("admin:payments:1")
        self.assertEqual(second.respond.await_count, 5)
        self.assertIn("صفحهٔ 2 از 2", self.last_text)
        self.assertNotIn(b"admin:payments:2", self.payloads())  # no dead next page

    async def test_a_page_of_receipts_costs_one_query(self) -> None:
        await self._pending(20)
        calls: list[int] = []
        original = self.db.payment_detail

        async def spy(payment_id: int):
            calls.append(int(payment_id))
            return await original(payment_id)

        self.db.payment_detail = spy  # type: ignore[method-assign]
        event = await self._callback("admin:payments")
        self.assertEqual(calls, [], "the row list must carry its own receipt fields")
        caption = str(event.respond.call_args.args[0])
        self.assertIn("پرداخت در انتظار بررسی دستی — درخواست", caption)
        # the receipt is missing on disk, and the panel says so instead of failing
        self.assertIn("تصویر رسید در فضای خصوصی در دسترس نیست", caption)
        self.assertTrue(any("کاربر: 900" in str(call.args[0]) for call in event.respond.call_args_list))

    async def test_the_overview_reports_the_whole_backlog(self) -> None:
        await self._pending(25)
        await self._callback("admin:home")
        self.assertIn("💳 25 پرداخت در انتظار بررسی", self.last_text)

    async def test_every_listed_special_user_has_its_own_remove_button(self) -> None:
        for index in range(20):
            await self.db.upsert_user(7100 + index, f"vip{index}")
            await self.db.set_user_unlimited(7100 + index, True, 7, reason="تیم داخلی")
        await self._callback("admin:special")
        first_page = {
            int(item.split(b":")[-1])
            for item in self.payloads()
            if item.startswith(b"admin:special:remove:")
        }
        self.assertEqual(len(first_page), self.bot.SPECIAL_PAGE_SIZE)
        self.assertIn("تعداد: 20", self.last_text)
        await self._callback("admin:special:1")
        second_page = {
            int(item.split(b":")[-1])
            for item in self.payloads()
            if item.startswith(b"admin:special:remove:")
        }
        # Every special user is actionable from the panel, not only the first 12.
        self.assertEqual(first_page | second_page, {7100 + index for index in range(20)})
        target = next(iter(second_page))
        await self._callback(f"admin:special:remove:{target}")
        self.assertEqual(await self.db.unlimited_user_count(), 19)

    async def test_a_stale_page_token_opens_the_last_page(self) -> None:
        for index in range(20):
            await self.db.upsert_user(7200 + index, f"vip{index}")
            await self.db.set_user_unlimited(7200 + index, True, 7, reason="تیم داخلی")
        await self._callback("admin:special:999")
        self.assertIn("صفحهٔ 2 از 2", self.last_text)

    async def test_the_plan_panel_stays_sendable_with_many_custom_plans(self) -> None:
        for index in range(45):
            await self.db.create_plan(
                code=f"custom{index:02d}",
                name=f"طرح داخلی {index}",
                hours=10,
                price_toman=100_000 + index,
                validity_days=30,
                admin_id=7,
            )
        total = len(await self.db.list_plans(include_disabled=True))
        pages = max(1, -(-total // self.bot.PLANS_PAGE_SIZE))
        self.assertGreater(pages, 1)
        seen: set[int] = set()
        for page in range(pages):
            await self._callback("admin:plans" if page == 0 else f"admin:plans:{page}")
            text = self.last_text
            payload = self.payloads()
            self.assertLessEqual(len(text), 4096, f"page {page} is too long to send")
            self.assertLessEqual(len(self.flat_buttons()), 100, f"page {page} has too many buttons")
            self.assertIn(f"تعداد طرح‌ها: {total}", text)
            self.assertIn(f"صفحهٔ {page + 1} از {pages}", text)
            seen |= {
                int(item.split(b":")[-1])
                for item in payload
                if item.startswith(b"admin:plan:toggle:")
            }
        # no plan is unreachable from the panel because the list was cut
        self.assertTrue(seen)
        self.assertGreaterEqual(len(seen), self.bot.PLANS_PAGE_SIZE)


class MonitoringDigestTests(PanelCase):
    """A digest renders what it read, and a ranked cut says it is a cut."""

    async def test_usage_panel_shows_one_line_per_provider_snapshot(self) -> None:
        slugs = list(PROVIDER_REGISTRY)[:8]
        for index, slug in enumerate(slugs):
            await self.db.ai_quota_upsert(
                slug, None, "some-model", "requests/min", str(100 - index), None
            )
        await self._callback("admin:ai:us:1")
        text = self.last_text
        # The panel used to fetch 12 rows and print four of them, so a deployment
        # with more than four snapshotted providers saw an unmarked subset.
        for slug in slugs:
            self.assertIn(f"• {slug}:", text)
        self.assertIn("(۸ ارائه‌دهنده)", text)

    async def test_benchmark_panel_labels_its_ranked_cut(self) -> None:
        rows = AIPanels.BENCHMARK_ROWS + 3
        for index in range(rows):
            await self.db.ai_model_set_quality("openrouter", f"model-{index:02d}", 50.0 + index)
        await self._callback("admin:ai:bm")
        text = self.last_text
        scored = [line for line in text.split("\n") if "امتیاز" in line and line[:2].rstrip(".").isdigit()]
        self.assertEqual(len(scored), AIPanels.BENCHMARK_ROWS)
        self.assertIn("فقط ۱۰ مدل برتر", text)
        # the top of the ranking really is the top
        self.assertIn("model-12", text)

    async def test_benchmark_panel_stays_quiet_when_nothing_is_cut(self) -> None:
        await self.db.ai_model_set_quality("openrouter", "only-model", 61.0)
        await self._callback("admin:ai:bm")
        self.assertNotIn("مدل برتر این‌جا فهرست می‌شود", self.last_text)

    async def test_users_export_marks_itself_as_a_prefix(self) -> None:
        for index in range(60):
            await self.db.upsert_user(4000 + index, f"buyer{index}")
        sent: list[str] = []

        async def capture(event, text_body, *, is_admin: bool = False) -> None:
            sent.append(text_body)

        self.bot._send_long_message = capture  # type: ignore[method-assign]
        await self.bot._handle_admin_command(_callback_event(7, "/users"), "/users", "/users")
        self.assertTrue(sent)
        self.assertIn("۵۰ کاربر اخیر از ۶۰ کاربر", sent[0])


    async def test_provider_detail_lists_every_quota_snapshot(self) -> None:
        windows = ["day", "month", "minutes", "requests/min", "tokens/min", "week"]
        for window in windows:
            await self.db.ai_quota_upsert("openrouter", None, "", window, f"val-{window}", None)
        await self._callback("admin:ai:pv:openrouter")
        text_body = self.last_text
        # The panel used to render quotas[:3] of a 20-row read, so three of the
        # provider's live quotas were shown and the rest vanished silently.
        for window in windows:
            self.assertIn(f"• {window}: val-{window}", text_body)
        self.assertNotIn("فقط ۱۲ تصویر سهمیه", text_body)
        # the local OpenRouter ledger is still shown, next to the snapshots
        self.assertIn("• حسابگر روزانه:", text_body)

    async def test_provider_detail_labels_a_quota_cut_and_keeps_it_stable(self) -> None:
        for index in range(AIPanels.QUOTA_ROWS + 3):
            await self.db.ai_quota_upsert("groq", None, "", f"w{index:02d}", "x", None)
        await self._callback("admin:ai:pv:groq")
        text_body = self.last_text
        self.assertIn("• w11: x", text_body)
        self.assertNotIn("• w12: x", text_body)  # the cut is a stable prefix, not recency
        self.assertIn("فقط ۱۲ تصویر سهمیه این‌جا فهرست می‌شود", text_body)

    async def test_health_panel_filters_by_service_before_its_cap(self) -> None:
        probes = [self._health_probe("stt", index) for index in range(20)]
        probes += [self._health_probe(SERVICE, index) for index in range(5)]
        self.bot.provider_health = _FakeHealthChecker(probes)
        await self._callback("admin:ai:he")
        text_body = self.last_text
        # The display cap used to be taken from the mixed cache, so twenty STT
        # probes pushed every AI result out and the panel looked nearly empty.
        for index in range(5):
            self.assertIn(f"• groq / ai{index}:", text_body)
        self.assertNotIn("تای نخست این‌جا فهرست می‌شوند", text_body)

    async def test_health_panel_states_what_a_capped_cache_left_out(self) -> None:
        probes = [self._health_probe(SERVICE, index) for index in range(AIPanels.HEALTH_ROWS + 8)]
        self.bot.provider_health = _FakeHealthChecker(probes)
        await self._callback("admin:ai:he")
        text_body = self.last_text
        self.assertIn("• groq / ai11:", text_body)
        self.assertNotIn("• groq / ai12:", text_body)
        self.assertIn("این سرویس ۲۰ نتیجه در کش سلامت دارد", text_body)

    async def test_settings_panel_lists_every_admin_override(self) -> None:
        slugs = list(PROVIDER_REGISTRY)[:9]
        for slug in slugs:
            await self.db.ai_provider_settings_upsert(slug, enabled=0)
        await self._callback("admin:ai:st")
        text_body = self.last_text
        # ``list(overrides.items())[:8]`` dropped the ninth provider entirely.
        for slug in slugs:
            self.assertIn(f"• {slug}: غیرفعال", text_body)
        self.assertIn("(۹ ارائه‌دهنده)", text_body)

    async def test_settings_panel_hides_an_empty_override_heading(self) -> None:
        await self.db.ai_provider_settings_upsert("groq", region="IR")
        await self._callback("admin:ai:st")
        # A stored row without a listed flag used to print the heading over
        # nothing at all.
        self.assertNotIn("لغوهای ثبت‌شدهٔ مدیر", self.last_text)

    @staticmethod
    def _health_probe(service: str, index: int):
        return SimpleNamespace(
            service=service,
            provider="groq",
            label=("ai" if service == SERVICE else "stt") + str(index),
            status_fa="🟢",
            http_status=200,
            latency_ms=10 + index,
            checked_at="2026-10-10T10:00:00Z",
            credential_id=None,
            check_type="read_only",
        )


class _FakeHealthChecker:
    """Cached-only health source: the panel must not need a network probe."""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    def cached(self) -> list:
        return list(self._rows)

    async def check_all(self, *, force: bool = False) -> list:
        raise AssertionError("the health panel must not probe providers")


class LatencySampleLabelTests(PanelCase):
    """A percentile sampled from a bounded read has to say that it is sampled."""

    async def _seed_usage(self, rows: int) -> None:
        for index in range(rows):
            await self.db.ai_usage_insert(
                {
                    "provider": "groq",
                    "latency_ms": 900 + index,
                    "result": "success",
                    "total_tokens": 10,
                }
            )

    async def test_overview_reports_a_saturated_percentile_sample(self) -> None:
        await self._seed_usage(120)
        full = await self.db.ai_usage_overview(days=1, sample=500)
        self.assertFalse(full["latency_sample_capped"])
        self.assertEqual(full["latency_samples"], 120)
        capped = await self.db.ai_usage_overview(days=1, sample=100)
        self.assertTrue(capped["latency_sample_capped"])
        self.assertEqual(capped["latency_samples"], 100)

    async def test_ai_panels_label_a_sampled_p95(self) -> None:
        real = self.db.ai_usage_overview

        async def overview(*, days: int = 1, sample: int | None = None):
            metrics = await real(days=days, sample=sample)
            # The flag is what the panels consume; forcing it here keeps the test
            # about the label rather than about seeding 5 001 rows.
            metrics["latency_sample_capped"] = True
            metrics["latency_samples"] = 5000
            return metrics

        self.db.ai_usage_overview = overview  # type: ignore[method-assign]
        await self._seed_usage(3)
        for token in ("admin:ai", "admin:ai:us:1"):
            await self._callback(token)
            with self.subTest(token=token):
                # the two panels word the latency line differently; both must
                # carry the same sampling note after the number
                self.assertIn("p95", self.last_text)
                self.assertIn(" (نمونه: 5/000 درخواست اخیر)", self.last_text)

    async def test_no_sample_label_when_the_read_saw_the_whole_window(self) -> None:
        await self._seed_usage(4)
        await self._callback("admin:ai")
        self.assertIn("تأخیر p95", self.last_text)
        self.assertNotIn("درخواست اخیر)", self.last_text)


class SttPanelTests(PanelCase):
    async def _seed_stt_models(self, provider: str = "groq", count: int = 14) -> None:
        rows = [
            STTModelInfo(
                provider=provider,
                model=f"{provider}-asr-{index:02d}",
                display_name=f"Model {index}",
                persian_supported=index % 2 == 0,
                quality_score=float(index),
            ).to_row()
            for index in range(count)
        ]
        await self.db.stt_models_upsert(rows)

    async def test_provider_page_lists_models_in_pages(self):
        await self._seed_stt_models()
        await self._callback("admin:stt:md:groq:0")
        self.assertIn("groq-asr-00", self.last_text)
        self.assertNotIn("groq-asr-10", self.last_text)
        self.assertIn("صفحهٔ 1 از 2", self.last_text)
        await self._callback("admin:stt:md:groq:1")
        self.assertIn("groq-asr-13", self.last_text)
        self.assertTrue(
            any(payload == b"admin:stt:md:groq:0" for payload in self.payloads())
        )

    async def test_provider_picker_shows_every_provider_with_a_count(self):
        await self._seed_stt_models(count=3)
        await self._callback("admin:stt:md")
        text = self.last_text
        self.assertNotIn("groq-asr-00", text)  # no model rows on the picker
        labels = [label for label, _payload in self.flat_buttons()]
        self.assertTrue(
            any("(3)" in label and "groq" in label.lower() for label in labels), labels
        )
        self.assertTrue(
            any(payload == b"admin:stt:md:groq:0" for payload in self.payloads()),
            self.payloads(),
        )

    async def test_marking_a_model_returns_to_its_page(self):
        await self._seed_stt_models(count=14)
        await self._callback("admin:stt:md:groq:1")
        self.assertIn("groq-asr-13", self.last_text)
        await self._callback("admin:stt:mdep:groq:groq-asr-13")
        rows = await self.db.stt_models_list("groq")
        marked = next(row for row in rows if row["model"] == "groq-asr-13")
        self.assertEqual(int(marked["deprecated"]), 1)
        self.assertEqual(len(rows), 14)
        # The list is redrawn on the page the button came from, not page one.
        self.assertIn("groq-asr-13", self.last_text)

    async def test_unknown_stt_provider_or_model_fails_safely(self):
        event = await self._callback("admin:stt:md:nosuchprovider:0")
        self.assertIsNone(self.last_call)
        self.assertIn("ناشناخته", event.answer.await_args.args[0])
        self.assertTrue(event.answer.await_args.kwargs.get("alert"))
        self.bot._edit_callback.reset_mock()
        second = await self._callback("admin:stt:mdep:groq:nosuchmodel")
        self.assertIn("دیگر در رجیستری نیست", second.answer.await_args.args[0])
        self.assertTrue(second.answer.await_args.kwargs.get("alert"))
        # no phantom model row was written by the stale action
        self.assertEqual(await self.db.stt_models_list("groq"), [])

    async def test_stt_logs_page_through_the_history(self):
        for index in range(30):
            await self.db.stt_provider_event_insert(
                {
                    "event": "stt_request_failed" if index % 2 else "stt_request_ok",
                    "provider": "groq",
                    "model": "whisper-1",
                    "http_status": 503 if index % 2 else 200,
                    "latency_ms": 100 + index,
                    "error_category": "transient" if index % 2 else None,
                    "level": "error" if index % 2 else "info",
                },
                prune_to=0,
            )
        await self._callback("admin:stt:lg")
        self.assertIn("صفحهٔ 1 از 3", self.last_text)
        first = self.last_text
        await self._callback("admin:stt:lgp:2:0")
        self.assertIn("صفحهٔ 3 از 3", self.last_text)
        self.assertNotEqual(first, self.last_text)
        await self._callback("admin:stt:lgp:9:0")
        self.assertIn("صفحهٔ 3 از 3", self.last_text)
        # The failure filter is a filter, not a re-label of the same page.
        await self._callback("admin:stt:lgerr")
        self.assertGreater(self.last_text.count("stt_request_failed"), 0)
        self.assertNotIn("stt_request_ok", self.last_text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
