"""Model capability registry: static seeds + live discovery + SQLite cache.

Hard-coding a free-model catalog ages badly; this module therefore keeps three
tiers, in preference order:

1. **Live discovery** — the provider's official model listing, synced on demand
   and cached in the ``ai_models`` table (TTL: ``settings.ai_provider_sync_ttl``).
2. **Static seeds** — conservative per-model metadata used only when discovery
   is unavailable. Seeds are marked ``source="static_seed"`` and never claim a
   verification date.
3. **Conservative defaults** — when nothing is known a model is treated as
   text-only prompt-JSON, unknown-free, with no vendor parameters beyond the
   OpenAI core.

"Free" is never inferred from a "$0" price page. A model earns a free status
only from explicit per-model metadata: OpenRouter's documented ``:free`` suffix,
a free-plan provider whose *plan* is account-level free, or a recorded
permanent/promotional free entry with an expiry.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from .registry import ProviderClass, registry_info

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
        if self.free_status == FREE_PROMOTIONAL and self.free_until:
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
# Static seeds — conservative; live discovery supersedes them.
# ---------------------------------------------------------------------------


def _seed(slug: str, model_id: str, caps: ModelCapabilities, **kwargs) -> ModelInfo:
    return ModelInfo(provider=slug, model_id=model_id, capabilities=caps, **kwargs)


STATIC_SEEDS: dict[str, list[ModelInfo]] = {
    "gemini": [
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
            free_status=FREE_PLAN,
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
            free_status=FREE_PLAN,
        ),
        _seed(
            "groq", "llama-3.3-70b-versatile",
            ModelCapabilities(supports_json_object=True, supports_tools=True),
            display_name="Llama 3.3 70B",
            context_window=131_072, max_output_tokens=32_768,
            free_status=FREE_PLAN,
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
            ModelCapabilities(supports_json_object=True, supports_json_schema=True),
            display_name="Mistral Small",
            context_window=131_072, max_output_tokens=8_192,
            free_status=FREE_PLAN,
        ),
    ],
    "sambanova": [
        _seed(
            "sambanova", "Meta-Llama-3.3-70B-Instruct",
            ModelCapabilities(supports_json_object=True),
            display_name="Llama 3.3 70B Instruct",
            context_window=131_072, max_output_tokens=8_192,
            free_status=FREE_PLAN,
        ),
    ],
    "zai": [
        _seed(
            "zai", "glm-4.5-flash",
            ModelCapabilities(supports_json_object=True, supports_reasoning=True),
            display_name="GLM 4.5 Flash",
            context_window=131_072, max_output_tokens=16_384,
            free_status=FREE_PERMANENT,
        ),
    ],
    "nvidia": [],  # catalog + "Free Endpoint" availability are live facts
    "cloudflare": [
        _seed(
            "cloudflare", "@cf/meta/llama-3.1-8b-instruct",
            ModelCapabilities(supports_json_object=True),
            display_name="Llama 3.1 8B Instruct",
            context_window=131_072, max_output_tokens=8_192,
            free_status=FREE_PLAN,
        ),
    ],
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
    """Provider-consistent capability inference for a *live-discovered* model.

    Conservative by design: only features that are safe to send without the
    provider rejecting the request are enabled. A static seed for the same
    model id always wins (it was reviewed), then family-name heuristics.
    """
    for seed in STATIC_SEEDS.get(provider, []):
        if seed.model_id == model_id:
            return seed.capabilities
    lowered = model_id.lower()
    caps = ModelCapabilities()
    if provider == "gemini":
        return ModelCapabilities(
            supports_json_object=True, supports_json_schema=True,
            supports_strict_json_schema=True, supports_image=True,
            supports_audio=True, supports_pdf=True, supports_tools=True,
            supports_reasoning="thinking" in lowered or "2.5" in lowered or "3" in lowered,
        )
    if provider == "openrouter":
        return ModelCapabilities(supports_json_object=False)
    if provider == "groq":
        oss = "gpt-oss" in lowered or lowered.startswith("openai/")
        return ModelCapabilities(
            supports_json_object=True,
            supports_json_schema=oss,
            supports_strict_json_schema=oss,
            supports_reasoning_effort=oss,
        )
    if provider in {"zai", "cerebras"}:
        return ModelCapabilities(supports_json_object=True, supports_reasoning="thinking" in lowered)
    if provider == "mistral":
        return ModelCapabilities(supports_json_object=True, supports_json_schema=True)
    if provider in {"sambanova", "cloudflare", "alibaba", "huggingface", "nvidia"}:
        return ModelCapabilities(supports_json_object=False)
    if provider == "cohere":
        return ModelCapabilities(supports_json_object=True)
    return caps


def infer_free_status(provider: str, model_id: str) -> str:
    """Model-level free status from *explicit* metadata only (spec §3/§5)."""
    for seed in STATIC_SEEDS.get(provider, []):
        if seed.model_id == model_id:
            return seed.free_status
    if provider == "openrouter":
        return FREE_PLAN if infer_openrouter_free(model_id) or model_id == "openrouter/free" else PAID
    info = registry_info(provider)
    # A genuinely free provider plan makes every model it lists free-plan
    # capacity; trial/promotional providers require per-model facts.
    if info.classification is ProviderClass.FREE_PLAN:
        return FREE_PLAN
    if info.classification is ProviderClass.PERMANENT_FREE:
        return FREE_PLAN
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
            thinking = "2.5" in name or "3" in name
            caps = infer_capabilities("gemini", name)
            results.append(
                ModelInfo(
                    provider="gemini",
                    model_id=name,
                    display_name=str(entry.get("displayName") or name),
                    context_window=int(entry.get("inputTokenLimit") or 0),
                    max_output_tokens=int(entry.get("outputTokenLimit") or 0),
                    capabilities=replace(
                        caps,
                        supports_reasoning=caps.supports_reasoning or thinking,
                    ),
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
        """Best-known metadata for one model (DB → static seed → conservative)."""
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
        stats = await self.db.ai_models_upsert_discovery(
            provider,
            [self._info_to_row(info) for info in discovered],
        )
        return stats

    @staticmethod
    def _row_to_info(row: dict) -> ModelInfo:
        return ModelInfo(
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
            source=str(row.get("source") or "static_seed"),
            source_last_verified_at=row.get("source_last_verified_at"),
            quality_score=row.get("quality_score"),
        )

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
            "source": info.source,
            "source_last_verified_at": info.source_last_verified_at,
            "quality_score": info.quality_score,
        }


def monotonic() -> float:
    return time.monotonic()
