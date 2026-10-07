from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

from .config import NOTE_MODES, RESERVED_HEADER_NAMES, Settings, resolve_note_mode
from .progress import to_persian_digits
from .provider_credentials import current_provider_credentials
from .qa import heading_topic_key, headings_overlap, headings_share_a_topic, notes_text, run_note_qa
from .textnorm import normalize_for_compare

logger = logging.getLogger(__name__)


class StructuringError(RuntimeError):
    pass


class ProviderHTTPError(StructuringError):
    """HTTP result retained for safe credential cooldown/quarantine decisions."""

    def __init__(
        self, message: str, *, status: int, retry_after_seconds: float | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after_seconds = retry_after_seconds


class ProviderTransientError(StructuringError):
    """Network failure after the configured bounded retry budget."""


ERROR_DETAIL_LIMIT = 180

#: Maximum accepted lengths when normalising model-supplied strings.
MAX_TITLE_CHARS = 200
MAX_TEXT_CHARS = 20000


# ---------------------------------------------------------------------------
# Note modes: how much compression the note generator applies.
# (NOTE_MODES and resolve_note_mode live in config.py; re-exported here so
# callers of structuring keep one import surface.)
# ---------------------------------------------------------------------------


#: Per-mode writing rules. ``full`` (the default) compiles a lecture into
#: notes while preserving explanations, examples and procedures; ``summary``
#: is the only intentionally concise mode.
MODE_RULES = {
    "full": (
        "- حالت خروجی: FULL. شما «گردآورندهٔ جزوهٔ درس» هستید، نه خلاصه‌ساز. مطلبی را که گوینده برای یادگیری لازم می‌داند حذف نکنید.\n"
        "- هر تعریف، توضیح، دلیل، مکانیزم، مثال، روند گام‌به‌گام و مقایسه را کامل بیاورید؛ جزوه باید جایگزین قابل‌اتکای حضار در کلاس باشد.\n"
        "- تکرارهایی که برای تأکید یا روشن‌شدن موضوع به‌کار رفته‌اند را نگه دارید؛ فقط تکرارهای سرهم و عین‌هم را یک بار بنویسید.\n"
        "- توضیح‌های مفصل گوینده را در همان بخشِ موضوعی، چند پاراگراف کامل بنویسید؛ یک توضیح چندجمله‌ای را به یک خط خلاصه فشرده نکنید.\n"
    ),
    "standard": (
        "- حالت خروجی: STANDARD. میان حفظ کامل مطالب و روانی جزوه تعادل برقرار کنید؛ توضیح‌های اصلی و مثال‌های مهم را نگه دارید و فقط پرگویی‌های آشکار را حذف کنید.\n"
    ),
    "summary": (
        "- حالت خروجی: SUMMARY. جزوهٔ کوتاه و فشرده بنویسید؛ فقط موضوع‌های اصلی، تعریف‌های کلیدی و اعداد مهم را بیاورید. مثال‌های فرعی و توضیح‌های طولانی را مختصر کنید.\n"
    ),
}

#: Shared content rules — the preservation contract every mode inherits.
_CONTENT_RULES = (
    "\nقواعد حفظ محتوا — مهم‌ترین بخش دستور است:\n"
    "- فقط بر پایهٔ متن داده‌شده بنویسید؛ اطلاعات، فرمول، تعریف یا نتیجهٔ تازه نسازید. اگر بخشی نامفهوم است، آن را حدس نزنید و همان‌قدر که فهمیده‌اید بنویسید.\n"
    "- هیچ عدد، واحد، درصد، دوز دارو، مقدار آزمایشگاهی یا علامت اختصاری را حذف یا تغییر ندهید؛ صورت دقیق آن‌ها را عیناً بیاورید.\n"
    "- اصطلاح‌های تخصصی و عبارت‌های انگلیسی (نام دارو، دستگاه، مفهوم علمی و مخفف‌ها) را به همان شکل انگلیسی و بدون ترجمهٔ اجباری داخل متن فارسی حفظ کنید؛ ترجمهٔ فارسی رایج را می‌توانید در پرانتز بیاورید.\n"
    "- تعریف‌ها را در آرایهٔ definitions بیاورید (term، term_en اختیاری، definition)؛ مثال‌ها را در examples؛ روند یا دستورالعمل گام‌به‌گام را در steps؛ فرمول‌ها و معادله‌ها را با صورت دقیق‌شان در formulas بنویسید.\n"
    "- توضیح‌های مهم گوینده را به‌جای یک خط کوتاه، پاراگراف کامل بنویسید؛ جزوه باید درس را بدون شنیدن صدا قابل فهم کند.\n"
    "- نکته‌های کلیدی، یادآوری‌ها و هشدارهای مهم را در callouts یا key_points جداگانه برجسته کنید.\n"
    "- مطالب را با عنوان‌های بخش کوتاه و گویا مرتب کنید و ترتیب منطقی متن اصلی را نگه دارید.\n"
    "- اگر چند مورد قابل مقایسه یا دسته‌بندی وجود دارد، از table استفاده کنید؛ جدول را بی‌دلیل به کار نبرید.\n"
    "- فقط حذف‌های مجاز: پرگویی بی‌محتوا، اصطلاح‌های گفتاری تصادفی، نویزِ پیاده‌سازی صدا و تکرار عین‌هم. هیچ توضیح آموزشی را به‌خاطر کوتاهی حذف نکنید.\n"
)

#: The semantic contract: *what* must survive. The enumerated list is the
#: definition of an "educational unit"; the allowed/forbidden lists make the
#: deletion policy unambiguous, so a model cannot read "compile" as "shorten".
_SEMANTIC_RULES = (
    "\nقرارداد معنایی — چه چیزی باید در جزوه حاضر باشد:\n"
    "- شما ویرایشگر جزوه هستید، نه خلاصه‌ساز: هر واحد آموزشی که در متن منبع آمده باید در جزوه هم بیاید. "
    "واحد آموزشی یعنی: تعریف، توضیح، مکانیزم و چرایی، مثال، مثال نقض، روند گام‌به‌گام، مقایسه، "
    "هشدار، استثنا، فرمول، عدد و اندازه‌گیری، تاریخ، نام‌های خاص، اصطلاح انگلیسی، مخفف، و نتیجه‌ای "
    "که گوینده صریحاً بیان کرده است.\n"
    "- فقط این چهار چیز را می‌توانید حذف کنید: پرگویی و مکث، گفت‌وگوی حاشیه‌ای بی‌محتوا، نویزِ آشکار "
    "پیاده‌سازی صدا، و تکرار عیناً لفظ‌به‌لفظ.\n"
    "- این‌ها را هرگز حذف نکنید: توضیح آموزشی، مثال، زمینه‌ای که گفته برای فهم آن لازم است، جزئیات "
    "عددی، اصطلاح تخصصی؛ و هرگز یک توضیح چندجمله‌ای را فقط برای کوتاه‌شدن جزوه با یک جملهٔ کوتاه "
    "جایگزین نکنید.\n"
    "- اگر نکته‌ای را با کلمات خودتان می‌نویسید، باید دقیقاً همان معنا و همان روابط متن اصلی باشد؛ "
    "هیچ ادعا، آمار، مرجع یا توصیهٔ تازه‌ای اضافه نکنید.\n"
)

#: How the compiled lecture must *read*. The rules are the difference between
#: a pile of summarized sentences and a handout a professor would hand out.
_STYLE_RULES = (
    "\nقواعد نگارش (لحن جزوهٔ یک استاد):\n"
    "- مفهوم را معرفی کنید، اگر گوینده دلیل یا ضرورتش را گفته همان را توضیح دهید، بعد مکانیزم و "
    "جزئیات را بیاورید، بعد پیوند آن را با مفهوم پیشین نشان دهید و در پایان — فقط اگر گوینده گفته — "
    "یک نتیجهٔ کوتاه بنویسید.\n"
    "- هر بخش باید در همان جملهٔ اول بگوید موضوعش چیست و چگونه به مطلب قبلی وصل می‌شود؛ شروع بخش‌ها "
    "را قالبی و یکسان تکرار نکنید.\n"
    "- توضیح‌ها را در پاراگراف کامل و روان بنویسید؛ هر جمله را به یک بولت تبدیل نکنید. "
    "بولت را فقط برای فهرست‌های واقعی (اقلام هم‌رده، مراحل، ویژگی‌ها) به کار ببرید.\n"
    "- عبارت‌های کلیشه‌ای مثل «نکتهٔ مهم»، «در ادامه»، «همان‌طور که گفته شد» را بی‌دلیل تکرار نکنید؛ "
    "فقط وقتی خواندن را روان‌تر می‌کنند به کار ببرید.\n"
    "- جزئیات را ناگهانی و بی‌مقدمه نیاورید؛ اگر گوینده اول دلیل یا زمینه را گفته، همان ترتیب را حفظ کنید.\n"
    "- ادعای علمی، آمار، مرجع، توصیهٔ درمانی یا مثال تازه از خود اضافه نکنید؛ هرچه در متن نیست، نوشته نمی‌شود.\n"
    "- اگر بخشی از متن مبهم یا ناقص است، همان ابهام را حفظ کنید؛ معنای احتمالی را حدس نزنید و آن را "
    "«بهتر» یا «منطقی‌تر» بازنویسی نکنید.\n"
    "- اصطلاح تخصصی و انگلیسی را داخل جملهٔ فارسی به همان صورت اصلی بنویسید.\n"
)

#: The relationship-preservation contract: a coherent booklet keeps the causal
#: and temporal links the lecturer actually made.
_TRANSITION_RULES = (
    "\nقواعد پیوند و انسجام:\n"
    "- روابط متن را با همان کلمات ربط منبع حفظ کنید: علت و معلول (چون، زیرا، بنابراین، در نتیجه)، "
    "تضاد (اما، در مقابل، برخلاف)، مثال (برای نمونه، برای مثال)، ترتیب زمانی (نخست، اول، سپس، "
    "در پایان).\n"
    "- اگر دو مفهوم پشت‌سرهم به یک موضوع واحد تعلق دارند، آن‌ها را در یک بخش با یک گذر طبیعی بنویسید؛ "
    "اگر گوینده به موضوع تازه‌ای رفته، بخش تازه بسازید و دو موضوع بی‌ربط را در یک بخش قاطی نکنید.\n"
    "- ترتیب موضوع‌ها را جابه‌جا نکنید و از خودتان گذار تازه نسازید.\n"
    "- اگر موضوعی ادامهٔ موضوع قبلی است، پیوند را با یک جملهٔ کوتاه نشان دهید؛ اگر موضوع تازه است، "
    "بخش تازه بسازید و دو موضوع را در یک بخش قاطی نکنید.\n"
)

_JSON_RULES = (
    "\nقواعد خروجی — مطلق‌اند و استثنا ندارند:\n"
    "۱) پاسخ فقط و فقط یک شیء JSON معتبر است. هیچ متن، عنوان، توضیح، علامت نقل‌قول بلوکی یا خنده‌کد (code fence) قبل یا بعد از آن ننویسید.\n"
    "۲) ساختار دقیق JSON این است:\n"
    "{\n"
    "  \"title\": \"عنوان کوتاه جزوه\",\n"
    "  \"summary\": \"خلاصهٔ چند جمله‌ایِ موضوع و هدف جزوه\",\n"
    "  \"learning_objectives\": [\"هدف یادگیری برگرفته از متن (اختیاری)\"],\n"
    "  \"sections\": [\n"
    "    {\n"
    "      \"heading\": \"عنوان بخش\",\n"
    "      \"paragraphs\": [\"پاراگراف توضیحی\"],\n"
    "      \"bullets\": [\"مورد فهرستی\"],\n"
    "      \"definitions\": [{\"term\": \"اصطلاح\", \"term_en\": \"English term\", \"definition\": \"تعریف کامل\"}],\n"
    "      \"examples\": [\"مثال کامل همراه با توضیح\"],\n"
    "      \"steps\": [\"گام ۱ …\", \"گام ۲ …\"],\n"
    "      \"formulas\": [\"صورت دقیق فرمول\"],\n"
    "      \"key_points\": [\"نکتهٔ کلیدی همین بخش\"],\n"
    "      \"table\": {\"headers\": [\"ستون ۱\", \"ستون ۲\"], \"rows\": [[\"مقدار\", \"مقدار\"]]},\n"
    "      \"callouts\": [{\"kind\": \"نکته\", \"text\": \"متن برجسته\"}]\n"
    "    }\n"
    "  ],\n"
    "  \"key_points\": [\"نکته‌های کلیدی کل جزوه\"],\n"
    "  \"review_questions\": [\"پرسش مرور برگرفته از متن (اختیاری)\"],\n"
    "  \"glossary\": [{\"term\": \"اصطلاح\", \"definition\": \"تعریف کوتاه\"}]\n"
    "}\n"
    "۳) هر کلید اختیاری است؛ اگر محتوایی برایش ندارید آن را حذف کنید یا آرایه/رشتهٔ خالی بدهید. کلید تازه‌ای از خودتان نسازید.\n"
    "۳-۱) «learning_objectives» و «review_questions» را فقط وقتی بنویسید که خودِ متن صریحاً پشتوانهٔ آن‌ها باشد؛ "
    "هدف یا پرسشی که در متن نیست، نسازید.\n"
    "۴) «sections» را خالی نگذارید؛ دست‌کم یک بخش با عنوان معنادار و محتوای واقعی بسازید.\n"
    "۵) در callouts مقدار kind فقط یکی از این سه باشد: «نکته»، «هشدار» یا «یادآوری».\n"
    "۶) JSON باید بدون خطا قابل خواندن باشد: از نقل‌قول دوگانه استفاده کنید، کامای اضافه نگذارید و خط جدید داخل رشته‌ها را با \\n بنویسید.\n"
)


#: Sent as ``reminder`` on the optional repair pass. It names the exact
#: signals QA found missing so the model can *re-read the source and restore
#: them* — it is explicitly forbidden from adding anything that is not in the
#: source, so a repair can raise coverage but never hallucinate.
REPAIR_REMINDER = (
    "\n\nهشدار کیفیت: بخشی از اطلاعات مهم متن ورودی در جزوه نیامده است. "
    "کار شما «طولانی‌تر کردن» جزوه نیست؛ کار شما بازگرداندن همان مطالب گم‌شده "
    "است، در حالی که هر چیزی که درست نوشته شده عیناً حفظ شود. "
    "متن ورودی را دوباره به‌دقت بخوانید و هر تعریف، توضیح، مثال، مرحله، مقایسه، "
    "هشدار، استثنا، فرمول، عدد، واحد، دوز، درصد و اصطلاح انگلیسی‌ای را که جا افتاده "
    "بود با همان معنا و همان اصطلاح بازگردانید. "
    "فقط و فقط اطلاعاتی را بنویسید که در همین متن ورودی آمده است؛ چیزی حدس نزنید و "
    "هیچ مثال، فرمول، مرجع یا نتیجه‌ای از خودتان اضافه نکنید. ترتیب منطقی متن را حفظ کنید. "
    "خروجی همچنان فقط و فقط یک شیء JSON معتبر با همان ساختار قبلی است."
)


def build_repair_prompt(
    document: str,
    missing: tuple[str, ...],
    findings: tuple[str, ...],
    existing: str = "",
    missing_units: tuple[str, ...] = (),
) -> str:
    """A targeted second-pass prompt naming what QA found missing.

    The second call is *not* a re-summarisation and is explicitly **not** a
    request to "make the notes longer". It receives:

    * the same source text, so every restoration is grounded in the lecture;
    * the existing notes, so correct material is preserved rather than
      regenerated (a blind rewrite could drop something that was already right);
    * the exact missing signals and the missing educational units.

    The reminder forbids invention, so a repair can only raise coverage, never
    fabricate content.
    """
    parts = [document]
    if existing:
        parts.append(
            "### جزوهٔ تولیدشده در تلاش قبلی (درست است؛ آن را حفظ کنید)\n" + existing
        )
    if missing:
        parts.append(
            "### گزارش کیفیت خودکار — سیگنال‌های غایب\n"
            "این سیگنال‌ها باید عیناً از متن بالا بازگردانده شوند: "
            + "، ".join(missing)
        )
    if missing_units:
        parts.append(
            "### گزارش کیفیت خودکار — مطالب آموزشی غایب\n"
            "این بخش‌ها از متن بالا در جزوه نیامده‌اند و باید با همان معنا بازگردانده شوند:\n"
            + "\n".join(f"- {unit}" for unit in missing_units)
        )
    if not missing and not missing_units:
        parts.append("### گزارش کیفیت خودکار\nموارد اعلام‌شده بازگردانده نشدند.")
    parts.append(
        "اگر موردی در متن بالا وجود ندارد، آن را نسازید و فقط همان‌قدر که در متن هست بنویسید."
    )
    return "\n\n".join(parts)


def build_system_prompt(mode: str = "full", *, context_block: str = "") -> str:
    """The Persian system prompt for one note mode (lecture-to-notes compiler).

    ``context_block`` carries the lecture's global orientation (title, ordered
    topics, key terminology, position of this part, neighbour sentences) when a
    long lecture is written chunk by chunk, plus the rules that stop a chunk
    from behaving like a lecture of its own. It is empty for a single-part
    lecture, which keeps the classic one-call prompt byte-compatible.
    """
    mode_rule = MODE_RULES.get(mode, MODE_RULES["full"])
    prompt = (
        "شما دستیار آموزشی فارسی «گاماس» هستید و نقش شما «گردآورندهٔ جزوهٔ درس» است، نه خلاصه‌ساز. "
        "ورودی شما متن پیاده‌سازی‌شدهٔ خام یک کلاس درسی است و خروجی شما باید یک جزوهٔ ساختارمند، کامل و "
        "روان فارسی باشد که دانشجو بتواند جای شنیدن صدا، آن را بخواند و درس را بفهمد.\n\n"
        + _JSON_RULES
        + "\n\nقواعد محتوا:\n"
        + mode_rule
        + _SEMANTIC_RULES
        + _CONTENT_RULES
        + _STYLE_RULES
        + _TRANSITION_RULES
    )
    if context_block:
        prompt += "\n\n" + _chunked_content_rules() + "\n" + context_block
    return prompt


SYSTEM_PROMPT = build_system_prompt("full")

#: The user-message wrapper for a raw lecture transcript.
TRANSCRIPT_PROMPT = "متن پیاده‌سازی‌شدهٔ خام:\n\n"


_PRESENTATION_CONTENT_RULES = (
    "\nقواعد ویژهٔ ارائه:\n"
    "- ورودی شامل متن اسلایدها (با شمارهٔ اسلاید و یادداشت گوینده) و متن پیاده‌سازی‌شدهٔ صدای ارائه است.\n"
    "- ترتیب بخش‌ها را از ترتیب اسلایدها بگیرید و توضیح‌های صوتی هر اسلاید را زیر همان موضوع ادغام کنید.\n"
    "- اگر صدا مطلبی فراتر از متن اسلاید دارد، آن را به‌عنوان توضیح کامل‌کننده بیاورید؛ اسلاید و صدا هر دو را پوشش دهید، نه فقط یکی را.\n"
    "- یادداشت گویندهٔ هر اسلاید جزو محتوای آموزشی است؛ آن را حذف نکنید.\n"
    "- تعریف‌ها را در definitions، مثال‌ها را در examples، روند گام‌به‌گام را در steps و فرمول‌ها را در formulas هر بخش بیاورید.\n"
    "- نکته‌های کلیدی، یادآوری‌ها و هشدارهای مهم گوینده را در callouts یا key_points جداگانه برجسته کنید.\n"
    "- اگر چند مورد قابل مقایسه یا دسته‌بندی وجود دارد، از table استفاده کنید؛ جدول را بی‌دلیل به کار نبرید.\n"
    "- برای فرمول‌ها و اصطلاح‌های تخصصی، صورت اصلی را حفظ کنید و متن را به فارسی روان بنویسید.\n"
)


def build_presentation_system_prompt(mode: str = "full", *, context_block: str = "") -> str:
    """The Persian system prompt for presentation material in one note mode."""
    mode_rule = MODE_RULES.get(mode, MODE_RULES["full"])
    prompt = (
        "شما دستیار آموزشی فارسی «گاماس» هستید. ورودی شما محتوای یک فایل ارائهٔ درسی (PowerPoint) است — "
        "شامل متن اسلایدها، یادداشت‌های گوینده و متن پیاده‌سازی‌شدهٔ صدای ضبط‌شدهٔ همان ارائه — و خروجی شما "
        "یک جزوهٔ ساختارمند و کامل فارسی است؛ نقش شما «گردآورندهٔ جزوهٔ درس» است که محتوای ارائه را منظم و "
        "کامل نگه می‌دارد، نه خلاصه‌سازی که حذف می‌کند.\n"
        + _JSON_RULES
        + "\n\nقواعد محتوا:\n"
        + mode_rule
        + _SEMANTIC_RULES
        + _PRESENTATION_CONTENT_RULES
        + "\n- فقط بر پایهٔ مطالب داده‌شده بنویسید؛ اطلاعات، فرمول، تعریف یا نتیجهٔ تازه نسازید. اگر بخشی نامفهوم است، آن را حدس نزنید.\n"
        + "- هیچ عدد، واحد، درصد، دوز دارو یا علامت اختصاری را حذف یا تغییر ندهید؛ اصطلاح‌های انگلیسی را بدون ترجمهٔ اجباری حفظ کنید.\n"
        + "- اگر متن ناقص یا تکراری است، مفهوم موجود را مرتب کنید و چیزی به آن نیفزایید.\n"
        + _STYLE_RULES
        + _TRANSITION_RULES
    )
    if context_block:
        prompt += "\n\n" + _chunked_content_rules() + "\n" + context_block
    return prompt


PRESENTATION_SYSTEM_PROMPT = build_presentation_system_prompt("full")


def _chunked_content_rules() -> str:
    """The part-of-a-long-lecture contract, imported lazily to avoid a cycle."""
    from .editorial import CHUNKED_CONTENT_RULES

    return CHUNKED_CONTENT_RULES

#: The user-message wrapper for combined slide text and narration.
PRESENTATION_PROMPT = "محتوای ارائه:\n\n"

#: Appended when a first answer failed JSON validation: one bounded repair pass.
JSON_REMINDER = (
    "\n\nیادآوری مهم: پاسخ قبلی JSON معتبر نبود. این بار فقط و فقط یک شیء JSON "
    "معتبر با ساختار خواسته‌شده برگردانید؛ بدون هیچ متن، توضیح یا خنده‌کد اضافه."
)


# ---------------------------------------------------------------------------
# Structured notes model
# ---------------------------------------------------------------------------

VALID_CALLOUT_KINDS = ("نکته", "هشدار", "یادآوری")


def _bounded_text(value: object, limit: int = MAX_TEXT_CHARS) -> str:
    """Model-supplied value as a bounded, stripped string."""
    return str(value if value is not None else "").strip()[:limit]


def _string_list(value: object) -> list[str]:
    """Model-supplied list as a list of non-empty bounded strings."""
    if not isinstance(value, (list, tuple)):
        return []
    return [_bounded_text(item) for item in value if _bounded_text(item)]


@dataclass(frozen=True, slots=True)
class NoteCallout:
    kind: str
    text: str

    @classmethod
    def from_payload(cls, payload: object) -> "NoteCallout | None":
        if not isinstance(payload, dict):
            return None
        text = _bounded_text(payload.get("text"))
        if not text:
            return None
        kind = _bounded_text(payload.get("kind"), 20)
        if kind not in VALID_CALLOUT_KINDS:
            kind = "نکته"
        return cls(kind, text)


@dataclass(frozen=True, slots=True)
class NoteTable:
    headers: list[str]
    rows: list[list[str]]

    @classmethod
    def from_payload(cls, payload: object) -> "NoteTable | None":
        if not isinstance(payload, dict):
            return None
        headers = _string_list(payload.get("headers"))
        raw_rows = payload.get("rows")
        if not headers or not isinstance(raw_rows, (list, tuple)):
            return None
        rows: list[list[str]] = []
        for raw_row in raw_rows:
            if isinstance(raw_row, (list, tuple)):
                cells = [_bounded_text(cell, 4000) for cell in raw_row]
            else:
                cells = [_bounded_text(raw_row, 4000)]
            if any(cell for cell in cells):
                # Keep the row aligned with the header for rendering.
                cells = (cells + [""] * len(headers))[: len(headers)]
                rows.append(cells)
        return cls(headers, rows) if rows else None


@dataclass(frozen=True, slots=True)
class NoteDefinition:
    """One term definition inside a section (glossary entries are separate)."""

    term: str
    definition: str
    term_en: str = ""

    @classmethod
    def from_payload(cls, payload: object) -> "NoteDefinition | None":
        if not isinstance(payload, dict):
            return None
        term = _bounded_text(payload.get("term"), MAX_TITLE_CHARS)
        definition = _bounded_text(payload.get("definition"))
        if not term or not definition:
            return None
        return cls(term, definition, _bounded_text(payload.get("term_en"), MAX_TITLE_CHARS))


@dataclass(frozen=True, slots=True)
class NoteSection:
    heading: str
    paragraphs: tuple[str, ...] = ()
    bullets: tuple[str, ...] = ()
    definitions: tuple[NoteDefinition, ...] = ()
    examples: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    formulas: tuple[str, ...] = ()
    key_points: tuple[str, ...] = ()
    table: NoteTable | None = None
    callouts: tuple[NoteCallout, ...] = ()

    @property
    def has_content(self) -> bool:
        return bool(
            self.paragraphs
            or self.bullets
            or self.definitions
            or self.examples
            or self.steps
            or self.formulas
            or self.key_points
            or self.table is not None
            or self.callouts
        )

    @classmethod
    def from_payload(cls, payload: object, fallback_index: int) -> "NoteSection | None":
        if not isinstance(payload, dict):
            return None
        heading = _bounded_text(payload.get("heading"), MAX_TITLE_CHARS)
        if not heading:
            heading = f"بخش {fallback_index}"
        callouts = tuple(
            filter(None, (NoteCallout.from_payload(item) for item in payload.get("callouts") or []))
        )
        definitions = tuple(
            filter(None, (NoteDefinition.from_payload(item) for item in payload.get("definitions") or []))
        )
        section = cls(
            heading=heading,
            paragraphs=tuple(_string_list(payload.get("paragraphs"))),
            bullets=tuple(_string_list(payload.get("bullets"))),
            definitions=definitions,
            examples=tuple(_string_list(payload.get("examples"))),
            steps=tuple(_string_list(payload.get("steps"))),
            formulas=tuple(_string_list(payload.get("formulas"))),
            key_points=tuple(_string_list(payload.get("key_points"))),
            table=NoteTable.from_payload(payload.get("table")),
            callouts=callouts,
        )
        return section if section.has_content else None


@dataclass(frozen=True, slots=True)
class GlossaryEntry:
    term: str
    definition: str

    @classmethod
    def from_payload(cls, payload: object) -> "GlossaryEntry | None":
        if not isinstance(payload, dict):
            return None
        term = _bounded_text(payload.get("term"), MAX_TITLE_CHARS)
        definition = _bounded_text(payload.get("definition"))
        return cls(term, definition) if term and definition else None


@dataclass(frozen=True, slots=True)
class StructuredNotes:
    """The validated note structure the LLM must produce as strict JSON.

    ``note_mode`` records the compression mode the notes were generated with
    so the DOCX exporter can label the deliverable and QA can judge
    coverage expectations (``full`` expects near-complete preservation).
    """

    title: str = ""
    summary: str = ""
    sections: tuple[NoteSection, ...] = ()
    key_points: tuple[str, ...] = ()
    glossary: tuple[GlossaryEntry, ...] = ()
    note_mode: str = "full"
    #: Optional, source-supported study aids. They are only ever generated when
    #: the lecture itself states the objectives/questions, and nothing is
    #: invented to fill them (see the prompt's schema rules).
    learning_objectives: tuple[str, ...] = ()
    review_questions: tuple[str, ...] = ()

    @property
    def display_title(self) -> str:
        return self.title or "جزوهٔ کلاس"

    @property
    def has_content(self) -> bool:
        return bool(
            self.summary
            or self.sections
            or self.key_points
            or self.glossary
            or self.learning_objectives
            or self.review_questions
        )

    def to_payload(self) -> dict:
        return {
            "title": self.title,
            "summary": self.summary,
            "learning_objectives": list(self.learning_objectives),
            "sections": [
                {
                    "heading": section.heading,
                    "paragraphs": list(section.paragraphs),
                    "bullets": list(section.bullets),
                    "definitions": [
                        {
                            "term": definition.term,
                            "term_en": definition.term_en,
                            "definition": definition.definition,
                        }
                        for definition in section.definitions
                    ],
                    "examples": list(section.examples),
                    "steps": list(section.steps),
                    "formulas": list(section.formulas),
                    "key_points": list(section.key_points),
                    "table": (
                        {
                            "headers": section.table.headers,
                            "rows": section.table.rows,
                        }
                        if section.table
                        else None
                    ),
                    "callouts": [
                        {"kind": callout.kind, "text": callout.text}
                        for callout in section.callouts
                    ],
                }
                for section in self.sections
            ],
            "key_points": list(self.key_points),
            "review_questions": list(self.review_questions),
            "glossary": [
                {"term": entry.term, "definition": entry.definition}
                for entry in self.glossary
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), ensure_ascii=False)

    def to_markdown(self) -> str:
        """Render as the Markdown subset the Telegram renderer understands."""
        parts: list[str] = [f"# {self.display_title}"]
        if self.summary:
            parts.append(f"**{self.summary}**")
        if self.learning_objectives:
            parts.append("**اهداف یادگیری**")
            parts.extend(f"- {objective}" for objective in self.learning_objectives)
        for section in self.sections:
            parts.append(f"## {section.heading}")
            parts.extend(section.paragraphs)
            if section.definitions:
                parts.append("**تعریف‌ها**")
                parts.extend(
                    f"- **{entry.term}**"
                    + (f" ({entry.term_en})" if entry.term_en else "")
                    + f": {entry.definition}"
                    for entry in section.definitions
                )
            parts.extend(f"- {bullet}" for bullet in section.bullets)
            if section.examples:
                parts.append("**مثال‌ها**")
                parts.extend(f"- {example}" for example in section.examples)
            if section.steps:
                parts.append("**مراحل انجام**")
                parts.extend(
                    f"{index}. {step}" for index, step in enumerate(section.steps, start=1)
                )
            if section.formulas:
                parts.append("**فرمول‌ها**")
                parts.extend(f"- {formula}" for formula in section.formulas)
            if section.key_points:
                parts.append("**نکته‌های کلیدی این بخش**")
                parts.extend(f"- {point}" for point in section.key_points)
            for callout in section.callouts:
                parts.append(f"**{callout.kind}:** {callout.text}")
            if section.table is not None:
                header = "| " + " | ".join(section.table.headers) + " |"
                separator = "|" + "|".join("-" * max(len(header) + 2, 3) for header in section.table.headers) + "|"
                rows = "\n".join(
                    "| " + " | ".join(row) + " |" for row in section.table.rows
                )
                parts.append(f"{header}\n{separator}\n{rows}")
        if self.key_points:
            parts.append("## نکته‌های کلیدی")
            parts.extend(f"- {point}" for point in self.key_points)
        if self.review_questions:
            parts.append("## پرسش‌های مرور")
            parts.extend(
                f"{index}. {question}"
                for index, question in enumerate(self.review_questions, start=1)
            )
        if self.glossary:
            parts.append("## واژه‌نامه")
            parts.extend(f"- **{entry.term}:** {entry.definition}" for entry in self.glossary)
        return "\n\n".join(parts)

    @classmethod
    def from_payload(cls, payload: object) -> "StructuredNotes":
        """Validate and normalise a model-supplied JSON payload."""
        if not isinstance(payload, dict):
            raise StructuringError("پاسخ سرویس تولید جزوه یک شیء JSON نبود.")
        raw_sections = payload.get("sections")
        sections = tuple(
            filter(
                None,
                (
                    NoteSection.from_payload(item, index)
                    for index, item in enumerate(
                        raw_sections if isinstance(raw_sections, (list, tuple)) else [],
                        start=1,
                    )
                ),
            )
        )
        raw_glossary = payload.get("glossary")
        glossary_items = raw_glossary if isinstance(raw_glossary, (list, tuple)) else []
        glossary = tuple(
            filter(None, (GlossaryEntry.from_payload(item) for item in glossary_items))
        )
        notes = cls(
            title=_bounded_text(payload.get("title"), MAX_TITLE_CHARS),
            summary=_bounded_text(payload.get("summary")),
            sections=sections,
            key_points=tuple(_string_list(payload.get("key_points"))),
            glossary=glossary,
            learning_objectives=tuple(_string_list(payload.get("learning_objectives"))),
            review_questions=tuple(_string_list(payload.get("review_questions"))),
        )
        if not notes.has_content:
            raise StructuringError("ساختار جزوهٔ دریافتی خالی بود.")
        return notes


def _compare_key(value: str) -> str:
    """Comparison key for deterministic duplicate detection.

    Two blocks that differ only in whitespace, Arabic/Persian letter variants
    or trailing punctuation are the same block; a paraphrase is not, and is
    never removed. This is the only similarity rule the merger uses.
    """
    return re.sub(r"\s+", " ", normalize_for_compare(value)).strip()


def _dedupe_by_key(values, key) -> tuple[str, ...]:
    """Keep every block in order, dropping only same-key repeats."""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value:
            continue
        identity = key(value)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(value)
    return tuple(result)


def _dedupe_definitions(definitions: tuple[NoteDefinition, ...]) -> tuple[NoteDefinition, ...]:
    """One entry per term, keeping the longest definition seen."""
    by_term: dict[str, NoteDefinition] = {}
    order: list[str] = []
    for entry in definitions:
        term_key = _compare_key(entry.term)
        if not term_key:
            continue
        existing = by_term.get(term_key)
        if existing is None:
            by_term[term_key] = entry
            order.append(term_key)
        elif len(entry.definition) > len(existing.definition):
            by_term[term_key] = entry
    return tuple(by_term[term] for term in order)


def _dedupe_callouts(callouts: tuple[NoteCallout, ...]) -> tuple[NoteCallout, ...]:
    seen: set[tuple[str, str]] = set()
    result: list[NoteCallout] = []
    for callout in callouts:
        identity = (callout.kind, _compare_key(callout.text))
        if identity in seen:
            continue
        seen.add(identity)
        result.append(callout)
    return tuple(result)


def _section_key(heading: str) -> str:
    """Heading identity used to recognise one logical section across chunks.

    A continuation marker before or after the topic ("ادامهٔ مقدمه"،
    "مقدمه (ادامه)") belongs to the *same* heading, so two adjacent sections a
    model titled that way merge back into one instead of being printed twice.
    The rule itself lives in :func:`gamas_bot.qa.heading_topic_key` so the merge
    and the QA diagnostics can never disagree about what "the same topic" means.
    """
    return heading_topic_key(heading)


def _dedupe_table_rows(rows: list[list[str]]) -> list[list[str]]:
    seen: set[tuple[str, ...]] = set()
    result: list[list[str]] = []
    for row in rows:
        identity = tuple(_compare_key(cell) for cell in row)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(row)
    return result


def _table_rows_as_bullets(table: NoteTable) -> tuple[str, ...]:
    """Render an incompatible second table as readable bullets (no data loss).

    A section carries at most one table. When two same-heading sections each
    bring their own *different* table, keeping one would silently drop the
    other, so the second is folded into labelled bullets instead.
    """
    headers = table.headers
    bullets: list[str] = []
    for row in table.rows:
        pairs = [
            f"{header}: {value}"
            for header, value in zip(headers, row, strict=False)
            if value.strip()
        ]
        if pairs:
            bullets.append("؛ ".join(pairs))
    return tuple(bullets)


def _combine_sections(first: NoteSection, second: NoteSection) -> NoteSection:
    """Join two sections that belong to one logical topic, in order."""
    if first.table is None:
        table = second.table
        extra_bullets: tuple[str, ...] = ()
    elif second.table is None:
        table = first.table
        extra_bullets = ()
    elif [_compare_key(header) for header in first.table.headers] == [
        _compare_key(header) for header in second.table.headers
    ]:
        table = NoteTable(
            list(first.table.headers),
            _dedupe_table_rows(
                [list(row) for row in first.table.rows] + [list(row) for row in second.table.rows]
            ),
        )
        extra_bullets = ()
    else:
        table = first.table
        extra_bullets = _table_rows_as_bullets(second.table)
    return replace(
        first,
        # Re-deduplicate across the two halves: a paragraph that both chunks
        # noted (a boundary sentence) must not survive twice in one section.
        paragraphs=_dedupe_by_key(first.paragraphs + second.paragraphs, _compare_key),
        bullets=_dedupe_by_key(first.bullets + second.bullets + extra_bullets, _compare_key),
        definitions=_dedupe_definitions(first.definitions + second.definitions),
        examples=_dedupe_by_key(first.examples + second.examples, _compare_key),
        steps=_dedupe_by_key(first.steps + second.steps, _compare_key),
        formulas=_dedupe_by_key(first.formulas + second.formulas, _compare_key),
        key_points=_dedupe_by_key(first.key_points + second.key_points, _compare_key),
        callouts=_dedupe_callouts(first.callouts + second.callouts),
        table=table,
    )


def _definition_keys(definitions) -> tuple[str, ...]:
    """Identity of every definition, used for cross-section redundancy checks."""
    return tuple(
        f"{_compare_key(entry.term)}|{_compare_key(entry.definition)}"
        for entry in definitions
        if _compare_key(entry.term)
    )


def _callout_keys(callouts) -> tuple[str, ...]:
    return tuple(f"{callout.kind}|{_compare_key(callout.text)}" for callout in callouts)


def _table_keys(table) -> tuple[str, ...]:
    if table is None:
        return ()
    headers = "|".join(_compare_key(header) for header in table.headers)
    return tuple(
        headers + "||" + "|".join(_compare_key(cell) for cell in row) for row in table.rows
    )


def _block_keys(section: NoteSection) -> tuple[str, ...]:
    """Identity of *every* block of a section, for the redundancy test below."""
    keys: list[str] = []
    keys.extend("p|" + _compare_key(value) for value in section.paragraphs if value)
    keys.extend("b|" + _compare_key(value) for value in section.bullets if value)
    keys.extend("d|" + key for key in _definition_keys(section.definitions))
    keys.extend("e|" + _compare_key(value) for value in section.examples if value)
    keys.extend("s|" + _compare_key(value) for value in section.steps if value)
    keys.extend("f|" + _compare_key(value) for value in section.formulas if value)
    keys.extend("k|" + _compare_key(value) for value in section.key_points if value)
    keys.extend("c|" + key for key in _callout_keys(section.callouts))
    keys.extend("t|" + key for key in _table_keys(section.table))
    return tuple(key for key in keys if not key.endswith("|"))


def _merge_sections(
    sections: list[NoteSection],
    *,
    chunk_starts: frozenset[int] = frozenset(),
) -> tuple[NoteSection, ...]:
    """Merge sections additively, in order, with conservative de-duplication.

    * verbatim repeats of a paragraph or bullet (the classic chunk-boundary
      accident) collapse to their first occurrence;
    * *adjacent* sections whose headings are the same topic merge into one —
      both inside a chunk and, with a slightly looser topic test, across a
      chunk boundary, where a model provably splits one topic in two;
    * a section whose *every* block was already seen verbatim in an earlier
      section is dropped — it is a restatement of something the booklet
      already says, and keeping it would print the same heading twice with
      duplicate definitions/callouts while its prose had already collapsed;
    * everything else — paraphrases, neighbouring explanations, different
      examples — is preserved exactly as the model wrote it.

    ``chunk_starts`` holds the indices at which a new chunk's sections begin.
    A section boundary that is *also* a chunk boundary gets the looser topic
    test, because that is the position where the lecture was cut; two sections
    in the middle of one chunk must clear the stricter test.
    """
    merged: list[NoteSection] = []
    # Paragraphs and bullets are de-duplicated *document-wide*: the same
    # sentence noted by two neighbouring chunks is the classic boundary
    # accident and must not appear twice in the booklet. The other block types
    # are only used (``seen_blocks``) to decide whether a whole section is a
    # verbatim restatement; inside a kept section they stay scoped, because the
    # same example can legitimately illustrate two different sections.
    seen_text: set[str] = set()
    seen_blocks: set[str] = set()
    for position, section in enumerate(sections):
        paragraphs = _dedupe_by_key(section.paragraphs, _compare_key)
        bullets = _dedupe_by_key(section.bullets, _compare_key)
        paragraphs = tuple(
            value for value in paragraphs if _compare_key(value) not in seen_text
        )
        bullets = tuple(value for value in bullets if _compare_key(value) not in seen_text)
        seen_text.update(_compare_key(value) for value in paragraphs)
        seen_text.update(_compare_key(value) for value in bullets)
        cleaned = replace(
            section,
            paragraphs=paragraphs,
            bullets=bullets,
            definitions=_dedupe_definitions(section.definitions),
            examples=_dedupe_by_key(section.examples, _compare_key),
            steps=_dedupe_by_key(section.steps, _compare_key),
            formulas=_dedupe_by_key(section.formulas, _compare_key),
            key_points=_dedupe_by_key(section.key_points, _compare_key),
            callouts=_dedupe_callouts(section.callouts),
        )
        if not cleaned.has_content:
            continue
        keys = _block_keys(cleaned)
        if keys and seen_blocks and all(key in seen_blocks for key in keys):
            # Nothing new: this section repeats an earlier one verbatim.
            logger.debug(
                "Merge dropped a restated section %r (%s duplicate blocks)",
                cleaned.heading,
                len(keys),
            )
            continue
        seen_blocks.update(keys)
        if merged and _same_logical_section(
            merged[-1], cleaned, across_chunk=position in chunk_starts
        ):
            merged[-1] = _combine_sections(merged[-1], cleaned)
        else:
            merged.append(cleaned)
    return tuple(merged)


def _same_logical_section(
    first: NoteSection, second: NoteSection, *, across_chunk: bool
) -> bool:
    """Do two adjacent sections belong to one logical section of the lecture?

    Conservative by construction: the stricter heading-identity test applies
    inside a chunk, and only at a chunk boundary — never repeated headings in
    general — is the looser topic-overlap test allowed. The test decides a
    *heading structure* question only; both sections' content is preserved
    either way (see :func:`_combine_sections`).
    """
    if not _section_key(first.heading) and not _section_key(second.heading):
        return False
    if headings_share_a_topic(first.heading, second.heading):
        return True
    return across_chunk and headings_overlap(first.heading, second.heading)


def _merge_summaries(notes: list[StructuredNotes], *, limit: int = 1200) -> str:
    """Join the distinct sentences of every chunk summary, in order.

    The document-level summary is the compiler's job; this is the safe
    fallback when the compiler is disabled or unavailable. Taking only the
    first chunk's summary — the previous behaviour — silently discarded what
    every later part of the lecture was about.
    """
    parts: list[str] = []
    seen: set[str] = set()
    for item in notes:
        for sentence in _sentence_spans(item.summary) if item.summary else ():
            text = item.summary[sentence[0] : sentence[1]].strip()
            if not text:
                continue
            identity = _compare_key(text)
            if identity in seen:
                continue
            seen.add(identity)
            parts.append(text)
    joined = " ".join(parts)
    return joined[:limit].rstrip()


def merge_structured_notes(notes: list[StructuredNotes]) -> StructuredNotes:
    """Combine per-chunk notes into one document, preserving order.

    Merging is additive: every *distinct* block survives in chunk order and
    nothing is re-summarised. De-duplication is deterministic and conservative —
    only blocks that are the same modulo whitespace/letter variants collapse
    (the boundary sentence two neighbouring chunks both noted, a heading a
    model repeated), and adjacent sections that clearly describe one topic are
    joined into one section so the booklet does not read as a stack of
    independently titled fragments.
    """
    if not notes:
        raise StructuringError("پاسخ سرویس تولید جزوه خالی بود.")
    if len(notes) == 1:
        return notes[0]
    # Where each chunk's sections start, so the section merger knows which
    # adjacency is a chunk boundary (see _same_logical_section).
    chunk_starts: set[int] = set()
    cursor = 0
    for item in notes[:-1]:
        cursor += len(item.sections)
        chunk_starts.add(cursor)
    merged = StructuredNotes(
        title=next((item.title for item in notes if item.title), ""),
        summary=_merge_summaries(notes),
        sections=_merge_sections(
            [section for item in notes for section in item.sections],
            chunk_starts=frozenset(chunk_starts),
        ),
        key_points=_dedupe_by_key(
            [point for item in notes for point in item.key_points], _compare_key
        ),
        glossary=_dedupe_glossary(notes),
        note_mode=next(
            (item.note_mode for item in notes if item.note_mode in NOTE_MODES), "full"
        ),
        learning_objectives=_dedupe_by_key(
            [item for note in notes for item in note.learning_objectives], _compare_key
        ),
        review_questions=_dedupe_by_key(
            [item for note in notes for item in note.review_questions], _compare_key
        ),
    )
    if not merged.has_content:
        raise StructuringError("ساختار جزوهٔ دریافتی خالی بود.")
    return merged


def _dedupe_glossary(notes: list[StructuredNotes]) -> tuple[GlossaryEntry, ...]:
    """Merge glossary entries by term, keeping the first (longest) definition."""
    by_term: dict[str, GlossaryEntry] = {}
    term_order: list[str] = []
    for item in notes:
        for entry in item.glossary:
            key = _compare_key(entry.term)
            if not key:
                continue
            if key not in by_term:
                by_term[key] = entry
                term_order.append(key)
            else:
                existing = by_term[key]
                if len(entry.definition) > len(existing.definition):
                    by_term[key] = GlossaryEntry(existing.term, entry.definition)
    return tuple(by_term[term] for term in term_order)


def extract_json_object(text: str) -> str:
    """Best-effort extraction of the JSON object from a raw model answer."""
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", candidate, re.DOTALL)
    if fence and "{" in fence.group(1):
        candidate = fence.group(1).strip()
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end <= start:
        raise StructuringError("پاسخ سرویس تولید جزوه شامل JSON نبود.")
    return candidate[start : end + 1]


def _load_json_payload(text: str) -> dict:
    candidate = extract_json_object(text)
    # Trailing commas before a closing bracket are the most common LLM slip.
    repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
    for attempt in (candidate, repaired):
        try:
            payload = json.loads(attempt)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    raise StructuringError("پاسخ سرویس تولید جزوه یک JSON معتبر نبود.")


def parse_structured_notes(text: str) -> StructuredNotes:
    """Parse a model answer into validated structured notes."""
    return StructuredNotes.from_payload(_load_json_payload(text))


# ---------------------------------------------------------------------------
# Transcript splitting
# ---------------------------------------------------------------------------


#: A chunk must be at least this full before a topic cue may end it early. Below
#: it the remaining budget is simply too large to waste: an early break there
#: would produce a short, unbalanced part.
TOPIC_BREAK_MIN_FILL = 0.6


def split_transcript(text: str, max_chars: int = 22000) -> list[str]:
    """Split a transcript into coherent, model-sized chunks.

    The transcript is first divided at paragraph breaks (blank lines) so a
    definition, procedure or worked example is never cut mid-unit. Oversized
    paragraphs are then split at sentence boundaries (Persian and Latin
    terminators), and only as a last resort at a word boundary.

    No text is ever dropped or reordered: the concatenation of the returned
    chunks equals the normalised input (token-for-token).
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    text = text.strip()
    if not text:
        return []

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if not paragraphs:
        paragraphs = [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if current:
            chunks.append("\n\n".join(current))
            current, current_len = [], 0

    for paragraph in paragraphs:
        # Oversized paragraphs are sentence-split and packed like paragraphs.
        pieces = [paragraph]
        if len(paragraph) > max_chars:
            pieces = _split_long_paragraph(paragraph, max_chars)
        for piece_index, piece in enumerate(pieces):
            # Prefer a *topic* boundary over the character budget: when the
            # chunk is already mostly full and the next paragraph opens a new
            # topic (a slide heading, a lecture transition), the chunk ends
            # here. It is exactly the same text in the same order — only the
            # cut moves a little earlier, to the nearest meaningful boundary.
            if (
                piece_index == 0
                and current
                and current_len >= max_chars * TOPIC_BREAK_MIN_FILL
                and _starts_a_topic(piece)
            ):
                flush()
            extra = len(piece) + (2 if current else 0)
            if current and current_len + extra > max_chars:
                flush()
                extra = len(piece)
            current.append(piece)
            current_len += extra
    flush()
    return chunks


#: Paragraph openings that reliably mark a new topic in the material this
#: project processes: the slide/heading markers of a presentation outline, and
#: the transitions a lecturer actually says out loud.
_TOPIC_START = re.compile(
    r"^\s*(?:#{1,6}\s+\S"                                   # `### اسلاید ۳ — …`
    r"|(?:اسلاید|slide)\s*[\d۰-۹]"                          # «اسلاید ۱۲»
    r"|(?:بخش|فصل|موضوع|مبحث|بحث|قسمت)\s*(?:بعد|جدید|دوم|سوم|چهارم|پنجم)"
    r"|(?:حالا|اکنون|خب|بسیار خوب)?\s*(?:می‌رسیم|میرسیم|می‌رویم|میرویم|برویم)\s+"
    r"(?:سراغ|به)\b"
    r"|(?:نکته|موضوع)\s*(?:بعدی|آخر|پایانی)"
    r")",
    re.IGNORECASE,
)


def _starts_a_topic(paragraph: str) -> bool:
    """Does this paragraph open a new topic (a safe early cut point)?"""
    return bool(_TOPIC_START.match(paragraph))


def _sentence_spans(paragraph: str) -> list[tuple[int, int]]:
    """Spans of consecutive sentences; terminators stay inside their sentence.

    A dot between digits ("7.2", "1.000") is a decimal/thousands separator,
    never a sentence boundary — splitting there would corrupt numeric values.
    """
    terminator = re.compile(r"(?:[!?؟…]+|؛|(?<!\d)\.(?!\d))[»\)\]”\"']*")
    spans: list[tuple[int, int]] = []
    start = 0
    for match in terminator.finditer(paragraph):
        end = match.end()
        if end <= start:
            continue  # a zero-length match can never terminate a sentence
        spans.append((start, end))
        start = end
    if start < len(paragraph):
        spans.append((start, len(paragraph)))
    return spans


def _split_long_paragraph(paragraph: str, max_chars: int) -> list[str]:
    """Cut an oversized paragraph at sentence, then word, boundaries.

    Sentence pieces are packed greedily so a piece never exceeds ``max_chars``
    and adjacent sentences stay together whenever they fit.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    pieces: list[str] = []
    for sentence_start, sentence_end in _sentence_spans(paragraph):
        sentence = paragraph[sentence_start:sentence_end].strip()
        while len(sentence) > max_chars:
            window = sentence[:max_chars]
            cut = window.rfind(" ")
            if cut < max_chars // 2:
                cut = max_chars  # a single monster word: hard cut, no loss
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            pieces.append(sentence)
    # Greedy packing: merge neighbours while the pair still fits.
    packed: list[str] = []
    for piece in pieces:
        if packed and len(packed[-1]) + len(piece) + 1 <= max_chars:
            packed[-1] = packed[-1] + " " + piece
        else:
            packed.append(piece)
    return packed or [paragraph[:max_chars]]


def build_presentation_document(outline: str, transcript: str) -> str:
    """Combine slide text and narration transcript into one prompt payload."""
    sections: list[str] = []
    if outline.strip():
        sections.append("## متن اسلایدها\n\n" + outline.strip())
    if transcript.strip():
        sections.append("## متن پیاده‌سازی‌شدهٔ صدای ارائه\n\n" + transcript.strip())
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Provider plumbing
# ---------------------------------------------------------------------------


RETRYABLE_HTTP_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}

