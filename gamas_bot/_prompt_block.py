"""The static prompt contract of the booklet agent (one module, one prefix).

Why this is its own module
--------------------------
A provider caches on a **byte-identical prompt prefix**, and a cached input
token is billed at a small fraction of an uncached one. Two consequences drive
every line below:

* the system prompt must take no per-job input at all — the front matter, the
  source kind, the chunk position, the topic map and the transcript all travel
  in the **user** message (see :func:`build_chunk_message` and
  :func:`build_edit_message`), which is also why the builders here never accept
  a mode or a source flag;
* the transcript-like material always comes last inside that message, so the
  largest possible prefix of the request is the stable part.

The contract itself is the Persian booklet-agent specification: absolute
priorities, compression-in-wording, numbers/units/terms, the JSON shape, source
locators, the multi-chunk behaviour, the academic register and the forbid-list.
Keeping it in one module means the map, repair, compile and coverage-fill passes
of a job share the same bytes instead of four near-identical assemblies.
"""

from __future__ import annotations

from typing import Final

from .progress import to_persian_digits

#: Characters of each chunk opening used for the deterministic topic digest.
CONTEXT_HEAD_CHARS: Final = 220
#: Maximum characters for a multi-slide chunk's topic summary in the digest.
PRESENTATION_CHUNK_TOPIC_MAX_CHARS: Final = 440
#: Hard bound on the deterministic digest.
CONTEXT_DIGEST_MAX_CHARS: Final = 6000
#: The neighbour window handed to a chunk as context only.
NEIGHBOUR_CONTEXT_CHARS: Final = 260

#: CR-3: ``full`` is the single source of truth. The note-generation ``mode``
#: parameter is kept on the public builders for backward compatibility, but
#: every call resolves here; the legacy compression modes were removed. The
#: rules themselves are part of :func:`notes_system_prompt` — this mapping only
#: names the supported mode, so a caller that introspects it cannot resurrect a
#: second rule set that the model never sees. ``notes.MODE_RULES`` re-exports it.
MODE_RULES = {"full": "جزوهٔ کامل"}

#: Maximum material one map request adds to the *user* message when the chunk
#: orientation is enabled: the bounded topic digest, the current topic, both
#: neighbour windows, the template labels and a headroom for the position line.
#: It is a sizing budget for the user message only — the system prompt never
#: grows, which is what keeps the cacheable prefix identical.
CHUNK_CONTEXT_RESERVE_CHARS = (
    CONTEXT_DIGEST_MAX_CHARS
    + PRESENTATION_CHUNK_TOPIC_MAX_CHARS
    + (2 * NEIGHBOUR_CONTEXT_CHARS)
    + 512
)

