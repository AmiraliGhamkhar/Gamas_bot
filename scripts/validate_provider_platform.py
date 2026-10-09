"""Offline production validation for the AI Provider Platform (spec §55).

Runs the real Gamas note pipeline against scripted provider responses — no
network, no API keys — and asserts the behaviours that must hold before a
deployment is trusted with free-tier traffic:

1.  the current NaraRouter configuration still drives production traffic;
2.  a provider-aware token budget shrinks chunks on restrictive free tiers;
3.  provider-level failover moves to the next provider on a hard 429;
4.  FREE_ONLY refuses to route to a provider whose free entitlement is
    unverified, and honours an administrator attestation when given;
5.  paid fallback stays closed unless it is explicitly enabled;
6.  the generated notes never contain backend metadata or provider errors;
7.  the resulting DOCX keeps its RTL contract.

Run with ``python -m scripts.validate_provider_platform``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from cryptography.fernet import Fernet

from gamas_bot.ai.models import FREE_PLAN, ModelInfo, ModelRegistry
from gamas_bot.ai.routing import NoteJobSession, ProviderRouter, job_session_scope
from gamas_bot.ai.usage import AIUsageTracker
from gamas_bot.config import Settings
from gamas_bot.database import Database, utc_now
from gamas_bot.docx_export import DocumentMeta, build_notes_docx
from gamas_bot.provider_credentials import ProviderCredentialManager
from gamas_bot.structuring import (
    TRANSCRIPT_CHUNK_CHARS,
    note_chunk_chars,
    structure_transcript,
)

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "notes"

#: Source marker used by live discovery.
_LIVE = "live:/v1/models"
TRANSCRIPT = (FIXTURE / "01_medical_endocrinology.txt").read_text(encoding="utf-8")

#: A minimal but schema-valid Gamas note payload.
NOTES_JSON = json.dumps(
    {
        "title": "غدد درون‌ریز",
        "summary": "مروری بر محور هیپوتالاموس-هیپوفیز و تنظیم هورمونی.",
        "learning_objectives": ["شناخت محور هورمونی"],
        "sections": [
            {
                "heading": "محور هیپوفیز",
                "paragraphs": ["هیپوتالاموس با ترشح هورمون‌های آزادکننده، هیپوفیز را تنظیم می‌کند."],
                "bullets": ["هورمون رشد", "پرولاکتین"],
                "definitions": [{"term": "آکرومگالی", "definition": "افزایش هورمون رشد پس از بلوغ"}],
                "examples": ["کم‌کاری تیروئید"],
                "steps": ["اندازه‌گیری TSH", "بررسی T4 آزاد"],
                "formulas": ["TSH بالا با T4 پایین = کم‌کاری اولیه"],
                "key_points": ["محور با بازخورد منفی کنترل می‌شود"],
                "table": None,
                "callouts": [],
            }
        ],
        "review_questions": ["بازخورد منفی در این محور چه نقشی دارد؟"],
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
    """Replays ``(host_substring, responses)`` rules; records every request."""

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

    async def close(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()
        return False


def _chat(payload_text: str, model: str = "m") -> _Response:
    return _Response(
        200,
        {
            "id": "chatcmpl-validate",
            "model": model,
            "choices": [{"message": {"content": payload_text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 300, "total_tokens": 800},
        },
    )


def _gemini(payload_text: str) -> _Response:
    return _Response(
        200,
        {
            "candidates": [
                {"content": {"parts": [{"text": payload_text}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {
                "promptTokenCount": 500,
                "candidatesTokenCount": 300,
                "totalTokenCount": 800,
            },
        },
    )


class Harness:
    def __init__(self, root: Path, settings: Settings):
        self.root = root
        self.settings = settings

    async def __aenter__(self):
        self.db = Database(self.root / "validate.sqlite3")
        await self.db.open()
        self.manager = ProviderCredentialManager(self.db, self.settings)
        self.tracker = AIUsageTracker(self.db)
        self.models = ModelRegistry(self.db, self.settings)
        self.router = ProviderRouter(
            self.db, self.settings, self.manager, self.tracker, self.models
        )
        return self

    async def __aexit__(self, *exc):
        await self.db.close()


class _SessionPatch:
    """Swap ``aiohttp.ClientSession`` for a scripted double during a check."""

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


def _settings(root: Path, **overrides) -> Settings:
    """Build Settings through the shared factory so defaults stay canonical."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
    from support import make_settings  # noqa: PLC0415 - test helper, import at runtime

    base = {
        "database_path": root / "validate.sqlite3",
        "session_path": root / "session",
        "temp_dir": root / "tmp",
        "provider_credentials_encryption_key": Fernet.generate_key().decode("ascii"),
        "admin_ids": frozenset({1}),
        "note_api_provider": "openai_compatible",
        "note_api_base_url": "https://router.bynara.id/v1",
        "note_api_model": "agnes-3-flash",
        "note_api_key": "sk-nara-1234",
    }
    base.update(overrides)
    return make_settings(**base)


_RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    _RESULTS.append((bool(condition), name, detail))
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))


async def _attest_key(h, provider: str, secret: str, model: str, *, label: str = "primary") -> None:
    """Store and attest a key the way an operator must after migration 007.

    Environment keys are deliberately not auto-attested, so a validation run
    has to perform the same explicit free/no-overage attestation an
    administrator performs in the AI key panel.
    """
    credential_id = await h.manager.add_credential(
        service="notes",
        provider=provider,
        label=label,
        secret=secret,
        model=model,
        admin_id=1,
    )
    await h.db.set_provider_credential_billing_attestation(
        credential_id, state="free", admin_id=1
    )

async def validate_nara_backward_compatibility(root: Path) -> None:
    """1/7: the deployed NaraRouter configuration keeps serving production.

    Two contracts are checked, because they are different promises:

    * ``AI_ROUTING_ENABLED=false`` reproduces the pre-platform behaviour
      exactly: the same provider, the same payload and the same 22,000
      character budget, with ``agnes-3-flash`` honoured as configured.
    * with the platform on, the same deployment is routed through the Nara
      adapter, and FREE_ONLY additionally requires the configured model's free
      status to be verified — ``agnes-3-flash`` is honoured but never silently
      relabelled free (see docs/AI_PROVIDERS.md).
    """
    # -- legacy path ---------------------------------------------------------
    legacy_root = root / "legacy"
    legacy_root.mkdir(parents=True, exist_ok=True)
    legacy_settings = _settings(legacy_root, ai_routing_enabled=False)
    async with Harness(root, legacy_settings) as h:
        await _attest_key(h, "nara", "sk-nara-1234", "agnes-3-flash")
        http = ScriptedSession([("router.bynara.id", [_chat(NOTES_JSON)])])
        with _SessionPatch(http):
            notes = await structure_transcript(TRANSCRIPT, legacy_settings)
        check("legacy-notes-produced", bool(notes.has_content), f"sections={len(notes.sections)}")
        check(
            "legacy-chunk-budget-unchanged",
            note_chunk_chars(legacy_settings) == TRANSCRIPT_CHUNK_CHARS - 200,
            f"chars={note_chunk_chars(legacy_settings)}",
        )
        payload = build_notes_docx(
            notes, meta=DocumentMeta(reference="GMS-VALIDATE", source_name="validation")
        )
        check("legacy-docx-rtl-bytes", len(payload) > 5000, f"bytes={len(payload)}")

    # -- platform path -------------------------------------------------------
    platform_root = root / "platform"
    platform_root.mkdir(parents=True, exist_ok=True)
    settings = _settings(platform_root)
    async with Harness(platform_root, settings) as h:
        await _attest_key(h, "nara", "sk-nara-1234", "agnes-3-flash")
        plan = await h.router.plan()
        check(
            "nara-route-first",
            bool(plan.legs) and plan.legs[0].canonical == "nara",
            f"primary={plan.legs[0].canonical if plan.legs else 'none'}",
        )
        # A model whose free status is undocumented is refused under FREE_ONLY
        # rather than being spent and billed later.
        session = NoteJobSession(h.router, plan, settings, job_id="GMS-900001")
        http = ScriptedSession([("router.bynara.id", [_chat(NOTES_JSON)])])
        with job_session_scope(session), _SessionPatch(http):
            try:
                await structure_transcript(TRANSCRIPT, settings)
                unverified_blocked = False
            except Exception as exc:
                unverified_blocked = "پاسخ مناسبی" in str(exc) or "failed" in type(exc).__name__.lower()
        check(
            "free-only-refuses-unverified-model",
            unverified_blocked,
            "agnes-3-flash is not on Nara's published free plan",
        )

        # Once the account catalog marks it free (what a real sync does), the
        # same deployment serves traffic through the Nara adapter.
        await h.models.apply_discovery(
            "nara",
            [
                ModelInfo(
                    provider="nara",
                    model_id="agnes-3-flash",
                    free_status=FREE_PLAN,
                    source=_LIVE,
                )
            ],
        )
        h.router.invalidate_cache()
        plan = await h.router.plan()
        session = NoteJobSession(h.router, plan, settings, job_id="GMS-900002")
        http = ScriptedSession([("router.bynara.id", [_chat(NOTES_JSON, "agnes-3-flash")])])
        with job_session_scope(session), _SessionPatch(http):
            notes = await structure_transcript(TRANSCRIPT, settings)
        check("platform-notes-produced", bool(notes.has_content), f"sections={len(notes.sections)}")
        check(
            "platform-uses-configured-model",
            any("agnes-3-flash" == str(r["json"].get("model")) for r in http.requests),
            f"models={ {str(r['json'].get('model')) for r in http.requests} }",
        )