# ---------------------------------------------------------------------------
# Anthropic request construction
# ---------------------------------------------------------------------------
# Sampling parameters are model-specific on the Anthropic Messages API:
#   * ``temperature`` and ``top_p`` must never be sent together, and ``top_k``
#     is not supported by the current generations at all, so this client only
#     ever sends ``temperature``;
#   * models from the Opus 4.7 generation onward reject *any* non-default
#     sampling value (a 400 invalid_request_error), so the parameter is
#     omitted for them and prompting carries the determinism contract instead.
# https://platform.claude.com/docs/en/about-claude/model-deprecations
_ANTHROPIC_MODEL_VERSION_PATTERN = re.compile(r"claude-([a-z]+)-(\d+)(?:-(\d+))?")
_ANTHROPIC_SAMPLING_MAX_VERSION = (4, 6)
#: Deterministic sampling for providers/models that accept it.
NOTE_TEMPERATURE = 0.2


def anthropic_supports_temperature(model: str) -> bool:
    """Whether ``model`` accepts a non-default ``temperature``.

    ``claude-haiku-4-5``/``claude-haiku-4-5-20251001`` do; ``claude-opus-4-7``
    and later do not. An unparsable name (a proxy, an alias without a version,
    or a model newer than this table) is treated as *unsupported*, because
    omitting a sampling hint only loses a tie-breaker while sending one to a
    model that rejects it fails the whole request.
    """
    match = _ANTHROPIC_MODEL_VERSION_PATTERN.match((model or "").strip().lower())
    if not match:
        return False
    version = (int(match.group(2)), int(match.group(3) or 0))
    return version <= _ANTHROPIC_SAMPLING_MAX_VERSION


