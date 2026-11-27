# بازبینی کامل مینی‌اپ گاماس — ۱۴۰۵/۰۹/۰۶ (۲۰۲۶-۱۱-۲۷)

دامنهٔ بازبینی: تمام فایل‌های `miniapp/` (۲۰ فایل سورس)، `gamas_bot/webapp_api.py`،
`gamas_bot/telegram_auth.py`، لایه‌های دادهٔ درگیر (`db_review.py`، `db_study_packs.py`،
`db_users.py`)، `gamas_bot/explore_recommender.py`، `passenger_wsgi.py`،
`docs/DEPLOY_MINIAPP.md` و تست‌های `tests/test_webapp_api.py` + `miniapp/src/test/`.

وضعیت مکانیکی: ✅ `tsc -b` بدون خطا — ✅ ۹ تست فرانت‌اند پاس — ✅ ۵ تست webapp_api پاس —
✅ ۱۴۵ تست مرتبط (webapp/auth/rewards/review/explore) پاس — ✅ منطق تاریخ جلالی روی ۸ نمونهٔ
مرزی (نوروز ۱۴۰۰/۱۴۰۳/۱۴۰۵، سال کبیسه ۱۴۰۳) تأیید شد.

---

## ۱) مشکلات مهم

### ۱.۱ تبدیل امتیاز (Redeem): محافظت idempotency عملاً کار نمی‌کند
`miniapp/src/features/account/AccountTab.tsx:91-115` + `miniapp/src/api.ts:237-243`

- دکمهٔ «تبدیل» هیچ guard در حال پرواز (in-flight) ندارد؛ فقط بر اساس `affordable` غیرفعال
  می‌شود. دوبار تپ سریع، دو درخواست مستقل می‌فرستد.
- کلید idempotency داخل خودِ `redeem()` و **در هر فراخوانی** ساخته می‌شود
  (`crypto.randomUUID()`). یعنی دو تپ = دو کلید متفاوت = **دو بار خرج امتیاز** روی سرور.
- کامنت `api.ts` ادعا می‌کند «a retry (double tap, flaky network) returns the original
  result»، ولی هیچ retry یا نگهداری کلید وجود ندارد؛ پس این محافظت فقط روی کاغذ است.

**پیشنهاد:** کلید idempotency را per-redemption (در state) نگه دارید و تا رسیدن پاسخ، دکمه
را `disabled` کنید؛ برای retry همان کلید قبلی را دوباره بفرستید.

### ۱.۲ پنجرهٔ ۲۵۰تایی recommender، سؤال‌های تازه را نامرئی می‌کند
`gamas_bot/explore_recommender.py:104-108`

```sql
WHERE status = 'approved' AND question_id > ? ORDER BY question_id ASC LIMIT 250
```
همیشه `cursor=0` پاس می‌شود (`webapp_api.py:773-778`)؛ یعنی فقط **قدیمی‌ترین ۲۵۰ سؤال
approved** کاندیدای فید هستند. بانک seed همین حالا ۲۰۰ سؤال دارد؛ به‌محض این‌که مجموع
سؤال‌های approved (seed + اشتراک‌گذاری کاربران از بخش مرور) از ۲۵۰ بگذرد، همهٔ سؤال‌های
جدیدتر — که دقیقاً همان اشتراک‌گذاری‌های کاربران هستند — برای همیشه از فید خارج می‌شوند.
هرچه پلتفرم رشد کند، فید بیشتر به سؤال‌های تکراریِ دیده‌شده محدود می‌شود.

**پیشنهاد:** پنجره را روی سؤال‌های دیده‌نشده ببندید (مثلاً `NOT IN (seen)` در خود کوئری یا
پیمایش صفحه‌ای با cursor واقعی)، یا candidate pool را از وسط بازهٔ کل approved نمونه‌برداری کنید.

### ۱.۳ ثبت دوبارهٔ تعامل (impression) برای یک سؤال در یک‌سؤال
`miniapp/src/features/explore/ExploreTab.tsx:76-88` + `webapp_api.py:815-872`

