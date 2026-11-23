"""Validation script for the initial 200-question Explore seed bank."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gamas_bot.explore_seed import build_seed_questions


def validate_seed_bank(expected_count: int = 200) -> list[str]:
    items = build_seed_questions(expected_count)
    errors: list[str] = []
    if len(items) != expected_count:
        errors.append(f"Expected {expected_count} questions, got {len(items)}")
    seen_texts: set[str] = set()
    for idx, (topic, q, opts, corr, exp) in enumerate(items):
        if not topic.strip():
            errors.append(f"Row {idx}: empty topic")
        q_norm = q.strip()
        if not q_norm:
            errors.append(f"Row {idx}: empty question_text")
        if q_norm in seen_texts:
            errors.append(f"Row {idx}: duplicate question_text")
        seen_texts.add(q_norm)
        if len(opts) != 4 or any(not str(o).strip() for o in opts):
            errors.append(f"Row {idx}: must have 4 non-empty options")
        if not isinstance(corr, int) or corr < 0 or corr > 3:
            errors.append(f"Row {idx}: invalid correct_index {corr!r}")
        if not exp.strip():
            errors.append(f"Row {idx}: empty explanation")
    return errors


if __name__ == "__main__":
    errs = validate_seed_bank(200)
    if errs:
        for e in errs[:20]:
            print("ERROR:", e)
        sys.exit(1)
    print("OK: 200 valid, unique Explore questions verified.")
