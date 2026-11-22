# Phase 0 — Gamas document pipeline audit

Baseline: `e2148f4dd097783b313685dbda205354ff7d0068` on `arena/44809b9d-gamas-bot`. Read-only with respect to implementation and tests. Defaults are from `config.from_env`, not comments, `.env.example`, or a production deployment. Provider capabilities below are repository declarations, not live upstream measurements.

## A. Current architecture and execution paths

| Entry point | Execution path |
|---|---|
| Voice/audio/video | `bot.py:_handle_message` → `intake.py:_media_metadata` → `bot_jobs.py:_accept_media` → admission/queue (`_create_admission_submission`, `_queue_admitted_submission`, `jobs.py`) → `_run_job` → `_process_media` → bounded Telegram download, reserve measured audio, `_prepare_audio` via media worker/PyAV → `stt.py:transcribe` → `transcript.py:build_document` → persist `document.plain()` and optional speaker turns → settle hold → `_generate_notes(document.llm_document())` → `structure_transcript` → `structure_document` → `_deliver_booklet`. With no note route, audio retains raw output. |
| Text-layer PDF / DOCX | `_handle_message` → `_document_metadata` → `bot_documents.py:_accept_document` / `_accept_document_inner` → `_ingest_document` → `doc_extract.extract_document`: `_read_pdf_pages` / `_pdf_sections_from_pages` or `_docx_sections` → `ExtractedDoc.text`, `chunk_sections` → `_save_document` (text and locator-tagged `chunks_json`, source identity/digest) + Library record → explicit callback `_route_document_callback` → `_run_document_notes` → cached/rebuilt chunks via Office adapters → `office_notes.generate_full_booklet` → `notes.structure_document(source_locators=True)` or deterministic Office baseline; compact mode instead uses `generate_compact_notes` → render. Ingestion itself makes no LLM call. |
| PPT/PPTX as document | Same document-first dispatch. Legacy `.ppt` conversion via `presentations.convert_to_pptx`; PPTX `_pptx_sections` uses `presentations._extract_slide_text` and `sections_from_slides`. The document path captures slide text/notes, not embedded narration. |
| PPT/PPTX as lecture | Explicit presentation offer/action (`_start_document_presentation`, `_start_presentation_from_message`) or presentation-metadata dispatch → `_accept_presentation` → `_process_presentation` → conversion if legacy → `load_presentation` / `slides_outline` / `_store_presentation_document` → prepare embedded audio and optionally `transcribe` → align timed words to slides or keep unassigned narration → store full raw result and skip notices → settle hold → `build_presentation_document` → `_generate_presentation_notes` → `structure_presentation` → shared `structure_document` → `_deliver_booklet`. The ordinary upload classifier tries document before presentation; do not collapse the two user workflows. |
| Plain text | `/booklet` inline/pending text or `_looks_like_raw_study_text` dispatch → `_accept_raw_text` (display normalization, admission, size rejection) → `_process_raw_text` → `RawTranscript.from_text` / `build_document` → `_generate_notes` → save text + notes JSON → `_deliver_booklet`. Unlike audio/presentation, the first persisted transcript write here follows the awaited generation call. Office text sessions are distinct: `_office_add_message` / `_accept_office_text_file` → `OfficeSession` → `_run_office_session_booklet` → Office full generation and render. |
| Quiz / flashcards | `_route_quiz_callback` / `_quiz_generate`, `_route_flashcard_callback` / `_flashcard_generate`; document callbacks can enter `_generate_from_document` directly. `_source_from_document` uses stored `DocChunk`s, falling back to stored text when the cache gives no chunks. Library paths can chunk stored study text. `_generate_learning` → claim free use or durable charge → `_generate_chunks` → `learning.py` prompts, `parse_quiz_payload` / `parse_flashcard_payload`, locator checks, dedupe and bounded top-up → persist/save or refund on failure/cancel/expiry per learning lifecycle → `_send_learning_html` via `learning_html.py`. Manual flashcards bypass the model. This is not the booklet HTML review planner. |

### Shared booklet compiler, in actual order

1. `structure_document` → `_structure_booklet`. Trim input, resolve sole `full` mode; `effective_chunk_chars` subtracts output and prompt reserve from the **configured** window. `split_transcript` uses paragraph/sentence/word boundaries and existing topic-start heuristics; `split_presentation_document` packs slide blocks and reintroduces continuation headers on splits. For multiple chunks a second sizing pass reserves orientation or omits it if no room. `LectureContext` / `build_context` derive topic hints from chunk openings; `context_block_for` attaches adjacent tails/heads as orientation, not primary material. No LLM planner.
2. `_structure_piece` uses `_prompt_block.notes_system_prompt` + `build_chunk_message`. It calls `llm.complete_json`, then `_load_json_payload` → `StructuredNotes.from_payload`. Valid JSON that fails the domain parser has already exited the provider dispatch; it does **not** automatically try the next provider. Typed context/output exhaustion that survives dispatch triggers bounded split/re-render/recursive merge. Parallel map outcomes retain input order; a failed chunk cannot simply disappear: it receives local extractive recovery, otherwise whole-source recovery or an error. Split-path siblings are cancelled and awaited if one child fails.
3. `merge_structured_notes`: first nonempty title/subtitle, sentence-deduped summary, `_merge_sections` and table/definition/block dedupe, ordered global lists, and per-key glossary/Q&A/tip selection. `verify_and_repair` runs after the merge: `gate_notes`, ordered claim → coverage → structural findings in `repair` mode, targeted or whole-draft edit depending on size, deterministic span scrub and/or fallback. `warn` suppresses LLM repair, not strict grounding cleanup. `ensure_structured_booklet` fills structural gaps locally, using existing content for final review.
4. Multi-chunk only: optional `booklet_synthesis` edit pass edits existing JSON plus a deterministic topic digest; one request when bounded, otherwise whole-section groups, optionally a second consolidation level if it fits. Rejected groups retain originals; failed whole compile retains merge. `on_merged` can expose the pre-compile merge for API callers, but the Telegram lecture entry explicitly passes no draft callback. Single-chunk early return bypasses compile even if enabled.
5. `finalize` optionally calls `fill_coverage_gaps`, then ensures the structure; strict mode also runs final evidence acceptance and may fall back to source-only notes. The fill selector uses sentence/topic-word overlap (unit threshold 0.65, total threshold 0.85) and a separate skeleton, so a missing skeleton alone does not trigger an LLM fill. Some strict compile/repair paths re-audit again. `structure_document` does another ensure/structural check and (strict plus callback only) runs a reporting audit to flag residual omissions. Office full additionally has its own fallback and, in strict mode, its own whole-source and locator-local checks, including 98% numeric coverage and aggregate coverage floors (100% if fewer than four signals, otherwise 95% full / 85% compact on their respective paths).
6. Lecture job `_deliver_booklet` checks structure again (and gates again in strict mode). `docx_export.build_notes_docx` and `notes_pdf.build_notes_pdf` use the same structured object and metadata; PDF tries `notes_typst.build_notes_pdf_typst` then ReportLab on failure/unavailability. Lecture validates bytes (`artifact_validation.validate_pdf_artifact` / `validate_docx_artifact`), persists each artifact independently, then exposes stored results via Telegram buttons. `_fetch_artifact` and `_fetch_persisted_booklet` serve stored artifacts; they do not regenerate a booklet. Office full uses the same core renderers; its wrapper does not explicitly call the same two lecture artifact-byte validators. Compact instead uses `build_plain_docx` and `build_office_document` → `build_office_pdf` through `_office_run_operation` delivery/Library handling.
7. Booklet HTML is **a derived interactive review sheet, not a third full serialization of `StructuredNotes`**. Eligible full booklets → `review_sheet.generate_review_sheet` → `build_review_units` / `plan_review_sheet` → `parse_review_plan` (evidence references resolve to existing units) → `review_sheet_html.render_review_sheet_html` → `artifact_validation.inspect_review_html` → store/send. No plan for recovered/degraded or unsuitable booklets; a review failure does not invalidate PDF/DOCX. The Office paths additionally avoid HTML when their own fallback flag is true.

