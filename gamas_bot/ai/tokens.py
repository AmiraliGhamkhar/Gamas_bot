"""Token-aware budgeting: estimate, don\'t guess.

Gamas lectures are mixed Persian/Latin text. Without shipping a tokenizer per
provider we estimate conservatively — deliberately biased to *over*-estimate
tokens so a chunk always fits a TPM/context budget. Character caps remain as
an independent lossless-splitting ceiling (see ``split_transcript``).
"""

from __future__ import annotations

import math
import re

_PERSIAN_ARABIC = re.compile(r"[؀-ۿݐ-ݿﭐ-ﹿ﻿-￻]+")

#: Characters-per-token ratios (chars/token, lower = more conservative):
#: Persian runs ~1.5-2 chars/token on BPE tokenizers; Latin ~4.
_PERSIAN_RATIO = 1.8
_LATIN_RATIO = 3.5
_MIXED_RATIO = 2.2


def estimate_tokens(text: str) -> int:
    """Conservative token estimate for one string (never an under-count goal)."""
    if not text:
        return 0
    persian_chars = 0
    for match in _PERSIAN_ARABIC.finditer(text):
        persian_chars += match.end() - match.start()
    total = len(text)
    latinish = total - persian_chars
    token_estimate = (persian_chars / _PERSIAN_RATIO) + (latinish / _LATIN_RATIO)
    return max(1, math.ceil(token_estimate))


def chars_for_token_budget(token_budget: int, *, sample: str = "", safety_margin: float = 0.15) -> int:
    """How many characters fit a token budget after the safety margin.

    When ``sample`` resembles the actual content, its own measured density is
    used (still floored at the mixed conservative ratio); otherwise the mixed
    ratio is used.
    """
    budget = max(1, int(token_budget))
    usable = budget * (1.0 - min(max(float(safety_margin), 0.0), 0.5))
    ratio = _MIXED_RATIO
    if sample and len(sample) >= 200:
        measured = len(sample) / max(estimate_tokens(sample), 1)
        ratio = max(_PERSIAN_RATIO, min(measured, _LATIN_RATIO))
    return max(200, int(usable * ratio))


def chunk_char_budget(
    token_budget: int,
    *,
    char_cap: int,
    overhead_tokens: int = 0,
    sample: str = "",
    safety_margin: float = 0.15,
) -> int:
    """Effective per-chunk character budget for one provider profile.

    ``overhead_tokens`` accounts for the system prompt and prompt wrappers the
    same request also carries; the character ceiling is the lossless splitter's
    own hard bound.
    """
    available = max(1, token_budget - max(0, overhead_tokens))
    return min(max(1, int(char_cap)), chars_for_token_budget(available, sample=sample, safety_margin=safety_margin))


def fits_budget(system_prompt: str, user_text: str, token_budget: int, *, safety_margin: float = 0.15) -> bool:
    usable = token_budget * (1.0 - min(max(float(safety_margin), 0.0), 0.5))
    return estimate_tokens(system_prompt) + estimate_tokens(user_text) <= usable
