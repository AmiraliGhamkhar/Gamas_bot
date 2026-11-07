"""Central source for the migrated Persian UX copy.

Start/help, library, wallet and receipts, support, membership, progress and the
referral screens read their wording from here. Handlers own layout, state and
routing; this module owns wording, emoji and the glossary (جزوه، فلش‌کارت،
آزمون، اشتراک، کیف پول، رسید). Keeping copy out of handlers makes it reviewable
as one document and lets tests assert screens instead of scattered literals.

The administrator report tables and the document/Office/learning workflow copy
are not migrated yet; they are tracked as the remaining part of the same work.

Rules followed here:

* Persian only, with correct ZWNJ (``\\u200c``) in compound words.
* Mixed RTL/LTR tokens (URLs, model names, amounts) stay on their own line or
  inside a single token so the bidi algorithm cannot reorder the sentence.
* No message is truncated: Telegram's 4096 UTF-16 limit is handled by
  :mod:`gamas_bot.telegram_text` at delivery time.
* The copy is deterministic: no clocks, randomness or formatting side effects.
"""

from __future__ import annotations

from typing import Final

# --------------------------------------------------------------------- buttons
BTN_LIBRARY: Final = "📚 کتابخانه من"
BTN_WALLET: Final = "🛒 خرید اشتراک"
BTN_OFFICE: Final = "🗂 آفیس رایگان"
BTN_LEADERBOARD: Final = "🏆 لیدربورد"
BTN_SUPPORT: Final = "🎧 پشتیبانی"
BTN_HELP: Final = "ℹ️ راهنما"
BTN_ADMIN: Final = "🛠 پنل مدیریت"
BTN_BACK_MAIN: Final = "↩️ منوی اصلی"
BTN_BACK: Final = "↩️ بازگشت"
BTN_CANCEL: Final = "↩️ لغو"
BTN_BOOKLETS: Final = "📖 جزوه‌ها"
BTN_DOCUMENTS: Final = "📄 اسناد"
BTN_FLASHCARDS: Final = "🃏 فلش‌کارت‌ها"
BTN_PREV: Final = "◀️ قبلی"
BTN_NEXT: Final = "بعدی ▶️"
BTN_REDOWNLOAD: Final = "📥 دریافت مجدد"
BTN_QUIZ_FROM_BOOKLET: Final = "🧪 آزمون از این جزوه"
BTN_CARDS_FROM_BOOKLET: Final = "🃏 فلش‌کارت از این جزوه"

# Admin menu labels.
BTN_ADMIN_STATS: Final = "📊 آمار و وضعیت"
BTN_ADMIN_PAYMENTS: Final = "💳 پرداخت‌ها"
BTN_ADMIN_USERS: Final = "👥 کاربران"
BTN_ADMIN_SYSTEM: Final = "🩺 سامانه"
BTN_ADMIN_SUPPORT: Final = "🎧 مدیریت پشتیبانی"
BTN_ADMIN_BROADCAST: Final = "📣 ارسال پیام"
BTN_ADMIN_AUDIT: Final = "🛡 گزارش فعالیت‌ها"
BTN_ADMIN_TOP_USERS: Final = "🏆 برترین کاربران"
BTN_ADMIN_BACK_USER: Final = "↩️ پنل کاربری"

# Subscription buttons (the labels the mandatory-channel gate renders).
BTN_CHANNEL_JOIN: Final = "🔗 عضویت در کانال"
BTN_CHANNEL_CHECK: Final = "✅ بررسی عضویت"

# ----------------------------------------------------------------- start/menu
def main_screen(
    *,
    first_start: bool,
    balance: str,
    office_enabled: bool,
    notes_configured: bool,
) -> str:
    """The start message and the return-to-menu screen."""
    greeting = "🎓 به گاماس خوش آمدید" if first_start else "منوی اصلی"
    lines = [
        greeting,
        "",
        "📤 فایل درس را همین‌جا بفرستید:",
        "🎧 صوت یا ویدیو",
        "📄 PDF، Word یا PowerPoint",
        "",
    ]
    if notes_configured:
        lines.append("📚 متن گفتار و جزوه در «📚 کتابخانه من» ذخیره می‌شود.")
    else:
        lines.append("📚 متن گفتار و سند در «📚 کتابخانه من» ذخیره می‌شود.")
    if office_enabled:
        lines.append(
            "🗂 آفیس رایگان: تبدیل متن به Word، Excel، PowerPoint یا PDF؛ بدون کسر اعتبار."
        )
    lines.extend(["", f"💳 اعتبار باقی‌مانده: {balance}", "", "از دکمه‌های زیر شروع کنید."])
    return "\n".join(lines)


