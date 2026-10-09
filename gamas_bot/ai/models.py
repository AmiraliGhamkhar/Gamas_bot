"""Model capability registry: static seeds + live discovery + SQLite cache.

Hard-coding a free-model catalog ages badly; this module therefore keeps three
tiers, in preference order:

1. **Live discovery** — the provider's official model listing, synced on demand
   and cached in the ``ai_models`` table (TTL: ``settings.ai_provider_sync_ttl``).
2. **Static seeds** — exact reviewed per-model capability/free facts fill gaps
   that generic discovery cannot verify. Live non-unknown free evidence and
   availability/deprecation facts remain current. Seeds do not claim a live
   verification date.
3. **Conservative defaults** — when nothing is known a model is treated as
   text-only prompt-JSON, unknown-free, with no vendor parameters beyond the
   OpenAI core.

"Free" is never inferred from a "$0" price page or provider-wide class. A model
earns a free status only from explicit model-level evidence: OpenRouter's
``:free`` convention, an intersection with an authoritative plan model list
(Nara), or a reviewed per-model permanent/promotional entitlement.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone



logger = logging.getLogger(__name__)

#: Free-status vocabulary (model level).
FREE_UNKNOWN = "unknown"
FREE_PERMANENT = "free_permanent"
FREE_PLAN = "free_plan"           # free because the provider's plan is free
FREE_PROMOTIONAL = "free_promotional"
PAID = "paid"


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    """Everything a request builder may legally send to the model.

    Nothing is inferred across models of one provider: unknown stays ``False``,
    which degrades to the strict Gamas prompt + parser.
    """

    supports_text: bool = True
    supports_image: bool = False
    supports_audio: bool = False
    supports_pdf: bool = False
    supports_tools: bool = False
    supports_reasoning: bool = False
    supports_streaming: bool = True
    supports_json_object: bool = False
    supports_json_schema: bool = False
    supports_strict_json_schema: bool = False
    supports_temperature: bool = True
    supports_top_p: bool = True
    supports_top_k: bool = False
    supports_reasoning_effort: bool = False
    supports_seed: bool = False
    supports_system_message: bool = True
    requires_paid_billing: bool = False
    #: Non-zero when the provider recommends a specific reasoning behaviour.
    recommended_reasoning_effort: str = ""  # "", "none", "low", "medium", "high"

    def to_json(self) -> str:
        return json.dumps({k: getattr(self, k) for k in self.__dataclass_fields__}, sort_keys=True)

    @classmethod
    def from_json(cls, payload: str | None) -> "ModelCapabilities":
        if not payload:
            return cls()
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        valid = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in valid})


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """One model on one provider."""

    provider: str
    model_id: str
    display_name: str = ""
    context_window: int = 0          # tokens; 0 = unknown
    max_output_tokens: int = 0       # tokens; 0 = unknown
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)
    free_status: str = FREE_UNKNOWN
    #: ISO date after which a promotional-free model stops being free.
    free_until: str | None = None
    commercial_use_allowed: bool = True
    region_restriction: str = ""
    deprecated: bool = False
    deprecation_date: str | None = None
    available: bool = True
    #: NVIDIA build.nvidia.com advertises some models through a "Free Endpoint".
    #: That is a live capability, not a permanent guarantee: it can be withdrawn
    #: or deprecated per model, so it is recorded per sync and re-checked.
    free_endpoint: bool = False
    source: str = "static_seed"      # static_seed | live:<endpoint> | admin
    source_last_verified_at: str | None = None
    quality_score: float | None = None

    @property
    def label(self) -> str:
        return self.display_name or self.model_id

    def free_now(self, *, at: datetime | None = None) -> bool:
        """Whether the model is usable *today* under a free plan."""
        if self.free_status not in {FREE_PERMANENT, FREE_PLAN, FREE_PROMOTIONAL}:
            return False
        if self.free_status == FREE_PROMOTIONAL:
            # A promotion with no authoritative expiry is not an evergreen
            # entitlement. Unknown validity must fail closed in FREE_ONLY.
            if not self.free_until:
                return False
            reference = at or datetime.now(timezone.utc)
            try:
                until = datetime.fromisoformat(str(self.free_until).replace("Z", "+00:00"))
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
                if reference > until:
                    return False
            except (ValueError, TypeError, OverflowError):
                return False
        return True

    def free_only_eligible(self) -> bool:
        """All of the FREE_ONLY conditions for this model (spec §3)."""
        if self.deprecated or not self.available:
            return False
        if self.capabilities.requires_paid_billing:
            return False
        if not self.commercial_use_allowed:
            return False
        if not self.capabilities.supports_text:
            return False
        return self.free_now()


# ---------------------------------------------------------------------------
# Static seeds — exact reviewed facts fill fields generic live catalogs omit.
# ---------------------------------------------------------------------------


def _seed(slug: str, model_id: str, caps: ModelCapabilities, **kwargs) -> ModelInfo:
    return ModelInfo(provider=slug, model_id=model_id, capabilities=caps, **kwargs)


STATIC_SEEDS: dict[str, list[ModelInfo]] = {
    "gemini": [
        # Current stable Gemini model (verified 2026-10-09): the official
        # model card lists GenerateContent support, structured outputs, a
        # 1,048,576-token input window and 65,536 output-token limit. Free-tier
        # price still requires the key's separate no-overage attestation.
        _seed(
            "gemini", "gemini-3.8-flash",
            ModelCapabilities(
                supports_json_object=True, supports_json_schema=True,
                supports_strict_json_schema=True, supports_reasoning=True,
                supports_reasoning_effort=True, supports_tools=True,
                supports_image=True, supports_audio=True, supports_pdf=True,
                supports_temperature=False, supports_system_message=True,
            ),
            display_name="Gemini 3.8 Flash",
            context_window=1_048_576, max_output_tokens=65_536,
            free_status=FREE_PLAN,
        ),
        _seed(
            "gemini", "gemini-3.5-flash-lite",
            ModelCapabilities(
                supports_json_object=True, supports_json_schema=True,
                supports_strict_json_schema=True, supports_reasoning=True,
                supports_reasoning_effort=True, supports_tools=True,
                supports_image=True, supports_audio=True, supports_pdf=True,
                supports_temperature=False, supports_system_message=True,
            ),
            display_name="Gemini 3.5 Flash-Lite",
            context_window=1_048_576, max_output_tokens=65_536,
            free_status=FREE_PLAN,
        ),
        _seed(
            "gemini", "gemini-2.5-flash",
            ModelCapabilities(
                supports_json_object=True, supports_json_schema=True,
                supports_strict_json_schema=True, supports_reasoning=True,
                supports_tools=True, supports_image=True, supports_audio=True,
                supports_pdf=True, supports_system_message=True,
            ),
            display_name="Gemini 2.5 Flash",
            context_window=1_048_576, max_output_tokens=65_536,
            free_status=FREE_PLAN,
        ),
        _seed(
            "gemini", "gemini-2.5-flash-lite",
            ModelCapabilities(
                supports_json_object=True, supports_json_schema=True,
                supports_strict_json_schema=True, supports_reasoning=True,
                supports_tools=True, supports_image=True, supports_audio=True,
                supports_pdf=True, supports_system_message=True,
            ),
            display_name="Gemini 2.5 Flash Lite",
            context_window=1_048_576, max_output_tokens=65_536,
            free_status=FREE_PLAN,
        ),
        _seed(
            "gemini", "gemini-2.5-pro",
            ModelCapabilities(
                supports_json_object=True, supports_json_schema=True,
                supports_strict_json_schema=True, supports_reasoning=True,
                supports_tools=True, supports_image=True, supports_audio=True,
                supports_pdf=True, supports_system_message=True,
            ),
            display_name="Gemini 2.5 Pro",
            context_window=1_048_576, max_output_tokens=65_536,
            free_status=FREE_PLAN,
        ),
    ],
    "nara": [],  # catalog is account-plan specific; always discovered live
    "sambanova": [
        # Official 2026-10-09 rate-limit docs list these exact production
        # model IDs on both the Free and Developer tiers. Free-tier capacity is
        # 20 RPM / 20 RPD / 200K TPD per model; preview models are deliberately
        # not seeded as production-eligible.
        _seed(
            "sambanova", "DeepSeek-V3.1",
            ModelCapabilities(
                supports_json_object=True, supports_json_schema=True,
                supports_system_message=True,
            ),
            display_name="DeepSeek V3.1",
            free_status=FREE_PLAN,
        ),
        _seed(
            "sambanova", "Meta-Llama-3.3-70B-Instruct",
            ModelCapabilities(
                supports_json_object=True, supports_system_message=True,
            ),
            display_name="Llama 3.3 70B Instruct",
            free_status=FREE_PLAN,
        ),
        _seed(
            "sambanova", "gpt-oss-120b",
            ModelCapabilities(
                supports_json_object=True, supports_system_message=True,
            ),
            display_name="GPT OSS 120B",
            free_status=FREE_PLAN,
        ),
    ],
    "groq": [
        _seed(
            "groq", "openai/gpt-oss-120b",
            ModelCapabilities(
                supports_json_object=True, supports_json_schema=True,
                supports_strict_json_schema=True, supports_reasoning=True,
                supports_reasoning_effort=True, supports_tools=True,
            ),
            display_name="GPT OSS 120B",
            context_window=131_072, max_output_tokens=32_768,
            # Groq's verified rate-limit table is for Developer plan, not an
            # explicitly no-charge production entitlement.
            free_status=FREE_UNKNOWN,
        ),
        _seed(
            "groq", "openai/gpt-oss-20b",
            ModelCapabilities(
                supports_json_object=True, supports_json_schema=True,
                supports_strict_json_schema=True, supports_reasoning=True,
                supports_reasoning_effort=True, supports_tools=True,
            ),
            display_name="GPT OSS 20B",
            context_window=131_072, max_output_tokens=32_768,
            free_status=FREE_UNKNOWN,
        ),
        # The exact Qwen ID is documented by Groq for structured outputs and
        # recommends reasoning_effort="none" for efficient instruction mode.
        # This is capability evidence only, not evidence of free-plan eligibility.
        _seed(
            "groq", "qwen/qwen3.8-27b",
            ModelCapabilities(
                supports_json_object=True, supports_json_schema=True,
                supports_strict_json_schema=True, supports_reasoning=True,
                supports_reasoning_effort=True, supports_tools=True,
                supports_image=True,
                recommended_reasoning_effort="none",
            ),
            display_name="Qwen 3.8 27B",
            context_window=131_072, max_output_tokens=16_384,
            free_status=FREE_UNKNOWN,
        ),
    ],
    "openrouter": [
        _seed(
            "openrouter", "openrouter/free",
            ModelCapabilities(supports_json_object=False),
            display_name="OpenRouter Free (auto)",
            free_status=FREE_PLAN,
        ),
    ],
    "mistral": [
        _seed(
            "mistral", "mistral-small-latest",
            # JSON-object mode is documented for this chat API/model family;
            # schema mode remains disabled until this exact model is verified.
            ModelCapabilities(supports_json_object=True),
            display_name="Mistral Small",
            context_window=131_072, max_output_tokens=8_192,
            # Free mode exists, but docs do not establish per-model free
            # entitlement or overage behavior for this exact ID.
            free_status=FREE_UNKNOWN,
        ),
    ],
    "zai": [
        # The official pricing table lists both model IDs as Free (verified
        # 2026-10-09); retain them as reviewed FREE_PLAN entries, not a claim
        # that every account has access or cannot incur overage.
        _seed(
            "zai", "glm-4.5-flash",
            ModelCapabilities(supports_json_object=True, supports_reasoning=True),
            display_name="GLM 4.5 Flash",
            context_window=131_072, max_output_tokens=16_384,
            free_status=FREE_PLAN,
        ),
        _seed(
            "zai", "glm-4.7-flash",
            ModelCapabilities(supports_json_object=True, supports_reasoning=True),
            display_name="GLM 4.7 Flash",
            free_status=FREE_PLAN,
        ),
    ],
    "nvidia": [],  # catalog + "Free Endpoint" availability are live facts
    # Workers AI is account-scoped; never select a stale static model. Require
    # the current account's Model Search snapshot before routing.
    "cloudflare": [],
    "huggingface": [],
    "alibaba": [
        _seed(
            "alibaba", "qwen-plus",
            ModelCapabilities(supports_json_object=True),
            display_name="Qwen Plus",
            context_window=131_072, max_output_tokens=8_192,
            free_status=FREE_PROMOTIONAL,
        ),
    ],
    "cohere": [
        _seed(
            "cohere", "command-a-03-2025",
            ModelCapabilities(supports_json_object=True, supports_json_schema=True),
            display_name="Command A",
            context_window=256_000, max_output_tokens=8_192,
            free_status=FREE_PROMOTIONAL,
            commercial_use_allowed=False,
        ),
    ],
    "cerebras": [
        _seed(
            "cerebras", "gpt-oss-120b",
            ModelCapabilities(supports_json_object=True, supports_strict_json_schema=True),
            display_name="GPT OSS 120B",
            context_window=131_072, max_output_tokens=32_768,
            free_status=FREE_PROMOTIONAL,
        ),
    ],
}


def static_models(slug: str) -> list[ModelInfo]:
    return list(STATIC_SEEDS.get(slug, []))


def default_model_for(slug: str) -> str | None:
    """The recommended default model ID for a provider (seed or None)."""
    seeds = STATIC_SEEDS.get(slug) or []
    return seeds[0].model_id if seeds else None


def fallback_free_model(slug: str) -> str | None:
    """A seed model known free under the provider's free plan, if any."""
    for info in STATIC_SEEDS.get(slug) or []:
        if info.free_only_eligible() and info.free_status in {FREE_PERMANENT, FREE_PLAN}:
            return info.model_id
    return None