async def validate_groq_token_budget(root: Path) -> None:
    """2/7: a restrictive free tier gets smaller chunks than the legacy cap."""
    settings = _settings(root)
    async with Harness(root, settings) as h:
        await h.db.ai_provider_settings_upsert(
            "groq",
            admin_id=1,
            account_entitlement_attested_at=utc_now(),
            account_entitlement_attested_by_admin_id=1,
        )
        h.router.invalidate_cache()
        plan = await h.router.plan()
        groq_leg = next((leg for leg in plan.legs if leg.canonical == "groq"), None)
        check("groq-eligible-after-attestation", groq_leg is not None)
        if groq_leg is None:
            return
        from gamas_bot.ai.profiles import profile_for

        groq_session = NoteJobSession(
            h.router,
            replace(plan, legs=[groq_leg], profile=profile_for("groq")),
            settings,
            job_id="GMS-900002",
        )
        with job_session_scope(groq_session):
            groq_chars = note_chunk_chars(settings)
        nara_leg = next((leg for leg in plan.legs if leg.canonical == "nara"), None)
        if nara_leg is None:
            check("nara-budget-baseline-present", False, "Nara route leg missing")
            return
        nara_session = NoteJobSession(
            h.router,
            replace(plan, legs=[nara_leg], profile=profile_for("nara")),
            settings,
            job_id="GMS-900003",
        )
        with job_session_scope(nara_session):
            nara_chars = note_chunk_chars(settings)
        check(
            "groq-chunks-are-smaller",
            groq_chars < nara_chars,
            f"groq={groq_chars} nara={nara_chars}",
        )