def help_screen(*, office_enabled: bool) -> str:
    """The one-screen guide followed by the billing contract."""
    lines = [
        "ℹ️ راهنمای گاماس",
        "",
        "📤 فایل درس: صوت، ویدیو، PDF، Word یا PowerPoint را همین‌جا بفرستید.",
        "📚 متن خام و جزوه در «📚 کتابخانه من» ذخیره می‌شود.",
        "🧪 آزمون و 🃏 فلش‌کارت از متن ذخیره‌شده ساخته می‌شود؛ هر پاسخ درست ۱۰ امتیاز.",
        "🎁 نخستین آزمون و نخستین مجموعهٔ فلش‌کارت جداگانه رایگان‌اند.",
    ]
    if office_enabled:
        lines.append(
            "🗂 آفیس رایگان: تبدیل متن به Word، Excel، PowerPoint یا PDF؛ بدون کسر اعتبار."
        )
    lines.extend(
        [
            "🛒 خرید اشتراک و 🎧 پشتیبانی از منوی اصلی در دسترس است.",
            "",
            "💳 اعتبار بر حسب ثانیه و پس از ارسال متن خام محاسبه می‌شود.",
            "اگر متن خام نرسد، اعتباری کم نمی‌شود.",
            "ساخت آزمون و فلش‌کارت هوش مصنوعی اعتبار کم می‌کند؛ اگر ناموفق باشد، کامل برمی‌گردد.",
            "خطای ساخت جزوه پس از دریافت متن خام، اعتبار را برنمی‌گرداند.",
        ]
    )
    return "\n".join(lines)


# --------------------------------------------------------------------- library
UNLIMITED_BALANCE: Final = "نامحدود ⭐ (کاربر ویژه)"
LIBRARY_TITLE: Final = "📚 کتابخانه من"
LIBRARY_EMPTY: Final = (
    "📚 کتابخانه من\n\n"
    "کتابخانهٔ شما خالی است.\n"
    "اولین فایل درس را بفرستید؛ متن و جزوه به‌صورت خودکار این‌جا ذخیره می‌شوند."
)
LIBRARY_COUNT_TEMPLATE: Final = "تعداد موارد: {count}"
ITEM_UNAVAILABLE: Final = "این فایل دیگر در دسترس نیست."
ITEM_UNAVAILABLE_FAILED: Final = "❌ دریافت فایل ناموفق بود."
ITEM_FORWARD_FAILED: Final = "ارسال فایل موقتاً ناموفق بود؛ دوباره تلاش کنید."
ITEM_FORWARD_DONE: Final = "✅ فایل دوباره در همین گفتگو ارسال شد."
ITEM_FORWARD_HEADLINE: Final = "📥 دریافت مجدد فایل"
ITEM_FORWARDED_HEADLINE: Final = "✅ فایل ارسال شد"
ITEM_FORWARDING_STEP: Final = "ارسال فایل"
ITEM_FORWARDING_DETAIL: Final = "فایل از تلگرام دوباره ارسال می‌شود…"
LIBRARY_INVALID_PAGE: Final = "صفحهٔ کتابخانه نامعتبر است."
DOCS_INVALID_PAGE: Final = "صفحهٔ اسناد نامعتبر است."
DOCUMENT_NOT_FOUND: Final = "این سند پیدا نشد."

# ------------------------------------------------------------------ acknowledgements
CALLBACK_FAILED: Final = "خطایی رخ داد؛ دوباره امتحان کنید."
UNEXPECTED_ERROR: Final = "⚠️ خطای غیرمنتظره‌ای رخ داد. لطفاً دوباره امتحان کنید."
RESTARTING: Final = "ربات در حال راه‌اندازی مجدد است؛ دوباره تلاش کنید."
RESTARTING_JOB_CANCELLED: Final = (
    "⏸ ربات در حال راه‌اندازی مجدد است؛ کار شما لغو شد. لطفاً دوباره بفرستید."
)
RESTARTING_JOB_MESSAGE_REFERENCE: Final = "شناسهٔ پیام اصلی: {message_id}"
ACCESS_BANNED: Final = "⛔ دسترسی شما به ربات مسدود شده است."
ADMIN_ONLY: Final = "دسترسی به این بخش فقط برای مدیران مجاز است."
INVALID_LEARNING_SIZE: Final = "اندازهٔ انتخاب‌شده نامعتبر است."
INVALID_REQUEST: Final = "درخواست نامعتبر است."
INVALID_PERIOD: Final = "بازهٔ زمانی نامعتبر است."
INVALID_REPORT: Final = "بخش گزارش نامعتبر است."
INVALID_ORDERS_PAGE: Final = "صفحهٔ سفارش‌ها نامعتبر است."
INVALID_ORDER_ID: Final = "شناسهٔ سفارش نامعتبر است."
INVALID_GIFT: Final = "درخواست هدیه نامعتبر است."
INVALID_PAYMENTS_PAGE: Final = "صفحهٔ پرداخت‌ها نامعتبر است."
INVALID_REPORT_PAGE: Final = "صفحهٔ گزارش نامعتبر است."
INVALID_COMMAND_CANCEL: Final = "دستور نامعتبر است؛ برای لغو /cancel بفرستید."
INVALID_COMMAND_TEXT: Final = "دستور نامعتبر است؛ متن پیام را بفرستید یا /cancel بزنید."

