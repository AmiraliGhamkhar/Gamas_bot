# راه‌اندازی کوتاه روی cPanel

این راهنما برای هاست اشتراکی Linux است. برای نصب محلی با Windows PowerShell، [README](../README.md) را ببینید. پوشهٔ پروژه، `.env`، پایگاه‌داده، نشست تلگرام و رسیدها را **بیرون از `public_html`** نگه دارید.

## پیش‌نیاز

در cPanel باید Terminal/SSH، Python 3.11 یا جدیدتر، Cron Jobs، فضای دیسک کافی و دسترسی خروجی TCP 443 به تلگرام و سرویس‌های API داشته باشید. هاست‌های اشتراکی ممکن است پردازش طولانی را متوقف کنند؛ اگر آزمون میزبان ناموفق بود، VPS لازم است.

## نصب و تنظیم

در Terminal سی‌پنل:

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

در `.env` مقدارهای `TELEGRAM_BOT_TOKEN`، `TELEGRAM_API_ID`، `TELEGRAM_API_HASH`، شناسهٔ عددی مدیرها در `ADMIN_IDS` و دست‌کم یک کلید STT را وارد کنید. کلید سرویس ساخت جزوه اختیاری است.

```bash
python -m scripts.cpanel_preflight
python -m gamas_bot --check
python -m gamas_bot
```

`FAIL`ها را رفع کنید، سپس با `Ctrl+C` اجرای آزمایشی را متوقف کنید. برای هاست اشتراکی با `MAX_ACTIVE_JOBS=1` و صف کوچک شروع کنید؛ سقف آپلود و فایل موقت را متناسب با فضای هاست تنظیم کنید.

## اجرای خودکار

در **cPanel → Cron Jobs** این دستور را هر دقیقه اجرا کنید و `USER` را با نام کاربری هاست جایگزین کنید:

```text
* * * * * /home/USER/gamas_bot/.venv/bin/python /home/USER/gamas_bot/scripts/ensure_running.py >/dev/null 2>&1
```

اگر از **Setup Python App** استفاده می‌کنید، مسیر Python همان virtualenv را بنویسید. گزارش‌ها در `data/logs/bot.log` و `data/logs/launcher.err` هستند.

پیش از به‌روزرسانی، ربات را متوقف و از `.env`، پایگاه‌داده، نشست تلگرام و رسیدها نسخهٔ پشتیبان بگیرید. سپس `git pull`، نصب مجدد `requirements.txt`، اجرای `python -m gamas_bot --check` و راه‌اندازی دوباره را انجام دهید. جزئیات در [راهنمای انگلیسی cPanel](DEPLOY_CPANEL.md) است.
