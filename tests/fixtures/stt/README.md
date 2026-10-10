# STT benchmark fixtures and provider test samples

This directory holds the **real Gamas-style audio fixtures** used by

* `scripts/benchmark_stt.py` (offline, reproducible provider comparison) and
* the admin "🧪 تست ارائه‌دهنده" tool (spec §39 sample modes).

Audio is **not committed** to the repository: lecture recordings may contain
personal or medical content, and benchmark integrity requires that each
deployment measures on its own reviewed corpus. CI never needs audio or API
keys — every metric helper in `gamas_bot/stt_platform/benchmark_metrics.py` is
pure and tested offline.

## Layout

```
tests/fixtures/stt/
├── README.md                  (this file)
├── samples/                   (admin test-tool samples, spec §39)
│   ├── ten_seconds.wav        (10-second sample — any language)
│   ├── persian.wav            (Persian sample)
│   └── medical.wav            (Persian medical sample)
└── benchmarks/                (benchmark corpus, one directory per category)
    ├── clean_persian_lecture/
    ├── noisy_persian_lecture/
    ├── persian_english_code_switching/
    ├── medical_lecture/
    ├── technical_lecture/
    ├── multiple_speakers/
    ├── classroom_noise/
    ├── numbers_and_units/
    ├── names_drugs_acronyms/
    └── long_lecture/
```

## Benchmark categories (spec §57)

The ten directory names above are the reviewed categories; they match
`gamas_bot.stt_platform.benchmark_metrics.BENCHMARK_PROFILES` exactly. The
benchmark tool takes the profile from the fixture's directory name, so every
result row identifies its provider, model, profile, language and audio
characteristics.

Each sample is placed as one audio file (`.wav`, `.mp3`, `.m4a`, `.ogg`,
`.flac`, `.mp4` — anything the media pipeline accepts) with an **optional**
reference transcript next to it:

```
benchmarks/medical_lecture/lecture_01.wav
benchmarks/medical_lecture/lecture_01.wav.txt   ← reference text (UTF-8)
```

Without a reference file the run records latency/output metrics only and the
WER/CER cells stay empty — a missing reference is never treated as a perfect
score.

For the `names_drugs_acronyms` and `numbers_and_units` categories, add a
`terms.txt` (one term per line: drug names, acronyms, units) at the corpus root
and run:

```
python -m scripts.benchmark_stt benchmarks/names_drugs_acronyms/lecture.wav \
    --terms benchmarks/terms.txt --output results.csv
```

## Admin test samples

The provider test tool sends **only** these three files, and only after an
explicit button press:

| Button | File |
| --- | --- |
| 10-second sample | `samples/ten_seconds.wav` |
| Persian sample | `samples/persian.wav` |
| Medical sample | `samples/medical.wav` |

If a file is missing the tool reports "not installed" and sends nothing — Gamas
never fabricates speech and never uploads silence pretending it is a test.
Temporary files are not created for these runs (the fixture is reused read-only),
and neither audio content nor transcript content is ever logged.

## Recommendations

* 16 kHz mono WAV is the safest input for every provider, but keep at least one
  sample in each non-WAV format you care about so format routing is exercised.
* Keep each fixture under 10 minutes except in `long_lecture/`.
* Record reference transcripts in the orthography your users expect (Persian
  yeh/kaf normalized is applied automatically during scoring).
* Never commit recordings that contain patient data. Redact or re-record.
