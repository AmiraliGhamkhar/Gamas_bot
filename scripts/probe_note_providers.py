"""Operator probe for the note-generation providers (never run by the bot).

Two stages, both explicit — this script is the Phase 0/1 verification tool for
the matched default model pair (``docs/PROVIDERS.md``):

Read-only stage (default)::

    python -m scripts.probe_note_providers

    Prints the eligible note routes from the current ``.env``, then asks each
    gateway only metadata questions: NaraRouter's authenticated ``/v1/models``
    (is the configured model entitled, and what limits does the gateway
    publish for it?), OpenRouter's ``/api/v1/key`` plus the configured
    model's ``/endpoints`` record (served context, completion cap, uptime,
    and the free-tier daily request counter), and Google AI Studio Gemini's
    ``/v1beta/models/{model}`` resource (input/output token limits, thinking
    support, and ``generateContent`` capability). No generation call is made
    and no secret is printed.

Generation stage (opt-in, consumes provider quota)::

    python -m scripts.probe_note_providers --generate lecture.txt

    Sends one real map-style note request per eligible route — the bot's own
    system prompt plus the first chunk-sized excerpt of the given transcript,
    built and parsed with the bot's own request/JSON code — and reports
    per-route success, elapsed time and section count, so the chunk/output
    settings can be validated before they are rolled out. Use a real,
    anonymised lecture transcript; expect exactly one generation request per
    route (plus the pipeline's bounded retries, if any).

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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gamas_bot.config import from_env, note_routes  # noqa: E402
from gamas_bot.llm import (  # noqa: E402
    _build_get_request_for,
    _clean_api_key,
    _endpoint,
    _openrouter_model_path,
    _redact_secrets,
)


def _get_json(
    url: str,
    api_key: str = "",
    timeout: float = 30.0,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, object]:
    """One authenticated GET; returns ``(status, parsed_json_or_text)``."""
    clean_key = _clean_api_key(api_key)
    req_headers: dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": "GamasBot/3.0",
    }
    if headers is not None:
        req_headers.update(headers)
    elif clean_key:
        req_headers["Authorization"] = f"Bearer {clean_key}"
    request = urllib.request.Request(url, headers=req_headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        status = exc.code
    except (urllib.error.URLError, OSError) as exc:
        return 0, _redact_secrets(f"unreachable: {exc}", clean_key)
    try:
        return status, json.loads(body)
    except ValueError:
        return status, _redact_secrets(body[:400], clean_key)


def _check_nara(route) -> bool:
    """NaraRouter: the configured model must be entitled on this key's plan."""
    url, headers = _build_get_request_for(route, "models")
    status, payload = _get_json(url, route.api_key, headers=headers)
    if status != 200 or not isinstance(payload, dict):
        detail = _redact_secrets(str(payload)[:200], route.api_key)
        print(f"[FAIL] nara /v1/models: HTTP {status}: {detail}")
        return False
    entries = payload.get("data")
    if not isinstance(entries, list):
        print("[FAIL] nara /v1/models: unexpected response shape (no data list)")
        return False
    ids = [entry.get("id") for entry in entries if isinstance(entry, dict)]
    if route.model.startswith("combo/") and ids:
        print(
            f"[PASS] nara combo model {route.model!r} is routable "
            f"({len(ids)} underlying model(s) on this plan)"
        )
        return True
    entitled = route.model in ids
    print(
        f"[{'PASS' if entitled else 'FAIL'}] nara model {route.model!r} "
        f"{'is' if entitled else 'is NOT'} entitled ({len(ids)} model(s) on this plan)"
    )
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") == route.model:
            limits = {
                key: entry[key]
                for key in ("context_length", "max_completion_tokens", "max_tokens", "reasoning")
                if key in entry
            }
            if limits:
                print(f"       published metadata: {limits}")
            else:
                print("       gateway publishes no context/completion limits for it")
    return entitled


