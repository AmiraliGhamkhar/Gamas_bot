"""Azure AI Document Intelligence adapter for Persian/Arabic document OCR."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class OcrError(RuntimeError):
    """Safe-to-display OCR error."""


@dataclass(frozen=True)
class OcrResult:
    text: str
    searchable_pdf: bytes


SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".bmp"}


def classify_file(filename: str | None, mime_type: str | None, *, is_photo=False):
    if is_photo:
        return "telegram-photo.jpg", "image/jpeg"
    extension = Path(filename or "").suffix.lower()
    mime_map = {
        "application/pdf": ".pdf",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/bmp": ".bmp",
    }
    if extension in SUPPORTED_EXTENSIONS:
        return filename or f"document{extension}", {
            ".pdf": "application/pdf", ".png": "image/png",
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".bmp": "image/bmp",
        }[extension]
    extension = mime_map.get((mime_type or "").split(";")[0].strip().lower())
    if extension:
        return filename or f"document{extension}", {
            ".pdf": "application/pdf", ".png": "image/png",
            ".jpg": "image/jpeg", ".bmp": "image/bmp",
        }[extension]
    return None


def run_azure_ocr(path: Path, endpoint: str, api_key: str, max_pages: int = 50,
                  timeout_seconds: float = 300) -> OcrResult:
    if not endpoint or not api_key:
        raise OcrError("OCR provider is not configured.")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
        raise OcrError("Azure OCR endpoint must be a valid HTTPS URL.")

    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(path), strict=False)
            if reader.is_encrypted:
                raise OcrError("Encrypted PDFs are not supported; upload an unlocked PDF.")
            if not 1 <= len(reader.pages) <= max_pages:
                raise OcrError(f"PDF page count must be between 1 and {max_pages}.")
        except OcrError:
            raise
        except Exception as exc:
            raise OcrError("Could not read this PDF.") from exc

    try:
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
        from azure.ai.documentintelligence.models import AnalyzeOutputOption
        from azure.ai.documentintelligence.models import DocumentContentFormat
        from azure.core.credentials import AzureKeyCredential
    except ImportError as exc:
        raise OcrError("OCR dependencies are missing. Reinstall requirements.txt.") from exc

    client = None
    try:
        client = DocumentIntelligenceClient(
            endpoint=endpoint, credential=AzureKeyCredential(api_key)
        )
        with path.open("rb") as f:
            request = AnalyzeDocumentRequest(bytes_source=f.read())
        poller = client.begin_analyze_document(
            model_id="prebuilt-read",
            body=request,
            output_content_format=DocumentContentFormat.TEXT,
            output=[AnalyzeOutputOption.PDF],
            content_type="application/json",
        )
        result = poller.result(timeout=timeout_seconds)
        operation_id = poller.details.get("operation_id")
        if not operation_id:
            raise OcrError("OCR provider did not return a result identifier.")
        pdf = b"".join(client.get_analyze_result_pdf(
            model_id="prebuilt-read", result_id=operation_id
        ))
        text = (getattr(result, "content", None) or "").strip()
        if not text or not pdf:
            raise OcrError("No readable text or searchable PDF was returned.")
        return OcrResult(text=text, searchable_pdf=pdf)
    except OcrError:
        raise
    except Exception as exc:
        raise OcrError(f"OCR provider failed ({type(exc).__name__}).") from exc
    finally:
        if client is not None:
            client.close()
