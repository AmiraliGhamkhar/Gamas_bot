"""Offline DER / JER / WER / CER evaluation for Gamas 2.0.

Two JSON files, one recording::

    {
      "text": "متن مرجع ...",
      "turns": [{"speaker_label": "استاد", "start": 0.0, "end": 4.2}, ...]
    }

The hypothesis may use the app's anonymous labels (``speaker_0`` …); the
evaluator aligns them to the reference labels by maximum overlap before
scoring. Nothing is written and no provider is called.

Usage::

    python -m scripts.evaluate_diarization --reference ref.json --hypothesis hyp.json
    python -m scripts.evaluate_diarization --reference ref.json --from-db 123
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from gamas_bot.diarization_eval import evaluate
from gamas_bot.transcript import TranscriptDocument


def load_json(path: str) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def hypothesis_from_db(db_path: str, submission_id: int) -> dict:
    """Read the stored speaker turns of one finished submission (read-only)."""
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT text, speaker_turns_json FROM transcriptions WHERE submission_id = ?",
            (int(submission_id),),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ValueError(f"no transcription for submission {submission_id}")
    turns = [
        {
            "speaker_label": turn.speaker_label or turn.speaker_id,
            "start": turn.start,
            "end": turn.end,
            "text": turn.text,
        }
        for turn in TranscriptDocument.turns_from_json(row[1])
        if turn.start is not None and turn.end is not None
    ]
    return {"text": str(row[0] or ""), "turns": turns}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True, help="reference JSON file")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--hypothesis", help="hypothesis JSON file")
    source.add_argument("--db", help="read-only SQLite database for --from-db")
    parser.add_argument(
        "--from-db", type=int, default=0, metavar="SUBMISSION_ID",
        help="evaluate the stored turns of this submission (requires --db)",
    )
    args = parser.parse_args(argv)
    if args.from_db and not args.db:
        parser.error("--from-db requires --db")
    try:
        reference = load_json(args.reference)
        hypothesis = (
            hypothesis_from_db(args.db, args.from_db)
            if args.from_db
            else load_json(args.hypothesis)
        )
        result = evaluate(reference, hypothesis)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"evaluation failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
