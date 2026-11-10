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
# docs/OPERATIONS.md.
OPUS_CODEC_OPTIONS = {"application": "audio", "compression_level": "0"}

#: The STT preprocessing chain, in the order it is applied. Every stage is a
#: well-documented, measurement-friendly step and every one of them can be
#: switched off per deployment (``STT_PREPROCESS=0`` restores the plain
#: resample this worker did before).
#:
#: ``highpass``  removes room rumble, desk thumps and handling noise below the
#:               fundamental of adult speech; keeping DC/very-low energy out of
#:               the recogniser is what stops a "hum" being decoded as speech.
#: ``afftdn``    spectral denoise (FFmpeg's FFT denoiser) for steady
#:               air-conditioner/projector hiss recorded in a lecture hall.
#: ``dynaudnorm`` evens out the near/far swings of a classroom: the lecturer at
#:               the board versus a student at the back. It is dynamic (per
#:               window) rather than a single gain, so it does not make the
#:               noise floor loud during silence.
#: ``alimiter``  a final brick-wall guard so the chain cannot clip.
STT_PREPROCESS_FILTERS: tuple[str, ...] = (
    "highpass=f=70:poles=2",
    "afftdn=nr=12:nf=-35:tn=1",
    "dynaudnorm=f=250:g=11:p=0.85:m=4",
    "alimiter=limit=0.95:level=false",
)
#: Filter arguments accept only digits, ``.``, ``,``, ``=`` and letters from a
#: fixed vocabulary; they are never built from user input, but the check keeps
#: that invariant reviewable.
_FILTER_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789._:,=-")

#: Bounded adaptive analysis: at most this many decoded frames (and this many
#: seconds of audio) are scanned to decide which cleanup stages a recording
#: actually needs. A two-hour lecture never costs a two-hour second pass.
ADAPTIVE_MAX_FRAMES = 600
ADAPTIVE_MAX_SECONDS = 30.0
#: Peak-to-sustained loudness ratio below which level evening is unnecessary.
ADAPTIVE_FLAT_CREST_DB = 12.0
#: Noise floor (dB below full scale) above which steady hiss is audible enough
#: to justify spectral denoising.
ADAPTIVE_NOISE_FLOOR_DBFS = -52.0


def _frame_rms(frame) -> float:
    """RMS of one packed s16 frame, in dBFS (``-inf`` for digital silence)."""
    import math as _math

    total = 0.0
    count = 0
    for plane in frame.planes:
        view = memoryview(plane)
        # PyAV exposes s16 planes as a flat byte buffer; interpret as signed
        # 16-bit little-endian samples without importing numpy.
        raw = bytes(view)
        for offset in range(0, len(raw) - 1, 2):
            sample = int.from_bytes(raw[offset : offset + 2], "little", signed=True)
            total += sample * sample
            count += 1
    if not count:
        return float("-inf")
    return 10.0 * _math.log10(max(1.0, total / count) / (32768.0**2))


