# راهنمای استقرار Gamas Bot روی هاست

این ربات باید همیشه روشن و به تلگرام متصل باشد. برای همین، VPS، سرور اختصاصی، کانتینر دائمی یا Worker یک PaaS انتخاب مناسبی است. هاست اشتراکی (به‌جز پلن‌های cPanel که شرایط [DEPLOY_CPANEL.md](DEPLOY_CPANEL.md) را دارند؛ آن راهنما به زبان انگلیسی است و شامل Cron، فایل `passenger_wsgi.py` و ابزار بررسی هاست `scripts/cpanel_preflight.py` است)، Vercel، Netlify، Lambda و سرویس‌هایی که خودکار خاموش می‌شوند مناسب نیستند.

## منابع پیشنهادی

برای شروع معمولاً این منابع کافی‌اند:

- حداقل ۲ هسته CPU و ۴ گیگابایت RAM
- حداقل ۱۰ تا ۲۰ گیگابایت فضای خالی
- یک Volume دائمی برای پوشه `data`
- مقدار `MAX_CONCURRENT_JOBS=1` تا وقتی مصرف واقعی منابع مشخص شود

هنگام پردازش PowerPoint چند فایل موقت هم ساخته می‌شود؛ بنابراین برای یک ورودی ۲ گیگابایتی، بیشتر از ۲ گیگابایت فضای خالی لازم دارید.

## نصب روی Ubuntu/Debian

### ۱. نصب ابزارهای سیستم

```bash
sudo apt update
sudo apt install -y \
  python3 python3-venv python3-pip git ca-certificates libreoffice-writer
```

باینری‌های رسانه‌ای `ffmpeg` و `ffprobe` لازم نیستند: کتابخانه‌های FFmpeg داخل
بستهٔ `av` نصب می‌شوند و تبدیل فایل قدیمی `ppt` با بستهٔ خالص پایتونی
`ppt2pptx` انجام می‌گیرد. اما `soffice` از LibreOffice برای صفحه‌بندی دقیق
فهرست ایستای جزوه‌های بلند لازم است؛ `pypdf` هم همراه نیازمندی‌های Python نصب
می‌شود. اگر LibreOffice نصب نیست، `DOCX_TOC_ENABLED=false` را آگاهانه تنظیم کنید؛
فهرست حذف می‌شود. در حالت پیش‌فرض، اگر صفحه‌بندی واقعی در دسترس نباشد سندِ دارای
فهرست با شماره‌های حدسی ساخته/ارسال نمی‌شود.

بررسی نصب پس از مرحلهٔ نصب وابستگی‌ها:

```bash
cd /opt/gamas-bot
.venv/bin/python -m gamas_bot.media_worker check
```

### ۲. ساخت کاربر سرویس و نصب پروژه

```bash
sudo useradd --system --no-create-home --home-dir /opt/gamas-bot \
  --shell /usr/sbin/nologin bot
sudo git clone https://github.com/AmiraliGhamkhar/Gamas_bot.git /opt/gamas-bot
sudo chown -R bot:bot /opt/gamas-bot

sudo -u bot python3 -m venv /opt/gamas-bot/.venv
sudo -u bot /opt/gamas-bot/.venv/bin/pip install --upgrade pip
sudo -u bot /opt/gamas-bot/.venv/bin/pip install -r /opt/gamas-bot/requirements.txt
sudo -u bot mkdir -p /opt/gamas-bot/data/tmp /opt/gamas-bot/data/.cache

sudo -u bot cp /opt/gamas-bot/.env.example /opt/gamas-bot/.env
sudo chmod 600 /opt/gamas-bot/.env
sudo nano /opt/gamas-bot/.env
```

گزینهٔ `--no-create-home` عمدی است: اگر `useradd` پوشه را با فایل‌های پیش‌فرض پر کند، `git clone` در آن شکست می‌خورد. برای نصب موجود، مراحل ساخت کاربر و Clone را تکرار نکنید و داده‌ها را حذف نکنید.

