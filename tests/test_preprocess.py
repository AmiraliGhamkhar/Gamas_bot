"""Audio preprocessing: the chain, its degradation, and its sample exactness.

Billing is derived from the number of samples the engine hears, so the strongest
promise of this stage is a negative one: *it must not change the length*. These
tests pin that promise down with a real filter graph rather than a mocked one,
because the whole risk of a filter chain is that it quietly resamples.
"""

from __future__ import annotations

import array
import math
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

import av

from gamas_bot.media import build_extract_command
from gamas_bot.media_worker import (
    STT_PREPROCESS_FILTERS,
    _FILTER_ALLOWED,
    _add_filter,
    _drain_filters,
    _playable_filters,
    _transcode,
    _through_filters,
)

RATE = 16_000


def _tone(seconds: float, *, hum: float = 0.02, far_gain: float = 1.0) -> list[int]:
    """A synthetic lecture: speech-band tones over mains hum, quieter "far" half."""
    samples: list[int] = []
    total = int(RATE * seconds)
    half = total // 2
    for index in range(total):
        time = index / RATE
        gain = 1.0 if index < half else far_gain
        value = (
            0.20 * math.sin(2 * math.pi * 180 * time)
            + 0.12 * math.sin(2 * math.pi * 900 * time)
            + hum * math.sin(2 * math.pi * 50 * time)
        )
        samples.append(int(max(-1.0, min(1.0, value * gain)) * 32767))
    return samples


def _write_wav(path: Path, samples: list[int]) -> None:
    with av.open(str(path), "w", format="wav") as container:
        stream = container.add_stream("pcm_s16le", rate=RATE)
        stream.layout = "mono"
        frame = av.AudioFrame(format="s16", layout="mono", samples=len(samples))
        frame.sample_rate = RATE
        frame.planes[0].update(array.array("h", samples).tobytes())
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)


def _read_wav(path: Path) -> list[int]:
    out: list[int] = []
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        for frame in container.decode(stream):
            buffer = array.array("h")
            buffer.frombytes(memoryview(frame.planes[0]).tobytes())
            out.extend(buffer)
    return out


def _rms(samples: list[int], start: int, end: int) -> float:
    window = samples[start:end]
    return math.sqrt(sum(value * value for value in window) / max(1, len(window))) / 32768


class ChainContractTests(unittest.TestCase):
    def test_the_chain_is_the_documented_one(self):
        names = [stage.split("=", 1)[0] for stage in STT_PREPROCESS_FILTERS]
        self.assertEqual(names, ["highpass", "afftdn", "dynaudnorm", "alimiter"])
        # Every stage must pass the worker's character whitelist, and every one
        # of them must be a filter this PyAV build actually provides.
        for stage in STT_PREPROCESS_FILTERS:
            self.assertTrue(set(stage) <= _FILTER_ALLOWED, stage)
            self.assertIn(stage, _playable_filters([stage]), stage)

    def test_every_stage_is_looked_up_by_name_and_args(self):
        """PyAV rejects ``add("highpass=f=70")``; the split is load-bearing."""
        graph = av.filter.Graph()
        source = graph.add_abuffer(
            format="s16", sample_rate=RATE, layout="mono", time_base=Fraction(1, RATE)
        )
        chain = [_add_filter(graph, stage) for stage in STT_PREPROCESS_FILTERS]
        sink = graph.add("abuffersink")
        graph.link_nodes(source, *chain, sink)
        graph.configure()
        self.assertEqual(len(chain), len(STT_PREPROCESS_FILTERS))

    def test_an_unknown_stage_is_skipped_not_fatal(self):
        played = _playable_filters(["highpass=f=70", "nosuchfilter=1"])
        self.assertEqual(played, ["highpass=f=70"])

    def test_a_chain_with_no_playable_stage_is_empty(self):
        self.assertEqual(_playable_filters(["nosuchfilter=1"]), [])

    def test_no_filters_means_no_chain(self):
        self.assertEqual(_playable_filters(()), [])
        self.assertEqual(_playable_filters([]), [])

    def test_a_shell_looking_stage_is_refused(self):
        """The whitelist is what keeps a stage from ever being a command."""
        for stage in ("highpass=f=70;rm -rf /", "a && b", "x|y", "x$(id)"):
            self.assertEqual(_playable_filters([stage]), [], stage)