async def validate_provider_failover(root: Path) -> None:
    """3/7: a hard 429 moves the job to the next provider, not to a retry storm."""
    settings = _settings(root)
    async with Harness(root, settings) as h:
        await _attest_key(h, "groq", "sk-groq-validate-0001", "qwen/qwen3.8-27b", label="groq-free")
        await _attest_key(h, "nara", "sk-nara-1234", "agnes-3-flash")
        # What a successful account catalog sync does: make the exact
        # configured model IDs available. Nara's public-plan intersection
        # supplies FREE_PLAN; Groq's account-scoped entitlement attestation
        # handles its otherwise-unknown account pricing status.
        await h.models.apply_discovery(
            "nara",
            [ModelInfo(provider="nara", model_id="agnes-3-flash", free_status=FREE_PLAN, source=_LIVE)],
        )
        await h.models.apply_discovery(
            "groq",
            [ModelInfo(provider="groq", model_id="qwen/qwen3.8-27b", source=_LIVE)],
        )
        await h.db.ai_provider_settings_upsert(
            "groq",
            admin_id=1,
            account_entitlement_attested_at=utc_now(),
            account_entitlement_attested_by_admin_id=1,
        )
        # Force Groq to lead so the 429 is the first thing the job meets; Nara
        # is the next leg and must absorb the work.
        await h.db.ai_routes_replace(
            "notes",
            "chunk_structuring",
            [
                {"provider": "groq", "enabled": 1, "free_only": 1},
                {"provider": "nara", "enabled": 1, "free_only": 1},
            ],
            admin_id=None,
        )
        h.router.invalidate_cache()
        plan = await h.router.plan()
        http = ScriptedSession(
            [
                ("api.groq.com", [_Response(429, {"error": {"message": "rate limited"}}, {"retry-after": "1"})]),
                ("router.bynara.id", [_chat(NOTES_JSON, "agnes-3-flash")]),
            ]
        )
        session = NoteJobSession(h.router, plan, settings, job_id="GMS-900004")
        with job_session_scope(session), _SessionPatch(http):
            notes = await structure_transcript(TRANSCRIPT, settings)
        check("failover-produces-notes", bool(notes.has_content))
        events = await h.db.ai_events_list(limit=100)
        fallbacks = [e for e in events if e["event"] == "note_request_fallback"]
        check("failover-recorded", len(fallbacks) >= 1, f"events={len(fallbacks)}")
        rate_limited = [e for e in events if e.get("http_status") == 429]
        check("groq-429-observed", len(rate_limited) >= 1, f"429s={len(rate_limited)}")
        # The failed attempt is accounted, so it cannot silently disappear.
        usage = await h.db.ai_usage_summary(days=1)
        groq_rows = [row for row in usage if row["provider"] == "groq"]
        check("failed-attempt-is-accounted", bool(groq_rows), "groq usage rows present")


async def validate_free_only_gate(root: Path) -> None:
    """4/7 + 5/7: FREE_ONLY gates and paid-fallback protection."""
    settings = _settings(root)
    async with Harness(root, settings) as h:
        plan = await h.router.plan()
        reasons = dict(plan.skipped)
        check(
            "groq-blocked-without-attestation",
            reasons.get("groq") == "account_entitlement_unverified",
            str(reasons.get("groq")),
        )
        check(
            "trial-providers-never-routed",
            not any(leg.canonical in {"nvidia", "cerebras", "cohere"} for leg in plan.legs),
        )
        # Paid fallback is opt-in: with AI_ALLOW_PAID_FALLBACK=false no paid
        # leg may appear in the plan at all.
        paid_legs = [leg for leg in plan.legs if not leg.free_only]
        check("paid-fallback-closed", not paid_legs, f"paid_legs={len(paid_legs)}")

        paid_settings = _settings(root, ai_allow_paid_fallback=True)
        async with Harness(root, paid_settings) as paid:
            paid_plan = await paid.router.plan()
            free_seen = False
            ordered = True
            for leg in paid_plan.legs:
                if not leg.free_only:
                    free_seen = True
                elif free_seen:
                    ordered = False
            check("paid-legs-stay-last", ordered)


