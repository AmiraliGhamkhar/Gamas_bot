from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from types import SimpleNamespace
import asyncio
import multiprocessing
import sqlite3

from tests.support import make_settings

import pytest

from gamas_bot.llm import _ACTUAL_USAGE_TOKENS, _dispatch_across_routes
from gamas_bot.metrics import METRICS
from gamas_bot.llm_quota import ProviderBudget, QuotaUnavailable, finish, reserve
import gamas_bot.llm_quota as quota_module


def test_rpm_and_rpd_are_checked_in_shared_sqlite_ledger(tmp_path):
    path = str(tmp_path / "shared.sqlite3")
    limits = ProviderBudget(rpm=2, rpd=3, max_in_flight=2)
    first = reserve(path, "openrouter", limits, 50, attempts=1)
    second = reserve(path, "openrouter", limits, 50, attempts=1)
    with pytest.raises(QuotaUnavailable, match="RPM"):
        reserve(path, "openrouter", limits, 50, attempts=1)
    finish(first, actual_tokens=20, succeeded=True)
    finish(second, actual_tokens=None, succeeded=False)


def test_tpm_and_inflight_reservations_are_atomic_across_threads(tmp_path):
    path = str(tmp_path / "shared.sqlite3")
    limits = ProviderBudget(tpm=100, max_in_flight=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reserve, path, "avalai", limits, 70, 1) for _ in range(2)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except QuotaUnavailable:
                outcomes.append(None)
    reservations = [item for item in outcomes if item is not None]
    assert len(reservations) == 1
    with pytest.raises(QuotaUnavailable, match="in-flight"):
        reserve(path, "avalai", limits, 10, 1)
    finish(reservations[0], actual_tokens=None, succeeded=True)
    with pytest.raises(QuotaUnavailable, match="TPM"):
        reserve(path, "avalai", limits, 40, 1)


def test_circuit_breaker_opens_and_allows_one_recovery_probe(tmp_path, monkeypatch):
    now = [1_800_000_000.0]
    monkeypatch.setattr(quota_module.time, "time", lambda: now[0])
    path = str(tmp_path / "circuit.sqlite3")
    limits = ProviderBudget(max_in_flight=1)
    for _ in range(3):
        held = reserve(path, "gemini", limits, 100, attempts=1)
        finish(held, actual_tokens=None, succeeded=False)
    with pytest.raises(QuotaUnavailable, match="circuit breaker open"):
        reserve(path, "gemini", limits, 100, attempts=1)
    now[0] += 61
    probe = reserve(path, "gemini", limits, 100, attempts=1)
    with pytest.raises(QuotaUnavailable, match="probe already in flight"):
        reserve(path, "gemini", limits, 100, attempts=1)
    finish(probe, actual_tokens=20, succeeded=True)
    healthy = reserve(path, "gemini", limits, 100, attempts=1)
    finish(healthy, actual_tokens=20, succeeded=True)


def test_central_dispatch_reserves_budget_and_skips_exhausted_route(tmp_path):
    async def scenario():
        db_path = tmp_path / "central.sqlite3"
        settings = make_settings(
            db_path=db_path,
            llm_primary_max_in_flight=3,
            llm_fallback_max_in_flight=1,
            llm_slot_wait_timeout=0,
            note_hedge_after=30,  # central router must still disable hedging
            avalai_rpm_limit=0,
            avalai_tpm_limit=0,
            avalai_rpd_limit=2,
            avalai_max_in_flight=3,
        )
        route = SimpleNamespace(slug="avalai", label="AvalAI", model="test-model")
        calls = []

        async def handler(_route, _last):
            calls.append("called")
            _ACTUAL_USAGE_TOKENS.set(75)
            return {"ok": True}

        for expected in (True, False):
            try:
                result = await _dispatch_across_routes(
                    (route,), object(), settings, handler, reservation_tokens=500,
                    task="BOOKLET_GENERATION", estimated_input_tokens=25,
                    reserved_output_tokens=475,
                )
                assert result == {"ok": True}
                assert expected
            except Exception as exc:
                assert not expected
                assert "quota" in str(exc).lower() or "سهمیه" in str(exc)
        assert calls == ["called"]
        with sqlite3.connect(db_path) as conn:
            requests = conn.execute(
                "SELECT COUNT(*) FROM llm_quota_requests WHERE provider='avalai'"
            ).fetchone()[0]
            tokens = conn.execute(
                "SELECT SUM(tokens) FROM llm_quota_requests WHERE provider='avalai'"
            ).fetchone()[0]
        assert requests == 2  # initial request + one reserved bounded retry
        assert tokens == 75  # actual usage reconciles below the 500-token reservation
        operation = METRICS.snapshot()["llm_operations"][
            "BOOKLET_GENERATION|avalai|test-model"
        ]
        assert operation["successes"] >= 1
        assert operation["actual_total_tokens"] >= 75

    asyncio.run(scenario())


def test_daily_allowance_survives_minute_rollover_and_failed_requests(tmp_path, monkeypatch):
    now = [1_800_000_000.0]
    monkeypatch.setattr(quota_module.time, "time", lambda: now[0])
    path = str(tmp_path / "daily.sqlite3")
    limits = ProviderBudget(rpm=10, rpd=2, max_in_flight=1)
    held = reserve(path, "openrouter", limits, 50, attempts=2)
    finish(held, actual_tokens=None, succeeded=False)
    now[0] += 61  # RPM has cleared; the daily request allowance has not.
    with pytest.raises(QuotaUnavailable, match="daily request budget"):
        reserve(path, "openrouter", limits, 50)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_quota_reservations").fetchone()[0] == 0
        assert conn.execute("SELECT SUM(tokens) FROM llm_quota_requests").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM llm_quota_requests").fetchone()[0] == 2


def test_actual_usage_reconciliation_reopens_tpm_budget(tmp_path):
    path = str(tmp_path / "reconcile.sqlite3")
    limits = ProviderBudget(tpm=100, max_in_flight=1)
    held = reserve(path, "avalai", limits, 90)
    finish(held, actual_tokens=20, succeeded=True)
    next_call = reserve(path, "avalai", limits, 70)
    finish(next_call, actual_tokens=5, succeeded=True)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT SUM(tokens) FROM llm_quota_requests").fetchone()[0] == 25
        assert conn.execute("SELECT COUNT(*) FROM llm_quota_reservations").fetchone()[0] == 0


def _try_reserve_in_child(path):
    try:
        return reserve(path, "avalai", ProviderBudget(max_in_flight=1), 70)
    except QuotaUnavailable:
        return None


def test_inflight_budget_is_atomic_across_independent_processes(tmp_path):
    path = str(tmp_path / "multiprocess.sqlite3")
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
        futures = [pool.submit(_try_reserve_in_child, path) for _ in range(4)]
        held = [result for future in futures if (result := future.result(timeout=20)) is not None]
    assert len(held) == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("SELECT COUNT(*) FROM llm_quota_reservations").fetchone()[0] == 1
    finish(held[0], actual_tokens=30, succeeded=True)
    # The one-slot project is usable immediately after the winner releases it.
    next_call = reserve(path, "avalai", ProviderBudget(max_in_flight=1), 10)
    finish(next_call, actual_tokens=None, succeeded=False)
