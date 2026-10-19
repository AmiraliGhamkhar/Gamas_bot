# Local Persian/Arabic OCR (API-free)

Gamas Bot uses the open-source Tesseract executable locally and PyMuPDF to rasterize scanned PDF pages. OCR does not send file contents to Azure, Google, or any other OCR API.

## Runtime requirements

- Tesseract OCR executable, runnable by the same cPanel account as the bot.
- Tesseract language data: `fas` (Persian), `ara` (Arabic), and `eng` (English, for mixed-language pages).
- Python dependency `PyMuPDF`, installed by `pip install -r requirements.txt`.
- Enough CPU and temporary disk space. The bot runs one OCR job at a time.

Tesseract and its trained data are operating-system resources, not pip packages. Ask the cPanel provider to install them or confirm that user-local binaries are allowed. If the host blocks executing local binaries, API-free OCR cannot run on that plan; run the OCR worker on a VPS or another machine you control instead.

## Check the hosting account

Run these from the project's activated Python environment in cPanel Terminal:

```bash
command -v tesseract
tesseract --version
tesseract --list-langs
python -c "import fitz; print('PyMuPDF', fitz.VersionBind)"
```

The language list must include `fas`, `ara`, and `eng`. If the binary is outside PATH, set its full path in `.env`:

```dotenv
TESSERACT_CMD=/home/CPANEL_USER/bin/tesseract
OCR_LANGUAGES=fas+ara+eng
```

Use the real path supplied by your hosting provider; do not copy the example path literally. `TESSDATA_PREFIX` may be set if the provider places trained data in a non-standard location.

## User behavior and safety limits

- Supported files: PDF and PNG/JPEG/WebP/BMP/TIFF images.
- Maximum upload: 15 MiB.
- Maximum PDF length: 20 pages.
- OCR timeout budget: 240 seconds per request.
- Output: UTF-8 plain text as a downloadable `.txt` file.
- Temporary source and output files are removed after delivery/failure.
- OCR does not deduct speech-to-text credits.
- Text already embedded in PDF pages is extracted directly; scanned pages are rasterized and sent to local Tesseract.

These limits are intentionally conservative for cPanel shared hosting. Accuracy depends on image quality, fonts and document layout. Tables and multi-column pages may not retain their original layout in plain text.
