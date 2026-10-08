# Gamas AI Provider Platform

این سند مرجع پلتفرم چندارائه‌دهندهٔ تولید جزوه است: معماری، وضعیت رایگان‌ها،
تنظیمات، مسیریابی/failover، پنل مدیریت، راه‌اندازی و محدودیت‌های شناخته‌شده.

> همهٔ مقادیر عددیِ سهمیه‌ای که در این فایل می‌بینید **«last verified»** هستند و
> با زمان ممکن است عوض شوند. منبع صحیح همیشه مستندات رسمی ارائه‌دهنده است؛
> ستون «Docs URL» هر ارائه‌دهنده در پنل ربات نیز همین لینک را نشان می‌دهد.

## قانون‌های سخت (غیر قابل مذاکره)

1. **FREE_ONLY پیش‌فرض روشن است** (`AI_FREE_ONLY=true`). در این حالت هیچ
   ترافیکی به مدل/ارائه‌دهندهٔ پولی نمی‌رود؛ اگر مسیر رایگان نباشد، «بلاک»
   ثبت و خطای خوانا به کاربر داده می‌شود.
2. **Fallback پولی فقط opt-in** (`AI_ALLOW_PAID_FALLBACK=false` پیش‌فرض) و
   همیشه **آخرِ** زنجیره است؛ ترتیب مسیر نمی‌تواند آن را جلوتر بیاندازد.
3. هیچ ترافیک عادی‌ای به ارائه‌دهنده‌های trial-only/region-restricted/
   commercial-prohibited نمی‌رود؛ این‌ها stateهای صریح‌اند که مدیر باید به‌
   آگاهی باز کند (قفل آزمایشی).
4. API keyها، هدرهای Authorization، متن سخنرانی، system prompt و خروجی خام
   مدل‌ها **هرگز** در لاگ، پنل، پیام کاربر یا فایل جزوه ظاهر نمی‌شوند.
5. مدل‌های منسوخ/منقضی‌شده خودکار از مسیر حذف می‌شوند؛ «بازنشانی سهمیه» برای
   رفع قفل روزانه وجود دارد ولی خطای ۵xx پشت‌سرهم بدون توقف ادامه نمی‌یابد.

## ارائه‌دهنده‌ها و وضعیت رایگان (handbook، «آخرین بررسی ۲۰۲۶‑۱۰»)

| slug | نام | کلاس | آیا در FREE_ONLY؟ | سهمیهٔ حدودی (last verified) | Trial/منطقه |
|-----|-----|------|------------------|------------------------------|--------------|
| `gemini` | Google Gemini (AI Studio) | رایگان با پلن | ✅ | ۲۵۰ req/day (15 RPM, flash-lite متفاوت) | — |
| `nara` | NaraRouter | رایگان با پلن | ✅ | مدیریت‌شده توسط Nara (هر کلید) | — |
| `groq` | Groq | رایگان با پلن | ✅ | ~1k req/day، 30 RPM، 8k/16k TPM | — |
| `openrouter` | OpenRouter | رایگان با پلن | ✅ | ۵۰ req/day برای مدل‌های `:free` | — |
| `mistral` | Mistral La Plateforme | رایگان با پلن | ✅ | experiment tier (RPS کم) | — |
| `sambanova` | SambaNova Cloud | رایگان با پلن | ✅ | free tier محدود | — |
| `zai` | z.ai GLM | تبلیغاتی | ✅ | اعتبار رایگان موقت + مدل‌های flash رایگان | اعتبار منقضی می‌شود |
| `nvidia` | NVIDIA NIM | تبلیغاتی | ✅ | اعتبار اولیهٔ رایگان (۱۰۰۰ token/req محدود) | منقضی می‌شود |
| `cloudflare` | Cloudflare Workers AI | رایگان دائمی | ✅ | ۱۰۰۰۰ neuron/day | نیاز به Account ID |
| `huggingface` | HF Inference Providers | رایگان با پلن | ✅ | اعتبار ماهانهٔ کوچک | — |
| `alibaba` | Alibaba Model Studio | محدودیت منطقه | ❌ (با بازکردن قفل + fallback پولی) | quota ناحیه‌ای | region-restricted |
| `cohere` | Cohere | trial-only | ❌ | Trial key محدود | تولید تجاری ممنوع |
| `cerebras` | Cerebras | نامعتبر/آزمایشی | ❌ | — | وضعیت نامشخص |
| `openai_compatible` | درگاه سازگار با OpenAI | عمومی | با fallback | بسته به درگاه | — |
| `anthropic` | Anthropic | فقط پولی | ❌ (با fallback) | — | — |