# -------------------------------------------------------------------- broadcast
BROADCAST_PROMPT: Final = (
    "📣 ارسال پیام به کاربران\n\n"
    "متن موردنظر را در پیام بعدی بفرستید؛ برای همهٔ کاربران ثبت‌شده در ربات تلاش می‌شود ارسال شود.\n"
    "کاربرانی که ربات را مسدود کرده‌اند ممکن است پیام را دریافت نکنند.\n"
    "برای لغو، /cancel بفرستید یا دکمهٔ زیر را انتخاب کنید."
)
BROADCAST_RUNNING: Final = "یک ارسال همگانی در حال اجراست."
BROADCAST_PREVIEW: Final = "📣 پیش‌نمایش پیام همگانی\n\n{text}\n\nتعداد گیرندگان: {count}"
BROADCAST_TOO_LONG: Final = "پیام همگانی بیش از حد طولانی است (حداکثر ۴۰۹۶ نویسه)."
BROADCAST_PROGRESS: Final = "📣 ارسال همگانی: ارسال‌شده {sent}، ناموفق {failed}، کل {total}"
BROADCAST_SUMMARY: Final = "📣 پیام به {sent} نفر از {total} کاربر ارسال شد.\n⚠️ ارسال برای {failed} کاربر ناموفق بود."
BROADCAST_STOPPED: Final = "ارسال همگانی متوقف شد."
BROADCAST_RESUME_PROMPT: Final = "متن همان پیام همگانی را دوباره بفرستید؛ در صورت تفاوت، ارسال تازه آغاز می‌شود."
BROADCAST_RECOVERED: Final = "ارسال همگانی ناتمام #{run} متوقف شد. آخرین شناسه: {cursor}. برای ادامه متن را دوباره وارد کنید."
BTN_BROADCAST_SEND: Final = "✅ تأیید و ارسال"
BTN_BROADCAST_STOP: Final = "⏹ توقف ارسال"
BTN_BROADCAST_RESUME: Final = "▶️ ادامهٔ ارسال"
BROADCAST_CANCELLED: Final = "پیام همگانی لغو شد."
BROADCAST_NEEDS_TEXT: Final = "پیام همگانی باید متن داشته باشد؛ متن را بفرستید یا /cancel بزنید."
REJECT_CANCELLED: Final = "رد پرداخت لغو شد."
REJECT_REASON_PROMPT: Final = (
    "لطفاً دلیل رد را در یک پیام متنی بنویسید؛ برای لغو /cancel بفرستید."
)
REJECT_REASON_INVALID: Final = (
    "دستور نامعتبر است؛ دلیل را بنویسید یا برای لغو /cancel بفرستید."
)

# ----------------------------------------------------------------- intake hints
OFFICE_DISABLED: Final = "آفیس رایگان در تنظیمات سرور غیرفعال است."
OFFICE_DRAFT_OPEN: Final = (
    "یک پیش‌نویس آفیس رایگان باز دارید. ابتدا خروجی را بسازید یا «پایان و پاک‌کردن متن» را بزنید؛ "
    "سپس فایل دیگری را بفرستید تا با جریان مناسب خودش پردازش شود."
)
UNSUPPORTED_FILE: Final = (
    "⚠️ این نوع فایل پشتیبانی نمی‌شود. فایل صوتی، ویدیویی، پاورپوینت یا "
    "سند PDF/Word بفرستید."
)
UPLOAD_HINT: Final = "همین‌جا فایل 🎧 صوت/ویدیو یا 📄 PDF، Word و پاورپوینت درس را بفرستید."
ORDER_CANCELLED: Final = "ارسال لغو شد. لغو سفارش از «🛒 خرید اشتراک»."

