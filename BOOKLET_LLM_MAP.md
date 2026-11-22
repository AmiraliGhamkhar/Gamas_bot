# نقشهٔ کامل فایل‌های «تولید جزوه» و «LLM» در Gamas Bot

> بررسی روی شاخهٔ `arena/39673769-gamas-bot` (Base: `a11d553`) انجام شده است.
> این سند فقط **فهرست و تحلیل** است؛ هیچ فایلی از کد تغییر نکرده است.
> آمار خطوط: `wc -l` روی درخت فعلی؛ آمار تست: اجرای واقعی `pytest` (پایین سند).

---

## ۱) تصویر کلی: مسیر جزوه از متن تا فایل

```
منبع (صدا/ویدیو/PDF/DOCX/PPTX/متن خام)
   ↓  stt.py / transcript.py / doc_extract.py / presentations.py / office_notes.py
NormalizedDocument (document_model.py) + ContentPlan (note_planning.py)
   ↓  semantic_segments.py  →  واحدهای نقشه‌برداری
SectionWriter / _structure_piece  ← notes.py (پایپ‌لاین map/reduce)
   ↓  llm.complete_json()  ← llm.py + llm_profiles.py + llm_quota.py + capacity.py
StructuredNotes  (notes.py)
   ↓  gate_notes + audit_notes  ← notes_evidence.py
   ↓  verify_and_repair  ← booklet_verification.py   (اختیاری: booklet_synthesis.py)
   ↓
رندر: docx_export.py (Word) | notes_typst.py + notes_pdf.py + pdf_common.py (PDF)
   ↓  document/* (طرح، اجزا، دارایی‌ها، RTL، QA) + artifact_validation.py
   ↓  bot_jobs._deliver_booklet → db_documents/db_library (ذخیرهٔ بایت‌ها)
   ↓  اختیاری: review_sheet.py + review_sheet_html.py (جزوهٔ مرور HTML)
```

پرامپت‌های فارسی در **یک** ماژول متمرکز شده‌اند: `gamas_bot/_prompt_block.py`.

---

## ۲) هستهٔ LLM (۵ فایل + ۱ فایل کمکی)

| فایل | خط | نقش |
|---|---|---|
| `gamas_bot/llm.py` | 3179 | تنها کلاینت LLM: انتخاب مسیر، دیالکت‌ها (OpenAI/Gemini)، SSE، failover، hedge، بودجهٔ ورودی/خروجی، `complete_json`، `complete`، `get_json` |
| `gamas_bot/llm_profiles.py` | 644 | پروفایل قابلیت هر provider/model (Gemini 3.5 Flash-Lite، DeepSeek V4.1 Flash روی AvalAI، Nemotron روی OpenRouter)، تبدیل JSON-Schema به schema نیتیو |
| `gamas_bot/llm_quota.py` | 219 | دفتر سهمیهٔ SQLite (RPM/TPM/RPD/`max_in_flight`) با `BEGIN IMMEDIATE` برای چند پردازه |
| `gamas_bot/capacity.py` | 261 | `CapacityPool` غیرمسدودکننده + `CooldownRegistry` برای پرش سریع به مسیر سالم بعدی |
| `gamas_bot/http_util.py` | 110 | خواندن بدنهٔ خطا، `Retry-After`، پاک‌سازی متن خطا |
| `gamas_bot/logging_config.py` | 428 | `collect_configured_secrets` / `redact_log_secrets` (کلیدها هرگز در لاگ نمی‌روند) |
| `gamas_bot/protocols.py` | 334 | `LLMProviderDialect` و `LLMProviderProfileProtocol` (قرارداد قابل تعویض) |
| `gamas_bot/metrics.py` | 658 | `record_llm_operation` برای مشاهده‌پذیری provider/مدل/توکن |

**API عمومی `llm.py`:** `complete_json` (مصرف اصلی)، `complete`، `get_json`، `booklet_input_budget`، `register_llm_dialect`، `get_dialect`، `reset_cooldowns`، کلاس‌های خطا (`LLMError`، `LLMTransientError`، `LLMAuthError`، `LLMResponseError`، `LLMContextOverflowError`، `LLMOutputBudgetError`).

**اولویت وظیفه‌ای (`TASK_PROVIDER_ORDER` در `llm.py`):** جزوه → `avalai,gemini,openrouter`؛ کوییز/فلش‌کارت/Office/مرور کوتاه → `gemini,avalai,openrouter`؛ OpenRouter همیشه آخر.

---

## ۳) هستهٔ تولید جزوه (۹ فایل)

