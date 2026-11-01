# OCR for scans and photos

**Off by default.** `OCR_ENABLED=false` refuses text-poor scans/photos instead of
saving empty documents. Enabling OCR sends text-poor PDF pages or photos to
**i2ocr.com**, a third-party service; no local OCR engine runs. Review privacy
and consent requirements before using sensitive sources.

## Enable

Install the optional transport from the repository root.

**Windows PowerShell:**

```powershell
.\.venv\Scripts\python.exe -m pip install ".[ocr]"
```

**cPanel Terminal** (virtualenv active):

```bash
python -m pip install '.[ocr]'
```

Edit `.env`, run the [configuration check](CONFIGURATION.md#validate) and restart:

```dotenv
OCR_ENABLED=true
OCR_LANGUAGE=persian
OCR_MAX_PAGES=20
OCR_TIMEOUT_SECONDS=600
```

`fa`/`farsi` are language aliases. Keep bounds on shared hosting;
`OCR_MAX_PAGES=0` removes the page-count bound. Validation fails when OCR is on
without `curl-cffi`. Your host needs a compatible wheel and service connectivity.

## Live test

Use your own non-sensitive image. This command **uploads it to i2ocr.com**:

```powershell
.\.venv\Scripts\python.exe -m scripts.ocr_smoke scan.jpg --language persian
```

```bash
python -m scripts.ocr_smoke scan.jpg --language persian
```

For PDF use `lesson.pdf --pages 1,2`. The script reports stages/text length, not
the recognized text, and does not write to SQLite. Offline mocks do not prove
live service availability.

## Limits and privacy

- Existing PDF text pages are not OCRed. Photos become extracted documents.
- Embedded Word/PowerPoint images are not automatically OCRed; send an image/PDF separately.
- Service protocol changes, rate limits and poor images can cause failures. Fix the
  reported stage or request a clearer source; empty recognition is not a valid document.
- Successful text is cached in SQLite. Library pruning does not erase that cache.
- Third-party retention claims are not bot-enforced guarantees. Review current terms.
  Disable OCR and restart to stop future uploads.

[Security](SECURITY.md) · [Document limits](OFFICE.md)
