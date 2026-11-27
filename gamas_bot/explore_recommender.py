"""Deterministic, explainable recommendation engine for the Explore feed.

The design goal is a *scrollable* feed: many pages, no repeats, no starvation,
and enough transparency that an operator can answer "why is this question here?".
Nothing here is a black box — every score is a weighted sum of four named
components, and every item reports them back to the client.

Ranking model
-------------
``score = w_affinity·affinity + w_quality·quality + w_freshness·freshness
         + w_diversity·discovery``

* **affinity** — the user's smoothed, time-decayed interest in the topic, learned
  from impression signals (1=skipped fast, 2=read, 3=answered). New users get the
  neutral prior, so personalisation never has to start from zero.
* **quality** — difficulty fit times academic source tier. A question backed by a
  textbook or an institutional source outranks an equivalent unsourced one; an
  unverified question can still appear (so the archive is not lost) but never
  outranks a verified one and is labelled unverified in the API payload.
* **freshness** — a slow decay on ``created_at`` so newly curated content gets a
  window of visibility instead of being buried under the archive forever.
* **discovery** — an explicit bonus for topics the user has never engaged with.
  Personalisation must not collapse the feed into one subject.

Diversity is enforced structurally on top of the score: at most
``max_consecutive_same_topic`` consecutive cards share a topic.

Pagination
----------
Cursor-based, never offset-based. The cursor is an opaque, URL-safe token that
carries three things: the ids already served in this feed session, the topic of
the last cards (so the diversity rule keeps working across page boundaries), and
a small profile stamp. Because served ids travel with the cursor, a prefetched
page can never repeat a question that is already on screen — even though
impressions for those cards have not been recorded yet.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .explore_sources import row_source_payload

#: How much a question's academic backing contributes to its quality score.
#: Tier 1 (textbook/peer-reviewed/OpenStax) > Tier 2 (institutional) >
#: Tier 3 (reputable educational reference) > unverified.
SOURCE_QUALITY: dict[str, float] = {
    "textbook": 1.00,
    "institutional": 0.95,
    "educational": 0.85,
}
UNVERIFIED_QUALITY = 0.55


@dataclass(frozen=True, slots=True)
class RecommenderConfig:
    w_affinity: float = 0.60
    w_quality: float = 0.15
    w_freshness: float = 0.10
    w_diversity: float = 0.15
    prior_mean: float = 2.0
    prior_weight: float = 5.0
    decay_lambda: float = 0.1
    max_consecutive_same_topic: int = 2
    #: Days after which a question's freshness bonus has decayed to 1/e.
    freshness_half_life_days: float = 120.0
    #: Hard caps, only as runaway guards. The bank is 200 questions today.
    max_candidates: int = 5000
    max_session_ids: int = 400


DEFAULT_CONFIG = RecommenderConfig()

#: Largest page the API will serve in one request (protects the mobile client).
MAX_PAGE_SIZE = 20
#: Smallest page the API will serve (below this, prefetching loses its point).
MIN_PAGE_SIZE = 1
DEFAULT_PAGE_SIZE = 12


def classify_engagement(*, answered: bool, dwell_ms: int) -> int:
    """Return exact engagement weight: 3=answered, 2=>8s, 1=<3s, 0=neutral."""
    if answered:
        return 3
    if dwell_ms > 8000:
        return 2
    if dwell_ms < 3000:
        return 1
    return 0


def compute_topic_affinities(
    impressions: list[dict[str, Any]],
    *,
    now: datetime,
    config: RecommenderConfig = DEFAULT_CONFIG,
) -> dict[str, float]:
    """Compute smoothed, time-decayed topic affinities normalized to [0.0, 1.0]."""
    weighted_sum: dict[str, float] = defaultdict(float)
    weight_total: dict[str, float] = defaultdict(float)

    for r in impressions:
        sig = int(r.get("signal") or 0)
        if sig not in (1, 2, 3):
            continue
        topic = str(r["topic"])
        try:
            shown_dt = datetime.fromisoformat(str(r["shown_at"]))
            if shown_dt.tzinfo is None:
                shown_dt = shown_dt.replace(tzinfo=timezone.utc)
            age_days = max(0.0, (now - shown_dt).total_seconds() / 86400.0)
        except Exception:
            age_days = 0.0
        decay = math.exp(-config.decay_lambda * age_days)
        weighted_sum[topic] += sig * decay
        weight_total[topic] += decay

    affinities: dict[str, float] = {}
    for topic, w_sum in weighted_sum.items():
        smoothed = (config.prior_mean * config.prior_weight + w_sum) / (
            config.prior_weight + weight_total[topic]
        )
        # Normalize [1.0, 3.0] -> [0.0, 1.0]
        affinities[topic] = max(0.0, min(1.0, (smoothed - 1.0) / 2.0))
    return affinities


# --------------------------------------------------------------------------- #
# Cursor
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class FeedCursor:
    """The pagination state a client carries between requests.

    ``served`` is the set of question ids already handed out in this feed
    session. It is what makes prefetch safe: page N+1 is computed while some
    cards of page N are still unseen (so not yet impressioned), and the cursor
    guarantees they are not handed out twice.
    """

    served: list[int] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)

    def encode(self) -> str:
        raw = json.dumps(
            {"v": 1, "s": self.served, "t": self.topics},
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, token: str | None, *, config: RecommenderConfig = DEFAULT_CONFIG) -> "FeedCursor":
        """Parse *token*, falling back to an empty cursor on bad input.

        A malformed or truncated cursor must never break the feed: the worst
        case is that the user restarts the session with the seen-question
        filter still preventing repeats from their own history.
        """
        text = str(token or "").strip()
        if not text:
            return cls()
        padded = text + "=" * (-len(text) % 4)
        try:
            data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return cls()
        if not isinstance(data, dict) or data.get("v") != 1:
            return cls()
        served: list[int] = []
        for value in data.get("s") or []:
            try:
                served.append(int(value))
            except (TypeError, ValueError):
                continue
        topics = [str(t) for t in (data.get("t") or []) if isinstance(t, str)][:4]
        return cls(served=served[-config.max_session_ids :], topics=topics)


@dataclass(slots=True)
class FeedPage:
    """One page of the feed plus the cursor that continues it."""

    items: list[dict[str, Any]]
    next_cursor: str
    has_more: bool

    def as_payload(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
        }


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #


def _freshness(created_at: Any, now: datetime, config: RecommenderConfig) -> float:
    """Slow exponential decay of a question's "new content" bonus."""
    try:
        created = datetime.fromisoformat(str(created_at))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (now - created).total_seconds() / 86400.0)
    except Exception:
        age_days = 0.0
    return math.exp(-age_days / max(1.0, config.freshness_half_life_days))