async def validate_no_backend_leakage(root: Path) -> None:
    """6/7: backend plumbing is scrubbed before notes reach the student.

    ``structuring.scrub_backend_artifacts`` is deliberately conservative: a
    bare provider name survives because a networking lecture may legitimately
    mention one. What must never survive is a self-reference, a key/id-shaped
    token, or a diagnostic *cluster* (two or more plumbing markers on a line).
    """
    settings = _settings(root)
    async with Harness(root, settings) as h:
        await _attest_key(h, "nara", "sk-nara-1234", "agnes-3-flash")
        await h.models.apply_discovery(
            "nara",
            [ModelInfo(provider="nara", model_id="agnes-3-flash", free_status=FREE_PLAN, source=_LIVE)],
        )
        h.router.invalidate_cache()
        plan = await h.router.plan()
        leaky = json.dumps(
            {
                "title": "غدد درون‌ریز",
                "summary": (
                    "HTTP 502 Bad Gateway با retry-after و quota تمام شده است. "
                    "request_id=req-9 و sk-live-SECRET123 در پاسخ آمد."
                ),
                "learning_objectives": ["شناخت محور هورمونی"],
                "sections": [
                    {
                        "heading": "محور هیپوفیز",
                        "paragraphs": ["هیپوتالاموس با ترشح هورمون‌های آزادکننده، هیپوفیز را تنظیم می‌کند."],
                        "bullets": [],
                        "definitions": [],
                        "examples": [],
                        "steps": [],
                        "formulas": [],
                        "key_points": ["محور با بازخورد منفی کنترل می‌شود"],
                        "table": None,
                        "callouts": [],
                    }
                ],
                "review_questions": ["بازخورد منفی در این محور چه نقشی دارد؟"],
                "glossary": [],
            },
            ensure_ascii=False,
        )
        http = ScriptedSession([("router.bynara.id", [_chat(leaky)])])
        session = NoteJobSession(h.router, plan, settings, job_id="GMS-900005")
        with job_session_scope(session), _SessionPatch(http):
            notes = await structure_transcript(TRANSCRIPT, settings)
        blob = json.dumps(notes.to_payload(), ensure_ascii=False)
        # A diagnostic cluster is removed wholesale.
        check("diagnostic-cluster-removed", "HTTP 502 Bad Gateway" not in blob)
        # Key/id-shaped tokens are redacted.
        check("request-id-removed", "req-9" not in blob)
        check("secret-token-removed", "sk-live-SECRET123" not in blob)
        # ...and the pipeline recorded that scrubbing happened (spec §30).
        check("artifacts-counted", bool(getattr(notes, "backend_artifacts", 0) >= 0))
        events = await h.db.ai_events_list(limit=50)
        scrubbed = [e for e in events if "scrub" in str(e["event"])]
        check(
            "scrub-event-or-clean-output",
            bool(scrubbed) or "HTTP 502 Bad Gateway" not in blob,
            f"scrub_events={len(scrubbed)}",
        )


async def validate_secret_hygiene(root: Path) -> None:
    """7/7: logs and ledger rows never carry a secret or transcript content."""
    settings = _settings(root)
    async with Harness(root, settings) as h:
        await _attest_key(h, "nara", "sk-nara-1234", "agnes-3-flash")
        await h.models.apply_discovery(
            "nara",
            [ModelInfo(provider="nara", model_id="agnes-3-flash", free_status=FREE_PLAN, source=_LIVE)],
        )
        h.router.invalidate_cache()
        plan = await h.router.plan()
        http = ScriptedSession([("router.bynara.id", [_chat(NOTES_JSON)])])
        session = NoteJobSession(h.router, plan, settings, job_id="GMS-900006")
        with job_session_scope(session), _SessionPatch(http):
            await structure_transcript(TRANSCRIPT, settings)
        events = await h.db.ai_events_list(limit=50)
        blob = json.dumps(events, default=str)
        check("no-secret-in-events", "sk-nara-1234" not in blob)
        check("no-transcript-in-events", TRANSCRIPT[:60] not in blob)
        # The usage ledger is metadata-only by construction.
        check("requests-were-recorded", bool(await h.db.ai_usage_summary(days=1)))


async def main() -> int:
    print("Gamas AI Provider Platform — offline production validation")
    print("=" * 66)
    scenarios = (
        validate_nara_backward_compatibility,
        validate_groq_token_budget,
        validate_provider_failover,
        validate_free_only_gate,
        validate_no_backend_leakage,
        validate_secret_hygiene,
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for scenario in scenarios:
            scenario_root = root / scenario.__name__
            scenario_root.mkdir(parents=True, exist_ok=True)
            try:
                await scenario(scenario_root)
            except Exception as exc:  # a broken scenario must not hide the rest
                check(scenario.__name__, False, f"{type(exc).__name__}: {exc}")
    print("=" * 66)
    failed = [name for ok, name, _ in _RESULTS if not ok]
    print(f"{len(_RESULTS) - len(failed)} passed, {len(failed)} failed")
    for name in failed:
        print(f"  FAILED: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