### Model and validation contract

- `notes_response_schema` is a closed JSON contract. `NoteSection`, `NoteTable`, `NoteDefinition`, `NoteCallout`, `GlossaryEntry`, `PracticeQuestion`, and `PracticalTip` parse/normalize JSON into `StructuredNotes`. Primary fields are title/subtitle/summary, educational sections, glossary, objectives, key points, practice/review, tips, mistakes, final review. `to_payload`/`to_json` and `to_markdown` are used for transport/storage and analysis/export respectively. Preserve compatibility of archived payloads (`full` normalization and legacy review-question handling). Operational quality state and delivery notices must not require modifying the provider schema.
- `notes_evidence.build_source_index` / `audit_notes` compare values, units, dates and terms; `sentence_coverage` estimates lexical recall; `prose_claim_evidence`, `validate_table`, and `validate_tip` evaluate the **draft against source**. `gate_notes` returns both a cleaned copy and `NoteQualityReport`. Missing information and unsupported additions are different defects. Strict `acceptance_failure` actually enforces 95% numeric and term recall plus 85% sentence recall, whereas the diagnostic `assess_completeness` flags even individual missing signals. Strict delivery can therefore fall back or fail below thresholds despite comments describing completeness as diagnostic. Default direct-scientific mode deliberately preserves controlled enrichment and does not run strict evidence acceptance.
- Provider and document boundaries are distinct: `notes_response_schema` guides compliant gateways; `_parse_json_object` checks JSON, not the domain model; `from_payload` normalizes/rejects; `ensure_structured_booklet` is a structural repair, not evidence verification; artifact validators inspect bytes/layout, not completeness. None is a source sensitivity classifier.

## B. Function inventory and disposition

Function families are grouped when they share a boundary and proposed disposition. No removals in Phase 0.

| Function | File | Responsibility | Keep / Modify / Remove | Reason |
|---|---|---|---|---|
| `_handle_message`, `_handle_callback`; `_document_metadata`, `_presentation_metadata`, `_media_metadata` | `bot.py`, `intake.py` | Telegram dispatch and intake classification | Keep | User workflows, admission and ordering are production contracts. |
| `_accept_media`, `_accept_presentation`, `_accept_raw_text`, `_queue_admitted_submission`, `_run_job`, `_settle`, `_process_media`, `_process_presentation`, `_process_raw_text` | `bot_jobs.py` | Queued lifecycle, reservations, storage, STT dispatch | Keep; characterize text write timing | Preserve accounting and statuses; address text durability separately after tests. |
| `transcribe`, `_attempt_provider`, `_speechmatics`, `_speechmatics_poll`, `_deepgram`, `_openai_compatible`, `_openai_plain` | `stt.py` | ASR and failover | Keep | Different workload and reliability contract from document compilation. |
| `load_presentation`, `convert_to_pptx`, `_extract_slide_text`, `slides_outline` | `presentations.py` | Deck conversion, text and media discovery | Keep; verify omissions | Shared extraction; cover chart caps and mixed media before changing behavior. |
| `extract_document`, `_read_pdf_pages`, `_docx_sections`, `_pptx_sections`, `sections_from_slides` | `doc_extract.py` | Source extraction and locators | Modify incrementally | Disclose extraction gaps; keep current text-only policy. |
| `chunk_sections`, `chunk_text`, `chunks_from_json`, `preview_text` | `doc_extract.py` | Locator-aware packing, stored cache replay, UI preview | Keep; validate cache integrity | A nonempty partial cache currently prevents fallback to complete stored text. |
| `build_document`, `guarded`, `fingerprint`, `clean_typography`, `TranscriptDocument.llm_document`, `split_transcript`, `starts_a_topic` | `transcript.py` | Transcript representation, normalization and ordered chunking | Keep; strengthen fidelity | Fingerprint is counts only, not a proof of identity/order; topic-awareness already exists. |
| `build_presentation_document`, `_slide_blocks`, `_slide_narration_blocks`, `split_presentation_document` | `notes.py` | Aligned source composition and slide boundaries | Modify incrementally | Preserve unmatched content and distinction between synthetic labels and body. |
| `LectureContext`, `build_context`, `context_block_for`, `build_source_digest`, `effective_chunk_chars` | `notes.py` | Deterministic orientation and window sizing | Strengthen | Add stable source units/spans and task-aware budget calculation. |
| `notes_system_prompt`, `build_chunk_message`, `edit_system_prompt`, `build_edit_message` | `_prompt_block.py` | Shared Persian generation/edit prompts | Keep separation; refine | Keep source in user payload and static prefix; distinguish generation/edit responsibilities. |
| `_structure_booklet`, `structure_document`, `_structure_piece`, `structure_transcript`, `structure_presentation` | `notes.py` | Generate/map and recover | Modify internals only | Preserve public signatures, order and stage callbacks; split orchestration responsibilities. |
| `_load_json_payload`, `StructuredNotes.from_payload`, `notes_response_schema`, `to_payload` / `to_json` / `to_markdown` | `notes.py` | Domain contract and serialization | Keep; make discard observable | Must round-trip stored jobs and keep renderer compatibility. |
| `_merge_sections`, `_combine_sections`, `merge_structured_notes`, `_merge_summaries` | `notes.py` | Deterministic reduction | Keep; measure distinct-block retention | Distinguish exact duplicates from complementary evidence. |
| `build_source_index`, `audit_notes`, `sentence_coverage`, `prose_claim_evidence`, `validate_table`, `validate_tip` | `notes_evidence.py` | Deterministic evidence/coverage primitives | Keep; reuse indexes | Do not reinterpret as source filtering. |
| `gate_notes`, `ensure_structured_booklet`, `enforce_final_acceptance`, `assess_completeness`, `acceptance_failure` | `notes.py` | Cleanup, shape, accept/reject, omission reporting | Consolidate orchestration | Same draft is reprocessed across pipeline, Office and delivery; semantics differ by mode. |
| `build_repair_issues`, `build_repair_document`, `_VerificationRun.repair`, `fill_coverage_gaps` | `notes.py`, `booklet_verification.py` | Repair and replacement/append | Modify | Always select targets first, patch only validated IDs, consolidate overlapping coverage work. |
| `instructional_source_text`, `is_usable_transcript`, `deterministic_fallback_notes`, `scrub_unsupported_content` | `notes.py` | Fallback/source projection and draft salvage | Keep; audit guardrails | Preserve useful degraded output and raw access with accurate status. |
| `booklet_synthesis.synthesize_booklet`, `_Editor.request`, `_split_point`, `compile_accepts` | `notes.py`, `booklet_synthesis.py` | Optional synthesis | Modify | Enforce baseline evidence retention, bound call count independently of group count. |
| `_sanitize_prompt_text`, `scrub_backend_artifacts` | `llm.py`, `notes.py` | Technical credential and echoed plumbing scrubbing | Keep | Security controls; characterize pattern collisions, do not remove redaction. |
| `complete_json`, `_complete_json_on_route`, `_dispatch_across_routes`, `_complete_on_route`, provider dialects, `clamp_output_tokens` | `llm.py`, `llm_profiles.py` | Transport, schema dialect, retry, route leases | Keep | Preserve fallback, quotas and compatibility. Budget inputs can improve without replacing transport. |
| `_ingest_document`, `_run_document_notes`, `_run_office_session_booklet`, `_office_run_operation`, `generate_full_booklet`, `_full_result_is_grounded`, `generate_compact_notes` | `bot_documents.py`, `office_notes.py` | Office wrappers and compact product | Keep interfaces; align full verification | Preserve office-specific locators and product limits; compact is intentionally lossy. |
| `_generate_learning`, `_generate_chunks`, `_send_learning_html`, `_claim_generation_budget`, `_refund_generation`, `parse_quiz_payload`, `parse_flashcard_payload` | `bot_learning.py`, `learning.py` | Learning outputs and durable charges | Keep outside core migration | Scope/billing and finite item counts differ from full booklet. |
| `_deliver_booklet`, `_fetch_artifact`, `_fetch_persisted_booklet`, `build_notes_docx`, `build_notes_pdf`, `build_notes_pdf_typst`, `generate_review_sheet`, `render_review_sheet_html` | `bot_jobs.py`, exporters, `review_sheet*.py` | Delivery and render | Keep; unify verifier handoff | Preserve RTL, typography, HTML contract, ownership, and stored-byte retrieval. |