def anthropic_sampling_params(model: str) -> dict[str, float]:
    """Sampling parameters for one Anthropic model (possibly none)."""
    return {"temperature": NOTE_TEMPERATURE} if anthropic_supports_temperature(model) else {}


def _endpoint(base_url: str, suffix: str) -> str:
    """Append an API path without breaking an exact endpoint's query string."""
    parsed = urlsplit(base_url)
    base_path = parsed.path.rstrip("/")
    suffix_path = "/" + suffix.strip("/")
    path = base_path if base_path.endswith(suffix_path) else base_path + suffix_path
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def _retry_after_seconds(headers) -> float | None:
    """Parse Retry-After for durable cooldowns, bounded to seven days."""
    raw = str(headers.get("Retry-After", "")).strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
        if math.isfinite(seconds):
            return min(max(seconds, 0.0), 604_800.0)
    except (TypeError, ValueError):
        pass
    try:
        retry_at = parsedate_to_datetime(raw)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
        return min(max(seconds, 0.0), 604_800.0)
    except (TypeError, ValueError, OverflowError):
        return None


def _retry_delay(response: aiohttp.ClientResponse, attempt: int) -> float:
    """Respect Retry-After while keeping request retries bounded."""
    requested = _retry_after_seconds(response.headers)
    if requested is not None:
        return min(max(requested, 0.25), 30.0)
    return min(2 ** attempt, 15.0)


