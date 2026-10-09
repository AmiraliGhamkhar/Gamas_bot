"""Telegram admin panels for the AI Provider Platform.

UI-facing companion of ``gamas_bot.ai.*``: provider cards, key wizard,
routing, usage, model catalog, logs and dry-run request previews. All text is
Persian to match the existing admin panel; technical ids stay ASCII. Nothing
here ever renders raw keys, prompts or transcripts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import aiohttp
from telethon import Button

from .ai.adapters import RequestContext, adapter_for
from .ai.models import (
    ModelCapabilities,
    ModelInfo,
    default_model_for,
    fallback_free_model,
    static_models,
)
from .ai.profiles import profile_for
from .ai.registry import (
    PROVIDER_REGISTRY,
    ProviderClass,
    free_only_generation_allowed,
    registry_info,
)
from .ai.sync import sync_provider_catalog
from .ai.tokens import estimate_tokens
from .ai.usage import sanitize_text
from .database import utc_now
from .provider_health import cooldown_remaining_seconds

SERVICE = "notes"
TASK = "chunk_structuring"

#: Extra-pass override columns and their Persian labels (spec §26).
_PASS_COLUMNS = {
    "outline_enabled": "طرح کلی",
    "repair_enabled": "تعمیر",
    "final_compile_enabled": "تلفیق نهایی",
}

_CLASS_FA = {
    ProviderClass.PERMANENT_FREE: "رایگان دائمی",
    ProviderClass.FREE_PLAN: "پلن رایگان",
    ProviderClass.PROMOTIONAL_FREE: "رایگان تبلیغاتی (موقت)",
    ProviderClass.TRIAL_ONLY: "فقط آزمایشی (trial)",
    ProviderClass.PAID_ONLY: "فقط پولی",
    ProviderClass.REGION_RESTRICTED: "محدودیت منطقه",
    ProviderClass.UNAVAILABLE: "در دسترس نیست",
    ProviderClass.ACCOUNT_UNVERIFIED: "استحقاق حساب تأییدنشده",
}


def _provider_mark(slug: str, settings_row: dict | None) -> str:
    info = registry_info(slug)
    if settings_row and not settings_row.get("enabled", 1):
        return "⛔"
    if free_only_generation_allowed(slug):
        return "🟢"
    if info.classification is ProviderClass.TRIAL_ONLY:
        return "🟡"
    if info.classification is ProviderClass.REGION_RESTRICTED:
        return "🟠"
    return "🔴"


def _fmt_int(value) -> str:
    try:
        return f"{int(value):,}".replace(",", "/")
    except Exception:
        return str(value)


def _requires_paid_billing(row: dict) -> bool:
    """Read the ``requires_paid_billing`` capability from a catalog row.

    The column is a JSON blob, so the panel degrades to "not billable" when a
    row predates the capability or carries malformed JSON — never to a guess.
    """
    caps = row.get("capabilities") or row.get("capabilities_json") or {}
    if isinstance(caps, str):
        import json as _json

        try:
            caps = _json.loads(caps)
        except ValueError:
            return False
    return bool((caps or {}).get("requires_paid_billing"))


class AIPanels:
    """Thin UI layer bound to one bot instance (kept out of bot.py for size)."""

    def __init__(self, bot) -> None:
        self.bot = bot
        # Deep-link payload -> admin id (key test confirmation after wizard).
        self._pending_key_test: dict[str, dict] = {}

    # ------------------------------------------------------------------ UI
    async def show_platform_home(self, event) -> None:
        db = self.bot.db
        metrics = await db.ai_usage_metrics(days=1)
        latency_p95 = await db.ai_usage_latency_p95(days=1)
        settings = self.bot.settings
        lines = [
            "🤖 پلتفرم ارائه‌دهنده‌های هوش مصنوعی",
            "",
            f"درخواست‌های امروز: {_fmt_int(metrics['requests'])}"
            f" (موفق {_fmt_int(metrics['successes'])}, "
            f"429/quota {_fmt_int(metrics['rate_limited'])}, "
            f"fallback {_fmt_int(metrics['fallbacks'])})",
            f"تأخیر p95: {_fmt_int(latency_p95)}ms" if latency_p95 else "تأخیر p95: —",
            "",
            f"حالت FREE_ONLY: {'روشن ✅' if settings.ai_free_only else 'خاموش ⛔'}"
            f" | fallback پولی: {'روشن ⚠️' if settings.ai_allow_paid_fallback else 'خاموش ✅'}",
            f"موتور مسیریاب: {'فعال' if settings.ai_routing_enabled else 'غیرفعال (legacy)'}",
            "",
            "تغییر حالت FREE_ONLY/fallback فقط از طریق متغیرهای محیطی سرور انجام می‌شود.",
        ]
        buttons = [
            [
                Button.inline("📡 ارائه‌دهنده‌ها", b"admin:ai:pv"),
                Button.inline("🧭 مسیرها", b"admin:ai:rt"),
            ],
            [
                Button.inline("📊 مصرف", b"admin:ai:us"),
                Button.inline("🧬 مدل‌ها", b"admin:ai:md"),
            ],
            [
                Button.inline("🧾 لاگ‌ها", b"admin:ai:lg"),
                Button.inline("🧪 Dry-run", b"admin:ai:dry"),
            ],
            [
                Button.inline("📈 نمای مسیر و بودجه", b"admin:ai:plan"),
                Button.inline("🏆 ارزیابی", b"admin:ai:bm"),
            ],
            [
                Button.inline("🩺 سلامت", b"admin:ai:he"),
                Button.inline("⚙️ تنظیمات", b"admin:ai:st"),
            ],
            [
                Button.inline("🔑 کلیدها (قدیمی)", b"admin:credentials"),
                Button.inline("↩️ پنل مدیریت", b"admin:home"),
            ],
        ]
        await self.bot._edit_callback(event, "\n".join(lines), buttons)

    async def show_providers(self, event) -> None:
        db = self.bot.db
        rows = await db.ai_provider_settings_all()
        lines = ["📡 ارائه‌دهنده‌های تولید جزوه", ""]
        buttons: list[list] = []
        row_pairs: list[list] = []
        for slug in PROVIDER_REGISTRY:
            info = registry_info(slug)
            if SERVICE not in info.supported_services:
                continue
            stored = rows.get(slug) or {}
            mark = _provider_mark(slug, stored)
            row_pairs.append(
                [
                    Button.inline(
                        f"{mark} {info.display_name}", f"admin:ai:pv:{slug}".encode("ascii")
                    )
                ]
            )
        for index in range(0, len(row_pairs), 2):
            buttons.append(
                row_pairs[index] + (row_pairs[index + 1] if index + 1 < len(row_pairs) else [])
            )
        lines.append("🟢 رایگان | 🟡 trial | 🟠 محلودیت منطقه | 🔴 پولی | ⛔ غیرفعال")
        lines.append("برای مدیریت کلید، مسیر، سیاست و تست هر ارائه‌دهنده روی نام آن بزنید.")
        buttons.append([Button.inline("↩️ پلتفرم AI", b"admin:ai")])
        await self.bot._edit_callback(event, "\n".join(lines), buttons)

    async def show_provider_detail(self, event, slug: str) -> None:
        if slug not in PROVIDER_REGISTRY:
            await event.answer("ارائه‌دهندهٔ نامعتبر.", alert=True)
            return
        db = self.bot.db
        info = registry_info(slug)
        stored = (await db.ai_provider_settings_get(slug)) or {}
        profile = profile_for(slug)
        keys = await self.bot.credential_manager.list_summaries()
        key_rows = [
            item for item in keys
            if item["service"] == SERVICE
            and (item["provider"] == slug or _canonical_of(item) == slug)
        ]
        enabled = stored.get("enabled", 1)
        unlocked = bool(stored.get("experimental_unlocked"))
        free_blocked = bool(stored.get("free_only_blocked"))
        quotas = await db.ai_quota_latest(slug)
        quota_lines: list[str] = []
        for row in quotas[:3]:
            quota_lines.append(
                f"• {row['window']}: {row['remaining'] or '—'}"
                + (f" | بازنشانی: {row['reset_at']}" if row.get("reset_at") else "")
                + f" | {str(row['observed_at'])[:16]}"
                + (f" ({row['source']})" if row.get("source") not in {None, "headers"} else "")
            )
        if slug == "openrouter":
            # The local free-request ledger (spec §12): OpenRouter free models
            # are capped per UTC day; the live counter comes from the
            # key-endpoint probe (کاوش سهمیه), the reset is 00:00 UTC.
            used = await db.ai_usage_today_count("openrouter")
            live = await db.ai_quota_live_remaining("openrouter")
            limit = profile.daily_request_limit or 50
            remaining = live if live is not None else max(0, limit - used)
            quota_lines.append(
                f"• حسابگر روزانه: {used} مصرف‌شده | {remaining} باقی‌مانده"
                f" (سقف پیش‌فرض {limit}) | بازنشانی ۰۰:۰۰ UTC"
            )
        # Non-token metering (spec §17): Cloudflare bills Neurons and never
        # returns them, so the daily inclusion is guarded from an estimate.
        unit_lines: list[str] = []
        if profile.metering_unit:
            budget = await self.bot.provider_router._unit_budget(slug, profile)
            try:
                used_units = await db.ai_usage_today_units(slug)
            except Exception:
                used_units = 0
            unit_lines.append(
                f"• {profile.metering_unit}: برآورد امروز {_fmt_int(used_units)}"
                + (f" از {_fmt_int(budget)}" if budget else "")
                + " (برآورد محافظه‌کارانه)"
            )
        # Catalog-derived plan view (spec §10/§12): what the last sync saw.
        catalog_lines: list[str] = []
        try:
            catalog = await db.ai_models_list(slug, include_unavailable=False)
        except Exception:
            catalog = []
        if catalog:
            free_models = [
                row
                for row in catalog
                if str(row.get("free_status")) in {"free_permanent", "free_plan"}
            ]
            verified = str(catalog[0].get("source_last_verified_at") or "")[:16]
            catalog_lines.append(
                f"• کاتالوگ: {len(catalog)} مدل | رایگانِ احراز‌شده: {len(free_models)}"
            )
            if free_models:
                catalog_lines.append(f"• مدل رایگان فعلی: {free_models[0]['model']}")
                # OpenRouter/Nara IDs carry the upstream provider as a prefix.
                upstream = str(free_models[0]["model"]).split("/", 1)[0]
                if "/" in str(free_models[0]["model"]) and upstream != free_models[0]["model"]:
                    catalog_lines.append(f"• ارائه‌دهندهٔ پشت مدل: {upstream}")
            if verified:
                catalog_lines.append(f"• آخرین تأیید کاتالوگ: {verified}")
        # Extra-pass policy with its origin (spec §26). Computed for *this*
        # provider, independently of which provider currently leads the route,
        # so the panel never shows another provider's policy.
        pass_lines: list[str] = []
        for column, fa in (
            ("outline_enabled", "طرح کلی"),
            ("repair_enabled", "تعمیر"),
            ("final_compile_enabled", "تلفیق نهایی"),
        ):
            default = getattr(profile, column, True)
            override = stored.get(column)
            enabled = default if override is None else bool(override)
            pass_lines.append(
                f"• {fa}: {'روشن ✅' if enabled else 'خاموش ❌'}"
                f" ({'تنظیم مدیر' if override is not None else 'پیش‌فرض ارائه‌دهنده'})"
            )
        lines = [
            f"{info.display_name}",
            "",
            f"دسته: {_CLASS_FA.get(info.classification, 'سازگار با OpenAI')}",
            f"پلن رایگان: {info.free_tier_policy}",
            f"سیاست استفادهٔ داده: {info.data_use_policy}",
            f"پروتکل: {info.protocol}"
            + (f" | محدودیت منطقه: {info.region_restriction}" if info.region_restriction else ""),
            f"مستندات: {info.docs_url}",
            f"قیمت‌ها: {info.pricing_url}",
            "",
            f"وضعیت: {'فعال ✅' if enabled else 'غیرفعال ⛔'}"
            + (" | قفل آزمایشی: باز 🔓" if unlocked else ""),
            f"بودجهٔ چانک: {_fmt_int(profile.chunk_token_budget)} توکن"
            f" | حداکثر خروجی: {_fmt_int(profile.max_output_tokens)} توکن",
            f"کلیدهای ثبت‌شده: {len(key_rows)}",
            "(وضعیت «آخرین بررسی» ثابت است تا زمانی که تأیید سند رسمی بروز شود.)",
        ]
        if quota_lines:
            lines.append("")
            lines.append("سهمیهٔ رصدشده (آخرین تصویر):")
            lines.extend(quota_lines)
        if unit_lines:
            lines.append("")
            lines.append(f"مصرف برآوردی {profile.metering_unit}:")
            lines.extend(unit_lines)
        if catalog_lines:
            lines.append("")
            lines.append("کاتالوگ زنده (spec §10/§12):")
            lines.extend(catalog_lines)
        if pass_lines:
            lines.append("")
            lines.append("فراخوان‌های اضافی (هر کدام سهمیه مصرف می‌کنند):")
            lines.extend(pass_lines)
        if info.requires_account_verification:
            attested = bool(stored.get("account_entitlement_attested_at")) and (
                stored.get("account_entitlement_attested_by_admin_id") is not None
            )
            lines.append("")
            lines.append("⚠️ استحقاق حساب:")
            if attested:
                lines.append(
                    f"• تأییدشده توسط مدیر {stored.get('account_entitlement_attested_by_admin_id')}"
                    f" در {str(stored.get('account_entitlement_attested_at'))[:16]}"
                )
                lines.append("• این ارائه‌دهنده در مسیر FREE_ONLY شرکت می‌کند.")
            else:
                lines.append("• مستندات این ارائه‌دهنده استحقاق رایگان حساب را اثبات نمی‌کند.")
                lines.append("• تا زمان تأیید مدیر، در FREE_ONLY مسیردهی نمی‌شود.")
        buttons = [
            [
                Button.inline(
                    "⛔ غیرفعال‌سازی" if enabled else "✅ فعال‌سازی",
                    f"admin:ai:ptgl:{slug}".encode("ascii"),
                ),
                Button.inline(
                    "🚫 بلاک رایگان‌سازی" if not free_blocked else "🔓 رفع بلاک",
                    f"admin:ai:pblk:{slug}".encode("ascii"),
                ),
            ],
        ]
        if info.requires_explicit_enable or info.experimental_only:
            buttons.append(
                [
                    Button.inline(
                        "🔓 بازکردن قفل آزمایشی" if not unlocked else "🔒 قفل آزمایشی",
                        f"admin:ai:pxun:{slug}".encode("ascii"),
                    )
                ]
            )
        if info.requires_account_verification:
            attested = bool(stored.get("account_entitlement_attested_at")) and (
                stored.get("account_entitlement_attested_by_admin_id") is not None
            )
            buttons.append(
                [
                    Button.inline(
                        "🧾 لغو تأیید استحقاق" if attested else "🧾 تأیید استحقاق حساب",
                        f"admin:ai:patt:{slug}".encode("ascii"),
                    )
                ]
            )
        # Extra-pass overrides (spec §26): one row of three toggles.
        pass_row: list = []
        for column, fa in (
            ("outline_enabled", "طرح‌کلی"),
            ("repair_enabled", "تعمیر"),
            ("final_compile_enabled", "تلفیق"),
        ):
            default = getattr(profile, column, True)
            override = stored.get(column)
            enabled = default if override is None else bool(override)
            mark = "✅" if enabled else "❌"
            pass_row.append(
                Button.inline(
                    f"{mark} {fa}",
                    f"admin:ai:ppass:{column}:{slug}".encode("ascii"),
                )
            )
        buttons.append(pass_row)
        buttons.extend(
            [
                [
                    Button.inline("🔑 کلیدها", f"admin:ai:ky:{slug}".encode("ascii")),
                    Button.inline("🧬 مدل‌ها", f"admin:ai:md:{slug}".encode("ascii")),
                ],
                [
                    Button.inline("🧪 Dry-run", f"admin:ai:dry:{slug}".encode("ascii")),
                    Button.inline("🔄 همگام‌سازی مدل‌ها", f"admin:ai:mdsync:{slug}".encode("ascii")),
                ],
                [
                    Button.inline("📡 کاوش سهمیه", f"admin:ai:pqprobe:{slug}".encode("ascii")),
                    Button.inline("🧾 لاگ‌ها", f"admin:ai:lg:{slug}".encode("ascii")),
                ],
                [
                    Button.inline("↩️ ارائه‌دهنده‌ها", b"admin:ai:pv"),
                ],
            ]
        )
        await self.bot._edit_callback(event, "\n".join(lines), buttons)

    async def _toggle_provider(self, event, slug: str, field: str, label: str) -> None:
        if slug not in PROVIDER_REGISTRY:
            await event.answer("ارائه‌دهندهٔ نامعتبر.", alert=True)
            return
        db = self.bot.db
        stored = (await db.ai_provider_settings_get(slug)) or {}
        current = bool(stored.get(field, 0 if field != "enabled" else 1))
        new_value = 0 if current else 1
        admin_id = int((await event.get_sender()).id)
        await db.ai_provider_settings_upsert(
            slug, admin_id=admin_id, **{field: new_value}
        )
        await db.add_audit_entry(
            admin_id=admin_id,
            action=f"ai_provider_{field}",
            target_type="ai_provider",
            target_id=slug,
            details={"value": new_value},
        )
        await event.answer(f"{label} {'روشن' if new_value else 'خاموش'} شد.")
        await self.show_provider_detail(event, slug)

    async def _toggle_account_entitlement(self, event, slug: str) -> None:
        """Attest or revoke this deployment's free-account entitlement.

        Only meaningful for providers whose free eligibility is a property of
        the account rather than of published documentation. The attestation is
        an administrator statement, is audited, and is revocable; it does not
        relax the per-key billing attestation or the model-level gates.
        """
        if slug not in PROVIDER_REGISTRY:
            await event.answer("ارائه‌دهندهٔ نامعتبر.", alert=True)
            return
        if not registry_info(slug).requires_account_verification:
            await event.answer("این ارائه‌دهنده نیازی به تأیید حساب ندارد.", alert=True)
            return
        db = self.bot.db
        stored = (await db.ai_provider_settings_get(slug)) or {}
        attested = bool(stored.get("account_entitlement_attested_at")) and (
            stored.get("account_entitlement_attested_by_admin_id") is not None
        )
        admin_id = int((await event.get_sender()).id)
        if attested:
            await db.ai_provider_settings_upsert(
                slug,
                admin_id=admin_id,
                account_entitlement_attested_at=None,
                account_entitlement_attested_by_admin_id=None,
            )
        else:
            await db.ai_provider_settings_upsert(
                slug,
                admin_id=admin_id,
                account_entitlement_attested_at=utc_now(),
                account_entitlement_attested_by_admin_id=admin_id,
            )
        self.bot.provider_router.invalidate_cache()
        await db.add_audit_entry(
            admin_id=admin_id,
            action="ai_provider_account_entitlement_revoked"
            if attested
            else "ai_provider_account_entitlement_attested",
            target_type="ai_provider",
            target_id=slug,
            details={"attested": not attested},
        )
        await event.answer(
            "تأیید استحقاق لغو شد." if attested else "استحقاق حساب تأیید شد.", alert=True
        )
        await self.show_provider_detail(event, slug)

    async def _toggle_pass(self, event, column: str, slug: str) -> None:
        """Cycle one extra-pass override: inherit -> on -> off -> inherit."""
        if slug not in PROVIDER_REGISTRY or column not in _PASS_COLUMNS:
            await event.answer("درخواست نامعتبر.", alert=True)
            return
        db = self.bot.db
        stored = (await db.ai_provider_settings_get(slug)) or {}
        current = stored.get(column)
        profile = profile_for(slug)
        default = bool(getattr(profile, column, True))
        # None (inherit) -> explicit opposite of the default -> explicit default
        # -> back to inherit, so an admin can always return to "profile decides".
        if current is None:
            new_value = 0 if default else 1
        elif bool(current) == default:
            new_value = None
        else:
            new_value = 1 if default else 0
        admin_id = int((await event.get_sender()).id)
        await db.ai_provider_settings_upsert(slug, admin_id=admin_id, **{column: new_value})
        self.bot.provider_router.invalidate_cache()
        await db.add_audit_entry(
            admin_id=admin_id,
            action=f"ai_provider_{column}",
            target_type="ai_provider",
            target_id=slug,
            details={"value": new_value},
        )
        label = {None: "پیش‌فرض", 1: "روشن", 0: "خاموش"}.get(new_value, "پیش‌فرض")
        await event.answer(f"{_PASS_COLUMNS[column]}: {label}")
        await self.show_provider_detail(event, slug)

    # ----------------------------------------------------------------- keys
    async def show_provider_keys(self, event, slug: str) -> None:
        if slug not in PROVIDER_REGISTRY:
            await event.answer("ارائه‌دهندهٔ نامعتبر.", alert=True)
            return
        info = registry_info(slug)
        items = await self.bot.credential_manager.list_summaries()
        rows = [
            item for item in items
            if item["service"] == SERVICE
            and (item["provider"] == slug or _canonical_of(item) == slug)
        ]
        lines = [f"🔑 کلیدهای {info.display_name}", ""]
        lines.append(
            "کلیدهای محیطی قدیمی (از جمله NOTE_API_*) حفظ می‌شوند، اما تا ثبت نسخهٔ "
            "رمزشده در این خزانه و attestation صریح، برای تولید یادداشت قابل‌استفاده نیستند."
        )
        lines.append(
            "FREE یعنی مدیر تأیید می‌کند overage پولی با خاموش‌بودن billing یا سقف سخت $0 ناممکن است."
        )
        buttons: list[list] = []
        if not rows:
            lines.append("کلید خزانه‌ای ثبت نشده است؛ برای فعال‌سازی امن از «افزودن کلید» شروع کنید.")
        for item in rows:
            state = "فعال" if item["enabled"] else "غیرفعال"
            if item["quarantined_at"]:
                state = "قرنطینه (401/403)"
            elif cooldown_remaining_seconds(item.get("cooldown_until")) > 0:
                state = "cooldown"
            last = f" | HTTP {item['last_status_code']}" if item["last_status_code"] else ""
            billing_state = str(item.get("billing_state") or "unknown").lower()
            billing_labels = {
                "free": "🔒 FREE, no paid overage (attested)",
                "paid": "💳 paid use authorized (attested)",
                "unknown": "⚠️ billing not attested — generation blocked",
            }
            billing_text = billing_labels.get(billing_state, "⚠️ billing state unknown")
            attested_at = item.get("billing_attested_at")
            if attested_at:
                billing_text += f" | attested {attested_at[:10]}"
            model_text = f" | مدل: {item['model']}" if item.get("model") else ""
            lines.append(
                f"• #{item['id']} {item['label']} "
                f"{self.bot._masked_key(item['secret_last4'])} — {state}{last}"
                f" | {billing_text}{model_text}"
            )
            key_id = item["id"]
            toggle = "disable" if item["enabled"] else "enable"
            buttons.append(
                [
                    Button.inline(
                        f"{'غیرفعال' if toggle == 'disable' else 'فعال'} #{key_id}",
                        f"admin:ai:kstate:{toggle}:{key_id}".encode("ascii"),
                    ),
                    Button.inline("⭐ اصلی", f"admin:ai:kstate:primary:{key_id}".encode("ascii")),
                    Button.inline("❌ حذف", f"admin:ai:kstate:del1:{key_id}".encode("ascii")),
                ]
            )
            buttons.append(
                [
                    Button.inline("▲", f"admin:ai:kstate:up:{key_id}".encode("ascii")),
                    Button.inline("▼", f"admin:ai:kstate:down:{key_id}".encode("ascii")),
                    Button.inline("🧪 تست", f"admin:ai:keytest:{key_id}".encode("ascii")),
                    Button.inline("🩺 سلامت", f"admin:ai:kstate:health:{key_id}".encode("ascii")),
                ]
            )
            buttons.append(
                [
                    Button.inline("✏️ برچسب", f"admin:ai:kstate:edit:label:{key_id}".encode("ascii")),
                    Button.inline("🧬 مدل", f"admin:ai:kstate:edit:model:{key_id}".encode("ascii")),
                    Button.inline("🌐 Base URL", f"admin:ai:kstate:edit:base:{key_id}".encode("ascii")),
                ]
            )
            billing_state = str(item.get("billing_state") or "unknown").lower()
            free_mark = "✅" if billing_state == "free" else "🔒"
            paid_mark = "✅" if billing_state == "paid" else "💳"
            buttons.append(
                [
                    Button.inline(
                        f"{free_mark} attest FREE/no-overage",
                        f"admin:ai:kstate:attest_free:{key_id}".encode("ascii"),
                    ),
                    Button.inline(
                        f"{paid_mark} attest paid",
                        f"admin:ai:kstate:attest_paid:{key_id}".encode("ascii"),
                    ),
                    Button.inline(
                        "🛑 clear attestation",
                        f"admin:ai:kstate:attest_clear:{key_id}".encode("ascii"),
                    ),
                    Button.inline("🔄 تعویض کلید", f"admin:ai:kstate:repl:{key_id}".encode("ascii")),
                    Button.inline("📊 مصرف", f"admin:ai:kstate:usage:{key_id}".encode("ascii")),
                ]
            )
        buttons.extend(
            [
                [Button.inline("➕ افزودن کلید (راهنما)", f"admin:ai:kadd:{slug}".encode("ascii"))],
                [Button.inline("↩️ ارائه‌دهنده", f"admin:ai:pv:{slug}".encode("ascii"))],
            ]
        )
        await self.bot._edit_callback(event, "\n".join(lines), buttons)

    async def _key_action(self, event, action: str, key_id_text: str, slug: str) -> None:
        try:
            key_id = int(key_id_text)
        except ValueError:
            await event.answer("شناسهٔ کلید نامعتبر.", alert=True)
            return
        admin_id = int((await event.get_sender()).id)
        manager = self.bot.credential_manager
        if action == "enable":
            changed = await manager.enable(key_id, admin_id)
        elif action == "disable":
            changed = await manager.disable(key_id, admin_id)
        elif action == "delete":
            changed = await manager.delete(key_id, admin_id)
        elif action in {"up", "down"}:
            changed = await manager.reorder(key_id, action, admin_id)
        elif action == "primary":
            changed = await manager.set_primary(key_id, admin_id)
        elif action in {"attest_free", "attest_paid"}:
            state = "free" if action == "attest_free" else "paid"
            if state == "free":
                warning = (
                    "تأیید می‌کنید این حساب برای این کلید واقعاً رایگان است و "
                    "هر نوع overage پولی غیرفعال یا با سقف سخت $0 مسدود شده؟"
                )
            else:
                warning = (
                    "تأیید می‌کنید استفادهٔ پولی از این کلید مجاز است؟ این فقط "
                    "مجوز کلید است؛ fallback پولی همچنان با تنظیم سرور خاموش می‌ماند."
                )
            confirm_action = f"attest_{state}_confirm"
            await event.answer("تأیید billing لازم است.", alert=True)
            await self.bot._edit_callback(
                event,
                warning,
                [[
                    Button.inline("✅ تأیید", f"admin:ai:kstate:{confirm_action}:{key_id}".encode("ascii")),
                    Button.inline("انصراف", f"admin:ai:ky:{slug}".encode("ascii")),
                ]],
            )
            return
        elif action in {"attest_free_confirm", "attest_paid_confirm", "attest_clear"}:
            new_state = {
                "attest_free_confirm": "free",
                "attest_paid_confirm": "paid",
                "attest_clear": "unknown",
            }[action]
            changed = await manager.set_billing_attestation(key_id, new_state, admin_id)
        elif action in {"free", "paid"}:
            # Legacy callback compatibility: nullable flags never count as an
            # account billing attestation, so these actions only clear safely.
            changed = await manager.set_billing_attestation(key_id, "unknown", admin_id)
        elif action == "del1":
            # Two-step delete: the first press only asks for confirmation.
            await event.answer("⚠️ حذف کلید قطعی است؛ تأیید کنید.", alert=True)
            await self.bot._edit_callback(
                event,
                f"⚠️ آیا از حذف کلید #{key_id} مطمئن هستید؟ این کار پس از تأیید، کلید را "
                "برای همیشه پاک می‌کند.",
                [
                    [
                        Button.inline("✅ بله، حذف شود", f"admin:ai:kstate:del2:{key_id}".encode("ascii")),
                        Button.inline("❌ انصراف", f"admin:ai:ky:{slug}".encode("ascii")),
                    ]
                ],
            )
            return
        elif action == "del2":
            changed = await manager.delete(key_id, admin_id)
        elif action == "repl":
            # The new secret arrives as a message; bot.py deletes it before
            # persisting. Old and new values are never displayed.
            self.bot._pending_admin_actions[admin_id] = f"aikey_replace:{key_id}"
            await event.answer("کلید جدید را بفرستید؛ پیام فوراً حذف می‌شود.", alert=True)
            text = (
                "🔐 کلید جدید را به‌تنهایی بفرستید. پیام پس از دریافت حذف می‌شود؛ "
                "اگر حذف ممکن نباشد، کلید تعویض نمی‌شود.\n\nبرای انصراف /start را بزنید."
            )
            respond_fn = getattr(event, "respond", None)
            if callable(respond_fn):
                await respond_fn(text)
            else:
                await self.bot._edit_callback(
                    event,
                    text,
                    [[Button.inline("↩️ کلیدها", f"admin:ai:ky:{slug}".encode("ascii"))]],
                )
            return
        elif action.startswith("edit:"):
            field = action.split(":", 1)[1]
            if field not in {"label", "model", "base"}:
                await event.answer("فیلد نامعتبر.", alert=True)
                return
            self.bot._pending_admin_actions[admin_id] = f"aikey_edit:{field}:{key_id}"
            prompts = {
                "label": "✏️ برچسب جدید را بفرستید (حداکثر ۸۰ نویسه).",
                "model": "🧬 مدل جدید این کلید را بفرستید (برای حذف، «-» بفرستید).",
                "base": "🌐 Base URL جدید را بفرستید (برای حذف، «-» بفرستید).",
            }
            await event.answer(prompts[field], alert=True)
            text = prompts[field] + "\n\nبرای انصراف /start را بزنید."
            respond_fn = getattr(event, "respond", None)
            if callable(respond_fn):
                await respond_fn(text)
            else:
                await self.bot._edit_callback(
                    event,
                    text,
                    [[Button.inline("↩️ کلیدها", f"admin:ai:ky:{slug}".encode("ascii"))]],
                )
            return
        elif action == "health":
            await self._key_health(event, key_id, slug)
            return
        elif action == "usage":
            await self._key_usage(event, key_id, slug)
            return
        else:
            changed = False
        if changed:
            self.bot.provider_health.invalidate()
        await event.answer("ذخیره شد." if changed else "تغییری ثبت نشد.")
        await self.show_provider_keys(event, slug)

    async def _key_health(self, event, key_id: int, slug: str) -> None:
        """Per-key health: read-only probe, result card (spec §34/§37)."""
        await event.answer("در حال بررسی سلامت…")
        result = await self.bot.provider_health.test_credential(key_id, force=True)
        lines = [
            f"🩺 سلامت کلید #{key_id} ({result.label})",
            "",
            f"ارائه‌دهنده: {result.provider}",
            f"وضعیت: {result.status_fa}",
            f"HTTP: {result.http_status or '—'} | تأخیر: {result.latency_ms or '—'}ms",
            f"نوع بررسی: {result.check_type}",
            f"کلید: {result.masked} (فقط ۴ رقم آخر نمایش داده می‌شود)",
        ]
        if result.detail:
            lines.append(f"جزئیات: {sanitize_text(result.detail)[:200]}")
        await self.bot._edit_callback(
            event,
            "\n".join(lines),
            [[Button.inline("↩️ کلیدها", f"admin:ai:ky:{slug}".encode("ascii"))]],
        )

    async def _key_usage(self, event, key_id: int, slug: str) -> None:
        """Recent usage + failures for one key (metadata only, spec §34)."""
        rows = await self.bot.db.ai_usage_recent(limit=8, credential_id=key_id)
        failures = await self.bot.db.ai_usage_recent(
            limit=8, credential_id=key_id, failures_only=True
        )
        metrics = await self.bot.db.ai_usage_metrics(days=1)
        lines = [f"📊 مصرف کلید #{key_id}", ""]
        if not rows:
            lines.append("هنوز مصرفی برای این کلید ثبت نشده است.")
        for row in rows:
            lines.append(
                f"• {str(row['created_at'])[5:16]} {row['request_type'] or '-'}"
                f" {row['result']}"
                + (f" HTTP {row['http_status']}" if row.get("http_status") else "")
                + (f" | {row['latency_ms']}ms" if row.get("latency_ms") else "")
                + (f" | {row['error_class']}" if row.get("error_class") else "")
            )
        lines.append("")
        lines.append(f"شکست‌های اخیر: {len(failures)} (از {metrics['failures']} امروز)")
        await self.bot._edit_callback(
            event,
            "\n".join(lines),
            [[Button.inline("↩️ کلیدها", f"admin:ai:ky:{slug}".encode("ascii"))]],
        )

    async def _wizard_model_choices(self, slug: str) -> list[ModelInfo]:
        """Model buttons for the wizard: static seeds first, then the cached
        live-discovered catalog (NaraRouter/NVIDIA have no static seeds)."""
        choices = static_models(slug)[:6]
        if choices:
            return choices
        try:
            rows = await self.bot.db.ai_models_list(slug, include_unavailable=False)
        except Exception:
            rows = []
        discovered: list[ModelInfo] = []
        for row in rows[:6]:
            discovered.append(
                ModelInfo(
                    provider=slug,
                    model_id=str(row["model"]),
                    display_name=str(row.get("display_name") or ""),
                    free_status=str(row.get("free_status") or "unknown"),
                    capabilities=ModelCapabilities.from_json(row.get("capabilities_json")),
                )
            )
        return discovered

    async def begin_key_wizard(self, event, slug: str) -> None:
        if slug not in PROVIDER_REGISTRY:
            await event.answer("ارائه‌دهندهٔ نامعتبر.", alert=True)
            return
        bot = self.bot
        if not bot.credential_manager.encryption_configured:
            await self.bot._edit_callback(
                event,
                "ابتدا PROVIDER_CREDENTIALS_ENCRYPTION_KEY را در سرور تنظیم کنید؛ "
                "بدون آن کلید جدید ذخیره نمی‌شود.",
                [[Button.inline("↩️", f"admin:ai:ky:{slug}".encode("ascii"))]],
            )
            return
        info = registry_info(slug)
        choices = await self._wizard_model_choices(slug)
        lines = [
            f"🧭 راهنمای افزودن کلید برای {info.display_name}",
            "",
            f"۱) کلید را از کنسول ارائه‌دهنده بسازید: {info.docs_url}",
            f"۲) پروتکل: {info.protocol} | احراز هویت: {info.auth_style.value}",
            "۳) مدل پیش‌فرض این کلید را انتخاب کنید (اختیاری — بدون انتخاب، مدل خودکار تعیین می‌شود):",
        ]
        if not choices:
            lines.append(
                "هنوز مدلی کش نشده است — «مدل سفارشی» را بزنید یا ابتدا همگام‌سازی مدل‌ها را انجام دهید."
            )
        buttons = []
        for index, m in enumerate(choices):
            free_mark = "🆓" if m.free_status in {"free_permanent", "free_plan"} else "❔"
            label = f"{free_mark} {m.model_id[:26]}"
            buttons.append(
                [Button.inline(label, f"admin:ai:kmdl:{slug}:{index}".encode("ascii"))]
            )
        buttons.append(
            [Button.inline("مدل سفارشی / بدون مدل", f"admin:ai:kmdl:{slug}:c".encode("ascii"))]
        )
        buttons.append([Button.inline("لغو", f"admin:ai:ky:{slug}".encode("ascii"))])
        await self.bot._edit_callback(event, "\n".join(lines), buttons)

    async def wizard_pick_model(self, event, slug: str, choice: str) -> None:
        if slug not in PROVIDER_REGISTRY:
            await event.answer("ارائه‌دهندهٔ نامعتبر.", alert=True)
            return
        admin_id = int((await event.get_sender()).id)
        model = None
        if choice != "c":
            choices = await self._wizard_model_choices(slug)
            try:
                model = choices[int(choice)].model_id
            except (ValueError, IndexError):
                await event.answer("مدل نامعتبر.", alert=True)
                return
        bot = self.bot
        bot._pending_credential_setup[admin_id] = {
            "service": SERVICE,
            "provider": slug,
            "base_url": None,
            "model": model,
            # Wizard keys are stored DISABLED; the admin activates them only
            # after the non-destructive test report (spec §33 steps 7-9).
            "enabled": False,
            "via_wizard": True,
        }
        bot._pending_admin_actions[admin_id] = "aikey_label"
        await self.bot._edit_callback(
            event,
            "۴) یک برچسب کوتاه برای کلید بنویسید و بفرستید (مثلاً «کلید اصلی تیم»).\n"
            + (f"مدل انتخاب‌شده: {model}\n" if model else "")
            + "۵) سپس خود کلید را می‌فرستید؛ پیام کلید بلافاصله حذف می‌شود.\n"
            "۶) پس از ذخیره، یک تست غیرمخرب اجرا می‌شود و نتیجه را می‌بینید؛ "
            "کلید تا زمانی که شما آن را فعال نکنید، وارد مسیر نمی‌شود.",
            [[Button.inline("لغو", f"admin:ai:ky:{slug}".encode("ascii"))]],
        )

    async def wizard_finish_report(
        self, event, *, credential_id: int, slug: str, label: str, model: str | None
    ) -> None:
        """Spec §33 steps 7-9: non-destructive test, report, activate choice."""
        bot = self.bot
        result = await bot.provider_health.test_credential(credential_id, force=True)
        self._pending_key_test[f"{credential_id}"] = {
            "slug": slug,
            "http_status": result.http_status,
            "latency_ms": result.latency_ms,
        }
        lines = [
            f"🧪 نتیجهٔ تست کلید #{credential_id} ({label})",
            "",
            f"ارائه‌دهنده: {slug}" + (f" | مدل: {model}" if model else ""),
            f"کلید: {result.masked} (به‌صورت رمزنگاری‌شده ذخیره شد — فقط ۴ رقم آخر نمایش داده می‌شود)",
            f"HTTP: {result.http_status or '—'} | وضعیت: {result.status_fa}",
            f"تأخیر: {result.latency_ms or '—'}ms | نوع بررسی: {result.check_type} (read-only)",
        ]
        if result.detail:
            lines.append(f"جزئیات: {sanitize_text(result.detail)[:200]}")
        lines.append("")
        lines.append(
            "کلید در حالت «غیرفعال» ذخیره شد. برای ورود به مسیر تولید، فعال کنید:"
        )
        buttons = [
            [
                Button.inline(
                    "✅ فعال‌سازی", f"admin:ai:kact:{credential_id}".encode("ascii")
                ),
                Button.inline(
                    "❌ حذف کلید", f"admin:ai:kdel:{credential_id}".encode("ascii")
                ),
            ],
            [Button.inline("↩️ کلیدها", f"admin:ai:ky:{slug}".encode("ascii"))],
        ]
        await event.respond("\n".join(lines), buttons=buttons)

    async def _wizard_activate(self, event, credential_id: int) -> None:
        admin_id = int((await event.get_sender()).id)
        changed = await self.bot.credential_manager.enable(credential_id, admin_id)
        record = await self.bot.db.provider_credential_record(credential_id)
        slug = _canonical_of(record) if record else "openai_compatible"
        self.bot.provider_health.invalidate()
        await event.answer("کلید فعال شد و وارد مسیر می‌شود." if changed else "تغییری ثبت نشد.")
        await self.show_provider_keys(event, slug)

    async def _wizard_delete(self, event, credential_id: int) -> None:
        admin_id = int((await event.get_sender()).id)
        changed = await self.bot.credential_manager.delete(credential_id, admin_id)
        slug = "openai_compatible"
        self.bot.provider_health.invalidate()
        await event.answer("کلید حذف شد." if changed else "کلید پیدا نشد.")
        await self.show_provider_keys(event, slug)

    async def handle_wizard_label(self, event, text: str) -> None:
        bot = self.bot
        admin_id = int((await event.get_sender()).id)
        setup = bot._pending_credential_setup.get(admin_id) or {}
        label = text.strip()[:60] or "کلید"
        setup["label"] = label
        bot._pending_credential_setup[admin_id] = setup
        bot._pending_admin_actions[admin_id] = "credential_secret"
        await event.reply(
            "۴) حالا خود کلید API را به‌تنهایی بفرستید. پیام آن پیش از ذخیره حذف می‌شود؛ "
            "اگر حذف پیام ممکن نباشد، کلید ذخیره نخواهد شد."
        )

    async def _key_test(self, event, key_id_text: str, slug: str) -> None:
        try:
            key_id = int(key_id_text)
        except ValueError:
            await event.answer("شناسهٔ کلید نامعتبر.", alert=True)
            return
        await event.answer("در حال تست کلید…")
        result = await self.bot.provider_health.test_credential(key_id, force=True)
        await self.bot._edit_callback(
            event,
            "نتیجهٔ تست کلید:\n" + result.status_fa,
            [[Button.inline("↩️ کلیدها", f"admin:ai:ky:{slug}".encode("ascii"))]],
        )

    # --------------------------------------------------------------- routes
    async def show_routes(self, event) -> None:
        db = self.bot.db
        rows = await db.ai_routes_list(SERVICE, TASK)
        lines = ["🧭 مسیر تولید جزوه (ترتیب failover)", ""]
        buttons: list[list] = []
        if not rows:
            lines.append("مسیری ثبت نشده؛ پیش‌فرض داخلی اعمال می‌شود.")
        for index, row in enumerate(rows):
            slug = str(row["provider"])
            mark = _provider_mark(slug, None)
            label = (
                f"{index + 1}. {mark} {slug}"
                + (" 🆓" if row.get("free_only") else " 💳")
                + ("" if row.get("enabled", 1) else " ⛔")
            )
            lines.append(label + (f" — مدل: {row['model']}" if row.get("model") else ""))
            buttons.append(
                [
                    Button.inline("▲", f"admin:ai:rtmv:{index}:u".encode("ascii")),
                    Button.inline("▼", f"admin:ai:rtmv:{index}:d".encode("ascii")),
                    Button.inline(
                        "⛔" if row.get("enabled", 1) else "✅",
                        f"admin:ai:rten:{index}".encode("ascii"),
                    ),
                    Button.inline(
                        "🆓" if row.get("free_only", 1) else "💳",
                        f"admin:ai:rtfo:{index}".encode("ascii"),
                    ),
                    Button.inline("❌", f"admin:ai:rtdel:{index}".encode("ascii")),
                ]
            )
        existing = {str(r["provider"]) for r in rows}
        addable = [
            slug for slug in PROVIDER_REGISTRY
            if SERVICE in registry_info(slug).supported_services and slug not in existing
        ][:4]
        if addable:
            buttons.append(
                [
                    Button.inline(
                        f"➕ {slug}", f"admin:ai:rtadd:{slug}".encode("ascii")
                    )
                    for slug in addable[:2]
                ]
            )
            if len(addable) > 2:
                buttons.append(
                    [
                        Button.inline(
                            f"➕ {slug}", f"admin:ai:rtadd:{slug}".encode("ascii")
                        )
                        for slug in addable[2:4]
                    ]
                )
        lines.append("")
        lines.append("🆓 رایگان | 💳 fallback پولی | ⛔ غیرفعال. مسیرها بلافاصله در موتور اعمال می‌شوند.")
        buttons.append([Button.inline("↩️ پلتفرم AI", b"admin:ai")])
        await self.bot._edit_callback(event, "\n".join(lines), buttons)

    async def _routes(self) -> list[dict]:
        rows = await self.bot.db.ai_routes_list(SERVICE, TASK)
        return [dict(row) for row in rows]

    async def _save_routes(self, rows: list[dict], admin_id: int) -> None:
        entries = [
            {
                "provider": str(row["provider"]),
                "enabled": int(row.get("enabled", 1)),
                "free_only": int(row.get("free_only", 1)),
                "model": row.get("model"),
            }
            for row in rows
        ]
        await self.bot.db.ai_routes_replace(SERVICE, TASK, entries, admin_id=admin_id)
        await self.bot.db.add_audit_entry(
            admin_id=admin_id,
            action="ai_route_update",
            target_type="ai_route",
            target_id=TASK,
            details={"providers": [e["provider"] for e in entries]},
        )
        self.bot.provider_router.invalidate_cache()

    async def _route_move(self, event, index_text: str, direction: str) -> None:
        try:
            index = int(index_text)
        except ValueError:
            await event.answer("ایندکس نامعتبر.", alert=True)
            return
        rows = await self._routes()
        target = index + (1 if direction == "d" else -1)
        if not (0 <= index < len(rows) and 0 <= target < len(rows)):
            await event.answer("حرکت ممکن نیست.", alert=True)
            return
        rows[index], rows[target] = rows[target], rows[index]
        admin_id = int((await event.get_sender()).id)
        await self._save_routes(rows, admin_id)
        await self.show_routes(event)

    async def _route_toggle(self, event, index_text: str) -> None:
        try:
            index = int(index_text)
        except ValueError:
            await event.answer("ایندکس نامعتبر.", alert=True)
            return
        rows = await self._routes()
        if not 0 <= index < len(rows):
            await event.answer("ایندکس نامعتبر.", alert=True)
            return
        rows[index]["enabled"] = 0 if rows[index].get("enabled", 1) else 1
        admin_id = int((await event.get_sender()).id)
        await self._save_routes(rows, admin_id)
        await self.show_routes(event)

    async def _route_toggle_free_only(self, event, index_text: str) -> None:
        try:
            index = int(index_text)
        except ValueError:
            await event.answer("ایندکس نامعتبر.", alert=True)
            return
        rows = await self._routes()
        if not 0 <= index < len(rows):
            await event.answer("ایندکس نامعتبر.", alert=True)
            return
        rows[index]["free_only"] = 0 if rows[index].get("free_only", 1) else 1
        admin_id = int((await event.get_sender()).id)
        await self._save_routes(rows, admin_id)
        state_label = "فقط-رایگان" if rows[index]["free_only"] else "مجاز پولی"
        await event.answer(f"حالت این پله به «{state_label}» تغییر یافت.")
        await self.show_routes(event)

    async def _route_delete(self, event, index_text: str) -> None:
        try:
            index = int(index_text)
        except ValueError:
            await event.answer("ایندکس نامعتبر.", alert=True)
            return
        rows = await self._routes()
        if not 0 <= index < len(rows):
            await event.answer("ایندکس نامعتبر.", alert=True)
            return
        removed = rows.pop(index)
        admin_id = int((await event.get_sender()).id)
        await self._save_routes(rows, admin_id)
        await event.answer(f"{removed['provider']} از مسیر حذف شد.")
        await self.show_routes(event)

    async def _route_add(self, event, slug: str) -> None:
        if slug not in PROVIDER_REGISTRY:
            await event.answer("ارائه‌دهندهٔ نامعتبر.", alert=True)
            return
        rows = await self._routes()
        if any(str(r["provider"]) == slug for r in rows):
            await event.answer("این ارائه‌دهنده هم‌اکنون در مسیر است.", alert=True)
            return
        info = registry_info(slug)
        rows.append(
            {
                "provider": slug,
                "enabled": 1,
                "free_only": 1 if info.classification != ProviderClass.PAID_ONLY else 0,
                "model": None,
            }
        )
        admin_id = int((await event.get_sender()).id)
        await self._save_routes(rows, admin_id)
        await event.answer(f"{slug} به انتهای مسیر اضافه شد.")
        await self.show_routes(event)

    # ---------------------------------------------------------------- usage
    async def show_usage(self, event, days: int = 7) -> None:
        db = self.bot.db
        rows = await db.ai_usage_summary(days=days)
        metrics = await db.ai_usage_metrics(days=1)
        event_counts = await db.ai_event_metrics(days=1)
        latency_p95 = await db.ai_usage_latency_p95(days=1)
        quotas = await db.ai_quota_latest_all(limit=12)
        lines = [
            f"📊 وضعیت مصرف و رصدپذیری ({'امروز' if days == 1 else f'{days} روز گذشته'})",
            "",
            f"امروز: {_fmt_int(metrics['requests'])} درخواست"
            f" (موفق {_fmt_int(metrics['successes'])}, خطا {_fmt_int(metrics['failures'])}, "
            f"429/quota {_fmt_int(metrics['rate_limited'])}, 5xx {_fmt_int(metrics['server_errors'])})",
            f"تأخیر: p95 {_fmt_int(latency_p95)}ms | fallback‌ها: {_fmt_int(metrics['fallbacks'])}",
            f"توکن‌ها: ورودی {_fmt_int(metrics['input_tokens'])} | "
            f"خروجی {_fmt_int(metrics['output_tokens'])} | مجموع {_fmt_int(metrics['total_tokens'])}",
        ]
        pipeline_stats = []
        if event_counts.get("note_repair_started"):
            pipeline_stats.append(
                f"تعمیر چانک: {event_counts.get('note_repair_accepted', 0)}/{event_counts['note_repair_started']}"
            )
        if event_counts.get("note_compile_started"):
            pipeline_stats.append(
                f"تلفیق نهایی: {event_counts.get('note_compile_accepted', 0)}/{event_counts['note_compile_started']}"
            )
        if event_counts.get("note_backend_artifacts_scrubbed"):
            pipeline_stats.append(
                f"پالایش ترشحات: {event_counts['note_backend_artifacts_scrubbed']}"
            )
        if pipeline_stats:
            lines.append("مراحل خط‌لوله: " + " | ".join(pipeline_stats))
        lines.append("")
        lines.append(f"تفکیک به ازای ارائه‌دهنده ({days} روز):")
        buttons: list[list] = []
        totals: dict[str, dict] = {}
        for row in rows:
            provider = str(row["provider"])
            agg = totals.setdefault(
                provider,
                {
                    "requests": 0, "successes": 0, "failures": 0,
                    "rl": 0, "fb": 0, "paid": 0, "in_tok": 0, "out_tok": 0,
                },
            )
            agg["requests"] += int(row["requests"] or 0)
            agg["successes"] += int(row["successes"] or 0)
            agg["failures"] += int(row["failures"] or 0)
            agg["rl"] += int(row["rate_limit_hits"] or 0)
            agg["fb"] += int(row["fallbacks"] or 0)
            agg["paid"] += int(row["paid_block_events"] or 0)
            agg["in_tok"] += int(row["input_tokens"] or 0)
            agg["out_tok"] += int(row["output_tokens"] or 0)
        if not totals:
            lines.append("هنوز مصرفی در این بازه ثبت نشده است.")
        lines.append("شمارنده‌های محلی سهمیه خودکار با پنجرهٔ UTC جلو می‌روند؛ reset دستی برای جلوگیری از عبور از سقف حذف شده است.")
        for provider, agg in sorted(totals.items()):
            lines.append(
                f"• {provider}: {_fmt_int(agg['requests'])} req"
                f" (موفق {_fmt_int(agg['successes'])}, 429/quota {_fmt_int(agg['rl'])}, "
                f"fallback {_fmt_int(agg['fb'])})"
                f" | {_fmt_int(agg['in_tok'] + agg['out_tok'])} tok"
                + (f" | بلاک پولی ⚠️ {_fmt_int(agg['paid'])}" if agg["paid"] else "")
            )

        if quotas:
            lines.append("")
            lines.append("آخرین تصویر سهمیهٔ ارائه‌دهنده‌ها:")
            for q in quotas[:4]:
                lines.append(
                    f"• {q['provider']}: {q['remaining'] or '—'}"
                    + (f" | بازنشانی {q['reset_at']}" if q.get("reset_at") else "")
                )
        buttons.append(
            [
                Button.inline("امروز", b"admin:ai:us:1"),
                Button.inline("۷ روز", b"admin:ai:us:7"),
                Button.inline("۳۰ روز", b"admin:ai:us:30"),
            ]
        )
        buttons.append([Button.inline("↩️ پلتفرم AI", b"admin:ai")])
        await self.bot._edit_callback(event, "\n".join(lines), buttons)

    async def _usage_reset(self, event, provider: str) -> None:
        """Legacy callback is intentionally fail-closed; resets could overspend."""
        admin_id = int((await event.get_sender()).id)
        await self.bot.db.add_audit_entry(
            admin_id=admin_id,
            action="ai_usage_reset_blocked",
            target_type="ai_provider",
            target_id=provider,
            details={"reason": "manual reset could bypass provider quota"},
        )
        await event.answer("بازنشانی دستی سهمیه غیرفعال است؛ پنجرهٔ UTC یا reset رسمی provider را صبر کنید.", alert=True)
        await self.show_usage(event)

    # --------------------------------------------------------------- models
    async def show_models(self, event, slug: str | None = None) -> None:
        db = self.bot.db
        buttons: list[list] = []
        if slug is None:
            lines = ["🧬 کاتالوگ مدل‌ها", "", "برای دیدن مدل‌های هر ارائه‌دهنده روی نام آن بزنید:"]
            seen = 0
            row: list = []
            for candidate in PROVIDER_REGISTRY:
                info = registry_info(candidate)
                if SERVICE not in info.supported_services or not info.model_discovery:
                    continue
                row.append(
                    Button.inline(candidate, f"admin:ai:md:{candidate}".encode("ascii"))
                )
                seen += 1
                if seen % 2 == 0:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)
            buttons.append([Button.inline("↩️ پلتفرم AI", b"admin:ai")])
            await self.bot._edit_callback(event, "\n".join(lines), buttons)
            return
        if slug not in PROVIDER_REGISTRY:
            await event.answer("ارائه‌دهندهٔ نامعتبر.", alert=True)
            return
        models = await db.ai_models_list(slug, include_unavailable=True)
        lines = [f"🧬 مدل‌های {slug}", ""]
        if not models:
            lines.append("هنوز مدلی ثبت نشده؛ «همگام‌سازی» بزنید.")
        for row in models[:14]:
            status = []
            if row.get("deprecated"):
                status.append("منسوخ")
            if not row.get("available", 1):
                status.append("حذف‌شده از کاتالوگ")
            if _requires_paid_billing(row):
                status.append("نیازمند صورتحساب 💳")
            free_status = str(row.get("free_status") or "unknown")
            lines.append(
                f"• #{row['id']} {row['model']} — {free_status}"
                + (" (" + ", ".join(status) + ")" if status else "")
            )
            if not row.get("deprecated") and row.get("available", 1):
                paid_billing = _requires_paid_billing(row)
                buttons.append(
                    [
                        Button.inline(
                            f"🗂 منسوخ #{row['id']}",
                            f"admin:ai:mdep:{row['id']}".encode("ascii"),
                        ),
                        Button.inline(
                            f"{'💳' if paid_billing else '🆓'} پولی #{row['id']}",
                            f"admin:ai:mpaid:{row['id']}".encode("ascii"),
                        ),
                    ]
                )
        # Replacement suggestions for dead models (spec §16): a route pointing
        # at a withdrawn NVIDIA endpoint should offer a live alternative
        # instead of silently failing over at request time.
        from .ai.models import suggest_replacement

        try:
            live_catalog = await self.bot.provider_router.models.cached(slug)
        except Exception:
            live_catalog = []
        for row in models[:14]:
            if not (row.get("deprecated") or not row.get("available", 1)):
                continue
            replacement = suggest_replacement(live_catalog, str(row["model"]))
            if replacement is None:
                continue
            lines.append(
                f"  ↳ جایگزین پیشنهادی: {replacement.model_id}"
                + (" (Free Endpoint)" if replacement.free_endpoint else "")
            )
        stored = (await db.ai_provider_settings_get(slug)) or {}
        region_bits = [
            f"{fa}: {stored[key]}"
            for key, fa in (
                ("region", "منطقه"),
                ("deployment_scope", "محدودهٔ استقرار"),
                ("quota_expires_at", "انقضای سهمیه"),
            )
            if stored.get(key)
        ]
        if region_bits:
            lines.append("")
            lines.append("محدودیت منطقه‌ای: " + " | ".join(region_bits))
        buttons.extend(
            [
                [Button.inline("🔄 همگام‌سازی مدل‌ها", f"admin:ai:mdsync:{slug}".encode("ascii"))],
                [Button.inline("↩️ ارائه‌دهنده", f"admin:ai:pv:{slug}".encode("ascii"))],
            ]
        )
        await self.bot._edit_callback(event, "\n".join(lines), buttons)

    async def _mark_deprecated(self, event, model_id_text: str) -> None:
        try:
            model_row_id = int(model_id_text)
        except ValueError:
            await event.answer("شناسهٔ مدل نامعتبر.", alert=True)
            return
        db = self.bot.db
        record = await db.ai_models_get_by_id(model_row_id)
        if not record:
            await event.answer("مدل پیدا نشد.", alert=True)
            return
        admin_id = int((await event.get_sender()).id)
        await db.ai_models_set_deprecated(
            record["provider"], record["model"], date=None
        )
        await db.add_audit_entry(
            admin_id=admin_id,
            action="ai_model_deprecated",
            target_type="ai_model",
            target_id=str(model_row_id),
            details={"provider": record["provider"], "model": record["model"]},
        )
        await event.answer("مدل منسوخ علامت‌گذاری شد؛ از مسیر کنار گذاشته می‌شود.")
        await self.show_models(event, record["provider"])

    async def _mark_paid_billing(self, event, model_id_text: str) -> None:
        """Toggle whether a model needs a billable plan (spec §17).

        Cloudflare and similar catalogs list frontier models that cannot run on
        the free allocation. Marking one here makes FREE_ONLY reject it up
        front instead of burning the daily quota discovering that the hard way.
        """
        try:
            model_row_id = int(model_id_text)
        except ValueError:
            await event.answer("شناسهٔ مدل نامعتبر.", alert=True)
            return
        db = self.bot.db
        record = await db.ai_models_get_by_id(model_row_id)
        if not record:
            await event.answer("مدل پیدا نشد.", alert=True)
            return
        new_value = not _requires_paid_billing(record)
        admin_id = int((await event.get_sender()).id)
        await db.ai_model_set_requires_paid_billing(
            record["provider"], record["model"], required=new_value
        )
        await db.add_audit_entry(
            admin_id=admin_id,
            action="ai_model_requires_paid_billing",
            target_type="ai_model",
            target_id=str(model_row_id),
            details={
                "provider": record["provider"],
                "model": record["model"],
                "value": new_value,
            },
        )
        await event.answer(
            "مدل نیازمند صورتحساب شد." if new_value else "علامت صورتحساب برداشته شد."
        )
        await self.show_models(event, record["provider"])

    async def _sync_models(self, event, slug: str) -> None:
        if slug not in PROVIDER_REGISTRY:
            await event.answer("ارائه‌دهندهٔ نامعتبر.", alert=True)
            return
        await event.answer("در حال همگام‌سازی کاتالوگ…")
        result = await self._run_catalog_sync(slug)
        detail = sanitize_text(result.detail or ("OK" if result.ok else "ناموفق"))
        await self.bot._edit_callback(
            event,
            f"همگام‌سازی مدل‌های {slug}: {'موفق ✅' if result.ok else 'ناموفق ⛔'}\n"
            f"مدل‌های همگام‌شده: {result.synced} | غیرفعال‌شده: {result.deactivated}\n"
            f"جزئیات: {detail[:300]}",
            [[Button.inline("↩️ مدل‌ها", f"admin:ai:md:{slug}".encode("ascii"))]],
        )

    async def _run_catalog_sync(self, slug: str):
        async def session_get(url: str, headers: dict, timeout: float):
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)
                ) as response:
                    body = await response.read()
                    return response.status, dict(response.headers), body

        return await sync_provider_catalog(
            self.bot.provider_router, slug, session_get=session_get, force=True
        )

    # ----------------------------------------------------------------- logs
    async def show_logs(
        self,
        event,
        slug: str | None = None,
        event_name: str | None = None,
        *,
        status_class: int | None = None,
        error_class: str | None = None,
        job_id: str | None = None,
        days: int = 0,
    ) -> None:
        """Spec §35: logs filterable by provider/status/date/error type.

        The panel never renders prompt text, transcripts, model output or
        secrets — ``ai_events`` stores metadata only, by construction.
        """
        db = self.bot.db
        since = None
        if days:
            since = (
                datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
            ).isoformat(timespec="seconds")
        events = await db.ai_events_list(
            limit=14,
            provider=slug,
            event=event_name,
            error_class=error_class,
            job_id=job_id,
            since=since,
            http_status=status_class,
        )
        title = "🧾 آخرین رویدادهای AI"
        if slug:
            title += f" — {slug}"
        if event_name:
            title += f" [{event_name}]"
        if status_class:
            title += f" | {status_class}xx" if status_class < 100 else f" | HTTP {status_class}"
        if error_class:
            title += f" | خطا: {error_class}"
        if job_id:
            title += f" | کار {job_id}"
        if days:
            title += f" | {days} روز"
        lines = [title, ""]
        if not events:
            lines.append("رویدادی با این فیلتر ثبت نشده است.")
        for row in events:
            extra = ""
            if row.get("http_status"):
                extra += f" HTTP {row['http_status']}"
            if row.get("latency_ms"):
                extra += f" {row['latency_ms']}ms"
            if row.get("error_class"):
                extra += f" [{row['error_class']}]"
            job = f" {row['job_id']}" if row.get("job_id") else ""
            lines.append(
                f"• {str(row['created_at'])[:16]} {row['event']}"
                f" {row.get('provider') or '-'}"
                + job
                + extra
            )
        lines.append("")
        lines.append("(لاگ‌ها هرگز شامل کلید، متن سخنرانی یا پاسخ مدل نیستند — spec §30)")
        buttons = [
            [
                Button.inline("همه", b"admin:ai:lg"),
                Button.inline("خطاها", b"admin:ai:lgfilter:err"),
                Button.inline("Fallback", b"admin:ai:lgfilter:fb"),
                Button.inline("429/Quota", b"admin:ai:lgfilter:rl"),
            ],
            [
                Button.inline("4xx", b"admin:ai:lgf:st4"),
                Button.inline("5xx", b"admin:ai:lgf:st5"),
                Button.inline("امروز", b"admin:ai:lgf:d1"),
                Button.inline("۷ روز", b"admin:ai:lgf:d7"),
            ],
            [
                Button.inline("⏱ تلاش مجدد", b"admin:ai:lgfilter:retry"),
                Button.inline("🔁 تعمیر JSON", b"admin:ai:lgfilter:json"),
            ],
            [Button.inline("↩️ پلتفرم AI", b"admin:ai")],
        ]
        await self.bot._edit_callback(event, "\n".join(lines), buttons)

    async def _log_filter(self, event, token: str) -> None:
        """Apply one compact log filter token (``st4``, ``st5``, ``d1``, …)."""
        if token == "st4":
            await self.show_logs(event, status_class=4)
        elif token == "st5":
            await self.show_logs(event, status_class=5)
        elif token == "d1":
            await self.show_logs(event, days=1)
        elif token == "d7":
            await self.show_logs(event, days=7)
        else:
            await self.show_logs(event)

    # ---------------------------------------------------------------- health
    async def show_health(self, event) -> None:
        """Spec §35/§37: cheap, read-only health with explicit capability state.

        Opening this panel never submits a generation request. It reuses the
        existing cached read-only probes (model/catalog endpoints) and only
        reports what the last probe already knew; an actual completion is only
        sent by the separate per-key "test request" action, which is logged
        with ``check_type='generation'``.
        """
        checker = getattr(self.bot, "health_checker", None)
        results = []
        if checker is not None:
            try:
                results = list(checker.cached())
            except Exception:
                results = []
        lines = ["🩺 سلامت ارائه‌دهنده‌ها", ""]
        if not results:
            lines.append("هنوز نتیجهٔ بررسی‌ای در حافظه نیست.")
            lines.append("بررسی‌ها فقط هنگام باز شدن پنل قدیمی سلامت یا تست دستی اجرا می‌شوند.")
        for result in results[:12]:
            if str(getattr(result, "service", "")) != SERVICE:
                continue
            http = f"HTTP {result.http_status}" if result.http_status else "HTTP —"
            latency = f"{result.latency_ms}ms" if result.latency_ms else "—"
            checked = str(result.checked_at or "")[:16] or "—"
            lines.append(
                f"• {result.provider} / {result.label or '—'}: {result.status_fa} ({http}, {latency})"
            )
            lines.append(f"  آخرین بررسی: {checked} | نوع: {result.check_type}")
        lines.append("")
        lines.append("🟢 سالم | 🟡 کاهش کیفیت | 🔴 محدود/خطا | ⛔ غیرفعال یا نیازمند صورتحساب")
        lines.append("بررسی خودکار فقط خواندنی (read_only) است و هیچ تولیدی ارسال نمی‌کند.")
        buttons = [
            [Button.inline("🔄 بررسی تازه (فقط خواندنی)", b"admin:ai:herefresh")],
            [Button.inline("↩️ پلتفرم AI", b"admin:ai")],
        ]
        await self.bot._edit_callback(event, "\n".join(lines), buttons)

    async def _health_refresh(self, event) -> None:
        """Run the cached read-only probes once and redraw the panel."""
        checker = getattr(self.bot, "health_checker", None)
        if checker is None:
            await event.answer("بررسی‌گر سلامت در دسترس نیست.", alert=True)
            return
        await event.answer("در حال بررسی فقط‌خواندنی…")
        try:
            results = await checker.check_all(force=True)
        except Exception:
            results = []
        await self.bot.db.ai_event_insert(
            {
                "level": "info",
                "event": "provider_health_checked",
                "service": SERVICE,
                "check_type": "read_only",
                "detail": f"probes={len(results)}",
            }
        )
        await self.show_health(event)

    # -------------------------------------------------------------- settings
    async def show_settings(self, event) -> None:
        """Spec §35/§41: deployment switches, all read-only here by design."""
        settings = self.bot.settings
        lines = [
            "⚙️ تنظیمات پلتفرم هوش مصنوعی",
            "",
            f"FREE_ONLY: {'روشن ✅' if settings.ai_free_only else 'خاموش ⛔'}",
            f"Fallback پولی: {'روشن ⚠️' if settings.ai_allow_paid_fallback else 'خاموش ✅'}",
            f"موتور مسیریاب: {'فعال' if settings.ai_routing_enabled else 'غیرفعال (legacy)'}",
            f"حداکثر failover ارائه‌دهنده: {int(getattr(settings, 'ai_max_provider_failovers', 3))}",
            f"حداکثر تلاش مجدد: {int(getattr(settings, 'ai_max_generation_retries', -1))}",
            f"TTL همگام‌سازی کاتالوگ: {int(getattr(settings, 'ai_provider_sync_ttl', 86400))} ثانیه",
            f"ضریب ایمنی سهمیه: {getattr(settings, 'ai_quota_safety_margin', 0.15)}",
            "",
            "این گزینه‌ها در سطح استقرار هستند و فقط با متغیرهای محیطی سرور تغییر می‌کنند؛",
            "تغییر ناگهانی آن‌ها از پنل می‌تواند از سقف رایگان عبور کند.",
        ]
        stored = await self.bot.db.ai_provider_settings_all()
        overrides = {k: v for k, v in (stored or {}).items() if v}
        if overrides:
            lines.append("")
            lines.append("لغوهای ثبت‌شدهٔ مدیر:")
            for slug, row in list(overrides.items())[:8]:
                flags = []
                if row.get("enabled") == 0:
                    flags.append("غیرفعال")
                if row.get("free_only_blocked"):
                    flags.append("بلاک رایگان")
                if row.get("experimental_unlocked"):
                    flags.append("قفل آزمایشی باز")
                if row.get("account_entitlement_attested_at"):
                    flags.append("استحقاق تأییدشده")
                if flags:
                    lines.append(f"• {slug}: " + "، ".join(flags))
        buttons = [[Button.inline("↩️ پلتفرم AI", b"admin:ai")]]
        await self.bot._edit_callback(event, "\n".join(lines), buttons)

    # ------------------------------------------------------------- dry-run
    async def show_dry(self, event, slug: str | None = None) -> None:
        if slug is None:
            lines = ["🧪 Dry-run (نمایش درخواست بدون ارسال)", "", "ارائه‌دهنده را انتخاب کنید:"]
            buttons: list[list] = []
            row: list = []
            for candidate in PROVIDER_REGISTRY:
                if SERVICE not in registry_info(candidate).supported_services:
                    continue
                row.append(Button.inline(candidate, f"admin:ai:dry:{candidate}".encode("ascii")))
                if len(row) == 2:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)
            buttons.append([Button.inline("↩️ پلتفرم AI", b"admin:ai")])
            await self.bot._edit_callback(event, "\n".join(lines), buttons)
            return
        if slug not in PROVIDER_REGISTRY:
            await event.answer("ارائه‌دهندهٔ نامعتبر.", alert=True)
            return
        profile = profile_for(slug)
        adapter = adapter_for(slug, self.bot.settings)
        base_url = self.bot.provider_router._base_url_for_slug(slug) or adapter.default_base_url()
        try:
            model_id = fallback_free_model(slug) or default_model_for(slug) or ""
        except Exception:
            model_id = ""
        ctx = RequestContext(
            service=SERVICE,
            request_type="dry_run",
            provider=slug,
            canonical=slug,
            base_url=base_url or "",
            model=model_id,
            system_prompt="SYSTEM_PROMPT",
            user_text="این یک متن آزمایشی برای پیش‌نمایش ساختار درخواست است.",
            max_output_tokens=min(512, profile.max_output_tokens),
            model_info=ModelInfo(
                provider=slug, model_id=model_id, capabilities=ModelCapabilities()
            ),
            json_strategy="prompt",
        )
        try:
            request = adapter.build(ctx, "")
        except Exception:
            # Adapters legitimately refuse a build without credentials (Gemini
            # native). Rebuild with a placeholder that is never displayed.
            request = adapter.build(ctx, "sk-preview-placeholder")
        request.redacted()  # double-check secret hygiene before rendering
        redacted = request.redacted()
        tokens = estimate_tokens(ctx.user_text or "") + estimate_tokens(ctx.system_prompt or "")
        lines = [
            f"🧪 Dry-run — {slug}",
            "",
            f"متد: {redacted.method}",
            f"آدرس: {redacted.url}",
            f"استراتژی JSON: {redacted.json_strategy}",
            f"تخمین توکن ورودی: {_fmt_int(tokens)}",
            "",
            "پارامترها:",
        ]
        for key, value in sorted((redacted.json_body or {}).items()):
            if isinstance(value, (int, float, str, bool)) and len(str(value)) < 48:
                lines.append(f"• {key} = {value}")
            else:
                lines.append(f"• {key} = …")
        lines.append("")
        lines.append("⚠️ چیزی ارسال نشد؛ این فقط پیش‌نمایش ساختار است.")
        buttons = [[Button.inline("↩️ ارائه‌دهنده", f"admin:ai:pv:{slug}".encode("ascii"))]]
        await self.bot._edit_callback(event, "\n".join(lines), buttons)

    # -------------------------------------------------------- quota probe
    async def probe_quota_action(self, event, slug: str) -> None:
        """Explicit, read-only entitlement probe (spec §10/§12)."""
        if slug not in PROVIDER_REGISTRY:
            await event.answer("ارائه‌دهندهٔ نامعتبر.", alert=True)
            return
        await event.answer("در حال کاوش وضعیت سهمیه…")
        from .ai.sync import probe_provider_quota

        result = await probe_provider_quota(self.bot.provider_router, slug)
        summary = result.summary() if result else "کاوش ممکن نشد."
        await self.bot._edit_callback(
            event,
            f"📡 نتیجهٔ کاوش سهمیه — {slug}\n\n{summary}",
            [[Button.inline("↩️ ارائه‌دهنده", f"admin:ai:pv:{slug}".encode("ascii"))]],
        )

    # ------------------------------------------------------------ benchmarks
    async def show_benchmarks(self, event) -> None:
        """Spec §35 Benchmarks panel: scores for models evaluated with scripts/benchmark_notes.py."""
        db = self.bot.db
        scores = await db.ai_model_benchmark_scores(limit=10)
        lines = [
            "🏆 ارزیابی و امتیاز کیفیت مدل‌ها (Gamas Quality Score)",
            "",
            "فرمول: ۴۰٪ پوشش معنایی | ۲۰٪ حفظ حقایق | ۱۵٪ اعتبار JSON |",
            "۱۰٪ ساختار | ۵٪ اصطلاحات | ۵٪ تأخیر | ۵٪ قابلیت اطمینان",
            "",
        ]
        if not scores:
            lines.append("هنوز امتیازی در پایگاه‌داده ثبت نشده است.")
            lines.append("برای اجرا: python -m scripts.benchmark_notes --live")
        for rank, s in enumerate(scores, start=1):
            free_mark = "🆓" if s.get("free_status") in {"free_permanent", "free_plan"} else "💳"
            lines.append(
                f"{rank}. {s['provider']}/{s['model']} — امتیاز {s['quality_score']:.1f}/100 {free_mark}"
                + (f" ({str(s['last_benchmarked_at'])[:10]})" if s.get("last_benchmarked_at") else "")
            )
        buttons = [[Button.inline("↩️ پلتفرم AI", b"admin:ai")]]
        await self.bot._edit_callback(event, "\n".join(lines), buttons)

    # ------------------------------------------------------------ plan view
    async def show_plan(self, event) -> None:
        plan = await self.bot.provider_router.plan()
        lines = ["📈 مسیر مؤثر فعلی (با فیلتر FREE_ONLY)", ""]
        if not plan.legs:
            lines.append("هیچ مسیر واجد شرایطی نیست؛ ربات از مسیر قدیمی استفاده می‌کند یا خطا می‌دهد.")
        for index, leg in enumerate(plan.legs, start=1):
            profile = profile_for(leg.canonical)
            from .ai.tokens import chunk_char_budget

            chars = chunk_char_budget(profile.chunk_token_budget, char_cap=profile.chunk_char_cap)
            lines.append(
                f"{index}. {leg.provider}"
                + (" 🆓" if leg.free_only else " 💳")
                + f" — چانک {_fmt_int(profile.chunk_token_budget)} توکن / {_fmt_int(chars)} نویسه"
            )
        for provider, reason in plan.skipped:
            lines.append(f"- {provider}: {reason}")
        lines.append("")
        lines.append(
            f"outline: {'✅' if plan.outline_enabled else '❌'}"
            f" | repair: {'✅' if plan.repair_enabled else '❌'}"
            f" | compile: {'✅' if plan.compile_enabled else '❌'}"
        )
        buttons = [[Button.inline("↩️ پلتفرم AI", b"admin:ai")]]
        await self.bot._edit_callback(event, "\n".join(lines), buttons)


def _canonical_of(item: dict) -> str:
    from .ai.registry import resolve_canonical

    try:
        return resolve_canonical(str(item.get("provider") or ""), item.get("base_url"))
    except Exception:
        return str(item.get("provider") or "")


async def handle_ai_callback(bot, event, data: str) -> None:
    """Dispatch for everything under the ``admin:ai`` namespace."""
    ui = bot.ai_panels
    parts = data.split(":")
    action = parts[2] if len(parts) > 2 else ""
    arg1 = parts[3] if len(parts) > 3 else None
    arg2 = parts[4] if len(parts) > 4 else None

    if data == "admin:ai":
        await ui.show_platform_home(event)
    elif action == "pv" and arg1:
        await ui.show_provider_detail(event, arg1)
    elif action == "pv":
        await ui.show_providers(event)
    elif action == "ptgl" and arg1:
        await ui._toggle_provider(event, arg1, "enabled", "وضعیت ارائه‌دهنده")
    elif action == "pblk" and arg1:
        await ui._toggle_provider(event, arg1, "free_only_blocked", "بلاک رایگان‌سازی")
    elif action == "pxun" and arg1:
        await ui._toggle_provider(event, arg1, "experimental_unlocked", "قفل آزمایشی")
    elif action == "patt" and arg1:
        await ui._toggle_account_entitlement(event, arg1)
    elif action == "ppass" and arg1 and arg2:
        await ui._toggle_pass(event, arg1, arg2)
    elif action == "he":
        await ui.show_health(event)
    elif action == "herefresh":
        await ui._health_refresh(event)
    elif action == "st":
        await ui.show_settings(event)
    elif action == "lgf" and arg1:
        await ui._log_filter(event, arg1)
    elif action == "ky" and arg1:
        await ui.show_provider_keys(event, arg1)
    elif action == "kstate" and arg1 and arg2:
        # slug for the redraw comes from the credential row itself.
        try:
            key_id = int(arg2)
        except ValueError:
            await event.answer("شناسهٔ کلید نامعتبر.", alert=True)
            return
        record = await bot.db.provider_credential_record(key_id)
        slug = _canonical_of(record) if record else "openai_compatible"
        await ui._key_action(event, arg1, arg2, slug)
    elif action == "kadd" and arg1:
        await ui.begin_key_wizard(event, arg1)
    elif action == "kmdl" and arg1 and arg2:
        await ui.wizard_pick_model(event, arg1, arg2)
    elif action == "keytest" and arg1:
        record = None
        try:
            record = await bot.db.provider_credential_record(int(arg1))
        except ValueError:
            record = None
        slug = _canonical_of(record) if record else "openai_compatible"
        await ui._key_test(event, arg1, slug)
    elif action == "rt":
        await ui.show_routes(event)
    elif action == "rtmv" and arg1 is not None and arg2:
        await ui._route_move(event, arg1, arg2)
    elif action == "rten" and arg1 is not None:
        await ui._route_toggle(event, arg1)
    elif action == "rtdel" and arg1 is not None:
        await ui._route_delete(event, arg1)
    elif action == "rtadd" and arg1:
        await ui._route_add(event, arg1)
    elif action == "kact" and arg1:
        await ui._wizard_activate(event, int(arg1))
    elif action == "kdel" and arg1:
        await ui._wizard_delete(event, int(arg1))
    elif action == "rtfo" and arg1 is not None:
        await ui._route_toggle_free_only(event, arg1)
    elif action == "pqprobe" and arg1:
        await ui.probe_quota_action(event, arg1)
    elif action == "bm":
        await ui.show_benchmarks(event)
    elif action == "lgfilter" and arg1:
        event_filter = {
            "err": "note_request_failed",
            "fb": "note_request_fallback",
            "rl": "provider_rate_limited",
            "retry": "note_request_retry",
            "json": "note_json_validation_failed",
        }.get(arg1)
        await ui.show_logs(event, event_name=event_filter)
    elif action == "us" and arg1:
        try:
            days = int(arg1)
        except ValueError:
            days = 7
        await ui.show_usage(event, days=days)
    elif action == "us":
        await ui.show_usage(event)
    elif action == "usrst" and arg1:
        await ui._usage_reset(event, arg1)
    elif action == "md" and arg1:
        await ui.show_models(event, arg1)
    elif action == "md":
        await ui.show_models(event)
    elif action == "mdep" and arg1:
        await ui._mark_deprecated(event, arg1)
    elif action == "mpaid" and arg1:
        await ui._mark_paid_billing(event, arg1)
    elif action == "mdsync" and arg1:
        await ui._sync_models(event, arg1)
    elif action == "lg" and arg1:
        await ui.show_logs(event, arg1)
    elif action == "lg":
        await ui.show_logs(event)
    elif action == "dry" and arg1:
        await ui.show_dry(event, arg1)
    elif action == "dry":
        await ui.show_dry(event)
    elif action == "plan":
        await ui.show_plan(event)
    else:
        await event.answer("دکمهٔ AI نامعتبر.", alert=True)