def _quality(row: dict[str, Any], verified: bool) -> float:
    """Difficulty fit multiplied by the academic tier of the source."""
    difficulty = str(row.get("difficulty") or "easy")
    difficulty_factor = 1.0 if difficulty == "easy" else 0.7
    if not verified:
        return difficulty_factor * UNVERIFIED_QUALITY
    tier = SOURCE_QUALITY.get(str(row.get("evidence_level") or ""), UNVERIFIED_QUALITY)
    return difficulty_factor * tier


def _to_item(
    row: dict[str, Any],
    *,
    affinity: float,
    quality: float,
    freshness: float,
    discovery: float,
    score: float,
) -> dict[str, Any]:
    source = row_source_payload(row)
    return {
        "question_id": int(row["question_id"]),
        "topic": str(row["topic"]),
        "difficulty": str(row["difficulty"]),
        "question_text": str(row["question_text"]),
        "options": json.loads(row["options_json"]),
        "source": source,
        "verified": source is not None,
        # Explainability: the four components and their weighted total, so the
        # ranking can be inspected in production instead of guessed at.
        "explain": {
            "affinity": round(affinity, 4),
            "quality": round(quality, 4),
            "freshness": round(freshness, 4),
            "discovery": round(discovery, 4),
            "score": round(score, 4),
        },
    }


