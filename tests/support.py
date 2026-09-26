"""Shared helpers for the test-suite: settings factory, deck builder, tool stubs."""

from __future__ import annotations

import os
import shutil
import zipfile
from dataclasses import replace
from pathlib import Path

from gamas_bot.config import Settings

AUDIO_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/audio"
VIDEO_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/video"
RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


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


def fake_media_bytes(duration: float | None = 5.0, has_audio: bool = True) -> bytes:
    """Payload the stub ffprobe below can interpret as a media file."""
    marker = f"DUR={duration}" if duration is not None else "DUR=none"
    audio = "AUDIO=1" if has_audio else "AUDIO=0"
    return f"FAKE-MEDIA {marker} {audio}\n".encode("utf-8")


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


FFPROBE_STUB = """#!/usr/bin/env python3
import json, pathlib, re, sys

path = pathlib.Path(sys.argv[-1])
text = path.read_bytes().decode("utf-8", "replace")
has_audio = "AUDIO=0" not in text
match = re.search(r"DUR=([0-9.]+)", text)
duration = match.group(1) if match else None
streams = [{"codec_type": "audio", "codec_name": "aac"}] if has_audio else []
report = {"streams": streams, "format": {"duration": duration} if duration else {}}
print(json.dumps(report))
"""

FFMPEG_STUB = """#!/usr/bin/env python3
import pathlib, sys

args = sys.argv[1:]
inputs = [args[i + 1] for i, value in enumerate(args) if value == "-i"]
output = pathlib.Path(args[-1])
output.write_text("MERGED\\n" + "\\n".join(inputs), encoding="utf-8")
pathlib.Path(str(output) + ".argv").write_text("\\n".join(args), encoding="utf-8")
"""

SOFFICE_STUB = """#!/usr/bin/env python3
import pathlib, shutil, sys

args = sys.argv[1:]
out_dir = pathlib.Path(args[args.index("--outdir") + 1])
source = pathlib.Path(args[-1])
out_dir.mkdir(parents=True, exist_ok=True)
shutil.copyfile(source, out_dir / (source.stem + ".pptx"))
"""


def install_stub(directory: Path, name: str, script: str) -> str:
    """Write an executable stub binary and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_text(script, encoding="utf-8")
    target.chmod(0o755)
    return str(target)


def stub_tools(directory: Path) -> dict[str, str]:
    return {
        "ffprobe_bin": install_stub(directory, "ffprobe", FFPROBE_STUB),
        "ffmpeg_bin": install_stub(directory, "ffmpeg", FFMPEG_STUB),
        "soffice_bin": install_stub(directory, "soffice", SOFFICE_STUB),
    }


def real_tool_missing(binary: str) -> bool:
    return shutil.which(binary) is None and not os.path.isfile(binary)
