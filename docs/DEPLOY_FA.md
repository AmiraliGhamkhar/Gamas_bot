# راهنمای استقرار Gamas Bot

ربات تلگرام است، نه وب‌سایت: یک اتصال دائمی خروجی به تلگرام دارد و هیچ صفحه یا
API سرو نمی‌کند. این راهنما اجرای آن را روی سرور لینوکسی با `systemd` پوشش
می‌دهد؛ برای هاست اشتراکی cPanel فایل
[DEPLOY_CPANEL.md](DEPLOY_CPANEL.md) را ببینید.

## پیش‌نیازها

| مورد | مقدار |
| --- | --- |
| پایتون | **۳.۱۱ تا ۳.۱۳** |
| سیستم‌عامل | glibc ۲.۲۸ به بالا (Ubuntu 20.04+، Debian 11+، AlmaLinux/Rocky 8+) |
| دسترسی خروجی | TCP 443 به تلگرام و سرویس‌های STT و ساخت جزوه |
| منابع | ۱ تا ۲ گیگ رم، چند گیگ دیسک (هر آپلود تا ۲ گیگ فضای موقت) |

هیچ ابزار سیستمی لازم نیست: نه `ffmpeg`، نه `ffprobe`، نه `soffice`، نه Redis،
نه Docker و نه پایگاه‌دادهٔ بیرونی. پردازش رسانه با PyAV در یک فرایند جدا انجام
می‌شود.

## ۱. نصب ابزارهای سیستم

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv git
```

## ۲. ساخت کاربر سرویس و نصب پروژه

```bash
sudo useradd --system --create-home --home-dir /opt/gamas-bot --shell /usr/sbin/nologin bot
sudo mkdir -p /opt/gamas-bot/data
sudo git clone https://github.com/AmiraliGhamkhar/Gamas_bot /opt/gamas-bot/repo
cd /opt/gamas-bot/repo
sudo -u bot python3.11 -m venv .venv
sudo -u bot .venv/bin/pip install --upgrade pip
sudo -u bot .venv/bin/pip install -r requirements.txt
```

## ۳. پیکربندی

```bash
cd /opt/gamas-bot/repo
sudo -u bot cp .env.example .env
sudo -u bot chmod 600 .env
sudo -u bot nano .env
```

حداقل مقدارهای لازم:

```dotenv
TELEGRAM_BOT_TOKEN=...
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
ADMIN_IDS=123456789

# حداقل یک موتور تبدیل گفتار:
SPEECHMATICS_API_KEY=...
# DEEPGRAM_API_KEY=...

# اختیاری — بدون آن فقط متن خام تحویل می‌شود.
# ترتیب، همان ترتیب failover است:
NARA_API_KEY=...
OPENROUTER_API_KEY=...
```

بقیهٔ متغیرها پیش‌فرض کاری دارند؛ فهرست کامل در
[CONFIGURATION.md](CONFIGURATION.md) و `.env.example`.

**کلیدها فقط از محیط خوانده می‌شوند.** هیچ کلیدی در پایگاه‌داده ذخیره نمی‌شود و
هیچ خزانهٔ رمزگذاری‌شده‌ای وجود ندارد.

## ۴. بررسی پیش از اجرا

```bash
cd /opt/gamas-bot/repo
sudo -u bot .venv/bin/python -m scripts.cpanel_preflight   # آمادگی میزبان و شبکه
sudo -u bot .venv/bin/python -m gamas_bot --check          # پیکربندی (بدون اتصال)
```

هر `FAIL` را برطرف کنید. `WARN` مانع اجرا نیست.

## ۵. اجرای آزمایشی

```bash
sudo -u bot /opt/gamas-bot/repo/.venv/bin/python -m gamas_bot
```

به ربات پیام بدهید، سپس با `Ctrl+C` متوقف کنید. خاموش‌شدن graceful است: کارهای
در حال اجرا «قطع‌شده» ثبت و اعتبارشان آزاد می‌شود.

## ۶. راه‌اندازی systemd

فایل `deploy/gamas-bot.service` آماده است. مسیرها را با نصب خودتان تطبیق دهید:

```bash
sudo cp deploy/gamas-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gamas-bot
journalctl -u gamas-bot -f
```

ویژگی‌های unit:

* `User=bot` و `WorkingDirectory=/opt/gamas-bot`
* `EnvironmentFile=/opt/gamas-bot/.env`
* `Restart=always` با `RestartSec=5`
* `UMask=0077`، `ProtectSystem=strict`، `ProtectHome=true`،
  `ReadWritePaths=/opt/gamas-bot/data`
* `KillSignal=SIGINT` تا خاموش‌شدن graceful بماند
* خروجی به journald می‌رود؛ اگر فایل چرخشی می‌خواهید `LOG_FILE` را در `.env`
  تنظیم کنید

اگر `WorkingDirectory` را عوض کردید، `HOME` و `XDG_CACHE_HOME` را هم عوض کنید تا
کش پایتون در مسیر قابل‌نوشت بماند.

## پردازش رسانه چه می‌کند؟

`media.py` اسکریپت `media_worker.py` را به‌صورت فرایند فرزند اجرا می‌کند
(بردار آرگومان ثابت، `shell=False`، زمان‌بند سخت). دو فایدهٔ واقعی دارد: خرابی
دکدر نمی‌تواند ربات را بیندازد، و عملیات گیرکرده را می‌توان کشت.

| عملیات | کاربرد |
| --- | --- |
| `probe` | مدت و وجود شاخهٔ صوتی — صورتحساب به مدت وابسته است |
| `extract` | استخراج صوت از ویدیو |
| `merge` | چسباندن کلیپ‌های صوتی اسلایدها |
| `convert` | تبدیل `.ppt` قدیمی به `.pptx`، درون‌فرایندی |

## تنظیم سرویس ساخت جزوه

```dotenv
#NOTE_PROVIDERS=nara,openrouter

