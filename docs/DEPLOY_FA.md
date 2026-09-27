# راهنمای استقرار Gamas Bot روی هاست

این ربات باید همیشه روشن و به تلگرام متصل باشد. برای همین، VPS، سرور اختصاصی، کانتینر دائمی یا Worker یک PaaS انتخاب مناسبی است. هاست اشتراکی، Vercel، Netlify، Lambda و سرویس‌هایی که خودکار خاموش می‌شوند مناسب نیستند.

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
  python3 python3-venv python3-pip git ca-certificates \
  ffmpeg libreoffice-core libreoffice-impress \
  fonts-dejavu-core fonts-noto-core
```

بررسی نصب:

```bash
command -v ffmpeg ffprobe soffice
ffmpeg -hide_banner -version
ffprobe -hide_banner -version
ffmpeg -hide_banner -encoders | grep -E 'libopus|pcm_s16le'
soffice --headless --version
```

اگر مسیر ابزارها متفاوت است، مسیر کامل را در `.env` قرار دهید:

```dotenv
FFMPEG_BIN=/usr/bin/ffmpeg
FFPROBE_BIN=/usr/bin/ffprobe
SOFFICE_BIN=/usr/bin/soffice
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

اطلاعات تلگرام و کلید حداقل یکی از سرویس‌های تبدیل گفتار را وارد کنید. API ساخت جزوه اختیاری است؛ اگر آن را تنظیم نکنید، ربات متن خام را تحویل می‌دهد.

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

## FFmpeg دقیقاً چه کاری انجام می‌دهد؟

ربات از FFmpeg/FFprobe برای این موارد استفاده می‌کند:

1. تشخیص وجود Track صوتی و مدت فایل؛
2. جدا کردن صدا از ویدیو؛
3. تبدیل Codecهای نامعمول به PCM یا Opus؛
4. ادغام صدای چند اسلاید؛
5. یکسان‌سازی Sample Rate و کانال صوتی.

فرمان‌ها بدون Shell اجرا می‌شوند و با `FFMPEG_TIMEOUT_SECONDS` محدود هستند. برای عیب‌یابی یک فایل:

```bash
ffprobe -v error -show_streams -show_format -of json /path/to/input.mp4
sudo -u bot /usr/bin/ffmpeg -hide_banner -version
sudo -u bot touch /opt/gamas-bot/data/tmp/write-test
df -h /opt/gamas-bot/data
df -i /opt/gamas-bot/data
```

خطاهای رایج:

- `Unknown encoder 'libopus'`: نسخه محدود FFmpeg نصب شده؛ پکیج کامل توزیع را نصب کنید.
- `Permission denied`: مالکیت `data/tmp` یا مسیر باینری اشتباه است.
- `No space left on device`: هم فضای دیسک و هم inodeها را بررسی کنید.
- Timeout مکرر: قبل از افزایش Timeout، CPU، سرعت دیسک و سالم بودن فایل ورودی را بررسی کنید.

## LibreOffice دقیقاً چه زمانی لازم است؟

برای `pptx` جدید نیازی به LibreOffice نیست. فقط فایل‌های قدیمی `ppt`، `pps`، `pot`، `odp` و `otp` ابتدا با LibreOffice به `pptx` تبدیل می‌شوند.

ربات LibreOffice را به‌صورت Headless و با Profile موقت اختصاصی اجرا می‌کند تا تبدیل‌های هم‌زمان قفل یکدیگر را نگیرند. با این حال، LibreOffice و fontconfig به `HOME` و Cache قابل‌نوشتن نیاز دارند؛ این متغیرها در unit آماده تنظیم شده‌اند.

تست تبدیل واقعی:

```bash
sudo -u bot env HOME=/opt/gamas-bot/data \
  XDG_CACHE_HOME=/opt/gamas-bot/data/.cache \
  timeout 60 /usr/bin/soffice --headless --convert-to pptx \
  --outdir /opt/gamas-bot/data/tmp /path/to/sample.ppt
```

اگر LibreOffice روی هاست قابل نصب نیست:

```dotenv
PPTX_LEGACY_ENABLED=false
```

در این حالت فایل‌های صوتی، ویدیویی و `pptx` جدید همچنان کار می‌کنند. برای نمایش درست متن فارسی، فونت‌های فارسی مورد استفاده فایل‌های ارائه را نیز روی سرور نصب کنید.

## تنظیم API ساخت جزوه

یکی از این سه روش را انتخاب کنید:

```dotenv
# Gemini
NOTE_API_PROVIDER=gemini
NOTE_API_KEY=...
NOTE_API_MODEL=gemini-2.5-flash-lite
```