# ---------------------------------------------------------------------------
# Live discovery parsing
# ---------------------------------------------------------------------------


def _openai_style_ids(payload) -> list[str]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    ids: list[str] = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip():
            ids.append(item["id"].strip())
    return ids


def _gemini_model_entries(payload) -> list[dict]:
    models = payload.get("models") if isinstance(payload, dict) else None
    return [m for m in (models or []) if isinstance(m, dict)]


def infer_openrouter_free(model_id: str) -> bool:
    """OpenRouter's documented free-model convention: the ``:free`` suffix."""
    return model_id.strip().lower().endswith(":free")


def infer_capabilities(provider: str, model_id: str) -> ModelCapabilities:
    """Conservative per-model capabilities for a *live-discovered* model.

    Exact static seeds win. Heuristics below are intentionally narrow and are
    backed by provider model tables; an unknown model gets only the OpenAI core
    request and prompt-enforced JSON.
    """
    for seed in STATIC_SEEDS.get(provider, []):
        if seed.model_id == model_id:
            return seed.capabilities
    lowered = model_id.strip().lower()
    if provider == "gemini":
        # The currently verified 2.5 models are explicit seeds. New/future IDs
        # stay conservative until their own generateContent docs are reviewed.
        return ModelCapabilities()
    if provider == "openrouter":
        # Capability support is model-specific; the marketplace catalog does
        # not prove JSON/schema support for every upstream model.
        return ModelCapabilities()
    if provider == "groq":
        strict_models = {
            "openai/gpt-oss-20b",
            "openai/gpt-oss-120b",
            "qwen/qwen3.8-27b",
        }
        strict = lowered in strict_models
        return ModelCapabilities(
            supports_json_object=True,
            supports_json_schema=strict,
            supports_strict_json_schema=strict,
            supports_reasoning_effort=strict,
            supports_image=lowered == "qwen/qwen3.8-27b",
            recommended_reasoning_effort="none" if lowered == "qwen/qwen3.8-27b" else "",
        )
    if provider == "zai":
        # Z.AI's structured-output guide documents JSON-object mode for named
        # GLM families; JSON Schema is not implied for every GLM model.
        family_has_json = any(
            token in lowered for token in ("glm-4.5", "glm-4.6", "glm-4.7")
        )
        return ModelCapabilities(
            supports_json_object=family_has_json,
            supports_reasoning="thinking" in lowered or "glm-4.5" in lowered
            or "glm-4.6" in lowered or "glm-4.7" in lowered,
        )
    if provider == "cerebras":
        strict_models = {
            "gpt-oss-120b",
            "qwen-3.8-27b",
            "kimi-k2.7-code",
        }
        strict = lowered in strict_models
        return ModelCapabilities(
            supports_json_object=strict,
            supports_json_schema=strict,
            supports_strict_json_schema=strict,
            supports_tools=strict,
            supports_reasoning="gpt-oss" in lowered or "qwen" in lowered,
        )
    if provider == "mistral":
        # Model-specific JSON support is populated only by reviewed seeds.
        return ModelCapabilities()
    if provider in {"sambanova", "cloudflare", "alibaba", "huggingface", "nvidia", "cohere"}:
        return ModelCapabilities()
    return ModelCapabilities()


