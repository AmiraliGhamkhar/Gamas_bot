"""Telegram admin panels for the Gamas Speech Platform (spec §60-§64).

UI companion of :mod:`gamas_bot.stt_platform`: provider cards, model registry,
route editor, quota panel, health, event logs, the provider test tool and the
dry-run request viewer. All user-facing text is Persian to match the existing
admin panel; technical ids stay ASCII. Nothing here ever renders API keys,
authorization headers, raw audio or transcript content (spec §54/§73).

The panels read the ONE registry in :mod:`gamas_bot.stt_platform.registry`, so
capabilities can never drift between the routing engine and the admin UI
(spec §47).
"""

from __future__ import annotations

import re
import tempfile
import wave
from datetime import datetime, timezone
from pathlib import Path

from telethon import Button

from .stt_platform.adapters import NativeSTTAdapter, RequestPreview, get_audio_duration_seconds
from .stt_platform.benchmark_metrics import BENCHMARK_PROFILES
from .stt_platform.models import (
    MODEL_DEPRECATED,
    STTModelInfo,
    STTModelRegistry,
    default_stt_model,
    static_stt_models,
)
from .stt_platform.registry import (
    STT_PROVIDER_REGISTRY,
    STTProviderInfo,
    free_class_fa,
    stt_registry_info,
)
from .stt_platform.router import resolve_route

SERVICE = "stt"

#: Provider test modes (spec §39). Sample modes use operator-installed
#: fixtures under ``tests/fixtures/stt/samples``; Gamas never generates fake
#: speech and never sends silence to a provider pretending it is a test.
TEST_MODES: dict[str, tuple[str, str | None]] = {
    "meta": ("فقط متادیتا (بدون ارسال صدا)", None),
    "sec10": ("نمونهٔ ۱۰ ثانیه‌ای", "ten_seconds.wav"),
    "fa": ("نمونهٔ فارسی", "persian.wav"),
    "med": ("نمونهٔ پزشکی فارسی", "medical.wav"),
}

#: Health-check mode labels (spec §38). The admin panel always says which one
#: produced a result; only ``test_credential_generation`` may spend quota.
_CHECK_MODE_FA = {
    "read_only": "READ_ONLY_HEALTH (بدون تولید/بدون هزینه)",
    "generation": "GENERATION_TEST (تولید واقعی — مصرف سهمیه)",
}

_CLASS_MARK = {
    "permanent_free": "🆓",
    "free_monthly": "🆓",
    "free_allocation": "🆓",
    "free_credit": "💳",
    "promotional_free": "⏳",
    "trial_only": "⏳",
    "paid_only": "💳",
    "region_restricted": "🚫",
    "unsupported": "🚫",
}


def _samples_dir() -> Path:
    """Fixture directory for provider sample tests (docs: STT benchmark README)."""
    return Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "stt" / "samples"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _fmt(value, unknown: str = "نامشخص") -> str:
    if value is None or value == "":
        return unknown
    return str(value)


def _fmt_bytes(size: int | None) -> str:
    if not size:
        return "نامشخص"
    if size >= 1_000_000_000:
        return f"{size / 1_000_000_000:.1f} GB"
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    if size >= 1000:
        return f"{size / 1000:.0f} KB"
    return f"{size} B"