## C. LLM and STT call inventory

**Configured candidate routes only.** A = AvalAI `deepseek-v4.1-flash` (profile 1,000,000 context / 393,216 max output); G = Gemini `gemini-3.5-flash-lite` (1,048,576 / 65,536); O = OpenRouter `nvidia/nemotron-3.5-super-vl:free` (1,000,000 / 131,072). These limits are declarations and can differ from current routing/model reality. Keys and model overrides resolve in `config.note_routes`; no keys means no LLM route. The default logical output reservation below is 16,384 tokens except learning overrides; wire limits are clamped per profile. `NOTE_CONTEXT_TOKENS=131072` is independent of profile window declarations. Repository search finds profile context/max-input fields used for declarations/tests, not for actual LLM input sizing. Schema/wrappers/tokenization can differ from the planning estimate.

| Call / task | Prompt, purpose and output | Provider / model | Input budget | Output budget |
|---|---|---|---|---|
| `_structure_piece` / `BOOKLET_GENERATION` | `notes_system_prompt` + `build_chunk_message` with one source chunk, header and optional neighboring orientation; full notes JSON | A → G → O | `min(NOTE_CHUNK_CHARS=64000, 2 × (configured context − configured output − ceil(reserved chars/2) − 512))`; optional context reserve; max 200 initial chunks; recursive split depth 2 on typed overflow | 16,384 default, profile clamp |
| `_VerificationRun.repair` / `BOOKLET_REPAIR` | `_REPAIR_PROMPT` + per-node repair request (booklet_verification), draft (full or subset) + repair commands and evidence; notes JSON; temperature 0 where supported | A → G → O | Context-derived editorial budget via `effective_compile_max_chars`, including compile operator cap; up to 40 issues, subset at most 6 sections, evidence windows/caps; may skip | 16,384; 2 attempt slots by default, max 3, strict `repair` only |
| `fill_coverage_gaps` / `BOOKLET_REPAIR` | `COVERAGE_FILL_SYSTEM_PROMPT` + `build_fill_document`: omission strings, evidence excerpts and heading hints, not full draft; supplemental notes JSON; temperature 0 | A → G → O | At most 6 strings × 700 chars, up to 8,000 chars of evidence and 600 chars of heading context; no call-specific context proof | 16,384; one logical call, off by default |
| `_Editor.request` / `BOOKLET_COMPILE` | `synthesis_system_prompt` + edit envelope (`build_edit_message` with topic digest and structured draft); notes JSON; temperature 0.1 | A → G → O (`NOTE_COMPILE_PROVIDER` passed but known task order wins) | Configured context minus output/prompt/4,096 editorial overhead and 512 token reserve; 160,000 hard chars, `NOTE_COMPILE_MAX_CHARS` can make smaller; each built request checked again | 16,384 each; multi-chunk only; opt-in, optional group/level-2 calls |
| `generate_compact_notes.make_one` / `COMPACT_NOTES` | `COMPACT_NOTE_SYSTEM_PROMPT` + `_compact_prompt(title, locator, text)`; `COMPACT_NOTE_SCHEMA` with heading/points/locators | G → A → O | 20,000 chars per rechunked source unit; 250,000 total source chars; prompt envelope extra | 16,384 default; one per chunk before local fallback |
| `plan_review_sheet` / `BOOKLET_REVIEW_PLAN` | `REVIEW_PLAN_SYSTEM_PROMPT` + `build_review_planning_document`, unit catalog and duration; `review_plan_response_schema` grounded IDs/section plan/self-test; temperature 0.1 | A → G → O | 60,000 chars; skip rather than clip if too large | 16,384; one logical request |
| `_generate_chunks._generate_piece` quiz / `QUIZ_GENERATION`, `QUIZ_20` | `quiz_system_prompt` + `learning_user_prompt` with requested count, locators, full chosen chunk and optional do-not-repeat stems; `QUIZ_SCHEMA` questions/options/correct/explain/difficulty/source | G → A → O standard; A → G → O when requested count ≥20 | `_learning_max_chars`: max 56,000, estimated 2 chars/token after output and 2,000-token margin, minimum derived 2,000 chars; split reduction on typed overflow for text ≥1,600 chars, child budget max(800, half length) with locator overhead; selected chunks only | `min(LEARNING_MAX_OUTPUT_TOKENS=8192, 2500/5000/10000)` for ≤5/≤10/>10 items |
| `_generate_chunks._generate_piece` deck / `FLASHCARD_GENERATION` | `flashcard_system_prompt` + `learning_user_prompt`, same evidence-locator mechanism; flashcard schema front/back/source | G → A → O | Same learning chunker; commonly up to 20 items/call, batch cap 10 for deck target ≥40; planned batch count +1 top-up per piece | `min(8192, 2000/4000)` for ≤10/>10 cards |
| `office_ai.plan_deck` / `OFFICE_SLIDE_PLANNING`; `office_ai.infer_table` / `OFFICE_TABLE_GENERATION` | Adjacent Office export only: `DECK_SYSTEM_PROMPT` or `TABLE_SYSTEM_PROMPT`, source/draft, section or table JSON, local validation and deterministic export fallback | G → A → O | Deck packs 5,000-char pieces; table uses bounded Office session text and local shape limits | 16,384 by default |

`complete()` is a generic text API but is not a booklet-generation call site. `KEY_POINTS`, `QUIZ_DIFFICULT`, and `OFFICE_DOCUMENT_GENERATION` are routing labels, not evidence of live `complete_json` calls; transcript key points can be read from stored notes. Prewarm is a metadata/connection operation, not generation with source data.

**Shared request mechanics.** AvalAI and OpenRouter use OpenAI-compatible chat completion; Gemini uses native generate/stream requests. Default sampling is 0.2 where sent; Gemini Flash-Lite omits sampling fields, so per-call temperature is not a universal wire guarantee. Profile dispatch handles JSON schema (AvalAI non-strict schema; Gemini native schema conversion; default free OpenRouter JSON object), system-role folding where necessary, sampling/reasoning settings, usage capture and configured credential scrubbing. Explicit length/`MAX_TOKENS` and reasoning exhaustion are not accepted as valid partial artifacts. One JSON parse attempt per route; bounded transport retry (one retry, `Retry-After` capped at 60 s), up to four compatibility adjustments, 10 s connect bound, configured request/TTFB timers. Intermediate timeout/rate limit can go straight to fallback; auth/quota/cooldown/circuit/capacity can bypass a route, and task order is preference among eligible routes. Hedging is disabled in active dispatch even if legacy setting is nonzero. `llm_quota` reserves estimated tokens using 1.5 chars/token by default plus configured output; dispatcher reconciles actual usage in `finally` and releases leases. Learning calls use nonwaiting capacity. Exhaustion re-raises the **first** failure type; a later budget failure can therefore be obscured by an earlier transport error (recovery risk, not a change recommendation for this phase).