# ------------------------------------------------------------------ membership
SUBSCRIPTION_REQUIRED_TEMPLATE: Final = (
    "🔒 استفاده از ربات گاماس فقط برای اعضای کانال رسمی آن مجاز است.\n\n"
    "برای دسترسی به خدمات ربات، ابتدا به کانال گاماس بپیوندید:\n"
    "{channel_url}\n\n"
    "پس از عضویت، دکمهٔ «✅ بررسی عضویت» را بزنید."
)
SUBSCRIPTION_STILL_REQUIRED: Final = (
    "🤔 عضویت شما در کانال گاماس هنوز تأیید نشده است.\n"
    "برای ادامه، ابتدا به کانال بپیوندید و سپس «✅ بررسی عضویت» را بزنید."
)
SUBSCRIPTION_CONFIRMED: Final = (
    "✅ عضویت شما در کانال گاماس تأیید شد.\n"
    "اکنون می‌توانید از ربات استفاده کنید."
)
SUBSCRIPTION_UNAVAILABLE: Final = (
    "⚠️ در حال حاضر توانایی بررسی عضویت موقتاً در دسترس نیست.\n"
    "یک لحظه دیگر دوباره امتحان کنید."
)
SUBSCRIPTION_ADMIN_PERMISSION_ALERT: Final = (
    "⚠️ بررسی عضویت کانال گاماس ناموفق است.\n"
    "دلیل: {detail}\n"
    "ربات باید administrator کانال باشد؛ "
    "راهنمای تنظیم در docs/TELEGRAM_SUBSCRIPTION.md را ببینید."
)
SUBSCRIPTION_NOT_ADMIN_ALERT: Final = (
    "⚠️ ربات در کانال گاماس administrator نیست؛ بررسی عضویت کاربران "
    "ممکن نیست (وضعیت: {status}). ربات را administrator کانال کنید."
)
SUBSCRIPTION_SELF_CHECK_FAILED: Final = (
    "⚠️ بررسی خودکار کانال گاماس ناموفق بود.\n"
    "دلیل: {error}\n"
    "ربات باید administrator کانال {channel} باشد."
)

# ------------------------------------------------------------------- referrals
BTN_REFERRALS: Final = "🤝 دعوت دوستان"
BTN_ADMIN_REFERRALS: Final = "🤝 آمار دعوت"
BTN_SHARE_INVITE: Final = "📤 اشتراک لینک دعوت"
REFERRALS_TITLE: Final = "🤝 دعوت دوستان"
REFERRALS_RULES: Final = (
    "با هر دعوت موفق اعتبار هدیه بگیرید:\n"
    "• ورود دوست با لینک شما: ۲۰ دقیقه اعتبار\n"
    "• اولین خرید تأییدشدهٔ او: ۱ ساعت اعتبار"
)
REFERRALS_LINK_TEMPLATE: Final = "لینک اختصاصی شما:\n{link}"
REFERRALS_CODE_TEMPLATE: Final = "کد دعوت شما: {code}"
REFERRALS_STATS_TEMPLATE: Final = "دعوت‌شده‌ها: {invited} | اعتبار هدیه: {seconds}"
REFERRALS_SELF: Final = "نمی‌توانید با لینک خودتان دعوت شوید."
REFERRALS_DUPLICATE: Final = "این دعوت قبلاً ثبت شده است."
REFERRALS_UNAVAILABLE: Final = (
    "ساخت لینک دعوت موقتاً در دسترس نیست؛ کمی بعد دوباره تلاش کنید."
)
REFERRAL_JOIN_REWARD_TEMPLATE: Final = (
    "🎁 هدیهٔ دعوت ثبت شد: {seconds} اعتبار به دوست شما اضافه شد."
)
REFERRAL_REFERRER_NOTICE_TEMPLATE: Final = (
    "🎉 یک دوست با لینک شما به گاماس پیوست؛ {seconds} اعتبار هدیه گرفتید."
)
ADMIN_REFERRALS_HEADER: Final = "🤝 آمار دعوت"
ADMIN_REFERRALS_TOTALS_TEMPLATE: Final = (
    "مجموع هدیه‌ها: {rewards} | اعتبار پرداخت‌شده: {seconds}"
)
ADMIN_REFERRALS_ROW_TEMPLATE: Final = "{rank}. {who} — {invited} دعوت | {seconds}"
ADMIN_REFERRALS_EMPTY: Final = "هنوز دعوتی ثبت نشده است."

# --------------------------------------------------------------------- progress
QUEUED_STAGE: Final = "در صف پردازش"
QUEUED_DETAIL: Final = "پردازش به‌زودی آغاز می‌شود."
DELIVERY_DONE_DETAIL: Final = "متن گفتار و فایل خروجی ارسال شد."
TRACKING_CODE_TEMPLATE: Final = "کد پیگیری: {reference}"