# نارا (NaraRouter) — سرویس نخست
#NARA_API_KEY=
#NARA_BASE_URL=https://router.bynara.id/v1
#NARA_MODEL=agnes-2.5-flash

# OpenRouter — سرویس دوم
#OPENROUTER_API_KEY=
#OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
#OPENROUTER_MODEL=google/gemma-4-31b-it:free

# Gemini
#GEMINI_API_KEY=

# Anthropic (مدل پیش‌فرض ندارد؛ NOTE_MODEL اجباری است)
#NOTE_PROVIDER=anthropic
#NOTE_API_KEY=
#NOTE_MODEL=claude-...
```

* `NOTE_PROVIDERS` همان سیاست failover است. یک سرویس فقط وقتی امتحان می‌شود که
  کلید **خودش** را داشته باشد.
* خطاهای موقت (429/5xx/شبکه) با بودجهٔ محدود تلاش، سپس سرویس بعدی.
* ورودی بزرگ‌تر از پنجرهٔ زمینه دو نیم می‌شود؛ پاسخی که به سقف توکن خروجی
  برخورده دوباره فرستاده نمی‌شود و `NOTE_MAX_OUTPUT_TOKENS` را باید بالا برد.

جزئیات کامل: [PROVIDERS.md](PROVIDERS.md).

## موتورهای STT

```dotenv
#STT_PROVIDERS=speechmatics,deepgram,openai_compatible
#STT_LANGUAGE=fa

#SPEECHMATICS_API_KEY=
#SPEECHMATICS_MODEL=enhanced

#DEEPGRAM_API_KEY=
#DEEPGRAM_MODEL=nova-3      # فارسی فقط روی nova-3

#STT_OPENAI_BASE_URL=https://api.groq.com/openai/v1
#STT_OPENAI_API_KEY=
#STT_OPENAI_MODEL=whisper-large-v3
```

فارسی روی `nova-2` پشتیبانی نمی‌شود و درخواست با HTTP 400 رد می‌شود؛ `--check`
این جفت‌نشدن را هشدار می‌دهد. جزئیات: [STT_PROVIDERS.md](STT_PROVIDERS.md).

## لاگ و پیگیری خطا

```bash
journalctl -u gamas-bot -f
journalctl -u gamas-bot --since "1 hour ago" | grep GMS-000123
```

هر کار با شناسهٔ `GMS-#####` در لاگ قابل پیگیری است. هرگز کلید، متن پیاده‌شده یا
محتوای رسید را در گزارش خطا نگذارید؛ به‌جای آن `request_id` سرویس را نقل کنید.

## به‌روزرسانی و پشتیبان‌گیری

```bash
cd /opt/gamas-bot/repo
sudo -u bot git pull
sudo -u bot .venv/bin/pip install -r requirements.txt
sudo -u bot .venv/bin/python -m gamas_bot --check
sudo systemctl restart gamas-bot
```

مهاجرت‌های `migrations/v3/` هنگام شروع اجرا به‌طور خودکار اعمال می‌شوند.

این‌ها را با هم پشتیبان بگیرید:

* `/opt/gamas-bot/data/gamas_v3.db` و فایل‌های `-wal` و `-shm` — یا یک نسخهٔ
  سازگار با `sqlite3 … ".backup backup.db"`
* فایل نشست تلگرام (`data/gamas.session`)
* `.env`
* `data/receipts/` اگر رسیدها باید بمانند

پشتیبان‌ها را رمز کنید و دسترسی‌شان را محدود نگه دارید؛ هرگز در ریپازیتوری Git
نگذارید.

## اعتبار و پرداخت

پرداخت کارت‌به‌کارت و تأیید دستی است: مدیر باید رسید را در پنل
**🧾 پرداخت‌ها** ببیند و تأیید یا رد کند. بارگذاری رسید هیچ اعتباری نمی‌دهد.

اعتبار پیش از کار رزرو و فقط پس از تحویل تسویه می‌شود؛ هر مسیر شکست یا لغو آن را
دقیقاً یک بار آزاد می‌کند. جزئیات: [BILLING.md](BILLING.md).

## رفتار محدودیت‌ها

`MAX_ACTIVE_JOBS` کار فعال و `MAX_QUEUED_JOBS` کار منتظر را محدود می‌کند.
فراتر از آن، آپلود با پیام صریح رد می‌شود تا صف در حافظه بزرگ نشود.

## عیب‌یابی

| نشانه | علت احتمالی |
| --- | --- |
| سرویس بالا می‌آید و می‌افتد | `journalctl -u gamas-bot -n 50` — معمولاً توکن یا API id/hash اشتباه |
| ربات پاسخ نمی‌دهد | فرایند دومی همان نشست را قفل کرده؛ کد خروج `3` یعنی قفل |
| همهٔ آپلودها رد می‌شوند | `df -h` و `df -i` و مجوز `data/tmp` |
| فقط متن خام می‌رسد | خط‌های `note provider failed` در لاگ |

فهرست کامل: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
