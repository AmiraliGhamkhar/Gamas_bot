"""Operator smoke test for the AvalAI note route (never run by the bot).

The offline suite proves the AvalAI contract against a mock: the base URL, the
bearer header, the ``json_schema`` envelope and the ``json_object`` fallback are
all asserted in ``tests/test_provider_contracts_and_quality.py``. What it cannot
prove is that a *deployment's* key, model id and tier actually answer. That is
this script::

    python -m scripts.smoke_avalai                 # metadata only
    python -m scripts.smoke_avalai --generate lecture.txt
    python -m scripts.smoke_avalai --generate lecture.txt --full

Three stages, each reported before the next one spends quota:

``config``
    The route the bot would actually use (base URL, model, key present) and the
    note settings that size a request. No network call.
``metadata``
    ``GET {base}/models`` with the configured key: is the model listed, and what
    limits does the gateway publish for it?
``generate`` (opt-in)
    One real notebook request through the bot's own request builder, message
    template and JSON parser. It prints the served model, elapsed time, token
    usage (including the cached-input count, which is how the static-prompt
    cache is verified) and the section count. ``--full`` runs the whole pipeline
    — chunking, the editorial compile and the coverage fill — and reports the
    final booklet, so the compile prompt and the gates are exercised too.

Nothing is written to the database, no Telegram client is created, and the API
key is never printed. Exit code is 1 when any stage fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gamas_bot.config import (  # noqa: E402
    DEFAULT_AVALAI_BASE_URL,
    DEFAULT_AVALAI_MODEL,
    from_env,
    note_routes,
)

#: The snippet used when ``--generate`` gets no file. It is deliberately small:
#: the smoke test proves the request shape, not the model's taste.
DEFAULT_SNIPPET = (
    "درفش کاویانی یکی از نشانه‌های شاهی در ایران باستان است. این درفش از پارچهٔ "
    "زرین‌بفت ساخته می‌شد و در حدود ۹ متر طول داشت. هر سپاه برای خود درفشی "
    "داشت که هنگام نبرد آن را برمی‌افراشت."
)


def _print_config(settings) -> object | None:
    """Report the configured route; return it, or ``None`` when unusable."""
    routes = note_routes(settings)
    avalai = [route for route in routes if route.slug == "avalai"]
    print("AvalAI route as this deployment would build it:")
    print(f"  AVALAI_API_KEY   {'set' if settings.avalai_api_key else 'MISSING'}")
    print(f"  AVALAI_BASE_URL  {settings.avalai_base_url or DEFAULT_AVALAI_BASE_URL}")
    print(f"  AVALAI_MODEL     {settings.avalai_model or DEFAULT_AVALAI_MODEL}")
    print(
        "  sizing           "
        f"NOTE_CONTEXT_TOKENS={settings.note_context_tokens} "
        f"NOTE_CHUNK_CHARS={settings.note_chunk_chars} "
        f"NOTE_MAX_OUTPUT_TOKENS={settings.note_max_output_tokens}"
    )
    print(
        "  switches         "
        f"NOTE_STRUCTURED_OUTPUT={int(settings.note_structured_output)} "
        f"NOTE_COMPLETENESS_FILL={int(settings.note_completeness_fill)} "
        f"NOTE_QUALITY_GATE={settings.note_quality_gate}"
    )
    if not avalai:
        reason = (
            "AVALAI_API_KEY is empty"
            if not settings.avalai_api_key
            else "'avalai' is not in NOTE_PROVIDERS"
        )
        print(f"[FAIL] no AvalAI route is eligible ({reason}); see docs/PROVIDERS.md")
        return None
    route = avalai[0]
    print(f"[PASS] route is eligible: {route.label} model={route.model} base={route.base_url}")
    return route


async def _check_metadata(route, settings) -> bool:
    """``GET /models`` with the real key: is the model entitled, and how big is it?"""
    import aiohttp

    from gamas_bot.llm import _clean_api_key, _endpoint

    url = _endpoint(route.base_url, "models")
    headers = {"Authorization": f"Bearer {_clean_api_key(route.api_key)}"}
    started = time.monotonic()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                status = response.status
                payload = await response.json(content_type=None)
    except Exception as exc:  # noqa: BLE001 - report any transport failure verbatim
        print(f"[FAIL] metadata GET {url}: {type(exc).__name__}: {exc}")
        return False
    elapsed = time.monotonic() - started
    if status != 200 or not isinstance(payload, dict):
        detail = json.dumps(payload, ensure_ascii=False)[:200] if payload else ""
        print(f"[FAIL] metadata GET {url}: HTTP {status} {detail}")
        return False
    entries = payload.get("data")
    if not isinstance(entries, list):
        print("[FAIL] metadata: unexpected response shape (no data list)")
        return False
    ids = [entry.get("id") for entry in entries if isinstance(entry, dict)]
    entitled = route.model in ids
    print(
        f"[{'PASS' if entitled else 'FAIL'}] model {route.model!r} "
        f"{'is' if entitled else 'is NOT'} listed ({len(ids)} model(s), {elapsed:.1f}s)"
    )
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") == route.model:
            limits = {
                key: entry[key]
                for key in ("context_window", "context_length", "max_output_tokens")
                if key in entry
            }
            if limits:
                print(f"       published metadata: {limits}")
            break
    return entitled


async def _generate(route, settings, text: str) -> bool:
    """One real chunk request, built and parsed by the bot's own code."""
    import aiohttp

    from gamas_bot.llm import _complete_json_on_route, _usage_summary
    from gamas_bot.notes import (
        _response_schema,
        build_chunk_message,
        build_system_prompt,
        effective_chunk_chars,
    )

    system = build_system_prompt("full")
    schema = _response_schema(settings)
    excerpt = text[: effective_chunk_chars(settings, system_prompt=system)]
    message = build_chunk_message(excerpt, index=1, total=1)
    print(
        f"\nGeneration probe: {len(excerpt)} chars of source, "
        f"system={len(system)} chars, schema={'yes' if schema else 'no'}"
    )
    started = time.monotonic()
    async with aiohttp.ClientSession() as session:
        try:
            payload = await _complete_json_on_route(
                route,
                session,
                settings,
                system=system,
                user=message,
                retry_on_timeout=True,
                schema=schema,
            )
        except Exception as exc:  # noqa: BLE001 - report the provider's answer verbatim
            elapsed = time.monotonic() - started
            print(f"[FAIL] generation ({elapsed:.1f}s): {type(exc).__name__}: {exc}")
            return False
    elapsed = time.monotonic() - started
    sections = payload.get("sections")
    if not isinstance(sections, list) or not sections:
        print(f"[FAIL] generation answered without a non-empty sections list: {list(payload)}")
        return False
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    served, input_tokens, output_tokens, _total = _usage_summary(payload, route.style)
    cached = None
    for key in ("cached_tokens", "cache_read_input_tokens"):
        if isinstance(usage.get(key), int):
            cached = usage[key]
            break
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    if cached is None and isinstance(details, dict):
        for key in ("cached_tokens", "cache_read"):
            if isinstance(details.get(key), int):
                cached = details[key]
                break
    print(
        f"[PASS] generation ({elapsed:.1f}s) model={served or route.model} "
        f"sections={len(sections)} input_tokens={input_tokens} "
        f"output_tokens={output_tokens} cached_input_tokens={cached if cached is not None else '-'}"
    )
    if cached:
        # A cached count here means the static prefix hit the provider's cache —
        # the whole reason the prompt is one byte-identical string.
        print("       static prompt prefix was served from cache on this request")
    return True