| فایل | خط | نقش |
|---|---|---|
| `gamas_bot/notes.py` | 4166 | مدل `StructuredNotes`/`NoteSection`/`NoteTable`/…، مرج قطعی، `SectionWriter`، `_structure_piece`، گیت‌های کیفیت (`gate_notes`, `acceptance_failure`)، بازیابی fallback منبع‌محور، `structure_document` / `structure_transcript` / `structure_presentation` |
| `gamas_bot/_prompt_block.py` | 654 | **تنها** محل پرامپت‌های فارسی: قواعد عامل جزوه، `build_chunk_message`، `edit_system_prompt`، `COMPILE_SYSTEM_PROMPT`، `section_writer_system_prompt`، `COVERAGE_FILL_RULES` (پیشوند بایت‌ثابت برای کش provider) |
| `gamas_bot/notes_evidence.py` | 1628 | شاهد قطعی: استخراج سیگنال (عدد/واحد/تاریخ/اصطلاح لاتین/URL)، `audit_notes`، `prose_claim_evidence`، `validate_table`، `validate_tip`، `sentence_coverage`، `classify_claim_risk` |
| `gamas_bot/note_planning.py` | 338 | برنامه‌ریزی استخراجی موضوع‌ها؛ `maybe_refine_plan` با LLM فقط برای منابع بزرگتر از ۲۵۰k کاراکتر و تنها با `NOTE_LLM_PLANNER=true` |
| `gamas_bot/semantic_segments.py` | 169 | `SemanticUnit` / `SemanticSource` / `build_semantic_topics` (بازه‌های دقیق منبع، بدون بازنویسی) |
| `gamas_bot/document_model.py` | 1133 | `NormalizedDocument`، `SourceBlock`، `ContentPlan`، `PlanTopic`، پرووننس |
| `gamas_bot/booklet_verification.py` | 1004 | `verify_booklet` (بررسی بدون مدل) + `verify_and_repair` (حداکثر ۲–۳ درخواست ترمیم هدفمند) |
| `gamas_bot/booklet_synthesis.py` | 455 | ویرایش فقط‌ساختاری (opt-in با `NOTE_COMPILE`)؛ هرگز بدنهٔ واقعی را بازنویسی نمی‌کند |
| `gamas_bot/transcript.py` | 1141 | متن خام → پاراگراف/چانک پایدار؛ همان چیزی که به LLM می‌رود |

---

## ۴) رندر و طرح جزوه (۱۱ فایل)

| فایل | خط | نقش |
|---|---|---|
| `gamas_bot/docx_export.py` | 3263 | Word: جلد، فهرست لینک‌دار، هدر/فوتر، RTL، جداول؛ همچنین `build_plain_docx` |
| `gamas_bot/notes_typst.py` | 1188 | موتور اصلی PDF (Typst، HarfBuzz نیتیو RTL، TOC واقعی) |
| `gamas_bot/notes_pdf.py` | 1065 | انتخاب موتور `typst`/`reportlab` + رندر ReportLab (fallback) |
| `gamas_bot/pdf_common.py` | — | فونت Vazirmatn، شکل‌دهی فارسی، بوم مشترک PDF‌ها |
| `gamas_bot/document/theme.py` | 318 | همهٔ توکن‌های طراحی (رنگ، تایپ، فاصله، هندسه) |
| `gamas_bot/document/components.py` | 159 | واژگان معنایی (کارت تعریف/هشدار/نکتهٔ کلیدی/…) |
| `gamas_bot/document/assets.py` | 312 | ثبت دارایی‌ها از `assets/booklet/manifest.json` |
| `gamas_bot/document/layout.py` | 73 | هندسهٔ A4 مشترک Word/PDF |
| `gamas_bot/document/rtl.py` | 166 | شکل‌دهی و ترتیب نمایش BiDi |
| `gamas_bot/document/typography.py` | 64 | نقش‌های فونت و مقیاس اندازه |
| `gamas_bot/document/qa.py` | 319 | بازرسی پس از رندر (PDF/DOCX/دارایی‌ها) |
| `gamas_bot/artifact_validation.py` | 470 | اعتبارسنجی بایت‌های PDF/DOCX و HTML مرور/کوییز |

دارایی‌ها: `assets/booklet/` (۲.۲MB، ۲۰ فایل: جلد موضوعی، آیکون، مونوگرام، ECG)، `assets/fonts/Vazirmatn-*.ttf`، `assets/review_sheet/skeleton.html`.

---

## ۵) آداپتورها و مصرف‌کنندگان LLM

