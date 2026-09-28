"""Media helpers used by the upload and presentation pipelines.

No system ``ffmpeg``/``ffprobe``/``soffice`` binaries are involved: every
operation runs in a dedicated child process (``python -m gamas_bot.media_worker``)
backed by the PyAV and ppt2pptx Python packages.  Each command is built by a
pure function so the argument lists stay unit-testable, and executed through
one async subprocess wrapper that always enforces a timeout, never runs through
a shell, and kills the whole process group on POSIX when a job times out or is
cancelled — the same process-safety guarantees the old external tools had.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

logger = logging.getLogger(__name__)

WAV_BYTES_PER_SECOND = 32000  # 16 kHz, mono, signed 16-bit PCM.
OPUS_SAMPLE_RATE = 48000
WAV_SAMPLE_RATE = 16000

WORKER_MODULE = "gamas_bot.media_worker"
CHECK_TIMEOUT_SECONDS = 60


class MediaToolError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MediaInfo:
    has_audio: bool
    duration: float | None
    audio_codec: str | None = None


def worker_command(*args: str) -> list[str]:
    """Argument vector for one media-worker invocation (never a shell string)."""
    return [sys.executable, "-m", WORKER_MODULE, *args]


def worker_env() -> dict[str, str]:
    """Environment additions that let the worker import the package anywhere.

    The worker is started with ``python -m gamas_bot.media_worker`` from the
    bot's working directory; prepending the package's parent directory keeps
    that import working even when the bot itself was launched from elsewhere
    with the package already importable.
    """
    package_parent = str(Path(__file__).resolve().parent.parent)
    existing = os.environ.get("PYTHONPATH")
    value = (
        package_parent
        if not existing
        else package_parent + os.pathsep + existing
    )
    return {"PYTHONPATH": value}


async def run_command(
    command: list[str],
    timeout: int,
    *,
    capture_output: bool = True,
    label: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run a command without a shell and return (returncode, stdout, stderr)."""
    stream = asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL
    started = time.monotonic()
    tool = label or Path(command[0]).name
    logger.info("External command started tool=%s timeout_seconds=%s", tool, timeout)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            env=({**os.environ, **env} if env else None),
            start_new_session=(os.name == "posix"),
        )
    except FileNotFoundError as exc:
        raise MediaToolError(f"ابزار «{command[0]}» روی سرور نصب نیست.") from exc
    except OSError as exc:
        raise MediaToolError(f"اجرای «{command[0]}» ممکن نشد: {exc}") from exc
    communication = asyncio.create_task(process.communicate())

    async def terminate() -> None:
        # A worker may still be spinning up. Kill its process group on POSIX so
        # nothing outlives a cancelled/timed-out job.
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass  # The process may have exited between the timeout and kill.
        # Drain pipes too; wait() alone can hang on a full stdout/stderr pipe.
        await communication

    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), timeout=timeout)
    except asyncio.TimeoutError as exc:
        await terminate()
        logger.error(
            "External command timed out tool=%s elapsed_seconds=%.3f timeout_seconds=%s",
            tool, time.monotonic() - started, timeout,
        )
        raise MediaToolError(f"زمان اجرای «{tool}» به پایان رسید.") from exc
    except asyncio.CancelledError:
        await terminate()
        logger.info(
            "External command cancelled tool=%s elapsed_seconds=%.3f",
            tool, time.monotonic() - started,
        )
        raise
    returncode = process.returncode or 0
    elapsed = time.monotonic() - started
    log = logger.info if returncode == 0 else logger.warning
    log(
        "External command finished tool=%s returncode=%s elapsed_seconds=%.3f",
        tool, returncode, elapsed,
    )
    return (
        returncode,
        (stdout or b"").decode("utf-8", "replace"),
        (stderr or b"").decode("utf-8", "replace"),
    )


def build_probe_command(path: Path) -> list[str]:
    return worker_command("probe", "--", str(path))


def parse_probe_output(payload: str) -> MediaInfo:
    """Read the worker's JSON report into a MediaInfo record.

    The worker reports ``duration`` already resolved from the container and
    stream headers; this parser keeps the old ffprobe-era defences: malformed
    structure is an error, while individual unusable values (non-numeric,
    non-finite, non-positive durations) degrade to ``None`` instead of failing
    the whole file.
    """
    try:
        data = json.loads(payload or "{}")
    except json.JSONDecodeError as exc:
        raise MediaToolError("خروجی بررسی رسانه قابل‌خواندن نیست.") from exc
    if not isinstance(data, dict):
        raise MediaToolError("ساختار خروجی بررسی رسانه معتبر نیست.")
    has_audio = data.get("has_audio", False)
    if not isinstance(has_audio, bool):
        raise MediaToolError("ساختار خروجی بررسی رسانه معتبر نیست.")
    duration: float | None = None
    raw_duration = data.get("duration")
    if raw_duration is not None and not isinstance(raw_duration, bool):
        try:
            value = float(raw_duration)
        except (TypeError, ValueError):
            value = math.nan
        if math.isfinite(value) and value > 0:
            duration = value
    raw_codec = data.get("audio_codec")
    codec = raw_codec if isinstance(raw_codec, str) and raw_codec else None
    return MediaInfo(has_audio, duration, codec)


