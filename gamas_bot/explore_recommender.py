"""Deterministic, explainable recommendation engine for the Explore feed."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


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


DEFAULT_CONFIG = RecommenderConfig()


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


async def recommend_explore_feed(
    db: Any,
    user_id: int,
    *,
    limit: int = 10,
    cursor: int = 0,
    now: datetime | None = None,
    config: RecommenderConfig = DEFAULT_CONFIG,
) -> list[dict[str, Any]]:
    """Return up to *limit* approved public Explore questions ranked deterministically."""
    current = now or datetime.now(timezone.utc)

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
    seen_ids = {int(r["question_id"]) for r in impressions}
    affinities = compute_topic_affinities(impressions, now=current, config=config)

    cand_rows = await db.db.execute_fetchall(
        """
        SELECT question_id, topic, difficulty, question_text, options_json, created_at
        FROM explore_questions
        WHERE status = 'approved' AND question_id > ?
        ORDER BY question_id ASC
        LIMIT 250
        """,
        (int(cursor),),
    )
    candidates = [dict(r) for r in cand_rows if int(r["question_id"]) not in seen_ids]
    if not candidates:
        return []

    neutral_affinity = (config.prior_mean - 1.0) / 2.0
    scored: list[tuple[float, int, dict[str, Any]]] = []
    for c in candidates:
        qid = int(c["question_id"])
        topic = str(c["topic"])
        affinity = affinities.get(topic, neutral_affinity)
        quality = 1.0 if str(c.get("difficulty")) == "easy" else 0.7
        freshness = 0.5
        discovery = 1.0 if topic not in affinities else 0.2
        score = (
            config.w_affinity * affinity
            + config.w_quality * quality
            + config.w_freshness * freshness
            + config.w_diversity * discovery
        )
        scored.append((score, -qid, c))

    scored.sort(key=lambda x: (-x[0], -x[1]))

    result: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    cap = config.max_consecutive_same_topic

    def _to_item(c: dict[str, Any]) -> dict[str, Any]:
        return {
            "question_id": int(c["question_id"]),
            "topic": str(c["topic"]),
            "difficulty": str(c["difficulty"]),
            "question_text": str(c["question_text"]),
            "options": json.loads(c["options_json"]),
        }

    for _, _, c in scored:
        if len(result) >= limit:
            break
        t = str(c["topic"])
        if len(result) >= cap and all(result[-k]["topic"] == t for k in range(1, cap + 1)):
            deferred.append(c)
            continue
        result.append(_to_item(c))

    for c in deferred:
        if len(result) >= limit:
            break
        result.append(_to_item(c))

    return result
