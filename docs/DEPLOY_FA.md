# راه‌اندازی سریع روی cPanel

راهنمای کامل انگلیسی: [DEPLOY_CPANEL.md](DEPLOY_CPANEL.md). برای اجرای محلی در ویندوز،
[README](../README.md) را ببینید. پوشهٔ پروژه، `.env`، پایگاه‌داده، نشست تلگرام و رسیدها را
**بیرون از `public_html`** نگه دارید.

## پیش‌نیاز

cPanel باید Terminal/SSH، Python 3.11 یا جدیدتر، Cron Jobs و خروجی TCP 443 به تلگرام و
سرویس‌های API بدهد. اگر هاست پردازش طولانی را متوقف می‌کند، VPS لازم است.

## نصب

```bash
cd "$HOME"
git clone https://github.com/AmiraliGhamkhar/Gamas_bot.git gamas_bot
cd "$HOME/gamas_bot"
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
nano .env
```

در `.env` مقدارهای `TELEGRAM_BOT_TOKEN`، `TELEGRAM_API_ID`، `TELEGRAM_API_HASH`، شناسهٔ عددی
مدیرها در `ADMIN_IDS` و دست‌کم یک کلید STT را وارد کنید. برای هاست اشتراکی:

```dotenv
MAX_ACTIVE_JOBS=1
MAX_QUEUED_JOBS=4
MAX_ACTIVE_JOBS_PER_USER=1
MEDIA_WORKER_CONCURRENCY=1
OFFICE_ENABLED=true
OFFICE_MAX_INPUT_BYTES=1000000
OFFICE_MAX_CHARS=100000
OFFICE_MAX_SLIDES=30
OFFICE_JOB_CONCURRENCY=2
```

## آزمایش

```bash
python -m scripts.cpanel_preflight
python -m gamas_bot --check
python -m gamas_bot
```

`FAIL`ها را رفع کنید و با `Ctrl+C` اجرای آزمایشی را ببندید.

## اجرای خودکار

در **cPanel → Cron Jobs** این دستور را هر دقیقه اجرا کنید و `USER` را جایگزین کنید:

```text
* * * * * /home/USER/gamas_bot/.venv/bin/python /home/USER/gamas_bot/scripts/ensure_running.py >/dev/null 2>&1
```

گزارش‌ها در `data/logs/bot.log` و `data/logs/launcher.err` هستند. پیش از به‌روزرسانی، کران را
موقتاً غیرفعال کنید، ربات را متوقف کنید، از `.env`، `data/gamas_v3.db` (و فایل‌های `-wal`/`-shm`)
و `data/receipts` نسخهٔ پشتیبان بگیرید، سپس `git pull`، نصب `requirements.txt`، اجرای
`python -m scripts.cpanel_preflight` و `python -m gamas_bot --check` را انجام دهید. بررسی `--check`
آفلاین است و پایگاه‌داده را تغییر نمی‌دهد؛ مهاجرت‌های خودکار هنگام راه‌اندازی اجرا می‌شوند. پیش از
ارتقا نسخهٔ پشتیبان SQLite بگیرید؛ migration 016 فقط جدول موجود کتابخانه را برای ذخیرهٔ ارجاع
فایل‌های Office بازسازی می‌کند و ردیف‌ها را نگه می‌دارد. در پایان کران را فعال کنید یا در Passenger
از `touch tmp/restart.txt` استفاده کنید. گزینهٔ
**🗂 دفتر آزاد** به‌طور پیش‌فرض فعال است؛ پیام یا فایل‌های `.txt`، `.md`، `.markdown`، `.csv` و
`.tsv` را به Word/Excel/PowerPoint/PDF تبدیل می‌کند و از اعتبار کاربر کم نمی‌کند. بدون کلید یادداشت
نیز خروجی‌های ایمنِ پایه ساخته می‌شوند؛ هزینهٔ احتمالی ارائه‌دهنده فقط هزینهٔ عملیاتی مدیر است.
پوشهٔ `TEMP_DIR` باید خصوصی و بیرون از `public_html` باشد. جزئیات سقف‌ها و آزمون را در
[Free Office](OFFICE.md) ببینید.
