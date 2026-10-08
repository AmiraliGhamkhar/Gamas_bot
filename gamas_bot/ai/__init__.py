"""Gamas AI Provider Platform.

A provider-aware, free-tier-aware inference layer for note generation. The
package owns provider metadata (:mod:`.registry`), model capability tracking
(:mod:`.models`), request/response adapters (:mod:`.adapters`), routing and
failover (:mod:`.routing`), token-budget aware chunking (:mod:`.tokens`),
usage accounting and structured lifecycle events (:mod:`.usage`).

Design contract:

* The canonical Gamas note schema (:class:`gamas_bot.structuring.StructuredNotes`)
  never changes per provider; providers differ only in *transport* details.
* Free tiers are an explicit classification, never inferred from a "$0" price
  page (see :data:`gamas_bot.ai.registry.PROVIDER_REGISTRY`).
* URL → provider resolution keeps legacy deployments working:
  ``NOTE_API_BASE_URL=https://router.bynara.id/v1`` is classified as NaraRouter
  with no configuration change.
* Secrets are handled exclusively by adapters; nothing in this package logs
  key material, prompts, transcripts or model answers.
"""

from .registry import (
    PROVIDER_CHOICES_NOTES,
    PROVIDER_REGISTRY,
    AuthStyle,
    ProviderClass,
    ProviderInfo,
    resolve_canonical,
)
from .models import ModelCapabilities, ModelInfo, ModelRegistry
from .adapters import (
    NoteAdapter,
    NoteFailure,
    NoteRequest,
    NoteResponse,
    adapter_for,
    resolve_json_strategy,
)
from .profiles import DEFAULT_PROFILES, NoteProviderProfile, RequestPolicy
from .usage import AIUsageTracker
from .routing import (
    NoteJobSession,
    PlannedRoute,
    ProviderRouter,
    RouteLeg,
    job_session_scope,
)

__all__ = [
    "AIUsageTracker",
    "AuthStyle",
    "DEFAULT_PROFILES",
    "ModelCapabilities",
    "ModelInfo",
    "ModelRegistry",
    "NoteAdapter",
    "NoteFailure",
    "NoteJobSession",
    "NoteProviderProfile",
    "NoteRequest",
    "NoteResponse",
    "PROVIDER_CHOICES_NOTES",
    "PROVIDER_REGISTRY",
    "PlannedRoute",
    "ProviderClass",
    "ProviderInfo",
    "ProviderRouter",
    "RequestPolicy",
    "RouteLeg",
    "adapter_for",
    "job_session_scope",
    "resolve_canonical",
    "resolve_json_strategy",
]
