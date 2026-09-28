"""Shared helpers for the test-suite: settings factory, deck builder, media.

Media payloads are *real* files (generated with the standard library and PyAV)
because the bot now processes media through the PyAV-based worker instead of
stub binaries — every fixture the suite probes/merges must actually decode.
"""

from __future__ import annotations

import io
import shutil
import tempfile
import wave
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from gamas_bot.config import Settings

AUDIO_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/audio"
VIDEO_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/video"
RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

# The committed legacy-deck fixtures come from the MIT-licensed ppt2pptx test
# corpus (https://github.com/HuiTurn/ppt2pptx); see tests/fixtures/README.md.
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class FakeJobEvent:
    """Minimal Telethon event double that also records sent document files.

    File payloads are snapshotted at send time because the job's temporary
    directory is removed right after delivery.
    """

    def __init__(self, source: Path | None = None):
        self.source = source
        self.replies: list[str] = []
        self.responses: list[str] = []
        self.files: list[tuple[str, str]] = []
        self.file_payloads: list[tuple[str, bytes]] = []
        if source is not None:
            self.message = SimpleNamespace(download_media=self._download)

    async def _download(self, file: str) -> str:
        shutil.copyfile(self.source, file)
        return file

    async def reply(self, text: str = "", *, file=None, **_kwargs) -> None:
        if file is not None:
            path = str(file)
            try:
                payload = Path(path).read_bytes()
            except OSError:
                payload = b""
            self.files.append((text, path))
            self.file_payloads.append((path, payload))
        else:
            self.replies.append(text)

    async def respond(self, text: str = "", **_kwargs) -> None:
        self.responses.append(text)

    @property
    def file_paths(self) -> list[str]:
        return [path for _caption, path in self.files]

    def file_bytes(self, suffix: str) -> bytes:
        """Content of the first sent file whose name ends with ``suffix``."""
        for path, payload in self.file_payloads:
            if path.endswith(suffix):
                return payload
        raise AssertionError(f"no sent file ends with {suffix!r}: {self.file_paths}")


def sample_notes_json(title: str = "جزوهٔ آزمایشی") -> str:
    """A valid strict-JSON note answer, as the note API is now required to emit."""
    return (
        "{"
        f'"title": "{title}",'
        '"summary": "خلاصهٔ آزمایشی",'
        '"sections": [{'
        '"heading": "بخش نخست",'
        '"paragraphs": ["متن بخش نخست"],'
        '"bullets": ["نکتهٔ یک"],'
        '"key_points": ["نکتهٔ کلیدی"],'
        '"callouts": [{"kind": "نکته", "text": "یادآوری تست"}]'
        "}],"
        '"key_points": ["نکتهٔ کلیدی کل"],'
        '"glossary": [{"term": "HbA1c", "definition": "هموگلوبین گلیکوزیله"}]'
        "}"
    )


