"""Reviewed default route order: fresh seeding, safe upgrade, admin preservation.

The product contract is "Nara first, OpenRouter second" for note generation,
with two hard limits on how it is delivered:

* a *fresh* installation seeds the reviewed order for every note task;
* an *existing* installation is aligned forward only when the stored route is
  provably still the automatic one, and the alignment may only renumber rows —
  it must never add, drop, re-flag or re-write a route an admin touched, and it
  must not disturb credentials, model catalogs or usage history.

Everything here runs offline: the router only reads its own tables.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from gamas_bot.ai.models import ModelRegistry
from gamas_bot.ai.routing import (
    DEFAULT_NOTE_ORDER,
    LEGACY_NOTE_ORDER,
    ROUTE_TASKS,
    TASK_BENCHMARK,
    TASK_CHUNK,
    TASK_HEALTH,
    ProviderRouter,
    TASK_OUTLINE,
    TASK_REPAIR,
    TASK_COMPILE,
)
from gamas_bot.ai.usage import AIUsageTracker
from gamas_bot.database import Database
from gamas_bot.provider_credentials import ProviderCredentialManager

from support import make_settings

#: The four tasks that generate a note (the ones ``AI_DEFAULT_NOTE_ROUTE`` and
#: the reviewed default order govern).
NOTE_TASKS = (TASK_CHUNK, TASK_OUTLINE, TASK_REPAIR, TASK_COMPILE)


class RouteSeedCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = Database(root / "routes.sqlite3")
        await self.db.open()
        self.settings = make_settings(
            database_path=root / "routes.sqlite3",
            session_path=root / "session",
            temp_dir=root / "tmp",
            provider_credentials_encryption_key=Fernet.generate_key().decode("ascii"),
            note_api_provider="openai_compatible",
            note_api_base_url="https://router.bynara.id/v1",
            note_api_model="agnes-3-flash",
            note_api_key="sk-nara-env-1234",
        )
        self.manager = ProviderCredentialManager(self.db, self.settings)
        self.tracker = AIUsageTracker(self.db)
        self.models = ModelRegistry(self.db, self.settings)
        self.router = ProviderRouter(
            self.db, self.settings, self.manager, self.tracker, self.models
        )

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def _order(self, task: str = TASK_CHUNK) -> list[str]:
        rows = await self.db.ai_routes_list("notes", task)
        return [str(row["provider"]) for row in rows]

    async def _write_route(
        self, order, *, admin_id=None, task: str = TASK_CHUNK, **row_overrides
    ) -> None:
        await self.db.ai_routes_replace(
            "notes",
            task,
            [
                {"provider": slug, "enabled": 1, "free_only": 1, **row_overrides}
                for slug in order
            ],
            admin_id=admin_id,
        )

    async def _rows(self, task: str = TASK_CHUNK) -> list[dict]:
        return await self.db.ai_routes_list("notes", task)


class DefaultOrderTests(RouteSeedCase):
    async def test_fresh_install_leads_with_nara_then_openrouter(self):
        await self.router.ensure_seeded()
        for task in NOTE_TASKS:
            with self.subTest(task=task):
                order = await self._order(task)
                self.assertEqual(order[:2], ["nara", "openrouter"])
                self.assertEqual(order, list(DEFAULT_NOTE_ORDER))

    async def test_benchmark_and_health_tasks_follow_the_same_default(self):
        await self.router.ensure_seeded()
        for task in (TASK_BENCHMARK, TASK_HEALTH):
            with self.subTest(task=task):
                self.assertEqual((await self._order(task))[0], "nara")

    async def test_seeded_rows_are_automatic_and_free_only(self):
        await self.router.ensure_seeded()
        rows = await self._rows()
        self.assertTrue(all(row["updated_by_admin_id"] is None for row in rows))
        self.assertTrue(all(int(row["free_only"]) == 1 for row in rows))
        self.assertTrue(all(int(row["enabled"]) == 1 for row in rows))
        self.assertTrue(all(row["model"] is None for row in rows))
        self.assertEqual(
            [row["route_index"] for row in rows], list(range(len(rows)))
        )

    async def test_custom_gateway_deployment_keeps_its_own_provider_routable(self):
        # A deployment whose base URL is not Nara's keeps its custom gateway on
        # the route (at the front, because the reviewed order says nothing about
        # it) — the reviewed default must not strand an existing integration.
        from dataclasses import replace

        self.router.settings = replace(
            self.settings,
            note_api_base_url="https://gateway.example.internal/v1",
        )
        await self.router.ensure_seeded()
        order = await self._order()
        self.assertEqual(order[0], "openai_compatible")
        self.assertIn("nara", order)

    async def test_ai_default_note_route_override_applies_to_every_note_task(self):
        from dataclasses import replace

        self.router.settings = replace(
            self.settings, ai_default_note_route="mistral,gemini"
        )
        await self.router.ensure_seeded()
        for task in NOTE_TASKS:
            with self.subTest(task=task):
                order = await self._order(task)
                self.assertEqual(order[:2], ["mistral", "gemini"])
                # An override reorders, it never drops the reviewed providers.
                self.assertIn("nara", order)
                self.assertIn("openrouter", order)
        # Non-note tasks are not governed by AI_DEFAULT_NOTE_ROUTE.
        self.assertEqual((await self._order(TASK_HEALTH))[0], "nara")

    async def test_legacy_single_provider_mode_keeps_its_provider_first(self):
        from dataclasses import replace

        self.router.settings = replace(
            self.settings,
            ai_routing_enabled=False,
            note_api_provider="gemini",
            note_api_base_url="",
            gemini_api_key="AIza-env-key-1234",
        )
        self.assertEqual(self.router._seed_order(TASK_CHUNK)[0], "gemini")

    async def test_multi_provider_mode_does_not_inherit_the_legacy_first_slot(self):
        from dataclasses import replace

        # The same leftover environment as above, but with routing enabled:
        # a stale NOTE_API_PROVIDER must not keep an older provider in first
        # position, otherwise "Nara first" never applies to upgraded hosts.
        self.router.settings = replace(
            self.settings, ai_routing_enabled=True, note_api_provider="gemini"
        )
        order = self.router._seed_order(TASK_CHUNK)
        self.assertEqual(order[:2], ["nara", "openrouter"])
        self.assertIn("gemini", order)


class SeededRouteUpgradeTests(RouteSeedCase):
    async def test_untouched_seeded_route_is_aligned_forward(self):
        await self._write_route(LEGACY_NOTE_ORDER)
        before = {row["id"]: row for row in await self._rows()}
        await self.router.ensure_seeded()
        rows = await self._rows()
        self.assertEqual([row["provider"] for row in rows], list(DEFAULT_NOTE_ORDER))
        # Reordered in place: same primary keys, so nothing keyed by them dies.
        self.assertEqual({row["id"] for row in rows}, set(before))
        self.assertTrue(all(row["updated_by_admin_id"] is None for row in rows))

    async def test_alignment_preserves_per_row_route_metadata(self):
        # free_only was flipped automatically by a policy write, not an admin:
        # the reorder must renumber the rows it has, not rewrite their flags.
        await self._write_route(LEGACY_NOTE_ORDER)
        rows = await self._rows()
        await self.db.ai_routes_replace(
            "notes",
            TASK_CHUNK,
            [
                {
                    "provider": row["provider"],
                    "enabled": row["enabled"],
                    "free_only": 0 if row["provider"] == "groq" else 1,
                }
                for row in rows
            ],
            admin_id=None,
        )
        await self.router.ensure_seeded()
        # A row that is no longer free-only means the stored shape is not the
        # seeded shape: the panel/router leaves it exactly as it found it.
        self.assertEqual(
            [row["provider"] for row in await self._rows()], list(LEGACY_NOTE_ORDER)
        )
        self.assertEqual(
            int(next(r for r in await self._rows() if r["provider"] == "groq")["free_only"]),
            0,
        )

    async def test_admin_edited_route_is_never_reordered(self):
        await self._write_route(LEGACY_NOTE_ORDER, admin_id=42)
        await self.router.ensure_seeded()
        self.assertEqual(
            [row["provider"] for row in await self._rows()], list(LEGACY_NOTE_ORDER)
        )

    async def test_one_disabled_row_keeps_the_whole_task_group_frozen(self):
        await self.db.ai_routes_replace(
            "notes",
            TASK_CHUNK,
            [
                {
                    "provider": slug,
                    "enabled": 0 if slug == "gemini" else 1,
                    "free_only": 1,
                }
                for slug in LEGACY_NOTE_ORDER
            ],
            admin_id=None,
        )
        await self.router.ensure_seeded()
        self.assertEqual(
            [row["provider"] for row in await self._rows()], list(LEGACY_NOTE_ORDER)
        )

    async def test_pinned_model_keeps_the_route_frozen(self):
        await self.db.ai_routes_replace(
            "notes",
            TASK_CHUNK,
            [
                {
                    "provider": slug,
                    "enabled": 1,
                    "free_only": 1,
                    "model": "gemini-2.5-flash" if slug == "gemini" else None,
                }
                for slug in LEGACY_NOTE_ORDER
            ],
            admin_id=None,
        )
        await self.router.ensure_seeded()
        self.assertEqual(
            [row["provider"] for row in await self._rows()], list(LEGACY_NOTE_ORDER)
        )

    async def test_alignment_touches_only_routes_that_still_look_automatic(self):
        # chunk_structuring was customised; the other note tasks were not.
        await self._write_route(LEGACY_NOTE_ORDER, admin_id=42)
        for task in NOTE_TASKS[1:]:
            await self._write_route(LEGACY_NOTE_ORDER, task=task)
        await self.router.ensure_seeded()
        self.assertEqual(
            [row["provider"] for row in await self._rows()], list(LEGACY_NOTE_ORDER)
        )
        for task in NOTE_TASKS[1:]:
            with self.subTest(task=task):
                self.assertEqual(
                    [row["provider"] for row in await self._rows(task)],
                    list(DEFAULT_NOTE_ORDER),
                )

    async def test_route_with_an_unrecognised_provider_is_left_alone(self):
        # An older release could seed a route that mentioned a provider the
        # reviewed defaults do not know. Renumbering that mix could silently
        # demote a hand-picked gateway, so it is not touched at all.
        await self.db.ai_routes_replace(
            "notes",
            TASK_CHUNK,
            [
                {"provider": slug, "enabled": 1, "free_only": 1}
                for slug in list(LEGACY_NOTE_ORDER) + ["cohere"]
            ],
            admin_id=None,
        )
        await self.router.ensure_seeded()
        self.assertEqual(
            [row["provider"] for row in await self._rows()],
            list(LEGACY_NOTE_ORDER) + ["cohere"],
        )

    async def test_alignment_never_adds_or_removes_a_provider(self):
        await self._write_route(("groq", "gemini", "nara"))
        await self.router.ensure_seeded()
        order = await self._order()
        # Only the three stored providers exist; nothing is invented, dropped or
        # re-flagged, and the stored ones move into reviewed-default order.
        self.assertEqual(order, ["nara", "gemini", "groq"])

    async def test_alignment_is_idempotent(self):
        await self._write_route(LEGACY_NOTE_ORDER)
        await self.router.ensure_seeded()
        first = await self._rows()
        await self.router.ensure_seeded()
        await self.router.ensure_seeded()
        second = await self._rows()
        self.assertEqual(
            [(row["id"], row["route_index"], row["updated_at"]) for row in first],
            [(row["id"], row["route_index"], row["updated_at"]) for row in second],
        )

    async def test_alignment_is_recorded_as_an_audit_event(self):
        await self._write_route(LEGACY_NOTE_ORDER)
        await self.router.ensure_seeded()
        events = await self.db.ai_events_list(event="provider_route_changed")
        self.assertTrue(events)
        detail = str(events[0]["detail"])
        self.assertIn("nara", detail)
        self.assertNotIn("sk-", detail)  # never a secret, never a transcript

    async def test_upgrade_keeps_credentials_catalog_and_history(self):
        credential_id = await self.manager.add_credential(
            service="notes",
            provider="groq",
            label="upgrade",
            secret="gsk-upgrade-key-0001",
            admin_id=1,
        )
        await self.db.ai_models_upsert_discovery(
            "groq", [{"model": "llama-3.3-70b-versatile", "available": 1}]
        )
        await self.db.ai_usage_insert(
            {
                "service": "notes",
                "job_id": "GMS-000900",
                "request_type": TASK_CHUNK,
                "provider": "groq",
                "canonical": "groq",
                "model": "llama-3.3-70b-versatile",
                "credential_id": credential_id,
                "route_position": 2,
                "attempt": 1,
                "result": "success",
                "latency_ms": 900,
                "total_tokens": 150,
                "actual_input_tokens": 100,
                "actual_output_tokens": 50,
                "free_class": "free",
            }
        )
        await self._write_route(LEGACY_NOTE_ORDER)
        await self.router.ensure_seeded()
        self.assertEqual(
            [row["provider"] for row in await self._rows()], list(DEFAULT_NOTE_ORDER)
        )
        record = await self.db.provider_credential_record(credential_id)
        self.assertIsNotNone(record)
        catalog = await self.db.ai_models_list("groq", include_unavailable=True)
        self.assertEqual([row["model"] for row in catalog], ["llama-3.3-70b-versatile"])
        usage = await self.db.ai_usage_recent(limit=10)
        self.assertEqual(
            [row["job_id"] for row in usage if row["job_id"] == "GMS-000900"],
            ["GMS-000900"],
        )

    async def test_a_failed_route_read_never_deletes_the_stored_route(self):
        await self._write_route(LEGACY_NOTE_ORDER, admin_id=42)
        real = self.db.ai_routes_list

        async def flaky(service, task):
            if task == TASK_CHUNK:
                raise RuntimeError("database is locked")
            return await real(service, task)

        self.db.ai_routes_list = flaky
        try:
            await self.router.ensure_seeded()
        finally:
            self.db.ai_routes_list = real
        # The defaults must not be re-inserted over a route we could not read.
        self.assertEqual(
            [row["provider"] for row in await real("notes", TASK_CHUNK)],
            list(LEGACY_NOTE_ORDER),
        )

    async def test_seeding_covers_every_registered_task(self):
        await self.router.ensure_seeded()
        for task in ROUTE_TASKS:
            with self.subTest(task=task):
                self.assertTrue(await self._rows(task))
