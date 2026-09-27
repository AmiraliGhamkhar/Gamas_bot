"""ffmpeg / ffprobe / LibreOffice helpers used by the presentation pipeline.

Every external command is built by a pure function so the argument lists stay
unit-testable, and executed through one async subprocess wrapper that always
enforces a timeout and never runs through a shell.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

logger = logging.getLogger(__name__)

WAV_BYTES_PER_SECOND = 32000  # 16 kHz, mono, signed 16-bit PCM.
OPUS_SAMPLE_RATE = 48000
WAV_SAMPLE_RATE = 16000


class MediaToolError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MediaInfo:
    has_audio: bool
    duration: float | None
    audio_codec: str | None = None


def tool_available(binary: str) -> bool:
    """True when the binary can be resolved on PATH or as an explicit path."""
    if not binary:
        return False
    candidate = Path(binary)
    if candidate.is_absolute() or candidate.parent != Path("."):
        return candidate.is_file()
    return shutil.which(binary) is not None


async def run_command(
    command: list[str], timeout: int, *, capture_output: bool = True
) -> tuple[int, str, str]:
    """Run a command without a shell and return (returncode, stdout, stderr)."""
    stream = asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL
    started = time.monotonic()
    tool = Path(command[0]).name
    logger.info("External command started tool=%s timeout_seconds=%s", tool, timeout)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
        )
    except FileNotFoundError as exc:
        raise MediaToolError(f"ابزار «{command[0]}» روی سرور نصب نیست.") from exc
    except OSError as exc:
        raise MediaToolError(f"اجرای «{command[0]}» ممکن نشد: {exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        logger.error(
            "External command timed out tool=%s elapsed_seconds=%.3f timeout_seconds=%s",
            tool,
            time.monotonic() - started,
            timeout,
        )
        raise MediaToolError(f"زمان اجرای «{command[0]}» به پایان رسید.") from exc
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        logger.info(
            "External command cancelled tool=%s elapsed_seconds=%.3f",
            tool,
            time.monotonic() - started,
        )
        raise
    returncode = process.returncode or 0
    elapsed = time.monotonic() - started
    log = logger.info if returncode == 0 else logger.warning
    log(
        "External command finished tool=%s returncode=%s elapsed_seconds=%.3f",
        tool,
        returncode,
        elapsed,
    )
    return (
        returncode,
        (stdout or b"").decode("utf-8", "replace"),
        (stderr or b"").decode("utf-8", "replace"),
    )


def build_probe_command(ffprobe_bin: str, path: Path) -> list[str]:
    return [
        ffprobe_bin,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]


def parse_probe_output(payload: str) -> MediaInfo:
    """Read the ffprobe JSON report into a MediaInfo record."""
    try:
        data = json.loads(payload or "{}")
    except json.JSONDecodeError as exc:
        raise MediaToolError("خروجی ffprobe قابل‌خواندن نیست.") from exc
    streams = data.get("streams") or []
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    duration: float | None = None
    for source in ({k: v for k, v in (data.get("format") or {}).items()}, *audio_streams):
        raw = source.get("duration")
        if raw in (None, "", "N/A"):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            duration = value
            break
    codec = audio_streams[0].get("codec_name") if audio_streams else None
    return MediaInfo(bool(audio_streams), duration, codec)


async def probe_media(path: Path, settings: Settings) -> MediaInfo:
    command = build_probe_command(settings.ffprobe_bin, path)
    code, stdout, stderr = await run_command(command, settings.ffmpeg_timeout)
    if code != 0:
        raise MediaToolError(f"ffprobe فایل را نپذیرفت: {stderr.strip()[:300]}")
    return parse_probe_output(stdout)


def choose_merge_format(total_duration: float | None, settings: Settings) -> str:
    """Pick a lossless WAV output, or Opus when WAV would grow too large."""
    if total_duration is None:
        return "wav"
    estimated = total_duration * WAV_BYTES_PER_SECOND
    limit = min(settings.presentation_wav_limit_bytes, settings.max_file_size)
    return "opus" if estimated > limit else "wav"


def merge_output_name(stem: str, output_format: str) -> str:
    return f"{stem}.{'ogg' if output_format == 'opus' else 'wav'}"


def build_merge_command(
    ffmpeg_bin: str,
    inputs: list[Path],
    output: Path,
    *,
    output_format: str = "wav",
    silence_seconds: float = 0.5,
) -> list[str]:
    """Build an ffmpeg call that concatenates the audio tracks of every input.

    Each input is resampled to a common mono layout first, because the concat
    filter refuses streams whose sample rate or channel layout differ. A short
    silence is padded between clips so neighbouring slides do not run together.
    """
    if not inputs:
        raise MediaToolError("فهرست ورودی برای ادغام صدا خالی است.")
    rate = OPUS_SAMPLE_RATE if output_format == "opus" else WAV_SAMPLE_RATE
    command = [ffmpeg_bin, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    for item in inputs:
        command += ["-i", str(item)]
    chains = []
    for index in range(len(inputs)):
        chain = (
            f"[{index}:a]aresample={rate}"
            ",aformat=sample_fmts=s16:channel_layouts=mono"
        )
        if silence_seconds > 0 and index < len(inputs) - 1:
            chain += f",apad=pad_dur={silence_seconds:g}"
        chains.append(chain + f"[a{index}]")
    labels = "".join(f"[a{index}]" for index in range(len(inputs)))
    graph = ";".join(chains) + f";{labels}concat=n={len(inputs)}:v=0:a=1[out]"
    command += ["-filter_complex", graph, "-map", "[out]", "-vn"]
    if output_format == "opus":
        command += ["-c:a", "libopus", "-b:a", "32k", "-application", "voip"]
    else:
        command += ["-c:a", "pcm_s16le"]
    command += ["-ar", str(rate), "-ac", "1", str(output)]
    return command


async def merge_audio_tracks(
    inputs: list[Path],
    output_dir: Path,
    settings: Settings,
    *,
    total_duration: float | None = None,
    stem: str = "merged",
) -> Path:
    """Concatenate every input into one mono track ready for the STT engines."""
    output_format = choose_merge_format(total_duration, settings)
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / merge_output_name(stem, output_format)
    command = build_merge_command(
        settings.ffmpeg_bin,
        inputs,
        output,
        output_format=output_format,
        silence_seconds=settings.presentation_silence_seconds,
    )
    code, _, stderr = await run_command(command, settings.ffmpeg_timeout)
    if code != 0 or not output.exists() or output.stat().st_size == 0:
        raise MediaToolError(f"ادغام صداهای ارائه ناموفق بود: {stderr.strip()[:300]}")
    return output


# Codecs both STT providers accept directly, so the upload can skip ffmpeg.
PASSTHROUGH_CODECS = {
    "aac", "alac", "flac", "mp3", "opus", "pcm_s16le", "pcm_s16be", "vorbis",
}
PASSTHROUGH_EXTENSIONS = {
    ".aac", ".flac", ".m4a", ".mp3", ".mp4", ".oga", ".ogg", ".opus", ".wav", ".webm",
}


def needs_transcode(filename: str | None, info: MediaInfo | None) -> bool:
    """Decide whether a plain audio upload must be normalised before STT.

    Without an ffprobe report nothing is known about the file, so the original
    is kept and sent as-is, exactly like before this feature existed.
    """
    if info is None:
        return False
    if not info.has_audio:
        return False
    suffix = Path(filename).suffix.lower() if filename else ""
    if suffix not in PASSTHROUGH_EXTENSIONS:
        return True
    return (info.audio_codec or "").lower() not in PASSTHROUGH_CODECS


def build_transcode_command(
    ffmpeg_bin: str,
    source: Path,
    output: Path,
    *,
    output_format: str = "wav",
) -> list[str]:
    """Build an ffmpeg call that takes the first audio track to mono PCM/Opus."""
    rate = OPUS_SAMPLE_RATE if output_format == "opus" else WAV_SAMPLE_RATE
    command = [
        ffmpeg_bin, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-map", "0:a:0",
        "-vn",
    ]
    if output_format == "opus":
        command += ["-c:a", "libopus", "-b:a", "32k", "-application", "voip"]
    else:
        command += ["-c:a", "pcm_s16le"]
    command += ["-ar", str(rate), "-ac", "1", str(output)]
    return command


async def extract_audio_track(
    source: Path,
    output_dir: Path,
    settings: Settings,
    *,
    total_duration: float | None = None,
    stem: str = "audio",
) -> Path:
    """Pull a single mono audio track out of any media file ffmpeg can read."""
    if not tool_available(settings.ffmpeg_bin):
        raise MediaToolError(
            "برای پردازش این فایل، ffmpeg باید روی سرور نصب باشد."
        )
    output_format = choose_merge_format(total_duration, settings)
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / merge_output_name(stem, output_format)
    command = build_transcode_command(
        settings.ffmpeg_bin, source, output, output_format=output_format
    )
    code, _, stderr = await run_command(command, settings.ffmpeg_timeout)
    if code != 0 or not output.exists() or output.stat().st_size == 0:
        raise MediaToolError(f"استخراج صدا از این فایل ناموفق بود: {stderr.strip()[:300]}")
    return output


def build_convert_command(
    soffice_bin: str, source: Path, out_dir: Path, profile_dir: Path
) -> list[str]:
    """Build the LibreOffice call that converts a legacy deck to .pptx."""
    return [
        soffice_bin,
        "--headless",
        "--norestore",
        "--invisible",
        "--nolockcheck",
        f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
        "--convert-to",
        "pptx",
        "--outdir",
        str(out_dir),
        str(source),
    ]


async def convert_to_pptx(source: Path, out_dir: Path, settings: Settings) -> Path:
    """Convert .ppt/.pps/.odp decks to .pptx through LibreOffice."""
    if not tool_available(settings.soffice_bin):
        raise MediaToolError(
            "برای فایل‌های قدیمی PowerPoint، LibreOffice روی سرور لازم است."
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    # A private profile keeps parallel conversions from fighting over one lock.
    profile_dir = out_dir / f"lo-profile-{uuid.uuid4().hex[:8]}"
    profile_dir.mkdir(parents=True, exist_ok=True)
    command = build_convert_command(settings.soffice_bin, source, out_dir, profile_dir)
    code, stdout, stderr = await run_command(command, settings.soffice_timeout)
    converted = out_dir / f"{source.stem}.pptx"
    if not converted.exists():
        candidates = sorted(out_dir.glob("*.pptx"))
        converted = candidates[0] if candidates else converted
    if code != 0 or not converted.exists():
        detail = (stderr or stdout).strip()[:300]
        raise MediaToolError(f"تبدیل فایل قدیمی PowerPoint ناموفق بود: {detail}")
    shutil.rmtree(profile_dir, ignore_errors=True)
    return converted