def _sanitize_error_text(value: object, key: str | None) -> str:
    """Bound and redact a provider-supplied string so it is safe to log.

    Only printable text survives, lengths are capped, and the configured API
    key is blanked if a gateway ever echoes it back.
    """
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)).strip()
    if key and key in text:
        text = text.replace(key, "***")
    return text[:ERROR_DETAIL_LIMIT]


def _extract_error_detail(raw_body: bytes | None, key: str | None) -> str | None:
    """Return the provider's structured diagnosis from an error body, or None.

    Only well-known metadata fields are extracted (status/type/code, the
    provider's own message, and per-detail ``reason`` codes), because all of
    the supported APIs shape errors as ``{"error": {...}}`` (Gemini's
    google.rpc shape, OpenAI-compatible, Anthropic). Raw bodies are never
    returned: gateways may answer with HTML pages or echo request fragments,
    so anything the parser does not recognise simply stays out of the logs.
    """
    if not raw_body:
        return None
    try:
        payload = json.loads(raw_body.decode("utf-8", "replace"))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("error"), dict):
        return None
    error = payload["error"]
    parts: list[str] = []
    for field_name in ("status", "type", "code", "message"):
        value = error.get(field_name)
        if isinstance(value, str) and value.strip():
            sanitized = _sanitize_error_text(value, key)
            if sanitized not in parts:
                parts.append(sanitized)
    details = error.get("details")
    if isinstance(details, list):
        for item in details[:3]:
            if isinstance(item, dict) and isinstance(item.get("reason"), str):
                reason = _sanitize_error_text(item["reason"], key)
                if reason and reason not in parts:
                    parts.append(reason)
    return "; ".join(parts) or None


