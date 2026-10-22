"""Media worker process: probing, extraction, merging and legacy PPT conversion.

The bot never shells out to ``ffmpeg``, ``ffprobe`` or ``soffice``.  Instead it
spawns ``python -m gamas_bot.media_worker`` and talks to it through plain
argument arrays.  Running the media pipeline in a dedicated child process keeps
the guarantees the previous external tools provided:

* every operation is bounded by the parent's timeout and killed as a whole
  process group on timeout/cancellation (see ``media.run_command``);
* a crash or hang inside the native media libraries takes down only the worker,
  never the bot;
* the parent's event loop never blocks on decoding or conversion.

Media decoding/encoding uses PyAV, the official Python bindings around the
FFmpeg *libraries* that ship inside the ``av`` wheel — no system binaries are
invoked.  Legacy ``.ppt``/``.pps``/``.pot`` conversion uses the pure-Python
``ppt2pptx`` package.

Every input is opened with the ``file,pipe`` protocol whitelist so a disguised
playlist cannot make the process fetch HTTP (or any other network) URLs.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from fractions import Fraction
from pathlib import Path

PROG = "gamas-bot media worker"

# Local-only inputs. Mirrors the old ``-protocol_whitelist file,pipe`` flag.
INPUT_PROTOCOL_OPTIONS = {"protocol_whitelist": "file,pipe"}

WAV_SAMPLE_RATE = 16000
OPUS_SAMPLE_RATE = 48000
OPUS_BIT_RATE = 32000
# Why these two flags:
#
# * ``application=audio`` keeps libopus on the hybrid SILK+CELT path, so the
#   4–8 kHz band (fricatives, which carry a lot of word identity) survives at
#   this bitrate; ``voip`` narrows the encoder to SILK and discards it.
# * ``compression_level=0`` is the cheap search mode. On the same 4-minute
#   speech sample it encodes ~3.3x faster than the shipped level of 5 for the
#   same audited output (13.5 MB/h vs 12.6 MB/h, +7 %), which is the right
#   trade because the encoder runs on the same 2 vCPUs that serve the bot.
#
# Measured with ``python -m scripts.perf_bench audio``; see
# docs/PERFORMANCE_PLAN.md §1.1.
OPUS_CODEC_OPTIONS = {"application": "audio", "compression_level": "0"}

LEGACY_EXTENSIONS = {".ppt", ".pps", ".pot"}
UNSUPPORTED_ODF_EXTENSIONS = {".odp", ".otp"}


class WorkerError(RuntimeError):
    """Raised for inputs the worker cannot process; reported on stderr."""


def _require_av():
    try:
        import av  # noqa: F401
        from av.audio.frame import AudioFrame  # noqa: F401
        from av.audio.resampler import AudioResampler  # noqa: F401
    except ImportError as exc:  # pragma: no cover - only when deps are broken
        raise WorkerError(
            "media dependencies are missing (run: pip install -r requirements.txt)"
        ) from exc
    import av

    return av


def _probe(path: Path) -> dict:
    """Return the same information the old ffprobe JSON report provided."""
    av = _require_av()
    with av.open(str(path), options=dict(INPUT_PROTOCOL_OPTIONS)) as container:
        audio = [stream for stream in container.streams if stream.type == "audio"]
        candidates: list[float] = []
        if container.duration is not None:
            candidates.append(container.duration / 1_000_000.0)
        for stream in audio:
            if stream.duration is not None and stream.time_base:
                candidates.append(float(stream.duration * stream.time_base))
        duration = next(
            (value for value in candidates if math.isfinite(value) and value > 0),
            None,
        )
        codec: str | None = None
        if audio:
            context = audio[0].codec_context
            try:
                # canonical_name equals ffprobe's codec_name (e.g. "mp3",
                # not the "mp3float" decoder name).
                codec = context.codec.canonical_name
            except Exception:  # unreadable codec parameters; treat as unknown
                codec = None
        return {
            "has_audio": bool(audio),
            "duration": duration,
            "audio_codec": codec,
        }


def _emit(
    container,
    stream,
    frame,
    rate: int,
    next_pts: list[int],
    *,
    time_base: Fraction | None = None,
) -> None:
    """Encode one audio frame with strictly monotonic timestamps."""
    frame.time_base = time_base if time_base is not None else Fraction(1, rate)
    frame.pts = next_pts[0]
    next_pts[0] += frame.samples
    for packet in stream.encode(frame):
        container.mux(packet)


def _transcode(
    inputs: list[Path],
    output: Path,
    *,
    rate: int,
    codec: str,
    container_format: str | None = None,
    silence_seconds: float = 0.0,
    bit_rate: int | None = None,
    codec_options: dict | None = None,
) -> dict:
    """Decode inputs to mono and report sample-accurate clip positions.

    Each input gets its own resampler because PyAV locks a resampler to the
    format of the first frame it sees, while inputs can differ in rate/layout.
    The source sample count excludes synthetic inter-clip silence; clip spans
    are measured on the exact output sample clock used by the STT file.
    """
    if not math.isfinite(silence_seconds) or not 0 <= silence_seconds <= 60:
        raise WorkerError("silence duration must be between 0 and 60 seconds")
    av = _require_av()
    from av.audio.frame import AudioFrame
    from av.audio.resampler import AudioResampler

    open_kwargs = {"format": container_format} if container_format else {}
    container = av.open(str(output), "w", **open_kwargs)
    summary: dict = {}
    time_base = Fraction(1, rate)
    try:
        stream = container.add_stream(codec, rate=rate)
        stream.layout = "mono"
        try:
            stream.thread_type = "AUTO"
        except Exception:
            pass
        if bit_rate is not None:
            stream.bit_rate = bit_rate
        if codec_options:
            stream.codec_context.options = dict(codec_options)
        next_pts = [0]
        source_samples = 0
        clip_spans: list[dict[str, int]] = []

        # Pre-allocate the inter-clip silence frame once and reuse it across
        # every clip boundary instead of allocating and zeroing per clip.
        silence_frame = None
        if silence_seconds > 0 and len(inputs) > 1:
            silence_samples = int(round(rate * silence_seconds))
            if silence_samples > 0:
                silence_frame = AudioFrame(
                    format="s16", layout="mono", samples=silence_samples
                )
                silence_frame.sample_rate = rate
                for plane in silence_frame.planes:
                    view = memoryview(plane)
                    view[: len(view)] = b"\0" * len(view)

        for index, source in enumerate(inputs):
            resampler = AudioResampler(format="s16", layout="mono", rate=rate)
            clip_start = next_pts[0]
            clip_samples = 0
            with av.open(str(source), options=dict(INPUT_PROTOCOL_OPTIONS)) as media:
                audio_streams = [
                    item for item in media.streams if item.type == "audio"
                ]
                if not audio_streams:
                    raise WorkerError(f"no audio track in '{source.name}'")
                audio_stream = audio_streams[0]
                try:
                    audio_stream.thread_type = "AUTO"
                except Exception:
                    pass
                decoded_to_eof = False
                try:
                    for frame in media.decode(audio_stream):
                        for converted in resampler.resample(frame):
                            _emit(
                                container, stream, converted, rate, next_pts,
                                time_base=time_base,
                            )
                            clip_samples += converted.samples
                            source_samples += converted.samples
                    decoded_to_eof = True
                finally:
                    if not decoded_to_eof:
                        # Flush threaded decoder buffers on early exit before
                        # closing container (prevents PyAV frame-thread deadlock).
                        try:
                            audio_stream.codec_context.flush_buffers()
                        except Exception:
                            pass
            for converted in resampler.resample(None):  # flush per-input state
                _emit(
                    container, stream, converted, rate, next_pts,
                    time_base=time_base,
                )
                clip_samples += converted.samples
                source_samples += converted.samples
            if clip_samples <= 0:
                raise WorkerError(f"no decodable audio samples in '{source.name}'")
            clip_spans.append({
                "index": index,
                "start_sample": clip_start,
                "end_sample": next_pts[0],
            })
            if silence_frame is not None and index < len(inputs) - 1:
                _emit(
                    container, stream, silence_frame, rate, next_pts,
                    time_base=time_base,
                )

        if source_samples == 0:
            raise WorkerError("no decodable audio samples in the inputs")
        for packet in stream.encode(None):
            container.mux(packet)
        summary = {
            "sample_rate": rate,
            "source_samples": source_samples,
            "output_samples": next_pts[0],
            "clip_spans": clip_spans,
        }
    finally:
        container.close()
    return summary


def _convert(source: Path, output: Path, max_input_bytes: int) -> dict:
    try:
        from ppt2pptx import convert
        from ppt2pptx.cfb import Limits
        from ppt2pptx.errors import Ppt2PptxError
    except ImportError as exc:  # pragma: no cover - only when deps are broken
        raise WorkerError(
            "ppt2pptx is missing (run: pip install -r requirements.txt)"
        ) from exc
    if source.suffix.lower() in UNSUPPORTED_ODF_EXTENSIONS:
        raise WorkerError(
            "ODP/OTP presentations are not supported; convert the deck to .pptx"
        )
    try:
        result = convert(
            source,
            output,
            limits=Limits(
                max_input_bytes=max_input_bytes, max_stream_bytes=max_input_bytes
            ),
        )
    except Ppt2PptxError as exc:
        raise WorkerError(str(exc)) from exc
    if not output.is_file() or output.stat().st_size == 0:
        raise WorkerError("the converter produced no output file")
    warnings = result.report.to_dict().get("warnings") or []
    return {
        "slide_count": result.slide_count,
        "warning_codes": sorted({item.get("code") for item in warnings if item.get("code")}),
    }


def _check() -> dict:
    """Report the importable media dependencies, or fail with a clear message.

    Uses :func:`_require_av` and an explicit ``ppt2pptx`` lookup so a missing
    dependency surfaces as the same actionable :class:`WorkerError` the media
    commands use, instead of an import traceback from the diagnostic itself.
    """
    from importlib.metadata import PackageNotFoundError, version

    av = _require_av()
    try:
        ppt2pptx = version("ppt2pptx")
    except PackageNotFoundError as exc:
        raise WorkerError(
            "ppt2pptx is missing (run: pip install -r requirements.txt)"
        ) from exc
    return {"av": av.__version__, "ppt2pptx": ppt2pptx}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG)
    commands = parser.add_subparsers(dest="command", required=True)

    probe = commands.add_parser(
        "probe",
        help="probe one media file (or several in a single process)",
    )
    probe.add_argument("path", nargs="+")

    extract = commands.add_parser("extract", help="extract one audio track")
    extract.add_argument("--output", required=True)
    extract.add_argument("--format", choices=("wav", "opus"), default="wav")
    extract.add_argument("source")

    merge = commands.add_parser("merge", help="concatenate audio tracks")
    merge.add_argument("--output", required=True)
    merge.add_argument("--format", choices=("wav", "opus"), default="wav")
    merge.add_argument("--silence", type=float, default=0.5)
    merge.add_argument("inputs", nargs="+")

    convert = commands.add_parser("convert", help="convert a legacy .ppt deck")
    convert.add_argument("--output", required=True)
    convert.add_argument("--max-input-bytes", type=int, required=True)
    convert.add_argument("source")

    commands.add_parser("check", help="verify worker dependencies")
    return parser


def _configure_logging() -> None:
    """Send FFmpeg-level ERROR records straight to stderr before any traceback.

    The parent keeps the first few hundred stderr characters as the user-facing
    failure detail, so the actual cause (for example a blocked network
    protocol) must be the first thing written. PyAV normally captures FFmpeg
    log records instead of printing them; restoring FFmpeg's default callback
    and limiting it to errors gives deterministic, bounded stderr output.
    """
    try:
        import av
    except ImportError:
        # A missing PyAV is reported by the command handler (``_require_av`` /
        # ``_check``) with a clear install instruction; configuring logging must
        # not pre-empt that with an import traceback.
        return

    av.logging.restore_default_callback()
    av.logging.set_libav_level(av.logging.ERROR)


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "probe":
            if len(args.path) == 1:
                # Single-file output stays a bare object for compatibility.
                print(json.dumps(_probe(Path(args.path[0]))))
            else:
                # One process amortises the interpreter + PyAV import over the
                # whole deck (measured: ~100 ms per file vs ~9 ms once loaded).
                # A file that cannot be probed becomes an error entry instead
                # of failing the batch, so one broken clip does not hide the
                # probes of every other clip.
                results = []
                for raw in args.path:
                    try:
                        entry = {"path": raw, **_probe(Path(raw))}
                    except Exception as exc:  # noqa: BLE001 - per-file isolation
                        entry = {
                            "path": raw,
                            "error": f"{type(exc).__name__}: {exc}"[:300],
                        }
                    results.append(entry)
                print(json.dumps(results))
        elif args.command == "extract":
            rate = OPUS_SAMPLE_RATE if args.format == "opus" else WAV_SAMPLE_RATE
            _transcode(
                [Path(args.source)],
                Path(args.output),
                rate=rate,
                codec="libopus" if args.format == "opus" else "pcm_s16le",
                container_format="ogg" if args.format == "opus" else None,
                bit_rate=OPUS_BIT_RATE if args.format == "opus" else None,
                codec_options=OPUS_CODEC_OPTIONS if args.format == "opus" else None,
            )
        elif args.command == "merge":
            if not args.inputs:
                raise WorkerError("no input files given for merging")
            rate = OPUS_SAMPLE_RATE if args.format == "opus" else WAV_SAMPLE_RATE
            summary = _transcode(
                [Path(item) for item in args.inputs],
                Path(args.output),
                rate=rate,
                codec="libopus" if args.format == "opus" else "pcm_s16le",
                container_format="ogg" if args.format == "opus" else None,
                silence_seconds=args.silence,
                bit_rate=OPUS_BIT_RATE if args.format == "opus" else None,
                codec_options=OPUS_CODEC_OPTIONS if args.format == "opus" else None,
            )
            print(json.dumps(summary))
        elif args.command == "convert":
            summary = _convert(
                Path(args.source), Path(args.output), args.max_input_bytes
            )
            print(json.dumps(summary))
        elif args.command == "check":
            print(json.dumps(_check()))
    except WorkerError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - surface any failure to the parent
        # One summary line first: the parent reports only the stderr head.
        print(f"{PROG}: {type(exc).__name__}: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