- پاسخ دادن (`/api/v1/explore/answers`) خودش یک ردیف `explore_impressions` می‌نویسد (signal=3).
- سپس دکمهٔ «سؤال بعدی» → `advance()` → `recordEngagement()` → یک POST دیگر به
  `/api/v1/explore/engagement` که **ردیف دوم** برای همان سؤال می‌سازد (signal=2/1/0).
- نتیجه: تاریخچهٔ impressions آلوده می‌شود و `compute_topic_affinities` یک سؤال را دو بار
  وزن می‌دهد.

**پیشنهاد:** بعد از ثبت پاسخ، engagement ارسال نشود (در `advance` فقط برای سؤال‌های
پاسخ‌داده‌نشده)، یا سرور از upsert per (user, question) استفاده کند.

### ۱.۴ دانلود فایل‌ها بدون پسوند ذخیره می‌شود
`miniapp/src/features/library/LibraryTab.tsx:131` (`saveBlob(blob, asset.title)`)

عنوان دارایی‌ها در دیتابیس برچسب فارسی بدون پسوند است («جزوهٔ PDF»، «متن خام درس» —
`bot_jobs.py:272-299`). چون `saveBlob` نامِ `a.download` را از `asset.title` می‌سازد و
هدر `Content-Disposition` سرور (که نام سالمِ فایل دارد) نادیده گرفته می‌شود، فایل ذخیره‌شده
پسوند ندارد و در اندروید/دسکتاپ با هیچ برنامه‌ای باز نمی‌شود.

**پیشنهاد:** نام فایل را از `mime_type`/`kind` با پسوند بسازید (مثلاً `جزوهٔ PDF.pdf`) یا
`Content-Disposition` پاسخ را بخوانید.

---

## ۲) مشکلات متوسط

### ۲.۱ پیام خطای شبکه به انگلیسی خام نمایش داده می‌شود
`miniapp/src/lib/useAsync.ts:62-70`

وقتی `fetch` به‌دلیل قطعی شبکه/فیلتر شکست بخورد، `TypeError("Failed to fetch")` از
`describeError` همان‌طور رد می‌شود و در `ErrorState` (مثلاً صفحهٔ «گاماس باز نشد» در
`App.tsx`) متن انگلیسی خام به کاربر نشان داده می‌شود. کاربر فارسی‌زبان باید «اتصال به
سرور برقرار نشد» ببیند.

**پیشنهاد:** در `describeError` برای خطاهای غیر `ApiError` (به‌خصوص TypeError مربوط به
fetch) یک پیام فارسی ثابت برگردانید.

### ۲.۲ پرش سریع سؤال در یک‌سؤال ثبت نمی‌شود
`miniapp/src/features/explore/ExploreTab.tsx:51,76-80`

`recordEngagement` فقط وقتی `dwell ≥ 800ms` بود impression می‌فرستد. کاربری که سؤالی را
سریع «رد کند» هیچ ردی به‌جا نمی‌گذارد و همان سؤال در «سؤال‌های تازه» بعدی دوباره برمی‌گردد —
برخلاف ادعای کامنت «Seen questions are filtered server-side». اگر skip هم مثل پاسخ همیشه
impression ثبت کند (با dwell واقعی)، مشکل حل می‌شود.

### ۲.۳ `webapp_api.py` — ورودی `dwell_ms` می‌تواند 500 بدهد، و bool از int رد می‌شود
`gamas_bot/webapp_api.py:785-786 و 819-820`

- `int(body.get("dwell_ms") or 0)` قبل از اعتبارسنجی اجرا می‌شود؛ مقدار رشته‌ای مثل
  `"abc"` باعث `ValueError` و پاسخ 500 به‌جای 400 می‌شود (بدون try/except).
- `isinstance(qid, int)` برای `bool` مقدار `True` برمی‌گرداند (در پایتون `bool` زیرکلاس
  `int` است)؛ endpoint پاسخ مرور (`review/answers`) این را جداً چک می‌کند
  (`isinstance(raw_qid, bool)`) ولی endpointهای explore نه — ناسازگاری.

