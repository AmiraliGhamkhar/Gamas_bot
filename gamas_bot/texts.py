"""Central source for the migrated Persian UX copy.

Start/help, library, wallet and receipts, support, membership, progress and the
referral screens read their wording from here. Handlers own layout, state and
routing; this module owns wording, emoji and the glossary (جزوه،
آزمون، اشتراک، کیف پول، رسید). Keeping copy out of handlers makes it reviewable
as one document and lets tests assert screens instead of scattered literals.

Administrator report tables and some workflow prompts remain in their owning
modules; document/Office and learning availability copy is centralized here.

Rules followed here:

* Persian only, with correct ZWNJ (``\\u200c``) in compound words.
* Mixed RTL/LTR tokens (URLs, model names, amounts) stay on their own line or
  inside a single token so the bidi algorithm cannot reorder the sentence.
* No message is truncated: Telegram's 4096 UTF-16 limit is handled by
  :mod:`gamas_bot.telegram_text` at delivery time.
* The copy is deterministic: no clocks, randomness or formatting side effects.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

# --------------------------------------------------------------------- buttons
BTN_LIBRARY: Final = "کتابخانه"
BTN_WALLET: Final = "اعتبار"
#: Legacy reply-keyboard labels accepted during the navigation transition.
BTN_LIBRARY_LEGACY: Final = "📚 کتابخانه من"
BTN_WALLET_LEGACY: Final = "🛒 خرید اشتراک"
BTN_WALLET_UPGRADE_LEGACY: Final = "🛒 افزایش اعتبار"
BTN_OFFICE: Final = "آفیس رایگان"
BTN_OFFICE_LEGACY: Final = "🗂 آفیس رایگان"
BTN_SUPPORT: Final = "پشتیبانی"
BTN_SUPPORT_LEGACY: Final = "🎧 پشتیبانی"
BTN_HELP: Final = "راهنما"
BTN_HELP_LEGACY: Final = "ℹ️ راهنما"
BTN_ADMIN: Final = "🛠 پنل مدیریت"
BTN_MORE: Final = "بیشتر"
BTN_BACK_MAIN: Final = "↩️ منوی اصلی"
BTN_BACK_TO_UPLOAD: Final = "📤 ارسال فایل"
BTN_BACK: Final = "↩️ بازگشت"
BTN_CANCEL: Final = "لغو"
BTN_BOOKLETS: Final = "همهٔ موارد"
BTN_DOCUMENTS: Final = "اسناد"
BTN_LIB_BOOKLETS_TAB: Final = "📖 جزوه‌ها ({count})"
BTN_LIB_QUIZZES_TAB: Final = "🧪 کوییزها ({count})"
BTN_LIB_OTHER_TAB: Final = "🗂 سایر فایل‌ها ({count})"
BTN_LIB_QUIZZES_SHORT: Final = "🧪 کوییزها"
BTN_AI_QUIZ_FROM_BOOKLET: Final = "🤖 ساخت کوییز از جزوه"
BTN_DELETE_ITEM: Final = "🗑 حذف از کتابخانه"
BTN_DELETE_ICON: Final = "🗑"
BTN_CONFIRM_DELETE: Final = "✅ بله، حذف کن"
BTN_HISTORY: Final = "نتایج پردازش"
BTN_PREV: Final = "◀️ قبلی"
BTN_NEXT: Final = "بعدی ▶️"
BTN_REDOWNLOAD: Final = "دریافت مجدد"
BTN_GET_BOOKLET: Final = "دریافت جزوه PDF"
BTN_GET_DOCX: Final = "دریافت نسخه Word"
BTN_GET_REVIEW_HTML: Final = "🌐 دریافت جزوه مرور تعاملی HTML"
DELIVERY_ROW_REVIEW_HTML: Final = "جزوهٔ مرور تعاملی HTML (آفلاین) آماده است."
REVIEW_STAGE_PLANNING: Final = "طراحی جزوهٔ مرور تعاملی"
REVIEW_STAGE_RENDERING: Final = "ساخت فایل HTML"
REVIEW_STAGE_VALIDATING: Final = "بررسی ساختار و امنیت HTML"
NOTICE_REVIEW_HTML: Final = "\n\nنکته: جزوهٔ مرور تعاملی HTML آماده نشد؛ فایل‌های دیگر جزوه سالم هستند."
NOTICE_REVIEW_HTML_STORAGE: Final = "\n\nنکته: ذخیرهٔ جزوهٔ مرور تعاملی HTML انجام نشد."
BTN_GET_TRANSCRIPT: Final = "خواندن متن خام در گفتگو"
BTN_DOWNLOAD_TXT: Final = "دانلود متن خام (TXT)"
BTN_GET_KEYPOINTS: Final = "دریافت نکات کلیدی"
BTN_MY_RESULTS: Final = "📦 خروجی‌های من"
BTN_START_HERE: Final = "🎓 شروع"
BTN_QUIZ_FROM_BOOKLET: Final = "🧪 ۲۰ سؤال نمونه"
OFFICE_CREDIT_NOTICE: Final = (
    "🗂 تبدیل متن به Word، Excel، PowerPoint یا PDF؛ "
    "سقف ورودی و محدودیت ظرفیت اعمال می‌شود."
)
CREDIT_ESTIMATE_NOTE: Final = (
    "اعتبار لازم برآورد اولیه است؛ کسر نهایی پس از پردازش بر اساس مدت واقعی انجام می‌شود. "
    "اگر متن خام آماده نشود، اعتباری کم نمی‌شود."
)
LEARNING_FREE_CHOICE_LABEL: Final = "رایگان در اولین استفاده"
PRESENTATION_PROCESSING_NOTICE: Final = (
    "متن اسلایدها استخراج می‌شود؛ صدای ارائه خودکار پردازش نمی‌شود و فقط با دکمهٔ "
    "«🎧 پردازش صدای ارائه» آغاز می‌شود."
)
DOCUMENT_SCANNED_NOTICE: Final = (
    "PDF اسکن‌شده یا عکس سند قابل‌خواندن نیست؛ "
    "لطفاً نسخهٔ متنی PDF یا فایل Word بفرستید."
)
DOCUMENT_PHOTO_UNSUPPORTED: Final = (
    "📷 عکس سند پشتیبانی نمی‌شود.\n"
    "PDF متنی یا فایل Word دارای متن بفرستید؛ عکس‌های داخل Word/PowerPoint خوانده نمی‌شوند."
)
DOCUMENT_EMBEDDED_IMAGES_NOTICE: Final = (
    "تصویرهای داخل Word یا PowerPoint استخراج نمی‌شوند."
)
DOCUMENT_WORD_NO_TEXT: Final = (
    "متن قابل‌استخراجی در این Word پیدا نشد؛ نسخهٔ دارای متن بفرستید."
)
DOCUMENT_PRESENTATION_NO_TEXT: Final = (
    "متن قابل‌استخراجی در اسلایدها پیدا نشد."
)
DOCUMENT_EXTRACTION_EMPTY: Final = (
    "متن قابل‌استخراجی از این سند پیدا نشد؛ اگر فایل تصویری یا اسکن‌شده است، "
    "نسخهٔ متنی را بفرستید."
)
DOCUMENT_FREE_NOTICE: Final = (
    "جزوه، مرور سریع و خروجی‌های Office از اعتبار کم نمی‌کنند؛ "
    "آفیس هم سقف ورودی و محدودیت ظرفیت دارد."
)

# Admin menu labels.
BTN_ADMIN_STATS: Final = "📊 آمار و وضعیت"
BTN_ADMIN_PAYMENTS: Final = "💳 پرداخت‌ها"
BTN_ADMIN_USERS: Final = "👥 کاربران"
BTN_ADMIN_SYSTEM: Final = "🩺 سامانه"
BTN_ADMIN_SUPPORT: Final = "🎧 مدیریت پشتیبانی"
BTN_ADMIN_BROADCAST: Final = "📣 ارسال پیام"
BTN_ADMIN_AUDIT: Final = "🛡 گزارش فعالیت‌ها"
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
    streak_days: int = 0,
) -> str:
    """The whole bot entry point: send a file, get the outputs back.

    Deliberately one sentence plus the input list. The first visit and every
    later visit render the same screen: there is no returning "panel" to learn,
    keep in balance, or read a second time.
    """
    del first_start, balance, office_enabled, streak_days
    # Never promise a booklet the deployment cannot produce.
    if notes_configured:
        promise = "متن خام، جزوهٔ PDF و Word و ۲۰ سؤال نمونه"
        action = f"فایل درس را بفرستید تا {promise} بگیرید."
    else:
        action = "فایل درس را بفرستید تا متن خام آن را بگیرید."
    return f"{action}\n\nصوت، ویدیو، PDF، Word یا PowerPoint"


#: A retried legacy label points to the chat menu rather than an inline URL.
MINI_APP_REDIRECT: Final = (
    "برای دسترسی به حساب، اعتبار، اشتراک، پرداخت‌ها، کتابخانه و کاوش، "
    "مینی‌اپ گاماس را از منوی ربات باز کنید."
)


# ---------------------------------------------------------------------------
# The always-visible menu keyboard (Telegram reply keyboard, not inline glass)
# ---------------------------------------------------------------------------

#: Old label accepted by the message router for already displayed keyboards.
BTN_MENU_GUIDE: Final = "ℹ️ راهنما"


def reply_menu_rows(*, is_admin: bool, office_enabled: bool = True) -> list[list[str]]:
    """The reply keyboard is retired: the bot has no bottom navigation.

    Kept as an empty result so stale callers (and the tests that pin the
    keyboard away) do not need a special case.
    """
    del is_admin, office_enabled
    return []


# ---------------------------------------------------------------------------
# Delivery — the results screen and the three fetch buttons
# ---------------------------------------------------------------------------

BOOKLET_READY: Final = "✅ جزوه شما آماده است."
BOOKLET_REFERRAL_CTA: Final = "🎁 اعتبار رایگان بیشتری می‌خواهی؟ دوستانت را دعوت کن و اعتبار رایگان بگیر."
BOOKLET_SUCCESS: Final = BOOKLET_READY + "\n\n" + BOOKLET_REFERRAL_CTA
INSUFFICIENT_CREDIT: Final = "💳 اعتبار شما کافی نیست. برای ادامه، بخش اعتبار را باز کنید."

DELIVERY_READY_HEADER: Final = "پردازش تمام شد — {reference}"
DELIVERY_READY_BODY: Final = "دکمه‌های خروجی‌های آماده در همین پیام هستند."
DELIVERY_ROW_BOOKLET: Final = "جزوهٔ PDF آماده است."
DELIVERY_ROW_TRANSCRIPT: Final = "متن خام گفتار را می‌توانید در گفتگو بخوانید."
DELIVERY_ROW_TXT: Final = "متن خام گفتار به‌صورت فایل TXT آماده است."
DELIVERY_ROW_KEYPOINTS: Final = "نکات کلیدی آماده است."
DELIVERY_ROW_DOCX: Final = "نسخهٔ Word آماده است."
DELIVERY_ROW_LEARNING: Final = "۲۰ سؤال نمونه از همین جزوه آمادهٔ ساخت است."
DELIVERY_ROW_NOTES_PENDING: Final = "نکات کلیدی در حال آماده‌شدن است."
DELIVERY_SPEAKERS_TEMPLATE: Final = "گویندگان شناسایی‌شده: {count} نفر"
DELIVERY_SPEAKERS_ONE: Final = "یک گوینده شناسایی شد."
DELIVERY_SPEAKERS_NONE: Final = "گویندهٔ جداگانه‌ای شناسایی نشد."
DELIVERY_TRANSCRIPT_READY: Final = "متن خام — {reference}"
DELIVERY_KEYPOINTS_READY: Final = "نکات کلیدی — {reference}"
DELIVERY_ARTIFACT_GONE: Final = (
    "این خروجی دیگر در دسترس نیست؛ دوباره فایل را بفرستید تا از نو ساخته شود."
)
DELIVERY_ARTIFACT_RETRY: Final = (
    "ارسال این فایل موقتاً انجام نشد؛ کمی بعد همان دکمه را دوباره بزنید."
)
DELIVERY_KEYPOINTS_CHAT_HEADER: Final = "⭐ نکات کلیدی — {reference}"
DELIVERY_KEYPOINTS_EMPTY: Final = "برای این فایل نکتهٔ کلیدی ثبت نشد."
DELIVERY_NO_OUTPUT: Final = "برای این پردازش هنوز خروجی‌ای در دسترس نیست."
RESULTS_HUB_TITLE: Final = "نتایج پردازش\n\nوضعیت فایل‌ها و خروجی‌های آماده را از اینجا ببینید."
RESULTS_HUB_PAGE_TEMPLATE: Final = "صفحهٔ {page} از {pages}"
RESULTS_HUB_EMPTY: Final = (
    "هنوز پردازشی ندارید.\n\nیک فایل درس بفرستید تا وضعیت و خروجی‌های آماده در نتایج پردازش نمایش داده شوند."
)
RESULTS_HUB_EMPTY_RAW_ONLY: Final = (
    "هنوز پردازشی ندارید.\n\n"
    "فایل صوتی یا ویدیویی بفرستید تا متن گفتار ذخیره شود؛ "
    "سندهای درسی را از کتابخانه ببینید."
)


def results_hub_empty(*, notes_configured: bool) -> str:
    """Return an empty-state promise that matches the configured job pipeline."""
    return RESULTS_HUB_EMPTY if notes_configured else RESULTS_HUB_EMPTY_RAW_ONLY


RESULTS_HUB_ROW_TEMPLATE: Final = "{reference} — {status}"
RESULTS_STATUS: Final = {
    "pending": "در انتظار",
    "processing": "در حال پردازش",
    "done": "آماده",
    "partial": "آماده (با هشدار)",
    "failed": "ناموفق",
    "rejected": "رد‌شده",
}


def delivery_screen(
    *,
    reference: str,
    speakers: int | None,
    has_booklet: bool,
    has_keypoints: bool = True,
    notes_pending: bool = False,
    learning_available: bool = True,
    has_transcript: bool = True,
    has_txt: bool = False,
    has_docx: bool = False,
    has_review_html: bool = False,
    has_learning_item: bool = True,
) -> str:
    """Describe only the artifacts and actions exposed by this message."""
    from .progress import to_persian_digits

    lines = [DELIVERY_READY_HEADER.format(reference=reference), ""]
    if speakers is not None:
        if speakers > 1:
            lines.append(
                DELIVERY_SPEAKERS_TEMPLATE.format(count=to_persian_digits(speakers))
            )
        elif speakers == 1:
            lines.append(DELIVERY_SPEAKERS_ONE)
        else:
            lines.append(DELIVERY_SPEAKERS_NONE)
        lines.append("")
    if has_booklet:
        lines.append(DELIVERY_ROW_BOOKLET)
    if has_transcript:
        lines.append(DELIVERY_ROW_TRANSCRIPT)
    if has_txt:
        lines.append(DELIVERY_ROW_TXT)
    if notes_pending:
        lines.append(DELIVERY_ROW_NOTES_PENDING)
    elif has_keypoints:
        lines.append(DELIVERY_ROW_KEYPOINTS)
    if has_docx:
        lines.append(DELIVERY_ROW_DOCX)
    if has_review_html:
        lines.append(DELIVERY_ROW_REVIEW_HTML)
    if has_booklet and has_learning_item and learning_available:
        lines.append(DELIVERY_ROW_LEARNING)
    elif has_booklet and not learning_available:
        lines.append(LEARNING_GENERATION_UNAVAILABLE)
    has_any_output = any(
        (
            has_booklet,
            has_transcript,
            has_txt,
            has_keypoints,
            notes_pending,
            has_docx,
            has_review_html,
        )
    )
    if not has_any_output:
        lines.append(DELIVERY_NO_OUTPUT)
    lines.extend(["", DELIVERY_READY_BODY])
    return "\n".join(lines)


def keypoints_text(*, reference: str, points: Sequence[str], limit: int = 12) -> str:
    """The key-points artifact as chat text (no file for a dozen lines)."""
    body = [f"• {point}" for point in points[:limit] if str(point).strip()]
    if not body:
        body = [DELIVERY_KEYPOINTS_EMPTY]
    return "\n".join(
        [DELIVERY_KEYPOINTS_CHAT_HEADER.format(reference=reference), "", *body]
    )


def help_screen(
    *,
    office_enabled: bool,
    notes_configured: bool = True,
) -> str:
    """A short guide: what to send, what comes back, the two bot commands.

    Everything that used to be a manual here (library, credits, referrals,
    quiz building) is one line pointing at the place that owns it.
    """
    if notes_configured:
        outputs = "متن خام، جزوهٔ PDF، نسخهٔ Word و ۲۰ سؤال نمونه"
    else:
        outputs = "متن خام و فایل‌های استخراج‌شده"
    lines = [
        "ℹ️ راهنمای گاماس",
        "",
        "📤 فایل بفرستید: صوت، ویدیو، ویس، PDF، Word یا PowerPoint",
        f"✅ در پایان می‌گیرید: {outputs}",
    ]
    if not notes_configured:
        lines.append("⚠️ ساخت خودکار جزوه و آزمون هوشمند فعلاً فعال نیست.")
    lines.extend(
        [
            "🛒 خرید اعتبار: /buy    🎧 پشتیبانی: /support",
            "🚀 حساب، کتابخانه، مرور و کاوش: مینی‌اپ",
        ]
    )
    if office_enabled:
        lines.append("✍️ آفیس رایگان: /office")
        lines.append(OFFICE_CREDIT_NOTICE)
    lines.extend(
        [
            "🎧 صدای PowerPoint خودکار پردازش نمی‌شود؛ "
            "دکمهٔ پردازش صدا را از کارت سند بزنید.",
            DOCUMENT_SCANNED_NOTICE,
            DOCUMENT_EMBEDDED_IMAGES_NOTICE,
        ]
    )
    return "\n".join(lines)


# --------------------------------------------------------------------- library
UNLIMITED_BALANCE: Final = "نامحدود ⭐ (کاربر ویژه)"
LIBRARY_TITLE: Final = "📚 کتابخانه من"
LIBRARY_HOME_SUBTITLE: Final = (
    "فقط فایل‌هایی که از ربات دریافت کرده‌اید این‌جا دسته‌بندی و ذخیره می‌شوند:"
)
LIBRARY_HOME_SUMMARY: Final = (
    "📖 جزوه‌ها: {booklet}   |   🧪 کوییزها: {quiz}   |   🗂 سایر فایل‌ها: {other}"
)
LIBRARY_EMPTY: Final = (
    "📚 کتابخانه من\n\n"
    "هنوز فایلی از ربات دریافت نکرده‌اید.\n"
    "هر جزوه، کوییز تعاملی HTML یا خروجی آفیس که از ربات "
    "دریافت کنید به‌صورت خودکار و دسته‌بندی‌شده این‌جا ذخیره می‌شود."
)
LIBRARY_EMPTY_RAW_ONLY: Final = (
    "📚 کتابخانه من\n\n"
    "هنوز فایلی از ربات دریافت نکرده‌اید.\n"
    "هر فایل متنی یا خروجی آفیس که از ربات دریافت کنید این‌جا ذخیره می‌شود."
)
LIBRARY_CATEGORY_TITLES: Final[dict[str, str]] = {
    "booklet": "📖 جزوه‌ها",
    "quiz": "🧪 کوییزها (HTML تعاملی)",
    "other": "🗂 سایر فایل‌ها",
}
LIBRARY_CATEGORY_EMPTY: Final[dict[str, str]] = {
    "booklet": (
        "هنوز جزوه‌ای از ربات دریافت نکرده‌اید.\n"
        "فایل صوتی، ویدیویی یا سند درسی بفرستید و پس از ساخت جزوه، نسخهٔ PDF، Word "
        "یا مرور HTML آن را دریافت کنید تا این‌جا بماند."
    ),
    "quiz": (
        "هنوز کوییز تعاملی نساخته‌اید.\n"
        "از روی هر جزوه یا سند می‌توانید کوییز HTML آفلاین بسازید تا مستقیم در چت "
        "ارسال و همین‌جا ذخیره شود."
    ),
    "other": (
        "هنوز فایل خروجی آفیس یا متن خام (TXT) دریافت نکرده‌اید."
    ),
}
LIBRARY_CATEGORY_EMPTY_RAW_ONLY: Final[dict[str, str]] = {
    "booklet": (
        "هنوز جزوه‌ای از ربات دریافت نکرده‌اید.\n"
        "فایل‌های دریافتی جزوه به‌صورت خودکار این‌جا ذخیره می‌شوند."
    ),
    "quiz": (
        "ساخت آزمون هوشمند فعلاً فعال نیست."
    ),
    "other": (
        "هنوز فایل خروجی آفیس یا متن خام (TXT) دریافت نکرده‌اید."
    ),
}


def library_category_empty(category: str, *, notes_configured: bool) -> str:
    """Return category-specific empty copy honoring AI availability."""
    table = LIBRARY_CATEGORY_EMPTY if notes_configured else LIBRARY_CATEGORY_EMPTY_RAW_ONLY
    return table.get(category, LIBRARY_EMPTY_RAW_ONLY)
LIBRARY_CATEGORY_TAP_HINT: Final = "برای دریافت مجدد فایل، روی نام آن بزنید:"
LIBRARY_CATEGORY_BOOKLET_HINT: Final = (
    "برای دریافت مجدد یا ساخت کوییز، روی جزوه بزنید:"
)
LIBRARY_DELETE_CONFIRM: Final = "آیا از حذف «{title}» از کتابخانه مطمئن هستید؟"
LIBRARY_ITEM_DELETED: Final = "🗑 فایل از کتابخانه حذف شد."
ITEM_SENT_DONE: Final = "✅ فایل در گفتگو ارسال شد."


def library_empty(*, notes_configured: bool) -> str:
    """Return the library empty state without promising unavailable notes."""
    return LIBRARY_EMPTY if notes_configured else LIBRARY_EMPTY_RAW_ONLY


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
DOCUMENTS_EMPTY: Final = (
    "📄 اسناد\n\n"
    "هنوز سندی ذخیره نکرده‌اید. فایل PDF، Word یا PowerPoint را بفرستید تا متنش استخراج شود؛ "
    "بعد می‌توانید از آن سؤال بسازید."
)
DOCUMENTS_EMPTY_RAW_ONLY: Final = (
    "📄 اسناد\n\n"
    "هنوز سندی ذخیره نکرده‌اید. فایل متنی PDF، Word یا PowerPoint را بفرستید تا متنش استخراج و ذخیره شود.\n"
    "ساخت آزمون هوشمند فعلاً فعال نیست."
)


def documents_empty(*, notes_configured: bool) -> str:
    """Return a document-library empty state that matches learning availability."""
    return DOCUMENTS_EMPTY if notes_configured else DOCUMENTS_EMPTY_RAW_ONLY

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
INVALID_PAYMENTS_PAGE: Final = "صفحهٔ پرداخت‌ها نامعتبر است."
INVALID_REPORT_PAGE: Final = "صفحهٔ گزارش نامعتبر است."
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
RAW_TEXT_BOOKLET_PROMPT: Final = (
    "📝 متن درس، پیاده‌سازی یا یادداشت خام خود را در پیام بعدی بفرستید "
    "تا مستقیم به جزوهٔ علمی کامل (PDF + Word + مرور HTML) تبدیل شود.\n"
    "برای لغو، /cancel بفرستید."
)
ORDER_CANCELLED: Final = "ارسال لغو شد. لغو سفارش از بخش «اعتبار» انجام می‌شود."

# -------------------------------------------------------------------- learning
LEARNING_GENERATION_UNAVAILABLE: Final = (
    "ساخت خودکار آزمون هوشمند فعلاً فعال نیست."
)

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
    "راهنمای تنظیم در docs/ADMIN.md را ببینید."
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
    "• ورود دوست جدید با لینک شما: {signup_credits}\n"
    "• اولین خرید تأییدشدهٔ او: {purchase_credits}\n"
    "کد دعوت فقط هنگام نخستین ورود دوست به ربات ثبت می‌شود."
)
REFERRALS_LINK_TEMPLATE: Final = "لینک اختصاصی شما:\n{link}"
REFERRALS_CODE_TEMPLATE: Final = "کد دعوت شما: {code}"
REFERRALS_STATS_TEMPLATE: Final = "دعوت‌شده‌ها: {invited} | اعتبار هدیه: {seconds}"
REFERRALS_SELF: Final = "نمی‌توانید با لینک خودتان دعوت شوید."
REFERRALS_DUPLICATE: Final = "این دعوت قبلاً ثبت شده است."
REFERRAL_JOIN_REWARD_TEMPLATE: Final = (
    "🎁 هدیهٔ دعوت ثبت شد: {seconds} به دوست شما اضافه شد."
)
REFERRAL_SHARE_TEXT: Final = "به گاماس بپیوندید:"
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
WALLET_TITLE: Final = "💳 اعتبار و خرید بسته"
WALLET_NO_PLANS: Final = "فعلاً بسته‌ای برای خرید موجود نیست."
WALLET_CHOOSE_PLAN: Final = "بسته را انتخاب کنید؛ اعتبار خرید به موجودی شما اضافه می‌شود."
WALLET_FINAL_HTML_TEMPLATE: Final = "<b>قیمت نهایی: {sale}</b>"
WALLET_DISCOUNT_HTML_TEMPLATE: Final = "قیمت قبلی: <s>{original}</s>\nبا تخفیف\n<b>قیمت نهایی: {sale}</b>"
WALLET_EXACT_BUTTON: Final = "جزئیات اعتبار دقیق"
WALLET_EXACT_TITLE: Final = "مقدار دقیق اعتبار بسته‌ها و موجودی"
WALLET_EXACT_PLAN: Final = "{credits} اعتبار — {duration}"
WALLET_PLAN_ROW_TEMPLATE: Final = "• اعتبار نمایشی: {credits}\n  اعتبار بسته: {validity} | {price}"
WALLET_PLAN_BUTTON_TEMPLATE: Final = "{credits} — {price}"
#: The credit amount as it appears inside a plan button.
WALLET_PLAN_BUTTON_CREDITS: Final = "{credits} اعتبار"
#: How the balance line names the unit it is showing.

#: How a charge is described everywhere (intake, job failure, learning).
#: ``{credits}`` is already grouped and in Persian digits; the hours it covers
#: follow in parentheses so the tariff is never a mystery.
#: Intake: the file's own length, in both currencies.
WALLET_CARD_DISABLED: Final = "🛒 پرداخت کارت‌به‌کارت فعلاً غیرفعال است."
WALLET_BALANCE_TEMPLATE: Final = "💳 اعتبار باقی‌مانده: {balance}"
WALLET_DISPLAY_BALANCE_TEMPLATE: Final = "💳 اعتبار باقی‌مانده (نمایشی): {balance}"
WALLET_UNIT_HINT: Final = "📐 هر ۱۰۰٬۰۰۰ اعتبار = ۳ ساعت صوت کلاس"
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

LEARNING_PRORATA_REFUND: Final = "اعتبار بازگشتی: {duration}"
PAYMENT_HISTORY_CANCELLED: Final = "لغوشده"

DOCUMENT_EXTRACTED_COUNT: Final = "متن استخراج‌شده: {count} نویسه"
DOCUMENT_TEXT_PREVIEW: Final = "پیش‌نمایش متن:"

LEARNING_CHUNK_PROGRESS: Final = "بخش {done} از {total}"
LEARNING_PRODUCING: Final = "در حال تولید {label}"

LEARNING_QUIZ_READY: Final = "✅ آزمون آماده است."
LEARNING_HTML_SENT_QUIZ: Final = (
    "✅ فایل تعاملی آزمون ({count} سؤال) ارسال شد و در بخش «🧪 کوییزها» در کتابخانه ذخیره شد."
)