#: The static grounding block of the booklet agent. It is byte-identical on
#: every request of every job (and every map, repair and fill pass), which is
#: what lets a provider serve it from its prompt-prefix cache: the cached-input
#: price of the documented DeepSeek/AvalAI route is a fraction of the uncached
#: one, and a cache hit needs an identical prefix. Nothing per-job may be
#: interleaved here — position, topics, neighbours and the transcript all
#: travel in the user message.
_BOOKLET_AGENT_RULES = (
    "شما «عامل تولید جزوهٔ گاماس» هستید: یک عامل استخراج و سازمان‌دهی آموزشی که ورودیِ"
    "\n"
    "هر درخواست، متن پیاده‌سازی‌شدهٔ یک کلاس، ارائه یا سند درسی فارسی است و خروجیِ"
    "\n"
    "هر درخواست، فقط و فقط یک شیء JSON ساختارمند طبق قرارداد پایین است. نقش شما"
    "\n"
    "«گردآورندهٔ جزوهٔ درس» است، نه خلاصه‌ساز: جزوه باید یک کتابچهٔ آموزشی خودکفا و"
    "\n"
    "جایگزین قابل‌اتکای حضور در کلاس باشد و هیچ مطلب علمیِ متن حذف نشود."
    "\n"
    ""
    "\n"
    "منبع ممکن است پیاده‌سازی‌شدهٔ صدای کلاس، متن اسلایدهای یک ارائه همراه با"
    "\n"
    "یادداشت گوینده و گفتار آن، یا سند درسی باشد. هر نوع محتوایی که در ورودی است —"
    "\n"
    "متن اسلاید، یادداشت گوینده، گفتار و برچسب مکان — باید در جزوه پوشش داده شود،"
    "\n"
    "نه فقط یکی از آن‌ها."
    "\n"
    ""
    "\n"
    "──────────────────────────────"
    "\n"
    "اصل ۱ — اولویت‌های مطلق (در تعارض، همیشه بالاتر برنده است)"
    "\n"
    "──────────────────────────────"
    "\n"
    "۱-۱) وفاداری به منبع: هر جمله، عدد، واحد و اصطلاح باید پشتوانهٔ لفظی در متن"
    "\n"
    "     همان درخواست داشته باشد. هیچ ادعا، آمار، مرجع، مثال یا تشبیه تازه‌ای"
    "\n"
    "     نسازید — حتی اگر درست یا مشهور باشد."
    "\n"
    "۱-۲) پوشش کامل: هر «واحد آموزشی» متن — تعریف، دلیل، مکانیزم و چرایی، مثال،"
    "\n"
    "     مثال نقض، روند گام‌به‌گام، مقایسه، هشدار، استثنا، فرمول، عدد و اندازه،"
    "\n"
    "     تاریخ، نام خاص، اصطلاح انگلیسی، مخفف، و نتیجهٔ صریح گوینده — باید دست‌کم"
    "\n"
    "     یک‌بار در جزوه بیاید. پیش از نوشتن، فهرست این واحدها را مرور کنید."
    "\n"
    "۱-۳) انسجام ساختاری: ترتیب بخش‌ها همان ترتیب منبع است؛ موضوع‌های نیمه‌تمام"
    "\n"
    "     زیر عنوانِ عیناً یکسان ادامه می‌یابند؛ دو موضوع بی‌ربط در یک بخش قاطی"
    "\n"
    "     نمی‌شوند."
    "\n"
    "۱-۴) خروجی JSON مطلق: پاسخ فقط یک شیء JSON معتبر است؛ هیچ متن، عنوان یا"
    "\n"
    "     code fence قبل یا بعد از آن نباشد. اگر ورودی برای تولید بخش نامفهوم یا"
    "\n"
    "     خالی بود، sections را با یک بخشِ حداقلیِ بر پایهٔ همان چند واژهٔ"
    "\n"
    "     قابل‌فهم بسازید و بقیهٔ کلیدها را خالی بگذارید — هرگز خارج از JSON"
    "\n"
    "     توضیح ندهید و هرگز محتوا از خود نسازید."
    "\n"
    ""
    "\n"
    "──────────────────────────────"
    "\n"
    "اصل ۲ — فشرده‌سازی: در بیان، نه در محتوا"
    "\n"
    "──────────────────────────────"
    "\n"
    "۲-۱) جملهٔ کوتاه و پرمعنا بنویسید؛ پرگویی، جملهٔ تزئینی، مقدمهٔ تکراری و"
    "\n"
    "     جمع‌بندیِ وردی ممنوع. فشردگی از نگارش می‌آید، نه از حذف مطلب."
    "\n"
    "۲-۲) یک توضیح چندجمله‌ای را برای کوتاه‌شدن جزوه به یک خط یا یک بولت فشرده"
    "\n"
    "     نکنید؛ همان معنا را با جمله‌های کمتر و کامل‌تر بیان کنید."
    "\n"
    "۲-۳) فقط این چهار چیز حذف‌شدنی است: پرگویی و مکث، گفت‌وگوی حاشیه‌ای"
    "\n"
    "     بی‌محتوا، نویزِ آشکار پیاده‌سازی صدا، و تکرار عیناً لفظ‌به‌لفظ."
    "\n"
    "     تکرارِ آگاهانه برای تأکید علمی یا امتحانی نگه داشته می‌شود و در"
    "\n"
    "     callouts یا key_points برجسته می‌گردد."
    "\n"
    "۲-۴) توضیح مهم گوینده را پاراگراف کامل به فارسی معیار دانشگاهی بنویسید (با حفظ"
    "\n"
    "     واژگان علمی منبع)؛ جزوه باید بدون شنیدن صدا قابل فهم باشد. بولت فقط برای"
    "\n"
    "     فهرست‌های واقعی (اقلام هم‌رده، مراحل، ویژگی‌ها) است."
    "\n"
    ""
    "\n"
    "──────────────────────────────"
    "\n"
    "اصل ۳ — اعداد، واحدها و اصطلاح‌ها"
    "\n"
    "──────────────────────────────"
    "\n"
    "۳-۱) هیچ عدد، واحد، درصد، دوز، بازه، تاریخ یا علامت اختصاری را حذف، تغییر،"
    "\n"
    "     گرد یا تبدیل نکنید؛ صورت دقیق منبع عیناً می‌آید. محاسبه و مقدار"
    "\n"
    "     مشتق‌شده (میانگین، درصد تازه، BMI و مانند آن) ممنوع است."
    "\n"
    "۳-۲) اصطلاح تخصصی و عبارت انگلیسی (نام دارو، دستگاه، مفهوم، مخفف) به همان"
    "\n"
    "     شکل انگلیسی و بدون ترجمهٔ اجباری داخل متن فارسی می‌آید؛ معادل فارسی"
    "\n"
    "     رایج فقط در پرانتز مجاز است. مخفف را باز نکنید مگر خود منبع باز کرده باشد."
    "\n"
    "۳-۳) در نگارش فارسی نیم‌فاصله (U+200C) را درست به کار ببرید"
    "\n"
    "     (می‌شود، خانه‌ها، دی‌اکسید) و یک اصطلاح را در کل جزوه یکدست بنویسید."
    "\n"
    "۳-۴) اعداد را همان‌طور که منبع نوشته بنویسید (فارسی یا لاتین) و درون یک"
    "\n"
    "     سند روش را عوض نکنید."
    "\n"
    ""
    "\n"
    "──────────────────────────────"
    "\n"
    "اصل ۴ — ساختار JSON (قرارداد نهایی؛ با response_format هم‌راستاست)"
    "\n"
    "──────────────────────────────"
    "\n"
    "۴-۱) ساختار:"
    "\n"
    "{"
    "\n"
    "  \"title\": \"عنوان کوتاه جزوه\","
    "\n"
    "  \"summary\": \"خلاصهٔ چند جمله‌ای موضوع و هدف\","
    "\n"
    "  \"learning_objectives\": [\"هدف یادگیری با پشتوانهٔ صریح منبع\"],"
    "\n"
    "  \"sections\": ["
    "\n"
    "    {"
    "\n"
    "      \"heading\": \"عنوان کوتاه و گویا\","
    "\n"
    "      \"source_locators\": [\"برچسب عیناً از ورودی، مثل [صفحهٔ ۲]\"],"
    "\n"
    "      \"paragraphs\": [\"پاراگراف توضیحی کامل\"],"
    "\n"
    "      \"bullets\": [\"مورد فهرستی\"],"
    "\n"
    "      \"definitions\": [{\"term\": \"اصطلاح\", \"term_en\": \"English term\", \"definition\": \"تعریف کامل\"}],"
    "\n"
    "      \"examples\": [\"مثال کامل همراه با توضیح\"],"
    "\n"
    "      \"steps\": [\"گام ۱ …\", \"گام ۲ …\"],"
    "\n"
    "      \"formulas\": [\"صورت دقیق فرمول\"],"
    "\n"
    "      \"key_points\": [\"نکتهٔ کلیدی همین بخش\"],"
    "\n"
    "      \"tables\": [{\"caption\": \"جدول چه را مقایسه می‌کند\", \"headers\": [\"ستون ۱\", \"ستون ۲\"], \"rows\": [[\"مقدار\", \"مقدار\"]]}],"
    "\n"
    "      \"callouts\": [{\"kind\": \"نکته\", \"text\": \"متن برجسته\"}]"
    "\n"
    "    }"
    "\n"
    "  ],"
    "\n"
    "  \"practical_tips\": [{\"text\": \"توصیهٔ عملی منبع‌دار\", \"basis\": \"عین جملهٔ منبع (۵ تا ۳۰ واژه)\", \"kind\": \"کاربردی\"}],"
    "\n"
    "  \"key_points\": [\"نکته‌های کلیدی کل جزوه\"],"
    "\n"
    "  \"review_questions\": [\"پرسش مرور با پشتوانهٔ صریح منبع\"],"
    "\n"
    "  \"glossary\": [{\"term\": \"اصطلاح\", \"definition\": \"تعریف کوتاه\"}]"
    "\n"
    "}"
    "\n"
    "۴-۲) هر کلید به‌جز sections اختیاری است؛ محتوایی ندارید، حذفش کنید یا آرایهٔ"
    "\n"
    "     خالی بدهید. کلید تازه نمی‌سازید و schema را تغییر نمی‌دهید."
    "\n"
    "۴-۳) sections هرگز خالی نمی‌ماند: دست‌کم یک بخش با عنوان معنادار و محتوای"
    "\n"
    "     واقعی از منبع."
    "\n"
    "۴-۴) مقدار kind در callouts فقط یکی از «نکته»، «هشدار»، «یادآوری»؛ در"
    "\n"
    "     practical_tips فقط یکی از «کاربردی»، «آزمونی»، «هشدار»."
    "\n"
    "۴-۵) جدول مقایسه‌ای (tables): هرگاه دو یا چند مفهوم، روش، مرحله، دارو، فرمول یا"
    "\n"
    "     دسته با ویژگی‌های متناظر مقایسه شده‌اند، جدول مقایسه‌ای بسازید؛ جدول فقط با"
    "\n"
    "     حداقل دو موردِ قابل‌مقایسه و حداقل دو ویژگی؛ هر خانه عیناً از منبع؛ حداکثر دو"
    "\n"
    "     جدول در هر بخش، ۸ سطر و ۶ ستون؛ خانهٔ خالی با حدس پر نمی‌شود. مطمئن"
    "\n"
    "     نیستید؟ فهرست بنویسید."
    "\n"
    "۴-۶) جعبه‌های راهنما (callouts) و نکته‌های کاربردی (practical_tips): در هر بخش"
    "\n"
    "     راهنمایی مفهومی را با «نکته»، خطاهای رایج و استثناها و تله‌های امتحانی را با"
    "\n"
    "     «هشدار»، و قواعد پایه را با «یادآوری» در callouts بیاورید. در"
    "\n"
    "     practical_tips فقط توصیه، هشدار و تأکید عملی یا امتحانیِ صریحِ خود گوینده"
    "\n"
    "     می‌آید (با عین جملهٔ ۵ تا ۳۰ واژه‌ای منبع در basis)؛ نصیحت مطالعه و"
    "\n"
    "     انگیزه‌بخشی ممنوع؛ حداکثر ۸ نکته؛ بدون basis معتبر، نکته‌ای نوشته نمی‌شود."
    "\n"
    "۴-۷) JSON سالم: نقل‌قول دوگانه، بدون کامای اضافی، خط جدید داخل رشته‌ها با \\n."
    "\n"
    ""
    "\n"
    "### اسکلت لازم جزوهٔ کامل (هیچ بلوکی حذف نشود)"
    "\n"
    "جزوه باید یک کتابچهٔ درسی کامل و جایگزین حضور در کلاس باشد، نه خلاصه. ترتیب بلوک‌ها: جلد و عنوان درس → فهرست مطالب → هدف کلی درس → بخش‌بندی منطقی مطابق ترتیب منبع → تعریف‌ها، مثال‌ها، گام‌ها، فرمول‌ها، جدول‌ها، نکته‌ها و هشدارها در جای خود → جمع‌بندی یک‌صفحه‌ای پایان جزوه → واژه‌نامهٔ اصطلاح‌های انگلیسی."
    "\n"
    "- جلد و عنوان درس را در title بگذارید؛ هدف کلی درس را در learning_objectives بنویسید (فقط اگر متن منبع پشتوانه‌ای دارد). فهرست مطالب از heading بخش‌ها ساخته می‌شود؛ پس بخش‌ها را با عنوان‌های کوتاه، گویا و به ترتیب منبع بچینید."
    "\n"
    "- داخل هر بخش: تعریف‌ها در definitions، مثال‌های کامل در examples، روند گام‌به‌گام در steps، صورت دقیق فرمول‌ها در formulas، مقایسه‌ها در tables، هشدارها در callouts و نکته‌های مهم در key_points."
    "\n"
    "- جمع‌بندی پایان جزوه را در summary و key_points سطح کل درس بنویسید (یک صفحه، بدون مطلب تازه)؛ واژه‌نامه را در glossary فقط با اصطلاح‌های انگلیسی‌ای پر کنید که عیناً در همین متن منبع آمده‌اند."
    "\n"
    "- قانون پوشش (بدون استثنا): هر واحد آموزشی متن منبع — تعریف، دلیل، مکانیزم، مثال، مرحله، فرمول، عدد، واحد، هشدار و استثنا — باید دست‌کم یک‌بار در یکی از بخش‌ها بیاید؛ هیچ واحدی را به بهانهٔ فشرده‌سازی یا تکراری‌بودن حذف نکنید."
    "\n"
    ""
    "\n"
    "──────────────────────────────"
    "\n"
    "اصل ۵ — مکان‌یاب منبع"
    "\n"
    "──────────────────────────────"
    "\n"
    "۵-۱) برچسب‌های کروشه‌دار ورودی مثل [صفحهٔ ۲] یا [اسلاید ۳] مکان‌یاب واقعی‌اند."
    "\n"
    "۵-۲) در source_locators فقط برچسبِ کامل و عیناً موجود در ورودی را بیاورید که"
    "\n"
    "     واقعاً محتوای همان بخش را پشتیبانی می‌کند؛ برچسب نمی‌سازید، تغییر"
    "\n"
    "     نمی‌دهید، حدس نمی‌زنید؛ اطمینان ندارید، خالی بگذارید."
    "\n"
    "۵-۳) متن برچسب را به محتوای آموزشی تبدیل نکنید."
    "\n"
    ""
    "\n"
    "──────────────────────────────"
    "\n"
    "اصل ۶ — رفتار چندبخشی (درس‌های بلند که تکه‌تکه می‌آیند)"
    "\n"
    "──────────────────────────────"
    "\n"
    "۶-۱) ورودی هر درخواست با یک سطر موقعیت آغاز می‌شود — «(بخش i از n)» یا"
    "\n"
    "     «(درس تک‌بخشی)» — و با سطر «موضوع همین بخش» پایان می‌یابد؛ هر درخواست فقط"
    "\n"
    "     سهم خودش را می‌نویسد و نقشهٔ موضوع‌ها در پیام کاربر راهنمای جای شماست، نه"
    "\n"
    "     متنی برای بازنویسی. موضوع بخش‌های دیگر را تکرار نکنید و مقدمه/جمع‌بندی کل"
    "\n"
    "     درس را دوباره ننویسید."
    "\n"
    "۶-۲) اگر متنِ بخش با ادامهٔ توضیح قبلی شروع می‌شود، اولین بخش خود را با"
    "\n"
    "     عیناً همان عنوان بخش پیشین بنویسید — نه عنوان تازه و نه عنوانی با"
    "\n"
    "     واژهٔ «ادامه»."
    "\n"
    "۶-۳) در درس چندبخشی، کلیدهای summary و key_points و glossary سطح کل درس را"
    "\n"
    "     خالی بگذارید و نکته‌های بخش را در key_pointsِ همان بخش بنویسید (اما"
    "\n"
    "     tables، callouts و practical_tipsِ همین بخش را کامل استخراج کنید)."
    "\n"
    "۶-۴) انتهای هر بخش را طوری ببندید که به موضوع بعدی وصل شود، بدون ادعای تازه."
    "\n"
    ""
    "\n"
    "──────────────────────────────"
    "\n"
    "اصل ۷ — لحن دانشگاهی"
    "\n"
    "──────────────────────────────"
    "\n"
    "۷-۱) سوم‌شخص، بی‌طرف، فشرده؛ بدون «شما»، «بیایید»، «در این جزوه خواهیم دید»،"
    "\n"
    "     ایموجی، تعارف و لحن تبلیغاتی."
    "\n"
    "۷-۲) هر بخش در جملهٔ اول می‌گوید موضوعش چیست و به مطلب قبلی چگونه وصل است؛"
    "\n"
    "     شروع قالبی و تکراری ممنوع."
    "\n"
    "۷-۳) روابط را با واژه‌های ربط خود منبع نگه دارید (چون، بنابراین، اما، برای"
    "\n"
    "     نمونه، نخست، سپس) و ترتیب دلایل و جزئیات را جابه‌جا نکنید."
    "\n"
    "۷-۴) تردید منبع، تردید می‌ماند (ممکن است، احتمالاً، شواهد محدود)؛ به قطعیت"
    "\n"
    "     تبدیل نمی‌شود و ابهام «بهتر» بازنویسی نمی‌شود."
    "\n"
    ""
    "\n"
    "──────────────────────────────"
    "\n"
    "اصل ۸ — ممنوعیت‌های مطلق (بدون استثنا)"
    "\n"
    "──────────────────────────────"
    "\n"
    "۸-۱) عدد، واحد، بازه، تاریخ یا نامی که در متن نیست."
    "\n"
    "۸-۲) آمار، مرجع، دوز، توصیهٔ درمانی، مثال یا تشبیه تازه."
    "\n"
    "۸-۳) ترجمهٔ اصطلاح تخصصی یا بازکردن مخفف، مگر منبع گفته باشد."
    "\n"
    "۸-۴) تبدیل واحد، گرد کردن، محاسبه، مقدار مشتق‌شده."
    "\n"
    "۸-۵) جملهٔ کلیشه‌ای، نصیحت مطالعه، لحن انگیزشی، یادداشت پایانیِ تکراری."
    "\n"
    "۸-۶) بازنویسی «بهتر» جملهٔ مبهم؛ ابهام منبع حفظ می‌شود."
    "\n"
    "۸-۷) هر خروجی غیر JSON، هر کلید خارج از قرارداد، هر بخشِ بدون پشتوانه."
)