async def probe_media(path: Path, settings: Settings) -> MediaInfo:
    code, stdout, stderr = await run_command(
        build_probe_command(path),
        settings.media_timeout,
        label="media_worker:probe",
        env=worker_env(),
    )
    if code != 0:
        raise MediaToolError(f"بررسی فایل رسانه ناموفق بود: {stderr.strip()[:300]}")
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
    inputs: list[Path],
    output: Path,
    *,
    output_format: str = "wav",
    silence_seconds: float = 0.5,
) -> list[str]:
    """Build the worker call that concatenates the audio tracks of every input.

    Each input is resampled to a common mono layout first (the worker pins the
    first audio track of each file), and a short silence is padded between
    clips so neighbouring slides do not run together.
    """
    if not inputs:
        raise MediaToolError("فهرست ورودی برای ادغام صدا خالی است.")
    command = worker_command(
        "merge",
        "--output", str(output),
        "--format", output_format,
        "--silence", f"{silence_seconds:g}",
        "--",
    )
    command.extend(str(item) for item in inputs)
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
        inputs,
        output,
        output_format=output_format,
        silence_seconds=settings.presentation_silence_seconds,
    )
    code, _, stderr = await run_command(
        command,
        settings.media_timeout,
        label="media_worker:merge",
        env=worker_env(),
    )
    if code != 0 or not output.exists() or output.stat().st_size == 0:
        raise MediaToolError(f"ادغام صداهای ارائه ناموفق بود: {stderr.strip()[:300]}")
    return output


# Codecs both STT providers accept directly, so the upload can skip transcoding.
PASSTHROUGH_CODECS = {
    "aac", "alac", "flac", "mp3", "opus", "pcm_s16le", "pcm_s16be", "vorbis",
}
PASSTHROUGH_EXTENSIONS = {
    ".aac", ".flac", ".m4a", ".mp3", ".mp4", ".oga", ".ogg", ".opus", ".wav", ".webm",
}


def needs_transcode(filename: str | None, info: MediaInfo | None) -> bool:
    """Decide whether a plain audio upload must be normalised before STT.

    Without a probe report nothing is known about the file, so the original is
    kept and sent as-is.
    """
    if info is None:
        return False
    if not info.has_audio:
        return False
    suffix = Path(filename).suffix.lower() if filename else ""
    if suffix not in PASSTHROUGH_EXTENSIONS:
        return True
    return (info.audio_codec or "").lower() not in PASSTHROUGH_CODECS


def build_extract_command(
    source: Path,
    output: Path,
    *,
    output_format: str = "wav",
) -> list[str]:
    """Build the worker call that takes the first audio track to mono PCM/Opus."""
    return worker_command(
        "extract",
        "--output", str(output),
        "--format", output_format,
        "--",
        str(source),
    )


async def extract_audio_track(
    source: Path,
    output_dir: Path,
    settings: Settings,
    *,
    total_duration: float | None = None,
    stem: str = "audio",
) -> Path:
    """Pull a single mono audio track out of any media file PyAV can read."""
    output_format = choose_merge_format(total_duration, settings)
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / merge_output_name(stem, output_format)
    command = build_extract_command(source, output, output_format=output_format)
    code, _, stderr = await run_command(
        command,
        settings.media_timeout,
        label="media_worker:extract",
        env=worker_env(),
    )
    if code != 0 or not output.exists() or output.stat().st_size == 0:
        raise MediaToolError(f"استخراج صدا از این فایل ناموفق بود: {stderr.strip()[:300]}")
    return output


def build_convert_command(
    source: Path, output: Path, *, max_input_bytes: int
) -> list[str]:
    """Build the worker call that converts a legacy deck to .pptx via ppt2pptx."""
    return worker_command(
        "convert",
        "--output", str(output),
        "--max-input-bytes", str(max_input_bytes),
        "--",
        str(source),
    )


async def convert_to_pptx(source: Path, out_dir: Path, settings: Settings) -> Path:
    """Convert a legacy .ppt/.pps/.pot deck to .pptx with the ppt2pptx package.

    ODP/OTP decks are refused with an explicit message: no verified pure-Python
    converter exists for them, and pretending otherwise would silently drop
    content.
    """
    if source.suffix.lower() in {".odp", ".otp"}:
        raise MediaToolError(
            "فرمت ODP/OTP پشتیبانی نمی‌شود؛ لطفاً ارائه را با پسوند pptx ذخیره و "
            "دوباره ارسال کنید."
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    converted = out_dir / f"{source.stem}.pptx"
    command = build_convert_command(
        source, converted, max_input_bytes=settings.max_file_size
    )
    code, stdout, stderr = await run_command(
        command,
        settings.convert_timeout,
        label="media_worker:convert",
        env=worker_env(),
    )
    if code != 0 or not converted.exists() or converted.stat().st_size == 0:
        detail = (stderr or stdout).strip()[:300]
        raise MediaToolError(f"تبدیل فایل قدیمی PowerPoint ناموفق بود: {detail}")
    summary = stdout.strip()
    if summary:
        logger.info("Legacy deck converted output=%s summary=%s", converted.name, summary)
    return converted


async def check_media_worker() -> str:
    """Run the worker's dependency self-check; returns its JSON summary."""
    code, stdout, stderr = await run_command(
        worker_command("check"),
        CHECK_TIMEOUT_SECONDS,
        label="media_worker:check",
        env=worker_env(),
    )
    if code != 0:
        raise MediaToolError(
            f"بررسی زیرساخت رسانه ناموفق بود: {(stderr or stdout).strip()[:300]}"
        )
    return stdout.strip()
