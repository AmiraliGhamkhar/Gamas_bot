"""Wallet, card-to-card payment orders, receipt verification, and retention loops (SRP).

Encapsulates subscription purchase screens, order lifecycle, JPEG receipt
normalization, administrator two-step payment review, and receipt/transcript
retention sweeps.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from pathlib import Path
import secrets
import sqlite3
from typing import Any

from telethon import Button

from .billing import format_duration, format_toman, format_validity
from .bot_ui import TELEGRAM_CAPTION_LIMIT
from .config import format_payment_card
from .db import OpenPaymentError, PaymentSnapshotError
from .fileio import run_file_io
from .intake import (
    _is_receipt_image,
    _is_safe_receipt_path,
    _normalize_receipt_image,
    _sanitize_log_value,
    _to_int,
    is_open_payment,
    is_pending_payment_review,
)
from .progress import to_persian_digits
from .telegram_text import button_label
from .telegram_transfer import DiskSpaceError

logger = logging.getLogger("gamas_bot.bot.wallet")


class WalletWorkflowMixin:
    """Wallet purchase screens, receipt uploads, admin review, and retention loops."""

    db: Any
    settings: Any
    client: Any
    _pending_payments: dict[int, int]
    _pending_reject_reason: dict[int, int]

    REJECTION_REASON_PRESETS: dict[str, str] = {
        "blur": "تصویر رسید ناخوانا است؛ تصویر واضح‌تری ارسال کنید.",
        "amount": "مبلغ واریزی با مبلغ بسته مطابقت ندارد.",
        "invalid": "اطلاعات تراکنش ناقص یا تکراری است.",
    }
    TRANSCRIPT_CLEANUP_BATCHES = 25

    @staticmethod
    def _payment_status_label(status: str) -> str:
        return {
            "pending": "⏳ منتظر پرداخت",
            "awaiting_payment": "⏳ منتظر پرداخت",
            "pending_review": "🕒 در انتظار بررسی",
            "approved": "✅ تأیید شده",
            "rejected": "❌ رد شده",
            "cancelled": "لغو شده",
        }.get(status, "نامشخص")

    async def _show_wallet(self, event: Any, *, payments_page: int = 0) -> None:
        """One scrollable purchase view: plans, then balance, then history."""
        user_id = event.sender_id
        if user_id is None:
            return
        page_size = 5
        payments_page = max(0, int(payments_page))
        lines = [
            "🛒 خرید اشتراک",
            "",
        ]
        buttons: list[list] = []

        latest = await self.db.payment_history_paginated(user_id, limit=1)
        if latest and is_open_payment(latest[0]["status"]):
            payment = latest[0]
            order_buttons = [
                Button.inline(
                    "🔎 پیگیری سفارش باز",
                    f"payopen:{payment['payment_id']}".encode("ascii"),
                )
            ]
            if not payment["receipt_path"]:
                order_buttons.append(
                    Button.inline(
                        "لغو سفارش",
                        f"payc:{payment['payment_id']}".encode("ascii"),
                    )
                )
            buttons.append(order_buttons)

        plans = [plan for plan in await self.db.plans() if plan["price"] > 0]
        if not plans:
            lines.append("فعلاً بسته‌ای برای خرید موجود نیست.")
        else:
            lines.append("بسته را انتخاب کنید؛ اعتبار خرید به موجودی شما اضافه می‌شود.")
            for plan in plans:
                hours = float(plan["hours"])
                hours_text = to_persian_digits(
                    int(hours) if hours.is_integer() else str(hours)
                )
                duration = format_duration(int(round(hours * 3600)))
                validity = format_validity(plan["validity_days"])
                lines.append(
                    f"\n• {duration}\n  اعتبار: {validity} | مبلغ: {format_toman(plan['price'])}"
                )
                if self.settings.payment_card_number:
                    label = button_label(
                        f"{hours_text} ساعت — {format_toman(plan['price'])}"
                    )
                    buttons.append(
                        [Button.inline(label, f"plan:{plan['code']}".encode("utf-8"))]
                    )
            if not self.settings.payment_card_number:
                lines.append("\n🛒 پرداخت کارت‌به‌کارت فعلاً غیرفعال است.")

        lines.extend(
            [
                "",
                f"💳 اعتبار باقی‌مانده: {await self._balance_text(user_id)}",  # type: ignore[attr-defined]
                "",
                "🧾 پرداخت‌های اخیر",
            ]
        )
        payments = await self.db.payment_history_paginated(
            user_id, limit=page_size + 1, offset=payments_page * page_size
        )
        has_more = len(payments) > page_size
        payments = payments[:page_size]
        if not payments:
            lines.append(
                "پرداختی ثبت نشده است."
                if payments_page == 0
                else "مورد دیگری وجود ندارد."
            )
        for item in payments:
            seconds = item.get("purchased_seconds")
            plan_text = format_duration(int(seconds)) if seconds else item["plan_code"]
            price = item.get("purchased_price")
            amount = format_toman(int(price)) if price is not None else "مبلغ نامشخص"
            lines.append(
                f"\n#{to_persian_digits(item['payment_id'])} | {plan_text} | {amount}\n"
                f"{self._payment_status_label(item['status'])}"
            )
            if item["status"] == "rejected" and item.get("review_reason"):
                lines.append(f"دلیل: {item['review_reason']}")

        nav = []
        if payments_page > 0:
            nav.append(
                Button.inline("◀️ قبلی", f"walletp:{payments_page - 1}".encode("ascii"))
            )
        if has_more:
            nav.append(
                Button.inline("بعدی ▶️", f"walletp:{payments_page + 1}".encode("ascii"))
            )
        if nav:
            buttons.append(nav)
        buttons.extend(
            [
                [Button.inline("🎧 پشتیبانی", b"support")],
                [Button.inline("↩️ منوی اصلی", b"menu:main")],
            ]
        )
        await self._edit_or_reply(event, "\n".join(lines), buttons)  # type: ignore[attr-defined]

    async def _start_payment(self, event: Any, plan_code: str) -> None:
        if not self.settings.payment_card_number:
            await event.respond(
                "🛒 پرداخت کارت‌به‌کارت فعلاً فعال نیست؛ کمی بعد دوباره امتحان کنید."
            )
            return
        try:
            payment_id = await self.db.create_payment(event.sender_id, plan_code)
        except OpenPaymentError:
            await event.respond(
                "⚠️ یک سفارش باز دارید؛ اول آن را پیگیری یا لغو کنید.",
                buttons=[[Button.inline("🛒 خرید اشتراک", b"wallet")]],
            )
            return
        except ValueError:
            await event.respond("این طرح در دسترس نیست.")
            return
        await self._show_payment(event, payment_id)

    async def _show_payment(self, event: Any, payment_id: int) -> None:
        payment = await self.db.payment(payment_id)
        if (
            payment is None
            or payment["user_id"] != event.sender_id
            or not is_open_payment(payment["status"])
        ):
            await event.respond(
                "این سفارش فعال نیست. وضعیت را در «🛒 خرید اشتراک» ببینید.",
                buttons=[[Button.inline("🛒 خرید اشتراک", b"wallet")]],
            )
            return
        if not payment["purchased_seconds"] or payment["purchased_price"] is None:
            await event.respond(
                "اطلاعات این سفارش ناقص است؛ لطفاً درخواست پشتیبانی ثبت کنید.",
                buttons=[[Button.inline("🎧 پشتیبانی", b"support")]],
            )
            return
        plan = {
            "hours": payment["purchased_seconds"] / 3600,
            "price": payment["purchased_price"],
            "validity_days": payment["purchased_validity_days"],
        }
        hours = plan["hours"]
        hours_text = to_persian_digits(
            int(hours) if float(hours).is_integer() else hours
        )
        order_ref = to_persian_digits(payment_id)
        summary = (
            f"🧾 سفارش {order_ref}\n"
            f"بسته: {hours_text} ساعت | مبلغ: {format_toman(plan['price'])}\n"
            f"اعتبار بسته: {format_validity(plan['validity_days'])}"
        )
        if payment["receipt_path"]:
            self._pending_payments.pop(event.sender_id, None)
            text = (
                f"{summary}\n\n"
                "🕒 رسید پرداخت شما ثبت شد و در انتظار بررسی است.\n"
                "نیازی به واریز مجدد نیست؛ پس از بررسی مدیر، نتیجه اطلاع‌رسانی می‌شود."
            )
            buttons = []
        elif not self.settings.payment_card_number:
            self._pending_payments.pop(event.sender_id, None)
            text = (
                f"{summary}\n\n"
                "پرداخت کارت‌به‌کارت فعلاً در دسترس نیست؛ سفارش را لغو کنید یا بعداً امتحان کنید."
            )
            buttons = [[Button.inline("لغو سفارش", f"payc:{payment_id}".encode())]]
        else:
            self._pending_payments[event.sender_id] = payment_id
            card = format_payment_card(self.settings.payment_card_number)
            text = (
                f"{summary}\n\n"
                "اطلاعات واریز:\n"
                f"💳 شماره کارت: {card}\n"
                f"به نام: {self.settings.payment_card_holder}\n"
                f"بانک: {self.settings.payment_bank_name}\n\n"
                "پس از واریز، تصویر رسید را بفرستید. اعتبار فقط پس از تأیید مدیر اضافه می‌شود."
            )
            buttons = [
                [Button.inline("📤 ارسال رسید", f"payr:{payment_id}".encode())],
                [Button.inline("لغو سفارش", f"payc:{payment_id}".encode())],
            ]
        buttons.append([Button.inline("🛒 خرید اشتراک", b"wallet")])
        await event.respond(text, buttons=buttons)

    async def _cancel_payment(self, event: Any, payment_id: int) -> None:
        changed = await self.db.cancel_payment(payment_id, event.sender_id)
        self._pending_payments.pop(event.sender_id, None)
        await event.respond(
            "سفارش لغو شد."
            if changed
            else "این سفارش قابل لغو نیست؛ رسید ارسال‌شده باید توسط مدیر بررسی شود."
        )
        await self._show_wallet(event)

    async def _await_receipt(self, event: Any, payment_id: int) -> None:
        payment = await self.db.payment(payment_id)
        if (
            payment is None
            or payment["status"] not in ("awaiting_payment", "pending")
            or payment["receipt_path"]
            or payment["user_id"] != event.sender_id
        ):
            await event.respond("این سفارش برای ارسال رسید فعال نیست.")
            return
        self._pending_payments[event.sender_id] = payment_id
        await event.respond(
            "📷 تصویر خوانای رسید را به‌صورت عکس یا فایل تصویری بفرستید.",
            buttons=[[Button.inline("↩️ بازگشت", f"payopen:{payment_id}".encode())]],
        )

    async def _handle_receipt_upload(
        self, event: Any, user_id: int, *, payment_id: int | None = None
    ) -> None:
        await self._guard_file_intake(  # type: ignore[attr-defined]
            event,
            lambda: self._handle_receipt_upload_inner(
                event, user_id, payment_id=payment_id
            ),
        )

    async def _handle_receipt_upload_inner(
        self, event: Any, user_id: int, *, payment_id: int | None = None
    ) -> None:
        if payment_id is None:
            payment_id = self._pending_payments.pop(user_id, None)
        else:
            self._pending_payments.pop(user_id, None)
        if payment_id is None:
            return
        payment = await self.db.payment(payment_id)
        if (
            payment is None
            or payment["status"] not in ("awaiting_payment", "pending")
            or payment["receipt_path"]
            or payment["user_id"] != user_id
        ):
            self._pending_payments.pop(user_id, None)
            await event.respond("این سفارش برای ارسال رسید فعال نیست.")
            return
        if not _is_receipt_image(event.message):
            self._pending_payments[user_id] = payment_id
            await event.respond("رسید باید عکس یا فایل تصویری JPEG/PNG/WebP باشد.")
            return
        size = getattr(event.message.file, "size", None)
        if isinstance(size, int) and size > self.settings.max_payment_receipt_bytes:
            await event.respond("حجم تصویر رسید زیاد است؛ فشرده‌تر بفرستید.")
            self._pending_payments[user_id] = payment_id
            return
        await run_file_io(self.settings.receipt_dir.mkdir, parents=True, exist_ok=True)
        token = secrets.token_hex(8)
        receipt_path = self.settings.receipt_dir / f"receipt-{payment_id}-{token}.jpg"
        committed = False
        try:
            try:
                await self._download_telegram_media(  # type: ignore[attr-defined]
                    event.message, receipt_path, self.settings.max_payment_receipt_bytes
                )
            except DiskSpaceError:
                self._pending_payments[user_id] = payment_id
                await event.respond(
                    "فضای موقت سرور کافی نیست؛ رسید را کمی بعد دوباره بفرستید."
                )
                return
            except ValueError:
                self._pending_payments[user_id] = payment_id
                await event.respond("حجم تصویر رسید زیاد است؛ فشرده‌تر بفرستید.")
                return
            except Exception:
                self._pending_payments[user_id] = payment_id
                await event.respond("دریافت رسید ناموفق بود؛ لطفاً دوباره ارسال کنید.")
                return
            try:
                if not await run_file_io(
                    _normalize_receipt_image,
                    receipt_path,
                    max_bytes=self.settings.max_payment_receipt_bytes,
                ):
                    await run_file_io(receipt_path.unlink, missing_ok=True)
                    await event.respond(
                        "فرمت رسید نامعتبر است؛ فقط JPEG/PNG/WebP پذیرفته می‌شود."
                    )
                    self._pending_payments[user_id] = payment_id
                    return
                await run_file_io(receipt_path.chmod, 0o600)
            except OSError:
                await run_file_io(receipt_path.unlink, missing_ok=True)
                self._pending_payments[user_id] = payment_id
                await event.respond("ذخیرهٔ رسید ناموفق بود؛ لطفاً دوباره ارسال کنید.")
                return
            try:
                attach = asyncio.create_task(
                    self.db.set_payment_receipt(
                        payment_id, str(receipt_path), user_id=user_id
                    )
                )
                try:
                    changed = await asyncio.shield(attach)
                except asyncio.CancelledError:
                    try:
                        committed = await attach
                    except Exception:
                        logger.debug(
                            "cancelled receipt attachment failed", exc_info=True
                        )
                    raise
            except sqlite3.Error:
                await run_file_io(receipt_path.unlink, missing_ok=True)
                self._pending_payments[user_id] = payment_id
                logger.exception(
                    "payment receipt storage failed payment_id=%s", payment_id
                )
                await event.respond("ثبت رسید انجام نشد؛ لطفاً دوباره ارسال کنید.")
                return
            if not changed:
                await run_file_io(receipt_path.unlink, missing_ok=True)
                await event.respond("این سفارش دیگر برای ارسال رسید فعال نیست.")
                return
            committed = True
            try:
                await event.respond(
                    "🕒 رسید پرداخت شما ثبت شد و در انتظار بررسی است.\n"
                    "پس از بررسی مدیر، نتیجه اطلاع‌رسانی می‌شود."
                )
            except Exception:
                logger.warning(
                    "receipt confirmation not delivered payment_id=%s", payment_id
                )
            await self._notify_admins_of_payment(payment_id)
        finally:
            if not committed:
                try:
                    await run_file_io(receipt_path.unlink, missing_ok=True)
                except OSError:
                    logger.warning(
                        "could not remove incomplete receipt payment_id=%s", payment_id
                    )

    async def _notify_admins_of_payment(self, payment_id: int) -> None:
        payment = await self.db.payment(payment_id)
        if payment is None:
            return
        seconds = int(payment.get("purchased_seconds") or 0)
        price = payment.get("purchased_price")
        owner = await self._identity_for_id(int(payment["user_id"]))  # type: ignore[attr-defined]
        text = (
            f"🧾 رسید سفارش {to_persian_digits(payment_id)}\n"
            f"کاربر: {owner}\n"
            f"بسته: {format_duration(seconds) if seconds else payment['plan_code']}\n"
            f"مبلغ: {format_toman(int(price)) if price is not None else 'نامشخص'}"
        )
        buttons = [
            [Button.inline("🔎 جزئیات سفارش", f"adm:pd:{payment_id}".encode())]
        ]
        path = payment.get("receipt_path")
        receipt_ready = bool(
            path
            and await run_file_io(
                _is_safe_receipt_path, self.settings.receipt_dir, path
            )
        )
        for admin_id in sorted(self.settings.admin_ids):
            try:
                if receipt_ready:
                    try:
                        await self._send_telegram_file(  # type: ignore[attr-defined]
                            admin_id,
                            Path(str(path)),
                            caption=text[:TELEGRAM_CAPTION_LIMIT],
                            buttons=buttons,
                        )
                    except Exception:
                        logger.warning(
                            "payment receipt delivery failed payment_id=%s",
                            payment_id,
                        )
                        await self.client.send_message(
                            admin_id,
                            text
                            + "\n⚠️ رسید تصویری ارسال نشد؛ آن را از جزئیات سفارش ببینید.",
                            buttons=buttons,
                        )
                else:
                    await self.client.send_message(
                        admin_id,
                        text + "\n⚠️ فایل رسید در دسترس نیست.",
                        buttons=buttons,
                    )
            except Exception:
                logger.warning(
                    "payment notification not delivered payment_id=%s",
                    payment_id,
                )

    async def _review_payment(self, event: Any, data: str) -> None:
        """Handle payv:<id>:a, payv:<id>:a:yes, payv:<id>:r and payv:<id>:rp:<code> safely."""
        parts = data.split(":")
        if (
            len(parts) not in (3, 4)
            or parts[0] != "payv"
            or parts[2] not in ("a", "r", "rp")
            or (len(parts) == 4 and parts[2] == "a" and parts[3] != "yes")
            or (
                len(parts) == 4
                and parts[2] == "rp"
                and parts[3] not in self.REJECTION_REASON_PRESETS
            )
            or (len(parts) == 3 and parts[2] == "rp")
            or (len(parts) == 4 and parts[2] == "r")
        ):
            await self._ack(event, "درخواست نامعتبر است.", alert=True)  # type: ignore[attr-defined]
            return
        payment_id = _to_int(parts[1])
        if payment_id is None:
            await self._ack(event, "شناسه سفارش نامعتبر است.", alert=True)  # type: ignore[attr-defined]
            return
        verdict = parts[2]
        payment = await self.db.payment(payment_id)
        if payment is None or not is_pending_payment_review(payment):
            await self._ack(  # type: ignore[attr-defined]
                event,
                "سفارش قابل بررسی نیست؛ ابتدا رسید باید ثبت شود یا سفارش قبلاً بررسی شده است.",
                alert=True,
            )
            await self._show_admin_payment_detail(event, payment_id)  # type: ignore[attr-defined]
            return
        if verdict == "rp":
            await self._ack(event)  # type: ignore[attr-defined]
            preset_reason = self.REJECTION_REASON_PRESETS[parts[3]]
            await self._reject_payment_with_reason(event, payment_id, preset_reason)
            return
        if verdict == "r":
            self._pending_reject_reason[event.sender_id] = payment_id
            await self._ack(event, "برای رد پرداخت، دلیل لازم است.", alert=True)  # type: ignore[attr-defined]
            await event.respond(
                f"دلیل رد سفارش {to_persian_digits(payment_id)} را انتخاب کنید یا در یک پیام بنویسید.\n"
                "برای لغو، /cancel بفرستید.",
                buttons=[
                    [
                        Button.inline(
                            "📷 تصویر رسید ناخوانا",
                            f"payv:{payment_id}:rp:blur".encode(),
                        )
                    ],
                    [
                        Button.inline(
                            "💰 عدم تطابق مبلغ",
                            f"payv:{payment_id}:rp:amount".encode(),
                        )
                    ],
                    [
                        Button.inline(
                            "⚠️ اطلاعات ناقص یا تکراری",
                            f"payv:{payment_id}:rp:invalid".encode(),
                        )
                    ],
                    [Button.inline("↩️ بازگشت", f"adm:pd:{payment_id}".encode())],
                ],
            )
            return
        if len(parts) == 3:
            await self._ack(event)  # type: ignore[attr-defined]
            seconds = int(payment.get("purchased_seconds") or 0)
            price = payment.get("purchased_price")
            owner = await self._identity_for_id(int(payment["user_id"]))  # type: ignore[attr-defined]
            await self._edit_or_reply(  # type: ignore[attr-defined]
                event,
                f"✅ تأیید سفارش {to_persian_digits(payment_id)}\n\n"
                f"کاربر: {owner}\n"
                f"اعتبار: {format_duration(seconds)}\n"
                f"مبلغ: {format_toman(int(price)) if price is not None else 'نامشخص'}\n\n"
                "پس از تأیید نهایی، اعتبار به حساب کاربر اضافه می‌شود.",
                [
                    [
                        Button.inline(
                            "✅ تأیید نهایی", f"payv:{payment_id}:a:yes".encode()
                        )
                    ],
                    [Button.inline("↩️ بازگشت", f"adm:pd:{payment_id}".encode())],
                ],
            )
            return
        try:
            changed = await self.db.review_payment(payment_id, True, event.sender_id)
        except PaymentSnapshotError:
            logger.error(
                "payment approval without a valid snapshot payment_id=%s", payment_id
            )
            await self._ack(  # type: ignore[attr-defined]
                event,
                "اطلاعات بسته یا مبلغ ناقص است؛ تأیید انجام نشد.",
                alert=True,
            )
            return
        except sqlite3.Error:
            logger.exception("payment activation failed payment_id=%s", payment_id)
            await self._ack(  # type: ignore[attr-defined]
                event,
                "فعال‌سازی انجام نشد. سفارش همچنان در انتظار بررسی است؛ بعداً دوباره تلاش کنید.",
                alert=True,
            )
            return
        if not changed:
            await self._ack(event, "این سفارش قبلاً بررسی شده است.", alert=True)  # type: ignore[attr-defined]
        else:
            await self._ack(event, "پرداخت تأیید و اعتبار فعال شد.")  # type: ignore[attr-defined]
            seconds = int(payment["purchased_seconds"] or 0)
            try:
                await self.client.send_message(
                    payment["user_id"],
                    "✅ پرداخت شما تأیید شد. اشتراک شما فعال است.\n"
                    f"💳 {format_duration(seconds)} اعتبار اضافه شد.",
                    buttons=[[Button.inline("🛒 خرید اشتراک", b"wallet")]],
                )
            except Exception:
                logger.warning(
                    "payment approval notification not delivered payment_id=%s",
                    payment_id,
                )
        await self._show_admin_payment_detail(event, payment_id)  # type: ignore[attr-defined]

    async def _reject_payment_with_reason(
        self, event: Any, payment_id: int, reason: str
    ) -> None:
        send_reply = event.reply if hasattr(event, "reply") else event.respond
        reason = reason.strip()
        if not reason:
            await send_reply(
                "برای رد پرداخت، واردکردن دلیل الزامی است.",
                buttons=self._admin_menu(),  # type: ignore[attr-defined]
            )
            return
        payment = await self.db.payment(payment_id)
        try:
            changed = await self.db.review_payment(
                payment_id, False, event.sender_id, reason=reason
            )
        except sqlite3.Error:
            logger.exception("payment rejection failed payment_id=%s", payment_id)
            await send_reply("رد پرداخت ثبت نشد؛ لطفاً دوباره تلاش کنید.")
            self._pending_reject_reason[event.sender_id] = payment_id
            return
        if not changed:
            await send_reply("این سفارش قبلاً بررسی شده است یا رسید ندارد.")
        else:
            await send_reply(f"❌ سفارش {to_persian_digits(payment_id)} رد شد.")
            if payment is not None:
                try:
                    await self.client.send_message(
                        payment["user_id"],
                        f"❌ پرداخت تأیید نشد.\nدلیل: {reason[:400]}\n"
                        "برای پیگیری می‌توانید درخواست پشتیبانی ثبت کنید.",
                        buttons=[[Button.inline("🎧 پشتیبانی", b"support")]],
                    )
                except Exception:
                    logger.warning(
                        "payment rejection notification not delivered payment_id=%s",
                        payment_id,
                    )
        await self._show_admin_payment_detail(event, payment_id)  # type: ignore[attr-defined]

    async def _receipt_cleanup_loop(self) -> None:
        """Delete reviewed receipts after the retention window."""
        while True:
            try:
                await self._cleanup_expired_receipts()
                await asyncio.sleep(24 * 3600)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("receipt cleanup failed", exc_info=True)

    async def _transcript_cleanup_loop(self) -> None:
        """Delete finished transcripts after the retention window (audit F-10)."""
        if self.settings.transcript_retention_days <= 0:
            return
        while True:
            try:
                await self._cleanup_expired_transcripts()
                await asyncio.sleep(24 * 3600)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("transcript cleanup failed", exc_info=True)

    async def _cleanup_expired_transcripts(self) -> int:
        """Delete expired transcript rows in bounded batches; returns the count."""
        days = self.settings.transcript_retention_days
        if days <= 0:
            return 0
        removed = 0
        for _ in range(self.TRANSCRIPT_CLEANUP_BATCHES):
            batch = await self.db.expire_transcriptions(older_than_days=days)
            removed += batch
            if batch == 0:
                break
        if removed:
            await self.db.audit(
                0, "transcript_expired", f"deleted:{removed} days:{days}"
            )
            logger.info(
                "removed %d expired transcriptions (retention %d days)", removed, days
            )
        return removed

    async def _cleanup_expired_receipts(self) -> None:
        if not await run_file_io(self.settings.receipt_dir.is_dir):
            return
        cutoff = datetime.now() - timedelta(days=self.settings.receipt_retention_days)
        protected = {
            payment["receipt_path"]
            for payment in await self.db.pending_payments()
            if payment.get("receipt_path")
        }
        removed: list[str] = []
        paths = await run_file_io(
            lambda: list(self.settings.receipt_dir.glob("receipt-*.jpg"))
        )
        for path in paths:
            try:
                if str(path) in protected:
                    continue
                if not await run_file_io(
                    _is_safe_receipt_path, self.settings.receipt_dir, path
                ):
                    logger.warning(
                        "skipping unsafe receipt path=%s", _sanitize_log_value(path)
                    )
                    continue
                if (
                    datetime.fromtimestamp((await run_file_io(path.stat)).st_mtime)
                    < cutoff
                ):
                    await run_file_io(path.unlink, missing_ok=True)
                    removed.append(str(path))
            except OSError:
                continue
        if removed:
            cleared = await self.db.forget_receipts(removed)
            logger.info(
                "removed %d expired receipts (%d payment references cleared)",
                len(removed),
                cleared,
            )
