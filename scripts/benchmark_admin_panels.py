"""Offline benchmark for the admin panel and routing hot paths.

Measures the reads that an operator actually pays for — the AI overview, the
model catalogs, the event logs, the user list, the route panel and one
``router.plan()`` — against synthetic data in a temporary SQLite database. It
deliberately includes the *paired* pre-change query shapes (two overview
queries, a full catalog table load, an unpaged user dump) so the improvement of
a change can be read off one run instead of comparing two sessions.

No Telegram, no providers, no network, no keys. Deterministic seed.

    python -m scripts.benchmark_admin_panels --rows 20000
    python -m scripts.benchmark_admin_panels --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

from gamas_bot.admin_ai import AIPanels
from gamas_bot.admin_stt import SttPanels
from gamas_bot.ai.models import ModelRegistry
from gamas_bot.ai.routing import ProviderRouter
from gamas_bot.ai.usage import AIUsageTracker
from gamas_bot.bot import StudyBot
from gamas_bot.config import Settings
from gamas_bot.database import Database, utc_now
from gamas_bot.provider_credentials import ProviderCredentialManager
from gamas_bot.provider_health import ProviderHealthChecker
from gamas_bot.stt_platform.models import STTModelInfo

PROVIDERS = ("nara", "openrouter", "gemini", "groq", "cloudflare")


def _base_settings(root: Path) -> Settings:
    base = Settings(
        telegram_bot_token="benchmark-token",
        telegram_api_id=1,
        telegram_api_hash="benchmark-hash",
        admin_ids=frozenset({7}),
        database_path=root / "bot.sqlite3",
        session_path=root / "session",
        temp_dir=root / "tmp",
        max_file_size=2_000_000_000,
        stt_primary="speechmatics",
        stt_language="fa",
        stt_fallback_enabled=True,
        stt_min_confidence=0.65,
        speechmatics_api_key="sm-benchmark-key",
        speechmatics_base_url="https://example.test/v2",
        deepgram_api_key="dg-benchmark-key",
        deepgram_model="nova-3",
        gemini_api_key="gemini-benchmark-key",
        gemini_model="gemini-2.5-flash-lite",
        max_concurrent_jobs=2,
        stt_poll_interval=1,
        stt_job_timeout=100,
        provider_credentials_encryption_key=Fernet.generate_key().decode("ascii"),
        note_api_provider="openai_compatible",
        note_api_base_url="https://router.bynara.id/v1",
        note_api_model="agnes-3-flash",
        note_api_key="sk-benchmark-key",
    )
    return replace(base)


async def _seed(db: Database, *, rows: int, events: int, models: int, users: int) -> None:
    now = utc_now()
    for index in range(rows):
        provider = PROVIDERS[index % len(PROVIDERS)]
        await db.ai_usage_insert(
            {
                "created_at": now,
                "service": "notes",
                "provider": provider,
                "canonical": provider,
                "model": f"{provider}-model-{index % 7}",
                "request_type": "chunk_structuring",
                "route_position": index % 3,
                "attempt": 1,
                "latency_ms": random.randint(200, 9000),
                "http_status": 200 if index % 9 else 429,
                "estimated_input_tokens": 3000,
                "actual_input_tokens": 2900,
                "actual_output_tokens": 800,
                "total_tokens": 3700,
                "json_strategy": "json_schema",
                "result": "success" if index % 9 else "failure",
                "error_class": None if index % 9 else "rate_limited",
                "free_class": "free",
            }
        )
    for index in range(events):
        await db.ai_event_insert(
            {
                "event": "note_request_failed" if index % 3 else "note_request_started",
                "provider": PROVIDERS[index % len(PROVIDERS)],
                "model": f"model-{index % 11}",
                "request_type": "chunk_structuring",
                "http_status": 503 if index % 3 else 200,
                "latency_ms": 300 + (index % 900),
                "level": "warning" if index % 3 else "info",
                "job_id": f"GMS-{index % 250:06d}",
            },
            prune_to=0,
        )
    await db.ai_models_upsert_discovery(
        "openrouter",
        [
            {
                "model": f"model-{index:04d}",
                "free_status": "free_permanent" if index % 2 else "unknown",
                "deprecated": 1 if index == models - 1 else 0,
            }
            for index in range(models)
        ],
    )
    await db.stt_models_upsert(
        [
            STTModelInfo(
                provider="groq", model=f"groq-asr-{index:03d}", display_name=f"Model {index}"
            ).to_row()
            for index in range(min(models, 40))
        ]
    )
    for index in range(users):
        await db.upsert_user(10_000 + index, username=f"u{index}")


def _event(data: str) -> SimpleNamespace:
    return SimpleNamespace(
        is_private=True,
        data=data.encode("utf-8"),
        get_sender=AsyncMock(
            return_value=SimpleNamespace(id=7, username="admin", bot=False)
        ),
        answer=AsyncMock(),
        edit=AsyncMock(),
        reply=AsyncMock(),
        respond=AsyncMock(),
        message=SimpleNamespace(delete=AsyncMock(), media=None),
    )


class _Timer:
    def __init__(self, repeat: int) -> None:
        self.repeat = repeat
        self.results: dict[str, float] = {}

    async def __call__(self, label: str, factory) -> float:
        await factory()  # warm the caches the panel is allowed to warm
        started = time.perf_counter()
        for _ in range(self.repeat):
            await factory()
        elapsed = (time.perf_counter() - started) / self.repeat * 1000
        self.results[label] = round(elapsed, 3)
        print(f"{label:48s} {elapsed:9.3f} ms/call", flush=True)
        return elapsed


async def _run(args) -> dict:
    timer = _Timer(args.repeat)
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        settings = _base_settings(root)
        db = Database(root / "bench.sqlite3")
        await db.open()
        try:
            return await _measure(args, timer, root, db, settings)
        finally:
            await db.close()


async def _measure(args, timer, root: Path, db: Database, settings: Settings) -> dict:
        manager = ProviderCredentialManager(db, settings)
        tracker = AIUsageTracker(db)
        registry = ModelRegistry(db, settings)
        router = ProviderRouter(db, settings, manager, tracker, registry)
        bot = StudyBot.__new__(StudyBot)
        bot.settings = settings
        bot.db = db
        bot.credential_manager = manager
        bot.ai_tracker = tracker
        bot.model_registry = registry
        bot.provider_router = router
        bot.provider_health = ProviderHealthChecker(db, settings, manager)
        bot._pending_admin_actions = {}
        bot._pending_credential_setup = {}
        bot.ai_panels = AIPanels(bot)
        bot.stt_panels = SttPanels(bot)
        bot._edit_callback = AsyncMock()

        started = time.perf_counter()
        await _seed(
            db,
            rows=args.rows,
            events=args.events,
            models=args.models,
            users=args.users,
        )
        seed_seconds = time.perf_counter() - started
        print(
            f"seeded: rows={args.rows} events={args.events} models={args.models} "
            f"users={args.users} in {seed_seconds:.1f}s\n",
            flush=True,
        )

        print("-- overview reads (before = two queries, after = one) --")
        async def _two_queries():
            await db.ai_usage_metrics(days=1)
            await db.ai_usage_latency_p95(days=1)

        before = await timer("BEFORE metrics + p95 (two windows)", _two_queries)
        after = await timer("AFTER  ai_usage_overview(days=1)", lambda: db.ai_usage_overview(days=1))
        print(f"     overview: {before:.1f} ms -> {after:.1f} ms\n")

        print("-- catalog reads (before = whole table, after = one page) --")
        await timer(
            "BEFORE ai_models_list(openrouter)",
            lambda: db.ai_models_list("openrouter", include_unavailable=True),
        )
        await timer(
            "AFTER  ai_models_page(first page)",
            lambda: db.ai_models_page("openrouter", offset=0, limit=10),
        )
        await timer(
            "AFTER  ai_models_page(last page)",
            lambda: db.ai_models_page(
                "openrouter", offset=max(0, args.models - 10), limit=10
            ),
        )
        await timer(
            "BEFORE user_summaries(limit=50)", lambda: db.user_summaries(limit=50)
        )
        await timer(
            "AFTER  user_summaries(page of 25)",
            lambda: db.user_summaries(limit=25, offset=250),
        )
        print()

        print("-- whole panel renders (reads + text + keyboard) --")
        await timer("admin:home overview", lambda: bot._handle_callback(_event("admin:home")))
        await timer("admin:ai platform home", lambda: bot.ai_panels.show_platform_home(_event("x")))
        await timer("admin:ai providers", lambda: bot.ai_panels.show_providers(_event("x")))
        await timer("admin:ai plan", lambda: bot.ai_panels.show_plan(_event("x")))
        await timer("admin:ai routes", lambda: bot.ai_panels.show_routes(_event("x")))
        await timer("admin:ai health (cached only)", lambda: bot.ai_panels.show_health(_event("x")))
        await timer("admin:ai logs page 0", lambda: bot.ai_panels.show_logs(_event("x")))
        await timer("admin:ai logs page 40", lambda: bot.ai_panels.show_logs(_event("x"), page=40))
        await timer(
            "admin:ai catalog page 0",
            lambda: bot.ai_panels.show_models(_event("x"), "openrouter", 0),
        )
        await timer(
            "admin:ai catalog last page",
            lambda: bot.ai_panels.show_models(
                _event("x"), "openrouter", max(0, args.models // 10 - 1)
            ),
        )
        await timer("admin:stt catalog", lambda: bot.stt_panels.show_models(_event("x"), "groq", 0))
        await timer("admin:stt logs", lambda: bot.stt_panels.show_logs(_event("x")))
        await timer("admin:users page 0", lambda: bot._show_admin_users(_event("x"), None))
        await timer("admin:users page 10", lambda: bot._show_admin_users(_event("x"), "10"))
        print()

        print("-- routing --")
        await timer("router.plan(chunk_structuring)", lambda: router.plan("chunk_structuring"))
        await timer("router.ensure_seeded() (steady state)", lambda: router.ensure_seeded())

        await db.close()
        payload = {
            "seed": {
                "rows": args.rows,
                "events": args.events,
                "models": args.models,
                "users": args.users,
                "seconds": round(seed_seconds, 2),
            },
            "milliseconds_per_call": timer.results,
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=20_000, help="usage rows to seed")
    parser.add_argument("--events", type=int, default=2_000, help="event rows to seed")
    parser.add_argument("--models", type=int, default=600, help="catalog models to seed")
    parser.add_argument("--users", type=int, default=800, help="user rows to seed")
    parser.add_argument("--repeat", type=int, default=20, help="timed iterations")
    parser.add_argument("--json", action="store_true", help="print a machine-readable summary")
    args = parser.parse_args()
    random.seed(20260917)
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