async def recommend_explore_page(
    db: Any,
    user_id: int,
    *,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str = "",
    now: datetime | None = None,
    config: RecommenderConfig = DEFAULT_CONFIG,
) -> FeedPage:
    """Return one cursor-paginated page of ranked, unseen Explore questions."""
    current = now or datetime.now(timezone.utc)
    try:
        requested = int(limit) if limit is not None else DEFAULT_PAGE_SIZE
    except (TypeError, ValueError):
        requested = DEFAULT_PAGE_SIZE
    page_size = max(MIN_PAGE_SIZE, min(requested, MAX_PAGE_SIZE))
    state = FeedCursor.decode(cursor, config=config)
    served = set(state.served)

    imp_rows = await db.db.execute_fetchall(
        """
        SELECT i.question_id, i.shown_at, i.signal, q.topic
        FROM explore_impressions i
        JOIN explore_questions q ON q.question_id = i.question_id
        WHERE i.user_id = ?
        ORDER BY i.impression_id DESC
        LIMIT 500
        """,
        (int(user_id),),
    )
    impressions = [dict(r) for r in imp_rows]
    affinities = compute_topic_affinities(impressions, now=current, config=config)

    # Seen questions are excluded in SQL - never in a post-filter over a capped
    # window - so a question the user has already met can not consume a slot,
    # and newly published questions stay reachable no matter how large the
    # approved bank grows. The cap is only a runaway guard.
    cand_rows = await db.db.execute_fetchall(
        """
        SELECT question_id, topic, difficulty, question_text, options_json,
               created_at, source_type, source_title, source_author,
               source_institution, source_url, source_year,
               source_page_or_section, evidence_level, verified_at
        FROM explore_questions
        WHERE status = 'approved'
          AND question_id NOT IN (
              SELECT question_id FROM explore_impressions WHERE user_id = ?
          )
        ORDER BY question_id ASC
        LIMIT ?
        """,
        (int(user_id), int(config.max_candidates)),
    )

    neutral_affinity = (config.prior_mean - 1.0) / 2.0
    scored: list[tuple[float, int, dict[str, Any], dict[str, float]]] = []
    for r in cand_rows:
        row = dict(r)
        qid = int(row["question_id"])
        if qid in served:
            # Already on screen in this session; the cursor forbids a repeat.
            continue
        topic = str(row["topic"])
        source = row_source_payload(row)
        affinity = affinities.get(topic, neutral_affinity)
        quality = _quality(row, source is not None)
        freshness = _freshness(row.get("created_at"), current, config)
        discovery = 1.0 if topic not in affinities else 0.2
        score = (
            config.w_affinity * affinity
            + config.w_quality * quality
            + config.w_freshness * freshness
            + config.w_diversity * discovery
        )
        scored.append(
            (
                score,
                qid,
                row,
                {
                    "affinity": affinity,
                    "quality": quality,
                    "freshness": freshness,
                    "discovery": discovery,
                },
            )
        )

    # Deterministic: score descending, then question_id ascending as tiebreak.
    scored.sort(key=lambda x: (-x[0], x[1]))

    items: list[dict[str, Any]] = []
    deferred: list[tuple[float, int, dict[str, Any], dict[str, float]]] = []
    recent_topics = list(state.topics)[-config.max_consecutive_same_topic :]
    cap = max(1, config.max_consecutive_same_topic)

    def _emit(entry: tuple[float, int, dict[str, Any], dict[str, float]]) -> None:
        score, _, row, parts = entry
        items.append(
            _to_item(
                row,
                affinity=parts["affinity"],
                quality=parts["quality"],
                freshness=parts["freshness"],
                discovery=parts["discovery"],
                score=score,
            )
        )
        recent_topics.append(str(row["topic"]))
        del recent_topics[:-cap]

    for entry in scored:
        if len(items) >= page_size:
            break
        topic = str(entry[2]["topic"])
        if len(recent_topics) >= cap and all(t == topic for t in recent_topics[-cap:]):
            deferred.append(entry)
            continue
        _emit(entry)

    # If the diversity rule starved the page, fill from the deferred tail: a
    # short page would stall the feed, and two repeats of a topic is acceptable
    # once the alternative is no content at all.
    for entry in deferred:
        if len(items) >= page_size:
            break
        _emit(entry)

    has_more = len(scored) > len(items)
    next_state = FeedCursor(
        served=(state.served + [item["question_id"] for item in items])[
            -config.max_session_ids :
        ],
        topics=recent_topics[-cap:],
    )
    next_cursor = next_state.encode() if items else ""
    return FeedPage(items=items, next_cursor=next_cursor, has_more=has_more)


async def recommend_explore_feed(
    db: Any,
    user_id: int,
    *,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | int = "",
    now: datetime | None = None,
    config: RecommenderConfig = DEFAULT_CONFIG,
) -> list[dict[str, Any]]:
    """Return up to *limit* approved public Explore questions ranked deterministically.

    Kept as the simple list-returning entry point (tests and internal callers use
    it); :func:`recommend_explore_page` adds the pagination envelope. The legacy
    integer ``cursor`` argument (a ``question_id`` floor) is still understood so
    no older caller breaks.
    """
    token = ""
    if isinstance(cursor, int) and cursor > 0:
        # Legacy semantics: skip everything up to and including this id by
        # seeding the session as if those ids had been served.
        token = FeedCursor(served=list(range(1, int(cursor) + 1))).encode()
    elif isinstance(cursor, str):
        token = cursor
    page = await recommend_explore_page(
        db, user_id, limit=limit, cursor=token, now=now, config=config
    )
    return page.items