اطلاعات تلگرام و کلید حداقل یکی از سرویس‌های تبدیل گفتار را وارد کنید. API ساخت جزوه اختیاری است؛ اگر آن را تنظیم نکنید، ربات متن خام را تحویل می‌دهد. `.env.example` شامل تنها مقصد کارت‌به‌کارت و طرح‌های canonical است. برای مدیریت چند کلید API از پنل مدیر، کلید Fernet را با دستور زیر بسازید و فقط در `.env` با مجوز محدود قرار دهید؛ کلید اصلی رمزگذاری در SQLite ذخیره نمی‌شود:

```bash
/opt/gamas-bot/.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

این کلید را ثابت نگه دارید و به‌شکل امن پشتیبان بگیرید؛ با گم‌شدن آن credentialهای رمز‌شده قابل بازیابی نیستند.

### ۳. اجرای آزمایشی با همان کاربر systemd

```bash
cd /opt/gamas-bot
sudo -u bot env HOME=/opt/gamas-bot/data \
  XDG_CACHE_HOME=/opt/gamas-bot/data/.cache \
  /opt/gamas-bot/.venv/bin/python -m gamas_bot
```

بعد از مشاهده پیام آنلاین شدن ربات، با `Ctrl+C` خارج شوید. اجرای آزمایشی با کاربر `bot` مهم است؛ چون بسیاری از خطاهای Permission در اجرای root دیده نمی‌شوند.

### ۴. راه‌اندازی systemd

```bash
sudo cp /opt/gamas-bot/deploy/gamas-bot.service /etc/systemd/system/gamas-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now gamas-bot
sudo systemctl status gamas-bot --no-pager -l
sudo journalctl -u gamas-bot -f
```

دستورهای روزمره:

```bash
sudo systemctl restart gamas-bot
sudo systemctl stop gamas-bot
sudo journalctl -u gamas-bot -n 200 --no-pager
sudo journalctl -u gamas-bot -p warning..alert --since '1 hour ago'
```

اگر پروژه را در مسیری غیر از `/opt/gamas-bot` نصب می‌کنید، این موارد را در unit تغییر دهید:

- `WorkingDirectory`
- `EnvironmentFile`
- `ExecStart`
- `HOME` و `XDG_CACHE_HOME`
- `ReadWritePaths`

Unit از `ProtectSystem=strict` استفاده می‌کند و فقط مسیر `data/` برای نوشتن باز است. مسیرهای سفارشی دیتابیس، Session، فایل موقت و `LOG_FILE` باید زیر همین مسیر باشند یا به `ReadWritePaths` اضافه شوند.

## پردازش رسانه دقیقاً چه کاری انجام می‌دهد؟

ربات پردازش صدا و تصویر را داخل خود پایتون انجام می‌دهد (فرزند پایتونی
`python -m gamas_bot.media_worker` با کتابخانهٔ PyAV):

1. تشخیص وجود Track صوتی و مدت فایل؛
2. جدا کردن صدا از ویدیو؛
3. تبدیل Codecهای نامعمول به PCM یا Opus؛
4. ادغام صدای چند اسلاید؛
5. یکسان‌سازی Sample Rate و کانال صوتی.

دستورها بدون Shell اجرا می‌شوند و با `MEDIA_TIMEOUT_SECONDS` محدود هستند.
برای عیب‌یابی یک فایل:

```bash
cd /opt/gamas-bot
sudo -u bot .venv/bin/python -m gamas_bot.media_worker probe -- /path/to/input.mp4
sudo -u bot touch /opt/gamas-bot/data/tmp/write-test
df -h /opt/gamas-bot/data
df -i /opt/gamas-bot/data
```

خطاهای رایج:

- `media dependencies are missing`: بستهٔ `av` در venv نصب نیست؛
  `pip install -r requirements.txt` را با کاربر سرویس اجرا کنید.
- `Permission denied`: مالکیت `data/tmp` اشتباه است.
- `No space left on device`: هم فضای دیسک و هم inodeها را بررسی کنید.
- Timeout مکرر: قبل از افزایش Timeout، CPU، سرعت دیسک و سالم بودن فایل ورودی را بررسی کنید.

## تبدیل PowerPoint قدیمی چگونه انجام می‌شود؟

برای `pptx` جدید هیچ تبدیلی لازم نیست. فایل‌های قدیمی `ppt`، `pps` و `pot` با
بستهٔ `ppt2pptx` (کاملاً درون پایتون، بدون دفتر یا باینری سیستمی) به `pptx`
تبدیل می‌شوند. زمان تبدیل با `PPT_CONVERT_TIMEOUT_SECONDS` محدود است و حداکثر
حجم ورودی همان `MAX_FILE_SIZE_BYTES` است.

ویژگی‌های قدیمی که قابل انتقال نیستند (انیمیشن، پخش صدا/ویدیوی جاسازی‌شده و
برخی شیءهای OLE) به‌صورت هشدارهای ساخت‌یافته در لاگ ثبت می‌شوند و صورت ظاهری
جایگزین نمایش داده می‌شود.

فرمت‌های `odp` و `otp` پشتیبانی نمی‌شوند و ربات با پیام روشن می‌خواهد فایل با
پسوند `pptx` ذخیره شود. برای غیرفعال کردن کل مسیر تبدیل قدیمی:

```dotenv
PPTX_LEGACY_ENABLED=false
```

## تنظیم API ساخت جزوه

ترتیب سرویس‌ها خودش سیاست failover است و فقط سرویس‌هایی که کلید خودشان را
دارند امتحان می‌شوند. پیش‌فرض پروژه **نارا و سپس OpenRouter** است:

```dotenv
NOTE_PROVIDERS=nara,openrouter