**ASR is separate, not text completion.** `transcribe` defaults to configured Speechmatics (`enhanced`, submit/poll/result) → Deepgram (`nova-3`, `/listen`) → OpenAI-compatible (`whisper-1`, multipart `/audio/transcriptions`). Upload is audio bytes plus language/diarization/config fields, no text-token budget; upload caps are provider-specific and response has a 64 MiB bound. Normalizes JSON to `RawTranscript` with available timings/speaker data; compatible route may switch from a rejected rich response format to plain JSON. Provider slots, cooldowns and time budgets govern the chain; `_attempt_provider` does not retry audio submission on transient failure, Speechmatics polling GET has bounded retries. Credit is tied to durable transcript and measured duration, distinct from LLM token quota. Office notes use operator provider quota, not user credits.

## D. Text-loss inventory

Distinguish **body loss**, **explicit refusal**, **intentional derivative**, and **metadata-only rewriting**. There is no educational-topic or privacy classifier on the source. The claim-risk function is applied to draft assertions for source support; it is not a source filter. Existing prompt safety tests already protect named/identifiable lessons and credential-only redaction.

| Boundary / functions | What can be lost or rejected | Visibility / risk |
|---|---|---|
| Telegram intake `_handle_message`, `_looks_like_raw_study_text`, `_accept_raw_text`, Office session/file admission | Auto-dispatch expects ≥120 chars and ≥20 words; explicit booklet command/prompt has separate entry. Paste cap is `max(OFFICE_MAX_CHARS, 200000)` (default 200,000). Office draft, byte, file, package, time, queue and slot limits reject rather than trim. Photos/scans unsupported; DOCX not legacy `.doc`. | UI refusal/hint; not semantic censorship. Direct-text memory before first persist is a durability gap vs audio. |
| `doc_extract._read_pdf_pages`, `_pdf_sections_from_pages`, `looks_scanned`, `_check_extracted_text_size` | Text-layer only: empty pages dropped from section list, no OCR/image extraction, a page parse exception becomes an empty page and a log warning. Mixed text/image or partially unreadable PDFs may pass aggregate scan heuristic. Encrypted/corrupt or thin scans and extracted text >5,000,000 chars are refused. `_clean_block` collapses whitespace and removes blank lines. | **User can get a partly extracted PDF without a page-level notice.** Layout-based math/columns can change before any model call. |
| `doc_extract._docx_sections` | Main body paragraphs and top-level table rows only; merged repeats deduped, empty cells not represented, table rows flattened; nested tables/headers/footers/text boxes and other non-body elements not traversed by this code. Locator heading shortened to 60 chars, full heading remains separate. | Scope omission is not a classifier. Blank cell removal can blur column relationships. |
| `presentations._extract_slide_text*`, chart/notes readers, `sections_from_slides`, `load_presentation`, `prepare_audio`, `slides_outline` | No OCR; unsupported objects/fields and unreadable shapes may be skipped; chart helper paths cap categories/points at 60; empty slides do not form document sections. Config flags can exclude slide text or video audio; unreferenced media excluded by default; legacy conversion/media clip caps or decode skips can omit narration (lecture stores skip notices where collected). `slides_outline(limit=…)` supports annotated clipping but main lecture call supplies no limit. | Audit chart and mixed-deck fixtures; do not change deliberate media caps silently. |
| `stt` response parsers, `transcript.build_document`, `guarded`, `plain` / `llm_document`, `textnorm.normalize_display` | Parsers may ignore unsupported result entries/alternatives; timed words take precedence over provider text if they form paragraphs, without cross-check that the provider text is fully represented. Speaker/timing scaffolding is omitted from LLM prose by design and archived separately where available. Typographic normalization changes punctuation, whitespace and Persian letter variants; fingerprint checks counts, not identities/order. | Defend against timed-word/text disagreement; keep raw and model-input concepts separate. |
| `notes._slide_blocks`, `_slide_narration_blocks`, `build_presentation_document`; `_deliver_booklet` Library source selection | Aligned branch parses from first marker onward: unmarked preambles can disappear; duplicate slide markers keep first nonempty title and merge bodies; aligned assembly reorders by slide then unassigned tail. The submission retains the original combined raw text, but the virtual Library learning source uses `document.llm_document()` (when present), not `plain_text` passed for the combined presentation. | Reproducible by code path; validate with mixed-marker and slide-only/narrated Library fixtures before changing. |
| `doc_extract.chunk_sections`, `chunk_text`, `chunks_from_json`, Office `_strip_chunk_locators` / `_rechunk_source_chunks`; `split_transcript`, `split_presentation_document` | Splits pack all body fragments in order apart from whitespace reconstruction and synthetic labels; very long unbroken spans may need hard cuts. Locator metadata is bounded/relabelled. Stored cache parser drops malformed entries; callers only rebuild from stored text when **no** valid chunks remain. Office projection strips recognized locator tags; test collision with literal body lines. | Nonempty damaged cache is a silent body-loss risk; no wholesale rechunking replacement needed. |
| `build_context`, `context_block_for`, `build_source_digest`, `_structure_booklet` limits | Topic openings → bounded digest (220-char opening, up to 6,000-char digest; neighboring context 260 chars each); narrow windows can disable orientation. The primary chunk is not shortened by that choice. Hard budget or >200 chunks → error; provider overflow → recursively smaller sources, fallback, or failure. | Context loss isn't primary source loss. Verify wrappers and schema overhead with realistic Persian/mathematical fixtures. |
| `llm._sanitize_prompt_text`; `llm` dialects/SSE parser | Only configured secrets, secret-named environment values, token-shaped strings and credential assignments are redacted from prompt. An educational literal matching the credential pattern can be masked as a side effect of security policy. No model payload/body truncation is treated as success: length/reasoning exhaustion, malformed JSON, or response >16 MiB fail the route. | Preserve hardening. Do not confuse secret-pattern collisions with topic filtering. |
| `_bounded_text`, `_string_list`, `NoteTable.from_payload`, `NoteSection.from_payload`, other constructors, `scrub_backend_artifacts`, `_load_json_payload` | Generated fields are scrubbed then sliced (common text 20,000 chars, titles 200; locators 120 chars each and 100 per section); invalid/empty objects may be dropped. Table rows are padded or sliced to header width. Forbidden operational metadata keys can reject whole output. Echo scrub strips backend lines/credential-like fragments on draft text, not uploaded text. | Actual **output** trimming can remove valid copied evidence (or occur again on archived JSON reparse). Preserve scrub; characterize affected fields and explicit status. |
| `_merge_sections`, `_combine_sections`, `_merge_summaries`, `_dedupe_definitions` / `_dedupe_glossary` / `_dedupe_practice_questions` / `_dedupe_tips` | Normalized duplicate leaf blocks/rows and summaries collapse. Per-term/per-question/per-tip winner selection can lose complementary definitions/answers/bases; first title/subtitle used, not every chunk title. Same-heading/topic criteria can combine sections. | Measure unique educational content and source-unit coverage, not section count or length alone. |
| `instructional_source_text`, `is_usable_transcript`, `deterministic_fallback_notes`, `ensure_structured_booklet`, `notes_evidence.mask_structural` | Fallback/grounding projection removes whole lines matching operational labels and strips selected label prefixes. Genuine text in a matching line is a collision risk. `is_usable_transcript` rejects very short/structural-only/numeric-only input (requires letters); ensure removes bodyless sections and builds a final review from ≤8 existing items. Evidence-only masks hide structural numbering from measurements, not model source. | Separate source projection from product acceptance, and keep raw access. |
| `gate_notes`, `scrub_unsupported_content`, `build_repair_document`, `_VerificationRun.repair`, `fill_coverage_gaps` | Strict gate can discard unsupported tables/tips/metadata and explicit false claim spans; broad lexical checks can incorrectly judge good paraphrases. Repair bounds issue/evidence subsets, can replace whole draft when it fits, matches targeted returns by heading via `_preserves_node`, and ignores unplaceable responses. Fill only requests selected gaps and appends/merges; can overlap coverage repair and change quality metadata. | Preserve unmodified sections and track retained/missing/unsupported separately. No coverage threshold is a proof of all source units represented. |
| `compile_accepts`, `booklet_synthesis.synthesize_booklet`, `_Editor.request` | `compile_accepts` checks primarily shape, not comparative retention; strict compares source for safety/coverage, while direct mode accepts shape without comparative audit. One valid response can replace many intact sections. Group failure retains original group but successful group replacements only contribute sections; global fields remain those of merged baseline at level 1. | High-priority preventable omission; keep compile opt-in and verified rollback. |
| `office_notes.generate_full_booklet`, `basic_source_booklet`, `generate_compact_notes`, `_parse_compact_payload`, `_extractive_compact_section`; `bot_learning._generate_chunks`, `learning.distribute_counts` / parsers | Office >250,000 source chars refuses generation without deleting source. Compact intentionally selects points (32 output points/chunk, ≤6,000 chars per point), constrained by evidence and 0.75 compression ratio for sources ≥500 chars. Learning requests finite item counts; chunks with allocation zero aren't sent, field bounds/dedupe/final item cap discard candidates; this is not a full-coverage output. | Maintain explicit product distinction and charge/refund behavior. Do not use compact/quiz counts as full-booklet completeness targets. |
| `review_sheet.build_review_units`, `build_review_planning_document`, `parse_review_plan`; exporters, preview and retrieval | Review HTML selectively plans bounded units (≤11 educational sections, ≤12 units/section, ≤12 self-test questions); invalid/unresolved references dropped; request above 60,000 chars is skipped, not clipped. Renderers shorten some running headers/cover-navigation labels and constrain table width to headers; these are not full-source projections. `preview_text` intentionally clips with ellipsis; transcript TXT sends stored body and chat renders pages; legacy raw fallback caps are not consulted by current fetch path. | HTML is optional and condensed; no mandate to copy every DOCX section into the review product. Compare rendered bodies, not running headers/preview. |