# ----------------------------------------------------------------------- wallet
WALLET_TITLE: Final = "🛒 خرید اشتراک"
WALLET_NO_PLANS: Final = "فعلاً بسته‌ای برای خرید موجود نیست."
WALLET_CHOOSE_PLAN: Final = "بسته را انتخاب کنید؛ اعتبار خرید به موجودی شما اضافه می‌شود."
WALLET_PLAN_ROW_TEMPLATE: Final = "• {duration}\n  اعتبار: {validity} | مبلغ: {price}"
WALLET_PLAN_BUTTON_TEMPLATE: Final = "{hours} ساعت — {price}"
WALLET_CARD_DISABLED: Final = "🛒 پرداخت کارت‌به‌کارت فعلاً غیرفعال است."
WALLET_BALANCE_TEMPLATE: Final = "💳 اعتبار باقی‌مانده: {balance}"
WALLET_RECENT_PAYMENTS: Final = "🧾 پرداخت‌های اخیر"
WALLET_NO_PAYMENTS: Final = "پرداختی ثبت نشده است."
WALLET_NO_MORE_PAYMENTS: Final = "مورد دیگری وجود ندارد."
UNKNOWN_AMOUNT: Final = "مبلغ نامشخص"
REASON_PREFIX_TEMPLATE: Final = "دلیل: {reason}"
WALLET_CLOSED: Final = "🛒 پرداخت کارت‌به‌کارت فعلاً فعال نیست؛ کمی بعد دوباره امتحان کنید."
WALLET_OPEN_ORDER: Final = "⚠️ یک سفارش باز دارید؛ اول آن را پیگیری یا لغو کنید."
PLAN_NOT_AVAILABLE: Final = "این طرح در دسترس نیست."
ORDER_NOT_ACTIVE: Final = "این سفارش فعال نیست. وضعیت را در «🛒 خرید اشتراک» ببینید."
ORDER_INCOMPLETE: Final = "اطلاعات این سفارش ناقص است؛ لطفاً درخواست پشتیبانی ثبت کنید."
ORDER_HEADER_TEMPLATE: Final = (
    "🧾 سفارش {order_ref}\n"
    "بسته: {hours} ساعت | مبلغ: {price}\n"
    "اعتبار بسته: {validity}"
)
RECEIPT_PENDING: Final = (
    "🕒 رسید پرداخت شما ثبت شد و در انتظار بررسی است.\n"
    "نیازی به واریز مجدد نیست؛ پس از بررسی مدیر، نتیجه اطلاع‌رسانی می‌شود."
)
RECEIPT_PENDING_SHORT: Final = (
    "🕒 رسید پرداخت شما ثبت شد و در انتظار بررسی است.\n"
    "پس از بررسی مدیر، نتیجه اطلاع‌رسانی می‌شود."
)
CARD_UNAVAILABLE: Final = (
    "پرداخت کارت‌به‌کارت فعلاً در دسترس نیست؛ سفارش را لغو کنید یا بعداً امتحان کنید."
)
CARD_DETAILS_TEMPLATE: Final = (
    "اطلاعات واریز:\n"
    "💳 شماره کارت: {card}\n"
    "به نام: {holder}\n"
    "بانک: {bank}\n\n"
    "پس از واریز، تصویر رسید را بفرستید. اعتبار فقط پس از تأیید مدیر اضافه می‌شود."
)
BTN_SEND_RECEIPT: Final = "📤 ارسال رسید"
BTN_CANCEL_ORDER: Final = "لغو سفارش"
BTN_ORDER_DETAILS: Final = "🔎 جزئیات سفارش"
BTN_TRACK_OPEN_ORDER: Final = "🔎 پیگیری سفارش باز"
ORDER_CANCELLED_OK: Final = "سفارش لغو شد."
ORDER_NOT_CANCELLABLE: Final = (
    "این سفارش قابل لغو نیست؛ رسید ارسال‌شده باید توسط مدیر بررسی شود."
)
ORDER_RECEIPT_CLOSED: Final = "این سفارش برای ارسال رسید فعال نیست."
RECEIPT_ASK_IMAGE: Final = (
    "📷 تصویر خوانای رسید را به‌صورت عکس یا فایل تصویری بفرستید."
)
RECEIPT_BAD_TYPE: Final = "رسید باید عکس یا فایل تصویری JPEG/PNG/WebP باشد."
RECEIPT_TOO_LARGE: Final = "حجم تصویر رسید زیاد است؛ فشرده‌تر بفرستید."
RECEIPT_NO_SPACE: Final = "فضای موقت سرور کافی نیست؛ رسید را کمی بعد دوباره بفرستید."
RECEIPT_DOWNLOAD_FAILED: Final = "دریافت رسید ناموفق بود؛ لطفاً دوباره ارسال کنید."
RECEIPT_BAD_FORMAT: Final = "فرمت رسید نامعتبر است؛ فقط JPEG/PNG/WebP پذیرفته می‌شود."
RECEIPT_SAVE_FAILED: Final = "ذخیرهٔ رسید ناموفق بود؛ لطفاً دوباره ارسال کنید."
RECEIPT_RECORD_FAILED: Final = "ثبت رسید انجام نشد؛ لطفاً دوباره ارسال کنید."
RECEIPT_ORDER_CLOSED: Final = "این سفارش دیگر برای ارسال رسید فعال نیست."
ADMIN_RECEIPT_HEADER_TEMPLATE: Final = (
    "🧾 رسید سفارش {payment_id}\n"
    "کاربر: {owner}\n"
    "بسته: {plan}\n"
    "مبلغ: {price}"
)
ADMIN_RECEIPT_DUPLICATE: Final = "⚠️ تصویر رسید قبلاً در سفارش دیگر ثبت شده است (#{payment_id})"
ADMIN_RECEIPT_MISSING_IMAGE: Final = "\n⚠️ رسید تصویری ارسال نشد؛ آن را از جزئیات سفارش ببینید."
ADMIN_RECEIPT_MISSING_FILE: Final = "\n⚠️ فایل رسید در دسترس نیست."
PAYMENT_INVALID_ID: Final = "شناسه سفارش نامعتبر است."
PAYMENT_NOT_REVIEWABLE: Final = (
    "سفارش قابل بررسی نیست؛ ابتدا رسید باید ثبت شود یا سفارش قبلاً بررسی شده است."
)
REJECT_NEEDS_REASON: Final = "برای رد پرداخت، دلیل لازم است."
REJECT_REASON_CHOOSE_TEMPLATE: Final = (
    "دلیل رد سفارش {payment_id} را انتخاب کنید یا در یک پیام بنویسید.\n"
    "برای لغو، /cancel بفرستید."
)
REJECT_BLUR: Final = "📷 تصویر رسید ناخوانا"
REJECT_AMOUNT: Final = "💰 عدم تطابق مبلغ"
REJECT_INCOMPLETE: Final = "⚠️ اطلاعات ناقص یا تکراری"
APPROVE_CONFIRM_TEMPLATE: Final = (
    "✅ تأیید سفارش {payment_id}\n\n"
    "کاربر: {owner}\n"
    "اعتبار: {seconds}\n"
    "مبلغ: {price}\n\n"
    "پس از تأیید نهایی، اعتبار به حساب کاربر اضافه می‌شود."
)
BTN_CONFIRM_APPROVE: Final = "✅ تأیید نهایی"
APPROVE_INCOMPLETE: Final = "اطلاعات بسته یا مبلغ ناقص است؛ تأیید انجام نشد."
ACTIVATION_FAILED: Final = (
    "فعال‌سازی انجام نشد. سفارش همچنان در انتظار بررسی است؛ بعداً دوباره تلاش کنید."
)
PAYMENT_ALREADY_REVIEWED: Final = "این سفارش قبلاً بررسی شده است."
PAYMENT_APPROVED: Final = "پرداخت تأیید و اعتبار فعال شد."
PAYMENT_APPROVED_USER_TEMPLATE: Final = (
    "✅ پرداخت شما تأیید شد. اشتراک شما فعال است.\n"
    "💳 {seconds} اعتبار اضافه شد."
)
REJECT_REASON_REQUIRED: Final = "برای رد پرداخت، واردکردن دلیل الزامی است."
REJECT_FAILED: Final = "رد پرداخت ثبت نشد؛ لطفاً دوباره تلاش کنید."
REJECT_ALREADY_DONE: Final = "این سفارش قبلاً بررسی شده است یا رسید ندارد."
REJECTED_USER_TEMPLATE: Final = "❌ سفارش {payment_id} رد شد."
REJECTED_NOTICE_TEMPLATE: Final = (
    "❌ پرداخت تأیید نشد.\nدلیل: {reason}\n"
    "برای پیگیری می‌توانید درخواست پشتیبانی ثبت کنید."
)
PAYMENT_STATUS_LABELS: Final[dict[str, str]] = {
    "pending": "⏳ منتظر پرداخت",
    "awaiting_payment": "⏳ منتظر پرداخت",
    "pending_review": "🕒 در انتظار بررسی",
    "approved": "✅ تأیید شده",
    "rejected": "❌ رد شده",
    "cancelled": "لغو شده",
}
PAYMENT_STATUS_UNKNOWN: Final = "نامشخص"
RECEIPT_REJECT_REASONS: Final[dict[str, str]] = {
    "blur": "تصویر رسید ناخوانا است؛ تصویر واضح‌تری ارسال کنید.",
    "amount": "مبلغ واریزی با مبلغ بسته مطابقت ندارد.",
    "invalid": "اطلاعات تراکنش ناقص یا تکراری است.",
}