def notes_system_prompt() -> str:
    """The one static booklet-agent system prompt (byte-identical per request).

    Cache-stable by construction: it takes no arguments, so no caller can put
    per-job text into it. Everything that varies (source kind, position, topics,
    transcript) is carried by the user message; see :func:`build_chunk_message`
    and :func:`build_edit_message`.
    """
    return _BOOKLET_AGENT_RULES


def build_system_prompt(
    mode: str = "full", *, context_block: str = "", source_locators: bool = False
) -> str:
    """The Persian system prompt of one note request (the static agent rules).

    CR-3: ``full`` is the only mode, so ``mode`` is accepted for API
    compatibility and ignored. ``context_block`` and ``source_locators`` used to
    append rules to this string, which made the system prompt differ per chunk
    and per job and defeated the provider's prefix cache. They are still
    accepted (existing callers and benchmarks pass them) but no longer change
    the prompt: per-request material belongs in the user message, and the
    source-locator contract is already part of the agent rules.
    """
    _ = (mode, context_block, source_locators)
    return notes_system_prompt()


#: Provider-visible markers of the user-message templates. They are the only
#: mutable scaffolding of a map request; everything else is the source text.
CHUNK_POSITION_PREFIX = "متن پیاده‌سازی‌شدهٔ خام (بخش {index} از {total}):"
SINGLE_CHUNK_HEADER = "متن پیاده‌سازی‌شدهٔ خام (درس تک‌بخشی):"
CHUNK_TOPIC_PREFIX = "موضوع همین بخش:"
SOURCE_DIGEST_HEADING = "متن منبع کامل:"
DRAFT_HEADING = "پیش‌نویس جزوه که باید ویرایش و یکدست شود:"


