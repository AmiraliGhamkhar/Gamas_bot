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
WORKER_MODULE = "gamas_bot.media_worker"

# Local-only inputs. Mirrors the old ``-protocol_whitelist file,pipe`` flag.
INPUT_PROTOCOL_OPTIONS = {"protocol_whitelist": "file,pipe"}

WAV_SAMPLE_RATE = 16000
OPUS_SAMPLE_RATE = 48000
OPUS_BIT_RATE = 32000
# Same speech-optimised libopus setting the old ffmpeg command used.
OPUS_CODEC_OPTIONS = {"application": "voip"}

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


def _emit(container, stream, frame, rate: int, next_pts: list[int]) -> None:
    """Encode one audio frame with strictly monotonic timestamps."""
    frame.time_base = Fraction(1, rate)
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
) -> None:
    """Decode every input's first audio track to mono PCM/Opus at ``output``.

    Each input gets its own resampler because PyAV locks a resampler to the
    format of the first frame it sees, while inputs can differ in rate/layout
    (mirroring ffmpeg's per-input ``aresample`` chains).  A short silence is
    appended after every clip but the last, exactly like the old
    ``apad=pad_dur`` filter did.
    """
    av = _require_av()
    from av.audio.frame import AudioFrame
    from av.audio.resampler import AudioResampler

    open_kwargs = {"format": container_format} if container_format else {}
    container = av.open(str(output), "w", **open_kwargs)
    try:
        stream = container.add_stream(codec, rate=rate)
        stream.layout = "mono"
        if bit_rate is not None:
            stream.bit_rate = bit_rate
        if codec_options:
            stream.codec_context.options = dict(codec_options)
        next_pts = [0]
        decoded_samples = 0

        for index, source in enumerate(inputs):
            resampler = AudioResampler(format="s16", layout="mono", rate=rate)
            with av.open(str(source), options=dict(INPUT_PROTOCOL_OPTIONS)) as media:
                audio_streams = [
                    item for item in media.streams if item.type == "audio"
                ]
                if not audio_streams:
                    raise WorkerError(f"no audio track in '{source.name}'")
                for frame in media.decode(audio_streams[0]):
                    for converted in resampler.resample(frame):
                        _emit(container, stream, converted, rate, next_pts)
                        decoded_samples += converted.samples
            for converted in resampler.resample(None):  # flush per-input state
                _emit(container, stream, converted, rate, next_pts)
                decoded_samples += converted.samples
            if silence_seconds > 0 and index < len(inputs) - 1:
                samples = int(round(rate * silence_seconds))
                if samples > 0:
                    silence = AudioFrame(
                        format="s16", layout="mono", samples=samples
                    )
                    silence.sample_rate = rate
                    for plane in silence.planes:
                        view = memoryview(plane)
                        view[: len(view)] = b"\0" * len(view)
                    _emit(container, stream, silence, rate, next_pts)
                    decoded_samples += samples

        if decoded_samples == 0:
            raise WorkerError("no decodable audio samples in the inputs")
        for packet in stream.encode(None):
            container.mux(packet)
    finally:
        container.close()


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
    import av
    from importlib.metadata import version

    return {"av": av.__version__, "ppt2pptx": version("ppt2pptx")}


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
    import av

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
            _transcode(
                [Path(item) for item in args.inputs],
                Path(args.output),
                rate=rate,
                codec="libopus" if args.format == "opus" else "pcm_s16le",
                container_format="ogg" if args.format == "opus" else None,
                silence_seconds=args.silence,
                bit_rate=OPUS_BIT_RATE if args.format == "opus" else None,
                codec_options=OPUS_CODEC_OPTIONS if args.format == "opus" else None,
            )
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
