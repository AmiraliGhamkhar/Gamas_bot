"""Operator probe for the note-generation providers (never run by the bot).

Two stages, both explicit — this script is the Phase 0/1 verification tool for
the matched default model pair (``docs/PROVIDERS.md``):

Read-only stage (default)::

    python -m scripts.probe_note_providers

    Prints the eligible note routes from the current ``.env``, then asks each
    gateway only metadata questions: NaraRouter's authenticated ``/v1/models``
    (is the configured model entitled, and what limits does the gateway
    publish for it?) and OpenRouter's ``/api/v1/key`` plus the configured
    model's ``/endpoints`` record (served context, completion cap, uptime,
    and the free-tier daily request counter). No generation call is made and
    no secret is printed.

Generation stage (opt-in, consumes provider quota)::

    python -m scripts.probe_note_providers --generate lecture.txt

    Runs the bot's real ``structure_transcript`` once per eligible route
    (each restricted to that single provider) over the given transcript text
    and reports per-route success, elapsed time and section count, so the
    chunk/output settings can be validated before they are rolled out. Use a
    real, anonymised lecture transcript; expect one map request per chunk
    plus at most one compile request per route.

Exit code is 1 when any check or generation probe fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gamas_bot.config import Settings, from_env, note_routes  # noqa: E402


def _get_json(url: str, api_key: str, timeout: float = 30.0) -> tuple[int, object]:
    """One authenticated GET; returns ``(status, parsed_json_or_text)``."""
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        status = exc.code
    except (urllib.error.URLError, OSError) as exc:
        return 0, f"unreachable: {exc}"
    try:
        return status, json.loads(body)
    except ValueError:
        return status, body[:400]


def _check_nara(settings: Settings, route) -> bool:
    """NaraRouter: the configured model must be entitled on this key's plan."""
    status, payload = _get_json(f"{route.base_url}/models", route.api_key)
    if status != 200 or not isinstance(payload, dict):
        print(f"[FAIL] nara /v1/models: HTTP {status}: {str(payload)[:200]}")
        return False
    entries = payload.get("data")
    if not isinstance(entries, list):
        print("[FAIL] nara /v1/models: unexpected response shape (no data list)")
        return False
    ids = [entry.get("id") for entry in entries if isinstance(entry, dict)]
    entitled = route.model in ids
    print(
        f"[{'PASS' if entitled else 'FAIL'}] nara model {route.model!r} "
        f"{'is' if entitled else 'is NOT'} entitled ({len(ids)} model(s) on this plan)"
    )
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") == route.model:
            limits = {
                key: entry[key]
                for key in ("context_length", "max_completion_tokens", "max_tokens")
                if key in entry
            }
            if limits:
                print(f"       published limits: {limits}")
            else:
                print("       gateway publishes no context/completion limits for it")
    return entitled


def _check_openrouter(settings: Settings, route) -> bool:
    """OpenRouter: endpoint caps for the pinned model + the free-tier counter."""
    ok = True
    status, payload = _get_json(
        f"{route.base_url}/models/{route.model}/endpoints", route.api_key
    )
    if status == 200 and isinstance(payload, dict):
        endpoints = (payload.get("data") or {}).get("endpoints") or []
        for endpoint in endpoints:
            print(
                f"[PASS] openrouter endpoint {endpoint.get('provider_name')}: "
                f"context_length={endpoint.get('context_length')} "
                f"max_completion_tokens={endpoint.get('max_completion_tokens')} "
                f"uptime_last_1d={endpoint.get('uptime_last_1d')}"
            )
        if not endpoints:
            print("[FAIL] openrouter endpoints: the model has no live endpoint")
            ok = False
    else:
        print(f"[FAIL] openrouter endpoints: HTTP {status}: {str(payload)[:200]}")
        ok = False

    status, payload = _get_json(f"{route.base_url}/key", route.api_key)
    if status == 200 and isinstance(payload, dict):
        data = payload.get("data") or {}
        counter = data.get("free_model_daily_requests") or {}
        if counter:
            print(
                f"[PASS] openrouter free-tier daily requests: "
                f"used={counter.get('used')} limit={counter.get('limit')} "
                f"remaining={counter.get('remaining')} "
                f"(is_free_tier={data.get('is_free_tier')})"
            )
        else:
            print("[WARN] openrouter /api/v1/key: no free-tier counter reported")
    else:
        print(f"[WARN] openrouter /api/v1/key: HTTP {status}: {str(payload)[:200]}")
    return ok


def _single_route_settings(settings: Settings, slug: str) -> Settings:
    """Settings whose eligible chain is exactly ``slug`` (its own key only)."""
    return replace(
        settings,
        note_providers=(slug,),
        note_provider="",
        note_api_key="",
        note_api_base_url="",
        gemini_api_key="",
    )


async def _generate(route, settings: Settings, text: str) -> bool:
    import aiohttp

    from gamas_bot.notes import structure_transcript

    scoped = _single_route_settings(settings, route.slug)
    if not note_routes(scoped):
        print(f"[WARN] {route.slug}: no dedicated credential; generation skipped")
        return True
    started = time.monotonic()
    try:
        async with aiohttp.ClientSession() as session:
            notes = await structure_transcript(text, scoped, session, mode=settings.note_mode)
    except Exception as exc:  # noqa: BLE001 - report any provider failure verbatim
        elapsed = time.monotonic() - started
        print(f"[FAIL] {route.slug} generation ({elapsed:.1f}s): {type(exc).__name__}: {exc}")
        return False
    elapsed = time.monotonic() - started
    print(
        f"[PASS] {route.slug} generation ({elapsed:.1f}s): "
        f"title={notes.display_title!r} sections={len(notes.sections)}"
    )
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--generate",
        metavar="TRANSCRIPT_FILE",
        help="also run one real note generation per provider over this text file",
    )
    args = parser.parse_args(argv)

    settings = from_env()
    routes = note_routes(settings)
    if not routes:
        print("[FAIL] no note provider is configured (see docs/PROVIDERS.md)")
        return 1
    print("Eligible note routes (in failover order):")
    for route in routes:
        print(f"  - {route.slug}: {route.label} model={route.model} base={route.base_url}")
    print(
        f"Sizing: NOTE_CONTEXT_TOKENS={settings.note_context_tokens} "
        f"NOTE_CHUNK_CHARS={settings.note_chunk_chars} "
        f"NOTE_MAX_OUTPUT_TOKENS={settings.note_max_output_tokens} "
        f"NOTE_COMPILE_MAX_CHARS={settings.note_compile_max_chars} (0 = automatic) "
        f"NOTE_REASONING_EFFORT={settings.note_reasoning_effort or '(provider default)'}"
    )

    ok = True
    for route in routes:
        if route.slug == "nara":
            ok = _check_nara(settings, route) and ok
        elif route.slug == "openrouter":
            ok = _check_openrouter(settings, route) and ok
        else:
            print(f"[WARN] {route.slug}: no metadata probe; generation stage still applies")

    if args.generate:
        text = Path(args.generate).read_text(encoding="utf-8")
        print(
            f"\nGeneration probe over {len(text)} chars "
            f"(~{max(1, len(text) // max(1, settings.note_chunk_chars))} chunk(s) per route "
            "plus at most one compile call; consumes provider quota):"
        )
        for route in routes:
            ok = asyncio.run(_generate(route, settings, text)) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
