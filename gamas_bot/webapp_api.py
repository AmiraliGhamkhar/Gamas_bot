"""Stdlib WSGI JSON API & static Mini App handler for cPanel Passenger."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
from pathlib import Path
from typing import Any, Callable

from .config import Settings, from_env
from .db import Database
from .study_storage import resolve_private_asset
from .telegram_auth import InitDataError, validate_init_data

_PACK_RE = re.compile(r"^/api/packs/(\d+)$")
_ASSET_DL_RE = re.compile(r"^/api/assets/(\d+)/download$")


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
    """Create a WSGI callable for ``/api/*`` and ``/app/*``."""
    dist_dir = (
        Path(miniapp_dist)
        if miniapp_dist is not None
        else Path(__file__).resolve().parent.parent / "miniapp" / "dist"
    )

    def _get_settings() -> Settings:
        return settings if settings is not None else from_env()

    def _assets_root(cfg: Settings) -> Path:
        if private_assets_dir is not None:
            return Path(private_assets_dir)
        return Path(cfg.temp_dir).parent / "private_assets"

    async def _with_db(cfg: Settings, fn: Callable[[Database], Any]) -> Any:
        db = Database(cfg.db_path)
        await db.connect()
        try:
            return await fn(db)
        finally:
            await db.close()

    def app(environ: dict[str, Any], start_response: Callable) -> list[bytes]:
        path = environ.get("PATH_INFO", "/") or "/"
        method = (environ.get("REQUEST_METHOD") or "GET").upper()

        if path == "/api/health" and method in {"GET", "HEAD"}:
            return _json_response(
                start_response, "200 OK", {"status": "ok"}, method=method
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

            if path == "/api/me" and method == "GET":
                async def _me(db: Database) -> dict[str, Any]:
                    await db.ensure_user(user_id, "")
                    ident = await db.user_identity(user_id) or {}
                    bal = int(ident.get("credit_seconds", 0))
                    pts = await db.study_packs.reward_points_balance(user_id)
                    return {
                        "user_id": user_id,
                        "credit_seconds": int(bal),
                        "reward_points": int(pts),
                        "free_uses": ident.get("free_uses", {"quiz": True}),
                        "banned": bool(ident.get("banned", False)),
                    }

                return _json_response(
                    start_response, "200 OK", asyncio.run(_with_db(cfg, _me))
                )

            if path == "/api/packs" and method == "GET":
                async def _list(db: Database) -> dict[str, Any]:
                    packs = await db.study_packs.study_pack_list(user_id)
                    return {"packs": packs}

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

            m_pack = _PACK_RE.match(path)
            if m_pack:
                pack_id = int(m_pack.group(1))
                if method == "GET":
                    async def _get(db: Database) -> dict[str, Any] | None:
                        pack = await db.study_packs.study_pack_get(user_id, pack_id)
                        if pack is None:
                            return None
                        assets = await db.study_packs.study_pack_assets(
                            user_id, pack_id
                        )
                        return {"pack": pack, "assets": assets}

                    res = asyncio.run(_with_db(cfg, _get))
                    if res is None:
                        return _json_response(
                            start_response, "404 Not Found", {"error": "not found"}
                        )
                    return _json_response(start_response, "200 OK", res)

                if method == "DELETE":
                    async def _del(db: Database) -> bool:
                        return await db.study_packs.study_pack_delete(user_id, pack_id)

                    deleted = asyncio.run(_with_db(cfg, _del))
                    if not deleted:
                        return _json_response(
                            start_response, "404 Not Found", {"error": "not found"}
                        )
                    return _json_response(
                        start_response, "200 OK", {"deleted": True, "pack_id": pack_id}
                    )

            m_dl = _ASSET_DL_RE.match(path)
            if m_dl and method == "GET":
                asset_id = int(m_dl.group(1))

                async def _get_asset(db: Database) -> dict[str, Any] | None:
                    return await db.study_packs.study_asset_get(user_id, asset_id)

                asset = asyncio.run(_with_db(cfg, _get_asset))
                if asset is None:
                    return _json_response(
                        start_response, "404 Not Found", {"error": "not found"}
                    )
                try:
                    file_path = resolve_private_asset(
                        _assets_root(cfg), str(asset["storage_path"])
                    )
                except ValueError:
                    return _json_response(
                        start_response, "404 Not Found", {"error": "not found"}
                    )
                if not file_path.is_file():
                    return _json_response(
                        start_response, "404 Not Found", {"error": "file missing"}
                    )
                data = file_path.read_bytes()
                mime = str(asset.get("mime_type") or "application/octet-stream")
                start_response(
                    "200 OK",
                    [
                        ("Content-Type", mime),
                        ("Content-Length", str(len(data))),
                        (
                            "Content-Disposition",
                            f'attachment; filename="{file_path.name}"',
                        ),
                        ("Cache-Control", "private, no-store"),
                    ],
                )
                return [data]

            return _json_response(
                start_response, "404 Not Found", {"error": "not found"}, method=method
            )

        # Static Mini App serving at /app and /app/*
        if path == "/app" or path.startswith("/app/"):
            rel = path[len("/app"):].lstrip("/")
            if dist_dir.is_dir():
                candidate = (dist_dir / rel).resolve() if rel else (dist_dir / "index.html").resolve()
                if not candidate.is_relative_to(dist_dir.resolve()) or not candidate.is_file():
                    candidate = (dist_dir / "index.html").resolve()
                if candidate.is_file():
                    data = candidate.read_bytes()
                    ctype, _ = mimetypes.guess_type(str(candidate))
                    start_response(
                        "200 OK",
                        [
                            ("Content-Type", ctype or "application/octet-stream"),
                            ("Content-Length", str(len(data))),
                        ],
                    )
                    return [b"" if method == "HEAD" else data]

        return _json_response(
            start_response, "404 Not Found", {"error": "not found"}, method=method
        )

    return app