| فایل | خط | نقش |
|---|---|---|
| `gamas_bot/office_notes.py` | 768 | جزوهٔ کامل از سند آپلودی + «مرور سریع» با پرامپت/اسکیمای مستقل (`COMPACT_NOTES`) |
| `gamas_bot/office_ai.py` | 513 | تنها کار LLM در Office: چیدمان اسلاید و ساخت جدول (`OFFICE_SLIDE_PLANNING`, `OFFICE_TABLE_GENERATION`) |
| `gamas_bot/office.py` | 1346 | بخش «آفیس رایگان» (Word/Excel/PowerPoint بدون مدل) |
| `gamas_bot/learning.py` | 933 | پرامپت/اسکیما/اعتبارسنجی کوییز و فلش‌کارت از متن جزوه |
| `gamas_bot/learning_html.py` | 1021 | رندر HTML آفلاین کوییز/فلش‌کارت |
| `gamas_bot/review_sheet.py` | 804 | برنامهٔ مرور زمان‌بندی‌شده؛ تولیدی از `render_booklet_review` (استخراجی) و `plan_review_sheet`/`generate_review_sheet` (با مدل) |
| `gamas_bot/review_sheet_html.py` | 371 | رندر قطعی HTML مرور از روی skeleton |
| `gamas_bot/doc_extract.py` | 709 | PDF/DOCX/PPTX → بخش‌های متنی با مکان‌نما (بدون LLM) |
| `gamas_bot/presentations.py` | 1552 | استخراج صدای ارائه و متن اسلایدها (ورودی جزوه) |
| `gamas_bot/artifacts.py` | 253 | صفحهٔ نتایج و دکمه‌های دریافت فایل |
| `gamas_bot/titles.py` | 170 | عنوان کوتاه برای کتابخانه/دکمه‌ها |
| `gamas_bot/texts.py` | 930 | پیام‌های فارسی مرتبط با جزوه (۳۶ ارجاع «جزوه») |
| `gamas_bot/bot_jobs.py` | 2098 | `_generate_notes`, `_generate_presentation_notes`, `_deliver_booklet`, `_fetch_booklet`, `_fetch_persisted_booklet` |
| `gamas_bot/bot_documents.py` | 2201 | `_run_document_notes`، `_run_office_session_booklet` |
| `gamas_bot/bot_learning.py` | 1613 | `complete_json` برای کوییز/فلش‌کارت (۳ نقطهٔ فراخوانی) |
| `gamas_bot/bot_deps.py` | 157 | تزریق `structure_transcript` و `complete_json` (DIP) |
| `gamas_bot/bot.py` | 2104 | ارکستراتور تلگرام + re-export در `__all__` |
| `gamas_bot/intake.py` / `jobs.py` | 447/392 | حالت‌های شغلی: `generating_notes`، `validating_notes`، `compiling_notes` |

---

## ۶) پیکربندی، ذخیره‌سازی، مهاجرت‌ها

- `gamas_bot/config.py` (1954 خط): ۴۲ فیلد `note_*`/`llm_*`؛ ثابت‌های provider (`APPROVED_LLM_PROVIDERS`, `NOTE_REQUEST_STYLE`, `DEFAULT_NOTE_MODELS`, `DEFAULT_NOTE_*`, `TASK_*`)، `NoteRoute`، `note_routes()`, `check()`.
- `.env.example` (خط ۱۱۹–۲۱۲): همهٔ `NOTE_*`, `LLM_*`, `GEMINI_*`, `AVALAI_*`, `OPENROUTER_*` (کامنت‌شده با مقدار پیش‌فرض).
- `docs/CONFIGURATION.md` (۹۰–۱۱۲) و `docs/PROVIDERS.md`: مستند مرجع همان مقادیر.
- ذخیره‌سازی: `db_documents.py` (`submission_binary_artifacts`)، `db_library.py` (kind=`booklet`)، `db_learning.py`، `db_reporting.py`.
- مهاجرت‌های مرتبط: `027_persisted_booklet_files`, `030_booklet_delivery_tracking`, `033_review_html_artifacts`, `034_remove_leaderboard_and_studio_remnants`, `035_html_learning_and_delivered_library`, `010_library`, `012_learning`, `014_free_use_recovery`, `016_office_library_kind`.

---

## ۷) اسکریپت‌های اپراتوری