class FakeSessionContext:
    """Stand-in for aiohttp.ClientSession used as an async context manager."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def fake_session_factory(**_kwargs):
    return FakeSessionContext()


def docx_text(docx_bytes: bytes) -> str:
    """All paragraph and table text of a generated Word document."""
    import io

    import docx as docx_library

    document = docx_library.Document(io.BytesIO(docx_bytes))
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(parts)


def make_settings(**overrides) -> Settings:
    base = Settings(
        telegram_bot_token="token",
        telegram_api_id=1,
        telegram_api_hash="hash",
        admin_ids=frozenset(),
        database_path=Path("db.sqlite3"),
        session_path=Path("session"),
        temp_dir=Path("tmp"),
        max_file_size=2_000_000_000,
        stt_primary="speechmatics",
        stt_language="fa",
        stt_fallback_enabled=True,
        stt_min_confidence=0.65,
        speechmatics_api_key="sm-key",
        speechmatics_base_url="https://example.test/v2",
        deepgram_api_key="dg-key",
        deepgram_model="nova-3",
        gemini_api_key="gemini-key",
        gemini_model="gemini-2.5-flash-lite",
        max_concurrent_jobs=2,
        stt_poll_interval=1,
        stt_job_timeout=100,
    )
    return replace(base, **overrides) if overrides else base


def wav_bytes(
    duration: float, *, rate: int = 16000, channels: int = 1
) -> bytes:
    """A real PCM WAV file of exactly ``duration`` seconds (digital silence)."""
    frames = int(round(duration * rate))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\0\0" * frames * channels)
    return buffer.getvalue()


def video_bytes(
    *, duration: float = 1.0, with_audio: bool = True, rate: int = 15
) -> bytes:
    """A real MP4 (H.264, optionally AAC) built with the bundled encoders."""
    import av

    from fractions import Fraction

    width, height = 64, 48
    with tempfile.TemporaryDirectory() as folder:
        target = Path(folder) / "clip.mp4"
        with av.open(str(target), "w") as container:
            video = container.add_stream("libx264", rate=rate)
            video.width = width
            video.height = height
            video.pix_fmt = "yuv420p"
            audio = None
            if with_audio:
                audio = container.add_stream("aac", rate=44100)
                audio.layout = "stereo"
            total = max(1, int(round(duration * rate)))
            for index in range(total):
                frame = av.VideoFrame(width, height, "yuv420p")
                for plane in frame.planes:
                    view = memoryview(plane)
                    view[: len(view)] = bytes([(index * 8) % 255]) * len(view)
                frame.pts = index
                frame.time_base = Fraction(1, rate)
                for packet in video.encode(frame):
                    container.mux(packet)
                if audio is not None:
                    samples = 44100 // rate
                    audio_frame = av.AudioFrame(
                        format="s16", layout="stereo", samples=samples
                    )
                    audio_frame.sample_rate = 44100
                    audio_frame.time_base = Fraction(1, 44100)
                    audio_frame.pts = index * samples
                    for plane in audio_frame.planes:
                        view = memoryview(plane)
                        view[: len(view)] = b"\0" * len(view)
                    for packet in audio.encode(audio_frame):
                        container.mux(packet)
            for packet in video.encode(None):
                container.mux(packet)
            if audio is not None:
                for packet in audio.encode(None):
                    container.mux(packet)
        return target.read_bytes()


def mp3_bytes(duration: float, *, rate: int = 16000) -> bytes:
    """A real MP3 file produced by the bundled libmp3lame encoder."""
    import av

    from fractions import Fraction

    with tempfile.TemporaryDirectory() as folder:
        target = Path(folder) / "audio.mp3"
        with av.open(str(target), "w") as container:
            stream = container.add_stream("mp3", rate=rate)
            stream.layout = "mono"
            chunk = rate // 10
            total = int(round(duration * rate))
            sent = 0
            while sent < total:
                samples = min(chunk, total - sent)
                frame = av.AudioFrame(format="s16", layout="mono", samples=samples)
                frame.sample_rate = rate
                frame.time_base = Fraction(1, rate)
                frame.pts = sent
                for plane in frame.planes:
                    view = memoryview(plane)
                    view[: len(view)] = b"\0" * len(view)
                for packet in stream.encode(frame):
                    container.mux(packet)
                sent += samples
            for packet in stream.encode(None):
                container.mux(packet)
        return target.read_bytes()


def build_deck(
    path: Path,
    slides: list[dict] | None = None,
    orphan_media: dict[str, bytes] | None = None,
) -> Path:
    """Create a .pptx whose slides carry real text plus injected media relations.

    ``slides`` entries accept ``title``, ``bullets``, ``notes`` and ``media``
    (a list of ``(part_filename, payload, rel_type)`` tuples).
    """
    from pptx import Presentation

    slides = slides or []
    deck = Presentation()
    layout = deck.slide_layouts[1]
    for spec in slides:
        slide = deck.slides.add_slide(layout)
        slide.shapes.title.text = spec.get("title", "")
        body = slide.placeholders[1].text_frame
        bullets = spec.get("bullets") or []
        if bullets:
            body.text = bullets[0]
            for extra in bullets[1:]:
                body.add_paragraph().text = extra
        if spec.get("notes"):
            slide.notes_slide.notes_text_frame.text = spec["notes"]
    deck.save(str(path))

    content_types = {
        "m4a": "audio/mp4",
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "ogg": "audio/ogg",
        "mp4": "video/mp4",
        "mov": "video/quicktime",
    }
    members: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as archive:
        order = archive.namelist()
        for name in order:
            members[name] = archive.read(name)

    for index, spec in enumerate(slides, start=1):
        media = spec.get("media") or []
        if not media:
            continue
        rels_name = f"ppt/slides/_rels/slide{index}.xml.rels"
        rels = members[rels_name].decode("utf-8")
        additions = []
        for position, (part_filename, payload, rel_type) in enumerate(media, start=90):
            members[f"ppt/media/{part_filename}"] = payload
            additions.append(
                f'<Relationship Id="rIdMedia{index}_{position}" Type="{rel_type}" '
                f'Target="../media/{part_filename}"/>'
            )
        rels = rels.replace("</Relationships>", "".join(additions) + "</Relationships>")
        members[rels_name] = rels.encode("utf-8")

    for name, payload in (orphan_media or {}).items():
        members[f"ppt/media/{name}"] = payload

    # OPC requires a content type for every media extension used in the package.
    declared = members["[Content_Types].xml"].decode("utf-8")
    extensions = {
        Path(name).suffix.lstrip(".").lower()
        for name in members
        if name.startswith("ppt/media/")
    }
    defaults = "".join(
        f'<Default Extension="{extension}" ContentType="{content_types[extension]}"/>'
        for extension in sorted(extensions)
        if extension in content_types and f'Extension="{extension}"' not in declared
    )
    members["[Content_Types].xml"] = declared.replace("<Override", defaults + "<Override", 1).encode("utf-8")

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return path