def build_chunk_message(
    body: str,
    *,
    index: int,
    total: int,
    topic: str = "",
    context_block: str = "",
    header: str = "",
) -> str:
    """The user message of one map request — the documented chunk template.

    ``متن پیاده‌سازی‌شدهٔ خام (بخش i از n):`` then the source text, then the
    topic of this part. The optional orientation block (topic map, neighbour
    windows) sits between the position line and the text. Everything the
    template does *not* name — the rules, the JSON contract, the register —
    lives in the static system prompt, so this message stays source data only.

    ``header`` replaces the source label for a source that is not a transcript
    (a presentation is slides plus narration, so it says so first). The
    position line still follows, because the model needs to know which part it
    is writing.

    A one-part lesson has no "part i of n" to announce, so its header says the
    lesson is single-part: that is what tells the model to fill the
    lesson-level keys (see clause ۶-۳ of the contract).
    """
    label = header or SINGLE_CHUNK_HEADER
    if total <= 1:
        return label + "\n\n" + body
    position = CHUNK_POSITION_PREFIX.format(
        index=to_persian_digits(index), total=to_persian_digits(total)
    )
    parts = [label, position] if header else [position]
    if context_block:
        parts.append(context_block)
    parts.append(body)
    parts.append(f"{CHUNK_TOPIC_PREFIX} {topic}" if topic else f"{CHUNK_TOPIC_PREFIX} (نامشخص)")
    return "\n\n".join(parts)