def adaptive_filters(paths: list[Path]) -> list[str]:
    """Choose the cleanup stages a recording needs from a bounded audio scan.

    The original file is never touched. The chain keeps the two protective
    stages (rumble highpass, anti-clip limiter) unconditionally, and only adds
    spectral denoising when the measured noise floor is audible and level
    evening when the loudness swings are wide. Every stage still comes from
    :data:`STT_PREPROCESS_FILTERS`, so no undocumented parameters are used.
    """
    av = _require_av()
    levels: list[float] = []
    scanned_seconds = 0.0
    for path in paths:
        if scanned_seconds >= ADAPTIVE_MAX_SECONDS:
            break
        try:
            with av.open(str(path), options=dict(INPUT_PROTOCOL_OPTIONS)) as media:
                audio = [item for item in media.streams if item.type == "audio"]
                if not audio:
                    continue
                for frame in media.decode(audio[0]):
                    level = _frame_rms(frame)
                    if level != float("-inf"):
                        levels.append(level)
                    scanned_seconds += float(frame.samples) / max(1, frame.sample_rate or 1)
                    if len(levels) >= ADAPTIVE_MAX_FRAMES or scanned_seconds >= ADAPTIVE_MAX_SECONDS:
                        break
        except Exception:  # noqa: BLE001 - an unscannable input keeps the full chain
            continue
    chain = ["highpass=f=70:poles=2"]
    if len(levels) >= 8:
        quiet = sorted(levels)[: max(1, len(levels) // 10)]
        loud = sorted(levels)[-max(1, len(levels) // 10) :]
        noise_floor = sum(quiet) / len(quiet)
        peak = sum(loud) / len(loud)
        if noise_floor > ADAPTIVE_NOISE_FLOOR_DBFS:
            chain.append("afftdn=nr=12:nf=-35:tn=1")
        if peak - noise_floor > ADAPTIVE_FLAT_CREST_DB:
            chain.append("dynaudnorm=f=250:g=11:p=0.85:m=4")
    else:
        # No usable measurement: apply the documented full chain.
        chain.extend(STT_PREPROCESS_FILTERS[1:-1])
    chain.append("alimiter=limit=0.95:level=false")
    return chain

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
        channels: int | None = None
        sample_rate: int | None = None
        if audio:
            context = audio[0].codec_context
            try:
                # canonical_name equals ffprobe's codec_name (e.g. "mp3",
                # not the "mp3float" decoder name).
                codec = context.codec.canonical_name
            except Exception:  # unreadable codec parameters; treat as unknown
                codec = None
            try:
                channels = int(context.channels) if context.channels else None
            except Exception:  # noqa: BLE001
                channels = None
            try:
                sample_rate = int(context.sample_rate) if context.sample_rate else None
            except Exception:  # noqa: BLE001
                sample_rate = None
        return {
            "has_audio": bool(audio),
            "duration": duration,
            "audio_codec": codec,
            "channels": channels,
            "sample_rate": sample_rate,
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


def _playable_filters(chain: tuple[str, ...] | list[str]) -> list[str]:
    """Validate a filter chain and drop the stages PyAV cannot build.

    A build of PyAV can be compiled without one of the optional FFmpeg filters
    (``afftdn`` needs ``--enable-avfilter`` extras, for example). A missing
    stage must degrade to "less processing", never to a failed transcription,
    so each name is probed in isolation and unsupported stages are skipped.
    """
    import av

    playable: list[str] = []
    for item in chain:
        text = str(item).strip()
        if not text or not set(text) <= _FILTER_ALLOWED:
            continue
        try:
            av.filter.Filter(text.split("=", 1)[0])
        except Exception:  # noqa: BLE001 - an unsupported stage is not fatal
            continue
        playable.append(text)
    return playable


def _add_filter(graph, specification: str):
    """Add one ``name=key=value:...`` stage to a graph.

    PyAV takes the name and its arguments separately (``add("highpass",
    "f=70")``), so the documented single-string form is split here once.
    """
    name, _, arguments = specification.partition("=")
    if arguments:
        return graph.add(name, arguments)
    return graph.add(name)


def _graph_pull(graph, sink_pad) -> list:
    """Drain every frame the sink currently holds, tolerating EAGAIN/EAGAIN-EOF."""
    import av

    frames: list = []
    if graph is None or sink_pad is None:
        return frames
    while True:
        try:
            frames.append(sink_pad.pull())
        except (av.error.BlockingIOError, av.error.EOFError):
            break
        except Exception:  # noqa: BLE001 - any other error ends this pull
            break
    return frames


def _through_filters(graph, source_pad, sink_pad, frame) -> list:
    """Push one resampled frame through the chain, returning what came out.

    ``dynaudnorm`` buffers a window, so a push can legitimately yield no frame;
    the caller keeps encoding whatever is produced and flushes at the end.
    """
    if graph is None or source_pad is None:
        return [frame]
    try:
        source_pad.push(frame)
    except Exception:  # noqa: BLE001 - never lose audio to a filter defect
        return [frame]
    return _graph_pull(graph, sink_pad)


def _drain_filters(graph, source_pad, sink_pad) -> list:
    """Flush the chain after the last input frame."""
    if graph is None or source_pad is None:
        return []
    try:
        source_pad.push(None)
    except Exception:  # noqa: BLE001
        return _graph_pull(graph, sink_pad)
    return _graph_pull(graph, sink_pad)


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
    filters: tuple[str, ...] | list[str] = (),
    layout: str = "mono",
) -> dict:
    """Decode inputs and report sample-accurate clip positions.

    Each input gets its own resampler because PyAV locks a resampler to the
    format of the first frame it sees, while inputs can differ in rate/layout.
    The source sample count excludes synthetic inter-clip silence; clip spans
    are measured on the exact output sample clock used by the STT file.

    ``filters`` is a per-input speech-cleanup chain (see
    :data:`STT_PREPROCESS_FILTERS`). It runs after the resampler so every stage
    sees the same documented format, and it is rebuilt per input exactly like
    the resampler because a filter graph carries state between frames.

    ``layout`` is ``mono`` (default, the historic STT upload) or ``stereo``
    (preserves channel separation for diarization when
    ``STT_PRESERVE_STEREO`` is on). Stereo doubles the WAV payload, so it is
    opt-in and bounded by the provider upload ceilings in the parent.
    """
    if not math.isfinite(silence_seconds) or not 0 <= silence_seconds <= 60:
        raise WorkerError("silence duration must be between 0 and 60 seconds")
    av = _require_av()
    from av.audio.frame import AudioFrame
    from av.audio.resampler import AudioResampler

    resolved_layout = "stereo" if layout == "stereo" else "mono"
    open_kwargs = {"format": container_format} if container_format else {}
    container = av.open(str(output), "w", **open_kwargs)
    summary: dict = {}
    time_base = Fraction(1, rate)
    try:
        stream = container.add_stream(codec, rate=rate)
        stream.layout = resolved_layout
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
                    format="s16", layout=resolved_layout, samples=silence_samples
                )
                silence_frame.sample_rate = rate
                for plane in silence_frame.planes:
                    view = memoryview(plane)
                    view[: len(view)] = b"\0" * len(view)

        played_filters = _playable_filters(filters)
        filters_applied: list[str] = []
        for index, source in enumerate(inputs):
            resampler = AudioResampler(
                format="s16", layout=resolved_layout, rate=rate
            )
            graph = None
            source_pad = sink_pad = None
            if played_filters:
                try:
                    graph = av.filter.Graph()
                    source_pad = graph.add_abuffer(
                        format="s16", sample_rate=rate, layout=resolved_layout,
                        time_base=time_base,
                    )
                    chain = [_add_filter(graph, stage) for stage in played_filters]
                    # Some stages (dynaudnorm, alimiter) negotiate a float planar
                    # format internally. Pin the sink back to the s16 layout the
                    # encoder and the inter-clip silence frame were set up for,
                    # otherwise the first silence insert raises
                    # "Frame does not match AudioResampler setup".
                    chain.append(
                        _add_filter(
                            graph,
                            "aformat=sample_fmts=s16:"
                            f"sample_rates={rate}:channel_layouts={resolved_layout}",
                        )
                    )
                    sink_pad = graph.add("abuffersink")
                    graph.link_nodes(source_pad, *chain, sink_pad)
                    graph.configure()
                except Exception:  # noqa: BLE001 - degrade to no filtering
                    graph = None
            if graph is not None and not filters_applied:
                filters_applied = list(played_filters)
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
                            for final in _through_filters(
                                graph, source_pad, sink_pad, converted
                            ):
                                _emit(
                                    container, stream, final, rate, next_pts,
                                    time_base=time_base,
                                )
                                clip_samples += final.samples
                                source_samples += final.samples
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
                for final in _through_filters(
                    graph, source_pad, sink_pad, converted
                ):
                    _emit(
                        container, stream, final, rate, next_pts,
                        time_base=time_base,
                    )
                    clip_samples += final.samples
                    source_samples += final.samples
            for final in _drain_filters(graph, source_pad, sink_pad):
                _emit(
                    container, stream, final, rate, next_pts,
                    time_base=time_base,
                )
                clip_samples += final.samples
                source_samples += final.samples
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
            "filters": filters_applied,
            "layout": resolved_layout,
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
    # ``--preprocess`` turns on the documented speech-cleanup chain; the
    # parent decides per deployment (STT_PREPROCESS) so the worker stays a
    # pure, deterministic tool. ``--adaptive`` measures a bounded prefix of
    # the recording first and keeps only the stages it actually needs.
    extract.add_argument("--preprocess", action="store_true")
    extract.add_argument("--adaptive", action="store_true")
    extract.add_argument("--stereo", action="store_true")
    extract.add_argument("source")

    merge = commands.add_parser("merge", help="concatenate audio tracks")
    merge.add_argument("--output", required=True)
    merge.add_argument("--format", choices=("wav", "opus"), default="wav")
    merge.add_argument("--silence", type=float, default=0.5)
    merge.add_argument("--preprocess", action="store_true")
    merge.add_argument("--adaptive", action="store_true")
    merge.add_argument("--stereo", action="store_true")
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
            sources = [Path(args.source)]
            filters: tuple[str, ...] | list[str] = ()
            if args.adaptive and args.preprocess:
                filters = adaptive_filters(sources)
            elif args.preprocess:
                filters = STT_PREPROCESS_FILTERS
            summary = _transcode(
                sources,
                Path(args.output),
                rate=rate,
                codec="libopus" if args.format == "opus" else "pcm_s16le",
                container_format="ogg" if args.format == "opus" else None,
                bit_rate=OPUS_BIT_RATE if args.format == "opus" else None,
                codec_options=OPUS_CODEC_OPTIONS if args.format == "opus" else None,
                filters=filters,
                layout="stereo" if args.stereo else "mono",
            )
            print(json.dumps(summary))
        elif args.command == "merge":
            if not args.inputs:
                raise WorkerError("no input files given for merging")
            rate = OPUS_SAMPLE_RATE if args.format == "opus" else WAV_SAMPLE_RATE
            sources = [Path(item) for item in args.inputs]
            filters = ()
            if args.adaptive and args.preprocess:
                filters = adaptive_filters(sources)
            elif args.preprocess:
                filters = STT_PREPROCESS_FILTERS
            summary = _transcode(
                sources,
                Path(args.output),
                rate=rate,
                codec="libopus" if args.format == "opus" else "pcm_s16le",
                container_format="ogg" if args.format == "opus" else None,
                silence_seconds=args.silence,
                bit_rate=OPUS_BIT_RATE if args.format == "opus" else None,
                codec_options=OPUS_CODEC_OPTIONS if args.format == "opus" else None,
                filters=filters,
                layout="stereo" if args.stereo else "mono",
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