class SampleExactnessTests(unittest.TestCase):
    def test_the_graph_returns_exactly_the_samples_it_was_given(self):
        samples = _tone(1.0)
        graph = av.filter.Graph()
        source = graph.add_abuffer(
            format="s16", sample_rate=RATE, layout="mono", time_base=Fraction(1, RATE)
        )
        chain = [_add_filter(graph, stage) for stage in STT_PREPROCESS_FILTERS]
        sink = graph.add("abuffersink")
        graph.link_nodes(source, *chain, sink)
        graph.configure()
        produced = 0
        for start in range(0, len(samples), 1024):
            chunk = samples[start : start + 1024]
            frame = av.AudioFrame(format="s16", layout="mono", samples=len(chunk))
            frame.sample_rate = RATE
            frame.planes[0].update(array.array("h", chunk).tobytes())
            frame.pts = start
            frame.time_base = Fraction(1, RATE)
            for output in _through_filters(graph, source, sink, frame):
                produced += output.samples
        for output in _drain_filters(graph, source, sink):
            produced += output.samples
        self.assertEqual(produced, len(samples))

    def test_extraction_keeps_the_duration_and_reports_the_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "in.wav"
            output = root / "out.wav"
            _write_wav(source, _tone(1.0))
            summary = _transcode(
                [source],
                output,
                rate=RATE,
                codec="pcm_s16le",
                filters=STT_PREPROCESS_FILTERS,
            )
            self.assertEqual(summary["source_samples"], summary["output_samples"])
            self.assertEqual(summary["filters"], list(STT_PREPROCESS_FILTERS))
            self.assertEqual(len(_read_wav(output)), summary["output_samples"])

    def test_without_the_flag_nothing_is_filtered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "in.wav"
            output = root / "out.wav"
            _write_wav(source, _tone(0.5))
            summary = _transcode([source], output, rate=RATE, codec="pcm_s16le")
            self.assertEqual(summary["filters"], [])
            self.assertEqual(summary["source_samples"], summary["output_samples"])

    def test_a_stage_the_build_lacks_degrades_instead_of_failing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "in.wav"
            output = root / "out.wav"
            _write_wav(source, _tone(0.5))
            summary = _transcode(
                [source],
                output,
                rate=RATE,
                codec="pcm_s16le",
                filters=["nosuchfilter=1", *STT_PREPROCESS_FILTERS],
            )
            # The unknown stage is dropped; the rest still runs and the job lives.
            self.assertEqual(summary["filters"], list(STT_PREPROCESS_FILTERS))
            self.assertEqual(summary["source_samples"], summary["output_samples"])


class EffectTests(unittest.TestCase):
    def test_the_chain_removes_hum_and_lifts_a_quiet_half(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "in.wav"
            output = root / "out.wav"
            _write_wav(source, _tone(1.0, hum=0.10, far_gain=0.3))
            _transcode(
                [source],
                output,
                rate=RATE,
                codec="pcm_s16le",
                filters=STT_PREPROCESS_FILTERS,
            )
            cleaned = _read_wav(output)
            original = _read_wav(source)
            half = len(original) // 2

            def energy(samples: list[int], start: int, end: int, hz: float) -> float:
                window = samples[start:end]
                real = sum(
                    value * math.cos(2 * math.pi * hz * index / RATE)
                    for index, value in enumerate(window)
                )
                imaginary = sum(
                    value * math.sin(2 * math.pi * hz * index / RATE)
                    for index, value in enumerate(window)
                )
                return math.hypot(real, imaginary) / max(1, len(window)) / 32768

            self.assertLess(
                energy(cleaned, 0, half, 50), energy(original, 0, half, 50)
            )
            quiet_before = _rms(original, half, len(original))
            quiet_after = _rms(cleaned, half, len(cleaned))
            self.assertGreater(quiet_after, quiet_before)
            self.assertLessEqual(max(abs(value) for value in cleaned), 32767)


class CommandTests(unittest.TestCase):
    def test_the_command_carries_the_flag_only_when_asked(self):
        plain = build_extract_command(Path("a.mp4"), Path("b.wav"))
        self.assertNotIn("--preprocess", plain)
        cleaned = build_extract_command(
            Path("a.mp4"), Path("b.wav"), preprocess=True
        )
        self.assertIn("--preprocess", cleaned)
        # The flag must sit before the ``--`` separator, or argparse treats it as
        # an input file name.
        self.assertLess(cleaned.index("--preprocess"), cleaned.index("--"))
        self.assertEqual(cleaned[-1], "a.mp4")


if __name__ == "__main__":
    unittest.main()
