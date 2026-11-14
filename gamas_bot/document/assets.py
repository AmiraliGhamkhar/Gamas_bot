"""The booklet asset registry: semantic names, deterministic paths.

Renderers ask for ``cover`` or ``brand/monogram``, never for a filename. The
manifest (``assets/booklet/manifest.json``) is the only mapping from meaning to
file, and it carries each asset's role and intended pixel size so a missing or
wrong-sized asset is detectable rather than silent.

Assets are static, committed and generated once by
``scripts/build_booklet_assets.py``; a booklet run never renders an image, which
is what keeps generation cheap on shared hosting.

Topic selection is deterministic keyword scoring over the booklet's own title
and headings. It never invents a subject: when nothing scores, the general
Gamas artwork is used.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..config import PROJECT_DIR

logger = logging.getLogger(__name__)

ASSET_ROOT = PROJECT_DIR / "assets" / "booklet"
MANIFEST_NAME = "manifest.json"


class MissingAsset(LookupError):
    """A manifest entry or its file is unavailable."""

    def __init__(self, key: str, detail: str) -> None:
        super().__init__(f"booklet asset {key!r} is unavailable: {detail}")
        self.key = key
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Asset:
    """One resolved asset: semantic key, absolute path, role and size."""

    key: str
    path: Path
    role: str
    width: int
    height: int
    alt: str = ""

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0


@dataclass(frozen=True, slots=True)
class Topic:
    """A subject family and the artwork it selects."""

    key: str
    label: str
    cover: str
    emblem: str


#: Scoring vocabulary per topic. Persian and English, because a pharmacology
#: lecture is just as likely to be titled «Metformin» as «داروشناسی».
_TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "medical": (
        "پزشکی", "بالینی", "بیمار", "درمان", "تشخیص", "علائم", "سندرم", "فشار خون", "قند خون",
        "medical", "clinical", "patient", "diagnosis", "treatment", "syndrome",
    ),
    "pharma": (
        "فارماکو", "دارو", "دوز", "مصرف دارو", "دیابت", "انسولین", "متفورمین",
        "pharma", "drug", "dose", "metformin", "insulin", "diabetes",
    ),
    "anatomy": (
        "آناتومی", "کالبدشناسی", "عضله", "استخوان", "مفصل", "عصب",
        "anatomy", "muscle", "bone", "joint", "nerve", "skeletal",
    ),
    "nursing": (
        "پرستاری", "مراقبت", "نursing", "nursing", "care plan", "vital signs",
        "علائم حیاتی", "مراقبت پرستاری",
    ),
    "epidemiology": (
        "اپیدمی", "همه‌گیر", "بروز", "شیوع", "مطالعهٔ کوهورت", "مورد‌شاهدی",
        "epidemiology", "incidence", "prevalence", "cohort", "odds ratio",
        "p-value", "ci 95",
    ),
    "chemistry": (
        "بیوشیمی", "آنزیم", "متابولیسم", "واکنش", "مولکول", "پروتئین",
        "biochem", "enzyme", "metabolism", "molecule", "protein", "atp",
    ),
    "data": (
        "انفورماتیک", "داده", "پایگاه داده", "الگوریتم", "برنامه‌نویسی", "شبکهٔ عصبی",
        "informatics", "database", "sql", "algorithm", "python", "neural network",
        "machine learning", "data flow",
    ),
}

_TOPIC_LABELS: dict[str, str] = {
    "general": "عمومی",
    "medical": "پزشکی و علوم سلامت",
    "pharma": "داروشناسی",
    "anatomy": "آناتومی",
    "nursing": "پرستاری",
    "epidemiology": "اپیدمیولوژی",
    "chemistry": "علوم آزمایشگاهی و بیوشیمی",
    "data": "داده و فناوری سلامت",
}

#: Priority for ties, so a lecture that mentions both a drug and a patient
#: resolves consistently instead of depending on dict order.
_TOPIC_PRIORITY = (
    "pharma",
    "anatomy",
    "nursing",
    "epidemiology",
    "chemistry",
    "data",
    "medical",
)

_WORD_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _topic_pattern(keyword: str) -> re.Pattern[str]:
    pattern = _WORD_RE_CACHE.get(keyword)
    if pattern is None:
        pattern = re.compile(rf"(?<![A-Za-z]){re.escape(keyword)}(?![A-Za-z])", re.IGNORECASE)
        _WORD_RE_CACHE[keyword] = pattern
    return pattern


@lru_cache(maxsize=1)
def manifest() -> dict:
    """The parsed manifest, or an empty mapping when it is unavailable."""
    for base in (ASSET_ROOT, Path.cwd() / "assets" / "booklet"):
        path = base / MANIFEST_NAME
        try:
            if path.is_file():
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    return payload
        except (OSError, ValueError):
            logger.warning("booklet asset manifest could not be read: %s", path, exc_info=True)
    return {}


def asset_root() -> Path:
    """The directory the manifest's relative paths resolve against."""
    for base in (ASSET_ROOT, Path.cwd() / "assets" / "booklet"):
        if (base / MANIFEST_NAME).is_file():
            return base
    return ASSET_ROOT