def _fmt_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "نامشخص"
    total = int(round(float(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _onoff(enabled: bool) -> str:
    return "روشن" if enabled else "خاموش"


def _placeholder_wav(directory: str) -> Path:
    """One second of silence: only used to render request-shape previews."""
    path = Path(directory) / "preview_placeholder.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 16000)
    return path


def _flatten(prefix: str, value, out: list[tuple[str, str]]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _flatten(f"{prefix}.{key}" if prefix else str(key), item, out)
    elif isinstance(value, list):
        if value and isinstance(value[0], dict) and "content" in value[0]:
            out.append((f"{prefix}[{len(value)}]", f"<{len(value)} vocabulary terms>"))
        else:
            out.append((prefix, str(value)[:80]))
    else:
        out.append((prefix, str(value)[:80]))


class SttPanels:
    """Every screen under the ``admin:stt`` namespace."""

    def __init__(self, bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------- helpers
    @property
    def db(self):
        return self.bot.db

    @property
    def settings(self):
        return self.bot.settings

    async def _edit(self, event, text: str, buttons=None) -> None:
        await self.bot._edit_callback(event, text, buttons)

    def _home_buttons(self) -> list:
        return [
            [Button.inline("🏢 ارائه‌دهندگان", b"admin:stt:pv"),
             Button.inline("🧠 مدل‌ها", b"admin:stt:md")],
            [Button.inline("🗺 مسیرها (Routing)", b"admin:stt:rt"),
             Button.inline("📊 سهمیه‌ها (Quotas)", b"admin:stt:qt")],
            [Button.inline("🩺 سلامت", b"admin:stt:he"),
             Button.inline("📜 رویدادها (Logs)", b"admin:stt:lg")],
            [Button.inline("🧪 تست ارائه‌دهنده", b"admin:stt:tst"),
             Button.inline("🔍 درخواست آزمایشی (Dry-run)", b"admin:stt:dry")],
            [Button.inline("📏 معیارها (Benchmarks)", b"admin:stt:bm"),
             Button.inline("🔑 کلیدهای STT", b"admin:ai:keys:stt")],
            [Button.inline("⚙️ سیاست‌ها", b"admin:stt:pol"),
             Button.inline("↩️ پنل مدیریت", b"admin:home")],
        ]

    def _back_home(self) -> list:
        return [[Button.inline("🎙 پلتفرم STT", b"admin:stt"),
                 Button.inline("↩️ پنل مدیریت", b"admin:home")]]

    # --------------------------------------------------------------- home
    async def show_home(self, event) -> None:
        settings = self.settings
        rows = await self.db.stt_routes_list()
        route = [str(row["provider"]) for row in rows] or list(resolve_route(settings))
        lines = [
            "🎙 پلتفرم STT گمس (Gamas Speech Platform)",
            "",
            f"زبان: {settings.stt_language} | مسیر فعال: {' ← '.join(route) or '—'}",
            "ویژگی‌های درخواست: "
            f"diarization={_onoff(bool(settings.stt_request_diarization))} | "
            f"word_timestamps={_onoff(bool(settings.stt_request_word_timestamps))} | "
            f"smart={_onoff(bool(settings.stt_smart_transcription))}",
            "",
            "برای دیدن وضعیت هر بخش، دکمهٔ مربوط را بزنید.",
        ]
        await self._edit(event, "\n".join(lines), self._home_buttons())

    async def show_policy(self, event) -> None:
        settings = self.settings
        lines = [
            "⚙️ سیاست‌های STT (از environment خوانده می‌شوند)",
            "",
            f"STT_FREE_ONLY = {_onoff(bool(settings.stt_free_only))}",
            f"STT_ALLOW_TRIAL_PROVIDERS = {_onoff(bool(settings.stt_allow_trial_providers))}",
            f"STT_ALLOW_PAID_FALLBACK = {_onoff(bool(settings.stt_allow_paid_fallback))}",
            f"STT_TRIAL_ALLOWLIST = {', '.join(settings.stt_trial_allowlist) or '—'}",
            f"STT_QUOTA_SAFETY_MARGIN = {settings.stt_quota_safety_margin:.0%}",
            f"STT_QUALITY_GATE_ENABLED = {_onoff(bool(settings.stt_quality_gate_enabled))}",
            f"STT_MAX_PROVIDER_FAILOVERS = {settings.stt_max_provider_failovers}",
            f"STT_FALLBACK_ENABLED = {_onoff(bool(settings.stt_fallback_enabled))}",
            "",
            "مسیر و مدل هر گام از پنل «مسیرها» به‌صورت داده‌محور قابل تغییر است.",
        ]
        await self._edit(event, "\n".join(lines), self._back_home())

    # ---------------------------------------------------------- providers
    async def show_providers(self, event) -> None:
        settings_state = await self.db.stt_provider_settings_all()
        lines = ["🏢 ارائه‌دهندگان STT", ""]
        buttons: list = []
        for info in STT_PROVIDER_REGISTRY.values():
            state = settings_state.get(info.provider_slug) or {}
            admin_enabled = bool(state.get("enabled", True))
            configured = bool(self.settings.stt_api_key(info.provider_slug)) or (
                info.provider_slug == "openai_compatible" and bool(self.settings.stt_openai_base_url)
            )
            status = "✅ فعال" if (info.enabled and admin_enabled) else "⏸ غیرفعال"
            mark = _CLASS_MARK.get(info.free_type, "❔")
            lines.append(
                f"{mark} {info.display_name} (`{info.provider_slug}`) — {status}"
                + ("" if configured else " — کلید تنظیم نشده")
            )
            buttons.append(
                [
                    Button.inline(
                        f"📄 {info.display_name}",
                        f"admin:stt:pv:{info.provider_slug}".encode("ascii"),
                    )
                ]
            )
        lines.append("")
        lines.append("وضعیت‌ها: ✅ فعال | ⏸ غیرفعال (registry یا admin)")
        buttons.append([Button.inline("🎙 پلتفرم STT", b"admin:stt")])
        await self._edit(event, "\n".join(lines), buttons)

    async def show_provider_detail(self, event, slug: str) -> None:
        info = stt_registry_info(slug)
        if info is None:
            await event.answer("ارائه‌دهندهٔ ناشناخته.", alert=True)
            return
        state = (await self.db.stt_provider_settings_all()).get(slug) or {}
        model = self.settings.stt_model(slug) or info.default_model
        quotas = await self.db.stt_quota_latest(slug, account_scope="provider")
        lines = [
            f"🏢 {info.display_name} (`{info.provider_slug}`)",
            "",
            f"پروتکل: {info.protocol} | احراز هویت: {info.authentication_type}",
            f"سرویس: {info.service_type} | Base URL: {_fmt(info.base_url)}",
            f"رایگان: {free_class_fa(slug)} ({info.free_type})",
            f"محدودیت رایگان: {info.free_limit or '—'}",
            f"بازنشانی: {info.free_reset or '—'} | انقضای trial: {info.trial_expiration or '—'}",
            f"زبان‌ها: {', '.join(info.language_capabilities) or '—'}",
            f"مدل پیش‌فرض: {model} | مدل‌ها: {', '.join(info.models) or '—'}",
            f"حداکثر فایل: {_fmt_bytes(info.max_file_size)} | "
            f"حداکثر مدت: {_fmt_duration(info.max_audio_duration) if info.max_audio_duration else 'نامشخص'}",
            f"batch={_onoff(info.batch_supported)} | realtime={_onoff(info.realtime_supported)} | "
            f"streaming={_onoff(info.streaming_supported)}",
            f"diarization={_onoff(info.supports_diarization)}"
            + (f" (حداکثر {info.max_speakers} گوینده)" if info.supports_diarization else "")
            + f" | word_timestamps={_onoff(info.supports_word_timestamps)}"
            + f" | confidence={_onoff(info.supports_confidence)}",
            f"واژگان: {_onoff(info.vocabulary_supported)}"
            + (f" (حداکثر {info.vocabulary_max_terms} اصطلاح — {info.vocabulary_parameter})" if info.vocabulary_supported else ""),
            f"persian_batch={_onoff(info.persian_batch)} | تجربی={_onoff(info.experimental)}",
            f"ریت: {info.rate_limits or '—'}",
            f"همزمانی: {info.concurrency} | chunking: {info.chunking_policy}",
            "",
            "حریم خصوصی (فقط مستندات رسمی؛ بدون ادعای HIPAA/GDPR):",
            f"• data_training_policy: {info.data_training_policy}",
            f"• retention_policy: {info.data_retention}",
            f"• data_region: {info.data_region}",
            f"• medical_compliance: {info.medical_compliance}",
            f"• commercial_use: {'بله' if info.commercial_use else 'خیر'}",
            f"• region_restrictions: {info.region_restrictions or '—'}",
            "",
            f"سند رسمی: {info.official_docs_url}",
            f"قیمت: {info.pricing_url}",
            f"آخرین راستی‌آزمایی: {info.last_verified_at} (evidence: {info.evidence})",
            "",
            f"وضعیت admin: {_onoff(bool(state.get('enabled', True)))} | "
            f"billing_state: {_fmt(state.get('billing_state'), 'unknown')}",
        ]
        if quotas:
            top = quotas[0]
            lines.append(
                f"آخرین مشاهدهٔ سهمیه: {top.get('quota_type')} remaining="
                f"{_fmt(top.get('remaining'))} (source: {_fmt(top.get('source'))})"
            )
        else:
            lines.append("آخرین مشاهدهٔ سهمیه: Unknown (هیچ مقداری ثبت نشده است)")
        buttons = [
            [Button.inline("🧪 تست این ارائه‌دهنده", f"admin:stt:tst:{slug}".encode("ascii")),
             Button.inline("🔍 درخواست آزمایشی", f"admin:stt:dry:{slug}".encode("ascii"))],
            [Button.inline(
                "⏸ غیرفعال‌سازی" if bool(state.get("enabled", True)) else "✅ فعال‌سازی",
                f"admin:stt:pen:{slug}".encode("ascii"),
            )],
            [Button.inline("🧾 billing: free", f"admin:stt:pbl:{slug}:free".encode("ascii")),
             Button.inline("🧾 billing: paid", f"admin:stt:pbl:{slug}:paid".encode("ascii")),
             Button.inline("🧾 billing: unknown", f"admin:stt:pbl:{slug}:unknown".encode("ascii"))],
            [Button.inline("🏢 ارائه‌دهندگان", b"admin:stt:pv")],
        ]
        await self._edit(event, "\n".join(lines), buttons)

    async def toggle_provider_enabled(self, event, slug: str) -> None:
        admin_id = int((await event.get_sender()).id)
        state = (await self.db.stt_provider_settings_all()).get(slug) or {}
        new_state = not bool(state.get("enabled", True))
        await self.db.stt_provider_settings_upsert(slug, enabled=new_state, admin_id=admin_id)
        await event.answer("فعال شد." if new_state else "غیرفعال شد.")
        await self.show_provider_detail(event, slug)

    async def set_billing_state(self, event, slug: str, billing: str) -> None:
        if billing not in {"free", "paid", "unknown"}:
            await event.answer("مقدار نامعتبر.", alert=True)
            return
        admin_id = int((await event.get_sender()).id)
        await self.db.stt_provider_settings_upsert(slug, billing_state=billing, admin_id=admin_id)
        await event.answer(f"billing_state = {billing}")
        await self.show_provider_detail(event, slug)

    # ------------------------------------------------------------- models
    async def show_models(self, event, slug: str | None = None) -> None:
        registry = STTModelRegistry(self.db, self.settings)
        lines = ["🧠 مدل‌های STT (رجیستری مدل‌ها)", ""]
        buttons: list = []
        providers = [slug] if slug else list(STT_PROVIDER_REGISTRY)
        for provider in providers:
            info = stt_registry_info(provider)
            if info is None:
                continue
            rows = await registry.cached(provider)
            lines.append(f"▫️ {info.display_name} (`{provider}`)")
            if not rows:
                lines.append("   مدل ثبت‌شده‌ای وجود ندارد.")
            for item in rows:
                badge = "🚫 DEPRECATED" if item.deprecated else (
                    "❌ unavailable" if not item.available else "✅"
                )
                fa = "fa✓" if item.persian_supported else "fa✗"
                lines.append(
                    f"   {badge} `{item.model}` [{fa}] "
                    f"quality={_fmt(item.quality_score)} free={item.free_status} "
                    f"src={item.source}"
                )
                if item.deprecated:
                    replacement = default_stt_model(provider)
                    if replacement and replacement != item.model:
                        lines.append(f"      ↳ جایگزین پیشنهادی: `{replacement}`")
                if item.available and not item.deprecated:
                    buttons.append(
                        [
                            Button.inline(
                                f"🚫 DEPRECATED · {provider}/{item.model}"[:60],
                                f"admin:stt:mdep:{provider}:{item.model}".encode("ascii"),
                            )
                        ]
                    )
            lines.append("")
        lines.append(
            "مدل deprecated حذف نمی‌شود؛ فقط از مسیر خارج می‌شود (spec §49). "
            "تغییرات کاتالوگ زنده فقط با همگام‌سازی صریح اعمال می‌شود."
        )
        buttons.append([Button.inline("🎙 پلتفرم STT", b"admin:stt")])
        await self._edit(event, "\n".join(lines), buttons)

    async def mark_model(self, event, provider: str, model: str) -> None:
        registry = STTModelRegistry(self.db, self.settings)
        info = await registry.resolve(provider, model)
        updated = STTModelInfo(
            provider=info.provider,
            model=info.model,
            display_name=info.display_name,
            status=MODEL_DEPRECATED,
            language_support=info.language_support,
            persian_supported=info.persian_supported,
            feature_support=info.feature_support,
            free_status=info.free_status,
            max_duration_seconds=info.max_duration_seconds,
            max_file_size=info.max_file_size,
            quality_score=info.quality_score,
            deprecated=True,
            deprecation_date=_utc_now()[:10],
            available=info.available,
            source=info.source,
            last_verified=info.last_verified,
        )
        await self.db.stt_models_upsert([updated.to_row()])
        replacement = default_stt_model(provider)
        message = "مدل به عنوان DEPRECATED علامت خورد؛ حذف نشد."
        if replacement and replacement != model:
            message += f" جایگزین پیشنهادی: {replacement}"
        await event.answer(message)
        await self.show_models(event, provider)

    # -------------------------------------------------------------- routes
    def _route_summary_lines(self) -> list[str]:
        settings = self.settings
        policy = [
            f"Free-only: {_onoff(bool(settings.stt_free_only))}",
            f"Trial providers: {_onoff(bool(settings.stt_allow_trial_providers))}",
            f"Paid fallback: {_onoff(bool(settings.stt_allow_paid_fallback))}",
        ]
        return [
            "🗺 مسیر STT — Persian batch transcription",
            "",
            "Primary/Secondary/Tertiary ← ترتیب فهرست پایین",
            " | ".join(policy),
            "",
        ]

    async def show_routes(self, event) -> None:
        rows = await self.db.stt_routes_list()
        lines = self._route_summary_lines()
        buttons: list = []
        if not rows:
            fallback = list(resolve_route(self.settings))
            lines.append(
                "جدول مسیر خالی است؛ مسیر از تنظیمات خوانده می‌شود: "
                + " ← ".join(fallback)
            )
            lines.append("برای ویرایش از پنل، ابتدا مسیر را در پایگاه‌داده ثبت کنید.")
            buttons.append([Button.inline("💾 ثبت مسیر فعلی در پایگاه‌داده", b"admin:stt:rsave")])
            buttons.append(self._back_home()[0])
            await self._edit(event, "\n".join(lines), buttons)
            return
        labels = ["Primary", "Secondary", "Tertiary"] + [""] * 10
        for index, row in enumerate(rows):
            provider = str(row["provider"])
            info = stt_registry_info(provider)
            name = info.display_name if info else provider
            override = str(row.get("model_override") or "").strip()
            mark = "▶️" if int(row.get("enabled", 1)) else "⏸"
            lines.append(
                f"{mark} {labels[index] or f'#{index + 1}'} — {name} (`{provider}`)"
                + (f" | مدل: `{override}`" if override else "")
            )
            buttons.append(
                [
                    Button.inline("▲", f"admin:stt:rmv:{provider}:u".encode("ascii")),
                    Button.inline("▼", f"admin:stt:rmv:{provider}:d".encode("ascii")),
                    Button.inline(
                        "⏸/▶️" if int(row.get("enabled", 1)) else "▶️",
                        f"admin:stt:ren:{provider}".encode("ascii"),
                    ),
                    Button.inline("⚙️ مدل", f"admin:stt:rmd:{provider}".encode("ascii")),
                    Button.inline("✕", f"admin:stt:rdel:{provider}".encode("ascii")),
                ]
            )
        routed = {str(row["provider"]) for row in rows}
        missing = [slug for slug in STT_PROVIDER_REGISTRY if slug not in routed]
        if missing:
            lines.append("")
            lines.append("افزودن به مسیر:")
            for index in range(0, len(missing), 3):
                row = [
                    Button.inline(f"➕ {slug}", f"admin:stt:radd:{slug}".encode("ascii"))
                    for slug in missing[index : index + 3]
                ]
                buttons.append(row)
        buttons.append([Button.inline("🎙 پلتفرم STT", b"admin:stt")])
        await self._edit(event, "\n".join(lines), buttons)

    async def _save_routes(self, rows: list[dict], admin_id: int) -> None:
        await self.db.stt_routes_replace(rows, admin_id=admin_id)

    async def persist_settings_route(self, event) -> None:
        admin_id = int((await event.get_sender()).id)
        route = list(resolve_route(self.settings))
        await self._save_routes(
            [{"provider": name, "position": index, "enabled": True} for index, name in enumerate(route)],
            admin_id,
        )
        await event.answer("مسیر فعلی در پایگاه‌داده ثبت شد.")
        await self.show_routes(event)

    async def route_move(self, event, provider: str, direction: str) -> None:
        admin_id = int((await event.get_sender()).id)
        rows = await self.db.stt_routes_list()
        order = [dict(row) for row in rows]
        index = next((i for i, row in enumerate(order) if row["provider"] == provider), None)
        if index is None:
            await event.answer("این ارائه‌دهنده در مسیر نیست.", alert=True)
            return
        target = index - 1 if direction == "u" else index + 1
        if target < 0 or target >= len(order):
            await event.answer("در انتهای فهرست است.", alert=True)
            return
        order[index], order[target] = order[target], order[index]
        await self._save_routes(
            [
                {
                    "provider": row["provider"],
                    "position": position,
                    "enabled": bool(row.get("enabled", 1)),
                    "model_override": row.get("model_override"),
                }
                for position, row in enumerate(order)
            ],
            admin_id,
        )
        await self.show_routes(event)

    async def route_toggle(self, event, provider: str) -> None:
        admin_id = int((await event.get_sender()).id)
        changed = await self.db.stt_route_toggle(provider, admin_id=admin_id)
        await event.answer("وضعیت گام مسیر عوض شد." if changed else "در مسیر نیست.")
        await self.show_routes(event)

    async def route_delete(self, event, provider: str) -> None:
        admin_id = int((await event.get_sender()).id)
        rows = [row for row in await self.db.stt_routes_list() if row["provider"] != provider]
        await self._save_routes(
            [
                {
                    "provider": row["provider"],
                    "position": position,
                    "enabled": bool(row.get("enabled", 1)),
                    "model_override": row.get("model_override"),
                }
                for position, row in enumerate(rows)
            ],
            admin_id,
        )
        await event.answer("گام از مسیر حذف شد.")
        await self.show_routes(event)

    async def route_add(self, event, provider: str) -> None:
        if provider not in STT_PROVIDER_REGISTRY:
            await event.answer("ارائه‌دهندهٔ ناشناخته.", alert=True)
            return
        admin_id = int((await event.get_sender()).id)
        rows = [dict(row) for row in await self.db.stt_routes_list()]
        if any(row["provider"] == provider for row in rows):
            await event.answer("قبلاً در مسیر هست.", alert=True)
            return
        rows.append({"provider": provider, "position": len(rows), "enabled": True})
        await self._save_routes(
            [
                {
                    "provider": row["provider"],
                    "position": position,
                    "enabled": bool(row.get("enabled", 1)),
                    "model_override": row.get("model_override"),
                }
                for position, row in enumerate(rows)
            ],
            admin_id,
        )
        await self.show_routes(event)

    async def show_route_models(self, event, provider: str) -> None:
        info = stt_registry_info(provider)
        if info is None:
            await event.answer("ارائه‌دهندهٔ ناشناخته.", alert=True)
            return
        models = list(dict.fromkeys(list(info.models) + [m.model for m in static_stt_models(provider)]))
        lines = [
            f"⚙️ مدل گام «{info.display_name}» در مسیر",
            "",
            "مدل انتخاب‌شده فقط برای همین گام اعمال می‌شود؛",
            "«خودکار» مدل پیش‌فرض تنظیمات را نگه می‌دارد.",
        ]
        buttons = [
            [Button.inline("⚙️ خودکار (بدون override)", f"admin:stt:rset:{provider}:-".encode("ascii"))]
        ]
        for model in models:
            buttons.append(
                [Button.inline(f"🎯 {model}"[:60], f"admin:stt:rset:{provider}:{model}".encode("ascii"))]
            )
        buttons.append([Button.inline("↩️ مسیرها", b"admin:stt:rt")])
        await self._edit(event, "\n".join(lines), buttons)

    async def route_set_model(self, event, provider: str, model: str) -> None:
        admin_id = int((await event.get_sender()).id)
        rows = [dict(row) for row in await self.db.stt_routes_list()]
        found = False
        for row in rows:
            if row["provider"] == provider:
                row["model_override"] = None if model == "-" else model
                found = True
        if not found:
            await event.answer("در مسیر نیست.", alert=True)
            return
        await self._save_routes(
            [
                {
                    "provider": row["provider"],
                    "position": position,
                    "enabled": bool(row.get("enabled", 1)),
                    "model_override": row.get("model_override"),
                }
                for position, row in enumerate(rows)
            ],
            admin_id,
        )
        await event.answer("مدل گام مسیر ثبت شد.")
        await self.show_routes(event)

    # -------------------------------------------------------------- quotas
    async def show_quotas(self, event, slug: str | None = None) -> None:
        margin = float(self.settings.stt_quota_safety_margin or 0.0)
        lines = [
            "📊 سهمیه‌های STT",
            "",
            f"STT_QUOTA_SAFETY_MARGIN = {margin:.0%} (از باقی‌ماندهٔ اعلام‌شده کسر می‌شود)",
            "مقادیر Unknown ساخته نمی‌شوند؛ نبود داده ≠ نامحدود.",
            "",
        ]
        buttons: list = []
        providers = [slug] if slug else list(STT_PROVIDER_REGISTRY)
        for provider in providers:
            info = stt_registry_info(provider)
            if info is None:
                continue
            budgets = await self.db.stt_quota_budgets(provider)
            snapshots = await self.db.stt_quota_latest(provider)
            lines.append(f"▫️ {info.display_name} (`{provider}`)")
            if budgets:
                for budget in budgets:
                    limit = budget.get("quota_limit")
                    remaining = budget.get("remaining")
                    used = None
                    if limit is not None and remaining is not None:
                        used = float(limit) - float(remaining)
                    safe = (
                        f"{float(remaining) * (1.0 - margin):.1f}"
                        if remaining is not None
                        else "Unknown"
                    )
                    lines.append(
                        f"   سقف: {budget.get('quota_type')} | limit={_fmt(limit)} | "
                        f"used={_fmt(used)} | remaining={_fmt(remaining)} | safe={safe}"
                    )
                    lines.append(
                        f"   reset={_fmt(budget.get('reset_at'))} | "
                        f"source={_fmt(budget.get('source'))} | scope={budget.get('account_scope')}"
                    )
                    buttons.append(
                        [
                            Button.inline(
                                f"✕ حذف سقف {provider}/{budget.get('quota_type')}"[:60],
                                f"admin:stt:qdel:{provider}:{budget.get('account_scope')}:{budget.get('quota_type')}".encode(
                                    "ascii"
                                ),
                            )
                        ]
                    )
            else:
                lines.append("   سقف (budget): Unknown — سقفی ثبت نشده است.")
            if snapshots:
                for snapshot in snapshots[:3]:
                    lines.append(
                        f"   مشاهده: {snapshot.get('quota_type')} limit={_fmt(snapshot.get('quota_limit'))} "
                        f"used={_fmt(snapshot.get('used'))} remaining={_fmt(snapshot.get('remaining'))}"
                    )
                    lines.append(
                        f"   reset={_fmt(snapshot.get('reset_at'))} | "
                        f"source={_fmt(snapshot.get('source'))} | "
                        f"observed={str(snapshot.get('observed_at') or '')[:16] or 'نامشخص'}"
                    )
            else:
                lines.append("   مشاهدهٔ زنده: Unknown — هیچ هدری ثبت نشده است.")
            buttons.append(
                [
                    Button.inline(
                        f"➕ ثبت سقف {provider}"[:60],
                        f"admin:stt:qset:{provider}".encode("ascii"),
                    )
                ]
            )
            lines.append("")
        buttons.append([Button.inline("🎙 پلتفرم STT", b"admin:stt")])
        await self._edit(event, "\n".join(lines), buttons)

    def budget_prompt(self, provider: str) -> str:
        return (
            f"ثبت سقف سهمیه برای `{provider}`\n\n"
            "قالب (یک خط):\n"
            "scope | quota_type | limit | remaining | reset_at\n\n"
            "مثال:\n"
            "provider | audio_seconds_day | 28800 | 22400 | —\n\n"
            "quota_type یکی از: rpm, rpd, audio_seconds_hour, audio_seconds_day, "
            "minutes_month, credits, concurrent_requests, requests\n"
            "reset_at اختیاری است (ISO یا —). لغو: /cancel"
        )

    async def handle_budget_input(self, event, provider: str, text: str) -> None:
        admin_id = int((await event.get_sender()).id)
        parts = [part.strip() for part in text.split("|")]
        if len(parts) not in {4, 5}:
            await event.reply(self.budget_prompt(provider))
            return
        scope, quota_type, limit_text, remaining_text = parts[:4]
        reset_at = parts[4] if len(parts) == 5 and parts[4] not in {"", "—", "-"} else None
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", scope or "provider") or not re.fullmatch(
            r"[A-Za-z0-9_]{1,40}", quota_type or ""
        ):
            await event.reply(
                "scope و quota_type فقط باید حروف/اعداد انگلیسی باشند؛ دوباره بفرستید.\n\n"
                + self.budget_prompt(provider)
            )
            return
        try:
            limit = float(limit_text)
            remaining = float(remaining_text)
            await self.db.stt_quota_set_budget(
                provider,
                scope or "provider",
                quota_type,
                limit=limit,
                remaining=remaining,
                reset_at=reset_at,
                admin_id=admin_id,
            )
        except (ValueError, TypeError):
            await event.reply("مقدار عددی نامعتبر است؛ دوباره بفرستید.\n\n" + self.budget_prompt(provider))
            return
        except Exception:
            await event.reply("ثبت سقف انجام نشد؛ قالب و مقادیر را بررسی کنید.")
            return
        self.bot._pending_admin_actions.pop(admin_id, None)
        await event.reply("سقف سهمیه ثبت شد.")
        await self.show_quotas(event, provider)

    async def budget_delete(self, event, provider: str, scope: str, quota_type: str) -> None:
        admin_id = int((await event.get_sender()).id)
        deleted = await self.db.stt_quota_budget_delete(
            provider, scope, quota_type, admin_id=admin_id
        )
        await event.answer("سقف حذف شد." if deleted else "سقفی پیدا نشد.")
        await self.show_quotas(event, provider)

    # -------------------------------------------------------------- health
    async def show_health(self, event) -> None:
        lines = [
            "🩺 سلامت ارائه‌دهندگان STT",
            "",
            "حالت پیش‌فرض: READ_ONLY_HEALTH — فقط متادیتا/مدل‌ها/کوئوتا؛ بدون تولید.",
            "تست تولید واقعی فقط از «🧪 تست ارائه‌دهنده» اجرا می‌شود.",
            "",
        ]
        results = await self.bot.provider_health.check_all(force=False)
        rows = [result for result in results if result.service == SERVICE]
        if not rows:
            lines.append("کلید STT ثبت‌شده‌ای برای بررسی وجود ندارد.")
        for result in rows:
            lines.append(
                f"• {result.provider} [{result.masked}] — {result.status_fa} "
                f"(HTTP {_fmt(result.http_status, '—')}, {_fmt(result.latency_ms, '—')} ms, "
                f"{_CHECK_MODE_FA.get(result.check_type, result.check_type)})"
            )
            if result.detail:
                lines.append(f"  جزئیات: {result.detail[:140]}")
        lines.append("")
        lines.append("وضعیت‌های ممکن: HEALTHY | DEGRADED | RATE_LIMITED | QUOTA_EXHAUSTED | "
                     "AUTH_FAILED | MODEL_UNAVAILABLE | LANGUAGE_UNSUPPORTED | BILLING_REQUIRED | DISABLED")
        buttons = [
            [Button.inline("🔄 بررسی مجدد", b"admin:stt:herefresh")],
            [Button.inline("🎙 پلتفرم STT", b"admin:stt")],
        ]
        await self._edit(event, "\n".join(lines), buttons)

    async def refresh_health(self, event) -> None:
        await event.answer("در حال بررسی…")
        await self.show_health(event)

    # ---------------------------------------------------------------- logs
    async def show_logs(self, event, failures_only: bool = False) -> None:
        rows = await self.db.stt_events_list(limit=40, failures_only=failures_only)
        lines = [
            "📜 رویدادهای STT (stt_provider_events)",
            "",
            "رویدادها فقط فرادادهٔ عملیاتی دارند؛ متن ترنسکریپت، صدا و کلید هرگز ثبت نمی‌شوند.",
            "",
        ]
        if not rows:
            lines.append("رویدادی ثبت نشده است.")
        for row in rows:
            created = str(row.get("created_at") or "")[:16].replace("T", " ")
            lines.append(
                f"• {created} | {row.get('event')} | {row.get('provider') or '—'} "
                f"| model={row.get('model') or '—'} | HTTP {_fmt(row.get('http_status'), '—')} "
                f"| {_fmt(row.get('latency_ms'), '—')} ms | {row.get('error_category') or '—'}"
            )
        buttons = [
            [
                Button.inline("فقط خطاها" if not failures_only else "همه", b"admin:stt:lgerr"
                              if not failures_only else b"admin:stt:lg"),
            ],
            [Button.inline("🎙 پلتفرم STT", b"admin:stt")],
        ]
        await self._edit(event, "\n".join(lines), buttons)

    # ----------------------------------------------------------- test tool
    async def show_test(self, event, slug: str | None = None) -> None:
        if slug is None:
            lines = ["🧪 تست ارائه‌دهندهٔ STT", "", "ارائه‌دهنده را انتخاب کنید:"]
            buttons = []
            for info in STT_PROVIDER_REGISTRY.values():
                buttons.append(
                    [
                        Button.inline(
                            f"{info.display_name}",
                            f"admin:stt:tst:{info.provider_slug}".encode("ascii"),
                        )
                    ]
                )
            buttons.append([Button.inline("🎙 پلتفرم STT", b"admin:stt")])
            await self._edit(event, "\n".join(lines), buttons)
            return
        info = stt_registry_info(slug)
        if info is None:
            await event.answer("ارائه‌دهندهٔ ناشناخته.", alert=True)
            return
        samples = _samples_dir()
        lines = [
            f"🧪 تست {info.display_name}",
            "",
            "حالت‌های تست (spec §39):",
            "۱) فقط متادیتا — بدون ارسال صدا و بدون مصرف سهمیه.",
            "۲–۴) نمونهٔ صوتی — از فایل‌های نمونهٔ محلی ارسال می‌شود؛",
            f"    مسیر نمونه‌ها: {samples}",
            "    (اگر فایل نصب نباشد، چیزی ارسال نمی‌شود.)",
            "",
            "محتوای صدا و متن ترنسکریپت هرگز ثبت یا نمایش داده نمی‌شود.",
            "فایل‌های موقت پس از تست حذف می‌شوند.",
        ]
        buttons = []
        for mode, (label, filename) in TEST_MODES.items():
            suffix = "" if filename is None or (samples / filename).is_file() else " (نصب نشده)"
            buttons.append(
                [
                    Button.inline(
                        f"{label}{suffix}"[:60],
                        f"admin:stt:trun:{slug}:{mode}".encode("ascii"),
                    )
                ]
            )
        buttons.append([Button.inline("↩️", f"admin:stt:pv:{slug}".encode("ascii"))])
        await self._edit(event, "\n".join(lines), buttons)

    def _test_report(self, *, provider: str, model: str, status: str, http, latency_ms,
                     language, confidence, words, duration, quota_line, failure: str | None) -> str:
        info = stt_registry_info(provider)
        lines = [
            f"نتیجهٔ تست — {info.display_name if info else provider}",
            "",
            f"STATUS: {status}",
            f"HTTP: {_fmt(http, '—')}",
            f"LATENCY: {_fmt(latency_ms, '—')} ms",
            f"PROVIDER: {provider}",
            f"MODEL: {_fmt(model, 'خودکار')}",
            f"LANGUAGE: {_fmt(language)}",
            f"DURATION: {_fmt(duration)}",
            f"CONFIDENCE: {_fmt(confidence, 'provider-reported / نامشخص')}",
            f"WORDS: {_fmt(words, '—')}",
            f"FREE STATUS: {free_class_fa(provider)}",
            f"QUOTA STATUS: {quota_line}",
        ]
        if failure:
            lines.append(f"FAILURE: {failure}")
        return "\n".join(lines)

    async def _quota_line(self, provider: str) -> str:
        snapshots = await self.db.stt_quota_latest(provider, account_scope="provider")
        if not snapshots:
            return "Unknown (بدون مشاهده)"
        top = snapshots[0]
        return (
            f"{top.get('quota_type')} remaining={_fmt(top.get('remaining'))} "
            f"source={_fmt(top.get('source'))}"
        )

    async def run_test(self, event, slug: str, mode: str) -> None:
        info = stt_registry_info(slug)
        if info is None or mode not in TEST_MODES:
            await event.answer("درخواست تست نامعتبر است.", alert=True)
            return
        admin_id = int((await event.get_sender()).id)
        _label, filename = TEST_MODES[mode]
        model = self.settings.stt_model(slug) or info.default_model
        if filename is None:
            await event.answer("در حال بررسی متادیتا…")
            results = await self.bot.provider_health.check(SERVICE, slug, force=True)
            result = results[0] if results else None
            report = self._test_report(
                provider=slug,
                model=model,
                status=result.status_fa if result else "not_configured",
                http=result.http_status if result else None,
                latency_ms=result.latency_ms if result else None,
                language="—",
                confidence="—",
                words="—",
                duration="—",
                quota_line=await self._quota_line(slug),
                failure=(result.detail if result and result.status not in {"healthy", "ok"} else None),
            )
            report += f"\n\nCHECK: {_CHECK_MODE_FA.get(getattr(result, 'check_type', 'read_only'), 'read_only')}"
            await self.bot._edit_callback(
                event,
                report,
                [[Button.inline("↩️ تست", f"admin:stt:tst:{slug}".encode("ascii"))]],
            )
            return
        samples = _samples_dir()
        audio = samples / filename
        if not audio.is_file():
            await self.bot._edit_callback(
                event,
                "فایل نمونه روی این سرور نصب نشده است؛ هیچ صدایی ارسال نشد.\n"
                f"مسیر مورد انتظار: {audio}\n"
                "نمونه‌ها را مطابق tests/fixtures/stt/README.md اضافه کنید.",
                [[Button.inline("↩️ تست", f"admin:stt:tst:{slug}".encode("ascii"))]],
            )
            return
        await event.answer("در حال اجرای تست تولید واقعی…")
        from dataclasses import replace as _replace

        from .stt import transcribe

        run_settings = _replace(
            self.settings,
            stt_primary=slug,
            stt_default_route=slug,
            stt_fallback_enabled=False,
        )
        started = datetime.now(timezone.utc)
        try:
            transcript = await transcribe(
                audio, run_settings, credentials=self.bot.credential_manager,
                job_id=f"admin-test-{admin_id}",
            )
            elapsed = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            words = len((transcript.text or "").split())
            report = self._test_report(
                provider=slug,
                model=model,
                status="READY",
                http=200,
                latency_ms=elapsed,
                language=getattr(transcript, "language", None) or run_settings.stt_language,
                confidence=transcript.confidence,
                words=words,
                duration=_fmt_duration(get_audio_duration_seconds(audio)),
                quota_line=await self._quota_line(slug),
                failure=None,
            )
        except Exception as exc:  # noqa: BLE001 — report card must never leak internals
            elapsed = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            category = getattr(exc, "category", None) or type(exc).__name__
            report = self._test_report(
                provider=slug,
                model=model,
                status="FAILED",
                http=getattr(exc, "status", None),
                latency_ms=elapsed,
                language=run_settings.stt_language,
                confidence="—",
                words="—",
                duration=_fmt_duration(get_audio_duration_seconds(audio)),
                quota_line=await self._quota_line(slug),
                failure=str(category),
            )
        report += "\n\nCHECK: GENERATION_TEST (تولید واقعی انجام شد)"
        report += "\nمحتوای صدا/متن ثبت نشد؛ فایل نمونه نگه‌داشته می‌شود (فایل موقتی ساخته نشد)."
        await self.bot._edit_callback(
            event,
            report,
            [[Button.inline("↩️ تست", f"admin:stt:tst:{slug}".encode("ascii"))]],
        )

    # ------------------------------------------------------------- dry-run
    async def show_dry(self, event, slug: str | None = None) -> None:
        if slug is None:
            lines = ["🔍 درخواست آزمایشی (Dry-run)", "",
                     "بدون ارسال صدا و بدون نمایش رمز؛ فقط ساختار درخواست.", "",
                     "ارائه‌دهنده را انتخاب کنید:"]
            buttons = []
            for info in STT_PROVIDER_REGISTRY.values():
                buttons.append(
                    [
                        Button.inline(
                            f"{info.display_name}",
                            f"admin:stt:dry:{info.provider_slug}".encode("ascii"),
                        )
                    ]
                )
            buttons.append([Button.inline("🎙 پلتفرم STT", b"admin:stt")])
            await self._edit(event, "\n".join(lines), buttons)
            return
        info = stt_registry_info(slug)
        if info is None:
            await event.answer("ارائه‌دهندهٔ ناشناخته.", alert=True)
            return
        with tempfile.TemporaryDirectory(prefix="gamas-stt-dry-") as directory:
            audio = _placeholder_wav(directory)
            preview = self._build_preview(slug, audio)
        lines = self._preview_lines(info, preview)
        await self._edit(
            event,
            "\n".join(lines),
            [[Button.inline("↩️", b"admin:stt:dry")],
             [Button.inline("🎙 پلتفرم STT", b"admin:stt")]],
        )

    def _build_preview(self, slug: str, audio: Path) -> RequestPreview:
        """Provider-native request preview; legacy engines reuse their builders."""
        from .stt import deepgram_params, speechmatics_config

        settings = self.settings
        options_model = settings.stt_model(slug) or ""
        if slug == "speechmatics":
            config = speechmatics_config(settings)
            fields: list[tuple[str, str]] = []
            _flatten("", config, fields)
            return RequestPreview(
                provider=slug,
                model=options_model or settings.speechmatics_operating_point,
                endpoint=(settings.speechmatics_base_url or "").rstrip("/") + "/jobs",
                method="POST",
                protocol="batch_rest",
                headers_redacted=(("Authorization", "<redacted>"),),
                audio_bytes=audio.stat().st_size,
                audio_duration_seconds=get_audio_duration_seconds(audio),
                language=config.get("transcription_config", {}).get("language"),
                requested_features=("vocabulary",) if settings.speechmatics_additional_vocab else (),
                form_fields=tuple(fields),
                payload_shape="multipart: transcription JSON + audio file (whole-file)",
            )
        if slug == "deepgram":
            params = deepgram_params(settings)
            return RequestPreview(
                provider=slug,
                model=settings.deepgram_model,
                endpoint="https://api.deepgram.com/v1/listen",
                method="POST",
                protocol="batch_rest",
                headers_redacted=(("Authorization", "<redacted>"),),
                audio_bytes=audio.stat().st_size,
                audio_duration_seconds=get_audio_duration_seconds(audio),
                language=params.get("language"),
                requested_features=("smart_format", "punctuate"),
                form_fields=tuple(params.items()),
                payload_shape="raw audio body; query parameters above (whole-file)",
            )
        if slug == "openai_compatible":
            fields = [
                ("model", settings.stt_openai_model),
                ("response_format", "json"),
            ]
            if settings.stt_language and settings.stt_language not in {"auto"}:
                fields.append(("language", settings.stt_language))
            return RequestPreview(
                provider=slug,
                model=settings.stt_openai_model,
                endpoint=(settings.stt_openai_base_url or "").rstrip("/") + "/audio/transcriptions",
                method="POST",
                protocol="openai_audio_transcriptions",
                headers_redacted=(("Authorization", "<redacted>"),),
                audio_bytes=audio.stat().st_size,
                audio_duration_seconds=get_audio_duration_seconds(audio),
                language=settings.stt_language,
                requested_features=(),
                form_fields=tuple(fields),
                payload_shape="multipart: file, model, language?, response_format",
            )
        return NativeSTTAdapter(slug).build_preview(settings, audio)

    def _preview_lines(self, info: STTProviderInfo, preview: RequestPreview) -> list[str]:
        lines = [
            f"🔍 درخواست آزمایشی — {info.display_name}",
            "(ساختار درخواست؛ نه صدا، نه رمز، نه بدنهٔ multipart)",
            "",
            f"provider = {preview.provider}",
            f"model = {_fmt(preview.model, 'خودکار')}",
            f"endpoint = {preview.endpoint}",
            f"method = {preview.method}",
            f"protocol = {preview.protocol}",
            "headers = " + ", ".join(f"{name}: {value}" for name, value in preview.headers_redacted),
            f"file = <audio file: {_fmt_bytes(preview.audio_bytes)} (placeholder؛ اندازهٔ واقعی وابسته به شغل است)>",
            f"duration = {_fmt_duration(preview.audio_duration_seconds) if preview.audio_duration_seconds else 'وابسته به فایل شغل'}",
            f"language = {_fmt(preview.language, '(auto/omit)')}",
            "requested features = " + (", ".join(preview.requested_features) or "none"),
            "",
            "form fields / parameters:",
        ]
        if preview.form_fields:
            lines.extend(f"  {name} = {value}" for name, value in preview.form_fields)
        else:
            lines.append("  (none)")
        lines.append("")
        lines.append(f"payload structure = {preview.payload_shape}")
        return lines

    # ---------------------------------------------------------- benchmarks
    async def show_benchmarks(self, event) -> None:
        lines = [
            "📏 معیارهای STT (Benchmarks)",
            "",
            "معیارها offline و تکرارپذیرند؛ هیچ کلید تولیدی در CI لازم نیست.",
            "دستور اجرا:",
            "  python -m scripts.benchmark_stt --plan sample.wav",
            "  python -m scripts.benchmark_stt sample.wav --terms terms.txt --output out.csv",
            "",
            "امتیاز نهایی (spec §58): ۴۰٪ دقت فارسی، ۲۰٪ واژگان تخصصی،",
            "۱۰٪ اعداد، ۱۰٪ نگارش، ۱۰٪ پایداری، ۵٪ تأخیر، ۵٪ بهرهٔ سهمیه.",
            "«رایگان بودن» هرگز امتیاز کیفیت را بالا نمی‌برد.",
            "",
            "دسته‌بندی نمونه‌ها (spec §57):",
        ]
        lines.extend(f"• {profile}" for profile in BENCHMARK_PROFILES)
        lines.append("")
        lines.append("نتایج واقعی فقط پس از اجرای نمونه‌های فارسی گمس ثبت می‌شوند؛")
        lines.append("تا آن زمان هیچ ارائه‌دهنده‌ای «بهترین» اعلام نمی‌شود.")
        await self._edit(event, "\n".join(lines), self._back_home())


async def handle_stt_callback(bot, event, data: str) -> None:
    """Dispatch for everything under the ``admin:stt`` namespace."""
    ui = bot.stt_panels
    parts = data.split(":")
    action = parts[2] if len(parts) > 2 else ""
    arg1 = parts[3] if len(parts) > 3 else None
    arg2 = parts[4] if len(parts) > 4 else None
    arg3 = parts[5] if len(parts) > 5 else None

    if data == "admin:stt":
        await ui.show_home(event)
    elif action == "pol":
        await ui.show_policy(event)
    elif action == "pv" and arg1:
        await ui.show_provider_detail(event, arg1)
    elif action == "pv":
        await ui.show_providers(event)
    elif action == "pen" and arg1:
        await ui.toggle_provider_enabled(event, arg1)
    elif action == "pbl" and arg1 and arg2:
        await ui.set_billing_state(event, arg1, arg2)
    elif action == "md" and arg1:
        await ui.show_models(event, arg1)
    elif action == "md":
        await ui.show_models(event)
    elif action == "mdep" and arg1 and arg2:
        await ui.mark_model(event, arg1, arg2)
    elif action == "rt":
        await ui.show_routes(event)
    elif action == "rsave":
        await ui.persist_settings_route(event)
    elif action == "rmv" and arg1 and arg2:
        await ui.route_move(event, arg1, arg2)
    elif action == "ren" and arg1:
        await ui.route_toggle(event, arg1)
    elif action == "rdel" and arg1:
        await ui.route_delete(event, arg1)
    elif action == "radd" and arg1:
        await ui.route_add(event, arg1)
    elif action == "rmd" and arg1:
        await ui.show_route_models(event, arg1)
    elif action == "rset" and arg1 and arg2 is not None:
        await ui.route_set_model(event, arg1, arg2)
    elif action == "qt" and arg1:
        await ui.show_quotas(event, arg1)
    elif action == "qt":
        await ui.show_quotas(event)
    elif action == "qset" and arg1:
        bot._pending_admin_actions[int((await event.get_sender()).id)] = f"stt_budget:{arg1}"
        await bot._edit_callback(
            event,
            ui.budget_prompt(arg1),
            [[Button.inline("لغو", b"admin:stt:qt")]],
        )
    elif action == "qdel" and arg1 and arg2 and arg3:
        await ui.budget_delete(event, arg1, arg2, arg3)
    elif action == "he":
        await ui.show_health(event)
    elif action == "herefresh":
        await ui.refresh_health(event)
    elif action == "lgerr":
        await ui.show_logs(event, failures_only=True)
    elif action == "lg":
        await ui.show_logs(event)
    elif action == "tst" and arg1:
        await ui.show_test(event, arg1)
    elif action == "tst":
        await ui.show_test(event)
    elif action == "trun" and arg1 and arg2:
        await ui.run_test(event, arg1, arg2)
    elif action == "dry" and arg1:
        await ui.show_dry(event, arg1)
    elif action == "dry":
        await ui.show_dry(event)
    elif action == "bm":
        await ui.show_benchmarks(event)
    else:
        await event.answer("دکمهٔ STT نامعتبر.", alert=True)
