"""Telegram admin panels for the AI Provider Platform.

UI-facing companion of ``gamas_bot.ai.*``: provider cards, key wizard,
routing, usage, model catalog, logs and dry-run request previews. All text is
Persian to match the existing admin panel; technical ids stay ASCII. Nothing
here ever renders raw keys, prompts or transcripts.
"""

from __future__ import annotations

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
from .provider_health import cooldown_remaining_seconds

SERVICE = "notes"
TASK = "chunk_structuring"

_CLASS_FA = {
    ProviderClass.PERMANENT_FREE: "رایگان دائمی",
    ProviderClass.FREE_PLAN: "پلن رایگان",
    ProviderClass.PROMOTIONAL_FREE: "رایگان تبلیغاتی (موقت)",
    ProviderClass.TRIAL_ONLY: "فقط آزمایشی (trial)",
    ProviderClass.PAID_ONLY: "فقط پولی",
    ProviderClass.REGION_RESTRICTED: "محلودیت منطقه",
    ProviderClass.UNAVAILABLE: "نامعتبر/نافع",
    ProviderClass.PAID_ONLY: "فقط پولی",
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
            [Button.inline("📈 نمای مسیر و بودجه", b"admin:ai:plan")],
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
                    Button.inline("🧾 لاگ‌ها", f"admin:ai:lg:{slug}".encode("ascii")),
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
        buttons: list[list] = []
        if not rows:
            lines.append("کلیدی ثبت نشده است؛ از «افزودن کلید» شروع کنید.")
        for item in rows:
            state = "فعال" if item["enabled"] else "غیرفعال"
            if item["quarantined_at"]:
                state = "قرنطینه (401/403)"
            elif cooldown_remaining_seconds(item.get("cooldown_until")) > 0:
                state = "cooldown"
            last = f" | HTTP {item['last_status_code']}" if item["last_status_code"] else ""
            lines.append(
                f"• #{item['id']} {item['label']} "
                f"{self.bot._masked_key(item['secret_last4'])} — {state}{last}"
            )
            toggle = "disable" if item["enabled"] else "enable"
            buttons.append(
                [
                    Button.inline(
                        f"{'غیرفعال' if toggle == 'disable' else 'فعال'} #{item['id']}",
                        f"admin:ai:kstate:{toggle}:{item['id']}".encode("ascii"),
                    ),
                    Button.inline("❌ حذف", f"admin:ai:kstate:delete:{item['id']}".encode("ascii")),
                ]
            )
            buttons.append(
                [
                    Button.inline("▲", f"admin:ai:kstate:up:{item['id']}".encode("ascii")),
                    Button.inline("▼", f"admin:ai:kstate:down:{item['id']}".encode("ascii")),
                    Button.inline("🧪 تست", f"admin:ai:keytest:{item['id']}".encode("ascii")),
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
        else:
            changed = False
        if changed:
            self.bot.provider_health.invalidate()
        await event.answer("ذخیره شد." if changed else "تغییری ثبت نشد.")
        await self.show_provider_keys(event, slug)

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
        choices = static_models(slug)[:6]
        lines = [
            f"🧭 راهنمای افزودن کلید برای {info.display_name}",
            "",
            f"۱) کلید را از کنسول ارائه‌دهنده بسازید: {info.docs_url}",
            "۲) مدل پیش‌فرض این کلید را انتخاب کنید (اختیاری — بدون انتخاب، مدل خودکار تعیین می‌شود):",
        ]
        buttons = [
            [
                Button.inline(
                    m.model_id[:28], f"admin:ai:kmdl:{slug}:{index}".encode("ascii")
                )
            ]
            for index, m in enumerate(choices)
        ]
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
            choices = static_models(slug)
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
        }
        bot._pending_admin_actions[admin_id] = "aikey_label"
        await self.bot._edit_callback(
            event,
            "۳) یک برچسب کوتاه برای کلید بنویسید و بفرستید (مثلاً «کلید اصلی تیم»).\n"
            + (f"مدل انتخاب‌شده: {model}\n" if model else "")
            + "سپس در قدم بعد کلید را می‌فرستید؛ پیام کلید بلافاصله حذف می‌شود.",
            [[Button.inline("لغو", f"admin:ai:ky:{slug}".encode("ascii"))]],
        )

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
    async def show_usage(self, event) -> None:
        db = self.bot.db
        rows = await db.ai_usage_summary(days=7)
        lines = ["📊 مصرف ۷ روز گذشته", ""]
        buttons: list[list] = []
        if not rows:
            lines.append("هنوز مصرفی ثبت نشده است.")
        totals: dict[str, dict] = {}
        for row in rows:
            provider = str(row["provider"])
            agg = totals.setdefault(
                provider,
                {"requests": 0, "successes": 0, "failures": 0, "rl": 0, "fb": 0, "paid": 0},
            )
            agg["requests"] += int(row["requests"] or 0)
            agg["successes"] += int(row["successes"] or 0)
            agg["failures"] += int(row["failures"] or 0)
            agg["rl"] += int(row["rate_limit_hits"] or 0)
            agg["fb"] += int(row["fallbacks"] or 0)
            agg["paid"] += int(row["paid_block_events"] or 0)
        for provider, agg in sorted(totals.items()):
            lines.append(
                f"• {provider}: {_fmt_int(agg['requests'])} درخواست"
                f" | موفق {_fmt_int(agg['successes'])}"
                f" | 429/quota {_fmt_int(agg['rl'])}"
                f" | fallback {_fmt_int(agg['fb'])}"
                + (f" | بلاک پولی ⚠️ {_fmt_int(agg['paid'])}" if agg["paid"] else "")
            )
            buttons.append(
                [
                    Button.inline(
                        f"🔄 بازنشانی شمارندهٔ امروز {provider[:18]}",
                        f"admin:ai:usrst:{provider}".encode("ascii", "ignore")[:64],
                    )
                ]
            )
        buttons.append([Button.inline("↩️ پلتفرم AI", b"admin:ai")])
        await self.bot._edit_callback(event, "\n".join(lines), buttons)

    async def _usage_reset(self, event, provider: str) -> None:
        admin_id = int((await event.get_sender()).id)
        deleted = await self.bot.db.ai_usage_daily_delete_provider_day(provider)
        await self.bot.db.add_audit_entry(
            admin_id=admin_id,
            action="ai_usage_reset",
            target_type="ai_provider",
            target_id=provider,
            details={"rows": deleted},
        )
        await event.answer(f"شمارندهٔ امروز {provider} بازنشانی شد ({deleted} ردیف).")
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
            free_status = str(row.get("free_status") or "unknown")
            lines.append(
                f"• #{row['id']} {row['model']} — {free_status}"
                + (" (" + ", ".join(status) + ")" if status else "")
            )
            if not row.get("deprecated") and row.get("available", 1):
                buttons.append(
                    [
                        Button.inline(
                            f"🗂 علامت‌گذاری منسوخ #{row['id']}",
                            f"admin:ai:mdep:{row['id']}".encode("ascii"),
                        )
                    ]
                )
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
    async def show_logs(self, event, slug: str | None = None) -> None:
        db = self.bot.db
        events = await db.ai_events_list(limit=12, provider=slug)
        lines = ["🧾 آخرین رویدادهای AI" + (f" — {slug}" if slug else ""), ""]
        if not events:
            lines.append("رویدادی ثبت نشده است.")
        for row in events:
            extra = ""
            if row.get("http_status"):
                extra += f" HTTP {row['http_status']}"
            if row.get("latency_ms"):
                extra += f" {row['latency_ms']}ms"
            if row.get("error_class"):
                extra += f" [{row['error_class']}]"
            lines.append(
                f"• {str(row['created_at'])[:16]} {row['event']}"
                f" {row.get('provider') or '-'}"
                + extra
            )
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