## E. Migration plan, dependencies and regression risks

| Priority / target stage | Increment using current implementation | Dependency and acceptance check | Principal regression risk |
|---|---|---|---|
| P0 — characterize contracts | Extend existing source-fidelity/prompt safety tests through all source adapters. Mark actual current defects explicitly with fixtures; record call counts, status and artifact parity. | Phase 0 baseline; no code changes until next requested phase. Cover long Persian/mixed RTL, typed overflow, multi-document entry, and plain text durability. | Testing an invented behavior instead of the current contract; confusing credential redaction with lesson filtering. |
| P0 — NORMALIZE | Keep existing extraction and splitting. Carry source IDs/locator/spans beside normalized text, distinguish extracted vs original, detect extraction/cache holes, preserve unmatched slide/narration text and persist direct text consistently with lifecycle. | Characterization fixtures; assertions for normalized reconstruction and explicit partial extraction outcomes. Maintain stored cache compatibility. | Whitespace/RTL changes can break evidence and provenance; don't add OCR, modify billing, or invalidate old cache rows. |
| P0 — PLAN | Evolve `LectureContext`, topic heuristics and `effective_chunk_chars` to plan ordered units with source references and envelope-aware, profile-conscious budgets. Keep deterministic default and safe configured bounds; no LLM planner in the default path. | Canonical normalized input, typology/tokenization fixtures; verify all original units assigned once, size estimates bounded by real serialized requests and documented fallback. | Larger chunks can exceed output density or fallback limits; unknown model overrides do not guarantee real upstream context. |
| P0 — GENERATE / MERGE | Retain `complete_json`, schema, slots and route order; separate chunk generation and typed outcome tracking. Keep successful, ordered sub-results on recoverable split failures where appropriate; build deterministic merge with unique-block/source evidence retention. | Plan contract; provider-mocked call-count, cancellation, budget, ordering, failover, billing and old JSON tests. | Provider retries and domain recovery can amplify API usage; dedupe may lose complementary facts; checkpoints must not imply success or duplicate charges. |
| P0 — VERIFY → TARGETED REPAIR | Reuse evidence index, separate unsupported additions vs source omissions, derive a single actionable report and patch only stable affected IDs with bounded evidence. Unify overlapping coverage repair/fill after defining compatibility; never serialize the whole draft for a local fix. Recheck patched sections and final acceptance. | Stable draft/source IDs and explicit accepted/recovered/partial/failed outcomes; unchanged sections byte/structurally equal in repair fixtures. Test strict and default educational enrichment independently. | Mislabeling direct-scientific notes as verified; over-rejecting explanatory paraphrases; violating strict Office locator checks or existing notices. |
| P1 — optional SYNTHESIZE | Propose edits to existing structured notes, using the deterministic merge as rollback, not raw-source re-generation. Bound calls/groups and require comparative retention/security verification before accepting. | Merge and shared verify contracts; edit-only tests, preservation of numbers/units/terms/sections, compile disabled means zero calls. | Current tests deliberately permit some degraded compile results; change expectations only when a later requested phase specifies acceptance policy. |
| P1 — RENDER / integration | Pass the accepted model and report to existing renderers and keep byte/layout validation. Keep derived review HTML a separate bounded product; measure extra calls before proposing an opt-in or deterministic alternative. | End-to-end lifecycle, persisted artifact retrieval, visual golden/RTL tests and Library flows. | Repeated render/generation on download; semantics of `None` for HTML; loss of typography, notices, or credit behavior. |

### Duplicate work and conflicting boundaries to resolve later

- Multiple `ensure_structured_booklet` and `gate_notes` traversals occur during strict map, merge, repair, compile, finalization, callback, Office validation and render boundary. Office `_full_result_is_grounded` computes cleaned notes and returns a boolean, then `generate_full_booklet` re-cleans/re-audits the original. Carry forward a canonical report/validated value, not unchecked inputs.
- Strict `acceptance_failure` thresholds conflict with descriptions of all safe incomplete results being deliverable; direct mode lacks this coverage gate by design. `compile_accepts` can permit loss, while final acceptance may replace the result with the extractive fallback. Omission reporting is diagnostic (`assess_completeness`) while `acceptance_failure` decides delivery; callback-only reporting and output-quality metadata are not equivalent to a verified source-unit ledger.
- Repair builds a full draft even before deciding whether a subset is needed, uses the compile budget, and can service the same omissions as `fill_coverage_gaps`. `compile_skip_reason` and `booklet_synthesis.synthesize_booklet` also repeat sizing/skip work; group plus level-2 compile adds requests. `NOTE_COMPILE_PROVIDER` does not override the fixed task order, and `_complete_json_on_route`'s two-attempt docstring does not reflect its one JSON attempt.
- Compact Office builds extractive fallback sections in advance and recomputes each with an active provider. Office full eagerly builds and can even strictly validate the fallback before requesting generation. These are duplicate local CPU work, not duplicate LLM calls.
- Review HTML is a genuine separate LLM responsibility under the current feature, not duplicate synthesis. Count it when measuring per-user request cost; do not remove a user-facing artifact without a replacement contract. No baseline global-planner call exists.

## Baseline tests and existing contract coverage

Installed the runtime requirements plus pytest and PyMuPDF into ignored `.venv`. Ran `.venv/bin/python -m pytest -q` with Python 3.11.2 / pytest 9.2.0: **exit 0, 1,901 passed, 503 subtests passed in 206.54s (3:26)**. No live provider or Telegram calls were made for this audit. The separate unittest, lint, mypy, deployment and live-provider gates were not run.