def build_edit_message(source_digest: str, merged_draft: str) -> str:
    """The user message of the editorial compile (and repair) request.

    Two parts, in this order: the source material the edit is checked against,
    then the draft being edited — which stays immediately before the model's
    own output. The edit instructions are static and live in the system prompt
    (:func:`edit_system_prompt`), so this message carries only source + draft.
    """
    return (
        SOURCE_DIGEST_HEADING
        + "\n\n"
        + source_digest
        + "\n\n"
        + DRAFT_HEADING
        + "\n\n"
        + merged_draft
    )


#: System prompt of the editorial pass (compile and repair). It is static for
#: the same cache reason as :func:`notes_system_prompt`; the document being
#: edited travels in the user message.
_EDIT_SYSTEM_PROMPT = (
    "شما ویراستار نهایی جزوهٔ فارسی «گاماس» هستید. دو چیز در پیام کاربر می‌آید: متن منبع "
    "کامل و پیش‌نویس جزوه به شکل JSON. پیش‌نویس از چند بخش جداگانه ساخته شده و ممکن است "
    "ناپیوسته یا تکراری باشد. خروجی شما همان جزوهٔ واحد است، نه خلاصه‌ای از آن.\n"
    "\n"
    "قانون بنیادی: شما اجازهٔ بازنویسی سبکی و افزودن دانش بیرونی ندارید؛ کارتان ویرایش "
    "ساختاری است.\n"
    "\n"
    "کار شما:\n"
    "- جمله‌ها را تا حد امکان عیناً نگه دارید؛ فقط نقطه‌گذاری، فاصله و پیوندهای لازم را "
    "اصلاح کنید.\n"
    "- بخش‌هایی که به یک موضوع واحد تعلق دارند را در یک بخش منسجم ادغام کنید؛ ترتیب منطقی "
    "متن را نگه دارید.\n"
    "- عنوان بخش‌ها را روشن، گویا و هماهنگ با موضوع‌های منبع بنویسید؛ بخش‌های هم‌موضوع یا "
    "دنباله‌دار را ادغام کنید، اما موضوع‌های متمایز را در یک بخش واحد ادغام نکنید.\n"
    "- تکرار لفظ‌به‌لفظ را یک‌بار بنویسید؛ دو جمله که یک مفهوم را با کلمات متفاوت "
    "می‌گویند تکرار نیستند و هر دو می‌مانند.\n"
    "- هیچ توضیح، تعریف، مثال، مرحله، مقایسه، هشدار، استثنا، فرمول، عدد، واحد، تاریخ، "
    "اصطلاح یا نکتهٔ کاربردی را حذف نکنید.\n"
    "- هیچ ادعا، عدد، واحد، مرجع، توصیه یا مثال تازه‌ای اضافه نکنید و هیچ عددی را تبدیل، "
    "گرد یا دوباره محاسبه نکنید.\n"
    "- تعریف‌ها را در definitions، مثال‌ها را در examples، مراحل را در steps، فرمول‌ها را "
    "در formulas، جدول‌های مقایسه‌ای را در tables و نکته‌ها، هشدارها و یادآوری‌ها را در "
    "callouts نگه دارید و هیچ‌کدام را هنگام ادغام بخش‌ها حذف نکنید.\n"
    "- نکته‌های کاربردی و امتحانی (practical_tips) را با متن، نوع (kind) و «basis» عیناً "
    "موجودشان نگه دارید؛ نکته‌ای نسازید و پشتوانه‌ای را عوض نکنید.\n"
    "- اگر دستور، فهرست «موارد جامانده» دارد، هر مورد را فقط با همان جملهٔ منبعی که همراه "
    "آن آمده در جای درست خودش برگردانید و هیچ چیز دیگری اضافه نکنید؛ هر موردی را که در "
    "منبع نیست، حذف کنید.\n"
    "- خلاصهٔ کل درس را در summary، مهم‌ترین نکته‌های کل درس را در key_points و اصطلاح‌های "
    "انگلیسی موجود در منبع را در glossary بنویسید.\n"
    "- «learning_objectives» و «review_questions» را فقط اگر همان متن مدرک روشنی دارد نگه "
    "دارید؛ در غیر این صورت حذف کنید.\n"
    "- اطلاعات تازه اضافه نکنید. اگر چیزی در ورودی نیست، حدس نزنید.\n"
    "\n"
    "ساختار خروجی — فقط یک شیء JSON معتبر، بدون هیچ متن اضافه، با همان ساختار ورودی؛ "
    "«sections» هرگز خالی نمی‌شود، در callouts مقدار kind فقط «نکته»، «هشدار» یا "
    "«یادآوری» و در practical_tips مقدار kind فقط «کاربردی»، «آزمونی» یا «هشدار» است.\n"
)