async def _full_pipeline(route, settings, text: str) -> bool:
    """The whole booklet pipeline, so chunking, compile and fill are exercised."""
    import aiohttp

    from gamas_bot.notes import structure_transcript

    print("\nFull pipeline probe: chunking -> merge -> editorial compile -> coverage fill")
    started = time.monotonic()
    async with aiohttp.ClientSession() as session:
        try:
            notes = await structure_transcript(text, settings, session)
        except Exception as exc:  # noqa: BLE001 - report the pipeline's own error
            elapsed = time.monotonic() - started
            print(f"[FAIL] pipeline ({elapsed:.1f}s): {type(exc).__name__}: {exc}")
            return False
    elapsed = time.monotonic() - started
    # Rendering the DOCX proves the answer survives the exporter too; nothing is
    # written to disk — this is a diagnostic, not a viewer.
    try:
        from gamas_bot.docx_export import DocumentMeta, build_notes_docx

        docx = build_notes_docx(
            notes,
            meta=DocumentMeta(reference="smoke", source_name="smoke"),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] export: {type(exc).__name__}: {exc}")
        return False
    size = len(docx)
    print(
        f"[PASS] pipeline ({elapsed:.1f}s) title={notes.display_title!r} "
        f"sections={len(notes.sections)} docx={size} bytes"
    )
    return True


async def _run(args, settings) -> int:
    route = _print_config(settings)
    if route is None:
        return 1
    ok = await _check_metadata(route, settings)
    if not args.generate and not args.snippet:
        return 0 if ok else 1

    if args.generate:
        path = Path(args.generate)
        if not path.is_file():
            print(f"[FAIL] transcript file not found: {path}")
            return 1
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            print(f"[FAIL] transcript file is empty: {path}")
            return 1
    else:
        text = DEFAULT_SNIPPET
        print("\nNo --generate file given; using the built-in three-sentence snippet.")

    ok = await _generate(route, settings, text) and ok
    if args.full:
        ok = await _full_pipeline(route, settings, text) and ok
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--generate",
        metavar="TRANSCRIPT_FILE",
        help="also send one real note request over this text file (consumes quota)",
    )
    parser.add_argument(
        "--snippet",
        action="store_true",
        help="run the generation probe on the built-in snippet instead of a file",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="also run the whole booklet pipeline over the same text (more quota)",
    )
    args = parser.parse_args(argv)
    if args.full and not (args.generate or args.snippet):
        parser.error("--full needs --generate TRANSCRIPT_FILE or --snippet")

    settings = from_env()
    return asyncio.run(_run(args, settings))


if __name__ == "__main__":
    raise SystemExit(main())
