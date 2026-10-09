"""STT route planning (spec §28-§31, §50-§51).

The router is deliberately pure: ``stt.py`` gathers the facts that need I/O
(keys, language mapping, upload limits, billing attestation) and this module
turns them into an explicit, explainable plan. Every candidate gets either an
``eligible`` verdict or a stable list of denial reasons, so an operator can
see *why* a provider was skipped without reading provider responses.

Ordering is the explicit route (``STT_DEFAULT_ROUTE``, or the legacy chain
``STT_PRIMARY`` -> speechmatics -> deepgram -> openai_compatible). The advisory
score is reported for the admin preview and logs; it never reorders providers
on its own, because accuracy ranking must come from benchmarks, not from
marketing-derived constants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .policy import TIER_FREE, TIER_LEGACY, TIER_PAID, TIER_TRIAL, SttPolicy, evaluate_gate, tier_for
from .registry import LEGACY_STT_PROVIDERS, STT_PROVIDER_REGISTRY, STTProviderInfo, stt_registry_info

#: The pre-platform fallback chain. Native providers are only used when routed
#: explicitly, so the default behaviour of an existing deployment is unchanged.
LEGACY_ROUTE_ORDER: tuple[str, ...] = ("speechmatics", "deepgram", "openai_compatible")

#: Gemini Transcribe: diarization or word timestamps cut the per-request limit
#: from 1 hour to 30 minutes (official docs, updated 2026-09-23).
GEMINI_FEATURE_DURATION_LIMIT_SECONDS = 1800

#: Advisory score weights (documented, not used for ordering).
SCORE_WEIGHTS = {"accuracy": 0.5, "persian": 0.2, "features": 0.15, "headroom": 0.15}
_HEADROOM = {TIER_FREE: 1.0, TIER_LEGACY: 0.6, TIER_TRIAL: 0.5, TIER_PAID: 0.2}


def _normalise_slugs(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    items = value.split(",") if isinstance(value, str) else list(value)
    seen: dict[str, None] = {}
    for item in items:
        slug = str(item).strip().lower()
        if slug:
            seen.setdefault(slug, None)
    return tuple(seen)


def resolve_route(settings: Any) -> tuple[str, ...]:
    """Explicit provider order for this deployment.

    ``STT_DEFAULT_ROUTE`` wins when set. Otherwise the primary is tried first
    and the legacy fallbacks follow in their historical order.
    """
    configured = _normalise_slugs(getattr(settings, "stt_default_route", ""))
    if configured:
        return configured
    primary = str(getattr(settings, "stt_primary", "speechmatics") or "speechmatics").strip().lower()
    return (primary,) + tuple(name for name in LEGACY_ROUTE_ORDER if name != primary)


@dataclass(frozen=True, slots=True)
class SttRequirements:
    """What one job needs from a provider, derived once per job."""

    persian: bool
    file_bytes: int
    duration_seconds: float | None = None
    diarization: bool = False
    word_timestamps: bool = False
    vocabulary_terms: int = 0

    @classmethod
    def for_job(
        cls,
        *,
        language: str,
        file_bytes: int,
        duration_seconds: float | None,
        diarization: bool = False,
        word_timestamps: bool = False,
        vocabulary_terms: int = 0,
    ) -> "SttRequirements":
        base = (language or "").strip().lower().split("-")[0].split("_")[0]
        return cls(
            persian=base == "fa",
            file_bytes=int(file_bytes),
            duration_seconds=duration_seconds,
            diarization=diarization,
            word_timestamps=word_timestamps,
            vocabulary_terms=int(vocabulary_terms),
        )


@dataclass(frozen=True, slots=True)
class CandidateFacts:
    """I/O-derived facts about one candidate, supplied by the caller."""

    has_credential: bool
    #: ``stt_provider_settings.enabled`` (admin toggle); default on.
    admin_enabled: bool = True
    billing_state: str = ""
    max_upload: int = 0
    #: Stable code when the requested language cannot be mapped (e.g. the
    #: provider rejects ``auto``), otherwise ``None``.
    language_error: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    provider: str
    position: int
    eligible: bool
    tier: str
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    score: float = 0.0
    duration_limit: int | None = None

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "position": self.position,
            "eligible": self.eligible,
            "tier": self.tier,
            "reasons": ",".join(self.reasons) or "-",
            "warnings": ",".join(self.warnings) or "-",
        }


@dataclass(frozen=True, slots=True)
class RoutePlan:
    route: tuple[str, ...]
    decisions: tuple[CandidateDecision, ...]
    execution: tuple[str, ...]
    _by_name: dict = field(default_factory=dict, compare=False, repr=False)

    def decision_for(self, provider: str) -> CandidateDecision | None:
        return self._by_name.get(provider)


def duration_limit(info: STTProviderInfo, req: SttRequirements) -> int | None:
    """Longest audio accepted for this request shape, in seconds."""
    if info.provider_slug == "gemini_transcribe" and (req.diarization or req.word_timestamps):
        return GEMINI_FEATURE_DURATION_LIMIT_SECONDS
    return info.max_audio_duration


def advisory_score(info: STTProviderInfo, req: SttRequirements, tier: str) -> float:
    """Informational 0..1 score for the admin preview. Never used to reorder."""
    wanted = []
    if req.diarization:
        wanted.append(info.supports_diarization)
    if req.word_timestamps:
        wanted.append(info.supports_word_timestamps)
    features = 1.0 if not wanted else sum(1 for ok in wanted if ok) / len(wanted)
    score = (
        SCORE_WEIGHTS["accuracy"] * float(info.quality_score or 0.0)
        + SCORE_WEIGHTS["persian"] * (1.0 if info.persian_batch else 0.0)
        + SCORE_WEIGHTS["features"] * features
        + SCORE_WEIGHTS["headroom"] * _HEADROOM.get(tier, 0.0)
    )
    return round(score, 3)


def evaluate_candidate(
    provider: str,
    position: int,
    req: SttRequirements,
    policy: SttPolicy,
    facts: CandidateFacts,
) -> CandidateDecision:
    info = stt_registry_info(provider)
    if info is None or provider not in STT_PROVIDER_REGISTRY:
        return CandidateDecision(provider, position, False, TIER_PAID, ("unknown_provider",))

    reasons: list[str] = []
    warnings: list[str] = []
    gate = evaluate_gate(info, policy, billing_state=facts.billing_state)
    if not gate.allowed:
        reasons.append(gate.reason)
    warnings.extend(gate.warnings)
    if not info.enabled:
        reasons.append("provider_disabled")
    if not facts.admin_enabled:
        reasons.append("admin_disabled")
    if not facts.has_credential:
        reasons.append("not_configured")
    if req.persian and not info.persian_batch:
        reasons.append("persian_unsupported")
    if facts.language_error:
        reasons.append(facts.language_error)
    if facts.max_upload and req.file_bytes >= facts.max_upload:
        reasons.append("file_too_large")

    limit = duration_limit(info, req)
    if req.duration_seconds is None:
        if limit:
            warnings.append("duration_unknown")
    elif limit and req.duration_seconds > limit:
        reasons.append("duration_too_long")

    # Legacy providers keep their historical request shape and ignore the
    # feature flags, so the feature checks apply to native adapters only.
    native = provider not in LEGACY_STT_PROVIDERS
    if native and req.diarization and not info.supports_diarization:
        reasons.append("feature_unsupported:diarization")
    if native and req.word_timestamps and not info.supports_word_timestamps:
        reasons.append("feature_unsupported:word_timestamps")
    if native and req.vocabulary_terms:
        if not info.vocabulary_supported:
            warnings.append("vocabulary_not_supported_ignored")
        elif (req.diarization or req.word_timestamps) and provider == "gemini_transcribe":
            warnings.append("vocabulary_dropped_gemini_constraint")

    tier = tier_for(info)
    return CandidateDecision(
        provider=provider,
        position=position,
        eligible=not reasons,
        tier=tier,
        reasons=tuple(dict.fromkeys(reasons)),
        warnings=tuple(dict.fromkeys(warnings)),
        score=advisory_score(info, req, tier),
        duration_limit=limit,
    )


def plan_route(
    settings: Any,
    req: SttRequirements,
    facts_by_provider: Mapping[str, CandidateFacts],
    *,
    policy: SttPolicy | None = None,
) -> RoutePlan:
    """Evaluate every routed provider and return the execution order.

    Paid-tier candidates always run after every free/trial/legacy candidate.
    With ``STT_FALLBACK_ENABLED=false`` only the first eligible provider runs.
    ``STT_MAX_PROVIDER_FAILOVERS`` caps how many providers one job may try.
    """
    policy = policy or SttPolicy.from_settings(settings)
    route = resolve_route(settings)
    decisions = []
    for position, name in enumerate(route):
        facts = facts_by_provider.get(name) or CandidateFacts(has_credential=False)
        decisions.append(evaluate_candidate(name, position, req, policy, facts))
    eligible = [item for item in decisions if item.eligible]
    ordered = sorted(eligible, key=lambda item: (item.tier == TIER_PAID, item.position))
    if not bool(getattr(settings, "stt_fallback_enabled", True)):
        ordered = ordered[:1]
    failovers = int(getattr(settings, "stt_max_provider_failovers", 4) or 0)
    ordered = ordered[: max(1, failovers + 1)]
    by_name = {item.provider: item for item in decisions}
    return RoutePlan(
        route=route,
        decisions=tuple(decisions),
        execution=tuple(item.provider for item in ordered),
        _by_name=by_name,
    )