# نارا (NaraRouter)
NARA_API_KEY=...
#NARA_BASE_URL=https://router.bynara.id/v1
#NARA_MODEL=agnes-2.5-flash

# OpenRouter
OPENROUTER_API_KEY=...
#OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
#OPENROUTER_MODEL=openrouter/free
```

اگر فقط یک سرویس می‌خواهید، همان یکی را در `NOTE_PROVIDERS` بگذارید. برای
سرویس‌های دیگر (Gemini، Anthropic، OpenAI یا هر Gateway سازگار با
`/chat/completions`) همان اسلات تک‌سرویسی نسخهٔ قبل کار می‌کند:

```dotenv
# Gemini
NOTE_PROVIDER=gemini
GEMINI_API_KEY=...
#NOTE_MODEL=gemini-2.5-flash

# OpenAI یا هر API سازگار با /chat/completions
NOTE_PROVIDER=openai_compatible
NOTE_API_KEY=...
NOTE_BASE_URL=https://api.openai.com/v1
NOTE_MODEL=gpt-4o-mini

# Anthropic (مدل پیش‌فرض ندارد؛ NOTE_MODEL اجباری است)
NOTE_PROVIDER=anthropic
NOTE_API_KEY=...
NOTE_MODEL=claude-haiku-4-5
```

اگر `NOTE_BASE_URL` به یکی از سرویس‌های شناخته‌شده اشاره کند
(`router.bynara.id` یا `openrouter.ai`)، همان سرویس شناخته می‌شود و در جای
خودش در زنجیره می‌ماند؛ یک کلید هرگز برای دو سرویس استفاده نمی‌شود.

`NOTE_CONTEXT_TOKENS` را روی کوچک‌ترین پنجرهٔ زمینهٔ مدل‌های فعال بگذارید
(پیش‌فرض ۳۲۰۰۰). اندازهٔ هر درخواست از همین عدد حساب می‌شود: خروجی مورد
انتظار، system prompt و زمینهٔ درس کم می‌شوند و باقی‌مانده در مرزهای پاراگراف و
جمله تکه‌تکه می‌شود. اگر مدل باز هم درخواست را بزرگ بداند، همان تکه یک بار
نصف می‌شود (محدود) و هرگز درخواست بزرگِ بدون‌تغییر دوباره فرستاده نمی‌شود.

خطاهای شبکه و پاسخ‌های `429` و `5xx` با Backoff محدود Retry می‌شوند؛ خطای
اعتبارنامه، رد شدن درخواست یا پاسخ نامعتبر باعث رفتن به سرویس بعدی زنجیره
می‌شود. سرویس ساخت جزوه با یک System Prompt سخت‌گیرانه فقط JSON می‌پذیرد
(حالت بومی JSON فقط برای Gemini و `response_format` فقط برای سرویس‌هایی که آن
را مستند کرده‌اند فرستاده می‌شود)، خروجی اعتبارسنجی می‌شود و در صورت نامعتبر
بودن یک بار درخواست اصلاح تکرار می‌شود. با وجود شکست API ساخت جزوه، متن
استخراج‌شده از بین نمی‌رود: **اول فایل `txt` متن خام و سپس جزوهٔ Word** فرستاده
می‌شود، و اگر ارسال فایل متن خام ناموفق باشد همان متن در چند پیام (با رعایت
سقف پیام تلگرام) تحویل داده می‌شود.

خروجی نهایی هر کار دو فایل است: سند Word راست‌به‌چپِ مرتب (با تاریخ شمسی،
جدول‌ها و کادرهای نکته/هشدار) و یک فایل `txt` با متن خام. اگر فقط متن خام
رسیده باشد، در لاگ دنبال خط `note generation failed` یا `note provider failed`
بگردید؛ نام سرویس و دلیل آن‌جا ثبت می‌شود.

### عیب‌یابی خطای HTTP 400 (مثلاً Gemini)

در صورت شکست، بدنهٔ خام پاسخ سرویس لاگ نمی‌شود، اما اطلاعات ساخت‌یافتهٔ خطا (مانند `INVALID_ARGUMENT` و کد `API_KEY_INVALID` در خطای معروف «API key not valid» گوگل، یا `authentication_error`/`invalid_api_key` در سرویس‌های سازگار با OpenAI) با حذف کلید از متن، در لاگ ثبت می‌شود:

```bash
sudo journalctl -u gamas-bot -n 200 --no-pager | grep 'Note API request failed'
```

رایج‌ترین علت‌های HTTP 400 در Gemini: اشتباه بودن یا خالی بودن کلید (`API_KEY_INVALID` — گوگل به‌جای 401، کد 400 برمی‌گرداند)، محدودیت Referrer روی کلید، فعال نبودن Generative Language API در پروژه، یا سقف توکن خروجی نامعتبر برای مدل انتخابی. نوشتن `NOTE_API_MODEL=gemini-2.5-flash-lite` کافی است؛ پیشوند `models/` هم پذیرفته و خودکار اصلاح می‌شود.

### موتورهای STT

سه موتور پشتیبانی می‌شود و ترتیب `STT_PROVIDERS` همان ترتیب failover است.
Speechmatics و Deepgram و یک endpoint سازگار با OpenAI:

```dotenv
STT_PROVIDERS=speechmatics,deepgram,openai_compatible
STT_LANGUAGE=fa

