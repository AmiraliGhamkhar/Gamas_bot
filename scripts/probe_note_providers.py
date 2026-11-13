"""Operator probe for the note-generation providers (never run by the bot).

Three explicitly separate stages (``docs/PROVIDERS.md``):

Offline configuration inspection (no network, no provider quota)::

    python -m scripts.probe_note_providers --show-config

    Prints the effective provider order and the non-secret note settings the
    *next* bot start will use, plus each route's effective outgoing output
    budget. No API key or URL credential is ever printed. This is also the
    first step of every other stage.

Metadata stage (default, read-only, no generation quota)::

    python -m scripts.probe_note_providers

    Asks each gateway only metadata questions: AvalAI's authenticated
    ``/v1/models`` (is the configured model entitled, and what limits does the
    gateway publish for it?), OpenRouter's ``/api/v1/key`` plus the configured
    model's ``/endpoints`` record (served context, completion cap, uptime,
    and the free-tier daily request counter), and Google AI Studio Gemini's
    ``/v1beta/models/{model}`` resource (input/output token limits, thinking
    support, and ``generateContent`` capability). A metadata PASS proves
    authentication/entitlement only — it does NOT prove text generation works.

Generation stage (opt-in, CONSUMES PROVIDER QUOTA)::

    python -m scripts.probe_note_providers --generate lecture.txt

    Sends one real map-style note request per eligible route — the bot's own
    system prompt plus the first chunk-sized excerpt of the given transcript,
    built and parsed with the bot's own request/JSON code — and reports
    per-route success, elapsed time and section count. Only this stage can
    verify that a model actually generates valid Persian study notes. Use a
    non-sensitive test transcript; expect one generation request per route
    (plus the pipeline's bounded retries, if any).

Automated CI and the standard test suite never call ``--generate`` and need no
live keys. Exit code is 1 when any check or generation probe fails.
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
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gamas_bot.config import from_env, note_route_summary, note_routes  # noqa: E402
from gamas_bot.llm import (  # noqa: E402
    _build_get_request_for,
    _build_request_for,
    _clean_api_key,
    _endpoint,
    _openrouter_model_path,
    _redact_secrets,
    request_payload_output_tokens,
)


#: Metadata endpoints are untrusted network inputs too. ``/models`` can be
#: large, but 16 MiB is far beyond normal provider listings while still keeping
#: this operator CLI bounded like the production LLM client.
MAX_PROBE_RESPONSE_BYTES = 16 * 1024 * 1024


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Turn redirects into HTTP errors instead of forwarding bearer headers."""

    def redirect_request(self, *_args, **_kwargs):
        return None


def _safe_base_url(base_url: str) -> str:
    """A printable base URL without userinfo, query string or fragment.

    An operator can paste a URL carrying ``?key=...`` or ``user:pass@``; those
    parts are credentials and must never reach a terminal, log or ticket.
    """
    parsed = urlsplit(base_url or "")
    if not parsed.scheme or not parsed.netloc:
        return base_url or "(not configured)"
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _effective_output_tokens(route, settings) -> int | None:
    """The output budget the bot's own request builder will really send."""
    try:
        _url, _headers, payload = _build_request_for(route, settings, "", "")
    except Exception:  # noqa: BLE001 - a diagnostic must never abort the probe
        return None
    return request_payload_output_tokens(payload)


