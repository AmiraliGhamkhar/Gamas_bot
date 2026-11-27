"""Stdlib WSGI JSON API & static Mini App handler for cPanel Passenger."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote

from .config import PROJECT_DIR, Settings, from_env
from .db import Database
from .db_broker import db_reuse_enabled, get_broker
from .explore_recommender import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    recommend_explore_page,
)
from .explore_sources import row_source_payload
from .rewards import REWARD_CATALOG as _REWARD_CATALOG
from .study_storage import resolve_private_asset
from .telegram_auth import InitDataError, validate_init_data

logger = logging.getLogger(__name__)

_PACK_RE = re.compile(r"^/api/packs/(\d+)$")
_ASSET_DL_RE = re.compile(r"^/api/assets/(\d+)/download$")

_V1_LIB_PACK_RE = re.compile(r"^/api/v1/library/(\d+)$")
_V1_LIB_ASSETS_RE = re.compile(r"^/api/v1/library/(\d+)/assets$")
_V1_LIB_DL_RE = re.compile(r"^/api/v1/library/(\d+)/download/(\d+)$")
_V1_REVIEW_Q_RE = re.compile(r"^/api/v1/review/questions/(\d+)$")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_asset(asset: dict[str, Any]) -> dict[str, Any]:
    return {
        "asset_id": asset["asset_id"],
        "pack_id": asset["pack_id"],
        "kind": asset["kind"],
        "title": asset["title"],
        "mime_type": asset["mime_type"],
        "byte_size": asset["byte_size"],
        "sha256": asset["sha256"],
        "created_at": asset["created_at"],
    }


def _json_response(
    start_response: Callable,
    status: str,
    payload: Any,
    *,
    method: str = "GET",
) -> list[bytes]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    start_response(
        status,
        [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
        ],
    )
    return [b"" if method == "HEAD" else body]


def _query_params(environ: dict[str, Any]) -> dict[str, str]:
    """First value of every query-string parameter (empty for no query)."""
    raw = str(environ.get("QUERY_STRING") or "")
    if not raw:
        return {}
    return {key: values[0] for key, values in parse_qs(raw).items() if values}


def _paging(params: dict[str, str], *, default: int = 50, maximum: int = 200) -> tuple[int, int]:
    """Clamped ``(limit, offset)`` from the query string; never raises."""
    try:
        limit = int(params.get("limit", default))
    except (TypeError, ValueError):
        limit = default
    try:
        offset = int(params.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    return max(1, min(maximum, limit)), max(0, offset)


# --------------------------------------------------------------- settings cache
_env_settings_cache: tuple[tuple[Any, ...], Settings] | None = None


def _env_file_stamp() -> tuple[Any, ...]:
    """Cheap staleness key for the ``.env`` files ``from_env`` would load."""
    stamp: list[Any] = []
    for candidate in (PROJECT_DIR / ".env", Path.cwd() / ".env"):
        try:
            stat = candidate.stat()
        except OSError:
            stamp.append(None)
        else:
            stamp.append((stat.st_mtime_ns, stat.st_size))
    return tuple(stamp)


def _cached_env_settings() -> Settings:
    """``from_env()`` once per process; re-parsed only when ``.env`` changes.

    Production calls ``make_wsgi_app()`` without explicit settings, and parsing
    ~100 environment variables on every request cost ~0.4 ms for a result that
    only changes when the deployment file changes.
    """
    global _env_settings_cache
    key = _env_file_stamp()
    if _env_settings_cache is not None and _env_settings_cache[0] == key:
        return _env_settings_cache[1]
    settings = from_env()
    _env_settings_cache = (key, settings)
    return settings


# --------------------------------------------------------------- gzip helpers
_COMPRESSIBLE_SUFFIXES = {
    ".js",
    ".mjs",
    ".css",
    ".html",
    ".json",
    ".map",
    ".svg",
    ".txt",
    ".xml",
}
_GZIP_MIN_BYTES = 1024
_GZIP_CACHE_MAX = 32
_gzip_cache: dict[tuple[str, int, int], bytes] = {}


def _accepts_gzip(accept_encoding: str) -> bool:
    """True when the client explicitly allows ``gzip`` (q>0)."""
    for part in accept_encoding.lower().split(","):
        token, _, params = part.strip().partition(";")
        if token != "gzip":
            continue
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip() == "q":
                try:
                    return float(value.strip() or "1") > 0
                except ValueError:
                    return True
        return True
    return False


def _gzip_bytes(data: bytes, cache_key: tuple[str, int, int]) -> bytes:
    """Compress once per (file, mtime, size); the dist is immutable per deploy."""
    cached = _gzip_cache.get(cache_key)
    if cached is not None:
        return cached
    compressed = gzip.compress(data, compresslevel=6)
    if len(_gzip_cache) >= _GZIP_CACHE_MAX:
        _gzip_cache.clear()
    _gzip_cache[cache_key] = compressed
    return compressed


def _read_json_body(environ: dict[str, Any]) -> dict[str, Any]:
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0 or length > 1_048_576:
        return {}
    raw = environ["wsgi.input"].read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _authenticate(environ: dict[str, Any], settings: Settings) -> int:
    init_data = environ.get("HTTP_X_TELEGRAM_INIT_DATA", "")
    auth = validate_init_data(init_data, settings.bot_token)
    return int(auth["user_id"])


def make_wsgi_app(
    settings: Settings | None = None,
    *,
    miniapp_dist: Path | None = None,
    private_assets_dir: Path | None = None,
) -> Callable:
    """Create a WSGI callable for ``/api/*``, ``/api/v1/*``, and ``/app/*``."""
    dist_dir = (
        Path(miniapp_dist)
        if miniapp_dist is not None
        else Path(__file__).resolve().parent.parent / "miniapp" / "dist"
    )

    def _get_settings() -> Settings:
        return settings if settings is not None else _cached_env_settings()

    def _assets_root(cfg: Settings) -> Path:
        if private_assets_dir is not None:
            return Path(private_assets_dir)
        return Path(cfg.temp_dir).parent / "private_assets"

    async def _with_db(cfg: Settings, fn: Callable[[Database], Any]) -> Any:
        # One shared connection per data file per process (~5-8x request
        # capacity); GAMAS_DB_REUSE=0 restores the connect/close-per-request
        # path, and :memory: databases can never be shared.
        db_path = str(cfg.db_path)
        if db_reuse_enabled() and db_path not in {":memory:", ""}:
            return await get_broker().run(Path(db_path), fn)
        db = Database(cfg.db_path)
        await db.connect()
        try:
            return await fn(db)
        finally:
            await db.close()

    def _safe_filename(name: str) -> str:
        """A download name that can never break the header or the path."""
        cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name or "").strip("._")
        return cleaned or "file"

    async def _read_asset(
        db: Database, user_id: int, cfg: Settings, asset: dict[str, Any]
    ) -> tuple[bytes, str, str] | None:
        """Resolve one asset to ``(bytes, content_type, filename)``.

        A pack asset is a *reference*: the bytes stay in the store that already
        owns them (transcription, submission artifact, document text or library
        blob), so nothing is copied and every source keeps a single owner. The
        owning row's ``user_id`` is re-checked here so a reference can never be
        used to read another account's content.
        """
        source_kind = str(asset.get("source_kind") or "file")
        ref = str(asset.get("source_ref") or "")
        mime = str(asset.get("mime_type") or "application/octet-stream")
        if source_kind == "file":
            try:
                path = resolve_private_asset(_assets_root(cfg), str(asset["storage_path"]))
            except ValueError:
                return None
            if not path.is_file():
                return None
            return path.read_bytes(), mime, path.name
        if source_kind == "artifact":
            submission_part, _, kind = ref.partition(":")
            if not submission_part.isdigit() or not kind:
                return None
            row = await db.binary_artifact(int(submission_part), int(user_id), kind)
            if row is None:
                return None
            return (
                bytes(row.get("content") or b""),
                str(row.get("content_type") or mime),
                str(row.get("filename") or kind),
            )
        if source_kind == "transcription":
            if not ref.isdigit():
                return None
            submission = await db.submission(int(ref))
            if submission is None or int(submission.user_id) != int(user_id):
                return None
            row = await db.transcription(int(ref))
            text = str((row or {}).get("text") or "")
            return text.encode("utf-8"), mime, f"transcript-{ref}.txt"
        if source_kind == "document_text":
            if not ref.isdigit():
                return None
            document = await db.document_get(int(user_id), int(ref))
            if document is None:
                return None
            return (
                str(document.get("text") or "").encode("utf-8"),
                mime,
                f"document-{ref}.txt",
            )
        if source_kind == "library_blob":
            if not ref.isdigit():
                return None
            blob = await db.library_file_blob(int(user_id), int(ref))
            if blob is None:
                return None
            return (
                bytes(blob.get("content") or b""),
                str(blob.get("content_type") or mime),
                str(blob.get("filename") or f"file-{ref}"),
            )
        return None

    def _serve_asset(
        start_response: Callable, resolved: tuple[bytes, str, str] | None
    ) -> list[bytes]:
        if resolved is None:
            return _json_response(
                start_response, "404 Not Found", {"error": "not found"}
            )
        data, mime, filename = resolved
        start_response(
            "200 OK",
            [
                ("Content-Type", mime),
                ("Content-Length", str(len(data))),
                (
                    "Content-Disposition",
                    "attachment; "
                    f'filename="{_safe_filename(filename)}"; '
                    f"filename*=UTF-8''{quote(filename or _safe_filename(filename), safe='')}",
                ),
                ("Cache-Control", "private, no-store"),
            ],
        )
        return [data]

    def app(environ: dict[str, Any], start_response: Callable) -> list[bytes]:
        path = environ.get("PATH_INFO", "/") or "/"
        method = (environ.get("REQUEST_METHOD") or "GET").upper()

        if path in {"/api/health", "/api/v1/health"} and method in {"GET", "HEAD"}:
            return _json_response(
                start_response, "200 OK", {"status": "ok"}, method=method
            )

        if path == "/api/v1/healthz" and method in {"GET", "HEAD"}:
            # Deep health for uptime monitors: the shallow /api/health above
            # must stay cheap and stable, so database reachability lives here.
            async def _healthz(db: Database) -> dict[str, Any]:
                await db.db.execute_fetchall("SELECT 1")
                return {
                    "status": "ok",
                    "db": "ok",
                    "time": _utc_now_iso(),
                }

            try:
                payload = asyncio.run(_with_db(_get_settings(), _healthz))
                return _json_response(
                    start_response, "200 OK", payload, method=method
                )
            except Exception:
                logger.warning("healthz database check failed", exc_info=True)
                return _json_response(
                    start_response,
                    "503 Service Unavailable",
                    {"status": "error", "db": "down"},
                    method=method,
                )

        if path.startswith("/api/"):
            cfg = _get_settings()
            try:
                user_id = _authenticate(environ, cfg)
            except InitDataError as exc:
                return _json_response(
                    start_response,
                    "401 Unauthorized",
                    {"error": str(exc)},
                    method=method,
                )

            # ------------------------------------------------ Account APIs
            if path in {"/api/me", "/api/v1/me"} and method == "GET":
                async def _me(db: Database) -> dict[str, Any]:
                    await db.ensure_user(user_id, "")
                    ident = await db.user_identity(user_id) or {}
                    # ``user_identity`` carries no credit balance, so reading one
                    # from it always produced 0. Ask the ledger instead: this is
                    # the same authoritative number a redemption increases.
                    bal = int(await db.available_seconds(int(user_id)))
                    pts = await db.study_packs.reward_points_balance(user_id)
                    return {
                        "user_id": user_id,
                        "credit_seconds": bal,
                        "reward_points": int(pts),
                        "free_uses": ident.get("free_uses", {"quiz": True}),
                        "banned": bool(ident.get("banned", False)),
                        # The Mini App builds invite/share/support/purchase links
                        # from this, so no deployment name is baked into the UI.
                        "bot_username": cfg.bot_username,
                    }

                return _json_response(
                    start_response, "200 OK", asyncio.run(_with_db(cfg, _me))
                )

            if path == "/api/v1/me/subscription" and method == "GET":
                # ``required_channel`` never existed on Settings: the endpoint
                # raised AttributeError for every caller. The field is
                # ``subscription_channel``, and its public URL is derived from it.
                channel = (cfg.subscription_channel or "").strip()
                return _json_response(
                    start_response,
                    "200 OK",
                    {
                        "required_channel": channel or None,
                        "channel_url": cfg.subscription_channel_url if channel else None,
                        "required": bool(cfg.subscription_required and channel),
                    },
                )

            if path == "/api/v1/me/plan" and method == "GET":
                """The user's current plan, read from the rows that own it.

                Nothing here is derived from the client: the plan is the one on
                the latest *approved* payment, and the active window is the
                earliest expiry among live credit lots. An admin-granted
                unlimited flag is reported as ``unlimited``.
                """

                async def _plan(db: Database) -> dict[str, Any]:
                    unlimited = bool(await db.is_unlimited(int(user_id)))
                    credit = int(await db.available_seconds(int(user_id)))
                    expiry_rows = list(await db.db.execute_fetchall(
                        """
                        SELECT MIN(expires_at) AS expires_at
                        FROM ledger
                        WHERE user_id = ? AND kind = 'credit'
                          AND expires_at IS NOT NULL
                          AND julianday(expires_at) > julianday('now')
                        """,
                        (int(user_id),),
                    ))
                    expires_at = expiry_rows[0]["expires_at"] if expiry_rows else None
                    plan_rows = list(await db.db.execute_fetchall(
                        """
                        SELECT p.plan_code,
                               p.purchased_seconds, p.purchased_price, p.purchased_validity_days,
                               p.created_at, p.reviewed_at,
                               pl.hours AS plan_hours, pl.price AS plan_price,
                               pl.original_price AS plan_original_price,
                               pl.validity_days AS plan_validity_days
                        FROM payments p
                        LEFT JOIN plans pl ON pl.code = p.plan_code
                        WHERE p.user_id = ? AND p.status = 'approved'
                        ORDER BY p.payment_id DESC LIMIT 1
                        """,
                        (int(user_id),),
                    ))
                    plan: dict[str, Any] | None = None
                    if plan_rows:
                        row = dict(plan_rows[0])
                        purchased_seconds = row.get("purchased_seconds")
                        if purchased_seconds is None and row.get("plan_hours") is not None:
                            purchased_seconds = int(round(float(row["plan_hours"]) * 3600))
                        hours = (
                            int(round(int(purchased_seconds) / 3600))
                            if purchased_seconds is not None
                            else row.get("plan_hours")
                        )
                        purchased_price = row.get("purchased_price")
                        if purchased_price is None:
                            purchased_price = row.get("plan_price")
                        purchased_validity_days = row.get("purchased_validity_days")
                        if purchased_validity_days is None:
                            purchased_validity_days = row.get("plan_validity_days")
                        plan = {
                            "code": row.get("plan_code"),
                            "hours": hours,
                            "price": (
                                int(purchased_price)
                                if purchased_price is not None
                                else None
                            ),
                            "original_price": (
                                int(row["plan_original_price"])
                                if row.get("plan_original_price") is not None
                                else None
                            ),
                            "validity_days": purchased_validity_days,
                            "purchased_at": row.get("reviewed_at") or row.get("created_at"),
                        }
                    return {
                        "unlimited": unlimited,
                        "credit_seconds": credit,
                        "expires_at": expires_at,
                        "plan": plan,
                    }

                return _json_response(
                    start_response, "200 OK", asyncio.run(_with_db(cfg, _plan))
                )

            if path == "/api/v1/plans" and method == "GET":
                """Purchase options, exactly as the billing table defines them.

                Prices come from ``plans`` (the same rows the bot's wallet
                renders), so the Mini App can never show a different price.
                """

                async def _plans(db: Database) -> dict[str, Any]:
                    rows = await db.plans()
                    return {
                        "plans": [
                            {
                                "code": row["code"],
                                "hours": row["hours"],
                                "price": int(row["price"]),
                                "original_price": (
                                    int(row["original_price"])
                                    if row.get("original_price") is not None
                                    else None
                                ),
                                "validity_days": row.get("validity_days"),
                            }
                            for row in rows
                            if int(row["price"]) > 0
                        ]
                    }

                return _json_response(
                    start_response, "200 OK", asyncio.run(_with_db(cfg, _plans))
                )

            if path == "/api/v1/me/credits" and method == "GET":
                async def _credits(db: Database) -> dict[str, Any]:
                    ident = await db.user_identity(user_id) or {}
                    return {
                        "credit_seconds": int(await db.available_seconds(int(user_id))),
                        "free_uses": ident.get("free_uses", {"quiz": True}),
                    }

                return _json_response(
                    start_response, "200 OK", asyncio.run(_with_db(cfg, _credits))
                )

            if path == "/api/v1/me/referrals" and method == "GET":
                async def _refs(db: Database) -> dict[str, Any]:
                    rows = await db.db.execute_fetchall(
                        "SELECT COUNT(*) AS cnt FROM referrals WHERE referrer_id = ?",
                        (user_id,),
                    )
                    return {"invited_count": int(rows[0]["cnt"]) if rows else 0}

                return _json_response(
                    start_response, "200 OK", asyncio.run(_with_db(cfg, _refs))
                )

            if path == "/api/v1/me/payments" and method == "GET":
                async def _pays(db: Database) -> dict[str, Any]:
                    # ``payments`` has no ``amount_toman`` column: the price is
                    # snapshotted at order time in ``purchased_price``. Selecting
                    # the missing column made this endpoint fail for every user.
                    rows = await db.db.execute_fetchall(
                        """
                        SELECT payment_id, plan_code, status, created_at,
                               COALESCE(purchased_price, 0) AS amount_toman,
                               purchased_seconds
                        FROM payments WHERE user_id = ?
                        ORDER BY payment_id DESC LIMIT 50
                        """,
                        (user_id,),
                    )
                    return {"payments": [dict(r) for r in rows]}

                return _json_response(
                    start_response, "200 OK", asyncio.run(_with_db(cfg, _pays))
                )

            # ------------------------------------------------ Library APIs
            if path in {"/api/packs", "/api/v1/library"} and method == "GET":
                params = _query_params(environ)
                limit, offset = _paging(params, default=50, maximum=200)

                async def _list(db: Database) -> dict[str, Any]:
                    packs = await db.study_packs.study_pack_list(
                        user_id, limit=limit, offset=offset
                    )
                    return {
                        "packs": packs,
                        "limit": limit,
                        "offset": offset,
                        "total": await db.study_packs.study_pack_count(user_id),
                    }

                return _json_response(
                    start_response, "200 OK", asyncio.run(_with_db(cfg, _list))
                )

            if path == "/api/packs" and method == "POST":
                body = _read_json_body(environ)
                title = str(body.get("title") or "").strip()
                if not title:
                    return _json_response(
                        start_response,
                        "400 Bad Request",
                        {"error": "title is required"},
                    )
                source_type = str(body.get("source_type") or "upload")[:40]

                async def _create(db: Database) -> dict[str, Any]:
                    await db.ensure_user(user_id, "")
                    pack_id = await db.study_packs.study_pack_create(
                        user_id, title, source_type=source_type
                    )
                    pack = await db.study_packs.study_pack_get(user_id, pack_id)
                    return {"pack": pack}

                return _json_response(
                    start_response, "201 Created", asyncio.run(_with_db(cfg, _create))
                )

            m_pack = _PACK_RE.match(path) or _V1_LIB_PACK_RE.match(path)
            if m_pack:
                pack_id = int(m_pack.group(1))
                if method == "GET":
                    async def _get(db: Database) -> dict[str, Any] | None:
                        pack = await db.study_packs.study_pack_get(user_id, pack_id)
                        if pack is None:
                            return None
                        assets = await db.study_packs.study_pack_assets(user_id, pack_id)
                        return {
                            "pack": pack,
                            "assets": [_sanitize_asset(a) for a in assets],
                            # Review is the pack's own questions; the count lets
                            # the detail screen offer Review only when it exists.
                            "question_count": await db.review.review_question_count(
                                user_id, pack_id
                            ),
                        }

                    res = asyncio.run(_with_db(cfg, _get))
                    if res is None:
                        return _json_response(
                            start_response, "404 Not Found", {"error": "not found"}
                        )
                    return _json_response(start_response, "200 OK", res)

                if method == "DELETE":
                    async def _del(db: Database) -> bool:
                        return await db.study_packs.study_pack_delete(user_id, pack_id)

                    if not asyncio.run(_with_db(cfg, _del)):
                        return _json_response(
                            start_response, "404 Not Found", {"error": "not found"}
                        )
                    return _json_response(
                        start_response, "200 OK", {"deleted": True, "pack_id": pack_id}
                    )

            m_assets = _V1_LIB_ASSETS_RE.match(path)
            if m_assets and method == "GET":
                pack_id = int(m_assets.group(1))

                async def _list_assets(db: Database) -> list[dict[str, Any]] | None:
                    if await db.study_packs.study_pack_get(user_id, pack_id) is None:
                        return None
                    assets = await db.study_packs.study_pack_assets(user_id, pack_id)
                    return [_sanitize_asset(a) for a in assets]

                res = asyncio.run(_with_db(cfg, _list_assets))
                if res is None:
                    return _json_response(
                        start_response, "404 Not Found", {"error": "not found"}
                    )
                return _json_response(start_response, "200 OK", {"assets": res})

            m_v1_dl = _V1_LIB_DL_RE.match(path)
            if m_v1_dl and method == "GET":
                pack_id, asset_id = int(m_v1_dl.group(1)), int(m_v1_dl.group(2))

                async def _get_v1_asset(db: Database) -> dict[str, Any] | None:
                    asset = await db.study_packs.study_asset_get(user_id, asset_id)
                    if asset is None or int(asset["pack_id"]) != pack_id:
                        return None
                    return asset

                asset = asyncio.run(_with_db(cfg, _get_v1_asset))
                if asset is None:
                    resolved = None
                elif str(asset.get("source_kind") or "file") == "file":
                    try:
                        file_path = resolve_private_asset(
                            _assets_root(cfg), str(asset["storage_path"])
                        )
                    except ValueError:
                        resolved = None
                    else:
                        resolved = (
                            (file_path.read_bytes(),
                             str(asset.get("mime_type") or "application/octet-stream"),
                             file_path.name)
                            if file_path.is_file() else None
                        )
                else:
                    resolved = asyncio.run(
                        _with_db(cfg, lambda db: _read_asset(db, user_id, cfg, asset))
                    )
                return _serve_asset(start_response, resolved)

            m_dl = _ASSET_DL_RE.match(path)
            if m_dl and method == "GET":
                asset_id = int(m_dl.group(1))

                async def _get_asset(db: Database) -> dict[str, Any] | None:
                    return await db.study_packs.study_asset_get(user_id, asset_id)

                asset = asyncio.run(_with_db(cfg, _get_asset))
                if asset is None:
                    resolved = None
                elif str(asset.get("source_kind") or "file") == "file":
                    try:
                        file_path = resolve_private_asset(
                            _assets_root(cfg), str(asset["storage_path"])
                        )
                    except ValueError:
                        resolved = None
                    else:
                        resolved = (
                            (file_path.read_bytes(),
                             str(asset.get("mime_type") or "application/octet-stream"),
                             file_path.name)
                            if file_path.is_file() else None
                        )
                else:
                    resolved = asyncio.run(
                        _with_db(cfg, lambda db: _read_asset(db, user_id, cfg, asset))
                    )
                return _serve_asset(start_response, resolved)

            # ------------------------------------------------- Review APIs
            # Review is a Study Pack view: questions are read through the pack
            # that owns them, grouped by it, and titled with the pack's own
            # title. Questions whose source predates Study Packs are returned as
            # explicit legacy groups instead of being attached to a pack they
            # never belonged to.
            if path == "/api/v1/review/groups" and method == "GET":
                params = _query_params(environ)
                limit, offset = _paging(params)

                async def _groups(db: Database) -> dict[str, Any]:
                    groups, total = await db.review.review_groups(
                        user_id, limit=limit, offset=offset
                    )
                    return {
                        "groups": groups,
                        "limit": limit,
                        "offset": offset,
                        "total": total,
                    }

                return _json_response(
                    start_response, "200 OK", asyncio.run(_with_db(cfg, _groups))
                )

            if path == "/api/v1/review/questions" and method == "GET":
                params = _query_params(environ)
                limit, offset = _paging(params)
                review_pack_id: int | None = None
                review_legacy_item_id: int | None = None
                try:
                    if params.get("pack_id"):
                        review_pack_id = int(params["pack_id"])
                    group_key = str(params.get("group_key") or "")
                    if group_key.startswith("pack:"):
                        review_pack_id = int(group_key[len("pack:"):])
                    elif group_key.startswith("legacy:"):
                        review_legacy_item_id = int(group_key[len("legacy:"):])
                    elif group_key:
                        raise ValueError("unknown group")
                except ValueError:
                    return _json_response(
                        start_response, "400 Bad Request", {"error": "invalid group"}
                    )

                async def _r_qs(db: Database) -> dict[str, Any]:
                    questions, total = await db.review.review_questions(
                        user_id,
                        pack_id=review_pack_id,
                        legacy_item_id=review_legacy_item_id,
                        limit=limit,
                        offset=offset,
                    )
                    return {
                        "questions": questions,
                        "limit": limit,
                        "offset": offset,
                        "total": total,
                    }

                return _json_response(
                    start_response, "200 OK", asyncio.run(_with_db(cfg, _r_qs))
                )

            m_rq = _V1_REVIEW_Q_RE.match(path)
            if m_rq and method == "GET":
                qid = int(m_rq.group(1))

                async def _r_q(db: Database) -> dict[str, Any] | None:
                    return await db.review.review_question(user_id, qid)

                res = asyncio.run(_with_db(cfg, _r_q))
                if res is None:
                    return _json_response(
                        start_response, "404 Not Found", {"error": "not found"}
                    )
                return _json_response(start_response, "200 OK", {"question": res})

            if path == "/api/v1/review/answers" and method == "POST":
                body = _read_json_body(environ)
                raw_qid = body.get("question_id")
                raw_chosen = body.get("chosen_index")
                if not isinstance(raw_qid, int) or not isinstance(raw_chosen, int):
                    return _json_response(
                        start_response, "400 Bad Request", {"error": "invalid payload"}
                    )
                answer_qid: int = raw_qid
                answer_choice: int = raw_chosen
                if (
                    isinstance(raw_qid, bool)
                    or isinstance(raw_chosen, bool)
                    or not 0 <= answer_choice <= 3
                ):
                    return _json_response(
                        start_response, "400 Bad Request", {"error": "invalid payload"}
                    )

                async def _r_ans(db: Database) -> dict[str, Any] | None:
                    return await db.review.review_answer(
                        user_id, answer_qid, answer_choice
                    )

                res = asyncio.run(_with_db(cfg, _r_ans))
                if res is None:
                    return _json_response(
                        start_response, "404 Not Found", {"error": "not found"}
                    )
                return _json_response(start_response, "200 OK", res)

            if path == "/api/v1/review/share" and method == "POST":
                body = _read_json_body(environ)
                raw_share_qid = body.get("question_id")
                if not isinstance(raw_share_qid, int) or isinstance(raw_share_qid, bool):
                    return _json_response(
                        start_response, "400 Bad Request", {"error": "invalid payload"}
                    )
                share_qid: int = raw_share_qid

                async def _share(db: Database) -> dict[str, Any] | None:
                    return await db.review.review_share(user_id, share_qid)

                res = asyncio.run(_with_db(cfg, _share))
                if res is None:
                    return _json_response(
                        start_response, "404 Not Found", {"error": "not found"}
                    )
                return _json_response(
                    start_response,
                    "200 OK" if res.get("already_shared") else "201 Created",
                    res,
                )

            # ------------------------------------------ Admin Moderation APIs
            if path == "/api/v1/admin/explore/pending" and method == "GET":
                if user_id not in cfg.admin_ids:
                    return _json_response(
                        start_response, "403 Forbidden", {"error": "forbidden"}
                    )

                async def _pending(db: Database) -> dict[str, Any]:
                    rows = await db.db.execute_fetchall(
                        """
                        SELECT question_id, topic, difficulty, question_text, options_json,
                               correct_index, explanation, status, created_at
                        FROM explore_questions
                        WHERE status = 'pending'
                        ORDER BY question_id ASC LIMIT 50
                        """
                    )
                    items: list[dict[str, Any]] = []
                    for row in rows:
                        item = dict(row)
                        raw_options = item.get("options_json")
                        try:
                            options = json.loads(raw_options) if isinstance(raw_options, str) else raw_options
                        except Exception:
                            options = []
                        if not isinstance(options, list):
                            options = []
                        item["options"] = options
                        items.append(item)
                    return {"items": items}

                return _json_response(
                    start_response, "200 OK", asyncio.run(_with_db(cfg, _pending))
                )

            if path == "/api/v1/admin/explore/moderate" and method == "POST":
                if user_id not in cfg.admin_ids:
                    return _json_response(
                        start_response, "403 Forbidden", {"error": "forbidden"}
                    )
                body = _read_json_body(environ)
                qid = body.get("question_id")
                action = str(body.get("action") or "").strip()
                if isinstance(qid, bool) or not isinstance(qid, int) or action not in {"approve", "reject"}:
                    return _json_response(
                        start_response, "400 Bad Request", {"error": "invalid payload"}
                    )
                new_status = "approved" if action == "approve" else "rejected"

                async def _mod(db: Database) -> bool:
                    cur = await db.db.execute(
                        "UPDATE explore_questions SET status = ? WHERE question_id = ?",
                        (new_status, qid),
                    )
                    await db.db.commit()
                    return cur.rowcount > 0

                if not asyncio.run(_with_db(cfg, _mod)):
                    return _json_response(
                        start_response, "404 Not Found", {"error": "not found"}
                    )
                return _json_response(
                    start_response, "200 OK", {"question_id": qid, "status": new_status}
                )

            # ------------------------------------------------ Explore APIs
            if path == "/api/v1/explore/feed" and method == "GET":
                # Cursor-paginated continuous feed. ``limit`` is clamped by the
                # recommender (1..MAX_PAGE_SIZE); ``cursor`` is the opaque token
                # from the previous page. A missing cursor starts a session.
                params = _query_params(environ)
                try:
                    want = int(params.get("limit") or DEFAULT_PAGE_SIZE)
                except (TypeError, ValueError):
                    want = DEFAULT_PAGE_SIZE
                limit = max(1, min(MAX_PAGE_SIZE, want))
                cursor_token = str(params.get("cursor") or "")

                async def _feed(db: Database) -> dict[str, Any]:
                    page = await recommend_explore_page(
                        db, user_id, limit=limit, cursor=cursor_token
                    )
                    return page.as_payload()

                return _json_response(
                    start_response, "200 OK", asyncio.run(_with_db(cfg, _feed))
                )

            if path in {"/api/v1/explore/impressions", "/api/v1/explore/engagement"} and method == "POST":
                body = _read_json_body(environ)
                qid = body.get("question_id")
                raw_dwell = body.get("dwell_ms")
                # "source_opened" is a real interest signal from the scroll
                # feed: the user expanded the academic citation. It is recorded
                # with a read-level signal so it strengthens the topic affinity
                # without ever awarding points. Everything else is unchanged.
                source_opened = bool(body.get("source_opened"))
                if isinstance(raw_dwell, (bool, float)):
                    return _json_response(
                        start_response, "400 Bad Request", {"error": "invalid payload"}
                    )
                try:
                    dwell_ms = int(raw_dwell or 0)
                except (TypeError, ValueError):
                    return _json_response(
                        start_response, "400 Bad Request", {"error": "invalid payload"}
                    )
                if isinstance(qid, bool) or not isinstance(qid, int) or dwell_ms < 0:
                    return _json_response(
                        start_response, "400 Bad Request", {"error": "invalid payload"}
                    )
                signal = 2 if dwell_ms > 8000 else (1 if dwell_ms < 3000 else 0)
                if source_opened and signal < 2:
                    signal = 2

                async def _imp(db: Database) -> bool:
                    exists = await db.db.execute_fetchall(
                        "SELECT 1 FROM explore_questions WHERE question_id = ? AND status = 'approved'",
                        (qid,),
                    )
                    if not exists:
                        return False
                    await db.db.execute(
                        """
                        INSERT INTO explore_impressions(user_id, question_id, shown_at, dwell_ms, signal)
                        VALUES(?, ?, ?, ?, ?)
                        """,
                        (user_id, qid, _utc_now_iso(), dwell_ms, signal),
                    )
                    await db.db.commit()
                    return True

                if not asyncio.run(_with_db(cfg, _imp)):
                    return _json_response(
                        start_response, "404 Not Found", {"error": "not found"}
                    )
                return _json_response(start_response, "200 OK", {"recorded": True})

            if path == "/api/v1/explore/answers" and method == "POST":
                body = _read_json_body(environ)
                qid = body.get("question_id")
                chosen = body.get("chosen_index")
                raw_dwell = body.get("dwell_ms")
                if isinstance(raw_dwell, (bool, float)):
                    return _json_response(
                        start_response, "400 Bad Request", {"error": "invalid payload"}
                    )
                try:
                    dwell_ms = int(raw_dwell or 0)
                except (TypeError, ValueError):
                    return _json_response(
                        start_response, "400 Bad Request", {"error": "invalid payload"}
                    )
                if (
                    isinstance(qid, bool)
                    or not isinstance(qid, int)
                    or isinstance(chosen, bool)
                    or not isinstance(chosen, int)
                    or chosen < 0
                    or chosen > 3
                ):
                    return _json_response(
                        start_response, "400 Bad Request", {"error": "invalid payload"}
                    )

                async def _e_ans(db: Database) -> dict[str, Any] | None:
                    rows = await db.db.execute_fetchall(
                        """
                        SELECT correct_index, explanation, source_type,
                               source_title, source_author, source_institution,
                               source_url, source_year, source_page_or_section,
                               evidence_level
                        FROM explore_questions
                        WHERE question_id = ? AND status = 'approved'
                        """,
                        (qid,),
                    )
                    if not rows:
                        return None
                    correct_idx = int(rows[0]["correct_index"])
                    is_correct = 1 if chosen == correct_idx else 0
                    now = _utc_now_iso()
                    await db.db.execute(
                        """
                        INSERT INTO explore_impressions(user_id, question_id, shown_at, dwell_ms, signal, is_correct)
                        VALUES(?, ?, ?, ?, 3, ?)
                        """,
                        (user_id, qid, now, max(0, dwell_ms), is_correct),
                    )
                    points_awarded = 0
                    if is_correct:
                        ref_id = f"explore_q:{qid}"
                        cur = await db.db.execute(
                            """
                            INSERT OR IGNORE INTO reward_points_ledger(user_id, delta, reason, ref_id, created_at)
                            VALUES(?, 10, 'explore_correct_answer', ?, ?)
                            """,
                            (user_id, ref_id, now),
                        )
                        if cur.rowcount > 0:
                            points_awarded = 10
                    await db.db.commit()
                    # The citation travels with the answer result too, so a
                    # card answered before its feed payload was enriched still
                    # renders its source panel without a second round-trip.
                    source = row_source_payload(dict(rows[0]))
                    return {
                        "is_correct": bool(is_correct),
                        "correct_index": correct_idx,
                        "explanation": rows[0]["explanation"] or "",
                        "points_awarded": points_awarded,
                        "source": source,
                        "verified": source is not None,
                    }

                res = asyncio.run(_with_db(cfg, _e_ans))
                if res is None:
                    return _json_response(
                        start_response, "404 Not Found", {"error": "not found"}
                    )
                return _json_response(start_response, "200 OK", res)

            # ------------------------------------------------ Rewards APIs
            if path == "/api/v1/rewards/balance" and method == "GET":
                async def _r_bal(db: Database) -> dict[str, Any]:
                    pts = await db.study_packs.reward_points_balance(user_id)
                    hist_rows = await db.db.execute_fetchall(
                        """
                        SELECT entry_id, delta, reason, ref_id, created_at
                        FROM reward_points_ledger
                        WHERE user_id = ?
                        ORDER BY entry_id DESC LIMIT 50
                        """,
                        (user_id,),
                    )
                    return {"balance": int(pts), "history": [dict(r) for r in hist_rows]}

                return _json_response(
                    start_response, "200 OK", asyncio.run(_with_db(cfg, _r_bal))
                )

            if path == "/api/v1/rewards/catalog" and method == "GET":
                return _json_response(
                    start_response,
                    "200 OK",
                    {"catalog": [c for c in _REWARD_CATALOG if c.get("enabled", True)]},
                )

            if path == "/api/v1/rewards/redeem" and method == "POST":
                body = _read_json_body(environ)
                code = str(body.get("reward_code") or "").strip()
                client_key = body.get("idempotency_key")
                if client_key is None:
                    client_key = environ.get("HTTP_IDEMPOTENCY_KEY")
                if client_key is not None and not isinstance(client_key, str):
                    return _json_response(
                        start_response,
                        "400 Bad Request",
                        {"error": "idempotency_key must be a string"},
                    )

                async def _redeem(db: Database) -> dict[str, Any]:
                    # The catalog, the rules, the debit and the credit grant all
                    # live in one transaction owned by the repository; the handler
                    # only translates the outcome. Client-supplied balances are
                    # never read, and nothing is reported as granted until the
                    # transaction that wrote the credit lot has committed.
                    result = await db.rewards.redeem(
                        user_id, code, idempotency_key=client_key
                    )
                    result["available_seconds"] = int(
                        await db.available_seconds(int(user_id))
                    )
                    return result

                try:
                    result = asyncio.run(_with_db(cfg, _redeem))
                except Exception:  # noqa: BLE001 - the transaction rolled back
                    # Nothing was committed: no points were spent and no credit
                    # was granted, so this is the only honest answer.
                    return _json_response(
                        start_response,
                        "500 Internal Server Error",
                        {
                            "error": "redemption_failed",
                            "detail": "no points were deducted and no credit was granted",
                        },
                    )
                if not result.get("ok"):
                    error = str(result.get("error") or "")
                    if error in {"unknown_reward", "reward_expired"}:
                        return _json_response(
                            start_response,
                            "400 Bad Request",
                            {"error": "invalid or disabled reward_code"},
                        )
                    if error == "usage_limit":
                        return _json_response(
                            start_response,
                            "400 Bad Request",
                            {"error": "usage limit reached"},
                        )
                    if error == "insufficient_points":
                        return _json_response(
                            start_response,
                            "400 Bad Request",
                            {
                                "error": "insufficient points",
                                "balance": int(result.get("reward_points") or 0),
                            },
                        )
                    logger.warning("reward redeem returned unexpected error=%s user=%s", error, user_id)
                    return _json_response(
                        start_response,
                        "400 Bad Request",
                        {
                            "error": "redemption_not_completed",
                            "detail": error or "unknown_error",
                        },
                    )

                payload = {
                    "balance": int(result["reward_points"]),
                    "credit_seconds_granted": int(result["credit_seconds_granted"]),
                    "credited": bool(result.get("credited")),
                    "reward_code": str(result["reward_code"]),
                    "redemption_id": int(result["redemption_id"]),
                    "ledger_id": result.get("ledger_id"),
                    "available_seconds": int(result.get("available_seconds") or 0),
                    "idempotent": bool(result.get("idempotent")),
                }
                return _json_response(start_response, "200 OK", payload)

            return _json_response(
                start_response, "404 Not Found", {"error": "not found"}, method=method
            )

        # Static Mini App serving at /app and /app/*
        if path == "/app" or path.startswith("/app/"):
            rel = path[len("/app"):].lstrip("/")
            if dist_dir.is_dir():
                candidate = (
                    (dist_dir / rel).resolve()
                    if rel
                    else (dist_dir / "index.html").resolve()
                )
                spa_fallback = False
                if (
                    not candidate.is_relative_to(dist_dir.resolve())
                    or not candidate.is_file()
                ):
                    # A missing *file* request must 404 — a <script> or <img>
                    # request must never receive the HTML shell (and the
                    # Telegram SDK loader needs a real failure to trigger its
                    # fallback). Only extension-less view paths fall back to
                    # the SPA entry.
                    if Path(rel).suffix:
                        return _json_response(
                            start_response,
                            "404 Not Found",
                            {"error": "not found"},
                            method=method,
                        )
                    candidate = (dist_dir / "index.html").resolve()
                    spa_fallback = True
                if candidate.is_file():
                    data = candidate.read_bytes()
                    ctype, _ = mimetypes.guess_type(str(candidate))
                    if not ctype:
                        ctype = {
                            ".js": "text/javascript; charset=utf-8",
                            ".mjs": "text/javascript; charset=utf-8",
                            ".css": "text/css; charset=utf-8",
                            ".html": "text/html; charset=utf-8",
                            ".json": "application/json; charset=utf-8",
                            ".svg": "image/svg+xml",
                            ".png": "image/png",
                            ".jpg": "image/jpeg",
                            ".jpeg": "image/jpeg",
                            ".webp": "image/webp",
                            ".ico": "image/x-icon",
                            ".woff": "font/woff",
                            ".woff2": "font/woff2",
                            ".ttf": "font/ttf",
                            ".map": "application/json; charset=utf-8",
                        }.get(candidate.suffix.lower())
                    # index.html must be revalidated so deploys take effect;
                    # Vite-hashed assets under /assets/ are immutable.
                    if spa_fallback or candidate.name == "index.html":
                        cache_control = "no-store"
                    elif rel.startswith("assets/"):
                        cache_control = "public, max-age=31536000, immutable"
                    else:
                        cache_control = "public, max-age=3600"
                    # Precompressed or on-the-fly gzip for text assets: the
                    # Mini App bundle is ~270KB raw vs ~75KB compressed, and
                    # mobile first-load latency is dominated by transfer.
                    encoding: str | None = None
                    if _accepts_gzip(str(environ.get("HTTP_ACCEPT_ENCODING") or "")):
                        gz_sibling = candidate.with_name(candidate.name + ".gz")
                        if gz_sibling.is_file():
                            data = gz_sibling.read_bytes()
                            encoding = "gzip"
                        elif (
                            candidate.suffix.lower() in _COMPRESSIBLE_SUFFIXES
                            and len(data) >= _GZIP_MIN_BYTES
                        ):
                            stat = candidate.stat()
                            data = _gzip_bytes(
                                data,
                                (str(candidate), stat.st_mtime_ns, stat.st_size),
                            )
                            encoding = "gzip"
                    headers = [
                        ("Content-Type", ctype or "application/octet-stream"),
                        ("Content-Length", str(len(data))),
                        ("Cache-Control", cache_control),
                        ("Vary", "Accept-Encoding"),
                    ]
                    if encoding is not None:
                        headers.append(("Content-Encoding", encoding))
                    start_response("200 OK", headers)
                    return [b"" if method == "HEAD" else data]
            # A deployed Python app without a built frontend should fail clearly,
            # not with a misleading JSON 404 from the API router.
            return _json_response(
                start_response,
                "503 Service Unavailable",
                {
                    "error": "miniapp_not_built",
                    "detail": "miniapp/dist is missing; build the Mini App and upload miniapp/dist.",
                },
                method=method,
            )

        return _json_response(
            start_response, "404 Not Found", {"error": "not found"}, method=method
        )

    return app