| Existing test area | Regression contracts to preserve |
|---|---|
| `test_doc_extract`, `test_document_ingestion`, `test_fileio`, `test_media`, `test_preprocess`, `test_stt`, `test_diarization`, `test_transcript`, `test_gamas2` | Page/slide/table and speaker metadata, scan refusal, package/upload bounds, chunking/order, timed fallback and Persian typography, presentation conversion/audio alignment. |
| `test_notes`, `test_note_evidence`, `test_note_acceptance_and_prompt_safety`, `test_booklet_contract`, `test_booklet_recovery`, `test_scientific_booklet_overhaul`, `test_acceptance_mission` | Schema closure/round-trip, educational enrichment, transport redaction, evidence/coverage/scope, constrained repair, deterministic fallback and no partial map success. Explicit examples: `test_named_and_identifiable_educational_source_is_not_rejected_or_masked`, `test_credentials_are_removed_from_prompt_but_educational_text_survives`, `test_one_chunk_failure_preserves_successful_chunks`, `test_compile_loss_of_coverage_is_delivered_degraded`. |
| `test_llm`, `test_llm_perf`, `test_llm_quota`, `test_llm_task_routing`, `test_note_provider_compatibility`, `test_provider_allowlist`, `test_provider_contracts_and_quality`, `test_concurrency_and_failover`, `test_hardening`, `test_hardening_large` | Route order/allowlist, dialect and schema normalization, retry/cancellation/error/reasoning behavior, context/output exhaustion, lease/quota invariants, no source in provider failure logs. |
| `test_office`, `test_office_flow`, `test_office_notes`, `test_learning`, `test_learning_html_and_library`, `test_free_uses`, `test_library` | Office full/compact/fallback, education item allocation/validation/shortfalls, generated HTML and saved library content, refund/gift semantics. |
| `test_docx_export`, `test_notes_pdf`, `test_booklet_design`, `test_booklet_rendering`, `test_pdf_visual_qa`, `test_pdf_export`, `test_document_model`, `test_review_sheet`, `test_artifact_validation` | Native RTL, hierarchy, tables/callouts/questions, Typst→ReportLab, byte/visual QA, grounded review unit references, self-test and HTML safety. |
| `test_bot`, `test_jobs`, `test_progress_bars`, `test_risky_module_characterization`, `test_workflow_regressions`, `test_reaudit_workflows`, `test_solid_architecture`, `test_download_reservations`, `test_db_billing`, `test_credits`, `test_money_invariants`, `test_config`, `test_config_consistency` | Telegram handlers, admission/progress/statuses, ownership and persisted fetch, queue/credit/retry/recovery behavior, default/env/doc consistency. |

Additional offline probes (not tests counted above), under empty environment and `/dev/null` dotenv: 0 configured LLM routes; both default map sizes = 64,000 chars; compile size limit = 160,000 chars; `note_quality_gate=repair` (inactive for default direct mode). Synthetic presentation input with unmarked slide and narration preambles plus valid markers lost both preambles and retained both marked bodies. A cache containing one valid entry and one null returned one chunk, not an invalid-cache signal. `is_usable_transcript('2025 123.45')` returned `False`. These probes show why a green suite does not establish complete source preservation for all accepted inputs.

Remaining validation risks: no live tokenizer/window or provider quality measurements; extraction coverage cannot be proven for unsupported Office/PDF structures; historical production data and status frequency were not inspected. Existing tests characterize many deliberate fallback/quality behaviors but do not certify the proposed migration.

Partial-map recovery retains successful chunks in memory, not durable per-chunk checkpoints; a process interruption is distinct from a recoverable provider error. Some domain or programmer errors currently degrade via broad catches; preserve correct user-visible failure reporting while narrowing these boundaries.

No source, test, dependency, migration, settings, or renderer changes; no functions added/modified/removed. Documentation-only audit. **Next: PROMPT 2, only if explicitly requested.**

## Appendix — effective configuration defaults

All `Settings` fields loaded from a clean environment with an empty dotenv (`config.from_env('/dev/null')`); names identify environment bindings, including legacy/derived cases. Paths under the checkout are written relative to repo root. Values are defaults, not credentials or production configuration. `PLAN_ENV_FIELDS` is expanded below. Relevant non-setting hard caps are documented in sections C/D. `NOTE_CONTEXT_TOKENS` and `NOTE_MAX_OUTPUT_TOKENS` are validated independently, with additional advisory checks for the combination and timeout; API keys default empty, so capacity and model defaults are not evidence of a configured route. `NOTE_PROGRESSIVE_BOOKLET` does not override the current Telegram wrappers' no-draft delivery, `NOTE_HEDGE_AFTER_SECONDS` does not activate hedging, and current raw retrieval does not use `RAW_FALLBACK_*` clipping knobs.