```dotenv
# OpenAI یا هر API سازگار با /chat/completions
NOTE_API_PROVIDER=openai_compatible
NOTE_API_KEY=...
NOTE_API_BASE_URL=https://api.openai.com/v1
NOTE_API_MODEL=gpt-4o-mini
```

```dotenv
# Anthropic
NOTE_API_PROVIDER=anthropic
NOTE_API_KEY=...
NOTE_API_MODEL=claude-3-5-haiku-latest
```

برای OpenRouter، Groq، Together، DeepSeek، Ollama یا vLLM فقط Base URL و Model را عوض کنید. اگر از هدر سفارشی زیر systemd استفاده می‌کنید، JSON را کامل داخل تک‌کوتیشن قرار دهید:

```dotenv
NOTE_API_EXTRA_HEADERS_JSON='{"HTTP-Referer":"https://example.com","X-Title":"Gamas Bot"}'
```

خطاهای شبکه و پاسخ‌های `429` و `5xx` با Backoff محدود Retry می‌شوند. با وجود شکست API ساخت جزوه، متن استخراج‌شده از بین نمی‌رود و به‌عنوان خروجی جایگزین ارسال می‌شود.

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

لاگ‌ها شامل شماره Job، زمان سرویس‌های STT، وضعیت و Request ID سرویس ساخت جزوه، زمان و Exit Code ابزارهای خارجی، Migrationهای دیتابیس و Stack Trace هستند. بدنهٔ پاسخ خطای سرویس‌ها لاگ نمی‌شود؛ خطای STT با نوع خطا یا وضعیت HTTP ثبت می‌شود. کلید Gemini در Header ارسال می‌شود، نه Query نشانی. کلید API، Prompt و متن جزوه عمداً لاگ نمی‌شوند؛ با این حال دسترسی به لاگ‌ها و فایل‌های داده را محدود نگه دارید.

در خطای پردازش، کاربر کدی مانند `GMS-000123` دریافت می‌کند. این کد همان Submission ID قابل جست‌وجو در لاگ است:

```bash
sudo journalctl -u gamas-bot --since today | grep 'GMS-000123'
```

ترتیب پیشنهادی عیب‌یابی:

1. `systemctl status gamas-bot -l`
2. `journalctl -u gamas-bot -n 200 --no-pager`
3. بررسی مالکیت `.env` و پوشه `data`
4. بررسی مسیرهای `ffmpeg`، `ffprobe` و `soffice`
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

روی SQLite و یک Session تلگرام، بیش از یک Replica هم‌زمان اجرا نکنید.


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

FFmpeg و FFprobe فقط از Protocolهای محلی `file` و `pipe` استفاده می‌کنند تا Playlist ارسالی نشانی شبکه را واکشی نکند. این محدودیت Sandbox کامل نیست؛ دسترسی به فایل‌های محلی همچنان تابع مجوز کاربر سرویس است. بسته‌های سیستم را به‌روز نگه دارید و محدودیت دیسک، حافظه و CPU و جداسازی فایل‌سیستم را اعمال کنید. در Linux، Timeout و Cancel گروه پردازش ابزار را هم متوقف می‌کند؛ در Windows برای پاک‌سازی فرایندهای فرزند از Supervisor مناسب استفاده کنید.

Workflow نامرتبط RDP حذف شده، چون گذرواژهٔ ثابت مدیر و تنظیم غیرفعال‌سازی NLA داشت. گذرواژه در تاریخچهٔ Git باقی است؛ اگر جایی دوباره استفاده شده آن را عوض کنید، Runnerهای قدیمی را متوقف کنید و دسترسی Tailscale آن‌ها را بررسی کنید. GitHub Actions جای هاست دائمی ربات نیست.

## آزمون قبل از استقرار

از ریشهٔ پروژه و در محیط مجازی:

```bash
python -m unittest discover -s tests -v
python -m pip check
```

آزمون‌های شبکهٔ Telegram و APIهای پولی Mock هستند و کلید لازم ندارند. سه آزمون واقعی FFmpeg با نصب ابزار یا تنظیم `FFMPEG_TEST_BIN` اجرا می‌شوند؛ در نبود ابزار Skip می‌شوند. CI لینوکسی Pythonهای ۳.۱۱، ۳.۱۲ و ۳.۱۳ را بررسی می‌کند؛ اجرای واقعی Windows و LibreOffice باید روی هاست مقصد تأیید شود.

گزارش بازبینی و محدودیت‌های تأیید: [AUDIT.md](AUDIT.md).