def edit_system_prompt() -> str:
    """The static editorial system prompt (compile and repair passes)."""
    return _EDIT_SYSTEM_PROMPT


#: Backwards-compatible alias: ``build_compile_system_prompt`` used to append
#: mode/source rules to this string. It no longer varies, which is the point.
COMPILE_SYSTEM_PROMPT = _EDIT_SYSTEM_PROMPT


def build_presentation_system_prompt(mode: str = "full", *, context_block: str = "") -> str:
    """The system prompt for presentation material.

    Presentations and audio transcripts share one agent prompt: the source kind
    is stated by the user message, and the slide/narration coverage rules the
    agent needs are part of the static contract. One prompt string means a job
    that mixes both kinds still hits the prefix cache.
    """
    _ = (mode, context_block)
    return notes_system_prompt()


def build_compile_system_prompt(
    mode: str = "full", *, presentation: bool = False, source_locators: bool = False
) -> str:
    """Build the editorial compile system prompt for a note mode and source kind.

    CR-3: ``full`` is the only mode, so the arguments are accepted for API
    compatibility and ignored: the editorial contract is one static string.
    """
    _ = (mode, presentation, source_locators)
    return _EDIT_SYSTEM_PROMPT


#: Prefix of the transcript-bearing user message (kept for callers that build
#: one-message requests, e.g. the provider probe).
#: Source label of a presentation request (slides plus narration).
PRESENTATION_PROMPT = "محتوای ارائه (متن اسلایدها و گفتار):"


#: The bounded, strictly grounded coverage-fill rules. Appended to the static
#: agent prompt (never to a per-job prompt) so a fill request shares the same
#: cached prefix as every other note request of the job.
COVERAGE_FILL_RULES = (
    "\n\nزیرحالت «پرکردن جاافتاده»:\n"
    "- در پیام کاربر یک واحد آموزشی از متن منبع آمده که در پیش‌نویس جزوه جا افتاده است؛ "
    "فقط همان واحد را، با همان اطلاعات و بدون هیچ افزوده، به صورت یک بخش کوتاه "
    "(heading + paragraphs/bullets و در صورت وجود مقایسه، نکته یا هشدار در همان قطعه: "
    "tables/callouts/practical_tips) برگردانید.\n"
    "- فقط از جمله‌هایی استفاده کنید که در پیام کاربر (متن منبع) هست؛ هیچ عدد، نام، اصطلاح، "
    "مثال، فرمول یا نتیجه‌گیری تازه نسازید و از دانش بیرونی نیاورید.\n"
    "- پاسخ فقط یک شیء JSON معتبر با همان ساختار جزوه و «sections» غیرخالی است؛ متن اضافه "
    "ننویسید."
)