def _provider_request(
    chunk: str,
    settings: Settings,
    prompt: str,
    system_prompt: str = SYSTEM_PROMPT,
) -> tuple[str, dict[str, str], dict, dict[str, str]]:
    """Build a request for Gemini, Anthropic, or an OpenAI-compatible endpoint."""
    provider = settings.note_api_provider
    key = settings.effective_note_api_key
    model = settings.effective_note_model
    # Authentication headers are reserved: a user-supplied header may never
    # override (or forge) the credential the provider layer builds below.
    # config.sanitize_extra_headers() already drops them at parse time; this
    # second filter protects programmatically built Settings objects too.
    extra_headers = {
        name: value
        for name, value in settings.note_api_extra_headers
        if name.lower() not in RESERVED_HEADER_NAMES
    }
    if len(extra_headers) != len(settings.note_api_extra_headers):
        logger.warning("Ignored reserved authentication header(s) for provider=%s", provider)
    full_prompt = prompt + chunk

    if provider == "gemini":
        if not key:
            raise StructuringError("کلید NOTE_API_KEY یا GEMINI_API_KEY تنظیم نشده است.")
        base = settings.note_api_base_url or "https://generativelanguage.googleapis.com/v1beta"
        # Accepting the documented "models/<name>" spelling prevents the
        # .../models/models%2F... URL that Google rejects with HTTP 400.
        model_id = quote(model.strip().removeprefix("models/"), safe="")
        url = _endpoint(base, f"models/{model_id}:generateContent")
        payload = {
            "contents": [{"role": "user", "parts": [{"text": full_prompt}]}],
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "generationConfig": {
                "temperature": NOTE_TEMPERATURE,
                "maxOutputTokens": settings.note_api_max_output_tokens,
                # Native JSON mode keeps Gemini from wrapping the answer in prose.
                "responseMimeType": "application/json",
            },
        }
        headers = {"x-goog-api-key": key}
        headers.update(extra_headers)
        return url, headers, payload, {}

    if provider == "openai_compatible":
        base = settings.note_api_base_url or "https://api.openai.com/v1"
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        headers.update(extra_headers)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": full_prompt},
            ],
            "temperature": NOTE_TEMPERATURE,
            "max_tokens": settings.note_api_max_output_tokens,
        }
        if settings.note_api_json_mode:
            # Opt-in: not every OpenAI-compatible gateway implements
            # response_format, so it must never be forced on by default.
            payload["response_format"] = {"type": "json_object"}
        return _endpoint(base, "chat/completions"), headers, payload, {}

    if provider == "anthropic":
        if not key:
            raise StructuringError("برای Anthropic باید NOTE_API_KEY تنظیم شود.")
        base = settings.note_api_base_url or "https://api.anthropic.com/v1"
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        }
        headers.update(extra_headers)
        payload = {
            "model": model,
            "max_tokens": settings.note_api_max_output_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": full_prompt}],
        }
        # Only a parameter the selected model actually accepts is sent;
        # ``top_p``/``top_k`` are never sent to Anthropic at all.
        payload.update(anthropic_sampling_params(model))
        return _endpoint(base, "messages"), headers, payload, {}

    raise StructuringError("سرویس تولید جزوه غیرفعال است.")


