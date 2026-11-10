"""Wallet, card-to-card payment orders, receipt verification, and retention loops (SRP).

Encapsulates subscription purchase screens, order lifecycle, JPEG receipt
normalization, administrator two-step payment review, and receipt/transcript
retention sweeps.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import secrets
import sqlite3
from typing import Any

from telethon import Button

from . import texts
from .doc_extract import sha256_file
from .metrics import METRICS
from .billing import (
    format_credits_number,
    format_credits_with_hours,
    format_seconds_as_credits,
    format_toman,
    format_validity,
    seconds_to_credits,
)
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
    # Supplied by the composed bot.
    deps: Any
    _edit_or_reply: Any

    REJECTION_REASON_PRESETS: dict[str, str] = texts.RECEIPT_REJECT_REASONS
    TRANSCRIPT_CLEANUP_BATCHES = 25

    @staticmethod
    def _payment_status_label(status: str) -> str:
        return texts.PAYMENT_STATUS_LABELS.get(status, texts.PAYMENT_STATUS_UNKNOWN)

    async def _show_wallet(self, event: Any, *, payments_page: int = 0) -> None:
        """One scrollable purchase view: plans, then balance, then history."""
        user_id = event.sender_id
        if user_id is None:
            return
        page_size = 5
        payments_page = max(0, int(payments_page))
        lines = [
            texts.WALLET_TITLE,
            "",
        ]
        buttons: list[list] = []

        payment = await self.db.open_payment_for_user(user_id)
        if payment is not None:
            order_buttons = [
                Button.inline(
                    texts.BTN_TRACK_OPEN_ORDER,
                    f"payopen:{payment['payment_id']}".encode("ascii"),
                )
            ]
            if payment["status"] in ("awaiting_payment", "pending") and not payment["receipt_path"]:
                order_buttons.append(
                    Button.inline(
                        texts.BTN_CANCEL_ORDER,
                        f"payc:{payment['payment_id']}".encode("ascii"),
                    )
                )
            buttons.append(order_buttons)

        plans = [plan for plan in await self.db.plans() if plan["price"] > 0]
        if not plans:
            lines.append(texts.WALLET_NO_PLANS)
        else:
            lines.append(texts.WALLET_CHOOSE_PLAN)
            for plan in plans:
                hours = float(plan["hours"])
                credits = seconds_to_credits(hours * 3600)
                validity = format_validity(plan["validity_days"])
                lines.append(
                    "\n"
                    + texts.WALLET_PLAN_ROW_TEMPLATE.format(
                        credits=format_credits_with_hours(credits, hours),
                        validity=validity,
                        price=format_toman(plan["price"]),
                    )
                )
                if self.settings.payment_card_number:
                    label = button_label(
                        texts.WALLET_PLAN_BUTTON_TEMPLATE.format(
                            credits=texts.WALLET_PLAN_BUTTON_CREDITS.format(
                                credits=format_credits_number(credits)
                            ),
                            price=format_toman(plan["price"]),
                        )
                    )
                    buttons.append(
                        [Button.inline(label, f"plan:{plan['code']}".encode("utf-8"))]
                    )
            if not self.settings.payment_card_number:
                lines.append("\n" + texts.WALLET_CARD_DISABLED)

        lines.extend(
            [
                "",
                texts.WALLET_BALANCE_TEMPLATE.format(
                    balance=await self._balance_text(user_id)  # type: ignore[attr-defined]
                ),
                texts.WALLET_UNIT_HINT,
                "",
                texts.WALLET_RECENT_PAYMENTS,
            ]
        )
        count = await self.db.count_user_payments(user_id)
        payments_page = min(payments_page, max(0, (count - 1) // page_size))
        payments = await self.db.payment_history_paginated(
            user_id, limit=page_size + 1, offset=payments_page * page_size
        )
        has_more = len(payments) > page_size
        payments = payments[:page_size]
        if not payments:
            lines.append(
                texts.WALLET_NO_PAYMENTS
                if payments_page == 0
                else texts.WALLET_NO_MORE_PAYMENTS
            )
        for item in payments:
            seconds = item.get("purchased_seconds")
            plan_text = (
                format_seconds_as_credits(int(seconds)) if seconds else item["plan_code"]
            )
            price = item.get("purchased_price")
            amount = (
                format_toman(int(price)) if price is not None else texts.UNKNOWN_AMOUNT
            )
            lines.append(
                f"\n#{to_persian_digits(item['payment_id'])} | {plan_text} | {amount}\n"
                f"{self._payment_status_label(item['status'])}"
            )
            if item["status"] == "rejected" and item.get("review_reason"):
                lines.append(
                    texts.REASON_PREFIX_TEMPLATE.format(reason=item["review_reason"])
                )

        nav = []
        if payments_page > 0:
            nav.append(
                Button.inline(texts.BTN_PREV, f"walletp:{payments_page - 1}".encode("ascii"))
            )
        if has_more:
            nav.append(
                Button.inline(texts.BTN_NEXT, f"walletp:{payments_page + 1}".encode("ascii"))
            )
        if nav:
            buttons.append(nav)
        buttons.extend(
            [
                [Button.inline(texts.BTN_SUPPORT, b"support")],
                [Button.inline(texts.BTN_BACK_MAIN, b"menu:main")],
            ]
        )
        await self._edit_or_reply(event, "\n".join(lines), buttons)

    async def _start_payment(self, event: Any, plan_code: str) -> None:
        if not self.settings.payment_card_number:
            await event.respond(
                texts.WALLET_CLOSED
            )
            return
        try:
            payment_id = await self.db.create_payment(event.sender_id, plan_code)
        except OpenPaymentError:
            await event.respond(
                texts.WALLET_OPEN_ORDER,
                buttons=[[Button.inline(texts.BTN_WALLET, b"wallet")]],
            )
            return
        except ValueError:
            await event.respond(texts.PLAN_NOT_AVAILABLE)
            return
        await self._show_payment(event, payment_id)

    async def _show_payment(self, event: Any, payment_id: int, *, edit_in_place: bool = False) -> None:
        payment = await self.db.payment(payment_id)
        if (
            payment is None
            or payment["user_id"] != event.sender_id
            or not is_open_payment(payment["status"])
        ):
            await event.respond(
                texts.ORDER_NOT_ACTIVE,
                buttons=[[Button.inline(texts.BTN_WALLET, b"wallet")]],
            )
            return
        if not payment["purchased_seconds"] or payment["purchased_price"] is None:
            await event.respond(
                texts.ORDER_INCOMPLETE,
                buttons=[[Button.inline(texts.BTN_SUPPORT, b"support")]],
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
        summary = texts.ORDER_HEADER_TEMPLATE.format(
            order_ref=order_ref,
            hours=hours_text,
            price=format_toman(plan["price"]),
            validity=format_validity(plan["validity_days"]),
        )
        if payment["receipt_path"]:
            self._pending_payments.pop(event.sender_id, None)
            text = f"{summary}\n\n{texts.RECEIPT_PENDING}"
            buttons = []
        elif not self.settings.payment_card_number:
            self._pending_payments.pop(event.sender_id, None)
            text = f"{summary}\n\n{texts.CARD_UNAVAILABLE}"
            buttons = [[Button.inline(texts.BTN_CANCEL_ORDER, f"payc:{payment_id}".encode())]]
        else:
            self._pending_payments[event.sender_id] = payment_id
            card = format_payment_card(self.settings.payment_card_number)
            text = f"{summary}\n\n" + texts.CARD_DETAILS_TEMPLATE.format(
                card=card,
                holder=self.settings.payment_card_holder,
                bank=self.settings.payment_bank_name,
            )
            buttons = [
                [Button.inline(texts.BTN_SEND_RECEIPT, f"payr:{payment_id}".encode())],
                [Button.inline(texts.BTN_CANCEL_ORDER, f"payc:{payment_id}".encode())],
            ]
        buttons.append([Button.inline(texts.BTN_WALLET, b"wallet")])
        if edit_in_place:
            await self._edit_or_reply(event, text, buttons)
        else:
            await event.respond(text, buttons=buttons)

    async def _cancel_payment(self, event: Any, payment_id: int) -> None:
        changed = await self.db.cancel_payment(payment_id, event.sender_id)
        self._pending_payments.pop(event.sender_id, None)
        await event.respond(
            texts.ORDER_CANCELLED_OK
            if changed
            else texts.ORDER_NOT_CANCELLABLE
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
            await event.respond(texts.ORDER_RECEIPT_CLOSED)
            return
        self._pending_payments[event.sender_id] = payment_id
        await event.respond(
            texts.RECEIPT_ASK_IMAGE,
            buttons=[[Button.inline(texts.BTN_BACK, f"payopen:{payment_id}".encode())]],
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
            await event.respond(texts.ORDER_RECEIPT_CLOSED)
            return
        if not _is_receipt_image(event.message):
            self._pending_payments[user_id] = payment_id
            await event.respond(texts.RECEIPT_BAD_TYPE)
            return
        size = getattr(event.message.file, "size", None)
        if isinstance(size, int) and size > self.settings.max_payment_receipt_bytes:
            await event.respond(texts.RECEIPT_TOO_LARGE)
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
                await event.respond(texts.RECEIPT_NO_SPACE)
                return
            except ValueError:
                self._pending_payments[user_id] = payment_id
                await event.respond(texts.RECEIPT_TOO_LARGE)
                return
            except Exception:
                self._pending_payments[user_id] = payment_id
                await event.respond(texts.RECEIPT_DOWNLOAD_FAILED)
                return
            try:
                if not await run_file_io(
                    _normalize_receipt_image,
                    receipt_path,
                    max_bytes=self.settings.max_payment_receipt_bytes,
                ):
                    await run_file_io(receipt_path.unlink, missing_ok=True)
                    await event.respond(texts.RECEIPT_BAD_FORMAT)
                    self._pending_payments[user_id] = payment_id
                    return
                await run_file_io(receipt_path.chmod, 0o600)
                receipt_hash = await run_file_io(sha256_file, receipt_path)
            except OSError:
                await run_file_io(receipt_path.unlink, missing_ok=True)
                self._pending_payments[user_id] = payment_id
                await event.respond(texts.RECEIPT_SAVE_FAILED)
                return
            try:
                attach = asyncio.create_task(
                    self.db.set_payment_receipt(
                        payment_id, str(receipt_path), user_id=user_id, receipt_hash=receipt_hash
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
                await event.respond(texts.RECEIPT_RECORD_FAILED)
                return
            if not changed:
                await run_file_io(receipt_path.unlink, missing_ok=True)
                await event.respond(texts.RECEIPT_ORDER_CLOSED)
                return
            committed = True
            try:
                await event.respond(texts.RECEIPT_PENDING_SHORT)
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
        text = texts.ADMIN_RECEIPT_HEADER_TEMPLATE.format(
            payment_id=to_persian_digits(payment_id),
            owner=owner,
            plan=format_seconds_as_credits(seconds) if seconds else payment["plan_code"],
            price=format_toman(int(price)) if price is not None else texts.UNKNOWN_AMOUNT,
        )
        if payment.get("receipt_hash"):
            try:
                other_id = await self.db.other_payment_with_receipt_hash(payment_id, payment["receipt_hash"])
            except sqlite3.Error:
                METRICS.record_receipt_hash_error()
                logger.warning("receipt duplicate lookup failed payment_id=%s", payment_id)
                other_id = None
            if other_id is not None:
                text += "\n" + texts.ADMIN_RECEIPT_DUPLICATE.format(
                    payment_id=to_persian_digits(other_id)
                )
        buttons = [
            [Button.inline(texts.BTN_ORDER_DETAILS, f"adm:pd:{payment_id}".encode())]
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
                            text + texts.ADMIN_RECEIPT_MISSING_IMAGE,
                            buttons=buttons,
                        )
                else:
                    await self.client.send_message(
                        admin_id,
                        text + texts.ADMIN_RECEIPT_MISSING_FILE,
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
            await self._ack(event, texts.INVALID_REQUEST, alert=True)  # type: ignore[attr-defined]
            return
        payment_id = _to_int(parts[1])
        if payment_id is None:
            await self._ack(  # type: ignore[attr-defined]
                event, texts.PAYMENT_INVALID_ID, alert=True
            )
            return
        verdict = parts[2]
        payment = await self.db.payment(payment_id)
        if payment is None or not is_pending_payment_review(payment):
            await self._ack(  # type: ignore[attr-defined]
                event,
                texts.PAYMENT_NOT_REVIEWABLE,
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
            await self._ack(  # type: ignore[attr-defined]
                event, texts.REJECT_NEEDS_REASON, alert=True
            )
            await event.respond(
                texts.REJECT_REASON_CHOOSE_TEMPLATE.format(
                    payment_id=to_persian_digits(payment_id)
                ),
                buttons=[
                    [
                        Button.inline(
                            texts.REJECT_BLUR,
                            f"payv:{payment_id}:rp:blur".encode(),
                        )
                    ],
                    [
                        Button.inline(
                            texts.REJECT_AMOUNT,
                            f"payv:{payment_id}:rp:amount".encode(),
                        )
                    ],
                    [
                        Button.inline(
                            texts.REJECT_INCOMPLETE,
                            f"payv:{payment_id}:rp:invalid".encode(),
                        )
                    ],
                    [Button.inline(texts.BTN_BACK, f"adm:pd:{payment_id}".encode())],
                ],
            )
            return
        if len(parts) == 3:
            await self._ack(event)  # type: ignore[attr-defined]
            seconds = int(payment.get("purchased_seconds") or 0)
            price = payment.get("purchased_price")
            owner = await self._identity_for_id(int(payment["user_id"]))  # type: ignore[attr-defined]
            await self._edit_or_reply(
                event,
                texts.APPROVE_CONFIRM_TEMPLATE.format(
                    payment_id=to_persian_digits(payment_id),
                    owner=owner,
                    seconds=format_seconds_as_credits(seconds),
                    price=(
                        format_toman(int(price))
                        if price is not None
                        else texts.UNKNOWN_AMOUNT
                    ),
                ),
                [
                    [
                        Button.inline(
                            texts.BTN_CONFIRM_APPROVE,
                            f"payv:{payment_id}:a:yes".encode(),
                        )
                    ],
                    [Button.inline(texts.BTN_BACK, f"adm:pd:{payment_id}".encode())],
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
                texts.APPROVE_INCOMPLETE,
                alert=True,
            )
            return
        except sqlite3.Error:
            logger.exception("payment activation failed payment_id=%s", payment_id)
            await self._ack(  # type: ignore[attr-defined]
                event,
                texts.ACTIVATION_FAILED,
                alert=True,
            )
            return
        if not changed:
            await self._ack(  # type: ignore[attr-defined]
                event, texts.PAYMENT_ALREADY_REVIEWED, alert=True
            )
        else:
            await self._ack(  # type: ignore[attr-defined]
                event, texts.PAYMENT_APPROVED
            )
            seconds = int(payment["purchased_seconds"] or 0)
            try:
                await self.client.send_message(
                    payment["user_id"],
                    texts.PAYMENT_APPROVED_USER_TEMPLATE.format(
                        seconds=format_seconds_as_credits(seconds)
                    ),
                    buttons=[[Button.inline(texts.BTN_WALLET, b"wallet")]],
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
                texts.REJECT_REASON_REQUIRED,
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
            await send_reply(texts.REJECT_FAILED)
            self._pending_reject_reason[event.sender_id] = payment_id
            return
        if not changed:
            await send_reply(texts.REJECT_ALREADY_DONE)
        else:
            await send_reply(
                texts.REJECTED_USER_TEMPLATE.format(
                    payment_id=to_persian_digits(payment_id)
                )
            )
            if payment is not None:
                try:
                    await self.client.send_message(
                        payment["user_id"],
                        texts.REJECTED_NOTICE_TEMPLATE.format(reason=reason[:400]),
                        buttons=[[Button.inline(texts.BTN_SUPPORT, b"support")]],
                    )
                except Exception:
                    logger.warning(
                        "payment rejection notification not delivered payment_id=%s",
                        payment_id,
                    )
        await self._show_admin_payment_detail(event, payment_id)  # type: ignore[attr-defined]

    async def _receipt_cleanup_loop(self) -> None:
        """Delete reviewed receipts after the retention window."""
        last_optimize = None
        while True:
            try:
                await self._cleanup_expired_receipts()
                await self._cleanup_retained_data()
                now = self.deps.monotonic()
                optimize = last_optimize is None or now - last_optimize >= 7 * 86400
                await self.db.maintenance(optimize=optimize)
                if optimize:
                    last_optimize = now
            except asyncio.CancelledError:
                raise
            except Exception:
                METRICS.record_retention_error()
                logger.warning("receipt cleanup or maintenance failed", exc_info=True)
            await asyncio.sleep(24 * 3600)

    async def _cleanup_retained_data(self) -> dict[str, int]:
        totals = {}
        for kind, days in (("documents", self.settings.document_retention_days),
                           ("learning", self.settings.learning_retention_days),
                           ("audit", self.settings.audit_retention_days),
                           ("holds", self.settings.holds_retention_days)):
            total = 0
            if days > 0:
                for _ in range(10):
                    try:
                        count = await self.db.expire_retained_data(kind, days, batch=500)
                    except sqlite3.Error:
                        METRICS.record_retention_error()
                        logger.warning("retention sweep failed kind=%s", kind)
                        break
                    total += count
                    if count < 500:
                        break
            totals[kind] = total
        return totals

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
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.settings.receipt_retention_days)
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
                    datetime.fromtimestamp((await run_file_io(path.stat)).st_mtime, timezone.utc)
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