def resolve(key: str) -> Asset:
    """Resolve a semantic asset key to its file, or raise :class:`MissingAsset`."""
    entries = (manifest().get("assets") or {})
    entry = entries.get(key)
    if not isinstance(entry, dict) or not entry.get("path"):
        raise MissingAsset(key, "no manifest entry")
    path = asset_root() / str(entry["path"])
    try:
        if not path.is_file():
            raise MissingAsset(key, f"file not found at {path}")
    except OSError as exc:  # pragma: no cover - defensive
        raise MissingAsset(key, str(exc)) from exc
    return Asset(
        key=key,
        path=path,
        role=str(entry.get("role") or ""),
        width=int(entry.get("width") or 0),
        height=int(entry.get("height") or 0),
        alt=str(entry.get("alt") or ""),
    )


def optional(key: str) -> Asset | None:
    """Resolve an optional decoration; a missing file degrades, never fails."""
    try:
        return resolve(key)
    except MissingAsset:
        logger.warning("optional booklet asset %r is missing; continuing without it", key)
        return None


def all_assets() -> tuple[Asset, ...]:
    """Every manifest asset that actually exists (used by the asset QA test)."""
    resolved: list[Asset] = []
    for key in (manifest().get("assets") or {}):
        asset_value = optional(key)
        if asset_value is not None:
            resolved.append(asset_value)
    return tuple(resolved)


def validate_manifest() -> list[str]:
    """Problems with the manifest or its files, as human-readable strings."""
    problems: list[str] = []
    payload = manifest()
    if not payload:
        return ["asset manifest is missing or unreadable"]
    entries = payload.get("assets") or {}
    if not isinstance(entries, dict) or not entries:
        return ["asset manifest declares no assets"]
    for key, entry in entries.items():
        if not isinstance(entry, dict) or not entry.get("path"):
            problems.append(f"{key}: entry has no path")
            continue
        try:
            asset_value = resolve(key)
        except MissingAsset as exc:
            problems.append(str(exc))
            continue
        if asset_value.width <= 0 or asset_value.height <= 0:
            problems.append(f"{key}: declares no size")
    for topic_key, topic in (payload.get("topics") or {}).items():
        if not isinstance(topic, dict):
            problems.append(f"topic {topic_key}: not an object")
            continue
        for field in ("cover", "emblem"):
            reference = topic.get(field)
            if reference not in entries:
                problems.append(f"topic {topic_key}: {field} points at unknown asset {reference!r}")
    return problems


def topic_for(text: str, *, override: str | None = None) -> Topic:
    """The deterministic subject family for a booklet.

    ``override`` (from configuration, if a future caller adds it) always wins.
    Otherwise keyword counts decide; a tie follows ``_TOPIC_PRIORITY`` so the
    same material always selects the same cover.
    """
    payload = manifest()
    raw_topics = payload.get("topics")
    topics: dict = raw_topics if isinstance(raw_topics, dict) else {}
    key = override if override is not None and override in topics else _score_topic(text)
    candidate = topics.get(key)
    entry: dict = candidate if isinstance(candidate, dict) else {}
    return Topic(
        key=key,
        label=str(entry.get("label") or _TOPIC_LABELS.get(key, _TOPIC_LABELS["general"])),
        cover=str(entry.get("cover") or "cover/hero-general"),
        emblem=str(entry.get("emblem") or "icons/knowledge"),
    )


def _score_topic(text: str) -> str:
    lowered = (text or "").lower()
    if not lowered.strip():
        return "general"
    scores: dict[str, int] = {}
    for key, keywords in _TOPIC_KEYWORDS.items():
        score = sum(1 for keyword in keywords if _topic_pattern(keyword).search(lowered))
        if score:
            scores[key] = score
    if not scores:
        return "general"
    best = max(scores.values())
    for key in _TOPIC_PRIORITY:
        if scores.get(key) == best:
            return key
    return max(scores, key=lambda name: scores[name])


def cover_for(text: str, *, override: str | None = None) -> Asset | None:
    """The cover artwork for the booklet's subject, or ``None``."""
    return optional(topic_for(text, override=override).cover)


def emblem_for(text: str, *, override: str | None = None) -> Asset | None:
    """The small topic emblem used inside the booklet, or ``None``."""
    return optional(topic_for(text, override=override).emblem)


def booklet_text(notes: object) -> str:
    """The text asset selection is based on: title plus real section headings.

    Only source-derived, user-visible text participates. Body prose is
    deliberately excluded: a single mention of a drug inside one paragraph must
    not re-brand an entire anatomy booklet.
    """
    title = str(getattr(notes, "title", "") or "")
    headings = " ".join(
        str(getattr(section, "heading", "") or "")
        for section in (getattr(notes, "sections", ()) or ())
    )
    return f"{title} {headings}".strip()


__all__ = [
    "ASSET_ROOT",
    "Asset",
    "MissingAsset",
    "Topic",
    "all_assets",
    "asset_root",
    "booklet_text",
    "cover_for",
    "emblem_for",
    "manifest",
    "optional",
    "resolve",
    "topic_for",
    "validate_manifest",
]
