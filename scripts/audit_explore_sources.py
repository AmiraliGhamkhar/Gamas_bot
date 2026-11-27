"""Audit the academic-source coverage of the curated Explore bank.

The content pipeline is: academic source → authoring → validation → source
verification → review status → database → recommender → feed. This script is the
"source verification" step an operator runs before shipping content, and the
report is deliberately boring: it either proves every production question is
cited, or it lists exactly which ones are not.

    python3 scripts/audit_explore_sources.py            # human report
    python3 scripts/audit_explore_sources.py --strict   # exit 1 on any gap
    python3 scripts/audit_explore_sources.py --json     # machine report

Gaps that make it fail:

* a question whose source key does not resolve in the catalog;
* a source with no title, no institution, or a URL that is not safe `https`;
* an unknown evidence tier;
* a health/body question whose source is weaker than institutional.

It also flags *review* items that do not fail the build: health questions whose
source is a fact sheet rather than a textbook or a peer-reviewed work are listed
so a curator can double-check that the wording matches current guidance.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gamas_bot.explore_bank import BANK, TOPICS  # noqa: E402
from gamas_bot.explore_sources import (  # noqa: E402
    EVIDENCE_LEVELS,
    SOURCES,
    validate_catalog,
    validate_source,
)

MEDICAL_TOPICS = {"سلامت و پزشکی", "بدن انسان"}
STRONG_TIERS = {"textbook", "institutional"}


def audit() -> dict:
    errors: list[str] = list(validate_catalog())
    warnings: list[str] = []

    for item in BANK:
        source = item.source
        if source is None:
            errors.append(f"{item.key}: unknown source key {item.source_key!r}")
            continue
        errors.extend(validate_source(source))
        if item.topic in MEDICAL_TOPICS and source.evidence_level not in STRONG_TIERS:
            errors.append(
                f"{item.key}: medical question backed by a weak tier "
                f"({source.evidence_level})"
            )
        if item.topic in MEDICAL_TOPICS and source.source_type == "fact_sheet":
            warnings.append(
                f"{item.key}: health guidance from a fact sheet "
                f"({source.key}) — re-check wording against current guidance"
            )

    tiers = Counter(item.source.evidence_level for item in BANK if item.source)
    per_topic = Counter(item.topic for item in BANK)
    return {
        "questions": len(BANK),
        "sources": len(SOURCES),
        # Catalog-level problems surface on their own (they carry a source key,
        # not a question key), so the report separates them for readability.
        "catalog_errors": list(validate_catalog()),
        "errors": errors,
        "warnings": warnings,
        "tiers": dict(tiers),
        "topics": {topic: per_topic.get(topic, 0) for topic in TOPICS},
        "evidence_levels": list(EVIDENCE_LEVELS),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strict", action="store_true", help="fail on any warning too")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(argv)

    report = audit()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"questions: {report['questions']}  catalog sources: {report['sources']}")
        print("evidence tiers:", report["tiers"])
        for topic, count in report["topics"].items():
            print(f"  {topic}: {count}")
        for warning in report["warnings"]:
            print("WARN:", warning)
        for error in report["errors"]:
            print("ERROR:", error)
        if not report["errors"]:
            print("OK: every curated question carries a verified academic source.")

    if report["errors"]:
        return 1
    if args.strict and report["warnings"]:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - operator entry point
    sys.exit(main())