def infer_free_status(provider: str, model_id: str) -> str:
    """Model-level free status from *explicit* metadata only (spec §3/§5)."""
    for seed in STATIC_SEEDS.get(provider, []):
        if seed.model_id == model_id:
            return seed.free_status
    if provider == "openrouter":
        return FREE_PLAN if infer_openrouter_free(model_id) or model_id == "openrouter/free" else PAID
    # Provider-wide plan classification does not prove that this specific
    # model is included. Explicit static provider-plan model lists above and
    # live plan-aware adapters (Nara) are the only non-OpenRouter sources.
    return FREE_UNKNOWN


def parse_discovery(provider: str, payload) -> list[ModelInfo]:
    """Normalize one provider's model-listing payload into :class:`ModelInfo`."""
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    results: list[ModelInfo] = []
    if provider == "gemini":
        for entry in _gemini_model_entries(payload):
            name = str(entry.get("name", "")).removeprefix("models/")
            if not name:
                continue
            methods = entry.get("supportedGenerationMethods") or []
            if "generateContent" not in methods:
                continue
            caps = infer_capabilities("gemini", name)
            results.append(
                ModelInfo(
                    provider="gemini",
                    model_id=name,
                    display_name=str(entry.get("displayName") or name),
                    context_window=int(entry.get("inputTokenLimit") or 0),
                    max_output_tokens=int(entry.get("outputTokenLimit") or 0),
                    capabilities=caps,
                    free_status=infer_free_status("gemini", name),
                    source="live:/v1beta/models",
                    source_last_verified_at=now,
                )
            )
        return results
    for model_id in _openai_style_ids(payload):
        results.append(
            ModelInfo(
                provider=provider,
                model_id=model_id,
                capabilities=infer_capabilities(provider, model_id),
                free_status=infer_free_status(provider, model_id),
                source="live:/v1/models" if provider != "gemini" else "live",
                source_last_verified_at=now,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Model registry: DB cache with TTL + static fallback
# ---------------------------------------------------------------------------


class ModelRegistry:
    """Reads/writes the ``ai_models`` cache; sync is explicit, never implicit."""

    def __init__(self, db, settings):
        self.db = db
        self.settings = settings

    @property
    def ttl_seconds(self) -> float:
        return max(60.0, float(getattr(self.settings, "ai_provider_sync_ttl", 86400)))

    async def cached(self, provider: str, *, include_unavailable: bool = False) -> list[ModelInfo]:
        rows = await self.db.ai_models_list(provider, include_unavailable=include_unavailable)
        results = [self._row_to_info(row) for row in rows]
        if results:
            return results
        return static_models(provider)

    async def resolve(self, provider: str, model_id: str) -> ModelInfo:
        """Best-known metadata for a model (live, reviewed-seed overlay, defaults)."""
        row = await self.db.ai_model_get(provider, model_id)
        if row:
            return self._row_to_info(row)
        for seed in STATIC_SEEDS.get(provider, []):
            if seed.model_id == model_id:
                return seed
        return ModelInfo(
            provider=provider,
            model_id=model_id,
            capabilities=infer_capabilities(provider, model_id),
            free_status=infer_free_status(provider, model_id),
            source="inferred",
        )

    def stale(self, provider: str, synced_at: str | None) -> bool:
        """Whether the cached catalog is older than the configured TTL."""
        if not synced_at:
            return True
        try:
            last = datetime.fromisoformat(str(synced_at).replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError, OverflowError):
            return True
        age = datetime.now(timezone.utc) - last.astimezone(timezone.utc)
        return age.total_seconds() > self.ttl_seconds

    async def apply_discovery(self, provider: str, discovered: list[ModelInfo]) -> dict:
        """Persist a live catalog; models that vanished become unavailable.

        Historical usage records are never deleted; disappearing models are
        marked ``available=0`` (spec §38).
        """
        if not discovered:
            return {"synced": 0, "deactivated": 0}
        reviewed = [self._apply_reviewed_seed(info) for info in discovered]
        stats = await self.db.ai_models_upsert_discovery(
            provider,
            [self._info_to_row(info) for info in reviewed],
        )
        return stats

    @staticmethod
    def _apply_reviewed_seed(info: ModelInfo) -> ModelInfo:
        """Keep exact reviewed facts when a live catalog omits those fields.

        Live non-unknown free-status evidence may supersede a seed (for
        example, an exact paid/free variant); `unknown` may not erase a reviewed
        status. Capabilities remain the exact model-reviewed contract because
        generic catalogs generally do not describe request-feature support.
        """
        seed = next(
            (item for item in STATIC_SEEDS.get(info.provider, ()) if item.model_id == info.model_id),
            None,
        )
        if seed is None:
            return info
        free_status = info.free_status
        free_until = info.free_until
        if free_status == FREE_UNKNOWN and seed.free_status != FREE_UNKNOWN:
            free_status = seed.free_status
            free_until = seed.free_until
        return replace(
            info,
            display_name=info.display_name or seed.display_name,
            context_window=info.context_window or seed.context_window,
            max_output_tokens=info.max_output_tokens or seed.max_output_tokens,
            capabilities=seed.capabilities,
            free_status=free_status,
            free_until=free_until,
            commercial_use_allowed=info.commercial_use_allowed and seed.commercial_use_allowed,
            region_restriction=info.region_restriction or seed.region_restriction,
        )

    @staticmethod
    def _row_to_info(row: dict) -> ModelInfo:
        info = ModelInfo(
            provider=str(row["provider"]),
            model_id=str(row["model"]),
            display_name=str(row.get("display_name") or ""),
            context_window=int(row.get("context_window") or 0),
            max_output_tokens=int(row.get("max_output_tokens") or 0),
            capabilities=ModelCapabilities.from_json(row.get("capabilities_json")),
            free_status=str(row.get("free_status") or FREE_UNKNOWN),
            free_until=row.get("free_until"),
            commercial_use_allowed=bool(row.get("commercial_use_allowed", 1)),
            region_restriction=str(row.get("region_restriction") or ""),
            deprecated=bool(row.get("deprecated", 0)),
            deprecation_date=row.get("deprecation_date"),
            available=bool(row.get("available", 1)),
            free_endpoint=bool(row.get("free_endpoint", 0)),
            source=str(row.get("source") or "static_seed"),
            source_last_verified_at=row.get("source_last_verified_at"),
            quality_score=row.get("quality_score"),
        )
        return ModelRegistry._apply_reviewed_seed(info)

    @staticmethod
    def _info_to_row(info: ModelInfo) -> dict:
        return {
            "provider": info.provider,
            "model": info.model_id,
            "display_name": info.display_name,
            "context_window": info.context_window,
            "max_output_tokens": info.max_output_tokens,
            "capabilities_json": info.capabilities.to_json(),
            "free_status": info.free_status,
            "free_until": info.free_until,
            "commercial_use_allowed": 1 if info.commercial_use_allowed else 0,
            "region_restriction": info.region_restriction,
            "deprecated": 1 if info.deprecated else 0,
            "deprecation_date": info.deprecation_date,
            "available": 1 if info.available else 0,
            "free_endpoint": 1 if info.free_endpoint else 0,
            "source": info.source,
            "source_last_verified_at": info.source_last_verified_at,
            "quality_score": info.quality_score,
        }


#: Ranking used when suggesting a replacement for a deprecated/unavailable
#: model (spec §16). Free and verified beats unknown beats paid.
_FREE_RANK = {
    FREE_PERMANENT: 0,
    FREE_PLAN: 1,
    FREE_PROMOTIONAL: 2,
    FREE_UNKNOWN: 3,
    PAID: 4,
}


def suggest_replacement(
    catalog: list[ModelInfo], model_id: str
) -> ModelInfo | None:
    """Suggest a live model to replace a deprecated or withdrawn one.

    NVIDIA's hosted catalog rotates: endpoints are withdrawn and models are
    deprecated independently of Gamas. Rather than leaving an administrator
    with a dead route, the best still-available alternative on the same
    provider is offered — preferring an equivalent free endpoint, then a
    non-deprecated model, then the largest context window as a tie-breaker.
    """
    candidates = [
        info
        for info in catalog
        if info.model_id != model_id and info.available and not info.deprecated
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda info: (
            0 if info.free_endpoint else 1,
            _FREE_RANK.get(info.free_status, 3),
            -int(info.context_window or 0),
            info.model_id,
        )
    )
    return candidates[0]


def monotonic() -> float:
    return time.monotonic()