def _print_config(settings, routes) -> None:
    for line in note_route_summary(settings):
        print(line)
    print(
        f"NOTE_CONTEXT_TOKENS={settings.note_context_tokens} "
        f"NOTE_CHUNK_CHARS={settings.note_chunk_chars} "
        f"NOTE_REQUEST_TIMEOUT_SECONDS={settings.note_request_timeout:.0f} "
        f"NOTE_JSON_MODE={int(settings.note_json_mode)}"
    )
    print("Effective outgoing output budgets (from the built request payload):")
    if not routes:
        print("  (no eligible route)")
    for route in routes:
        budget = _effective_output_tokens(route, settings)
        print(
            f"  - {route.slug}: requested_output_tokens="
            f"{budget if budget is not None else '-'} "
            f"base={_safe_base_url(route.base_url)}"
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
    # Do not follow a provider-supplied redirect: urllib would otherwise carry
    # the request headers to a different origin. API endpoints should answer
    # directly, and a 3xx result is a useful probe failure.
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw_body = response.read(MAX_PROBE_RESPONSE_BYTES + 1)
            status = response.status
    except urllib.error.HTTPError as exc:
        raw_body = exc.read(MAX_PROBE_RESPONSE_BYTES + 1)
        status = exc.code
    except (urllib.error.URLError, OSError) as exc:
        return 0, _redact_secrets(f"unreachable: {exc}", clean_key)
    if len(raw_body) > MAX_PROBE_RESPONSE_BYTES:
        return status, (
            f"response body exceeded {MAX_PROBE_RESPONSE_BYTES} byte safety limit"
        )
    body = raw_body.decode("utf-8", "replace")
    try:
        return status, json.loads(body)
    except ValueError:
        return status, _redact_secrets(body[:400], clean_key)


def _check_avalai(route) -> bool:
    """AvalAI: the configured model must be entitled on this key's plan."""
    return _check_openai_models(route)


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


def _check_openai_models(route) -> bool:
    """Shared OpenAI-compatible ``/models`` metadata check (used by AvalAI)."""
    url, headers = _build_get_request_for(route, "models")
    status, payload = _get_json(url, route.api_key, headers=headers)
    if status != 200 or not isinstance(payload, dict):
        detail = _redact_secrets(str(payload)[:200], route.api_key)
        print(f"[FAIL] {route.slug} /v1/models: HTTP {status}: {detail}")
        return False
    entries = payload.get("data")
    if not isinstance(entries, list):
        print(f"[FAIL] {route.slug} /v1/models: unexpected response shape (no data list)")
        return False
    ids = [entry.get("id") for entry in entries if isinstance(entry, dict)]
    entitled = route.model in ids
    print(
        f"[{'PASS' if entitled else 'FAIL'}] {route.slug} model {route.model!r} "
        f"{'is' if entitled else 'is NOT'} listed ({len(ids)} model(s) returned)"
    )
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") == route.model:
            limits = {
                key: entry[key]
                for key in (
                    "context_window",
                    "context_length",
                    "max_completion_tokens",
                    "max_tokens",
                )
                if key in entry
            }
            if limits:
                print(f"       published metadata: {limits}")
    return entitled


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
        _load_json_payload,
        _response_schema,
        build_chunk_message,
        build_system_prompt,
        effective_chunk_chars,
    )

    system = build_system_prompt(settings.note_mode)
    schema = _response_schema(settings)
    budget = _effective_output_tokens(route, settings)
    print(
        f"[INFO] {route.slug}: sending ONE real generation request "
        f"(requested_output_tokens={budget if budget is not None else '-'}, "
        f"structured_output={int(settings.note_structured_output)}, "
        f"reasoning_effort={settings.note_reasoning_effort or '(provider default)'})"
    )
    started = time.monotonic()
    try:
        excerpt = text[: effective_chunk_chars(settings, system_prompt=system)]
        # The same user message the bot sends for a one-part lesson, so the pass
        # proves the real request shape, not an approximation of it.
        user = build_chunk_message(excerpt, index=1, total=1)
        async with aiohttp.ClientSession() as session:
            raw = await _complete_on_route(
                route,
                session,
                settings,
                system=system,
                user=user,
                schema=schema,
            )
        notes = _load_json_payload(_parse_json_object(raw))
    except Exception as exc:  # report the category, never a body or echoed source
        elapsed = time.monotonic() - started
        print(f"[FAIL] {route.slug} generation ({elapsed:.1f}s): {type(exc).__name__}")
        return False
    elapsed = time.monotonic() - started
    print(
        f"[PASS] {route.slug} generation ({elapsed:.1f}s, {len(excerpt)} chars): "
        f"sections={len(notes.sections)}"
    )
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--generate",
        metavar="TRANSCRIPT_FILE",
        help="OPT-IN, CONSUMES PROVIDER QUOTA: send one real note request per "
             "route over this text file (never used by CI or the test suite)",
    )
    parser.add_argument(
        "--show-config",
        action="store_true",
        help="offline only: print effective routing and non-secret settings, "
             "then exit without any network call or quota use",
    )
    args = parser.parse_args(argv)

    settings = from_env()
    routes = note_routes(settings)
    _print_config(settings, routes)
    if args.show_config:
        print("Offline inspection complete: no network request was made.")
        return 0 if routes else 1
    if not routes:
        print("[FAIL] no note provider is configured (see docs/PROVIDERS.md)")
        return 1

    print(
        "\nStage 1/2 — METADATA ONLY: authentication, model entitlement and "
        "published limits. No generation quota is used."
    )
    print(
        "A metadata PASS does NOT prove that text generation works. "
        "Run the opt-in --generate stage to verify that."
    )
    ok = True
    for route in routes:
        if route.slug == "avalai":
            ok = _check_avalai(route) and ok
        elif route.slug == "openrouter":
            ok = _check_openrouter(route) and ok
        elif route.slug == "gemini":
            ok = _check_gemini(route) and ok

    if args.generate:
        try:
            text = Path(args.generate).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"[FAIL] cannot read transcript file {args.generate!r}: {exc}")
            return 1
        if not text.strip():
            print(f"[FAIL] transcript file {args.generate!r} is empty")
            return 1
        print(
            "\nStage 2/2 — REAL GENERATION: CONSUMES PROVIDER QUOTA. "
            f"One map-style note request per route over {len(text)} chars of "
            "non-sensitive test text (plus the pipeline's bounded retries)."
        )
        for route in routes:
            ok = asyncio.run(_generate(route, settings, text)) and ok
    else:
        print(
            "\nMetadata stage complete. Generation was NOT tested; run "
            "`python -m scripts.probe_note_providers --generate TRANSCRIPT_FILE` "
            "before promoting a route to production (this consumes quota)."
        )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