| Environment | Settings field | Effective empty-environment default |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | `bot_token` | `''` |
| `TELEGRAM_API_ID` | `api_id` | `0` |
| `TELEGRAM_API_HASH` | `api_hash` | `''` |
| `ADMIN_IDS` | `admin_ids` | `frozenset()` |
| `TELEGRAM_SESSION` | `session_path` | `'./data/gamas'` |
| `DB_PATH` | `db_path` | `'./data/gamas_v3.db'` |
| `TEMP_DIR` | `temp_dir` | `'./data/tmp'` |
| `LOG_FILE` | `log_file` | `None` |
| `LOG_LEVEL` | `log_level` | `'INFO'` |
| `LOG_FORMAT` | `log_format` | `'plain'` |
| `LOG_MAX_BYTES` | `log_max_bytes` | `5242880` |
| `LOG_BACKUP_COUNT` | `log_backup_count` | `5` |
| `MAX_FILE_SIZE` | `max_file_size` | `1073741824` |
| `MAX_MEDIA_DURATION_SECONDS` | `max_media_duration_seconds` | `7200.0` |
| `MAX_ACTIVE_JOBS` | `max_active_jobs` | `3` |
| `MAX_ACTIVE_JOBS_PER_USER` | `max_active_jobs_per_user` | `2` |
| `MAX_QUEUED_JOBS` | `max_queued_jobs` | `8` |
| `MEDIA_WORKER_CONCURRENCY` | `media_worker_concurrency` | `1` |
| `MEDIA_TIMEOUT` | `media_timeout` | `3600.0` |
| `CONVERT_TIMEOUT` | `convert_timeout` | `600.0` |
| `STT_JOB_TIMEOUT` | `stt_job_timeout` | `21600.0` |
| `STT_POLL_INTERVAL` | `stt_poll_interval` | `10.0` |
| `TEMP_MIN_FREE_BYTES` | `temp_min_free_bytes` | `67108864` |
| `DOWNLOAD_STALL_SECONDS` | `download_stall_seconds` | `300.0` |
| `PRESENTATION_MAX_CLIPS` | `presentation_max_clips` | `50` |
| `PRESENTATION_MAX_TOTAL_DURATION` | `presentation_max_total_duration` | `7200.0` |
| `PRESENTATION_MAX_UNPACKED_BYTES` | `presentation_max_unpacked_bytes` | `1073741824` |
| `PRESENTATION_MAX_TEMP_BYTES` | `presentation_max_temp_bytes` | `2147483648` |
| `PRESENTATION_MIN_CLIP_SECONDS` | `presentation_min_clip_seconds` | `0.5` |
| `PRESENTATION_SILENCE_SECONDS` | `presentation_silence_seconds` | `0.25` |
| `PRESENTATION_WAV_LIMIT_BYTES` | `presentation_wav_limit_bytes` | `1073741824` |
| `PRESENTATION_INCLUDE_SLIDE_TEXT` | `presentation_include_slide_text` | `True` |
| `PRESENTATION_INCLUDE_VIDEO_AUDIO` | `presentation_include_video_audio` | `True` |
| `PRESENTATION_INCLUDE_UNREFERENCED_MEDIA` | `presentation_include_unreferenced_media` | `False` |
| `AUDIO_FORMAT_POLICY` | `audio_format_policy` | `'auto'` |
| `TELEGRAM_DOWNLOAD_CONNECTIONS` | `telegram_download_connections` | `1` |
| `STT_PROVIDERS` | `stt_providers` | `('speechmatics', 'deepgram', 'openai_compatible')` |
| `STT_LANGUAGE` | `stt_language` | `'fa'` |
| `SPEECHMATICS_API_KEY` | `speechmatics_api_key` | `''` |
| `SPEECHMATICS_BASE_URL` | `speechmatics_base_url` | `'https://asr.api.speechmatics.com/v2'` |
| `SPEECHMATICS_MODEL` | `speechmatics_model` | `'enhanced'` |
| `STT_DIARIZE`, `DIARIZATION_ENABLED` | `stt_diarize` | `True` |
| `STT_PREPROCESS` | `stt_preprocess` | `True` |
| `STT_SPEAKER_SENSITIVITY` | `stt_speaker_sensitivity` | `0.5` |
| `DEEPGRAM_API_KEY` | `deepgram_api_key` | `''` |
| `DEEPGRAM_BASE_URL` | `deepgram_base_url` | `'https://api.deepgram.com/v1'` |
| `DEEPGRAM_MODEL` | `deepgram_model` | `'nova-3'` |
| `STT_OPENAI_BASE_URL` | `stt_openai_base_url` | `''` |
| `STT_OPENAI_API_KEY` | `stt_openai_api_key` | `''` |
| `STT_OPENAI_MODEL` | `stt_openai_model` | `'whisper-1'` |
| `STT_OPENAI_MAX_UPLOAD_BYTES` | `stt_openai_max_upload` | `25000000` |
| `STT_TRANSFER_TIMEOUT_SECONDS` | `stt_transfer_timeout` | `600.0` |
| `MAX_STT_RESPONSE_BYTES` | `max_stt_response_bytes` | `67108864` |
| `STT_STATUS_LONG_POLL` | `stt_status_long_poll` | `25.0` |
| `NOTE_PROVIDERS` | `note_providers` | `('avalai', 'gemini', 'openrouter')` |
| `NOTE_PROVIDER` | `note_provider` | `''` |
| `NOTE_API_KEY` | `note_api_key` | `''` |
| `NOTE_BASE_URL` | `note_api_base_url` | `''` |
| `NOTE_MODEL` | `note_model` | `''` |
| `OPENROUTER_API_KEY` | `openrouter_api_key` | `''` |
| `OPENROUTER_BASE_URL` | `openrouter_base_url` | `'https://openrouter.ai/api/v1'` |
| `OPENROUTER_MODEL` | `openrouter_model` | `'nvidia/nemotron-3.5-super-vl:free'` |
| `GEMINI_API_KEY` | `gemini_api_key` | `''` |
| `GEMINI_MODEL` | `gemini_model` | `'gemini-3.5-flash-lite'` |
| `AVALAI_API_KEY` | `avalai_api_key` | `''` |
| `AVALAI_BASE_URL` | `avalai_base_url` | `'https://api.avalai.ir/v1'` |
| `AVALAI_MODEL` | `avalai_model` | `'deepseek-v4.1-flash'` |
| `NOTE_GEMINI_THINKING_LEVEL` | `gemini_thinking_level` | `'minimal'` |
| `NOTE_CONTEXT_TOKENS` | `note_context_tokens` | `131072` |
| `NOTE_MODE` | `note_mode` | `'full'` |
| `NOTE_MAX_OUTPUT_TOKENS` | `note_max_output_tokens` | `16384` |
| `NOTE_REQUEST_TIMEOUT_SECONDS` | `note_request_timeout` | `900.0` |
| `NOTE_JSON_MODE` | `note_json_mode` | `False` |
| `NOTE_CHUNK_CHARS` | `note_chunk_chars` | `64000` |
| `NOTE_CONCURRENCY` | `note_concurrency` | `3` |
| `NOTE_COMPILE` | `note_compile` | `False` |
| `NOTE_COMPILE_MAX_CHARS` | `note_compile_max_chars` | `0` |
| `NOTE_REASONING_EFFORT` | `note_reasoning_effort` | `''` |
| `NOTE_STRICT_GROUNDING` | `note_strict_grounding` | `False` |
| `NOTE_QUALITY_GATE` | `note_quality_gate` | `'repair'` |
| `NOTE_OPENROUTER_THROUGHPUT` | `note_openrouter_throughput` | `True` |
| `NOTE_STRUCTURED_OUTPUT` | `note_structured_output` | `True` |
| `NOTE_PROGRESSIVE_BOOKLET` | `note_progressive_booklet` | `False` |
| `NOTE_COMPILE_PROVIDER` | `note_compile_provider` | `''` |
| `RAW_TEXT_PROFILE` | `raw_text_profile` | `'plain'` |
| `RAW_FALLBACK_FILE_CHARS` | `raw_fallback_file_chars` | `100000` |
| `RAW_FALLBACK_MAX_FILES` | `raw_fallback_max_files` | `8` |
| `OFFICE_ENABLED` | `office_enabled` | `True` |
| `OFFICE_MAX_INPUT_BYTES` | `office_max_input_bytes` | `1000000` |
| `OFFICE_MAX_CHARS` | `office_max_chars` | `100000` |
| `OFFICE_MAX_SLIDES` | `office_max_slides` | `0` |
| `OFFICE_SESSION_TTL_MINUTES` | `office_session_ttl_minutes` | `30` |
| `OFFICE_MIN_INTERVAL_SECONDS` | `office_min_interval_seconds` | `10.0` |
| `PAYMENT_CARD_NUMBER` | `payment_card_number` | `''` |
| `PAYMENT_CARD_HOLDER` | `payment_card_holder` | `''` |
| `PAYMENT_BANK_NAME` | `payment_bank_name` | `''` |
| `RECEIPT_DIR` | `receipt_dir` | `'./data/receipts'` |
| `RECEIPT_RETENTION_DAYS` | `receipt_retention_days` | `90` |
| `MAX_PAYMENT_RECEIPT_BYTES` | `max_payment_receipt_bytes` | `5242880` |
| `TRANSCRIPT_RETENTION_DAYS` | `transcript_retention_days` | `0` |
| `MAX_CONCURRENT_DOWNLOADS` | `max_concurrent_downloads` | `2` |
| `MAX_CONCURRENT_UPLOADS` | `max_concurrent_uploads` | `2` |
| `DOWNLOAD_TIMEOUT_SECONDS` | `download_timeout` | `900.0` |
| `UPLOAD_TIMEOUT_SECONDS` | `upload_timeout` | `600.0` |
| `STT_FALLBACK_ENABLED` | `stt_fallback_enabled` | `True` |
| `SPEECHMATICS_MAX_IN_FLIGHT` | `speechmatics_max_in_flight` | `2` |
| `DEEPGRAM_MAX_IN_FLIGHT` | `deepgram_max_in_flight` | `4` |
| `STT_OPENAI_MAX_IN_FLIGHT` | `stt_openai_max_in_flight` | `2` |
| `STT_PROVIDER_TIMEOUT_SECONDS` | `stt_provider_timeout` | `1800.0` |
| `STT_SLOT_WAIT_TIMEOUT_SECONDS` | `stt_slot_wait_timeout` | `600.0` |
| `LLM_PRIMARY_MAX_IN_FLIGHT` | `llm_primary_max_in_flight` | `3` |
| `LLM_FALLBACK_MAX_IN_FLIGHT` | `llm_fallback_max_in_flight` | `1` |
| `LLM_SLOT_WAIT_TIMEOUT_SECONDS` | `llm_slot_wait_timeout` | `300.0` |
| `AVALAI_RPM_LIMIT` | `avalai_rpm_limit` | `35` |
| `AVALAI_TPM_LIMIT` | `avalai_tpm_limit` | `750000` |
| `AVALAI_RPD_LIMIT` | `avalai_rpd_limit` | `0` |
| `AVALAI_MAX_IN_FLIGHT` | `avalai_max_in_flight` | `3` |
| `GEMINI_RPM_LIMIT` | `gemini_rpm_limit` | `0` |
| `GEMINI_TPM_LIMIT` | `gemini_tpm_limit` | `0` |
| `GEMINI_RPD_LIMIT` | `gemini_rpd_limit` | `0` |
| `GEMINI_MAX_IN_FLIGHT` | `gemini_max_in_flight` | `1` |
| `OPENROUTER_RPM_LIMIT` | `openrouter_rpm_limit` | `10` |
| `OPENROUTER_TPM_LIMIT` | `openrouter_tpm_limit` | `0` |
| `OPENROUTER_RPD_LIMIT` | `openrouter_rpd_limit` | `15` |
| `OPENROUTER_MAX_IN_FLIGHT` | `openrouter_max_in_flight` | `1` |
| `LLM_TOKEN_ESTIMATION_CHARS_PER_TOKEN` | `llm_token_estimation_chars_per_token` | `1.5` |
| `DEDUP_WINDOW_SECONDS` | `dedup_window_seconds` | `120.0` |
| `OFFICE_JOB_CONCURRENCY` | `office_job_concurrency` | `2` |
| `LIBRARY_MAX_ITEMS` | `library_max_items` | `50` |
| `DOC_MAX_BYTES` | `doc_max_bytes` | `1073741824` |
| `DOC_UNPACKED_MAX_BYTES` | `doc_unpacked_max_bytes` | `1073741824` |
| `SUPPORT_MAX_OPEN_TICKETS` | `support_max_open_tickets` | `3` |
| `SUPPORT_TICKET_COOLDOWN_SECONDS` | `support_ticket_cooldown_seconds` | `600` |
| `SUBSCRIPTION_CHANNEL` | `subscription_channel` | `'@gamas_AI'` |
| `SUBSCRIPTION_REQUIRED` | `subscription_required` | `True` |
| `SUBSCRIPTION_CACHE_SECONDS` | `subscription_cache_seconds` | `300.0` |
| `SUBSCRIPTION_TIMEOUT_SECONDS` | `subscription_timeout_seconds` | `20.0` |
| `TELEGRAM_API_BASE_URL` | `telegram_api_base_url` | `'https://api.telegram.org'` |
| `QUIZ_COST_SECONDS_PER_QUESTION` | `quiz_cost_seconds_per_question` | `60` |
| `QUIZ_MAX_ACTIVE_HOURS` | `quiz_max_active_hours` | `6` |
| `QUIZ_MAX_SESSIONS_PER_HOUR` | `quiz_max_sessions_per_hour` | `3` |
| `FLASHCARD_AI_COST_SECONDS` | `flashcard_ai_cost_seconds` | `300` |
| `FLASHCARD_AI_DECK_SIZE` | `flashcard_ai_deck_size` | `10` |
| `FLASHCARD_MAX_DECKS_PER_USER` | `flashcard_max_decks_per_user` | `30` |
| `FLASHCARD_MAX_CARDS_PER_DECK` | `flashcard_max_cards_per_deck` | `200` |
| `LEARNING_CHUNK_CONCURRENCY` | `learning_chunk_concurrency` | `1` |
| `LEARNING_GLOBAL_CONCURRENCY` | `learning_global_concurrency` | `2` |
| `LEARNING_MAX_OUTPUT_TOKENS` | `learning_max_output_tokens` | `8192` |
| `DOCUMENT_RETENTION_DAYS` | `document_retention_days` | `0` |
| `LEARNING_RETENTION_DAYS` | `learning_retention_days` | `0` |
| `AUDIT_RETENTION_DAYS` | `audit_retention_days` | `0` |
| `HOLDS_RETENTION_DAYS` | `holds_retention_days` | `0` |
| `PLAN_ENV_FIELDS (below)` | `plan_values` | `See plan defaults below` |
| `LLM_CONNECTOR_LIMIT` | `llm_connector_limit` | `32` |
| `LLM_CONNECTOR_LIMIT_PER_HOST` | `llm_connector_limit_per_host` | `8` |
| `LLM_CONNECTOR_TTL_DNS_CACHE` | `llm_connector_ttl_dns_cache` | `300` |
| `LLM_CONNECTOR_KEEPALIVE` | `llm_connector_keepalive` | `300` |
| `LLM_PREWARM` | `llm_prewarm` | `True` |
| `NOTE_TTFB_TIMEOUT_SECONDS` | `note_ttfb_timeout` | `25.0` |
| `NOTE_HEDGE_AFTER_SECONDS` | `note_hedge_after` | `0.0` |
| `OFFICE_AI_CONCURRENCY` | `office_ai_concurrency` | `3` |
| `NOTE_COMPLETENESS_FILL` | `note_completeness_fill` | `False` |
| `NOTE_MAX_REPAIR_ATTEMPTS` | `note_max_repair_attempts` | `2` |
| `NOTE_ENABLE_DETERMINISTIC_FALLBACK` | `note_enable_deterministic_fallback` | `True` |
| `NOTE_PDF_ENGINE` | `note_pdf_engine` | `'typst'` |
| `STT_PRESERVE_STEREO` | `stt_preserve_stereo` | `False` |
| `DEEPGRAM_DIARIZE_MODEL` | `deepgram_diarize_model` | `''` |

