"""Shared Persian UI copy, labels, and Telegram delivery limits for bot workflows (SRP).

Centralizing domain copy constants prevents circular imports between
:mod:`gamas_bot.bot` and its single-responsibility workflow mixins.
"""

from __future__ import annotations

RAW_PREVIEW_CHARS = 800
MAX_RAW_PREVIEW_LINES = 12
TELEGRAM_CAPTION_LIMIT = 1024
RAW_FALLBACK_MAX_PAGES = 40

MAX_OFFICE_SESSIONS = 128
MAX_OFFICE_OUTPUT_BYTES = 50 * 1024 * 1024

ACTIVE_LIMIT_MESSAGE = (
    "⏳ شما هم‌اکنون {limit} درخواست در حال پردازش دارید. "
    "بگذارید یکی از آن‌ها تمام شود و بعد این فایل را بفرستید."
)

SUPPORT_CATEGORY_BUTTONS: dict[str, str] = {
    "payment": "💳 پرداخت",
    "technical": "⚙️ مشکل فنی",
    "subscription": "📦 اشتراک و اعتبار",
    "other": "📌 سایر موارد",
}

LEARNING_LEGACY_NOTICE = (
    "متن این جزوهٔ قدیمی ذخیره نشده است.\n"
    "🧪 آزمون و 🃏 فلش‌کارت فقط برای جزوه‌های تازه کار می‌کند."
)
QUIZ_START_GUIDANCE = (
    "سؤال‌های متوسط تا سخت؛ تعداد را انتخاب کنید.\n"
    "هر پاسخ درست ۱۰ امتیاز دارد."
)
LEADERBOARD_EMPTY = (
    "🏆 لیدربورد\n\n"
    "هنوز امتیازی ثبت نشده است.\n"
    "اولین آزمون خود را از «📚 کتابخانه من» شروع کنید."
)
FLASHCARD_EMPTY = (
    "🃏 فلش‌کارت‌ها\n\n"
    "هنوز مجموعه‌ای نساخته‌اید.\n"
    "از یک جزوه بسازید یا کارت‌های دستی خودتان را اضافه کنید."
)
LEARNING_NO_CREDIT = (
    "💳 اعتبار شما کافی نیست.\n"
    "از «🛒 خرید اشتراک» اعتبار بگیرید."
)
LEARNING_GENERATION_FAILED = "ساخت محتوا ناموفق بود.\nکمی بعد دوباره امتحان کنید."
LEARNING_GENERATION_RETRY = "کمی بعد دوباره امتحان کنید."
LEARNING_CREDIT_RESTORED = "💳 اعتبار شما کامل برگشت داده شد."
DOC_EXTRACTION_STOPPED = "📄 استخراج متن سند متوقف شد."
OFFICE_UPLOAD_STOPPED = "🗂 آفیس رایگان متوقف شد."
LEARNING_DELIVERY_CTA = (
    "\n🧪 از همین جزوه آزمون بده یا 🃏 فلش‌کارت بساز: «📚 کتابخانه من»"
)
FLASHCARD_ASK_FRONT = "پرسش کارت را بفرستید:"
FLASHCARD_ASK_BACK = "پاسخ کارت را بفرستید:"
LEARNING_STALE_ALERT = "این آزمون دیگر معتبر نیست."
LEARNING_BUSY_ALERT = "درخواست قبلی شما در حال پردازش است؛ کمی صبر کنید."
LEADERBOARD_FALLBACK_NAME = "کاربر گاماس"
QUIZ_COUNTS = (5, 10, 15, 20)
QUIZ_POINTS_PER_ANSWER = 10
FREE_TRY_BADGE = "🎁 اولین بار رایگان"
FREE_TRY_USED_NOTICE = (
    "🎁 هدیهٔ اولین بار شما برای این بخش استفاده شده است؛ "
    "برای ساخت بعدی به اعتبار نیاز دارید."
)
FREE_TRY_RESTORED = "🎁 هدیهٔ اولین بار شما برگشت؛ دوباره تلاش کنید."
LEARNING_REFUND_PENDING_NOTICE = (
    "بازگشت اعتبار یا هدیه فعلاً ناموفق بود؛ لطفاً با پشتیبانی تماس بگیرید."
)
CURATION_GUIDANCE = (
    "هر مورد را که نمی‌خواهید حذف کنید، بعد «✅ ذخیره» را بزنید."
)
CURATION_STALE_ALERT = "این پیش‌نمایش دیگر معتبر نیست؛ دوباره بسازید."
LEARNING_PROGRESS_EDIT_TIMEOUT_SECONDS = 1.0

DOC_UNSUPPORTED_HINT = (
    "⚠️ فقط PDF، Word (docx) یا PowerPoint (pptx) به‌عنوان سند پذیرفته می‌شود."
)
DOC_PHOTO_HINT = (
    "📷 عکس سند به‌تنهایی خوانده نمی‌شود (OCR پشتیبانی نمی‌شود).\n"
    "لطفاً فایل را با گزینهٔ «ارسال به‌صورت فایل» و در قالب PDF/DOCX/PPTX بفرستید."
)
DOC_OCR_FAILED = (
    "⚠️ خواندن تصویری سند ناموفق بود (سرویس OCR در دسترس نیست یا پاسخ نداد).\n"
    "کمی بعد دوباره تلاش کنید یا نسخهٔ متنی/Word فایل را بفرستید."
)
DEFAULT_INTAKE_SUFFIX: dict[str, str] = {"image": ".jpg"}
DOC_GENERATE_CARDS = "🃏 ساخت فلش‌کارت"
DOC_GENERATE_QUESTIONS = "❓ ساخت سؤالات"

SUPPORT_STATUS_LABELS: dict[str, str] = {
    "open": "🆕 باز",
    "awaiting_admin": "⏳ در انتظار پاسخ پشتیبانی",
    "awaiting_user": "💬 پاسخ پشتیبانی ثبت شد",
    "closed": "✅ بسته",
}
VIEW_LABELS: dict[str, str] = {
    "active": "🆕 فعال",
    "open": "📂 جدید",
    "wait": "⏳ منتظر پاسخ",
    "user": "💬 منتظر کاربر",
    "closed": "✅ بسته‌شده",
}
CATEGORY_FILTER_LABELS: dict[str, str] = {
    "all": "همه دسته‌ها",
    **SUPPORT_CATEGORY_BUTTONS,
}