SPEECHMATICS_API_KEY=...
SPEECHMATICS_MODEL=enhanced        # standard | enhanced | melia-1 | oak-1

DEEPGRAM_API_KEY=...
DEEPGRAM_MODEL=nova-3              # فارسی فقط با Nova-3؛ nova-2 زبان fa ندارد

# STT_OPENAI_BASE_URL=https://api.groq.com/openai/v1
# STT_OPENAI_API_KEY=...
# STT_OPENAI_MODEL=whisper-large-v3
```

موتوری که کلید ندارد یا از سقف حجم فایل بزرگ‌تر است، با یک پیام روشن در لاگ
رد می‌شود و نوبت به موتور بعدی می‌رسد. اگر همهٔ موتورها رد شوند، کار با خطای
قابل‌پیگیری شکست می‌خورد و اعتبار رزروشده آزاد می‌شود؛ هیچ متن ساختگی
تولید نمی‌شود.

نکته‌های مهم:

* **فارسی و Deepgram**: زبان `fa` فقط در Nova-3 وجود دارد؛ `nova-2` درخواست
  `language=fa` را با خطای ۴۰۰ رد می‌کند. مقدار پیش‌فرض `DEEPGRAM_MODEL`
  در کد `nova-3` است و `python -m gamas_bot --check` ترکیب ناسازگار را هشدار
  می‌دهد. برای `STT_LANGUAGE=auto` پارامتر `detect_language=true` فرستاده
  می‌شود (حذف `language` باعث تشخیص زبان نمی‌شود و Deepgram پیش‌فرض انگلیسی
  می‌گیرد).
* **Speechmatics**: `enhanced` و `standard` زبان `fa` را مستقیم می‌پذیرند.
  `melia-1` و `oak-1` فقط در Batch و فقط با `STT_LANGUAGE=multi` کار می‌کنند
  (`auto` را رد می‌کنند) و confidence و واژگان سفارشی نمی‌دهند.
* **OpenAI-compatible**: زبان به‌صورت کد ISO-639-1 (`fa`) فرستاده می‌شود و
  برای `auto`/`multi` حذف می‌شود تا Whisper خودش زبان را تشخیص دهد. خروجی با
  `response_format=verbose_json` گرفته می‌شود و اگر gateway آن را صریحاً رد
  کند، یک بار با `json` تکرار می‌شود.
* **خطاهای موقت** (شبکه، `408`، `425`، `429` و `5xx`) تا دو بار دیگر با
  Backoff محدود Retry می‌شوند؛ `Retry-After` تا سقف ۶۰ ثانیه رعایت می‌شود تا
  نوبت به موتور بعدی هم برسد. خطاهای دائمی (`400`، `404`، `422`، `401`،
  `403`) بلافاصله باعث رفتن به موتور بعدی می‌شوند.
* **Cooldown** در حافظهٔ همان پروسه: بعد از خطای اعتبارنامه ۱۵ دقیقه و بعد از
  تمام‌شدن سهم خطاهای موقت ۶۰ ثانیه. موتوری که در Cooldown است با ذکر
  ثانیه‌های باقی‌مانده در لاگ رد می‌شود؛ اگر همه در Cooldown باشند، خطای نهایی
  نام همهٔ موتورها را می‌آورد.
* **سقف حجم**: Speechmatics کمتر از ۱GB در هر `POST /jobs` (بیشتر از آن فقط
  با URL عمومی `fetch_data`، که این ربات استفاده نمی‌کند)، Deepgram تا ۲GB و
  سرویس سازگار تا `STT_OPENAI_MAX_UPLOAD_BYTES` (پیش‌فرض ۲۵MB). فایل
  بزرگ‌تر به موتور بعدی سپرده می‌شود و صدا برای STT تکه تکه نمی‌شود تا دقت
  کلمات در مرز قطعه‌ها کم نشود.
* **سقف مدت Speechmatics**: Batch برای هر Job حدود ۲ ساعت صدای ضبط‌شده را
  قبول می‌کند؛ فایل بلندتر را خود سرویس رد می‌کند و کار با موتور بعدی ادامه
  می‌یابد (برش خودکار پیاده‌سازی نشده است).
* **لاگ تشخیصی**: هر Job با کد `GMS-000123` در لاگ قابل جست‌وجو است؛ نام موتور،
  حجم فایل، مدل، زبان، مدت درخواست، شمارهٔ تلاش، وضعیت HTTP و متن خطای
  پاک‌سازی‌شده (بدون کلید و بدون متن گفتار) ثبت می‌شود.

## لاگینگ و پیگیری خطا

تنظیمات پیشنهادی systemd:

```dotenv
LOG_LEVEL=INFO
LOG_FORMAT=text
LOG_FILE=
LOG_MAX_BYTES=10000000
LOG_BACKUP_COUNT=5
```

با `LOG_FILE=` خالی، لاگ‌ها در journald قرار می‌گیرند. برای Loki/ELK/Cloud Logging می‌توانید `LOG_FORMAT=json` را انتخاب کنید. اگر `LOG_FILE=data/logs/bot.log` تنظیم شود، خود برنامه فایل را بر اساس حجم Rotate می‌کند و نیازی به logrotate جداگانه نیست.

لاگ‌ها شامل شماره Job، زمان سرویس‌های STT، وضعیت و Request ID سرویس ساخت جزوه، زمان و Exit Code ابزارهای خارجی، Migrationهای دیتابیس و Stack Trace هستند. بدنهٔ خام پاسخ خطای سرویس‌ها لاگ نمی‌شود، اما اطلاعات ساخت‌یافتهٔ خطای سرویس ساخت جزوه (وضعیت/نوع خطا، کد علت و پیام محدودشدهٔ سرویس) با حذف کلید API ثبت می‌شود تا خطاهایی مانند HTTP 400 قابل‌عیب‌یابی باشند. خطای STT با نوع خطا یا وضعیت HTTP ثبت می‌شود. کلید Gemini در Header ارسال می‌شود، نه Query نشانی. کلید API، Prompt و متن جزوه عمداً لاگ نمی‌شوند؛ با این حال دسترسی به لاگ‌ها و فایل‌های داده را محدود نگه دارید.

در خطای پردازش، کاربر کدی مانند `GMS-000123` دریافت می‌کند. این کد همان Submission ID قابل جست‌وجو در لاگ است:

```bash
sudo journalctl -u gamas-bot --since today | grep 'GMS-000123'
```

ترتیب پیشنهادی عیب‌یابی:

1. `systemctl status gamas-bot -l`
2. `journalctl -u gamas-bot -n 200 --no-pager`
3. بررسی مالکیت `.env` و پوشه `data`
4. بررسی نصب بسته‌های Python با `media_worker check`
5. اجرای Foreground با کاربر `bot`
6. بررسی فضای دیسک، RAM و خطاهای `401`، `403`، `429` و `5xx`

`status=203/EXEC` یعنی مسیر Python در `ExecStart` اشتباه است یا venv ساخته نشده. خطای `readonly database` یعنی کاربر `bot` یا محدودیت `ReadWritePaths` اجازه نوشتن در مسیر دیتابیس را ندارد.

## به‌روزرسانی و پشتیبان‌گیری

```bash
sudo systemctl stop gamas-bot
sudo -u bot git -C /opt/gamas-bot pull --ff-only
sudo -u bot /opt/gamas-bot/.venv/bin/pip install \
  -r /opt/gamas-bot/requirements.txt --upgrade