def _provider_response(payload: dict, provider: str) -> str:
    """Normalize supported provider responses to plain text."""
    try:
        if provider == "gemini":
            candidates = payload.get("candidates")
            if not candidates:
                # Safety-blocked or empty answers return no candidates; the
                # blockReason enum makes such failures diagnosable.
                feedback = payload.get("promptFeedback")
                if isinstance(feedback, dict) and isinstance(feedback.get("blockReason"), str):
                    reason = _sanitize_error_text(feedback["blockReason"], None)
                    raise StructuringError(f"پاسخ سرویس تولید جزوه مسدود شد ({reason}).")
                raise StructuringError("پاسخ سرویس تولید جزوه خالی یا نامعتبر است.")
            if candidates[0].get("finishReason") == "MAX_TOKENS":
                raise StructuringError("خروجی جزوه به سقف توکن رسید؛ متن خام برگردانده می‌شود.")
            parts = candidates[0]["content"]["parts"]
            text = "".join(str(part.get("text", "")) for part in parts)
        elif provider == "anthropic":
            if payload.get("stop_reason") == "max_tokens":
                raise StructuringError("خروجی جزوه به سقف توکن رسید؛ متن خام برگردانده می‌شود.")
            text = "".join(
                str(part.get("text", ""))
                for part in payload["content"]
                if part.get("type") == "text"
            )
        else:
            if payload["choices"][0].get("finish_reason") == "length":
                raise StructuringError("خروجی جزوه به سقف توکن رسید؛ متن خام برگردانده می‌شود.")
            content = payload["choices"][0]["message"]["content"]
            if isinstance(content, list):
                text = "".join(
                    str(part.get("text", ""))
                    for part in content
                    if isinstance(part, dict)
                )
            else:
                text = "" if content is None else str(content)
    except (AttributeError, KeyError, IndexError, TypeError) as exc:
        # A malformed answer is a contract violation, not a "KeyError". The
        # provider name and the failing field type are enough to diagnose it
        # without logging the answer itself (it carries lecture content).
        logger.warning(
            "Note provider returned an unexpected response schema provider=%s error=%s",
            provider,
            type(exc).__name__,
        )
        raise StructuringError(
            "پاسخ سرویس تولید جزوه با قرارداد انتظار ما سازگار نبود "
            "(ساختار پاسخ غیرمنتظره بود)."
        ) from exc
    text = text.strip()
    if not text:
        raise StructuringError("پاسخ سرویس تولید جزوه خالی بود.")
    return text