ارائه‌دهنده‌های `gemini`، `nara`، `groq`، `openrouter` و... در registry با
`Adapters` اختصاصی پیاده‌سازی شده‌اند؛ بقیه روی `OpenAICompatAdapter` سوارند.
پروتکل‌های `gemini_native`، `gemini_openai_compatible`، `anthropic` و
`openai_compatible` پشتیبانی می‌شوند.

## معماری به‌اختصار

```
gamas_bot/ai/
  registry.py   – ProviderInfo ثابت: کلاس، پروتکل، URL مستندات/قیمت، سیاست داده،
                  محدودیت منطقه، aliasها (نرمال‌سازی slug از hostname/base URL)
  models.py     – ModelRegistry: کش مدل‌ها در DB + STATIC_SEEDS + free-status
  schema.py     – نکتهٔ واحد ساخت Note schema + نسخهٔ سازگار با Gemini
  adapters.py   – NoteAdapter + زیرکلاس‌ها; build()/parse()/map_error()
                  دسته‌بندی خطاها: auth/quota/rate/billing/model/content-blocked…
  profiles.py   – RequestPolicy: بودجهٔ توکن چانک، retry، timeout، reasoning…
  tokens.py     – تخمین توکن و تبدیل بودجهٔ توکن→نویسه برای chunker
  routing.py    – ProviderRouter: plan(FREE_FIRST→PAID_LAST)+RouteLeg+NoteJobSession
  usage.py      – AIUsageTracker: ai_usage_records/daily/quota snapshots/events
  sync.py       – کشف از endpointهای «models» + به‌روزرسانی کش (TTL-guarded)
  admin_ai.py   – پنل ادمین تلگرام (admin:ai:*)
```

جریان داده: `bot._process_job` → `NoteJobSession` → `structure_*` →
`_structured_notes_for/_structure_chunk` → اگر routing فعال باشد از طریق
`ProviderRouter`، وگرنه مسیر legacy دقیقاً مثل قبل (byte-compatible).

## استراتژی JSON capability-aware

به‌ترتیب اولویت: `strict_json_schema` → `json_schema` → `gemini_schema`
(responseSchema native) → `gemini_mime` (responseMimeType) → `json_object`
(= NOTE_API_JSON_MODE قدیمی) → `prompt`. انتخاب بر اساس capabilityهای
مدل کش‌شده است و روی HTTP 400 «response_format unsupported» فقط **یک‌بار**
به prompt JSON تنزل می‌یابد و سپس failover طبیعی ادامه می‌یابد.

## تنظیمات

| متغیر | پیش‌فرض | معنا |
|------|--------|------|
| `AI_FREE_ONLY` | `true` | تولید فقط روی لایهٔ رایگان |
| `AI_ALLOW_PAID_FALLBACK` | `false` | fallback پولی opt-in (پایان زنجیره) |
| `AI_ROUTING_ENABLED` | `true` | موتور مسیریاب جدید؛ با `false` رفتار کاملاً قدیمی |
| `AI_PROVIDER_SYNC_TTL` | `86400` | TTL کشف مدل (ثانیه) |
| `AI_DEFAULT_NOTE_ROUTE` | `""` | مسیر پیش‌فرض جدای از seed env (فرمت slug1,slug2,...) |
| `AI_MAX_PROVIDER_FAILOVERS` | `3` | سقف hop بین ارائه‌دهنده‌ها در یک تلاش |
| `AI_MAX_GENERATION_RETRIES` | `-1` (auto) | سقف retry داخل هر ارائه‌دهنده |
| `AI_QUOTA_SAFETY_MARGIN` | `0.15` | حاشیهٔ امن شمارش روزانه (۰ تا ۰٫۴۹) |
| `CLOUDFLARE_ACCOUNT_ID` | – | برای ساخت base URL Workers AI |

سازگاری: `NOTE_API_*`/`GEMINI_API_KEY` به‌همان معنای قبل؛ seed مسیر با
ارائه‌دهندهٔ env اول ساخته می‌شود، بنابراین deployment فعلی NaraRouter بدون هیچ
تغییری کار می‌کند (آزمایش `LegacyPayloadFreezeTests::test_nararouter_shape_preserved`).

## مهاجرت‌ها

`migrations/006_ai_provider_platform.sql` (forward-only و idempotent):
جداول `ai_models`، `ai_provider_settings`، `ai_provider_routes`،
`ai_usage_records`، `ai_usage_daily`، `ai_quota_snapshots`، `ai_events` و
افزودن ستون‌های `key_type/free_only_mode/failure_streak/last_error_class`
به `provider_credentials`. اعمال خودکار در `Database.open`.

## پنل مدیریت تلگرام

`⚙️ پنل مدیریت → 🤖 پلتفرم AI`:

* **ارائه‌دهنده‌ها**: کارت هر ارائه‌دهنده با کلاس (🟢🟡🟠🔴⛔)، سیاست داده،
  لینک Docs/Pricing (نمایش URL به مدیر مجاز است)، فعال/غیرفعال، بلاک
  رایگان‌سازی، قفل آزمایشی، تعداد کلید.
* **کلیدها (راهنما)**: انتخاب ارائه‌دهنده → مدل (دکمه/سفارشی) → برچسب →
  کلید (پیام حذف می‌شود) → ذخیرهٔ رمزنگاری‌شده → تست. کارت کلید:
  فعال/غیرفعال، reorder، حذف، تست مستقیم.
* **مسیرها**: ترتیب failover با ▲▼، فعال/غیرفعال، ❌، ➕ افزودن پایانی.
* **مصرف**: مجموع ۷روز/ارائه‌دهنده + «بازنشانی شمارندهٔ امروز» (reset quota).
* **مدل‌ها**: کشف/کش هر ارائه‌دهنده + علامت‌گذاری «منسوخ».
* **لاگ‌ها**: رویدادهای ساختاری (kind/provider/model/http/latency/error_class)
  بدون secret و بدون محتوا.
* **Dry-run**: نمایش ساختار درخواست (آدرس، استراتژی JSON، تخمین توکن)
  **بدون ارسال** — با کلید جایگذاری؛ بدون محتوای خام.
* **نمای مسیر و بودجه**: chain مؤثر با بودجهٔ توکن/نویسه هر leg.

## راه‌اندازی از صفر (جزوه)

۱) env پایه را بچینید (هیچ کلید note لازم نیست اگر از پنل اضافه می‌کنید)
۲) در تلگرام: پنل مدیریت → پلتفرم AI → ارائه‌دهندهٔ موردنظر → «افزودن کلید»
۳) اگر `AI_FREE_ONLY=true` است کافی‌است یک کلید `gemini`/`nara`/`groq`/...
   بگذارید؛ با ۲ تا ۳ کلید، failover رایگان پوشش داده می‌شود.
۴) `python -m gamas_bot --check` و پنل‌های «وضعیت سرویس‌ها»/«نمای مسیر و بودجه» را برای راستی‌آزمایی ببینید.

## Health/Probe

بررسی‌های جدید adapter-driven هستند: هر adapter `discovery_request()` ارائه
می‌دهد (ارزان‌ترین read-only endpoint؛ Cloudflare با `CLOUDFLARE_ACCOUNT_ID`
بدون hardcode). `check_type` = `read_only|generation` در رویدادها ذخیره
می‌شود؛ تست مستقیم کلید از پنل همان `🧪 تست` است.

## لاگ/رویداد

هر 429/5xx با provider/model/status/attempt/latency/retry-delay/route-position/
credential-label ثبت می‌شود. کلیدها در `sanitize_text` (substrings ≥8 نویسه)
حذف می‌شوند؛ جزوهٔ نهایی هرگز شامل دیباگ/آیدی/خطای ارائه‌دهنده نیست.

## محدودیت‌های شناخته‌شده

1. **وضعیت رایگان ثابت است** (registry last-reviewed): تغییرات روز ارائه‌دهنده
   تا بعد از بروزرسانی registry در پنل «pending verification» دیده می‌شود.
2. سهمیه‌های دقیق بعضی ارائه‌دهنده‌ها (mistral, sambanova, nvidia) فقط از
   هدر زمان‌واقعی خوانده می‌شوند؛ بدون پاسخ در دسترس «حدس» ثبت نمی‌کنیم.
3. `AI_ALLOW_PAID_FALLBACK=true` فقط opt-in مدیر است؛ به‌طور خودکار به انتهای زنجیره اضافه نمی‌شود و هرگز خودبه‌خود روشن نمی‌شود.
4. `AI_MAX_PROVIDER_FAILOVERS` hop سخت است؛ بیشتر از آن خطای خوانا به جای
   ping-pong برگردانده می‌شود.
5. Sync مدل‌ها نیاز به HTTP از سرور دارد؛ در cPanel بدون شبکهٔ بیرونی،
   کش ثابت (static seeds) استفاده می‌شود.
6. Anthropic «به‌عنوان» ارائه‌دهندهٔ اصلی route نمی‌شود (فقط پولی) مگر با
   fallback — فعلاً برای پارس/سازگاری باقی‌مانده.

## دستورهای راستی‌آزمایی

```bash
.venv/bin/python -m pytest tests/test_ai_platform.py tests/test_provider_adapters.py \
    tests/test_ai_schema_compat.py tests/test_admin_ai.py -q
.venv/bin/python scripts/benchmark_notes.py --mode summary --router
```

هرگز کلید واقعی در گزارش/لاگ/PR قرار نمی‌گیرد.