**پیشنهاد:** تبدیل‌ها را داخل try ببرید و bool را در همهٔ ورودی‌ها رد کنید.

### ۲.۴ سقف ۱۰۰ سؤال برای هر گروه مرور، بدون صفحه‌بندی
`miniapp/src/api.ts:264-268` + `ReviewTab.tsx`

`getReviewQuestions` فقط `limit=100` می‌گیرد و UI هیچ «موارد بیشتر» ندارد؛ گروهی با بیش از
۱۰۰ سؤال ناقص نمایش داده می‌شود (شمارندهٔ «X از Y پاسخ داده شده» هم فقط روی همان ۱۰۰ عدد
حساب می‌کند). همین‌طور فهرست گروه‌ها روی ۵۰ می‌ماند (`getReviewGroups(50)`) در حالی که
سرور `offset` را پشتیبانی می‌کند.

### ۲.۵ ناسازگاری برچسب منبع در مرور (گروه‌های legacy)
`miniapp/src/features/review/ReviewTab.tsx:27-36`

`SOURCE_LABELS` در ReviewTab نسبت به LibraryTab ناقص است: `booklet`، `pptx`،
`presentation`، `upload` ترجمه ندارند. برای گروه‌های legacy، `source_type` همان
`library_items.kind` است (مثل `booklet`) و کاربر متن خام «booklet» می‌بیند.

### ۲.۶ `TOPIC_LABELS` در یک‌سؤال dead code است
`miniapp/src/features/explore/ExploreTab.tsx:15-37`

کلیدهای نقشه انگلیسی‌اند (`biology`, `chemistry`, …) ولی `explore_questions.topic` در عمل
همیشه فارسی است: بانک seed از `explore_bank.TOPICS` («زیست‌شناسی»، «بدن انسان»، …) استفاده
می‌کند و اشتراک‌گذاری‌ها topic را از عنوان بسته پر می‌کنند (`db_review.py:397-398`). خوشبختانه
fallback یعنی نمایش خودِ topic درست از آب درمی‌آید، ولی نقشه هرگز match نمی‌شود و گمراه‌کننده
است. (تست `app.test.tsx` هم topic انگلیسی `biology` را mock می‌کند که با واقعیت فرق دارد.)

---

## ۳) نکات جزئی

| # | موضوع | مکان |
| --- | --- | --- |
| ۳.۱ | بنر «در حال پردازش» فقط برای `processing` نشان داده می‌شود؛ برای `pending`/`queued`/`running` فقط polling هست و صفحه بی‌پیام می‌ماند (chip وضعیت کمک می‌کند). | `LibraryTab.tsx` (PackDetail) |
| ۳.۲ | خطای اشتراک‌گذاری (`shareError`) با موفقیتِ پاسخ‌دادن پاک نمی‌شود؛ یادداشت خطا می‌تواند بعد از پاسخ موفق بماند (state مشترک برای دو خطا). | `ReviewTab.tsx:86,102` |
| ۳.۳ | در race بین دو دستگاه، اگر سؤالی قبلاً پاسخ داشته باشد، `chosen_index` تپِ جدید به‌جای پاسخ ذخیره‌شده highlight می‌شود (سرور `chosen_index` ذخیره‌شده را برنمی‌گرداند). | `ReviewTab.tsx` + `db_review.review_answer` |
| ۳.۴ | `saveBlob` در WebViewهای iOS ممکن است به‌جای دانلود، فایل را باز کند (`a.download` نادیده گرفته می‌شود)؛ fallback ندارد. | `api.ts:280-297` |
| ۳.۵ | عمر نشست `initData` حداکثر ۲۴ ساعت است؛ بعد از آن کاربرِ در حال استفاده پیام ۴۰۱ می‌گیرد و باید مینی‌اپ را ببندد/باز کند (refresh خودکار نداریم). | `telegram_auth.py:57` |
| ۳.۶ | `window.open` fallback در `openTelegramLink` برای کلاینت‌های بدون `openTelegramLink` ممکن است در WebView بلاک شود. | `telegram.ts` |
| ۳.۷ | تست redeem با idempotency، تست دانلود فایل، و تست retry بعد از خطا پوشش داده نشده‌اند. | `miniapp/src/test/` |