async def _structure_chunk_once(
    chunk: str,
    settings: Settings,
    session: aiohttp.ClientSession,
    prompt: str = TRANSCRIPT_PROMPT,
    *,
    system_prompt: str = SYSTEM_PROMPT,
    reminder: str = "",
    retry_rate_limit: bool = True,
) -> str:
    url, headers, payload, params = _provider_request(
        chunk + reminder, settings, prompt, system_prompt
    )
    provider = settings.note_api_provider
    attempts = settings.note_api_retries + 1
    for attempt in range(attempts):
        try:
            async with session.post(
                url, headers=headers, params=params, json=payload
            ) as response:
                if (
                    (response.status != 429 or retry_rate_limit)
                    and (
                        response.status in RETRYABLE_HTTP_STATUSES
                        or 500 <= response.status < 600
                    )
                    and attempt + 1 < attempts
                ):
                    delay = _retry_delay(response, attempt)
                    await response.read()
                    logger.warning(
                        "Note API transient failure provider=%s status=%s retry=%s/%s delay=%.1fs",
                        provider,
                        response.status,
                        attempt + 1,
                        settings.note_api_retries,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                if response.status < 200 or response.status >= 300:
                    # Never log raw response bodies: gateways may echo fragments
                    # of the prompt or lecture text. Only the provider's
                    # structured error metadata is extracted and logged — enough
                    # to diagnose a Gemini HTTP 400 (e.g. API_KEY_INVALID)
                    # without exposing content or the API key.
                    body = await response.read()
                    detail = _extract_error_detail(body, settings.effective_note_api_key)
                    request_id = (
                        response.headers.get("x-request-id")
                        or response.headers.get("request-id")
                        or "unknown"
                    )
                    logger.error(
                        "Note API request failed provider=%s status=%s request_id=%s detail=%s",
                        provider,
                        response.status,
                        request_id,
                        detail or "not provided",
                    )
                    message = f"سرویس {provider} خطای HTTP {response.status} داد."
                    if detail:
                        message += f" ({detail})"
                    raise ProviderHTTPError(
                        message,
                        status=response.status,
                        retry_after_seconds=_retry_after_seconds(response.headers),
                    )
                data = await response.json(content_type=None)
                return _provider_response(data, provider)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt + 1 >= attempts:
                raise ProviderTransientError(
                    f"ارتباط با سرویس تولید جزوه ({provider}) برقرار نشد."
                ) from None
            delay = min(2 ** attempt, 15.0)
            logger.warning(
                "Note API network failure provider=%s retry=%s/%s delay=%.1fs: %s",
                provider,
                attempt + 1,
                settings.note_api_retries,
                delay,
                type(exc).__name__,
            )
            await asyncio.sleep(delay)
    raise StructuringError("سرویس تولید جزوه پاسخی برنگرداند.")


async def _structure_chunk(
    chunk: str,
    settings: Settings,
    session: aiohttp.ClientSession,
    prompt: str = TRANSCRIPT_PROMPT,
    *,
    system_prompt: str = SYSTEM_PROMPT,
    reminder: str = "",
) -> str:
    """Use the configured provider and rotate bounded credential candidates."""
    manager = current_provider_credentials()
    if manager is None:
        return await _structure_chunk_once(
            chunk, settings, session, prompt,
            system_prompt=system_prompt, reminder=reminder,
        )

    provider = settings.note_api_provider
    pool = await manager.candidates(
        "notes",
        provider,
        fallback_secret=settings.effective_note_api_key,
        fallback_base_url=settings.note_api_base_url,
        fallback_model=settings.effective_note_model,
    )
    if not pool:
        raise StructuringError("هیچ کلید فعالی برای سرویس تولید جزوه در دسترس نیست.")

    last_error: Exception | None = None
    for credential in pool:
        request_settings = (
            manager.apply_to_settings(settings, credential) if credential is not None else settings
        )
        try:
            result = await _structure_chunk_once(
                chunk, request_settings, session, prompt,
                system_prompt=system_prompt,
                reminder=reminder,
                retry_rate_limit=False,
            )
            if credential is not None:
                await manager.record_result(credential, result="success")
            return result
        except asyncio.CancelledError:
            raise
        except ProviderHTTPError as exc:
            last_error = exc
            if exc.status in {401, 403}:
                if credential is None:
                    raise
                await manager.record_result(
                    credential,
                    result="quarantined",
                    status_code=exc.status,
                    safe_error=f"HTTP {exc.status}",
                )
                continue
            if exc.status == 429:
                if credential is None:
                    raise
                await manager.record_result(
                    credential,
                    result="cooldown",
                    status_code=exc.status,
                    retry_after_seconds=exc.retry_after_seconds,
                    safe_error="HTTP 429",
                )
                continue
            if exc.status in RETRYABLE_HTTP_STATUSES or 500 <= exc.status < 600:
                if credential is not None:
                    await manager.record_result(
                        credential,
                        result="error",
                        status_code=exc.status,
                        safe_error=f"HTTP {exc.status}",
                    )
                    continue
                raise
            if credential is not None:
                await manager.record_result(
                    credential,
                    result="invalid_request",
                    status_code=exc.status,
                    safe_error=f"HTTP {exc.status}",
                )
            # Request errors (400/415/422, etc.) are not fixed by rotation.
            raise
        except ProviderTransientError as exc:
            last_error = exc
            if credential is not None:
                await manager.record_result(
                    credential,
                    result="error",
                    safe_error="transient network failure",
                )
                continue
            raise
    if last_error is not None:
        raise last_error
    raise StructuringError("هیچ کلید فعالی برای سرویس تولید جزوه در دسترس نیست.")


async def _structured_notes_for(
    document: str,
    settings: Settings,
    session: aiohttp.ClientSession,
    prompt: str,
    *,
    system_prompt: str = SYSTEM_PROMPT,
    label: str = "chunk",
    reminder: str = "",
) -> StructuredNotes:
    """One LLM answer parsed as strict JSON, with a single bounded repair pass."""
    started = asyncio.get_running_loop().time()
    try:
        raw = await _structure_chunk(
            document, settings, session, prompt, system_prompt=system_prompt,
            reminder=reminder,
        )
    except Exception as exc:
        if isinstance(exc, StructuringError):
            logger.warning(
                "Note structuring failed %s elapsed_seconds=%.1f error=%s",
                label,
                asyncio.get_running_loop().time() - started,
                exc,
            )
        else:
            logger.exception("Note structuring failed unexpectedly %s", label)
        raise
    try:
        notes = parse_structured_notes(raw)
    except StructuringError as exc:
        logger.warning(
            "Note API answer failed JSON validation (%s); requesting one repair pass", exc
        )
        raw = await _structure_chunk(
            document,
            settings,
            session,
            prompt,
            system_prompt=system_prompt,
            reminder=JSON_REMINDER,
        )
        try:
            notes = parse_structured_notes(raw)
        except StructuringError as exc:
            logger.error("Note API answer was not valid JSON even after the repair pass")
            raise StructuringError(
                "پاسخ سرویس تولید جزوه پس از تلاش مجدد همچنان JSON معتبر نبود."
            ) from exc
    logger.info(
        "Note structuring completed %s elapsed_seconds=%.1f sections=%s key_points=%s "
        "glossary=%s title_chars=%s",
        label,
        asyncio.get_running_loop().time() - started,
        len(notes.sections),
        len(notes.key_points),
        len(notes.glossary),
        len(notes.title),
    )
    return notes


def _chunk_prefix(index: int, total: int) -> str:
    """Positional context prepended to every chunk sent to the model.

    Unlike an overlapping-text window, this carries only position metadata, so
    it can never cause the same sentence to be noted twice; it tells the model
    the chunk is a middle (or final) part of one continuing lecture.
    """
    if total <= 1:
        return ""
    if index == 1:
        return (
            f"[بخش {to_persian_digits(index)} از {to_persian_digits(total)} این درس — "
            "ادامهٔ درس در بخش بعدی می‌آید]\n\n"
        )
    if index == total:
        return (
            f"[بخش {to_persian_digits(index)} از {to_persian_digits(total)} این درس — "
            "این آخرین بخش درس است]\n\n"
        )
    return (
        f"[بخش {to_persian_digits(index)} از {to_persian_digits(total)} این درس — "
        "این بخش ادامهٔ بخش قبل است و ادامهٔ آن در بخش بعد می‌آید]\n\n"
    )


#: Worst-case length of the positional chunk prefix. Chunk budgets are
#: reduced by this reserve so the *complete* model input (prefix + material)
#: stays within the intended context budget.
_CHUNK_PREFIX_RESERVE = 200

#: Default transcript chunk budget fed to the note model.
TRANSCRIPT_CHUNK_CHARS = 22000

#: Upper bound on how many missing educational units are named in one repair
#: prompt, so the QA framing stays small relative to the source text.
MAX_UNITS_IN_REPAIR_PROMPT = 8

#: Global-context tuning. Both passes exist only for multi-part lectures, so a
#: short lecture keeps the historical "one provider call" behaviour.
OUTLINE_MIN_CHUNKS = 2
#: A merge below this size is not worth an editorial call.
COMPILE_MIN_NOTES_CHARS = 400
#: Hard bound on the final compilation request. A booklet above it is compiled in
#: consecutive slices rather than re-sent as one oversized request.
COMPILE_MAX_CHARS = 48000
#: Upper bound on how many slices the compilation may cost. Beyond it the merged
#: notes are delivered unchanged instead of turning a booklet into an unbounded
#: chain of provider calls.
COMPILE_MAX_SLICES = 6
#: Characters reserved per compilation request for the outline and instruction
#: blocks (the system prompt length is measured exactly).
COMPILE_PROMPT_OVERHEAD = 2500
#: User-message wrapper for the orientation call.
OUTLINE_PROMPT = "آغاز بخش‌های پیاپی این درس:\n\n"
#: User-message wrapper for the final editorial call.
COMPILE_PROMPT = "زمینهٔ درس و جزوهٔ فعلی:\n\n"


async def _lecture_context(
    documents: list[str],
    settings: Settings,
    session: aiohttp.ClientSession,
    *,
    budget: int,
    label: str,
):
    """One cheap orientation call for a multi-part lecture.

    The whole lecture is represented by the *beginning* of every part, so the
    call is small and bounded. Any failure is non-fatal: the pipeline continues
    without global context, exactly as it did before this layer existed.
    """
    from .editorial import (
        OUTLINE_CHUNK_HEAD_CHARS,
        OUTLINE_DOCUMENT_MAX_CHARS,
        OUTLINE_SYSTEM_PROMPT,
        build_outline_document,
        parse_outline,
    )

    if len(documents) < OUTLINE_MIN_CHUNKS or not settings.note_global_context_enabled:
        return None
    digest_budget = min(OUTLINE_DOCUMENT_MAX_CHARS, max(600, budget - 400))
    document = build_outline_document(documents, max_chars=digest_budget)
    if not document.strip():
        return None
    try:
        raw = await _structure_chunk(
            document, settings, session, OUTLINE_PROMPT, system_prompt=OUTLINE_SYSTEM_PROMPT
        )
        context = parse_outline(raw)
    except Exception as exc:
        logger.warning(
            "Lecture outline pass failed (%s: %s); continuing without global context",
            type(exc).__name__,
            exc if isinstance(exc, StructuringError) else "unexpected error",
        )
        return None
    if context is None:
        logger.info("Lecture outline pass returned nothing usable; continuing without it")
        return None
    logger.info(
        "Lecture outline ready %s parts=%s topics=%s terminology=%s window=%s",
        label,
        len(documents),
        len(context.topics),
        len(context.terminology),
        OUTLINE_CHUNK_HEAD_CHARS,
    )
    return context


#: How many already-written headings of the previous part are shown to the
#: next part. One is usually enough (the last section); three tolerates a model
#: that ended its part with a short closing section.
MAX_PREVIOUS_HEADINGS = 3


def _previous_headings(notes: list[StructuredNotes]) -> tuple[str, ...]:
    """The tail headings of the part that was just written, if any."""
    if not notes:
        return ()
    headings = [section.heading.strip() for section in notes[-1].sections if section.heading.strip()]
    return tuple(headings[-MAX_PREVIOUS_HEADINGS:])


def _context_block_for(
    context, documents: list[str], index: int, previous_headings: tuple[str, ...] = ()
) -> str:
    """The global-context block for part ``index`` of ``documents``.

    A part with no outline still gets a context block when the previous part's
    headings are known: position, neighbour sentences and the heading wording to
    reuse are useful even without the orientation pass.
    """
    if context is None and not previous_headings:
        return ""
    from .editorial import LectureContext, context_block_for

    return context_block_for(
        context if context is not None else LectureContext(),
        documents,
        index,
        previous_headings=previous_headings,
    )


async def _compile_final(
    merged: StructuredNotes,
    context,
    documents: list[str],
    settings: Settings,
    session: aiohttp.ClientSession,
    *,
    note_mode: str,
    label: str,
    budget: int | None = None,
) -> StructuredNotes:
    """The controlled editorial pass over the merged notes.

    Its job is global coherence — merge logically related fragments, remove
    accidental duplication, repair transitions, unify terminology — never
    summarisation. Every slice is accepted only when the deterministic QA
    measures do not regress and the slice keeps almost all of its text, so a
    compilation can improve the reading experience but can never quietly drop
    content. On any provider error the merged notes for that slice are kept.

    Its input is the whole booklet, so its budget is the global compilation
    bound rather than one chunk's budget. A booklet larger than that bound is
    compiled in consecutive slices (each one labelled as a part, so no slice
    writes a whole-lecture summary from a fragment) instead of being skipped —
    a long lecture is exactly the document that needs this pass most. The number
    of slices is bounded so a pathological booklet cannot turn into a long chain
    of provider calls; beyond that bound the merged notes are delivered.
    """
    from .editorial import (
        COMPILE_SYSTEM_PROMPT,
        LectureContext,
        build_compile_document,
        compile_is_better,
        split_for_compilation,
    )

    if not settings.note_global_context_enabled or len(documents) < OUTLINE_MIN_CHUNKS:
        return merged
    before_text = notes_text(merged)
    if len(before_text) < COMPILE_MIN_NOTES_CHARS:
        return merged
    # Resolved at call time (not as a default argument) so the bound is one
    # place and a test or a future setting can move it.
    budget = COMPILE_MAX_CHARS if budget is None else budget
    budget = min(COMPILE_MAX_CHARS, max(budget, COMPILE_MIN_NOTES_CHARS))
    # The system prompt and the instruction block are sent once per slice, so
    # they are reserved up front and never charged to the notes.
    groups = split_for_compilation(
        merged, budget=budget, overhead=len(COMPILE_SYSTEM_PROMPT) + COMPILE_PROMPT_OVERHEAD
    )
    if len(groups) > COMPILE_MAX_SLICES:
        logger.info(
            "Final editorial pass skipped %s: %s characters of notes need %s slices, "
            "more than the %s allowed",
            label,
            len(before_text),
            len(groups),
            COMPILE_MAX_SLICES,
        )
        return merged

    kept: list[StructuredNotes] = []
    accepted_any = False
    for position, group in enumerate(groups, start=1):
        # A missing outline only removes the topic map; the compilation itself
        # is still worth doing, because it is the pass that produces one
        # coherent document out of the per-chunk drafts.
        document = build_compile_document(
            context or LectureContext(), group, part=position, parts=len(groups)
        )
        slice_label = (
            f"{label} final compilation"
            if len(groups) == 1
            else f"{label} final compilation {position}/{len(groups)}"
        )
        if len(document) > budget:
            kept.append(group)
            continue
        before_slice = run_note_qa(group, documents)
        try:
            compiled = await _structured_notes_for(
                document,
                settings,
                session,
                COMPILE_PROMPT,
                system_prompt=COMPILE_SYSTEM_PROMPT,
                label=slice_label,
            )
        except Exception as exc:
            logger.warning(
                "Final editorial pass failed for %s (%s: %s); keeping its merged notes",
                slice_label,
                type(exc).__name__,
                exc if isinstance(exc, StructuringError) else "unexpected error",
            )
            kept.append(group)
            continue
        after_text = notes_text(compiled)
        after_slice = run_note_qa(compiled, documents)
        accepted, reason = compile_is_better(
            before_slice,
            after_slice,
            before_chars=len(notes_text(group)),
            after_chars=len(after_text),
        )
        if not accepted:
            logger.warning(
                "Final editorial pass rejected %s (%s) coverage=%.2f->%.2f "
                "semantic=%.2f->%.2f; keeping its merged notes",
                slice_label,
                reason,
                before_slice.coverage,
                after_slice.coverage,
                before_slice.semantic_coverage,
                after_slice.semantic_coverage,
            )
            kept.append(group)
            continue
        accepted_any = True
        logger.info(
            "Final editorial pass accepted %s (%s) sections=%s->%s chars=%s->%s",
            slice_label,
            reason,
            len(group.sections),
            len(compiled.sections),
            len(notes_text(group)),
            len(after_text),
        )
        kept.append(compiled)

    if not accepted_any:
        return merged
    if len(kept) == 1:
        result = kept[0]
    else:
        # Additive, order-preserving merge of the compiled slices: the same
        # conservative merge the pipeline already trusts, so two slices cannot
        # print one topic twice or reorder the lecture.
        result = merge_structured_notes(kept)
    return replace(result, title=result.title or merged.title, note_mode=note_mode)


async def structure_transcript(
    text: str, settings: Settings, mode: str = "full"
) -> StructuredNotes:
    """Turn a transcript into structured notes with the configured provider.

    Pipeline: chunk losslessly -> (multi-part only) one orientation call for the
    whole lecture -> one strict-JSON call per part, each carrying the global
    context and its neighbours -> additive merge -> optional source-grounded
    repair when QA sees real loss -> one editorial compilation pass, accepted
    only when no measured content is lost.
    """
    if not text.strip():
        raise StructuringError("متن پیاده‌سازی‌شده خالی است.")
    if settings.note_api_provider == "disabled":
        raise StructuringError("سرویس تولید جزوه غیرفعال است.")
    note_mode = resolve_note_mode(mode)
    chunks = split_transcript(text, max_chars=TRANSCRIPT_CHUNK_CHARS - _CHUNK_PREFIX_RESERVE)
    timeout = aiohttp.ClientTimeout(
        total=settings.note_api_timeout,
        connect=min(30, settings.note_api_timeout),
        sock_read=settings.note_api_timeout,
    )
    notes: list[StructuredNotes] = []
    async with aiohttp.ClientSession(timeout=timeout) as session:
        context = await _lecture_context(
            chunks, settings, session, budget=TRANSCRIPT_CHUNK_CHARS, label="transcript"
        )
        for index, chunk in enumerate(chunks, start=1):
            notes.append(
                await _structured_notes_for(
                    _chunk_prefix(index, len(chunks)) + chunk,
                    settings,
                    session,
                    TRANSCRIPT_PROMPT,
                    system_prompt=build_system_prompt(
                        note_mode,
                        context_block=_context_block_for(
                            context, chunks, index, _previous_headings(notes)
                        ),
                    ),
                    label=f"chunk {index}/{len(chunks)}",
                )
            )
        # The optional repair pass reuses this session, so it must run while
        # the session is still open.
        merged = await _repair_notes_if_needed(
            merge_structured_notes(notes),
            chunks,
            settings,
            session,
            note_mode=note_mode,
            system_prompt=build_system_prompt(note_mode),
            prompt=TRANSCRIPT_PROMPT,
            label="transcript",
            system_prompt_for=lambda index, total: build_system_prompt(
                note_mode, context_block=_context_block_for(context, chunks, index)
            ),
            originals=notes,
        )
        return await _compile_final(
            merged,
            context,
            chunks,
            settings,
            session,
            note_mode=note_mode,
            label="transcript",
        )


def _repair_is_better(before, after) -> tuple[bool, str]:
    """Did the repaired notes measurably improve on the original?

    Returns ``(accepted, reason)``. The comparison is lexicographic over the
    measures that reflect information loss, and every branch also requires the
    other measures not to regress, so a repair cannot trade a real gain in
    semantic coverage for a real loss of numbers.
    """
    if after.semantic_coverage > before.semantic_coverage:
        # ``missing_numbers`` is a tuple of *values*, so it must be compared by
        # length: comparing the tuples compares the strings element-wise, which
        # rejected correct repairs whose new missing values happened to sort
        # higher than the old ones.
        if after.coverage < before.coverage or len(after.missing_numbers) > len(
            before.missing_numbers
        ):
            return False, "semantic coverage rose but signal coverage regressed"
        return True, "semantic coverage restored"
    if after.semantic_coverage < before.semantic_coverage:
        return False, "semantic coverage regressed"
    if after.coverage > before.coverage:
        return True, "signal coverage restored"
    if after.coverage < before.coverage:
        return False, "signal coverage regressed"
    if after.compression_ratio > before.compression_ratio:
        return True, "more of the lecture preserved at equal coverage"
    return False, "no measurable improvement"


def _chunk_quality(draft: StructuredNotes, source: str) -> tuple[float, float, int]:
    """``(semantic coverage, signal coverage, missing numbers)`` for one part.

    Used to accept a *targeted* repair part by part: the repair only ever
    replaces the draft it improves.
    """
    from .units import extract_all_units, semantic_coverage

    report = run_note_qa(draft, [source])
    semantic, _ = semantic_coverage(extract_all_units([source]), notes_text(draft))
    return semantic, report.coverage, len(report.missing_numbers)


def _choose_repaired_draft(
    original: StructuredNotes, candidate: StructuredNotes, source: str
) -> StructuredNotes:
    """Keep a repaired part only when it measurably improves on its own draft.

    A repair pass regenerates parts that were fine in order to restore the ones
    that were not. Accepting the whole answer because it improved *on average*
    is how a targeted repair silently becomes a rewrite, so each part is judged
    against its own draft with the same ordering the document-level gate uses:
    restored educational content first, then numbers/terms, and ties keep the
    draft that already existed.
    """
    if not candidate.has_content:
        return original
    before_semantic, before_coverage, before_missing = _chunk_quality(original, source)
    after_semantic, after_coverage, after_missing = _chunk_quality(candidate, source)
    if after_semantic > before_semantic:
        if after_coverage < before_coverage or after_missing > before_missing:
            return original
        return candidate
    if after_semantic < before_semantic:
        return original
    if after_coverage > before_coverage and after_missing <= before_missing:
        return candidate
    return original


async def _repair_notes_if_needed(
    merged: StructuredNotes,
    source_chunks: list[str],
    settings: Settings,
    session: aiohttp.ClientSession,
    *,
    note_mode: str,
    system_prompt: str,
    prompt: str,
    label: str,
    max_chars: int = TRANSCRIPT_CHUNK_CHARS,
    system_prompt_for=None,
    originals: list[StructuredNotes] | None = None,
) -> StructuredNotes:
    """Optional second pass, fired only when deterministic QA says it is needed.

    The normal path is exactly one provider call per chunk. This runs solely
    when :attr:`NoteQAReport.needs_repair` is true (missing numbers, low
    coverage, or compression far below a compiled lecture) and it is
    configurable via ``NOTE_REPAIR_ENABLED``.

    Safety rules, because a repair that invents content is worse than no repair:

    * the repaired notes are only accepted when their QA is *not worse* than the
      original's — measured by semantic coverage first, then signal coverage,
      then how much of the lecture was preserved;
    * the prompt is the same source text plus the existing notes and the missing
      signals/units, with an explicit instruction to restore rather than to
      lengthen, and never to invent anything;
    * on any provider error the original notes are returned unchanged;
    * the repair reuses the first pass's documents, so it can never turn a long
      lecture into one oversized request;
    * ``system_prompt_for`` lets the repair carry the same global lecture
      context the first pass had, so a restored section keeps its terminology
      and does not reintroduce the whole lecture.
    """
    report = run_note_qa(merged, source_chunks)
    if not settings.note_repair_enabled or not report.needs_repair:
        return merged

    missing = tuple(report.missing_numbers) + tuple(report.missing_terms)
    # The educational units that did not survive. Their *text* is what the model
    # must restore, so the prompt can point at a deleted explanation instead of
    # only at a missing number.
    from .units import extract_all_units, semantic_coverage

    _, missing_units = semantic_coverage(extract_all_units(source_chunks), notes_text(merged))
    unit_types = sorted({unit.type for unit in missing_units})
    logger.warning(
        "Note QA triggered the repair pass %s coverage=%.2f semantic=%.2f ratio=%.3f "
        "missing_signals=%s missing_units=%s",
        label,
        report.coverage,
        report.semantic_coverage,
        report.compression_ratio,
        ",".join(missing[:6]) or "-",
        ",".join(unit_types) or "-",
    )
    try:
        # The repair re-reads the *same documents* the first pass used, with the
        # same boundaries. Re-joining and re-splitting them would be actively
        # harmful: a presentation document carries the slide outline as
        # context, and splitting it can send a repair request with no slide
        # text at all. The only added cost is the bounded repair framing.
        framing = len(
            build_repair_prompt(
                "", missing, report.findings, notes_text(merged), ()
            )
        ) + len(REPAIR_REMINDER)
        if any(len(document) + framing > max_chars for document in source_chunks):
            logger.info(
                "Note repair skipped %s: documents plus repair framing exceed the "
                "request budget of %s characters",
                label,
                max_chars,
            )
            return merged
        sources = list(source_chunks)
        # The units relevant to one document only, so each repair request points
        # at what is missing from *that* part of the lecture.
        all_units = extract_all_units(source_chunks)
        _, all_missing = semantic_coverage(all_units, notes_text(merged))
        by_chunk: dict[int, list[str]] = {}
        for unit in all_missing:
            by_chunk.setdefault(unit.source_chunk, []).append(unit.text[:200])
        repaired_notes = []
        for index, source in enumerate(sources, start=1):
            chunk_prompt = (
                system_prompt
                if system_prompt_for is None
                else system_prompt_for(index, len(sources))
            )
            candidate = await _structured_notes_for(
                build_repair_prompt(
                    source,
                    missing,
                    report.findings,
                    existing=notes_text(merged),
                    missing_units=tuple(
                        by_chunk.get(index, [])[:MAX_UNITS_IN_REPAIR_PROMPT]
                    ),
                ),
                settings,
                session,
                prompt,
                system_prompt=chunk_prompt,
                label=f"{label} (repair {index}/{len(sources)})",
                reminder=REPAIR_REMINDER,
            )
            # Targeted repair: a part that was already right is kept as it was.
            if originals is not None and index <= len(originals):
                candidate = _choose_repaired_draft(originals[index - 1], candidate, source)
            repaired_notes.append(candidate)
        repaired = merge_structured_notes(repaired_notes)
    except Exception:
        logger.exception("Note repair pass failed; keeping the original notes")
        return merged

    repaired_report = run_note_qa(repaired, source_chunks)
    # Quality is compared in the order that reflects what actually matters:
    # did the repair restore educational *content*? Semantic coverage leads,
    # then signal coverage, and only when both tie do we prefer the version
    # that preserved more of the lecture. A repair that improves one measure
    # while regressing another is rejected, so a blind rewrite can never be
    # accepted just because it got longer.
    improved, reason = _repair_is_better(report, repaired_report)
    if not improved:
        logger.warning(
            "Note repair pass rejected (%s); keeping the original "
            "semantic=%.2f->%.2f coverage=%.2f->%.2f",
            reason,
            report.semantic_coverage,
            repaired_report.semantic_coverage,
            report.coverage,
            repaired_report.coverage,
        )
        return merged
    logger.info(
        "Note repair pass improved (%s) semantic %.2f -> %.2f coverage %.2f -> %.2f "
        "ratio %.3f -> %.3f",
        reason,
        report.semantic_coverage,
        repaired_report.semantic_coverage,
        report.coverage,
        repaired_report.coverage,
        report.compression_ratio,
        repaired_report.compression_ratio,
    )
    # Keep the original title/mode: the repair regenerates structure, not identity.
    return replace(repaired, title=repaired.title or merged.title, note_mode=note_mode)


async def structure_presentation(
    outline: str,
    transcript: str,
    settings: Settings,
    max_chars: int = 22000,
    mode: str = "full",
) -> StructuredNotes:
    """Build a slide-ordered booklet from slide text plus narration transcript."""
    if not outline.strip() and not transcript.strip():
        raise StructuringError("محتوای قابل‌استفاده‌ای از فایل ارائه به دست نیامد.")
    if settings.note_api_provider == "disabled":
        raise StructuringError("سرویس تولید جزوه غیرفعال است.")
    note_mode = resolve_note_mode(mode)
    system_prompt = build_presentation_system_prompt(note_mode)

    if max_chars < 1000:
        raise ValueError("max_chars must be at least 1000 for presentation context")
    outline = outline.strip()
    if transcript.strip() and len(outline) <= max_chars // 2:
        # Repeat a short outline as context for each narration chunk.
        transcript_budget = (
            max_chars - len(build_presentation_document(outline, "")) - 100 - _CHUNK_PREFIX_RESERVE
        )
        documents = [
            build_presentation_document(outline, chunk)
            for chunk in split_transcript(transcript, transcript_budget)
        ]
    else:
        # Long decks (including slides-only decks) must not silently lose their
        # final slides. Chunk the complete material instead of truncating it.
        documents = split_transcript(
            build_presentation_document(outline, transcript),
            max_chars=max_chars - _CHUNK_PREFIX_RESERVE,
        )

    timeout = aiohttp.ClientTimeout(
        total=settings.note_api_timeout,
        connect=min(30, settings.note_api_timeout),
        sock_read=settings.note_api_timeout,
    )
    notes: list[StructuredNotes] = []
    async with aiohttp.ClientSession(timeout=timeout) as session:
        context = await _lecture_context(
            documents, settings, session, budget=max_chars, label="presentation"
        )
        for index, document in enumerate(documents, start=1):
            notes.append(
                await _structured_notes_for(
                    _chunk_prefix(index, len(documents)) + document,
                    settings,
                    session,
                    PRESENTATION_PROMPT,
                    system_prompt=build_presentation_system_prompt(
                        note_mode,
                        context_block=_context_block_for(
                            context, documents, index, _previous_headings(notes)
                        ),
                    ),
                    label=f"presentation chunk {index}/{len(documents)}",
                )
            )
        merged = await _repair_notes_if_needed(
            merge_structured_notes(notes),
            documents,
            settings,
            session,
            note_mode=note_mode,
            system_prompt=system_prompt,
            prompt=PRESENTATION_PROMPT,
            label="presentation",
            max_chars=max_chars,
            system_prompt_for=lambda index, total: build_presentation_system_prompt(
                note_mode, context_block=_context_block_for(context, documents, index)
            ),
            originals=notes,
        )
        return await _compile_final(
            merged,
            context,
            documents,
            settings,
            session,
            note_mode=note_mode,
            label="presentation",
        )
