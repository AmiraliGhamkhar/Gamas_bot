# Testing

Tests use fake Telegram/provider responses: no API keys, no production data and no billable calls.
CI runs them on Python 3.11, 3.12 and 3.13.

## Windows PowerShell (local development)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m scripts.sync_requirements --check
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -m gamas_bot.media_worker check
```

If `ruff.exe` is not on the path, activate the virtualenv and run `ruff check .`.

## cPanel / Linux

After installing `requirements.txt` with the interpreter cron uses:

```bash
python -m gamas_bot --check
python -m scripts.cpanel_preflight
python -m gamas_bot.media_worker check
```

The first is offline; the preflight also tests disk, permissions, the lock and outbound network.
Run the test suite only on a development machine, never with production `.env` or data.

## What the suite covers

| File | Covers |
| --- | --- |
| `tests/test_doc_extract.py` | Real PDF/Word/PowerPoint extraction, Word tables and heading levels, malformed/encrypted/scanned/empty files, long sections, exact chunk budgets, locators and SHA-256 |
| `tests/test_document_ingestion.py` | PDF/DOCX/PPTX upload → cached extraction → source-anchored generation; legacy PPT conversion and presentation queue; size boundary/stream gate, per-user and concurrent dedupe, ownership, retry, coverage and bounded concurrency |
| `tests/test_free_uses.py` | Independent gifts, owned claim/reset races, failed/cancelled/undeliverable/expired previews, startup recovery, shutdown/save races, paid refunds, curation and paywall behavior |
| `tests/test_admin_hubs.py` | Three hubs and legacy aliases, admin authorization, identity in payment/support/audit views, peer-admin protection, confirmation and approval/rejection ledger paths |
| `tests/test_migration_schema.py` | Fresh migration gate, populated 013 → 014 and 014 → 015 upgrades, legacy orphan repair, preserved child links, FK enforcement and interrupted migration replay |
| `tests/test_reaudit_workflows.py` | Durable paid recovery and save/cancel races, one-shot callback alerts, narrated textless PPTX, range integrity, admission/shutdown, concurrent document extraction and text limits, progress backpressure, exact split regressions |
| `tests/test_user_identity.py` | Signed int64 boundaries, no float IDs, updated identity on duplicate `/start` and callbacks, username search/removal and strict user-reference integrity checks |
| `tests/test_learning.py` | Source and difficulty validation, prompts, 10/5–20/10–40/20 presets, weighted coverage and normalized near-deduplication |

Fixtures build real documents in memory (reportlab, `python-docx`, `python-pptx`); legacy PPT
conversion also uses the curated fixtures documented in `tests/fixtures/README.md`. Provider calls
are stubbed, so these tests do not verify live providers or the Telegram client UX.

## آزمون دستی فارسی در محیط آزمایشی

این مراحل را فقط با ربات، حساب‌ها و نسخهٔ پشتیبان پایگاه‌دادهٔ **آزمایشی** انجام دهید؛ برای
آزمون پرداخت از رسید یا اطلاعات واقعی استفاده نکنید. نتایج آزمون خودکار جایگزین بررسی ظاهر
تلگرام و کیفیت پاسخ سرویس واقعی نیست.

1. با کاربری که اعتبارش صفر است `/start` بزنید؛ سپس با نام و نام‌کاربری تغییرکرده دوباره وارد
   شوید. مدیر در «👥 کاربران» با شناسه و `@نام‌کاربری` جست‌وجو کند: فقط یک رکورد، نام کامل،
   شناسهٔ عددی دقیق و آخرین زمان مشاهده باید دیده شود. نمایش بدون نام‌کاربری و چینش راست‌به‌چپ
   و کپی شناسه را هم بررسی کنید.
2. PDF متنی، DOCX دارای عنوان و جدول، و PPTX متنی بفرستید. پیش‌نمایش، شمارهٔ صفحه/بخش/اسلاید،
   دکمه‌های ساخت و دریافت فایل و مدخل کتابخانه را ببینید. ارائهٔ دارای صدا را جداگانه با
   «🎧 پردازش صدای ارائه» به صف قدیمی بفرستید. `.ppt` را در صورت امکان امتحان کنید؛ برای `.doc`
   نامعتبر راهنمای تبدیل به `.docx` باید نمایش داده شود.
3. PDF اسکن‌شده یا رمزدار، فایل خالی/خراب، عکس به‌جای فایل و سند بیش از `DOC_MAX_BYTES` را
   امتحان کنید؛ پیام فارسی مناسب و عدم ایجاد سند خالی را بررسی کنید. یک فایل دقیقاً در سقف
   مجاز باید پذیرفته شود. یک سند تکراری را دو بار و نیز با کاربر دوم بفرستید: اولی همان رکورد
   و ارجاع تازه، دومی رکورد مستقل دارد.
4. از سند موجود در کتابخانه، هر سه اندازهٔ ۱۰/۵، ۲۰/۱۰ و ۴۰/۲۰ را انتخاب کنید. پیشرفت، منبع
   دقیق هر کارت/سؤال، گزینه‌ها، پاسخ و دشواری را بررسی کنید؛ موردی را حذف کنید، صفحات پیش‌نمایش
   را ورق بزنید و سپس ذخیره کنید. اگر بخشی ناموفق یا بررسی‌نشده است، هشدار پوشش و تعداد واقعی
   باید دیده شود؛ انصراف باید ممکن باشد.
5. برای همان کاربر بدون اعتبار، یک مجموعه و یک آزمون مستقل را رایگان ذخیره کنید و پایان آزمون را
   بزنید. تلاش دوم هر نوع باید به خرید اعتبار برسد. اگر کاربر اعتبار طرح رایگان قبلی دارد، ابتدا
   از همان اعتبار مصرف می‌شود؛ این اعتبار جدا از دو هدیه است.
6. خطای سرویس، قطع تولید، شکست ارسال پیش‌نمایش، انصراف و انقضای پیش‌نمایش (۳۰ دقیقه) را جداگانه
   امتحان کنید؛ هدیه/اعتبار مناسب باید برگردد. پیش‌نمایش ذخیره‌نشدهٔ رایگان و پولی را با توقف و
   راه‌اندازی مجدد ربات آزمایشی بررسی کنید؛ مورد ذخیره‌شده نباید بازپرداخت شود. با مکث پاسخ
   سرویس آزمایشی، مدیر هدیه را بازنشانی کند و سپس کار
   قدیمی را کامل/ناموفق کنید؛ نتیجه نباید هدیهٔ جدید را مصرف یا برگرداند.
7. مدیر هر سه مرکز پرداخت، کاربران و سامانه، دکمه‌های بازگشت و صفحه‌کلیدهای قدیمی را باز کند؛
   کاربر عادی نباید هیچ زیرصفحهٔ مدیریتی را باز کند. سفارش آزمایشی با رسید ثبت کنید: تأیید نخست
   نباید اعتبار بدهد، تأیید نهایی باید دقیقاً یک‌بار اعتبار و سابقه ثبت کند، و رد با دلیل نباید
   اعتبار بدهد. بازنشانی هدیه باید تأیید بخواهد و در گزارش فعالیت ثبت شود.
