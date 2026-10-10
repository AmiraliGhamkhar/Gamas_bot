"""Route execution: failover budget, disabled rows, Retry-After and leg budget.

These are the runtime guarantees behind "Nara first, OpenRouter second" — they
matter most when the preferred provider cannot answer:

* the failover budget is spent on providers that can actually be tried, so a
  long default route with unconfigured head providers still produces a note;
* a route row an administrator switched off is *disabled*, never silently
  reinterpreted as "this provider may be billed";
* a provider-specified pause longer than one retry cycle is honoured as a
  cooldown instead of a hot loop, and the route moves on;
* a provider that answers with a service-wide fault or too slowly does not walk
  the whole credential pool / the whole job clock before failing over.

All provider traffic is scripted: no network, no billable request.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from cryptography.fernet import Fernet

from gamas_bot.ai.adapters import FAIL_TRANSIENT
from gamas_bot.ai.models import ModelRegistry
from gamas_bot.ai.routing import NoteJobSession, ProviderRouter
from gamas_bot.ai.usage import AIUsageTracker
from gamas_bot.database import Database
from gamas_bot.provider_credentials import ProviderCredentialManager
from gamas_bot.provider_health import cooldown_remaining_seconds

from support import make_settings


class _Response:
    def __init__(self, status: int, payload=None, headers=None, *, delay: float = 0.0):
        self.status = status
        self.headers = headers or {}
        self._payload = payload
        self._delay = delay

    async def read(self):
        if self._delay:
            await asyncio.sleep(self._delay)
        return json.dumps(self._payload).encode("utf-8")

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _ScriptedSession:
    """aiohttp double that replays ``(host_substring, response)`` rules in order."""

    def __init__(self):
        self.rules: list[tuple[str, list]] = []
        self.requests: list[dict] = []

    def add(self, host: str, *responses):
        self.rules.append((host, list(responses)))

    def count(self, host: str) -> int:
        return sum(1 for request in self.requests if host in request["url"])

    def post(self, url, headers=None, params=None, json=None, timeout=None, data=None):
        self.requests.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        for host, responses in self.rules:
            if host in url and responses:
                response = responses.pop(0) if len(responses) > 1 else responses[0]
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f"unexpected request: {url}")


def _notes_payload(text: str, model: str = "m") -> dict:
    return {
        "id": "chatcmpl-test",
        "model": model,
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


def _gemini_payload(text: str) -> dict:
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
        "usageMetadata": {
            "promptTokenCount": 10,
            "candidatesTokenCount": 5,
            "totalTokenCount": 15,
        },
    }


class FailoverCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = Database(root / "failover.sqlite3")
        await self.db.open()
        self.settings = make_settings(
            database_path=root / "failover.sqlite3",
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

    def _relax_billing(self, **overrides):
        # Providers under test are deliberately not classified free, so these
        # tests exercise request failover the way a relaxed paid deployment does.
        self.settings = replace(
            self.settings,
            ai_free_only=False,
            ai_allow_paid_fallback=True,
            **overrides,
        )
        self.manager.settings = self.settings
        self.models.settings = self.settings
        self.router = ProviderRouter(
            self.db, self.settings, self.manager, self.tracker, self.models
        )

    async def _route(self, providers, *, enabled=1, free_only=0, task="chunk_structuring"):
        await self.db.ai_routes_replace(
            "notes",
            task,
            [
                {"provider": slug, "enabled": enabled, "free_only": free_only}
                for slug in providers
            ],
            admin_id=None,
        )

    async def _paid_credential(self, provider: str, *, label: str, secret: str) -> int:
        credential_id = await self.manager.add_credential(
            service="notes",
            provider=provider,
            label=label,
            secret=secret,
            admin_id=1,
        )
        await self.manager.set_billing_attestation(credential_id, "paid", 1)
        return credential_id

    async def _job(self, task: str = "chunk_structuring") -> NoteJobSession:
        plan = await self.router.plan(task)
        return NoteJobSession(self.router, plan, self.settings, job_id="GMS-000011")


class RouteEligibilityTests(FailoverCase):
    async def test_disabled_row_is_disabled_not_a_paid_leg(self):
        # The pre-fix router folded ``enabled`` into the free-only gate, so
        # switching a row off with paid fallback allowed turned it into a
        # *billable* leg instead of removing it from the route.
        self._relax_billing()
        await self.db.ai_routes_replace(
            "notes",
            "chunk_structuring",
            [
                {"provider": "nara", "enabled": 1, "free_only": 1},
                {"provider": "groq", "enabled": 0, "free_only": 1},
            ],
            admin_id=None,
        )
        plan = await self.router.plan()
        self.assertNotIn("groq", [leg.canonical for leg in plan.legs])
        self.assertEqual(dict(plan.skipped).get("groq"), "route_disabled")

    async def test_enabled_row_is_still_offered(self):
        # The same row with the flag back on is a normal, eligible leg: the skip
        # is about the switch, not about the provider being distrusted.
        self._relax_billing()
        await self._route(["nara", "groq"])
        plan = await self.router.plan()
        self.assertIn("groq", [leg.canonical for leg in plan.legs])
        self.assertNotIn("groq", dict(plan.skipped))

    async def test_unconfigured_head_providers_do_not_eat_the_failover_budget(self):
        # The reviewed default route starts with providers this deployment has
        # no key for. A budget of one failover must still reach the provider
        # that can answer, instead of burning both slots on "no credential".
        self._relax_billing(ai_max_provider_failovers=1)
        await self._route(["nara", "openrouter", "gemini"])
        await self._paid_credential("gemini", label="g", secret="AIza-test-paid-1234")
        session = _ScriptedSession()
        session.add(
            "generativelanguage.googleapis.com", _Response(200, _gemini_payload("{\"t\": \"x\"}"))
        )
        job = await self._job()
        text = await job.generate(
            request_type="chunk_structuring",
            task="chunk_structuring",
            system_prompt="S",
            user_text="U",
            session=session,
        )
        self.assertEqual(text, '{"t": "x"}')
        self.assertEqual(session.count("api.groq.com"), 0)
        self.assertEqual(session.count("generativelanguage"), 1)
        rows = await self.db.ai_usage_recent(limit=10)
        self.assertTrue(
            any(row["provider"] == "gemini" and row["result"] == "success" for row in rows)
        )

    async def test_a_genuine_missing_credential_error_is_still_reported(self):
        # Pruning must not hide the real problem when nothing can answer: the
        # user-facing error stays "no credentials configured", not a silent skip.
        self._relax_billing(ai_max_provider_failovers=0)
        await self._route(["nara", "openrouter"])
        from gamas_bot.structuring import StructuringError

        session = _ScriptedSession()
        job = await self._job()
        with self.assertRaises(StructuringError):
            await job.generate(
                request_type="chunk_structuring",
                task="chunk_structuring",
                system_prompt="S",
                user_text="U",
                session=session,
            )
        self.assertEqual(session.requests, [])
        self.assertIn("no configured credential", job.failures[-1].message)


class RateLimitTests(FailoverCase):
    async def test_long_retry_after_is_a_cooldown_not_a_hot_retry(self):
        self._relax_billing()
        groq_id = await self._paid_credential("groq", label="a", secret="gsk-key-0001")
        await self._paid_credential("gemini", label="g", secret="AIza-test-paid-1234")
        await self._route(["groq", "gemini"])
        session = _ScriptedSession()
        session.add(
            "api.groq.com",
            _Response(
                429,
                {"error": {"message": "rate limited"}},
                {"Retry-After": "600"},
            ),
        )
        session.add("generativelanguage.googleapis.com", _Response(200, _gemini_payload("OK")))
        job = await self._job()
        started = time.perf_counter()
        text = await job.generate(
            request_type="chunk_structuring",
            task="chunk_structuring",
            system_prompt="S",
            user_text="U",
            session=session,
        )
        elapsed = time.perf_counter() - started
        self.assertEqual(text, "OK")
        # One request to the limited provider: the leg neither retried in place
        # nor walked the pool, it moved on to the next provider.
        self.assertEqual(session.count("api.groq.com"), 1)
        self.assertLess(elapsed, 2.0, f"the leg slept {elapsed:.1f}s instead of failing over")
        record = await self.db.provider_credential_record(groq_id)
        # The provider asked for 600s; the credential is cooled down for that,
        # not for the internal retry cap.
        self.assertGreater(cooldown_remaining_seconds(record["cooldown_until"]), 500)

    async def test_service_wide_failure_does_not_walk_the_credential_pool(self):
        # 503 from the provider is not this key's fault, so the remaining keys
        # are not punished (and not spent) one by one on the same leg.
        self._relax_billing()
        for index in range(3):
            await self._paid_credential(
                "groq", label=f"k{index}", secret=f"gsk-key-000{index}"
            )
        await self._paid_credential("gemini", label="g", secret="AIza-test-paid-1234")
        await self._route(["groq", "gemini"])
        session = _ScriptedSession()
        session.add(
            "api.groq.com",
            _Response(503, {"error": {"message": "backend busy"}}),
            _Response(503, {"error": {"message": "backend busy"}}),
            _Response(503, {"error": {"message": "backend busy"}}),
            _Response(503, {"error": {"message": "backend busy"}}),
            _Response(503, {"error": {"message": "backend busy"}}),
        )
        session.add("generativelanguage.googleapis.com", _Response(200, _gemini_payload("OK")))
        job = await self._job()
        text = await job.generate(
            request_type="chunk_structuring",
            task="chunk_structuring",
            system_prompt="S",
            user_text="U",
            session=session,
        )
        self.assertEqual(text, "OK")
        self.assertLessEqual(session.count("api.groq.com"), 2)
        events = await self.db.ai_events_list(event="note_request_fallback")
        self.assertTrue(events)

    async def test_invalid_key_is_quarantined_and_the_next_key_is_tried(self):
        self._relax_billing()
        bad = await self._paid_credential("groq", label="bad", secret="gsk-bad-1111")
        await self._paid_credential("groq", label="good", secret="gsk-good-2222")
        await self._route(["groq"])
        session = _ScriptedSession()
        session.add(
            "api.groq.com",
            _Response(401, {"error": {"message": "invalid api key"}}),
            _Response(200, _notes_payload("FINE")),
        )
        job = await self._job()
        text = await job.generate(
            request_type="chunk_structuring",
            task="chunk_structuring",
            system_prompt="S",
            user_text="U",
            session=session,
        )
        self.assertEqual(text, "FINE")
        record = await self.db.provider_credential_record(bad)
        self.assertTrue(record["quarantined_at"])
        # A rejected key never leaks the provider body or the secret into audit.
        events = await self.db.ai_events_list(event="credential_quarantined")
        self.assertTrue(events)
        self.assertNotIn("gsk-", json.dumps([dict(row) for row in events]))


class LegBudgetTests(FailoverCase):
    async def test_leg_deadline_stops_rotation_and_leaves_room_for_the_next_provider(self):
        self._relax_billing()
        for index in range(2):
            await self._paid_credential(
                "groq", label=f"k{index}", secret=f"gsk-key-100{index}"
            )
        await self._route(["groq"])
        plan = await self.router.plan()
        session = _ScriptedSession()
        # A slow per-key rejection: it is the class of failure that *does* make
        # the pool rotate, so only the leg's wall-clock budget can stop it here.
        session.add(
            "api.groq.com",
            _Response(401, {"error": {"message": "invalid api key"}}, delay=1.9),
            _Response(200, _notes_payload("SHOULD NOT BE REACHED")),
            _Response(200, _notes_payload("SHOULD NOT BE REACHED")),
        )
        failures: list = []
        with self.assertRaises(Exception) as caught:
            await self.router.execute(
                task="chunk_structuring",
                leg=plan.legs[0],
                plan=plan,
                system_prompt="S",
                user_text="U",
                session=session,
                position=0,
                job_id="GMS-000012",
                submission_id=None,
                failure_log=failures,
                deadline=time.perf_counter() + 1.5,
                leg_budget_seconds=60.0,
            )
        self.assertTrue(caught.exception is not None)
        self.assertEqual(session.count("api.groq.com"), 1)
        self.assertTrue(failures)
        self.assertEqual(failures[-1].category, FAIL_TRANSIENT)
        self.assertIn("budget", failures[-1].message)

    async def test_leg_budget_is_bounded_and_never_below_a_useful_floor(self):
        plan = await self.router.plan()
        job = NoteJobSession(self.router, plan, self.settings, job_id="GMS-000013")
        budget = job._leg_budget_seconds()
        self.assertGreaterEqual(budget, 60.0)
        self.assertLessEqual(budget, 600.0)

    async def test_failed_attempts_are_attributed_to_the_provider_that_failed(self):
        # Accountability: the failed leg must be visible in usage/analytics with
        # its own provider and a non-success result, not only in the log text.
        self._relax_billing()
        await self._paid_credential("groq", label="a", secret="gsk-key-2001")
        await self._paid_credential("gemini", label="g", secret="AIza-test-paid-1234")
        await self._route(["groq", "gemini"])
        session = _ScriptedSession()
        session.add("api.groq.com", _Response(503, {"error": {"message": "busy"}}))
        session.add("generativelanguage.googleapis.com", _Response(200, _gemini_payload("OK")))
        job = await self._job()
        await job.generate(
            request_type="chunk_structuring",
            task="chunk_structuring",
            system_prompt="S",
            user_text="U",
            session=session,
        )
        rows = await self.db.ai_usage_recent(limit=20)
        groq = [row for row in rows if row["provider"] == "groq"]
        self.assertTrue(groq)
        self.assertEqual(groq[0]["result"], "failure")
        self.assertTrue(groq[0]["error_class"])
        self.assertEqual(
            [row["provider"] for row in rows if row["result"] == "success"], ["gemini"]
        )

    async def test_reservation_is_released_when_the_note_fails(self):
        # A failed job must not leak a slot in the provider concurrency limiter
        # (a leaked slot eventually wedges every later request).
        self._relax_billing(ai_max_provider_failovers=0)
        await self._paid_credential("groq", label="a", secret="gsk-key-3001")
        await self._route(["groq"])
        session = _ScriptedSession()
        session.add("api.groq.com", _Response(503, {"error": {"message": "busy"}}))
        job = await self._job()
        from gamas_bot.structuring import StructuringError

        with self.assertRaises(StructuringError):
            await job.generate(
                request_type="chunk_structuring",
                task="chunk_structuring",
                system_prompt="S",
                user_text="U",
                session=session,
            )
        # The next job on the same router must still get a slot: a reservation
        # leaked by the failure would wedge every later request behind it.
        retry = _ScriptedSession()
        retry.add("api.groq.com", _Response(200, _notes_payload("NEXT JOB")))
        second = await self._job()
        text = await asyncio.wait_for(
            second.generate(
                request_type="chunk_structuring",
                task="chunk_structuring",
                system_prompt="S",
                user_text="U",
                session=retry,
            ),
            timeout=5,
        )
        self.assertEqual(text, "NEXT JOB")