# ---------------------------------------------------------------------- support
SUPPORT_OLDER_MESSAGES: Final = "… پیام‌های قدیمی‌تر در صفحهٔ بعدی"
SUPPORT_WHO_STAFF: Final = "🎧 پشتیبانی"
SUPPORT_WHO_USER_TEMPLATE: Final = "👤 کاربر {sender_id}"
SUPPORT_WHO_YOU: Final = "👤 شما"
SUPPORT_EMPTY_THREAD: Final = "هنوز پیامی ثبت نشده است."
SUPPORT_MENU: Final = (
    "🎧 پشتیبانی\n\n"
    "برای ثبت درخواست جدید یا پیگیری درخواست‌های قبلی، یکی از گزینه‌ها را انتخاب کنید."
)
BTN_NEW_TICKET: Final = "➕ درخواست جدید"
BTN_MY_TICKETS: Final = "📋 درخواست‌های من"
BTN_VIEW_TICKET: Final = "📋 مشاهدهٔ درخواست"
BTN_REVIEW_TICKET: Final = "🔎 بررسی درخواست"
BTN_VIEW_REPLY: Final = "📋 مشاهدهٔ پاسخ"
BTN_REPLY_STAFF: Final = "✍️ پاسخ به پشتیبانی"
BTN_REPLY_USER: Final = "✍️ پاسخ به کاربر"
BTN_CLOSE_TICKET: Final = "✅ بستن درخواست"
BTN_REOPEN_TICKET: Final = "🔁 باز کردن دوباره"
BTN_OLDER_MESSAGES: Final = "پیام‌های قدیمی‌تر ▶️"
BTN_NEWER_MESSAGES: Final = "◀️ پیام‌های جدیدتر"
BTN_ALL_CATEGORIES: Final = "📂 همه دسته‌ها"
BTN_BACK_SUPPORT: Final = "↩️ درخواست‌های من"
BTN_BACK_ADMIN: Final = "↩️ پنل مدیریت"
BTN_BACK_ADMIN_SUPPORT: Final = "↩️ فهرست پشتیبانی"
SUPPORT_OPEN_LIMIT: Final = "شما {count} تیکت باز دارید؛ ابتدا یکی را ببندید."
SUPPORT_COOLDOWN: Final = "لطفاً کمی بعد دوباره تلاش کنید."
SUPPORT_CONFIRM_CLOSE: Final = "آیا بستن تیکت را تأیید می‌کنید؟"
SUPPORT_CHOOSE_CATEGORY: Final = "دسته‌بندی درخواست را انتخاب کنید:"
SUPPORT_NO_TICKETS: Final = "شما هنوز درخواستی ثبت نکرده‌اید."
SUPPORT_LIST_TITLE_TEMPLATE: Final = "📋 درخواست‌های شما — صفحهٔ {page}"
SUPPORT_TICKET_NOT_FOUND: Final = "این درخواست پیدا نشد."
SUPPORT_BAD_CATEGORY: Final = "این دسته معتبر نیست."
SUPPORT_SUBJECT_PROMPT: Final = (
    "شرح مشکل را در یک پیام بفرستید (حداکثر ۴۰۰۰ نویسه). خط اول موضوع خواهد بود.\n"
    "برای لغو، /cancel بفرستید."
)
SUPPORT_INVALID_REQUEST: Final = "درخواست نامعتبر است."
SUPPORT_TICKET_NOT_REPLYABLE: Final = (
    "این درخواست پاسخ‌پذیر نیست؛ ابتدا آن را باز کنید."
)
SUPPORT_REPLY_PROMPT_TEMPLATE: Final = (
    "پیام خود را برای درخواست {ticket_id} بفرستید.\n"
    "برای لغو، /cancel بفرستید."
)
SUPPORT_CLOSED_BY_USER_TEMPLATE: Final = (
    "🔒 کاربر {owner} درخواست {ticket_id} را بست."
)
SUPPORT_REOPENED_BY_USER_TEMPLATE: Final = (
    "🔁 کاربر {owner} درخواست {ticket_id} را دوباره باز کرد."
)
SUPPORT_TEXT_ONLY: Final = (
    "لطفاً پیام خود را به‌صورت متن بفرستید؛ برای لغو /cancel بفرستید."
)
SUPPORT_SUBJECT_LENGTH: Final = "موضوع باید بین ۲ تا ۴۰۰ نویسه باشد؛ دوباره بنویسید."
SUPPORT_CREATED: Final = (
    "✅ درخواست شما ثبت شد.\n"
    "شماره پیگیری: {ticket_id}\n"
    "از همین بخش می‌توانید پاسخ‌ها را پیگیری کنید."
)
SUPPORT_ADMIN_NEW_TICKET: Final = (
    "🎧 درخواست پشتیبانی جدید\n"
    "شماره: {ticket_id}\n"
    "کاربر: {owner}\n"
    "دسته: {category}\n"
    "موضوع: {subject}"
)
SUPPORT_MESSAGE_TOO_LONG: Final = (
    "پیام باید حداکثر ۴۰۰۰ نویسه باشد؛ کوتاه‌تر بنویسید."
)
SUPPORT_MESSAGE_SAVED: Final = "✅ پیام شما ثبت شد."
SUPPORT_NEW_MESSAGE_ADMIN: Final = (
    "💬 پیام جدیدی در درخواست {ticket_id} ثبت شد.\nکاربر: {owner}"
)
SUPPORT_REPLY_NEEDS_TEXT: Final = (
    "پاسخ باید متن داشته باشد؛ دوباره متن را بفرستید یا /cancel بزنید."
)
SUPPORT_REPLY_SAVED_ADMIN: Final = "✅ پاسخ به درخواست {ticket_id} ثبت شد."
SUPPORT_REPLY_NOTICE_USER: Final = (
    "📩 پاسخ جدیدی برای درخواست {ticket_id} ثبت شده است."
)
SUPPORT_ADMIN_HEADER_TEMPLATE: Final = (
    "🎧 مدیریت پشتیبانی\n\n"
    "فعال: {open_count} | "
    "جدید: {new_count} | "
    "منتظر پاسخ شما: {waiting_count} | "
    "بسته: {closed_count}\n"
    "فهرست: {view} — دسته: {category}"
)
SUPPORT_EMPTY_LIST: Final = "\n\nدرخواستی در این فهرست نیست."
SUPPORT_BAD_LIST: Final = "فهرست نامعتبر است."
SUPPORT_TICKET_HEADER_TEMPLATE: Final = (
    "🎫 درخواست {ticket_id}\n"
    "دسته: {category}\n"
    "وضعیت: {status}\n"
    "موضوع: {subject}\n"
    "ثبت: {created}"
)
SUPPORT_ADMIN_TICKET_TEMPLATE: Final = (
    "🎫 درخواست {ticket_id}\n"
    "کاربر: {owner}\n"
    "دسته: {category}\n"
    "وضعیت: {status}\n"
    "موضوع: {subject}\n"
    "ثبت: {created} | "
    "آخرین تغییر: {updated}\n"
)
SUPPORT_TICKET_CLOSED: Final = "این درخواست بسته است؛ ابتدا آن را باز کنید."
SUPPORT_ADMIN_REPLY_PROMPT: Final = (
    "پاسخ خود را برای درخواست {ticket_id} بنویسید.\n"
    "پس از ارسال، کاربر از پاسخ شما مطلع می‌شود. برای لغو /cancel بفرستید."
)
SUPPORT_CLOSED_NOTICE: Final = (
    "✅ درخواست {ticket_id} بسته شد. "
    "اگر مشکل شما هنوز باقی است، می‌توانید درخواست را دوباره باز کنید."
)
SUPPORT_REOPENED_NOTICE: Final = "🔁 درخواست {ticket_id} دوباره باز شد."