---

## ۴) چیزهایی که سالم و درست کار می‌کنند (بررسی شد)

- **قرارداد API فرانت‌اند ↔ بک‌اند کاملاً منطبق است** — هر ۱۹ مسیرِ `api.ts` با روت‌های
  `webapp_api.py` و شکل پاسخ‌ها (Me, Subscription, Plan, Plans, Payments, Referrals,
  Catalog, Redeem, Library, PackDetail, Review Groups/Questions/Answers/Share,
  Explore Feed/Answers/Engagement) یک‌به‌یک تطبیق داده شد.
- **احراز هویت**: اعتبارسنجی HMAC-SHA256 با `compare_digest`، بررسی `auth_date`، عدم اعتماد
  به `user_id` کلاینت (`telegram_auth.py`).
- **امنیت استاتیک**: جلوگیری از path traversal با `is_relative_to`، کش درست برای
  `index.html` (no-store) و assets هش‌دار immutable.
- **تاریخ جلالی `format.ts`**: الگوریتم jalaali-js بدون کتابخانه؛ روی نوروزها و سال کبیسه
  ۱۴۰۳ تست شد — همه درست. `parseDate` هر دو فرمت `iso()` و SQLite را درست UTC می‌بیند.
- **RTL و ارقام فارسی**: `toFa` سراسری، جهت‌ها، فونت وزیرمتن، safe-area و کیبورد امن.
- **دیزاین‌سیستم**: lint سفارشی (`design_lint.test.ts`) جلوی hardcode رنگ و افکت‌های ممنوعه
  را می‌گیرد؛ مطابق `DESIGN.md`.
- **لینک‌های deep-link ربات**: `?start=buy`، `?start=support` و `ref_<id>` در `bot.py:1766`
  مدیریت می‌شوند؛ `bot_username` از سرور می‌آید و در UI چیزی hardcode نشده.
- **مقاومت در برابر خطا**: `useAsync` با جلوگیری از overwrite شدن پاسخ‌های کهنه، حالت‌های
  loading/empty/error صادقانه، `StrictMode`-safe، و بدون نشت state بین سؤال‌ها (key روی
  question_id).
- **idempotency سمت سرور برای امتیاز یک‌سؤال**: `INSERT OR IGNORE` روی
  `reward_points_ledger` با `ref_id=explore_q:<id>` — امتیاز هر سؤال فقط یک بار.
- **`passenger_wsgi.py`** با مستندات `DEPLOY_MINIAPP.md` سازگار است (یک اپ WSGI، بدون سرور
  Node جدا).

---

## ۵) جمع‌بندی اولویت‌ها

| اولویت | مورد |
| --- | --- |
| 🔴 بالا | ۱.۱ دوبار خرج شدن امتیاز در Redeem (guard + کلید ثابت) |
| 🔴 بالا | ۱.۲ نامرئی شدن سؤال‌های تازه با عبور از ۲۵۰ سؤال approved |
| 🟠 متوسط | ۱.۳ ثبت دوبارهٔ impression بعد از پاسخ |
| 🟠 متوسط | ۱.۴ دانلود بدون پسوند فایل |
| 🟠 متوسط | ۲.۱ پیام خطای شبکه به انگلیسی |
| 🟡 کم | ۲.۲ تا ۲.۶ و موارد جزئی بخش ۳ |

---

## ضمیمه — اصلاحات اعمال‌شده (۱۴۰۵/۰۹/۰۶)