def _check_openrouter(route) -> bool:
    """OpenRouter: endpoint caps for the pinned model + the free-tier counter."""
    ok = True
    url, headers = _build_get_request_for(route, "model_endpoints")
    status, payload = _get_json(url, route.api_key, headers=headers)
    if status == 404 and ":" in route.model:
        base_model = route.model.split(":", 1)[0]
        fallback_url = _endpoint(
            route.base_url, f"models/{_openrouter_model_path(base_model)}/endpoints"
        )
        status, payload = _get_json(fallback_url, route.api_key, headers=headers)
    if status == 200 and isinstance(payload, dict):
        data_obj = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        endpoints = data_obj.get("endpoints") or []
        arch = data_obj.get("architecture") if isinstance(data_obj.get("architecture"), dict) else {}
        for endpoint in endpoints:
            print(
                f"[PASS] openrouter endpoint {endpoint.get('provider_name')}: "
                f"context_length={endpoint.get('context_length')} "
                f"max_completion_tokens={endpoint.get('max_completion_tokens')} "
                f"uptime_last_1d={endpoint.get('uptime_last_1d')}"
            )
        if not endpoints:
            if arch.get("tokenizer") == "Router" or route.model in ("openrouter/free", "openrouter/auto"):
                print(
                    f"[WARN] openrouter model {route.model!r} is a dynamic router "
                    "(no pinned endpoint; served context window varies per request)"
                )
            else:
                print("[FAIL] openrouter endpoints: the model has no live endpoint")
                ok = False
    else:
        detail = _redact_secrets(str(payload)[:200], route.api_key)
        print(f"[FAIL] openrouter endpoints: HTTP {status}: {detail}")
        ok = False

    key_url, key_headers = _build_get_request_for(route, "key")
    status, payload = _get_json(key_url, route.api_key, headers=key_headers)
    if status == 200 and isinstance(payload, dict):
        data = payload.get("data") or {}
        limit_remaining = data.get("limit_remaining")
        if (
            isinstance(limit_remaining, (int, float))
            and not isinstance(limit_remaining, bool)
            and limit_remaining <= 0
        ):
            print(
                f"[FAIL] openrouter /api/v1/key: credit limit exhausted "
                f"(limit_remaining={limit_remaining})"
            )
            ok = False
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
    elif status in (401, 403):
        detail = _redact_secrets(str(payload)[:200], route.api_key)
        print(f"[FAIL] openrouter /api/v1/key: HTTP {status}: {detail}")
        ok = False
    else:
        detail = _redact_secrets(str(payload)[:200], route.api_key)
        print(f"[WARN] openrouter /api/v1/key: HTTP {status}: {detail}")
    return ok


def _check_gemini(route) -> bool:
    """Google AI Studio Gemini: verify model metadata and ``generateContent`` support."""
    url, headers = _build_get_request_for(route, "model")
    status, payload = _get_json(url, route.api_key, headers=headers)
    if status != 200 or not isinstance(payload, dict):
        detail = _redact_secrets(str(payload)[:200], route.api_key)
        print(f"[FAIL] gemini model {route.model!r}: HTTP {status}: {detail}")
        return False
    methods = payload.get("supportedGenerationMethods")
    if isinstance(methods, list) and "generateContent" not in methods:
        print(
            f"[FAIL] gemini model {route.model!r} does not support generateContent "
            f"(supportedGenerationMethods={methods})"
        )
        return False
    print(
        f"[PASS] gemini model {payload.get('name') or route.model!r}: "
        f"inputTokenLimit={payload.get('inputTokenLimit')} "
        f"outputTokenLimit={payload.get('outputTokenLimit')} "
        f"thinking={payload.get('thinking')}"
    )
    return True


async def _generate(route, settings, text: str) -> bool:
    """One map-style note request against exactly ``route``.

    The request is built, sent and parsed with the bot's own LLM code for
    that single route (no failover, no Settings surgery), so a pass proves
    the route's model accepts the real prompt shape and answers with valid,
    schema-conforming Persian notes JSON.
    """
    import aiohttp

    from gamas_bot.llm import _complete_on_route, _parse_json_object
    from gamas_bot.notes import (
        TRANSCRIPT_PROMPT,
        _load_json_payload,
        build_system_prompt,
        effective_chunk_chars,
    )

    system = build_system_prompt(settings.note_mode)
    started = time.monotonic()
    try:
        excerpt = text[: effective_chunk_chars(settings, system_prompt=system)]
        async with aiohttp.ClientSession() as session:
            raw = await _complete_on_route(
                route, session, settings, system=system, user=TRANSCRIPT_PROMPT + excerpt
            )
        notes = _load_json_payload(_parse_json_object(raw))
    except Exception as exc:  # noqa: BLE001 - report any provider failure verbatim
        elapsed = time.monotonic() - started
        print(f"[FAIL] {route.slug} generation ({elapsed:.1f}s): {type(exc).__name__}: {exc}")
        return False
    elapsed = time.monotonic() - started
    print(
        f"[PASS] {route.slug} generation ({elapsed:.1f}s, {len(excerpt)} chars): "
        f"title={notes.display_title!r} sections={len(notes.sections)}"
    )
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--generate",
        metavar="TRANSCRIPT_FILE",
        help="also send one real note request per provider over this text file",
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
            ok = _check_nara(route) and ok
        elif route.slug == "openrouter":
            ok = _check_openrouter(route) and ok
        elif route.slug == "gemini":
            ok = _check_gemini(route) and ok
        else:
            print(f"[WARN] {route.slug}: no metadata probe; generation stage still applies")

    if args.generate:
        text = Path(args.generate).read_text(encoding="utf-8")
        print(
            f"\nGeneration probe over the first chunk-sized excerpt of "
            f"{len(text)} chars (one request per route; consumes provider quota):"
        )
        for route in routes:
            ok = asyncio.run(_generate(route, settings, text)) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