| فایل | خط | کار |
|---|---|---|
| `scripts/booklet_regression_bench.py` | 271 | مقایسهٔ دو چک‌اوت با provider ساختگی |
| `scripts/render_booklet_samples.py` | 371 | رندر سه نمونهٔ A/B/C به DOCX/PDF/PNG برای QA |
| `scripts/probe_note_providers.py` | 434 | پروب سه‌مرحله‌ای امن providerها |
| `scripts/smoke_avalai.py` | 341 | اسموک واقعی مسیر AvalAI |
| `scripts/document_chunk_bench.py` | 36 | سنجش چانک‌بندی سند |
| `scripts/build_booklet_assets.py` | 604 | تولید قطعی دارایی‌های `assets/booklet` |
| `scripts/plan_catalog_preview.py` | 102 | پیش‌نمایش کاتالوگ پلن (بیلینگ) |

---

## ۸) تست‌ها (۲۷ فایل، ≈۱۱٬۶۰۰ خط)

| فایل | خط | موضوع |
|---|---|---|
| `test_notes.py` | 1935 | مدل یادداشت، مرج قطعی، پایپ‌لاین map/reduce |
| `test_llm.py` | 1785 | کلاینت LLM: مسیرها، خطاها، SSE، JSON |
| `test_provider_contracts_and_quality.py` | 828 | قرارداد providerها + کیفیت |
| `test_docx_export.py` | 776 | رندر Word |
| `test_review_sheet.py` | 748 | جزوهٔ مرور HTML |
| `test_scientific_booklet_overhaul.py` | 664 | بازطراحی علمی جزوه |
| `test_llm_perf.py` | 663 | کارایی/پایداری ترنسپورت |
| `test_note_evidence.py` | 613 | لایهٔ شاهد قطعی + corpus |
| `test_note_provider_compatibility.py` | 540 | سازگاری مدل‌های ۲۰۲۶-۱۱-۰۷ |
| `test_office_flow.py` (۵۱۵)، `test_pdf_visual_qa.py` (۴۵۷)، `test_note_acceptance_and_prompt_safety.py` (۴۳۴)، `test_document_model.py` (۳۹۹)، `test_booklet_rendering.py` (۳۴۴)، `test_booklet_recovery.py` (۳۰۸)، `test_booklet_verification.py` (۲۷۴)، `test_content_plan.py` (۲۵۵)، `test_booklet_synthesis.py` (۲۲۵)، `test_semantic_segments.py` (۲۰۱)، `test_booklet_contract.py` (۱۹۹)، `test_llm_quota.py` (۱۹۶)، `test_booklet_design.py` (۱۸۹)، `test_section_writer.py` (۱۷۲)، `test_office_notes.py` (153)، `test_booklet_pipeline.py` (۱۴۶)، `test_provider_allowlist.py` (۱۰۲)، `test_notes_pdf.py` (۸۲)، `test_booklet_rollout.py` (۶۳)، `test_llm_task_routing.py` (۵۷)، `test_artifact_validation.py` (۲۰) |
| `tests/fixtures/notes/` | ۶ سند + ۶ مرجع JSON | corpus ارزیابی (پزشکی، دانشگاه، CS، ادبیات فارسی، code-switch، PPTX) |

---

## ۹) مستندات

`docs/PROVIDERS.md` (۲۳۸: مسیرها، اولویت وظیفه، سهمیه، پذیرش جزوه، پروب امن)، `docs/BOOKLET_DESIGN.md` (۱۶۴)، `docs/CONFIGURATION.md` (۱۵۹)، `docs/FEATURES.md`، `docs/OPERATIONS.md`، `docs/CPANEL.md`، `docs/DEVELOPMENT.md`، `docs/CHANGELOG.md`، و `AUDIT/PHASE_0_PIPELINE.md` / `AUDIT/architecture.md` (تاریخی).

---

## ۱۰) نتیجهٔ بررسی — نکات قابل توجه