| Tariff environment | `Settings.plan_values` key | Effective default |
|---|---|---:|
| `FREE_PLAN_HOURS` | `free_plan_hours` | `1` |
| `PLAN_3_HOURS` | `plan_3_hours` | `3` |
| `PLAN_3_ORIGINAL_PRICE_TOMAN` | `plan_3_original_price_toman` | `160000` |
| `PLAN_3_PRICE_TOMAN` | `plan_3_price_toman` | `79000` |
| `PLAN_3_VALIDITY_DAYS` | `plan_3_validity_days` | `30` |
| `PLAN_6_HOURS` | `plan_6_hours` | `6` |
| `PLAN_6_ORIGINAL_PRICE_TOMAN` | `plan_6_original_price_toman` | `200000` |
| `PLAN_6_PRICE_TOMAN` | `plan_6_price_toman` | `99000` |
| `PLAN_6_VALIDITY_DAYS` | `plan_6_validity_days` | `30` |
| `PLAN_10_HOURS` | `plan_10_hours` | `10` |
| `PLAN_10_ORIGINAL_PRICE_TOMAN` | `plan_10_original_price_toman` | `220000` |
| `PLAN_10_PRICE_TOMAN` | `plan_10_price_toman` | `109000` |
| `PLAN_10_VALIDITY_DAYS` | `plan_10_validity_days` | `30` |
| `PLAN_20_HOURS` | `plan_20_hours` | `20` |
| `PLAN_20_ORIGINAL_PRICE_TOMAN` | `plan_20_original_price_toman` | `360000` |
| `PLAN_20_PRICE_TOMAN` | `plan_20_price_toman` | `179000` |
| `PLAN_20_VALIDITY_DAYS` | `plan_20_validity_days` | `30` |
| `PLAN_26_HOURS` | `plan_26_hours` | `26` |
| `PLAN_26_ORIGINAL_PRICE_TOMAN` | `plan_26_original_price_toman` | `460000` |
| `PLAN_26_PRICE_TOMAN` | `plan_26_price_toman` | `229000` |
| `PLAN_26_VALIDITY_DAYS` | `plan_26_validity_days` | `30` |
| `PLAN_30_HOURS` | `plan_30_hours` | `30` |
| `PLAN_30_ORIGINAL_PRICE_TOMAN` | `plan_30_original_price_toman` | `600000` |
| `PLAN_30_PRICE_TOMAN` | `plan_30_price_toman` | `299000` |
| `PLAN_30_VALIDITY_DAYS` | `plan_30_validity_days` | `30` |