| مورد | اصلاح | فایل |
| --- | --- | --- |
| ۱.۱ Redeem | یک درخواست در هر لحظه (دکمه در پرواز غیرفعال) + کلید idempotency پایدار per-attempt که در retry همان کلید را می‌فرستد؛ تست رگرسیون اضافه شد | `AccountTab.tsx`، `miniapp/src/test/redeem.test.tsx` |
| ۱.۲ پنجرهٔ recommender | حذف سؤال‌های دیده‌شده در خود SQL (به‌جای post-filter روی پنجرهٔ ۲۵۰تایی) + سقف ۵۰۰۰ فقط به‌عنوان محافظ؛ تست رگرسیون اضافه شد | `explore_recommender.py`، `tests/test_explore_seed_bank.py` |
| ۱.۳ impression دوباره | بعد از ثبت پاسخ، engagement دیگر ارسال نمی‌شود | `ExploreTab.tsx` |
| ۱.۴ نام فایل دانلود | خواندن `Content-Disposition` سرور + fallback با پسوند از `mime_type` | `api.ts`، `LibraryTab.tsx` |
| ۲.۱ پیام خطای شبکه | همهٔ catchها به `describeError` وصل شدند؛ خطای fetch و رشته‌های رایج API فارسی می‌شوند؛ تست اضافه شد | `useAsync.ts` + چهار تب |
| ۲.۲ skip سریع | هر skip بدون درنظرگرفتن dwell ثبت می‌شود تا سؤال دیده‌شده برنگردد | `ExploreTab.tsx` |
| ۲.۳ ورودی dwell_ms | تبدیل‌های unsafe در try + رد کردن bool/float → ۴۰۰ به‌جای ۵۰۰؛ تست اضافه شد | `webapp_api.py`، `tests/test_explore_and_rewards.py` |
| ۲.۴ سقف سؤال | سقف بارگذاری گروه/سؤال به ۲۰۰ + یادداشت «N سؤال دیگر بارگذاری نشده» | `api.ts`، `ReviewTab.tsx` |
| ۲.۵ برچسب منبع | `SOURCE_LABELS` مرور با LibraryTab یکدست شد (booklet/pptx/…) | `ReviewTab.tsx` |
| ۲.۶ topic بلند | truncate روی چیپ موضوع برای topicهای بلند (عنوان بسته) | `ExploreTab.tsx` |
| ۳.۲ یادداشت خطای کهنه | پیام خطا با پاسخ موفق بعدی پاک می‌شود (در `answer`/`share`) | `ReviewTab.tsx` |
| WebView | fallback بارگذاری SDK تلگرام از `/app/telegram-web-app.js` (telegram.org در برخی شبکه‌ها بسته است) + تشخیص «داخل کلاینت اما SDK بارگذاری نشده» با پیام و دکمهٔ تلاش مجدد + 404 واقعی برای فایل‌های missing (نه پوستهٔ HTML) | `index.html`، `telegram.ts`، `App.tsx`، `webapp_api.py`، `DEPLOY_MINIAPP.md` §۴.۱ |

**تأیید end-to-end:** سرور WSGI با توپولوژی production (SPA + API روی یک origin) با
`initData` امضاشدهٔ HMAC بالا آمد و **۳۲/۳۲ بررسی اتصال** پاس شد: health، سرو SPA و
assets با کش درست، 401 برای initData نامعتبر/منقضی/غایب، قرارداد کامل ۱۹ مسیر `api.ts`،
هدر `Content-Disposition` در دانلود، شکل فید explore بدون کلید پاسخ، امتیازدهی یک‌بار،
بازپخش idempotent در redeem، و جداسازی مالکیت بین کاربران.

**وضعیت تست‌ها:** ۱۲ تست فرانت‌اند (۳ جدید) + ۲۲۰۰ تست پایتون پاس. ۱۴ شکست موجود در
`test_pdf_visual_qa.py` / `test_scientific_booklet_overhaul.py` قبل از این تغییرات هم
(روی commit پایه) وجود داشتند و مربوط به استخراج متن PDF فارسی در محیط اجرا هستند —
نه مینی‌اپ.