# --------------------------------------------------------------------- progress
PROGRESS_DEFAULT_HEADLINE: Final = "⏳ در حال پردازش فایل"
PROGRESS_WORK_HEADLINE: Final = "⏳ در حال پردازش"
PROGRESS_DEFAULT_DONE: Final = "✅ پردازش تکمیل شد"
PROGRESS_WORK_DONE: Final = "✅ آماده شد"
PROGRESS_DEFAULT_FAIL: Final = "❌ پردازش تکمیل نشد."
PROGRESS_WORK_FAIL: Final = "❌ انجام نشد."

CURATION_DELIVERY_RETRY: Final = "پیش‌نمایش ارسال نشد؛ با /start دوباره تلاش کنید."
CURATION_RESUME_BUTTON: Final = "🧐 ادامهٔ بررسی"

CURATION_SOURCE_REMOVED: Final = "منبع این پیش‌نمایش حذف شده بود؛ مبلغ بازگشت داده شد."
CURATION_SOURCE_REMOVED_FREE: Final = "منبع این پیش‌نمایش حذف شده بود؛ هزینه‌ای کسر نشد."
LEARNING_PRORATA_REFUND: Final = "اعتبار بازگشتی: {duration}"
PAYMENT_HISTORY_CANCELLED: Final = "لغوشده"

FLASHCARD_CARD_LIMIT: Final = "این مجموعه به حداکثر تعداد کارت رسیده است."

DOCUMENT_EXTRACTED_COUNT: Final = "متن استخراج‌شده: {count} نویسه"
DOCUMENT_TEXT_PREVIEW: Final = "پیش‌نمایش متن:"