sudo systemctl start gamas-bot
```

برای Backup سازگار، ابتدا سرویس را متوقف کنید و سپس این موارد را ذخیره کنید:

- `.env`
- `data/bot.sqlite3` و در صورت وجود فایل‌های `-wal` و `-shm`
- فایل Session تلگرام در `data/`
- `data/receipts/` (یا مسیر `RECEIPT_DIR`)؛ بیرون از web root و با دسترسی محدود
- کلید پایدار `PROVIDER_CREDENTIALS_ENCRYPTION_KEY` را جداگانه و رمز‌شده نگه دارید

روی SQLite و یک Session تلگرام، بیش از یک Replica هم‌زمان اجرا نکنید.


## اعتبار، پرداخت و کلیدهای سرویس

سهمیهٔ رایگان یک‌بار برای هر کاربر **۳۶۰۰ ثانیه مادام‌العمر** است و با `/start`
تجدید نمی‌شود. طرح‌های پرداختی canonical عبارت‌اند از ۹۰۰۰۰ ثانیه با قیمت
۱۵۰۰۰۰ تومان برای ۳۰ روز و ۱۸۰۰۰۰ ثانیه با قیمت ۲۵۰۰۰۰ تومان برای ۳۰ روز. طرح‌های
فعال روی هم جمع می‌شوند و مصرف از اعتباری شروع می‌شود که زودتر منقضی می‌شود؛
ثبت و آزادسازی رزرو زمان رسانه در تراکنش SQLite و با ثانیهٔ صحیح انجام می‌شود.

پرداخت کارت‌به‌کارت است: کاربر رسید را در گفت‌وگوی خصوصی می‌فرستد و **مدیر باید
تراکنش بانکی را دستی تأیید کند**. ارسال رسید هیچ اعتباری نمی‌دهد؛ رد رسید هم
اعتبار ایجاد نمی‌کند. رسیدها بیرون از web root، با مجوز پوشهٔ ۷۰۰ و فایل ۶۰۰
تا زمان بررسی دستی نگهداری می‌شوند؛ پس از تأیید یا رد، بر اساس زمان بررسی و
طبق `RECEIPT_RETENTION_DAYS` (پیش‌فرض ۹۰ روز) حذف می‌شوند.
`/balance`، `/buy`، `/history` و `/cancelpayment` برای کاربر؛ `/payments`،
`/credit شناسه [ثانیه دلیل]`، `/unlimited شناسه on|off دلیل` و `/audit` برای مدیر
در دسترس‌اند. دکمه‌های «💳 خرید اشتراک»، «⏱ اعتبار من»، «💳 پرداخت‌ها»،
«⏱ اعتبار کاربران»، «🧾 طرح‌های فروش» و «⭐ کاربران ویژه» هم در منو وجود دارند.
تغییر دستی اعتبار همیشه به «ثانیهٔ صحیح» و «دلیل» نیاز دارد، شناسهٔ مدیر را ثبت
می‌کند و در گزارش مدیر می‌آید؛ برای حساب مدیر دیگر انجام نمی‌شود.
تعرفهٔ canonical قابل تنظیم است (`FREE_PLAN_HOURS`، `PLAN_5_*`، `PLAN_10_*`،
`PLAN_20_*`، `PLAN_25_*`، `PLAN_50_*` در سه کلید ساعت/قیمت/روز) و همان مقادیر در
جدول `plans` seed می‌شوند؛ هیچ عددی داخل handlerها نیست. از پنل «🧾 طرح‌های
فروش» می‌توان طرح تازه ساخت (`کد | نام | ساعت | قیمت | روز اعتبار`)، قیمت/مدت را
ویرایش کرد، طرح را فعال/غیرفعال یا حذف کرد؛ طرح‌های ساخته/ویرایش‌شدهٔ مدیر با
`is_custom` علامت می‌خورند و sync راه‌اندازی آن‌ها را بازنویسی نمی‌کند. کاربران
ویژه (⭐) بدون کسر اعتبار از ربات استفاده می‌کنند؛ هر پردازش آن‌ها فقط در
`usage_reservations`/`usage_ledger` با دلیل «Unlimited special user» ثبت می‌شود و
موجودی صفر مانع کارشان نمی‌شود. مدیران به‌صورت خودکار ویژه نیستند.

مدیر می‌تواند چند credential را از پنل خصوصی «🔑 API Keys» مدیریت کند: افزودن،
فعال/غیرفعال، حذف، جابه‌جایی اولویت (▲/▼) و تست تک‌کلید. کلیدها در دیتابیس فقط
به‌شکل Fernet ciphertext ذخیره می‌شوند؛ master key فقط از environment خوانده
می‌شود و در پنل/لاگ به‌صورت `••••••••1234` می‌ماند. صفحهٔ «🩺 وضعیت سرویس‌ها»
فقط با درخواست مدیر و با نتیجهٔ cache‌شده اجرا می‌شود؛ هر بررسی یک درخواست
خواندنی و رایگان است (فهرست job/project/model) و هیچ job رونویسی یا تولید محتوا
ثبت نمی‌کند. وضعیت‌ها: سالم، کاهش کیفیت، سقف درخواست، خطای احراز هویت، در دسترس
نیست، غیرفعال، تنظیم‌نشده و تنظیم‌شده (بررسی‌نشده). خطای ۴۲۹ باعث رعایت
`Retry-After`، cooldown و چرخش کلید می‌شود؛ ۴۰۱/۴۰۳ کلید را قرنطینه می‌کند و
خطاهای درخواست نامعتبر ۴۰۰/۴۱۵/۴۲۲ باعث چرخش نمی‌شوند. تلاش‌های گذرا محدود
هستند و بعد از آن چرخش انجام می‌شود. کلیدهای محیطی قدیمی همچنان کار می‌کنند.

## رفتار محدودیت‌ها و بازیابی خطا

- `STT_JOB_TIMEOUT_SECONDS` سقف هر تلاش یک سرویس STT است؛ Upload، Poll و دریافت متن را شامل می‌شود. تلاش سرویس جایگزین بودجهٔ جدا دارد و ممکن است خطای Socket زودتر رخ دهد. Timeout محلی، Job ثبت‌شده در Speechmatics را از راه دور حذف نمی‌کند.
- `MAX_CONCURRENT_JOBS` فقط تعداد کارهای فعال را محدود می‌کند؛ تعداد کارهای منتظر محدود نشده است. تا قبل از افزودن Rate Limit و محدودیت پذیرش، ربات را در اختیار کاربران مورد اعتماد بگذارید.
- صف در حافظه است. پس از Restart، کارهای نیمه‌تمام Failed می‌شوند و باید دوباره ارسال شوند؛ پوشه‌های موقت باقی‌مانده پاک می‌شوند. فقط یک نمونه از برنامه را اجرا کنید.
- عددهای `nan` و `inf` برای زمان‌ها پذیرفته نمی‌شوند. مقادیر اعشاری باید متناهی باشند.
- فایل حداقل یک میلیارد بایتی فقط با Deepgram پردازش می‌شود؛ این قانون حتی با Fallback خاموش برقرار است. اگر فقط کلید موتور دوم موجود باشد، همان موتور استفاده می‌شود.
- متن اسلایدهای طولانی برای Chunking و خروجی خام کامل نگه داشته می‌شود؛ تعداد درخواست‌ها و هزینه می‌تواند بیشتر شود. اتصال دقیق زمان صدا به اسلاید وجود ندارد و حفظ ارتباط مطالب در چند Chunk تضمین نیست.
- اگر API تولید جزوه پایان خروجی به‌علت سقف Token را اعلام کند، خروجی ناقص پذیرفته نمی‌شود و محتوای استخراج‌شده ارسال می‌شود.
- Migration و تغییرهای چندمرحله‌ای دیتابیس تراکنشی هستند؛ خطا باعث Rollback می‌شود. برای این به‌روزرسانی Migration جدید لازم نیست.
- دستورها و دکمه‌های مدیریت فقط در چت خصوصی ربات فعال‌اند. پیام گروه نمی‌تواند پاسخ مرحلهٔ ارسال همگانی خصوصی باشد. `/cancel` یا رفتن به منو، عمل منتظر را لغو می‌کند.

## امنیت ابزارها و Workflow

کارگر رسانه فقط از Protocolهای محلی `file` و `pipe` استفاده می‌کند تا Playlist ارسالی نشانی شبکه را واکشی نکند. این محدودیت Sandbox کامل نیست؛ دسترسی به فایل‌های محلی همچنان تابع مجوز کاربر سرویس است. بستهٔ Python خود را به‌روز نگه دارید (`av` و `ppt2pptx`) و محدودیت دیسک، حافظه و CPU و جداسازی فایل‌سیستم را اعمال کنید. در Linux، Timeout و Cancel گروه پردازش کارگر رسانه را هم متوقف می‌کند؛ در Windows برای پاک‌سازی فرایندهای فرزند از Supervisor مناسب استفاده کنید.

Workflow نامرتبط RDP حذف شده، چون گذرواژهٔ ثابت مدیر و تنظیم غیرفعال‌سازی NLA داشت. گذرواژه در تاریخچهٔ Git باقی است؛ اگر جایی دوباره استفاده شده آن را عوض کنید، Runnerهای قدیمی را متوقف کنید و دسترسی Tailscale آن‌ها را بررسی کنید. GitHub Actions جای هاست دائمی ربات نیست.

## آزمون قبل از استقرار

از ریشهٔ پروژه و در محیط مجازی:

```bash
python -m unittest discover -s tests -v
python -m pip check
```

آزمون‌های شبکهٔ Telegram و APIهای پولی Mock هستند و کلید لازم ندارند. آزمون‌های رسانه‌ای واقعی با خود بستهٔ `av` اجرا می‌شوند و به هیچ باینری سیستمی نیاز ندارند؛ نصب `ffmpeg` لازم نیست. CI لینوکسی Pythonهای ۳.۱۱، ۳.۱۲ و ۳.۱۳ را بررسی می‌کند؛ اجرای واقعی Windows باید روی هاست مقصد تأیید شود.

گزارش بازبینی و محدودیت‌های تأیید (تاریخی): [AUDIT_HISTORY.md](AUDIT_HISTORY.md).