### الف) کد بی‌مصرف در مسیر تولید — ✅ پاک‌سازی شد
1. ~~`notes.build_compile_document`~~ — حذف شد (بدون هیچ ارجاعی).
2. ~~`notes.completeness_failure`, `notes.repair_target_indexes`, `notes._repair_issues_for_pass`~~ — حذف شدند.
3. ~~`_notes_draft_json`, `MAX_REPAIR_SECTIONS`, `REPAIR_STRUCTURAL_CODES`~~ — همراه آن‌ها یتیم شدند و حذف شدند.
4. ~~`REPAIR_STRUCTURE`, `REPAIR_DUPLICATE`, `REPAIR_FORMAT`~~ — دیگر هیچ‌جا تولید نمی‌شدند (کدهای غیرقابل‌دسترس) و حذف شدند.
5. `llm.complete()` — در production فراخوانی ندارد (فقط تست) و **عمداً نگه داشته شد**: تست‌های failover به آن وابسته‌اند.
6. `llm.get_json()` — تنها در تست استفاده می‌شود (اسکریپت پروب پیاده‌سازی خودش را دارد).
7. `review_sheet.generate_review_sheet()` — production از مسیر استخراجی `render_booklet_review` استفاده می‌کند؛ نسخهٔ LLM فقط در تست فراخوانی می‌شود.
8. `TASK_PROVIDER_ORDER["OFFICE_DOCUMENT_GENERATION"]` و `["KEY_POINTS"]` — هیچ فراخوانی با این `task` وجود ندارد.
9. `notes_evidence.advice_sentences`, `notes.generate_section`, `booklet_verification.verify_booklet`، `llm_profiles.model_supports_system_prompt` — فقط از تست‌ها استفاده می‌شوند (API عمومی آگاهانه).

### ب) ناسازگاری مستند/کد — ✅ رفع شد
- **`config.py` / `note_completeness_fill`:** کامنت می‌گفت «در production پیش‌فرض فعال است»، ولی کد و `.env.example` و `docs/CONFIGURATION.md` همه `false` هستند. کامنت با واقعیت هم‌تراز شد.
- **`AUDIT/PHASE_0_PIPELINE.md`:** ارجاع به ۱۰ نماد حذف‌شده/ناموجود (`_audit_and_repair`, `_compile_pass`, `_compile_once`, `_compile_in_groups`, `_gate_compiled`, `_repair_once`, `repair_request_document`, `merge_repaired_sections`, `completeness_failure`, `build_compile_document`) با نام‌های واقعی جایگزین شد.
- **باگ واقعی (کشف‌شده در همین بررسی):** `intake.FAILURE_KIND_LABELS_FA` کلید `"notes"` را **دو بار** داشت؛ کلید دوم برنده می‌شد و برچسب مرحله («ساخت جزوه / فایل Word») بی‌صدا حذف می‌شد. این باعث **شکست `ruff check .` در CI** روی `main` بود. کلید تکراری حذف شد؛ `ruff check .` اکنون سبز است.
- **باقی‌مانده (عمداً دست‌نخورده):** دو مهاجرت با پیشوند `016_` (`016_office_library_kind.sql`, `016_subscription_enforcement.sql`) و سه re-export آگاهانه در `notes.py` (`SOURCE_DIGEST_HEADING`, `edit_system_prompt`, `build_presentation_system_prompt`) و بزرگ‌بودن `notes.py`؛ هر سه تغییر ساختاری می‌خواهند و در این مرحلهٔ کم‌ریسک انجام نشدند.

### ج) نقاط قوت معماری (تأییدشده در کد)
- پرامپت‌ها تک‌منبع و بایت‌ثابت‌اند (کش پیشوند provider) — `_prompt_block.py` هیچ آرگومان متغیر نمی‌گیرد.
- جداسازی «تولید» از «رندر»: مدل هرگز رنگ/فونت/چیدمان تعیین نمی‌کند.
- گیت شاهد قطعی (`notes_evidence`) مستقل از مدل است و omission و fabrication را جدا می‌سنجد.
- failover کران‌دار با سهمیهٔ SQLite، بدون retry بی‌پایان و بدون ارسال مجدد 4xx دائمی.
- سقف تعداد درخواست نقشه (`MAX_NOTE_CHUNKS = 200`, `MAX_CONTEXT_SPLITS = 2`) و fallback منبع‌محور (`deterministic_fallback_notes`) به‌جای حذف بی‌صدا.

---

## ۱۱) آنچه در این بررسی اجرا شد

گیت کامل پروژه روی همین شاخه، پس از پاک‌سازی:

```
python -m compileall -q gamas_bot scripts tests   → موفق
ruff check .                                      → All checks passed  (روی main شکست می‌خورد)
python -m mypy (۸۰ ماژول)                          → فقط ۲ خطای از پیش موجود metrics.py (windll ویندوز)
python -m pytest -q                               → 2016 passed, 521 subtests
python -m unittest discover -s tests -q           → 1898 tests OK
pip check / sync_requirements --check / media_worker check → همه سالم
```

- بدون `pymupdf` (اختیاری و مخصوص dev) ۱۳ تست رندر/QA شکست می‌خورند؛ CI آن را نصب می‌کند.
- هیچ فایل تستی برای پوشش کدهای حذف‌شده لازم نبود؛ هیچ رفتاری تغییر نکرد (فقط کد مرده و یک کلید تکراری dict).
