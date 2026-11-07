"""Administrator dashboards, hubs, audit views, slash commands, and broadcasts (SRP).

Encapsulates reporting windows, Users/Payments/System hubs, receipt inspection,
admin cooldown enforcement, ``/grant`` / ``/ban`` / ``/unlimited``, and
flood-wait-aware broadcasts.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta
import logging
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

from telethon import Button

from . import texts
from .billing import format_duration, format_toman, format_validity
from .bot_ui import TELEGRAM_CAPTION_LIMIT
from .db import utc_now
from .docx_export import jalali_date
from .fileio import run_file_io
from .intake import (
    FAILURE_KIND_LABELS_FA,
    _command_name,
    _is_safe_receipt_path,
    _receipt_image_signature,
    _parse_telegram_user_id,
    _to_int,
    is_pending_payment_review,
)
from .metrics import METRICS
from .progress import to_persian_digits
from .telegram_text import button_label, split_message

if TYPE_CHECKING:
    from .bot_deps import BotDependencies

logger = logging.getLogger("gamas_bot.bot.admin")


class AdminWorkflowMixin:
    """Operator dashboards, user/payment hubs, commands, and broadcast fan-out."""

    db: Any
    settings: Any
    client: Any
    queue: Any
    deps: BotDependencies
    _started_at: float
    _pending_user_search: set[int]
    _pending_free_reset: dict[int, int]
    _admin_action_times: dict[tuple[int, str], float]
    _broadcast_running: set[int]

    ADMIN_PERIODS: tuple[tuple[str, str], ...] = (
        ("today", "امروز"),
        ("7d", "۷ روز"),
        ("30d", "۳۰ روز"),
        ("all", "کل"),
    )
    ADMIN_PERIOD_DAYS: dict[str, int] = {"7d": 7, "30d": 30}
    ADMIN_SERIES_LIMIT = 10
    ADMIN_P95_MIN_SAMPLES = 20
    ADMIN_PERIOD_KEYS = frozenset(key for key, _label in ADMIN_PERIODS)

    ADMIN_STATUS_LABELS_FA: dict[str, str] = {
        "pending": "در انتظار",
        "awaiting_payment": "در انتظار پرداخت",
        "pending_review": "در انتظار بررسی",
        "processing": "در حال پردازش",
        "done": "موفق",
        "partial": "ناقص (متن تحویل شد)",
        "failed": "ناموفق",
        "rejected": "ردشده",
        "approved": "تأییدشده",
    }
    ADMIN_KIND_LABELS_FA: dict[str, str] = {
        "audio": "صدا",
        "video": "ویدیو",
        "presentation": "ارائه / پاورپوینت",
        "office": "سند رایگان",
        "text": "متن",
    }
    ADMIN_ENGINE_LABELS_FA: dict[str, str] = {
        "speechmatics": "Speechmatics",
        "deepgram": "Deepgram",
        "openai_compatible": "OpenAI-compatible",
        "slides": "فقط اسلاید (بدون صدا)",
        "stt": "STT (بدون برچسب موتور)",
        "unrecorded": "ثبت‌نشده",
    }
    ADMIN_STAGE_LABELS_FA: dict[str, str] = {
        "downloading": "دریافت فایل",
        "extracting": "استخراج و آماده‌سازی صدا",
        "transcribing": "تبدیل گفتار",
        "generating_notes": "ساخت جزوه",
        "uploading": "ساخت و ارسال فایل",
    }

    ADMIN_HUB_TITLES = {
        "pay": "💳 پرداخت‌ها",
        "usr": "👥 کاربران",
        "sys": "🩺 سامانه",
    }

    PAYMENT_ADMIN_PAGE_SIZE = 8
    PAYMENT_HISTORY_FILTERS: tuple[tuple[str, str], ...] = (
        ("all", "همه"),
        ("pending_review", "در انتظار"),
        ("approved", "تأییدشده"),
        ("rejected", "ردشده"),
        ("cancelled", texts.PAYMENT_HISTORY_CANCELLED),
    )

    ADMIN_COMMAND_COOLDOWN_SECONDS = 30.0
    BROADCAST_COOLDOWN_SECONDS = 60.0
    MAX_BROADCAST_CHARS = 4096
    BROADCAST_THROTTLE_SECONDS = 0.12
    BROADCAST_BATCH_PROGRESS_EVERY = 25
    BROADCAST_MAX_FLOOD_WAIT_RETRIES = 3

    def _admin_period_start(self, period: str) -> str | None:
        now = utc_now()
        if period == "today":
            return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(
                timespec="seconds"
            )
        days = self.ADMIN_PERIOD_DAYS.get(period)
        if days is None:
            return None
        return (now - timedelta(days=days)).isoformat(timespec="seconds")

    def _admin_period_label(self, period: str, report: dict) -> str:
        title = dict(self.ADMIN_PERIODS).get(period, "کل")
        start = report.get("period", {}).get("start")
        if not start:
            return f"{title} (از آغاز، UTC)"
        try:
            return f"{title} — از {jalali_date(datetime.fromisoformat(start))} (UTC)"
        except ValueError:
            return f"{title} (UTC)"

    def _admin_period_row(self, prefix: str, period: str) -> list:
        row = []
        for key, label in self.ADMIN_PERIODS:
            marker = "• " if key == period else ""
            row.append(Button.inline(f"{marker}{label}", f"{prefix}:{key}".encode()))
        return row

    @staticmethod
    def _rate_text(rate: float | None) -> str:
        if rate is None:
            return "—"
        return to_persian_digits(f"{round(rate * 100)}٪")

    @staticmethod
    def _duration_text(seconds: float | None) -> str:
        if seconds is None:
            return "—"
        return format_duration(int(round(seconds)))

    def _timing_line(self, label: str, summary: dict) -> str:
        count = int(summary.get("count") or 0)
        if count <= 0:
            return f"{label}: داده‌ای ثبت نشده"
        parts = [f"میانگین {self._duration_text(summary.get('avg_seconds'))}"]
        if summary.get("median_seconds") is not None:
            parts.append(f"میانه {self._duration_text(summary['median_seconds'])}")
        if (
            count >= self.ADMIN_P95_MIN_SAMPLES
            and summary.get("p95_seconds") is not None
        ):
            parts.append(f"p95 {self._duration_text(summary['p95_seconds'])}")
        return f"{label}: " + " | ".join(parts) + f" (n={to_persian_digits(count)})"

    @staticmethod
    def _day_label(day: str) -> str:
        try:
            return jalali_date(datetime.fromisoformat(day))
        except ValueError:
            return day

    @staticmethod
    def _series_bar(value: int, maximum: int, width: int = 5) -> str:
        if value <= 0 or maximum <= 0:
            return "·"
        filled = max(1, round(width * min(value, maximum) / maximum))
        return "▇" * filled

    @staticmethod
    def _bytes_text(value: int) -> str:
        size = float(max(0, int(value)))
        if size == 0:
            return f"{to_persian_digits(int(size))} بایت"
        for unit in ("KB", "MB", "GB"):
            size /= 1024.0
            if size < 1024 or unit == "GB":
                return f"{to_persian_digits(f'{size:.1f}')} {unit}"
        return f"{to_persian_digits(int(value))} بایت"

    @staticmethod
    def _date_text(value: str | None) -> str:
        if not value:
            return "—"
        try:
            return jalali_date(datetime.fromisoformat(value))
        except ValueError:
            return str(value)

    @staticmethod
    def _timestamp_text(value: str | None) -> str:
        if not value:
            return "ثبت نشده"
        try:
            at = datetime.fromisoformat(value)
        except ValueError:
            return value
        return f"{jalali_date(at)} — {to_persian_digits(at.strftime('%H:%M'))} UTC"

    def _admin_error_label(self, kind: str) -> str:
        return FAILURE_KIND_LABELS_FA.get(kind, kind)

    def _admin_attention_lines(self, report: dict, runtime: dict) -> list[str]:
        payments = report["payments"]
        processing = report["processing"]
        items: list[str] = []
        if payments["pending_review"]:
            items.append(
                f"🧾 {to_persian_digits(payments['pending_review'])} سفارش در انتظار بررسی رسید"
            )
        if processing["undelivered"]:
            top = processing["errors"][0] if processing["errors"] else None
            suffix = (
                f" — بیشترین: {self._admin_error_label(top['kind'])}" if top else ""
            )
            items.append(
                f"❌ {to_persian_digits(processing['undelivered'])} درخواست ناموفق در این بازه{suffix}"
            )
        expiring = payments["expiring"]
        if expiring["count"]:
            items.append(
                f"⏳ {to_persian_digits(expiring['count'])} بستهٔ اعتبار تا "
                f"{to_persian_digits(expiring['within_days'])} روز آینده منقضی می‌شود "
                f"({format_duration(expiring['seconds'])})"
            )
        db_errors = int(runtime.get("db_errors", 0) or 0)
        if db_errors:
            items.append(
                f"🗄 {to_persian_digits(db_errors)} خطای پایگاه‌داده از شروع اجرای این نسخه"
            )
        transient = 0
        providers = runtime.get("providers", {}) or {}
        for layer in ("stt", "llm"):
            for stats in (providers.get(layer, {}) or {}).values():
                transient += sum(
                    int(stats.get(key, 0) or 0)
                    for key in ("timeouts", "rate_limits", "server_errors")
                )
        if transient:
            items.append(
                f"🌐 {to_persian_digits(transient)} خطای موقت ارائه‌دهنده "
                "(پایان مهلت / محدودیت نرخ / خطای سرور) از شروع اجرا"
            )
        if not items:
            items.append("✅ مورد فوری‌ای در انتظار نیست.")
        return items

    async def _show_admin_overview(self, event: Any, period: str = "7d") -> None:
        period = period if period in self.ADMIN_PERIOD_KEYS else "7d"
        report = await self.db.admin_report(
            period_start=self._admin_period_start(period)
        )
        processing = report["processing"]
        users = report["users"]
        payments = report["payments"]
        approved = payments["by_status"].get("approved", {"count": 0, "toman": 0})
        rejected = payments["by_status"].get("rejected", {"count": 0, "toman": 0})
        total_jobs = sum(processing["by_status"].values())
        active_now = self.queue.active_count
        queued_now = self.queue.pending
        lines = [
            "🛠 پنل مدیریت — نمای کلی",
            f"🗓 بازه: {self._admin_period_label(period, report)}",
            "",
            "👥 کاربران",
            f"• کل ثبت‌شده: {to_persian_digits(users['total'])}"
            f" | جدید در بازه: {to_persian_digits(users['new'])}"
            f" | فعال در بازه: {to_persian_digits(users['active'])}",
            "",
            "⚙️ پردازش",
            f"• درخواست در بازه: {to_persian_digits(total_jobs)}"
            f" | تحویل‌شده: {to_persian_digits(processing['delivered'])}"
            f" | ناموفق: {to_persian_digits(processing['undelivered'])}"
            f" | نرخ موفقیت: {self._rate_text(processing['success_rate'])}",
            "• " + self._timing_line("زمان پاسخ", processing["end_to_end"]),
            f"• اکنون: {to_persian_digits(active_now)} فعال"
            f" | {to_persian_digits(queued_now)} در صف",
            "",
            "💳 پرداخت و اعتبار",
            f"• در انتظار بررسی: {to_persian_digits(payments['pending_review'])}"
            f" | تأیید در بازه: {to_persian_digits(approved['count'])}"
            f" | رد در بازه: {to_persian_digits(rejected['count'])}",
            f"• مبلغ تأییدشده در بازه: {format_toman(approved['toman'])}",
            "",
            "🚨 نیازمند توجه",
        ]
        lines.extend(
            f"• {item}"
            for item in self._admin_attention_lines(report, self.runtime_metrics())  # type: ignore[attr-defined]
        )
        buttons = [self._admin_period_row("adm:ov", period)]
        buttons.extend(self._admin_menu())  # type: ignore[attr-defined]
        await self._edit_or_reply(event, "\n".join(lines), buttons)  # type: ignore[attr-defined]

    def _admin_status_label(self, status: str) -> str:
        return self.ADMIN_STATUS_LABELS_FA.get(status, status)

    def _admin_kind_label(self, kind: str) -> str:
        return self.ADMIN_KIND_LABELS_FA.get(kind, kind)

    def _admin_engine_label(self, engine: str) -> str:
        return self.ADMIN_ENGINE_LABELS_FA.get(engine, engine)

    def _admin_screen_header(self, title: str, period: str, report: dict) -> list[str]:
        return [title, f"🗓 بازه: {self._admin_period_label(period, report)}", ""]

    def _admin_series_lines(
        self, series: list[dict], *, limit: int | None = None
    ) -> list[str]:
        rows = series[-int(limit or self.ADMIN_SERIES_LIMIT) :]
        if not rows:
            return ["📈 در این بازه فعالیتی ثبت نشده است."]
        maximum = max(
            [max(int(row["jobs"]), int(row["new_users"])) for row in rows] + [1]
        )
        lines = ["📈 روند روزانه (UTC):"]
        for row in rows:
            lines.append(
                f"{self._day_label(row['day'])} {self._series_bar(int(row['jobs']), maximum)} "
                f"درخواست {to_persian_digits(int(row['jobs']))} "
                f"(✅ {to_persian_digits(int(row['delivered']))} / "
                f"❌ {to_persian_digits(int(row['undelivered']))}) "
                f"| کاربر جدید {to_persian_digits(int(row['new_users']))}"
            )
        return lines

    async def _show_admin_users_report(self, event: Any, period: str = "7d") -> None:
        period = period if period in self.ADMIN_PERIOD_KEYS else "7d"
        report = await self.db.admin_report(
            period_start=self._admin_period_start(period), with_timings=False
        )
        users = report["users"]
        lines = self._admin_screen_header("👥 کاربران — جزئیات", period, report)
        lines.extend(
            [
                f"• کل ثبت‌شده: {to_persian_digits(users['total'])}",
                f"• جدید در بازه: {to_persian_digits(users['new'])}",
                f"• فعال در بازه (دست‌کم یک درخواست): {to_persian_digits(users['active'])}",
                f"• تحویل‌گرفته در بازه (دست‌کم یک خروجی): {to_persian_digits(users['delivered'])}",
                f"• مسدود: {to_persian_digits(users['banned'])}"
                f" | ویژه (بدون محدودیت): {to_persian_digits(users['unlimited'])}",
                "",
            ]
        )
        lines.extend(self._admin_series_lines(report["processing"]["series"]))
        lines.extend(
            [
                "",
                "ℹ️ «فعال» = کاربری که در بازه دست‌کم یک درخواست ثبت کرده است.",
                "ℹ️ «تحویل‌گرفته» = کاربری که در بازه دست‌کم یک خروجی موفق داشته است.",
                "ℹ️ درآمد و مصرف هر کاربر در بخش «پرداخت و اعتبار» است.",
            ]
        )
        buttons = [
            self._admin_period_row("adm:ovu", period),
            [Button.inline("👥 کاربران", b"adm:hub:usr")],
            [Button.inline("↩️ نمای کلی", b"adm:home")],
        ]
        buttons.extend(self._admin_menu())  # type: ignore[attr-defined]
        await self._edit_or_reply(event, "\n".join(lines), buttons)  # type: ignore[attr-defined]

    async def _show_admin_processing_report(
        self, event: Any, period: str = "7d"
    ) -> None:
        period = period if period in self.ADMIN_PERIOD_KEYS else "7d"
        period_start = self._admin_period_start(period)
        report = await self.db.admin_report(period_start=period_start)
        engines = await self.db.admin_stt_engines(period_start=period_start)
        processing = report["processing"]
        runtime = self.runtime_metrics()  # type: ignore[attr-defined]
        lines = self._admin_screen_header("⚙️ پردازش — جزئیات", period, report)

        total_jobs = sum(processing["by_status"].values())
        lines.extend(
            [
                "📦 حجم",
                f"• کل در بازه: {to_persian_digits(total_jobs)}"
                f" | تحویل‌شده: {to_persian_digits(processing['delivered'])}"
                f" | ناموفق/ردشده: {to_persian_digits(processing['undelivered'])}"
                f" | نرخ موفقیت: {self._rate_text(processing['success_rate'])}",
            ]
        )
        if processing["by_status"]:
            statuses = " | ".join(
                f"{self._admin_status_label(status)}: {to_persian_digits(count)}"
                for status, count in sorted(processing["by_status"].items())
            )
            lines.append(f"• وضعیت‌ها: {statuses}")
        if processing["by_kind"]:
            kinds = " | ".join(
                f"{self._admin_kind_label(kind)}: {to_persian_digits(count)}"
                for kind, count in sorted(processing["by_kind"].items())
            )
            lines.append(f"• نوع: {kinds}")

        lines.extend(
            [
                "",
                "⏱ زمان (فقط کارهای پایان‌یافته)",
                "• " + self._timing_line("پایان به پایان", processing["end_to_end"]),
                "• " + self._timing_line("پردازش خالص", processing["processing_time"]),
                "• " + self._timing_line("انتظار در صف", processing["queue_wait"]),
            ]
        )
        if 0 < processing["end_to_end"]["count"] < self.ADMIN_P95_MIN_SAMPLES:
            lines.append(
                f"ℹ️ برای p95 دست‌کم {to_persian_digits(self.ADMIN_P95_MIN_SAMPLES)} "
                "نمونه لازم است؛ تا آن حد فقط میانه نمایش داده می‌شود."
            )

        stage_lines = []
        stages = runtime.get("stages", {}) or {}
        for stage_key, label in self.ADMIN_STAGE_LABELS_FA.items():
            summary = stages.get(stage_key, {}) or {}
            count = int(summary.get("count", 0) or 0)
            if count <= 0:
                continue
            stage_lines.append(
                f"• {label}: میانه {self._duration_text(summary.get('p50_seconds'))}"
                f" | p95 {self._duration_text(summary.get('p95_seconds'))}"
                f" (n={to_persian_digits(count)})"
            )
        if stage_lines:
            lines.extend(
                ["", "🧩 زمان مرحله‌ها (از شروع اجرای این نسخه):", *stage_lines]
            )

        if processing["errors"]:
            lines.extend(["", "🚨 خطاهای بازه (بر اساس دستهٔ ثبت‌شده):"])
            for item in processing["errors"]:
                lines.append(
                    f"• {self._admin_error_label(str(item['kind']))}: "
                    f"{to_persian_digits(int(item['count']))}"
                )
        else:
            lines.extend(["", "🚨 در این بازه خطای ثبت‌شده‌ای وجود ندارد."])

        if engines:
            usage = " | ".join(
                f"{self._admin_engine_label(str(item['engine']))}: "
                f"{to_persian_digits(int(item['count']))}"
                for item in engines
                if item["count"]
            )
            if usage:
                lines.extend(["", f"🎙 موتور تبدیل گفتار در بازه: {usage}"])

        failovers = {"stt": 0, "llm": 0}
        providers = runtime.get("providers", {}) or {}
        for layer in failovers:
            for stats in (providers.get(layer, {}) or {}).values():
                failovers[layer] += int(stats.get("failovers_in", 0) or 0)
        lines.append(
            "🔁 جایگزینی خودکار (از شروع اجرا): "
            f"STT {to_persian_digits(failovers['stt'])}"
            f" | LLM {to_persian_digits(failovers['llm'])}"
        )
        lines.append("")
        lines.extend(self._admin_series_lines(processing["series"]))
        buttons = [
            self._admin_period_row("adm:ovp", period),
            [Button.inline("🩺 وضعیت اکنون", b"adm:hub:sys")],
            [Button.inline("↩️ نمای کلی", b"adm:home")],
        ]
        buttons.extend(self._admin_menu())  # type: ignore[attr-defined]
        await self._edit_or_reply(event, "\n".join(lines), buttons)  # type: ignore[attr-defined]

    async def _show_admin_payments_report(self, event: Any, period: str = "7d") -> None:
        period = period if period in self.ADMIN_PERIOD_KEYS else "7d"
        report = await self.db.admin_report(
            period_start=self._admin_period_start(period), with_timings=False
        )
        payments = report["payments"]
        by_status = payments["by_status"]
        approved = by_status.get("approved", {"count": 0, "toman": 0})
        rejected = by_status.get("rejected", {"count": 0, "toman": 0})
        created = sum(int(entry["count"]) for entry in by_status.values())
        decided = int(approved["count"]) + int(rejected["count"])
        approval_rate = (int(approved["count"]) / decided) if decided else None
        lines = self._admin_screen_header(
            "💳 پرداخت و اعتبار — جزئیات", period, report
        )
        lines.extend(
            [
                f"• در انتظار بررسی رسید (اکنون): {to_persian_digits(payments['pending_review'])}",
                f"• سفارش‌های ایجادشده در بازه: {to_persian_digits(created)}",
                f"• تأییدشده در بازه: {to_persian_digits(approved['count'])}"
                f" — {format_toman(int(approved['toman']))}",
                f"• ردشده در بازه: {to_persian_digits(rejected['count'])}",
                f"• نرخ تأیید در بازه: {self._rate_text(approval_rate)}",
            ]
        )
        expiring = payments["expiring"]
        lines.append(
            f"• اعتبار در حال انقضا (تا {to_persian_digits(expiring['within_days'])} روز آینده): "
            f"{to_persian_digits(expiring['count'])} بسته"
            f" — {format_duration(expiring['seconds'])}"
        )
        open_statuses = {
            status: entry
            for status, entry in by_status.items()
            if status not in ("approved", "rejected")
        }
        if open_statuses:
            lines.append(
                "• سفارش‌های باز در بازه: "
                + " | ".join(
                    f"{self._admin_status_label(status)}: {to_persian_digits(entry['count'])}"
                    for status, entry in sorted(open_statuses.items())
                )
            )
        lines.extend(
            [
                "",
                "ℹ️ مبلغ فقط از سفارش‌های تأییدشده محاسبه می‌شود؛ رسیدهای در انتظار",
                "   در هیچ عددی به‌عنوان درآمد شمرده نمی‌شوند.",
                "ℹ️ «اعتبار در حال انقضا» بسته‌های اعتباری دارای تاریخ انقضاست.",
                "   بستهٔ بدون انقضا هرگز در این شمارش نیست.",
            ]
        )
        buttons = [
            self._admin_period_row("adm:ovpay", period),
            [
                Button.inline("💳 صف بررسی", b"adm:hub:pay"),
                Button.inline("📋 تاریخچه", b"adm:hub:pay:hist"),
            ],
            [Button.inline("↩️ نمای کلی", b"adm:home")],
        ]
        buttons.extend(self._admin_menu())  # type: ignore[attr-defined]
        await self._edit_or_reply(event, "\n".join(lines), buttons)  # type: ignore[attr-defined]

    async def _show_admin_system_report(self, event: Any, period: str = "7d") -> None:
        period = period if period in self.ADMIN_PERIOD_KEYS else "7d"
        runtime = self.runtime_metrics()  # type: ignore[attr-defined]
        health = await self.db.admin_health()
        uptime = max(0.0, self.deps.now_wall() - self._started_at)
        lines = [
            "🩺 سلامت سیستم",
            "🗓 وضعیت اکنون (آمار پردازش در نمای کلی و بخش پردازش است)",
            "",
            f"• زمان اجرای این نسخه: {format_duration(int(uptime))}",
            f"• کارها: {to_persian_digits(self.queue.active_count)} فعال"
            f" (اوج {to_persian_digits(int(runtime.get('peak_active_jobs', 0) or 0))})"
            f" | {to_persian_digits(self.queue.pending)} در صف"
            f" (اوج {to_persian_digits(int(runtime.get('peak_queued_jobs', 0) or 0))})",
            f"• حافظهٔ اوج: {self._bytes_text(int(int(runtime.get('peak_rss_mb', 0) or 0) * 1024 * 1024))}",
            f"• پایگاه‌داده: {self._bytes_text(int(health['db_bytes']))}"
            f" | WAL: {self._bytes_text(int(health['wal_bytes']))}",
            f"• خطاهای پایگاه‌داده از شروع اجرا: "
            f"{to_persian_digits(int(runtime.get('db_errors', 0) or 0))}",
            f"• آخرین پردازش موفق: {self._timestamp_text(health['last_success'])}",
            f"• به‌روزرسانی‌های تکراری حذف‌شده: "
            f"{to_persian_digits(int(runtime.get('deduplicated_updates', 0) or 0))}",
        ]
        providers = runtime.get("providers", {}) or {}
        provider_lines: list[str] = []
        for layer, layer_label in (("stt", "STT"), ("llm", "LLM")):
            entries = providers.get(layer, {}) or {}
            for slug, stats in sorted(entries.items()):
                provider_lines.append(
                    f"• {layer_label} {slug}: در حال اجرا "
                    f"{to_persian_digits(int(stats.get('in_flight', 0) or 0))}/"
                    f"{to_persian_digits(int(stats.get('peak_in_flight', 0) or 0))} اوج"
                    f" | موفقیت {self._rate_text(stats.get('success_rate'))}"
                    f" | پایان مهلت {to_persian_digits(int(stats.get('timeouts', 0) or 0))}"
                    f" | محدودیت نرخ {to_persian_digits(int(stats.get('rate_limits', 0) or 0))}"
                    f" | خطای سرور {to_persian_digits(int(stats.get('server_errors', 0) or 0))}"
                )
        if provider_lines:
            lines.extend(["", "🌐 ارائه‌دهنده‌ها (از شروع اجرا):", *provider_lines])
        else:
            lines.extend(
                ["", "🌐 از شروع اجرا هنوز درخواستی به ارائه‌دهنده‌ها نرفته است."]
            )
        if self.settings.subscription_required and self.settings.subscription_channel:
            lines.extend(
                [
                    "",
                    f"🔒 کانال الزامی: {self.settings.subscription_channel}",
                ]
            )
        else:
            lines.extend(["", "🔒 کانال الزامی: غیرفعال"])
        lines.extend(
            [
                "",
                "🧹 نگهداری",
                f"• رسید پرداخت: {to_persian_digits(self.settings.receipt_retention_days)} روز",
                "• رونوشت گفتار: "
                + (
                    "غیرفعال"
                    if self.settings.transcript_retention_days <= 0
                    else f"{to_persian_digits(self.settings.transcript_retention_days)} روز"
                ),
                "• فایل‌های موقت هر کار پس از پایان همان کار پاک می‌شوند.",
            ]
        )
        buttons = [
            self._admin_period_row("adm:ovs", period),
            [
                Button.inline(
                    "⚙️ جزئیات پردازش",
                    f"adm:hub:sys:proc:{period}".encode("ascii"),
                )
            ],
            [Button.inline("↩️ نمای کلی", b"adm:home")],
        ]
        buttons.extend(self._admin_menu())  # type: ignore[attr-defined]
        await self._edit_or_reply(event, "\n".join(lines), buttons)  # type: ignore[attr-defined]

    @staticmethod
    def _identity_line(
        *,
        username: str | None,
        telegram_id: int | str | None,
        display_name: str = "",
    ) -> str:
        handle = f"@{str(username).lstrip('@')}" if username else "—"
        identifier = "—" if telegram_id in (None, "") else str(telegram_id)
        name = " ".join(str(display_name or "").split()) or "—"
        return f"{handle} · شناسه: {identifier} · {name}"

    def _user_display_name(self, first_name: str = "", last_name: str = "") -> str:
        return " ".join(
            part for part in (str(first_name).strip(), str(last_name).strip()) if part
        )

    async def _identity_for_id(self, user_id: int) -> str:
        identity = await self.db.user_identity(int(user_id)) or {}
        return self._identity_line(
            username=identity.get("username"),
            telegram_id=user_id,
            display_name=self._user_display_name(
                identity.get("first_name", ""), identity.get("last_name", "")
            ),
        )

    def _hub_back_row(self, hub: str) -> list:
        return [
            Button.inline(
                f"↩️ {self.ADMIN_HUB_TITLES.get(hub, 'بازگشت')}",
                f"adm:hub:{hub}".encode(),
            )
        ]

    async def _show_admin_users(self, event: Any) -> None:
        users = await self.db.list_users(limit=12)
        total = await self.db.count_users()
        heading = (
            f"👥 فهرست کاربران — {to_persian_digits(len(users))} از "
            f"{to_persian_digits(total)} نفر"
        )
        if total > len(users):
            heading += " (۱۲ کاربر اخیر)"
        lines = [
            heading,
            "",
            "وضعیت حساب: 🚫 مسدود | ⭐ ویژه (اعتبار نامحدود) | 👤 عادی",
            "",
        ]
        for user in users:
            if user["banned"]:
                status = "🚫 مسدود"
            elif user["is_unlimited"]:
                status = "⭐ ویژه"
            else:
                status = "👤 عادی"
            balance = (
                "نامحدود"
                if user["is_unlimited"]
                else format_duration(user["available_seconds"])
            )
            joined = (user["joined_at"] or "")[:10] or "—"
            identity = self._identity_line(
                username=user.get("username"),
                telegram_id=user["telegram_id"],
                display_name=self._user_display_name(
                    user.get("first_name", ""), user.get("last_name", "")
                ),
            )
            lines.append(
                f"{status} | {identity} | اعتبار: {balance} | عضویت: {joined}"
            )
        lines.extend(
            [
                "",
                "🔎 جست‌وجوی کاربر: شناسهٔ عددی یا @نام‌کاربری را در پیام بعدی بفرستید.",
                "",
                "دستورهای مدیریت (در همین گفت‌وگو ارسال کنید):",
                "• افزودن اعتبار برحسب ساعت: /grant <شناسه کاربر> <ساعت>",
                "  مثال: /grant 123456789 2",
                "• فعال‌سازی وضعیت ویژه (بدون محدودیت اعتبار؛ دلیل الزامی است):",
                "  /unlimited <شناسه کاربر> on <دلیل>",
                "• لغو وضعیت ویژه: /unlimited <شناسه کاربر> off",
                "• مسدودکردن کاربر: /ban <شناسه کاربر>",
                "• رفع مسدودی: /unban <شناسه کاربر>",
                "شناسهٔ هر کاربر در فهرست بالا نمایش داده شده است.",
            ]
        )
        self._pending_user_search.add(int(event.sender_id or 0))
        buttons: list[list] = []
        for user in users[:8]:
            label = self._identity_line(
                username=user.get("username"),
                telegram_id=user["telegram_id"],
                display_name=self._user_display_name(
                    user.get("first_name", ""), user.get("last_name", "")
                ),
            )
            buttons.append(
                [
                    Button.inline(
                        button_label(f"🔎 {label}"),
                        f"adm:hub:usr:card:{user['telegram_id']}".encode("ascii"),
                    )
                ]
            )
        buttons.append(
            [
                Button.inline("📊 آمار", b"adm:hub:usr:stats"),
                Button.inline("🧾 رسیدهای در انتظار", b"adm:hub:usr:pending"),
            ]
        )
        buttons.append([Button.inline("🚫 مسدودشده‌ها", b"adm:hub:usr:banned")])
        buttons.extend(self._admin_menu())  # type: ignore[attr-defined]
        await self._edit_or_reply(event, "\n".join(lines), buttons)  # type: ignore[attr-defined]

    async def _show_admin_user_card(self, event: Any, target_id: int) -> None:
        identity = await self.db.user_identity(int(target_id))
        if identity is None:
            await self._edit(  # type: ignore[attr-defined]
                event,
                "کاربری با این شناسه ثبت نشده است.",
                self._hub_back_row("usr") + self._admin_menu(),  # type: ignore[attr-defined]
            )
            return
        if identity["is_unlimited"]:
            balance = "نامحدود ⭐"
        else:
            seconds = await self.db.available_seconds(int(target_id))
            balance = (
                f"{format_duration(seconds)} ({to_persian_digits(seconds)} ثانیه)"
            )
        orders = await self.db.count_user_payments(int(target_id))
        decks = await self.db.deck_count(int(target_id))
        free = identity.get("free_uses", {"deck": True, "quiz": True})
        status = (
            "🚫 مسدود"
            if identity["banned"]
            else (
                "⭐ ویژه (اعتبار نامحدود)" if identity["is_unlimited"] else "👤 عادی"
            )
        )
        name = self._user_display_name(identity["first_name"], identity["last_name"])
        lines = [
            "👤 کارت کاربر",
            self._identity_line(
                username=identity["username"],
                telegram_id=identity["telegram_id"],
                display_name=name,
            ),
            f"وضعیت: {status}",
            f"اعتبار: {balance}",
            f"سفارش‌ها: {to_persian_digits(orders)} | مجموعهٔ فلش‌کارت: {to_persian_digits(decks)}",
            "هدیهٔ اولین بار: "
            + (
                "🎁 فلش‌کارت رایگان مانده"
                if free.get("deck")
                else "فلش‌کارت استفاده شده"
            )
            + " | "
            + (
                "🎁 آزمون رایگان مانده"
                if free.get("quiz")
                else "آزمون استفاده شده"
            ),
            f"عضویت: {self._timestamp_text(identity.get('joined_at'))}",
            f"آخرین بازدید: {self._timestamp_text(identity.get('last_seen_at'))}",
        ]
        subscription_status = identity.get("subscription_status")
        if subscription_status:
            sub_line = f"عضویت کانال: {subscription_status}"
            if subscription_status == "member" and identity.get(
                "subscription_verified_at"
            ):
                sub_line += (
                    f" ({self._timestamp_text(identity.get('subscription_verified_at'))})"
                )
            lines.append(sub_line)
        else:
            lines.append("عضویت کانال: بررسی نشده")
        lines.extend(
            [
                "",
                "💡 اعتبار و سفارش‌ها در «💳 پرداخت‌ها» بررسی می‌شوند.",
            ]
        )
        buttons: list[list] = [
            [
                Button.inline(
                    "🔄 بازنشانی هدیه رایگان",
                    f"adm:hub:usr:fr:{int(target_id)}".encode("ascii"),
                )
            ]
        ]
        if not self._is_admin(target_id):  # type: ignore[attr-defined]
            buttons.append(
                [
                    Button.inline(
                        "🚫 مسدودکردن" if not identity["banned"] else "✅ رفع مسدودی",
                        f"adm:hub:usr:tg:{int(target_id)}".encode("ascii"),
                    )
                ]
            )
        buttons.append(self._hub_back_row("usr"))
        buttons.extend(self._admin_menu())  # type: ignore[attr-defined]
        await self._edit_or_reply(event, "\n".join(lines), buttons)  # type: ignore[attr-defined]

    async def _show_admin_pending_receipt_users(self, event: Any) -> None:
        payments = await self.db.pending_review_payments(limit=10, offset=0)
        lines = ["🧾 رسیدهای در انتظار بررسی"]
        buttons: list[list] = []
        if not payments:
            lines.append("")
            lines.append("در حال حاضر رسیدی در انتظار بررسی نیست.")
        for payment in payments:
            identity = await self.db.user_identity(int(payment["user_id"]))
            who = self._identity_line(
                username=(identity or {}).get("username"),
                telegram_id=payment["user_id"],
                display_name=self._user_display_name(
                    (identity or {}).get("first_name", ""),
                    (identity or {}).get("last_name", ""),
                ),
            )
            number = to_persian_digits(payment["payment_id"])
            lines.append("")
            lines.append(f"#{number} | {who}")
            lines.append(
                f"وضعیت: {self._payment_status_label(payment['status'])}"  # type: ignore[attr-defined]
            )
            buttons.append(
                [
                    Button.inline(
                        f"🔎 جزئیات سفارش {number}",
                        f"adm:pd:{payment['payment_id']}".encode("ascii"),
                    )
                ]
            )
        buttons.append([Button.inline("💳 صف پرداخت‌ها", b"adm:hub:pay")])
        buttons.append(self._hub_back_row("usr"))
        buttons.extend(self._admin_menu())  # type: ignore[attr-defined]
        await self._edit_or_reply(event, "\n".join(lines), buttons)  # type: ignore[attr-defined]

    async def _show_admin_blocked_users(self, event: Any, page: int = 0) -> None:
        page_size = 12
        page = max(0, int(page))
        users = await self.db.blocked_users(
            limit=page_size + 1, offset=page * page_size
        )
        has_more = len(users) > page_size
        users = users[:page_size]
        lines = ["🚫 کاربران مسدود", f"صفحهٔ {to_persian_digits(page + 1)}"]
        if not users:
            lines.append("")
            lines.append("هیچ کاربری مسدود نیست.")
        buttons: list[list] = []
        for user in users:
            who = self._identity_line(
                username=user.get("username"),
                telegram_id=user["telegram_id"],
                display_name=self._user_display_name(
                    user.get("first_name", ""), user.get("last_name", "")
                ),
            )
            lines.append(f"• {who}")
            buttons.append(
                [
                    Button.inline(
                        button_label(f"🔎 {who}"),
                        f"adm:hub:usr:card:{user['telegram_id']}".encode("ascii"),
                    )
                ]
            )
        nav = []
        if page > 0:
            nav.append(
                Button.inline(
                    "◀️ قبلی", f"adm:hub:usr:banned:{page - 1}".encode("ascii")
                )
            )
        if has_more:
            nav.append(
                Button.inline(
                    "بعدی ▶️", f"adm:hub:usr:banned:{page + 1}".encode("ascii")
                )
            )
        if nav:
            buttons.append(nav)
        buttons.append(self._hub_back_row("usr"))
        buttons.extend(self._admin_menu())  # type: ignore[attr-defined]
        await self._edit_or_reply(event, "\n".join(lines), buttons)  # type: ignore[attr-defined]

    async def _show_admin_free_reset_confirm(
        self, event: Any, target_id: int
    ) -> None:
        identity = await self.db.user_identity(int(target_id))
        if identity is None:
            await self._ack(event, "این کاربر پیدا نشد.", alert=True)  # type: ignore[attr-defined]
            return
        self._pending_free_reset[int(event.sender_id)] = int(target_id)
        who = self._identity_line(
            username=(identity or {}).get("username"),
            telegram_id=target_id,
            display_name=self._user_display_name(
                (identity or {}).get("first_name", ""),
                (identity or {}).get("last_name", ""),
            ),
        )
        await self._edit(  # type: ignore[attr-defined]
            event,
            "🔄 بازنشانی هدیهٔ رایگان\n\n"
            f"{who}\n\n"
            "با تأیید، هر دو هدیهٔ «اولین بار رایگان» (فلش‌کارت و آزمون) برای این "
            "کاربر برمی‌گردد.",
            [
                [
                    Button.inline(
                        "✅ تأیید بازنشانی",
                        f"adm:hub:usr:frx:{int(target_id)}".encode("ascii"),
                    )
                ],
                [
                    Button.inline(
                        "↩️ انصراف",
                        f"adm:hub:usr:card:{int(target_id)}".encode("ascii"),
                    )
                ],
            ],
        )

    async def _reset_user_free_gift(
        self, event: Any, admin_id: int, target_id: int
    ) -> None:
        if self._pending_free_reset.pop(admin_id, None) != int(target_id):
            await self._ack(  # type: ignore[attr-defined]
                event,
                "ابتدا بازنشانی را در کارت کاربر تأیید کنید.",
                alert=True,
            )
            return
        identity = await self.db.user_identity(int(target_id))
        if identity is None:
            await self._ack(event, "این کاربر پیدا نشد.", alert=True)  # type: ignore[attr-defined]
            return
        await self.db.reset_free_uses(int(target_id), admin_id=admin_id)
        await self._ack(event, "هدیهٔ رایگان بازنشانی شد.")  # type: ignore[attr-defined]
        await self._show_admin_user_card(event, target_id)

    async def _toggle_admin_block(
        self, event: Any, admin_id: int, target_id: int
    ) -> None:
        if self._is_admin(target_id):  # type: ignore[attr-defined]
            await self._ack(  # type: ignore[attr-defined]
                event, "حساب مدیران را نمی‌توان مسدود کرد.", alert=True
            )
            return
        identity = await self.db.user_identity(int(target_id))
        if identity is None:
            await self._ack(event, "این کاربر پیدا نشد.", alert=True)  # type: ignore[attr-defined]
            return
        await self.db.set_banned(
            int(target_id), not identity["banned"], admin_id=admin_id
        )
        await self._ack(  # type: ignore[attr-defined]
            event, "مسدود شد." if not identity["banned"] else "مسدودی برداشته شد."
        )
        await self._show_admin_user_card(event, target_id)

    async def _admin_user_search(
        self, event: Any, admin_id: int, query: str
    ) -> None:
        matches = await self.db.search_users(query, limit=5)
        if not matches:
            await event.reply(
                "کاربری با این شناسه یا نام‌کاربری پیدا نشد. "
                "شناسهٔ عددی یا @نام‌کاربری را دقیق بفرستید.",
                buttons=[[Button.inline("↩️ کاربران", b"adm:hub:usr")]],
            )
            return
        if len(matches) == 1:
            await self._show_admin_user_card(event, int(matches[0]["telegram_id"]))
            return
        buttons = []
        lines = ["چند کاربر با این نام‌کاربری پیدا شد؛ یکی را انتخاب کنید:"]
        for match in matches:
            who = self._identity_line(
                username=match.get("username"),
                telegram_id=match["telegram_id"],
                display_name=self._user_display_name(
                    match.get("first_name", ""), match.get("last_name", "")
                ),
            )
            lines.append(f"• {who}")
            buttons.append(
                [
                    Button.inline(
                        button_label(who),
                        f"adm:hub:usr:card:{match['telegram_id']}".encode("ascii"),
                    )
                ]
            )
        await event.reply("\n".join(lines), buttons=buttons)

    async def _show_admin_payments(self, event: Any, page: int = 0) -> None:
        page = max(0, int(page))
        page_size = self.PAYMENT_ADMIN_PAGE_SIZE
        payments = await self.db.pending_review_payments(
            limit=page_size + 1, offset=page * page_size
        )
        has_more = len(payments) > page_size
        payments = payments[:page_size]
        total = await self.db.count_pending_review_payments()
        buttons: list[list] = [
            [
                Button.inline("📋 تاریخچه", b"adm:hub:pay:hist"),
                Button.inline("📊 آمار", b"adm:hub:pay:stats"),
            ]
        ]
        if not payments:
            if page > 0 and total:
                await self._show_admin_payments(event, 0)
                return
            buttons.append([Button.inline("↩️ بازگشت", b"adm:home")])
            await self._edit_or_reply(  # type: ignore[attr-defined]
                event,
                "💳 پرداخت‌های در انتظار\n\nدر حال حاضر پرداختی برای بررسی وجود ندارد.",
                buttons,
            )
            return
        identities = await self.db.identities_for(
            [int(payment["user_id"]) for payment in payments]
        )
        lines = [
            f"💳 پرداخت‌های در انتظار — {to_persian_digits(total)} مورد",
            f"صفحهٔ {to_persian_digits(page + 1)}؛ برای بررسی، سفارش را باز کنید.",
        ]
        for payment in payments:
            number = to_persian_digits(payment["payment_id"])
            seconds = payment.get("purchased_seconds")
            price = payment.get("purchased_price")
            package = (
                format_duration(int(seconds))
                if seconds is not None
                else payment["plan_code"]
            )
            amount = format_toman(int(price)) if price is not None else "نامشخص"
            identity = identities.get(int(payment["user_id"]), {})
            who = self._identity_line(
                username=identity.get("username"),
                telegram_id=int(payment["user_id"]),
                display_name=self._user_display_name(
                    identity.get("first_name", ""), identity.get("last_name", "")
                ),
            )
            lines.append(
                f"\n#{number} · {who}\n{package} | {amount} | {self._date_text(payment.get('created_at'))}"
            )
            buttons.append(
                [
                    Button.inline(
                        f"🔎 جزئیات سفارش {number}",
                        f"adm:pd:{payment['payment_id']}".encode(),
                    )
                ]
            )
        nav = []
        if page > 0:
            nav.append(Button.inline("◀️ قبلی", f"adm:pay:{page - 1}".encode()))
        if has_more:
            nav.append(Button.inline("بعدی ▶️", f"adm:pay:{page + 1}".encode()))
        if nav:
            buttons.append(nav)
        buttons.append([Button.inline("↩️ بازگشت", b"adm:home")])
        await self._edit_or_reply(event, "\n".join(lines), buttons)  # type: ignore[attr-defined]

    async def _show_admin_payment_history(
        self, event: Any, page: int = 0, status: str = "all"
    ) -> None:
        page = max(0, int(page))
        filters = dict(self.PAYMENT_HISTORY_FILTERS)
        if status not in filters:
            status = "all"
        page_size = 8
        payments = await self.db.all_payments(
            limit=page_size + 1,
            offset=page * page_size,
            status=None if status == "all" else status,
        )
        has_more = len(payments) > page_size
        payments = payments[:page_size]
        active = filters[status]
        lines = [
            "📋 تاریخچه پرداخت‌ها — " + active,
            f"صفحهٔ {to_persian_digits(page + 1)}",
        ]
        buttons: list[list] = [
            [
                Button.inline(
                    button_label(f"✅ {label}" if code == status else label),
                    f"adm:hub:pay:hist:{code}".encode(),
                )
                for code, label in self.PAYMENT_HISTORY_FILTERS[:2]
            ],
            [
                Button.inline(
                    button_label(f"✅ {label}" if code == status else label),
                    f"adm:hub:pay:hist:{code}".encode(),
                )
                for code, label in self.PAYMENT_HISTORY_FILTERS[2:]
            ],
        ]
        identities = await self.db.identities_for(
            [int(payment["user_id"]) for payment in payments]
        )
        if not payments:
            lines.append(
                "\nدر این وضعیت سفارشی نیست."
                if page == 0
                else "\nمورد دیگری وجود ندارد."
            )
        for payment in payments:
            number = to_persian_digits(payment["payment_id"])
            identity = identities.get(int(payment["user_id"]), {})
            who = self._identity_line(
                username=identity.get("username"),
                telegram_id=int(payment["user_id"]),
                display_name=self._user_display_name(
                    identity.get("first_name", ""), identity.get("last_name", "")
                ),
            )
            lines.append(
                f"\n#{number} · {who}\n"
                f"{self._payment_status_label(payment['status'])} | "  # type: ignore[attr-defined]
                f"{self._date_text(payment.get('created_at'))}"
            )
            buttons.append(
                [
                    Button.inline(
                        f"🔎 جزئیات سفارش {number}",
                        f"adm:pd:{payment['payment_id']}".encode(),
                    )
                ]
            )
        nav = []
        if page > 0:
            nav.append(
                Button.inline(
                    "◀️ قبلی", f"adm:hub:pay:hist:{status}:{page - 1}".encode()
                )
            )
        if has_more:
            nav.append(
                Button.inline(
                    "بعدی ▶️", f"adm:hub:pay:hist:{status}:{page + 1}".encode()
                )
            )
        if nav:
            buttons.append(nav)
        buttons.append([Button.inline("💳 پرداخت‌های در انتظار", b"adm:hub:pay")])
        buttons.append([Button.inline("↩️ بازگشت", b"adm:home")])
        await self._edit_or_reply(event, "\n".join(lines), buttons)  # type: ignore[attr-defined]

    async def _show_admin_payment_detail(self, event: Any, payment_id: int) -> None:
        payment = await self.db.payment(payment_id)
        if payment is None:
            await self._ack(event, "سفارش پیدا نشد.", alert=True)  # type: ignore[attr-defined]
            await self._show_admin_payments(event)
            return
        seconds = payment.get("purchased_seconds")
        price = payment.get("purchased_price")
        validity_days = payment.get("purchased_validity_days")
        if payment["receipt_path"]:
            receipt_state = "ثبت شده"
        elif payment["status"] in {"awaiting_payment", "pending", "cancelled"}:
            receipt_state = "ثبت نشده"
        elif payment["status"] in {"approved", "rejected", "pending_review"}:
            receipt_state = "پاسخ‌شده؛ رسید موجود نیست"
        else:
            receipt_state = "در دسترس نیست"
        owner = await self.db.user_identity(int(payment["user_id"]))
        owner_line = (
            self._identity_line(
                username=owner["username"],
                telegram_id=int(payment["user_id"]),
                display_name=self._user_display_name(
                    owner["first_name"], owner["last_name"]
                ),
            )
            if owner
            else self._identity_line(
                username=None, telegram_id=int(payment["user_id"])
            )
        )
        lines = [
            f"🔎 سفارش {to_persian_digits(payment_id)}",
            f"شناسه سفارش: #{to_persian_digits(payment_id)}",
            f"کاربر: {owner_line}",
            f"طرح: {payment['plan_code']}",
            f"بسته: {format_duration(int(seconds)) if seconds is not None else payment['plan_code']}",
            f"مدت اعتبار: {format_validity(validity_days)}",
            f"مبلغ: {format_toman(int(price)) if price is not None else 'نامشخص'}",
            f"وضعیت: {self._payment_status_label(payment['status'])}",  # type: ignore[attr-defined]
            f"ثبت: {to_persian_digits(str(payment['created_at'])[:16].replace('T', ' '))}",
            f"رسید: {receipt_state}",
        ]
        if payment.get("reviewed_at"):
            lines.append(
                f"بررسی: {self._support_when(payment['reviewed_at'])} | مدیر: {payment['reviewed_by']}"  # type: ignore[attr-defined]
            )
        if payment.get("review_reason"):
            lines.append(f"دلیل: {payment['review_reason']}")
        if payment["status"] == "approved":
            credit_state = (
                "ثبت شده" if payment.get("credit_ledger_id") else "سابقهٔ قدیمی"
            )
            lines.append(f"اعتبار متصل به سفارش: {credit_state}")
        buttons: list[list] = []
        if payment["receipt_path"]:
            buttons.append(
                [Button.inline("📷 مشاهده رسید", f"adm:rcpt:{payment_id}".encode())]
            )
        if is_pending_payment_review(payment):
            buttons.append(
                [
                    Button.inline("✅ تأیید پرداخت", f"payv:{payment_id}:a".encode()),
                    Button.inline("❌ رد پرداخت", f"payv:{payment_id}:r".encode()),
                ]
            )
        buttons.append([Button.inline("💳 پرداخت‌های در انتظار", b"adm:hub:pay")])
        buttons.append([Button.inline("📋 تاریخچه پرداخت‌ها", b"adm:hub:pay:hist")])
        buttons.append([Button.inline("↩️ بازگشت", b"adm:home")])
        await self._edit_or_reply(event, "\n".join(lines), buttons)  # type: ignore[attr-defined]

    async def _show_receipt(self, event: Any, data: str) -> None:
        try:
            payment_id = int(data.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            await self._ack(event, "درخواست نامعتبر است.", alert=True)  # type: ignore[attr-defined]
            return
        payment = await self.db.payment(payment_id)
        if payment is None:
            await self._ack(event, "این پرداخت پیدا نشد.", alert=True)  # type: ignore[attr-defined]
            return
        path = payment.get("receipt_path")
        if (
            not path
            or not await run_file_io(
                _is_safe_receipt_path, self.settings.receipt_dir, path
            )
            or not await run_file_io(
                _receipt_image_signature,
                Path(str(path)),
                max_bytes=self.settings.max_payment_receipt_bytes,
            )
        ):
            await self._ack(  # type: ignore[attr-defined]
                event, "برای این پرداخت رسید معتبری ثبت نشده است.", alert=True
            )
            return
        await self._ack(event)  # type: ignore[attr-defined]
        number = to_persian_digits(payment_id)
        buttons: list[list] = []
        if is_pending_payment_review(payment):
            buttons.append(
                [
                    Button.inline("✅ تأیید پرداخت", f"payv:{payment_id}:a".encode()),
                    Button.inline("❌ رد پرداخت", f"payv:{payment_id}:r".encode()),
                ]
            )
        buttons.append(
            [Button.inline("↩️ جزئیات سفارش", f"adm:pd:{payment_id}".encode())]
        )
        owner = await self._identity_for_id(int(payment["user_id"]))
        await self._send_telegram_file(  # type: ignore[attr-defined]
            await event.get_input_chat(),
            Path(str(path)),
            caption=(
                f"📷 رسید پرداخت #{number}\n"
                f"کاربر: {owner} — طرح: {payment['plan_code']}"
            )[:TELEGRAM_CAPTION_LIMIT],
            buttons=buttons,
        )

    async def _show_admin_audit(self, event: Any, page: int = 0) -> None:
        page_size = 7
        page = max(0, int(page))
        rows = await self.db.recent_audit(limit=page_size + 1, offset=page * page_size)
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        action_labels = {
            "grant_credit": "افزودن اعتبار",
            "unlimited_on": "فعال‌کردن کاربر ویژه",
            "unlimited_off": "لغو وضعیت کاربر ویژه",
            "user_banned": "مسدودکردن کاربر",
            "user_unbanned": "رفع مسدودی کاربر",
            "payment_approved": "تأیید پرداخت",
            "payment_rejected": "رد پرداخت",
            "broadcast": "ارسال پیام همگانی",
            "ticket_closed": "بستن درخواست پشتیبانی",
            "ticket_reopened": "بازگشایی درخواست پشتیبانی",
            "receipt_expired": "حذف رسید منقضی‌شده",
            "transcript_expired": "حذف متن منقضی‌شده",
            "free_use_reset": "بازنشانی هدیهٔ رایگان",
        }
        lines = ["🛡 گزارش فعالیت مدیران", "", f"صفحهٔ {to_persian_digits(page + 1)}:"]
        if not rows:
            lines.append("هنوز فعالیتی ثبت نشده است.")
        ids = [int(row["admin_id"]) for row in rows]
        ids.extend(
            int(row["subject_user"])
            for row in rows
            if row["subject_user"] is not None
        )
        identities = await self.db.identities_for(ids)

        def who(user_id: int) -> str:
            identity = identities.get(int(user_id), {})
            return self._identity_line(
                username=identity.get("username"),
                telegram_id=user_id,
                display_name=self._user_display_name(
                    identity.get("first_name", ""), identity.get("last_name", "")
                ),
            )

        for row in rows:
            action = action_labels.get(row["action"], row["action"].replace("_", " "))
            subject = (
                f" — کاربر: {who(row['subject_user'])}"
                if row["subject_user"] is not None
                else ""
            )
            lines.append(
                f"• {row['created_at']} — مدیر: {who(row['admin_id'])} — {action}{subject}"
            )
        nav = []
        if page > 0:
            nav.append(
                Button.inline("◀️ قبلی", f"adm:audit:{page - 1}".encode("ascii"))
            )
        if has_more:
            nav.append(
                Button.inline("بعدی ▶️", f"adm:audit:{page + 1}".encode("ascii"))
            )
        buttons = ([nav] if nav else []) + self._admin_menu()  # type: ignore[attr-defined]
        await self._edit_or_reply(event, "\n".join(lines), buttons)  # type: ignore[attr-defined]

    async def _check_admin_cooldown(
        self, event: Any, action: str, cooldown_seconds: float
    ) -> bool:
        admin_id = event.sender_id
        if admin_id is None:
            return False
        now = self.deps.monotonic()
        key = (admin_id, action)
        previous = self._admin_action_times.get(key)
        if previous is not None:
            remaining = cooldown_seconds - (now - previous)
            if remaining > 0:
                reply = event.respond if hasattr(event, "respond") else event.reply
                await reply(
                    f"این دستور موقتاً محدود شده است؛ {math.ceil(remaining)} ثانیه دیگر تلاش کنید.",
                    buttons=self._admin_menu(),  # type: ignore[attr-defined]
                )
                return False
        self._admin_action_times[key] = now
        if len(self._admin_action_times) > 1024:
            cutoff = now - max(cooldown_seconds, self.BROADCAST_COOLDOWN_SECONDS) * 2
            self._admin_action_times = {
                cached_key: timestamp
                for cached_key, timestamp in self._admin_action_times.items()
                if timestamp >= cutoff
            }
        return True

    async def _admin_grant(self, event: Any, text: str) -> None:
        parts = text.split()
        if _command_name(text) != "/grant" or len(parts) != 3:
            await event.reply(
                "قالب درست برای افزودن اعتبار:\n"
                "/grant <شناسه کاربر> <ساعت>\n"
                "مثال: /grant 123456789 2",
                buttons=self._admin_menu(),  # type: ignore[attr-defined]
            )
            return
        target = _parse_telegram_user_id(parts[1])
        hours = _parse_telegram_user_id(parts[2])
        if target is None or hours is None:
            await event.reply(
                "قالب درست برای افزودن اعتبار:\n"
                "/grant <شناسه کاربر> <ساعت>\n"
                "مثال: /grant 123456789 2",
                buttons=self._admin_menu(),  # type: ignore[attr-defined]
            )
            return
        if self._is_admin(target):  # type: ignore[attr-defined]
            await event.reply(
                "برای حفظ تفکیک دسترسی‌ها، امکان افزودن اعتبار به حساب مدیر دیگر وجود ندارد.",
                buttons=self._admin_menu(),  # type: ignore[attr-defined]
            )
            return
        if not (0 < hours <= 10_000):
            await event.reply(
                "تعداد ساعت باید بین ۱ و ۱۰٬۰۰۰ باشد.",
                buttons=self._admin_menu(),  # type: ignore[attr-defined]
            )
            return
        if not await self._check_admin_cooldown(
            event, "grant", self.ADMIN_COMMAND_COOLDOWN_SECONDS
        ):
            return
        await self.db.ensure_user(target)
        await self.db.add_credit(
            target,
            hours * 3600,
            note=f"admin grant by {event.sender_id}",
            validity_days=None,
            admin_id=event.sender_id,
        )
        owner = await self._identity_for_id(target)
        await event.reply(
            f"✅ {format_duration(hours * 3600)} اعتبار به حساب کاربر {owner} اضافه شد.",
            buttons=self._admin_menu(),  # type: ignore[attr-defined]
        )

    async def _admin_ban(self, event: Any, text: str) -> None:
        parts = text.split()
        if _command_name(text) not in {"/ban", "/unban"} or len(parts) != 2:
            await event.reply(
                "قالب درست:\n"
                "مسدودکردن: /ban <شناسه کاربر>\n"
                "رفع مسدودی: /unban <شناسه کاربر>",
                buttons=self._admin_menu(),  # type: ignore[attr-defined]
            )
            return
        target = _parse_telegram_user_id(parts[1])
        if target is None:
            await event.reply(
                "قالب درست:\n"
                "مسدودکردن: /ban <شناسه کاربر>\n"
                "رفع مسدودی: /unban <شناسه کاربر>",
                buttons=self._admin_menu(),  # type: ignore[attr-defined]
            )
            return
        banning = _command_name(text) == "/ban"
        if self._is_admin(target):  # type: ignore[attr-defined]
            await event.reply(
                "حساب مدیران را نمی‌توان مسدود کرد.",
                buttons=self._admin_menu(),  # type: ignore[attr-defined]
            )
            return
        action = "ban" if banning else "unban"
        if not await self._check_admin_cooldown(
            event, action, self.ADMIN_COMMAND_COOLDOWN_SECONDS
        ):
            return
        await self.db.ensure_user(target)
        await self.db.set_banned(target, banning, admin_id=event.sender_id)
        logger.info("ban flag changed banned=%s", banning)
        owner = await self._identity_for_id(target)
        await event.reply(
            f"🚫 حساب کاربر {owner} مسدود شد."
            if banning
            else f"✅ مسدودی حساب کاربر {owner} برداشته شد.",
            buttons=self._admin_menu(),  # type: ignore[attr-defined]
        )

    async def _admin_unlimited(self, event: Any, text: str) -> None:
        parts = text.split(maxsplit=3)
        if (
            _command_name(text) != "/unlimited"
            or len(parts) < 3
            or parts[2].lower() not in ("on", "off")
        ):
            await event.reply(
                "قالب درست:\n"
                "فعال‌سازی: /unlimited <شناسه کاربر> on <دلیل>\n"
                "غیرفعال‌سازی: /unlimited <شناسه کاربر> off\n"
                "برای فعال‌سازی، دلیل را هم وارد کنید.",
                buttons=self._admin_menu(),  # type: ignore[attr-defined]
            )
            return
        target = _parse_telegram_user_id(parts[1])
        if target is None:
            await event.reply(
                "قالب درست:\n"
                "فعال‌سازی: /unlimited <شناسه کاربر> on <دلیل>\n"
                "غیرفعال‌سازی: /unlimited <شناسه کاربر> off\n"
                "برای فعال‌سازی، دلیل را هم وارد کنید.",
                buttons=self._admin_menu(),  # type: ignore[attr-defined]
            )
            return
        enable = parts[2].lower() == "on"
        reason = parts[3].strip() if len(parts) == 4 else ""
        if enable and not reason:
            await event.reply(
                "برای فعال‌کردن وضعیت کاربر ویژه، واردکردن دلیل الزامی است.",
                buttons=self._admin_menu(),  # type: ignore[attr-defined]
            )
            return
        if self._is_admin(target):  # type: ignore[attr-defined]
            await event.reply(
                "فعال‌کردن وضعیت کاربر ویژه برای حساب مدیران مجاز نیست.",
                buttons=self._admin_menu(),  # type: ignore[attr-defined]
            )
            return
        action = "unlimited_on" if enable else "unlimited_off"
        if not await self._check_admin_cooldown(
            event, action, self.ADMIN_COMMAND_COOLDOWN_SECONDS
        ):
            return
        await self.db.ensure_user(target)
        await self.db.set_unlimited(
            target, enable, admin_id=event.sender_id, reason=reason
        )
        logger.info("unlimited access updated enabled=%s", enable)
        owner = await self._identity_for_id(target)
        if enable:
            message = f"⭐ کاربر {owner} به فهرست کاربران ویژه افزوده شد؛ اعتبار او نامحدود است."
        else:
            message = f"وضعیت ویژهٔ کاربر {owner} لغو شد؛ از این پس استفاده از ربات به اعتبار او وابسته است."
        await event.reply(message, buttons=self._admin_menu())  # type: ignore[attr-defined]

    async def _recover_broadcasts(self) -> None:
        runs = await self.db.broadcast_recover_runs()
        for run in runs:
            try:
                await self.client.send_message(
                    run["admin_id"],
                    texts.BROADCAST_RECOVERED.format(
                        run=to_persian_digits(run["id"]),
                        cursor=to_persian_digits(run["last_telegram_id"]),
                    ),
                    buttons=[[Button.inline(texts.BTN_BROADCAST_RESUME,
                                            f"adm:bcresume:{run['id']}".encode("ascii"))]],
                    parse_mode=None,
                )
            except Exception:
                METRICS.record_broadcast_failure()
                logger.warning("broadcast recovery notice failed run=%s", run["id"])

    async def _prepare_broadcast(self, event: Any, admin_id: int, text: str) -> None:
        text = text.strip()
        if len(text) > self.MAX_BROADCAST_CHARS:
            await event.reply(texts.BROADCAST_TOO_LONG)
            return
        if self._broadcast_running:
            await event.reply(texts.BROADCAST_RUNNING)
            return
        resume_id = self._broadcast_resume.pop(admin_id, None)
        cursor = 0
        if resume_id is not None:
            run = await self.db.broadcast_get_run(resume_id)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if not run or run["admin_id"] != admin_id or run["status"] != "stopped" or run["text_hash"] != digest:
                resume_id = None
            else:
                cursor = int(run["last_telegram_id"])
        count = await self.db.count_broadcast_targets(admin_id, after_id=cursor)
        self._pending_broadcast.discard(admin_id)
        self._broadcast_previews[admin_id] = (text, resume_id)
        # Send the complete literal text separately so a 4096-character preview fits.
        await event.reply(text, parse_mode=None)
        await event.reply(
            texts.BROADCAST_PREVIEW.format(text="", count=to_persian_digits(count)),
            buttons=[[Button.inline(texts.BTN_BROADCAST_SEND, b"adm:bcgo"),
                      Button.inline(texts.BTN_CANCEL, b"adm:bccancel")]],
        )

    async def _send_broadcast_page(self, user_id: int, page: str) -> str:
        flood_err = self.deps.flood_wait_error
        retries = 0
        hard_errors = {"UserIsBlockedError", "ChatWriteForbiddenError", "PeerIdInvalidError",
                       "InputUserDeactivatedError", "UserDeactivatedError", "ChatIdInvalidError"}
        while True:
            try:
                await self.client.send_message(user_id, page, parse_mode=None)
            except flood_err as exc:
                seconds = max(0.0, float(getattr(exc, "seconds", 0.0) or 0.0))
                if seconds <= 60:
                    if retries >= self.BROADCAST_MAX_FLOOD_WAIT_RETRIES:
                        METRICS.record_broadcast_failure()
                        return "failed"
                    retries += 1
                logger.warning("broadcast flood wait seconds=%s", seconds)
                await asyncio.sleep(seconds + 1.0)
                continue
            except Exception as exc:
                METRICS.record_broadcast_failure()
                logger.warning("broadcast delivery failed error=%s", type(exc).__name__)
                return "hard" if type(exc).__name__ in hard_errors else "failed"
            await asyncio.sleep(self.BROADCAST_THROTTLE_SECONDS)
            return "success"

    async def _broadcast(self, event: Any, text: str, *, resume_id: int | None = None) -> None:
        text = text.strip()
        reply = event.respond if hasattr(event, "respond") else event.reply
        if not text:
            await reply(texts.BROADCAST_NEEDS_TEXT)
            return
        if len(text) > self.MAX_BROADCAST_CHARS:
            await reply(texts.BROADCAST_TOO_LONG)
            return
        admin_id = event.sender_id
        if admin_id is None:
            return
        if self._broadcast_running:
            await reply(texts.BROADCAST_RUNNING)
            return
        # Reserve before the first await so competing admins cannot start a run.
        self._broadcast_running.add(admin_id)
        self._broadcast_stop.clear()
        run_id = None
        cursor = sent = failed = 0
        outcomes: list[tuple[int, str]] = []
        status = "stopped"
        total = 0
        try:
            if not await self._check_admin_cooldown(event, "broadcast", self.BROADCAST_COOLDOWN_SECONDS):
                return
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if resume_id is not None and await self.db.broadcast_resume_run(resume_id, admin_id, digest):
                run_id = resume_id
                run = await self.db.broadcast_get_run(run_id)
                cursor, sent, failed = int(run["last_telegram_id"]), int(run["sent"]), int(run["failed"])
            else:
                run_id = await self.db.broadcast_create_run(admin_id, digest)
            total = sent + failed + await self.db.count_broadcast_targets(admin_id, after_id=cursor)
            progress = await self.client.send_message(
                admin_id, texts.BROADCAST_PROGRESS.format(sent=to_persian_digits(sent),
                    failed=to_persian_digits(failed), total=to_persian_digits(total)),
                buttons=[[Button.inline(texts.BTN_BROADCAST_STOP, b"adm:bcstop")]], parse_mode=None,
            )
            while not self._broadcast_stop.is_set():
                users = await self.db.user_ids_after(cursor, 500)
                if not users:
                    status = "done"
                    break
                for user_id in users:
                    if self._broadcast_stop.is_set():
                        break
                    if user_id == admin_id:
                        cursor = user_id
                        continue
                    outcome = await self._send_broadcast_page(user_id, text)
                    cursor = user_id
                    outcomes.append((user_id, outcome))
                    sent += outcome == "success"
                    failed += outcome != "success"
                    if (sent + failed) % self.BROADCAST_BATCH_PROGRESS_EVERY == 0:
                        try:
                            await progress.edit(
                                texts.BROADCAST_PROGRESS.format(sent=to_persian_digits(sent),
                                    failed=to_persian_digits(failed), total=to_persian_digits(total)),
                                buttons=[[Button.inline(texts.BTN_BROADCAST_STOP, b"adm:bcstop")]],
                            )
                        except Exception:
                            METRICS.record_broadcast_failure()
                            logger.warning("broadcast progress edit failed run=%s", run_id)
                await self.db.broadcast_checkpoint(run_id, cursor, sent, failed, outcomes)
                outcomes.clear()
            await self.db.broadcast_checkpoint(run_id, cursor, sent, failed, outcomes, status=status)
            outcomes.clear()
            await self.db.audit(admin_id, "broadcast", f"run:{run_id} delivered:{sent} failed:{failed} targets:{total} stopped:{int(status == 'stopped')}")
            await reply(texts.BROADCAST_SUMMARY.format(sent=to_persian_digits(sent),
                        failed=to_persian_digits(failed), total=to_persian_digits(total)),
                        buttons=self._admin_menu())
        except BaseException as exc:
            if not isinstance(exc, asyncio.CancelledError):
                METRICS.record_broadcast_failure()
            if run_id is not None:
                try:
                    await self.db.broadcast_checkpoint(run_id, cursor, sent, failed, outcomes, status="stopped")
                except Exception:
                    METRICS.record_broadcast_failure()
                    logger.warning("broadcast checkpoint failed run=%s", run_id)
            raise
        finally:
            self._broadcast_running.discard(admin_id)
            if run_id is not None:
                self._admin_action_times[(admin_id, "broadcast")] = self.deps.monotonic()

    async def _route_admin_hub(self, event: Any, admin_id: int, data: str) -> None:
        """Dispatch ``adm:hub:<hub>[:action[:arg]]`` to the merged screens."""
        parts = data.split(":")
        hub = parts[2] if len(parts) > 2 else ""
        rest = parts[3:]
        action = rest[0] if rest else ""
        if hub == "pay":
            if not action:
                await self._show_admin_payments(event, 0)
                return
            if action == "hist":
                status = rest[1] if len(rest) > 1 else "all"
                if status not in dict(self.PAYMENT_HISTORY_FILTERS):
                    await self._ack(  # type: ignore[attr-defined]
                        event, "فیلتر تاریخچه نامعتبر است.", alert=True
                    )
                    return
                page = _to_int(rest[2]) if len(rest) > 2 else 0
                if page is None:
                    await self._ack(  # type: ignore[attr-defined]
                        event, "صفحهٔ تاریخچه نامعتبر است.", alert=True
                    )
                    return
                await self._show_admin_payment_history(event, max(0, page), status)
                return
            if action == "stats":
                period = rest[1] if len(rest) > 1 else "7d"
                if period not in self.ADMIN_PERIOD_KEYS:
                    await self._ack(  # type: ignore[attr-defined]
                        event, "بازهٔ زمانی نامعتبر است.", alert=True
                    )
                    return
                await self._show_admin_payments_report(event, period)
                return
            await self._ack(event, "بخش پرداخت نامعتبر است.", alert=True)  # type: ignore[attr-defined]
            return
        if hub == "usr":
            if not action:
                await self._show_admin_users(event)
                return
            if action == "stats":
                period = rest[1] if len(rest) > 1 else "7d"
                if period not in self.ADMIN_PERIOD_KEYS:
                    await self._ack(  # type: ignore[attr-defined]
                        event, "بازهٔ زمانی نامعتبر است.", alert=True
                    )
                    return
                await self._show_admin_users_report(event, period)
                return
            if action == "pending":
                await self._show_admin_pending_receipt_users(event)
                return
            if action == "banned":
                page = _to_int(rest[1]) if len(rest) > 1 else 0
                if page is None or page < 0:
                    await self._ack(  # type: ignore[attr-defined]
                        event, "صفحهٔ کاربران مسدود نامعتبر است.", alert=True
                    )
                    return
                await self._show_admin_blocked_users(event, page)
                return
            if action in {"card", "fr", "frx", "tg"}:
                target = _to_int(rest[1]) if len(rest) > 1 else None
                if target is None:
                    await self._ack(  # type: ignore[attr-defined]
                        event, "شناسهٔ کاربر نامعتبر است.", alert=True
                    )
                    return
                if action == "card":
                    await self._show_admin_user_card(event, target)
                elif action == "fr":
                    await self._show_admin_free_reset_confirm(event, target)
                elif action == "frx":
                    await self._reset_user_free_gift(event, admin_id, target)
                else:
                    await self._toggle_admin_block(event, admin_id, target)
                return
            await self._ack(event, "بخش کاربران نامعتبر است.", alert=True)  # type: ignore[attr-defined]
            return
        if hub == "sys":
            if not action:
                await self._show_admin_system_report(event, "7d")
                return
            if action in {"proc", "status"}:
                period = rest[1] if len(rest) > 1 else "7d"
                if period not in self.ADMIN_PERIOD_KEYS:
                    await self._ack(  # type: ignore[attr-defined]
                        event, "بازهٔ زمانی نامعتبر است.", alert=True
                    )
                    return
                if action == "proc":
                    await self._show_admin_processing_report(event, period)
                else:
                    await self._show_admin_system_report(event, period)
                return
            await self._ack(event, "بخش سامانه نامعتبر است.", alert=True)  # type: ignore[attr-defined]
            return
        await self._ack(event, "بخش مدیریت نامعتبر است.", alert=True)  # type: ignore[attr-defined]
