"""Free-tier and trial policy for STT provider eligibility (spec §3, §26).

Pure functions only: no I/O, no secrets, no provider calls. The router feeds
in one registry record plus the operator's settings and receives an explicit
allow/deny decision with a stable reason code that is safe to log.

Gate summary (``STT_FREE_ONLY`` is the master switch):

* ``free``  classes (permanent / monthly / allocation) are always usable.
* ``trial`` classes (one-time credit, promotional, trial) are usable only when
  the provider is in ``STT_TRIAL_ALLOWLIST`` or ``STT_ALLOW_TRIAL_PROVIDERS``
  is true. Speechmatics and Deepgram are the operator's explicit allowlist.
* ``paid``  classes are usable only when ``STT_ALLOW_PAID_FALLBACK`` is true,
  and the router always orders them after every free/trial candidate.
* ``region_restricted`` / ``unsupported`` are never routed.
* An admin billing attestation of ``paid`` blocks free and trial use in
  free-only mode, so Gamas never silently moves onto a card-billed account.
* Operator-configured legacy providers (speechmatics, deepgram,
  openai_compatible) keep their pre-platform behaviour: paid-only
  ``openai_compatible`` endpoints stay usable, and Speechmatics/Deepgram go
  through the trial allowlist like every other trial-class provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .registry import (
    FREE_ONLY_CLASSES,
    LEGACY_STT_PROVIDERS,
    TRIAL_FREE_CLASSES,
    STTProviderClass,
    STTProviderInfo,
)

TIER_FREE = "free"
TIER_TRIAL = "trial"
TIER_PAID = "paid"
TIER_LEGACY = "legacy"

#: Operator decision (Speechmatics and Deepgram are the priority providers).
DEFAULT_TRIAL_ALLOWLIST: tuple[str, ...] = ("speechmatics", "deepgram")

#: Admin billing attestation values (provider_settings.billing_state).
BILLING_PAID = "paid"


@dataclass(frozen=True, slots=True)
class SttPolicy:
    free_only: bool = True
    allow_trial_providers: bool = False
    trial_allowlist: frozenset[str] = frozenset(DEFAULT_TRIAL_ALLOWLIST)
    allow_paid_fallback: bool = False

    @classmethod
    def from_settings(cls, settings: Any) -> "SttPolicy":
        raw_allowlist = getattr(settings, "stt_trial_allowlist", DEFAULT_TRIAL_ALLOWLIST)
        allowlist = frozenset(
            str(item).strip().lower() for item in (raw_allowlist or ()) if str(item).strip()
        )
        return cls(
            free_only=bool(getattr(settings, "stt_free_only", True)),
            allow_trial_providers=bool(getattr(settings, "stt_allow_trial_providers", False)),
            trial_allowlist=allowlist,
            allow_paid_fallback=bool(getattr(settings, "stt_allow_paid_fallback", False)),
        )


@dataclass(frozen=True, slots=True)
class GateDecision:
    allowed: bool
    tier: str
    reason: str
    warnings: tuple[str, ...] = ()


def tier_for(info: STTProviderInfo) -> str:
    """Economic tier used for ordering and gating (not the raw class)."""
    cls = info.classification
    if info.provider_slug in LEGACY_STT_PROVIDERS and cls == STTProviderClass.PAID_ONLY:
        return TIER_LEGACY
    if cls in FREE_ONLY_CLASSES:
        return TIER_FREE
    if cls in TRIAL_FREE_CLASSES:
        return TIER_TRIAL
    if cls == STTProviderClass.PAID_ONLY:
        return TIER_PAID
    return TIER_PAID  # region_restricted / unsupported are denied before tiering


def evaluate_gate(
    info: STTProviderInfo,
    policy: SttPolicy,
    *,
    billing_state: str = "",
) -> GateDecision:
    """Decide whether ``info`` may serve a job under ``policy``."""
    cls = info.classification
    if cls in (STTProviderClass.REGION_RESTRICTED, STTProviderClass.UNSUPPORTED):
        return GateDecision(False, TIER_PAID, f"class_{cls.value}")

    tier = tier_for(info)
    slug = info.provider_slug
    warnings: list[str] = []

    if tier == TIER_LEGACY:
        # Explicitly configured OpenAI-compatible endpoints keep working. The
        # endpoint may be a paid gateway, which the operator is responsible for.
        warnings.append("openai_compatible_endpoint_billing_unverified")
        return GateDecision(True, tier, "legacy_operator_configured", tuple(warnings))

    if policy.free_only and billing_state == BILLING_PAID and tier != TIER_PAID:
        return GateDecision(False, tier, "billing_attested_paid")

    if tier == TIER_FREE:
        return GateDecision(True, tier, "free_class")

    if tier == TIER_TRIAL:
        if not policy.free_only:
            return GateDecision(True, tier, "free_only_disabled", tuple(warnings))
        if slug in policy.trial_allowlist:
            return GateDecision(True, tier, "trial_allowlisted", tuple(warnings))
        if policy.allow_trial_providers:
            return GateDecision(True, tier, "trial_enabled_globally", tuple(warnings))
        return GateDecision(False, tier, "trial_not_allowed")

    # TIER_PAID
    if not policy.free_only:
        return GateDecision(True, tier, "free_only_disabled", tuple(warnings))
    if policy.allow_paid_fallback:
        return GateDecision(True, tier, "paid_fallback_enabled", tuple(warnings))
    return GateDecision(False, tier, "paid_not_allowed")
