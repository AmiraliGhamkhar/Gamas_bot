"""Measure document chunking only, with deterministic synthetic Persian text.

Run ``python -m scripts.document_chunk_bench``. This intentionally calls the
chunker directly: large test sizes can exceed the upload extraction limit. It
does not benchmark PDF parsing, SQLite, model calls, Telegram or live traffic.
"""

from __future__ import annotations

import argparse
import json
from time import perf_counter

from gamas_bot.doc_extract import DocSection, chunk_sections


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[1, 5, 10, 20], metavar="MICHARS")
    args = parser.parse_args()
    if any(size <= 0 for size in args.sizes):
        parser.error("sizes must be positive")
    phrase = "مطالب درسی با چند کلمه و فاصله. "
    results = []
    for size in args.sizes:
        chars = size * 1024 * 1024
        text = (phrase * (chars // len(phrase) + 1))[:chars]
        started = perf_counter()
        chunks = chunk_sections([DocSection("صفحه ۱", "", text)], max_chars=6000)
        elapsed = perf_counter() - started
        results.append({"michars": size, "chunks": len(chunks), "seconds": round(elapsed, 4)})
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
