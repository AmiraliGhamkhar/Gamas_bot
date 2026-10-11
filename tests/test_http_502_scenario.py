"""Mandatory HTTP 502 scenario, end to end, in routed and legacy mode.

A 502 is the failure that hides the most: it is retryable, it is *provider-wide*
rather than key-specific, and it can arrive after a perfectly good transcript was
already paid for. This module drives the real note pipeline against a scripted
gateway that answers 502, and records the answers to the questions the audit has
to be able to answer for every failure class:

1. which route was planned and why (leg order, free-only flags, skips);
2. which provider and model were selected;
3. which credentials were eligible;
4. how the retry budget was computed (profile retries, ``NOTE_API_RETRIES``,
   ``AI_MAX_GENERATION_RETRIES``) and how many requests it actually spent;
5. whether key rotation and provider failover happened (a 5xx must not be blamed
   on one key, but the route must still move on);
6. whether the recorded metadata (provider, canonical, model, request type,
   route position, status, error class) is accurate and free of secrets and
   transcript content;
7. whether the final result is a success, a controlled fallback, or a failure;
8. whether reservations were finalized or released correctly.

Everything below is deterministic and offline: no API key, no network, no
billable request.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from support import make_settings

from gamas_bot.ai.models import FREE_PLAN, ModelInfo, ModelRegistry
from gamas_bot.ai.profiles import profile_for
from gamas_bot.ai.routing import (
    AllProvidersFailedError,
    NoteJobSession,
    ProviderRouter,
    job_session_scope,
)
from gamas_bot.ai.usage import AIUsageTracker
from gamas_bot.database import Database, utc_now
from gamas_bot.provider_credentials import (
    ProviderCredentialManager,
    use_provider_credentials,
)
from gamas_bot.structuring import (
    ProviderHTTPError,
    StructuringError,
    _structure_chunk,
    structure_transcript,
)

#: Distinctive strings so a leak into metadata or an error message is provable.
NARA_SECRET = "sk-nara-502-scenario-0001"
GROQ_SECRET = "gsk-502-scenario-0002"
TRANSCRIPT_MARKER = "MARKER-پاراگراف-محرم"
TRANSCRIPT = (
    f"{TRANSCRIPT_MARKER} هورمون تیروئید سوخت‌وساز پایه را تنظیم می‌کند. "
    "کم‌کاری تیروئید با TSH بالا و T4 آزاد پایین شناخته می‌شود. "
    "درمان با لووتیروکسین ۵۰ میکروگرم آغاز می‌شود. "
) * 3

NOTES_JSON = json.dumps(
    {
        "title": "غدد درون‌ریز",
        "summary": "مروری بر تنظیم هورمونی و محور هیپوفیز.",
        "sections": [
            {
                "heading": "تنظیم تیروئید",
                "paragraphs": ["هیپوتالاموس با ترشح TRH، هیپوفیز را تنظیم می‌کند."],
                "key_points": ["TSH بالا با T4 پایین = کم‌کاری اولیه"],
            }
        ],
        "glossary": [{"term": "TSH", "definition": "هورمون محرک تیروئید"}],
    },
    ensure_ascii=False,
)


class _Response:
    def __init__(self, status: int, payload=None, headers=None):
        self.status = status
        self.headers = headers or {}
        self._payload = payload

    async def read(self):
        if isinstance(self._payload, bytes):
            return self._payload
        return json.dumps(self._payload).encode("utf-8")

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class ScriptedSession:
    """Replays ``(host_substring, responses)`` rules and records every request."""

    def __init__(self, rules):
        self.rules = {host: list(responses) for host, responses in rules}
        self.requests: list[dict] = []

    def post(self, url, headers=None, params=None, json=None, timeout=None, data=None):
        self.requests.append({"url": url, "headers": headers or {}, "json": json})
        for host, responses in self.rules.items():
            if host in url:
                if not responses:
                    raise AssertionError(f"script exhausted for {host}")
                response = responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f"unexpected request: {url}")

    def requests_for(self, host: str) -> list[dict]:
        return [request for request in self.requests if host in request["url"]]

    async def close(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()
        return False


def _bad_gateway(detail: str = "upstream gateway failure") -> _Response:
    return _Response(502, {"error": {"message": detail, "status": "UNAVAILABLE"}})


def _chat(payload_text: str, model: str = "m") -> _Response:
    return _Response(
        200,
        {
            "id": "chatcmpl-502",
            "model": model,
            "choices": [{"message": {"content": payload_text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 300, "total_tokens": 800},
        },
    )


class _SessionPatch:
    """Swap ``aiohttp.ClientSession`` for a scripted double during one scenario."""

    def __init__(self, session):
        self.session = session
        self._original = None

    def __enter__(self):
        import gamas_bot.structuring as structuring

        self._original = structuring.aiohttp.ClientSession
        structuring.aiohttp.ClientSession = lambda *a, **k: self.session  # type: ignore[assignment]
        return self.session

    def __exit__(self, *exc):
        import gamas_bot.structuring as structuring

        structuring.aiohttp.ClientSession = self._original  # type: ignore[assignment]
        return False


def _settings(root: Path, **overrides):
    base = {
        "database_path": root / "notes.sqlite3",
        "session_path": root / "session",
        "temp_dir": root / "tmp",
        "provider_credentials_encryption_key": Fernet.generate_key().decode("ascii"),
        "admin_ids": frozenset({1}),
        "note_api_provider": "openai_compatible",
        "note_api_base_url": "https://router.bynara.id/v1",
        "note_api_model": "agnes-3-flash",
        "note_api_key": NARA_SECRET,
        # One retry: the budget is small enough to assert exactly, and the
        # production default (2) is only a cap the profile can also lower.
        "note_api_retries": 1,
    }
    base.update(overrides)
    return make_settings(**base)


class Routed502Scenario(unittest.IsolatedAsyncioTestCase):
    """The 502 path with ``AI_ROUTING_ENABLED`` on (the platform path)."""

    async def asyncSetUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.settings = _settings(self.root)
        self.db = Database(self.root / "notes.sqlite3")
        await self.db.open()
        self.manager = ProviderCredentialManager(self.db, self.settings)
        self.tracker = AIUsageTracker(self.db)
        self.models = ModelRegistry(self.db, self.settings)
        self.router = ProviderRouter(
            self.db, self.settings, self.manager, self.tracker, self.models
        )
        # A free/no-overage attested key per provider, exactly as an administrator
        # must do after migration 007: an environment key is never auto-attested.
        self.nara_key = await self.manager.add_credential(
            service="notes", provider="nara", label="nara-primary",
            secret=NARA_SECRET, model="agnes-3-flash", admin_id=1,
        )
        await self.db.set_provider_credential_billing_attestation(
            self.nara_key, state="free", admin_id=1
        )
        self.groq_key = await self.manager.add_credential(
            service="notes", provider="groq", label="groq-backup",
            secret=GROQ_SECRET, model="qwen/qwen3.8-27b", admin_id=1,
        )
        await self.db.set_provider_credential_billing_attestation(
            self.groq_key, state="free", admin_id=1
        )
        # Account-owned catalogs, as a successful sync would leave them.
        await self.models.apply_discovery(
            "nara", [ModelInfo(provider="nara", model_id="agnes-3-flash",
                               free_status=FREE_PLAN, source="live:/v1/models")]
        )
        await self.models.apply_discovery(
            "groq", [ModelInfo(provider="groq", model_id="qwen/qwen3.8-27b",
                               source="live:/v1/models")]
        )
        await self.db.ai_provider_settings_upsert(
            "groq", admin_id=1, account_entitlement_attested_at=utc_now(),
            account_entitlement_attested_by_admin_id=1,
        )
        # NaraRouter first (the deployment preference), Groq as the next leg.
        await self.db.ai_routes_replace(
            "notes", "chunk_structuring",
            [
                {"provider": "nara", "enabled": 1, "free_only": 1},
                {"provider": "groq", "enabled": 1, "free_only": 1},
            ],
            admin_id=None,
        )
        self.router.invalidate_cache()
        self.plan = await self.router.plan()

    async def asyncTearDown(self):
        await self.db.close()
        self._temp.cleanup()

    def _retry_budget(self) -> int:
        """Attempts one leg may spend, from the same three sources the router uses."""
        profile = profile_for("nara")
        retries = profile.policy.max_retries
        retries = min(retries, max(0, int(self.settings.note_api_retries)))
        override = self.settings.ai_max_generation_retries
        if override is not None and int(override) >= 0:
            retries = min(retries, int(override))
        return retries + 1

    async def _run(self, session: ScriptedSession, job_id: str = "GMS-5020001"):
        job = NoteJobSession(self.router, self.plan, self.settings, job_id=job_id)
        # Real backoff would only slow the suite down; recording the sleep keeps
        # the delay itself under test.
        sleeps: list[float] = []

        async def _sleep(delay, *args, **kwargs):
            sleeps.append(float(delay))

        with patch("gamas_bot.ai.routing.asyncio.sleep", new=_sleep), job_session_scope(
            job
        ), _SessionPatch(session):
            notes = await structure_transcript(TRANSCRIPT, self.settings, mode="full")
        return job, notes, sleeps

    # -- 1..5: route, provider/model, credentials, budget, failover ------------

    async def test_routed_502_retries_the_leg_then_fails_over_to_the_next_provider(self):
        budget = self._retry_budget()
        self.assertEqual(budget, 2, "NOTE_API_RETRIES must bound the leg's retry budget")
        session = ScriptedSession(
            [
                ("router.bynara.id", [_bad_gateway() for _ in range(budget)]),
                ("api.groq.com", [_chat(NOTES_JSON, "qwen/qwen3.8-27b")]),
            ]
        )
        job, notes, sleeps = await self._run(session)

        # (1) The planned route: the deployment's NaraRouter preference first,
        # Groq second, both restricted to free-only legs.
        self.assertEqual([leg.canonical for leg in self.plan.legs], ["nara", "groq"])
        self.assertTrue(all(leg.free_only for leg in self.plan.legs))

        # (2)+(4) The dead leg spent exactly its retry budget, with backoff
        # between attempts (not a hot loop), and then the route moved on.
        self.assertEqual(len(session.requests_for("router.bynara.id")), budget)
        self.assertEqual(len(sleeps), budget - 1)
        self.assertTrue(all(delay > 0 for delay in sleeps), sleeps)

        # (5) Failover, not key rotation: Groq was asked once and answered.
        self.assertEqual(len(session.requests_for("api.groq.com")), 1)
        self.assertEqual(job.fallbacks, 1)

        # (7) The user still gets notes.
        self.assertTrue(notes.has_content)
        self.assertIn("TSH", json.dumps(notes.to_payload(), ensure_ascii=False))

    async def test_routed_502_does_not_blame_the_key_or_quarantine_it(self):
        session = ScriptedSession(
            [
                ("router.bynara.id", [_bad_gateway() for _ in range(2)]),
                ("api.groq.com", [_chat(NOTES_JSON, "qwen/qwen3.8-27b")]),
            ]
        )
        await self._run(session, job_id="GMS-5020002")
        record = await self.db.provider_credential_record(self.nara_key)
        # A 5xx is the provider's, so the key keeps its healthy state...
        self.assertIsNone(record["quarantined_at"])
        self.assertIsNone(record["cooldown_until"])
        # ...but the failure is still visible to an operator.
        self.assertEqual(record["last_status_code"], 502)
        self.assertGreaterEqual(int(record["failure_streak"] or 0), 1)
        # The key that served the request is back to healthy.
        healthy = await self.db.provider_credential_record(self.groq_key)
        self.assertIsNotNone(healthy["last_success_at"])
        self.assertEqual(healthy["failure_streak"], 0)

    async def test_routed_502_metadata_is_exact_and_accounted(self):
        session = ScriptedSession(
            [
                ("router.bynara.id", [_bad_gateway() for _ in range(2)]),
                ("api.groq.com", [_chat(NOTES_JSON, "qwen/qwen3.8-27b")]),
            ]
        )
        job, notes, _sleeps = await self._run(session, job_id="GMS-5020003")

        failures = await self.db.ai_usage_recent(limit=50, provider="nara", failures_only=True)
        self.assertEqual(len(failures), 2, "both 502 attempts must be accounted")
        for row in failures:
            # (6) provider, canonical, model, request type, position and status.
            self.assertEqual(row["provider"], "nara")
            self.assertEqual(row["canonical"], "nara")
            self.assertEqual(row["model"], "agnes-3-flash")
            self.assertEqual(row["request_type"], "chunk_structuring")
            self.assertEqual(row["route_position"], 0)
            self.assertEqual(row["http_status"], 502)
            self.assertEqual(row["error_class"], "server")
            self.assertEqual(row["job_id"], "GMS-5020003")
            self.assertEqual(row["credential_id"], self.nara_key)
            self.assertIsNone(row["total_tokens"], "a 502 has no usage to report")
        successes = [
            row for row in await self.db.ai_usage_recent(limit=50, provider="groq")
            if row["result"] == "success"
        ]
        self.assertEqual(len(successes), 1)
        self.assertEqual(successes[0]["route_position"], 1)
        self.assertEqual(successes[0]["model"], "qwen/qwen3.8-27b")
        self.assertEqual(successes[0]["http_status"], 200)

        events = await self.db.ai_events_list(limit=100, job_id="GMS-5020003")
        # The 502 events carry the status, and the leg that ran out of retries
        # is logged as a failure rather than disappearing after its warning.
        self.assertEqual(
            sorted(e["event"] for e in events if e.get("http_status") == 502),
            ["note_request_failed", "note_request_retry"],
        )
        terminal = [e for e in events if e["event"] == "note_request_failed"]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["provider"], "nara")
        self.assertEqual(terminal[0]["canonical"], "nara")
        self.assertEqual(terminal[0]["model"], "agnes-3-flash")
        self.assertEqual(terminal[0]["request_type"], "chunk_structuring")
        self.assertEqual(terminal[0]["route_position"], 0)
        self.assertEqual(terminal[0]["error_class"], "server")
        self.assertEqual(terminal[0]["http_status"], 502)
        self.assertEqual(terminal[0]["job_id"], "GMS-5020003")
        self.assertIn("note_request_fallback", [e["event"] for e in events])
        self.assertIn("provider_route_changed", [e["event"] for e in events])

        # No secret and no transcript content anywhere in the recorded metadata.
        blob = json.dumps([dict(e) for e in events], ensure_ascii=False) + json.dumps(
            [dict(r) for r in await self.db.ai_usage_recent(limit=100)], ensure_ascii=False
        )
        for secret in (NARA_SECRET, GROQ_SECRET):
            self.assertNotIn(secret, blob)
        self.assertNotIn(TRANSCRIPT_MARKER, blob)
        self.assertNotIn("502-scenario", blob)

    async def test_routed_all_providers_502_is_a_controlled_secret_free_failure(self):
        session = ScriptedSession(
            [
                ("router.bynara.id", [_bad_gateway() for _ in range(4)]),
                ("api.groq.com", [_bad_gateway() for _ in range(4)]),
            ]
        )
        with self.assertRaises(AllProvidersFailedError) as raised:
            await self._run(session, job_id="GMS-5020004")
        error = raised.exception
        # (7) With every leg dead there is no single provider to blame, so the
        # aggregate error is raised — and it stays a StructuringError, which is
        # the contract bot.py relies on to deliver the raw transcript with an
        # honest notice instead of losing the lecture.
        self.assertIsInstance(error, StructuringError)
        self.assertIn(
            ("server", 502, "nara"),
            [(f.category, f.http_status, f.provider) for f in error.failures],
        )
        message = str(error)
        self.assertNotIn(NARA_SECRET, message)
        self.assertNotIn(GROQ_SECRET, message)
        self.assertNotIn(TRANSCRIPT_MARKER, message)
        # (4)/(5) Each leg spent at most one attempt plus its one retry: the
        # retry budget is not multiplied by the number of route positions.
        self.assertEqual(len(session.requests_for("router.bynara.id")), 2)
        groq_requests = len(session.requests_for("api.groq.com"))
        self.assertLessEqual(groq_requests, 2)
        events = await self.db.ai_events_list(limit=100, job_id="GMS-5020004")
        names = [e["event"] for e in events]
        # The exhausted leg is a recorded failure, not just a retry warning.
        self.assertTrue(
            [
                e
                for e in events
                if e["event"] == "note_request_failed" and e.get("http_status") == 502
            ],
            "the terminal 502 must be recorded as a failure",
        )
        if groq_requests == 1:
            # The local per-provider budget refused the retry before it reached
            # the network: the skip must be explained in the log, not billed.
            self.assertIn("provider_quota_blocked", names)
        blob = json.dumps([dict(e) for e in events], ensure_ascii=False) + json.dumps(
            [dict(r) for r in await self.db.ai_usage_recent(limit=100)], ensure_ascii=False
        )
        for secret in (NARA_SECRET, GROQ_SECRET):
            self.assertNotIn(secret, blob)
        self.assertNotIn(TRANSCRIPT_MARKER, blob)

    async def test_routed_502_releases_every_local_reservation_it_took(self):
        session = ScriptedSession(
            [
                ("router.bynara.id", [_bad_gateway() for _ in range(2)]),
                ("api.groq.com", [_chat(NOTES_JSON, "qwen/qwen3.8-27b")]),
            ]
        )
        await self._run(session, job_id="GMS-5020005")
        # (8) The per-request provider budget reservations are released even on
        # the failed attempts: a 502 must not leak a local budget slot.
        self.assertEqual(self.router._budget_reservations, {})


class Legacy502Scenario(unittest.IsolatedAsyncioTestCase):
    """The same failure with ``AI_ROUTING_ENABLED=false`` (the compat path)."""

    async def asyncSetUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.manager = None

    async def asyncTearDown(self):
        self._temp.cleanup()

    def _session_double(self, responses):
        class _Session:
            def post(self, url, headers=None, params=None, json=None, timeout=None):
                return responses.pop(0)

            async def close(self):
                return None

        return _Session()

    async def _run_legacy(self, responses, *, note_api_retries=1, **overrides):
        settings = _settings(
            self.root, ai_routing_enabled=False, note_api_retries=note_api_retries, **overrides
        )
        session = self._session_double(list(responses))
        seen: list[dict] = []

        class RecordingSession:
            def post(self, url, headers=None, params=None, json=None, timeout=None):
                seen.append({"url": url, "headers": headers or {}})
                return session.post(url, headers=headers, params=params, json=json)

            async def close(self):
                return None

        sleeps: list[float] = []

        async def _sleep(delay, *args, **kwargs):
            sleeps.append(float(delay))

        recording = RecordingSession()
        with patch("gamas_bot.structuring.asyncio.sleep", new=_sleep), _SessionPatch(
            recording
        ):
            result = await _structure_chunk(TRANSCRIPT, settings, recording)
        return result, seen, sleeps

    async def test_legacy_502_is_retried_inside_the_configured_budget_then_succeeds(self):
        result, seen, sleeps = await self._run_legacy(
            [_bad_gateway(), _chat(NOTES_JSON)]
        )
        self.assertIn("TSH", result)
        # One retry budget: the 502, then the success. Not an unbounded storm.
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(sleeps), 1)
        self.assertGreater(sleeps[0], 0)

    async def test_legacy_502_exhausting_the_budget_reports_the_provider_status(self):
        with self.assertRaises(ProviderHTTPError) as raised:
            await self._run_legacy([_bad_gateway() for _ in range(4)])
        self.assertEqual(raised.exception.status, 502)
        self.assertNotIn(NARA_SECRET, str(raised.exception))

    async def test_legacy_502_rots_across_a_multi_key_pool_without_a_retry_storm(self):
        manager = ProviderCredentialManager(
            Database(self.root / "legacy.sqlite3"), _settings(self.root)
        )
        await manager.db.open()
        self.manager = manager
        first = await manager.add_credential(
            service="notes", provider="gemini", label="primary",
            secret="gemini-primary-502", admin_id=1, priority=1,
        )
        second = await manager.add_credential(
            service="notes", provider="gemini", label="backup",
            secret="gemini-backup-502", admin_id=1, priority=2,
        )
        responses = [_bad_gateway() for _ in range(6)]
        seen: list[dict] = []

        class RecordingSession:
            def post(self, url, headers=None, params=None, json=None, timeout=None):
                seen.append({"url": url, "headers": headers or {}})
                return responses.pop(0)

            async def close(self):
                return None

        settings = _settings(
            self.root,
            ai_routing_enabled=False,
            note_api_retries=1,
            note_api_provider="gemini",
            note_api_key=None,
            gemini_api_key=None,
        )
        recording = RecordingSession()

        async def _sleep(*_args, **_kwargs):
            return None

        with patch("gamas_bot.structuring.asyncio.sleep", new=_sleep), use_provider_credentials(
            manager
        ), _SessionPatch(recording):
            with self.assertRaises(ProviderHTTPError) as raised:
                await _structure_chunk(TRANSCRIPT, settings, recording)
        self.assertEqual(raised.exception.status, 502)
        # Both keys were probed (rotation preserved), and each candidate gets
        # its own configured attempt budget: two keys with NOTE_API_RETRIES=1
        # cost four requests and no more. A 502 is the provider's fault, so the
        # total stays bounded by pool size x attempts instead of looping.
        self.assertEqual(len(seen), 4, f"requests={len(seen)}")
        self.assertLessEqual(len(seen), 2 * (settings.note_api_retries + 1))
        self.assertNotIn("gemini-primary-502", str(raised.exception))
        records = {row["id"]: row for row in await manager.list_summaries()}
        self.assertEqual(records[first]["last_status_code"], 502)
        self.assertEqual(records[second]["last_status_code"], 502)
        # A provider error is not a key problem: no quarantine, no cooldown.
        for credential_id in (first, second):
            self.assertIsNone(records[credential_id]["quarantined_at"])
            self.assertIsNone(records[credential_id]["cooldown_until"])
        await self.manager.db.close()


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
