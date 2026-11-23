"""Stdlib WSGI JSON API & static Mini App handler for cPanel Passenger."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import Settings, from_env
from .db import Database
from .explore_recommender import recommend_explore_feed
from .study_storage import resolve_private_asset
from .telegram_auth import InitDataError, validate_init_data

_PACK_RE = re.compile(r"^/api/packs/(\d+)$")
_ASSET_DL_RE = re.compile(r"^/api/assets/(\d+)/download$")

_V1_LIB_PACK_RE = re.compile(r"^/api/v1/library/(\d+)$")
_V1_LIB_ASSETS_RE = re.compile(r"^/api/v1/library/(\d+)/assets$")
_V1_LIB_DL_RE = re.compile(r"^/api/v1/library/(\d+)/download/(\d+)$")
_V1_REVIEW_Q_RE = re.compile(r"^/api/v1/review/questions/(\d+)$")

_REWARD_CATALOG = [
    {"code": "theme_ocean", "category": "theme", "title": "تم اقیانوسی مینی‌اپ", "cost_points": 30, "credit_seconds": 0, "enabled": True, "expires_at": None, "max_per_user": 1},
    {"code": "badge_scholar", "category": "badge", "title": "نشان پژوهشگر برتر", "cost_points": 40, "credit_seconds": 0, "enabled": True, "expires_at": None, "max_per_user": 1},
    {"code": "credit_5m", "category": "credit_bonus", "title": "۵ دقیقه اعتبار گاماس", "cost_points": 50, "credit_seconds": 300, "enabled": True, "expires_at": None, "max_per_user": 5},
    {"code": "discount_10", "category": "discount", "title": "کد تخفیف ۱۰٪", "cost_points": 80, "credit_seconds": 0, "enabled": True, "expires_at": None, "max_per_user": 2},
]


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

    def _serve_asset(
        start_response: Callable, cfg: Settings, asset: dict[str, Any] | None
    ) -> list[bytes]:
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
                ("Content-Disposition", f'attachment; filename="{file_path.name}"'),
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
                    bal = int(ident.get("credit_seconds", 0))
                    pts = await db.study_packs.reward_points_balance(user_id)
                    return {
                        "user_id": user_id,
                        "credit_seconds": bal,
                        "reward_points": int(pts),
                        "free_uses": ident.get("free_uses", {"quiz": True}),
                        "banned": bool(ident.get("banned", False)),
                    }

                return _json_response(
                    start_response, "200 OK", asyncio.run(_with_db(cfg, _me))
                )

            if path == "/api/v1/me/subscription" and method == "GET":
                return _json_response(
                    start_response,
                    "200 OK",
                    {"required_channel": cfg.required_channel or None},
                )

            if path == "/api/v1/me/credits" and method == "GET":
                async def _credits(db: Database) -> dict[str, Any]:
                    ident = await db.user_identity(user_id) or {}
                    return {
                        "credit_seconds": int(ident.get("credit_seconds", 0)),
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
                    rows = await db.db.execute_fetchall(
                        """
                        SELECT payment_id, amount_toman, status, created_at
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

                return _serve_asset(
                    start_response, cfg, asyncio.run(_with_db(cfg, _get_v1_asset))
                )

            m_dl = _ASSET_DL_RE.match(path)
            if m_dl and method == "GET":
                asset_id = int(m_dl.group(1))

                async def _get_asset(db: Database) -> dict[str, Any] | None:
                    return await db.study_packs.study_asset_get(user_id, asset_id)

                return _serve_asset(
                    start_response, cfg, asyncio.run(_with_db(cfg, _get_asset))
                )

            # ------------------------------------------------- Review APIs
            if path == "/api/v1/review/groups" and method == "GET":
                async def _groups(db: Database) -> dict[str, Any]:
                    rows = await db.db.execute_fetchall(
                        """
                        SELECT session_id, item_id, question_count, status, created_at
                        FROM quiz_sessions WHERE user_id = ?
                        ORDER BY session_id DESC LIMIT 50
                        """,
                        (user_id,),
                    )
                    return {"groups": [dict(r) for r in rows]}

                return _json_response(
                    start_response, "200 OK", asyncio.run(_with_db(cfg, _groups))
                )

            if path == "/api/v1/review/questions" and method == "GET":
                async def _r_qs(db: Database) -> dict[str, Any]:
                    rows = await db.db.execute_fetchall(
                        """
                        SELECT q.question_id, q.session_id, q.idx, q.question, q.options_json
                        FROM quiz_questions q
                        JOIN quiz_sessions s ON s.session_id = q.session_id
                        WHERE s.user_id = ?
                        ORDER BY q.question_id DESC LIMIT 50
                        """,
                        (user_id,),
                    )
                    items = []
                    for r in rows:
                        d = dict(r)
                        d["options"] = json.loads(d.pop("options_json"))
                        items.append(d)
                    return {"questions": items}

                return _json_response(
                    start_response, "200 OK", asyncio.run(_with_db(cfg, _r_qs))
                )

            m_rq = _V1_REVIEW_Q_RE.match(path)
            if m_rq and method == "GET":
                qid = int(m_rq.group(1))

                async def _r_q(db: Database) -> dict[str, Any] | None:
                    rows = await db.db.execute_fetchall(
                        """
                        SELECT q.question_id, q.session_id, q.idx, q.question, q.options_json
                        FROM quiz_questions q
                        JOIN quiz_sessions s ON s.session_id = q.session_id
                        WHERE q.question_id = ? AND s.user_id = ?
                        """,
                        (qid, user_id),
                    )
                    if not rows:
                        return None
                    d = dict(rows[0])
                    d["options"] = json.loads(d.pop("options_json"))
                    return d

                res = asyncio.run(_with_db(cfg, _r_q))
                if res is None:
                    return _json_response(
                        start_response, "404 Not Found", {"error": "not found"}
                    )
                return _json_response(start_response, "200 OK", {"question": res})

            if path == "/api/v1/review/answers" and method == "POST":
                body = _read_json_body(environ)
                qid = body.get("question_id")
                chosen = body.get("chosen_index")
                if not isinstance(qid, int) or not isinstance(chosen, int) or chosen < 0 or chosen > 3:
                    return _json_response(
                        start_response, "400 Bad Request", {"error": "invalid payload"}
                    )

                async def _r_ans(db: Database) -> dict[str, Any] | None:
                    rows = await db.db.execute_fetchall(
                        """
                        SELECT q.session_id, q.correct_index, q.explanation
                        FROM quiz_questions q
                        JOIN quiz_sessions s ON s.session_id = q.session_id
                        WHERE q.question_id = ? AND s.user_id = ?
                        """,
                        (qid, user_id),
                    )
                    if not rows:
                        return None
                    correct_idx = int(rows[0]["correct_index"])
                    is_correct = 1 if chosen == correct_idx else 0
                    await db.db.execute(
                        """
                        INSERT INTO quiz_answers(session_id, question_id, chosen_index, is_correct, answered_at)
                        VALUES(?, ?, ?, ?, ?)
                        """,
                        (int(rows[0]["session_id"]), qid, chosen, is_correct, _utc_now_iso()),
                    )
                    await db.db.commit()
                    return {
                        "is_correct": bool(is_correct),
                        "correct_index": correct_idx,
                        "explanation": rows[0]["explanation"] or "",
                    }

                res = asyncio.run(_with_db(cfg, _r_ans))
                if res is None:
                    return _json_response(
                        start_response, "404 Not Found", {"error": "not found"}
                    )
                return _json_response(start_response, "200 OK", res)

            if path == "/api/v1/review/share" and method == "POST":
                body = _read_json_body(environ)
                qid = body.get("question_id")
                if not isinstance(qid, int):
                    return _json_response(
                        start_response, "400 Bad Request", {"error": "invalid payload"}
                    )

                async def _share(db: Database) -> dict[str, Any] | None:
                    rows = await db.db.execute_fetchall(
                        """
                        SELECT q.question, q.options_json, q.correct_index, q.explanation
                        FROM quiz_questions q
                        JOIN quiz_sessions s ON s.session_id = q.session_id
                        WHERE q.question_id = ? AND s.user_id = ?
                        """,
                        (qid, user_id),
                    )
                    if not rows:
                        return None
                    r = rows[0]
                    cur = await db.db.execute(
                        """
                        INSERT INTO explore_questions(
                            topic, difficulty, question_text, options_json,
                            correct_index, explanation, status, created_by, created_at
                        ) VALUES ('عمومی', 'easy', ?, ?, ?, ?, 'pending', ?, ?)
                        """,
                        (
                            str(r["question"]),
                            str(r["options_json"]),
                            int(r["correct_index"]),
                            str(r["explanation"] or ""),
                            user_id,
                            _utc_now_iso(),
                        ),
                    )
                    await db.db.commit()
                    return {
                        "shared": True,
                        "explore_question_id": int(cur.lastrowid),
                        "status": "pending",
                    }

                res = asyncio.run(_with_db(cfg, _share))
                if res is None:
                    return _json_response(
                        start_response, "404 Not Found", {"error": "not found"}
                    )
                return _json_response(start_response, "201 Created", res)

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
                    return {"items": [dict(r) for r in rows]}

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
                if not isinstance(qid, int) or action not in {"approve", "reject"}:
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
                async def _feed(db: Database) -> dict[str, Any]:
                    items = await recommend_explore_feed(db, user_id, limit=10)
                    return {"items": items}

                return _json_response(
                    start_response, "200 OK", asyncio.run(_with_db(cfg, _feed))
                )

            if path in {"/api/v1/explore/impressions", "/api/v1/explore/engagement"} and method == "POST":
                body = _read_json_body(environ)
                qid = body.get("question_id")
                dwell_ms = int(body.get("dwell_ms") or 0)
                if not isinstance(qid, int) or dwell_ms < 0:
                    return _json_response(
                        start_response, "400 Bad Request", {"error": "invalid payload"}
                    )
                signal = 2 if dwell_ms > 8000 else (1 if dwell_ms < 3000 else 0)

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
                dwell_ms = int(body.get("dwell_ms") or 0)
                if not isinstance(qid, int) or not isinstance(chosen, int) or chosen < 0 or chosen > 3:
                    return _json_response(
                        start_response, "400 Bad Request", {"error": "invalid payload"}
                    )

                async def _e_ans(db: Database) -> dict[str, Any] | None:
                    rows = await db.db.execute_fetchall(
                        """
                        SELECT correct_index, explanation
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
                    return {
                        "is_correct": bool(is_correct),
                        "correct_index": correct_idx,
                        "explanation": rows[0]["explanation"] or "",
                        "points_awarded": points_awarded,
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
                item = next((c for c in _REWARD_CATALOG if c["code"] == code), None)
                if item is None or not item.get("enabled", True):
                    return _json_response(
                        start_response, "400 Bad Request", {"error": "invalid or disabled reward_code"}
                    )
                if item.get("expires_at") and str(item["expires_at"]) < _utc_now_iso():
                    return _json_response(
                        start_response, "400 Bad Request", {"error": "reward expired"}
                    )

                async def _redeem(db: Database) -> tuple[str, dict[str, Any]]:
                    await db.db.execute("BEGIN IMMEDIATE")
                    try:
                        max_u = item.get("max_per_user")
                        if max_u is not None:
                            cnt_rows = await db.db.execute_fetchall(
                                "SELECT COUNT(*) AS cnt FROM reward_points_ledger WHERE user_id = ? AND reason = ?",
                                (user_id, f"redeem:{code}"),
                            )
                            if int(cnt_rows[0]["cnt"]) >= int(max_u):
                                await db.db.execute("ROLLBACK")
                                return "400 Bad Request", {"error": "usage limit reached"}
                        bal = await db.study_packs.reward_points_balance(user_id)
                        if bal < int(item["cost_points"]):
                            await db.db.execute("ROLLBACK")
                            return "400 Bad Request", {"error": "insufficient points"}
                        await db.db.execute(
                            """
                            INSERT INTO reward_points_ledger(user_id, delta, reason, ref_id, created_at)
                            VALUES(?, ?, ?, NULL, ?)
                            """,
                            (user_id, -int(item["cost_points"]), f"redeem:{code}", _utc_now_iso()),
                        )
                        await db.db.commit()
                        new_bal = await db.study_packs.reward_points_balance(user_id)
                        return "200 OK", {
                            "balance": int(new_bal),
                            "credit_seconds_granted": int(item["credit_seconds"]),
                            "reward_code": code,
                        }
                    except Exception:
                        await db.db.execute("ROLLBACK")
                        raise

                status, payload = asyncio.run(_with_db(cfg, _redeem))
                return _json_response(start_response, status, payload)

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
                if (
                    not candidate.is_relative_to(dist_dir.resolve())
                    or not candidate.is_file()
                ):
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
