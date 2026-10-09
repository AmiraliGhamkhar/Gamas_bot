"""Static provider registry: classification, transport and policy metadata.

The registry answers three questions:

1. **What is this provider?** — slug, display name, protocol, base URL,
   authentication style, documentation anchors.
2. **May Gamas use it in FREE-ONLY mode?** — an explicit classification
   (:class:`ProviderClass`) plus free-tier/commercial-use policy notes. A
   provider is *never* classified free because a marketing page shows "$0";
   the classification is reviewed metadata (see per-entry ``last_reviewed``)
   and administrators can override it per deployment (``ai_provider_settings``).
3. **Which adapter speaks its protocol?** — via :attr:`ProviderInfo.api_style`.

Legitimate-looking "$0" offers that expire, require billing opt-in, forbid
production use, or are region-locked are modelled as first-class states
(``promotional_free``, ``trial_only``, ``region_restricted``) instead of being
silently treated as production-free capacity.

Numbers quoted in ``free_tier_policy`` are *hints last reviewed on the stated
date* — they protect the free tier, they are not a billing contract. Admins
always see the live values discovered at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ProviderClass(str, Enum):
    """Free-tier classification of a provider (spec §3)."""

    PERMANENT_FREE = "permanent_free"
    FREE_PLAN = "free_plan"
    PROMOTIONAL_FREE = "promotional_free"
    TRIAL_ONLY = "trial_only"
    PAID_ONLY = "paid_only"
    UNAVAILABLE = "unavailable"
    REGION_RESTRICTED = "region_restricted"


class AuthStyle(str, Enum):
    """How the adapter authenticates a request."""

    BEARER = "bearer"
    X_GOOG_API_KEY = "x-goog-api-key"
    X_API_KEY = "x-api-key"
    #: OpenAI-compatible endpoint for the Gemini API (x-goog-api-key still applies).
    NONE = "none"


#: API protocol families the adapters implement.
PROTOCOL_OPENAI = "openai_compatible"
PROTOCOL_GEMINI_NATIVE = "gemini_native"
PROTOCOL_GEMINI_OPENAI = "gemini_openai"
PROTOCOL_ANTHROPIC = "anthropic_messages"


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    """Registry record for one provider.

    ``last_reviewed`` is the date the classification/policy text was last
    cross-checked against the provider's official documentation. Treat entries
    older than ~90 days as stale hints: the live values discovered at runtime
    (rate-limit headers, plan probes, model catalog) always win.
    """

    slug: str
    display_name: str
    classification: ProviderClass
    protocol: str
    base_url: str | None
    auth_style: AuthStyle
    docs_url: str
    pricing_url: str
    #: One-paragraph plain-text data-use summary ("privacy" page anchor).
    data_use_policy: str
    #: Whether commercial/production use is allowed by the provider's terms.
    commercial_use_allowed: bool
    #: Free-tier description for the admin panel (numbers = last-reviewed hint).
    free_tier_policy: str
    #: Services this provider can serve inside Gamas.
    supported_services: tuple[str, ...] = ("notes",)
    #: May the provider serve requests when AI_FREE_ONLY is on?
    generation_allowed_in_free_only: bool = False
    #: Can an account silently move from free to paid? (billing-linked plans)
    quota_can_become_paid: bool = False
    #: Does the API expose remaining-quota/rate-limit response headers?
    exposes_quota_headers: bool = False
    #: Official model listing endpoint usable for discovery?
    model_discovery: bool = True
    #: Must an admin explicitly enable this provider before any routing?
    requires_explicit_enable: bool = False
    #: Region/API restriction note (empty = none).
    region_restriction: str = ""
    #: Show as "experimental" in the admin panel and never auto-route.
    experimental_only: bool = False
    #: Provider alias slugs (legacy config names that resolve here).
    aliases: tuple[str, ...] = ()
    last_reviewed: str = "2026-10"  # month precision: reviewed metadata date


# ---------------------------------------------------------------------------
# Registry data
# ---------------------------------------------------------------------------

_GEMINI = ProviderInfo(
    slug="gemini",
    display_name="Google Gemini (AI Studio)",
    classification=ProviderClass.FREE_PLAN,
    protocol=PROTOCOL_GEMINI_NATIVE,
    base_url="https://generativelanguage.googleapis.com/v1beta",
    auth_style=AuthStyle.X_GOOG_API_KEY,
    docs_url="https://ai.google.dev/gemini-api/docs",
    pricing_url="https://ai.google.dev/gemini-api/docs/pricing",
    data_use_policy=(
        "Free-of-charge API use may be used by Google to improve products "
        "(unpaid services); paid-tier traffic is not. See the Gemini API terms."
    ),
    commercial_use_allowed=True,
    free_tier_policy=(
        "Free tier per model in AI Studio projects without billing. RPM/TPM/RPD "
        "limits are model-dependent and read live from responses."
    ),
    generation_allowed_in_free_only=True,
    quota_can_become_paid=False,
    exposes_quota_headers=True,
    aliases=(),
)

_NARA = ProviderInfo(
    slug="nara",
    display_name="NaraRouter",
    classification=ProviderClass.FREE_PLAN,
    protocol=PROTOCOL_OPENAI,
    base_url="https://router.bynara.id/v1",
    auth_style=AuthStyle.BEARER,
    docs_url="https://router.bynara.id/docs",
    pricing_url="https://router.bynara.id/pricing",
    data_use_policy="Third-party gateway; prompts transit the router. Review its terms.",
    commercial_use_allowed=True,
    free_tier_policy=(
        "Free plan models are listed by GET /v1/models on the free key (the "
        "endpoint returns exactly the aliases the account's plan entitles; the "
        "public /api/plans endpoint lists tiers). Quota is account-plan "
        "dependent and discovered, never assumed."
    ),
    generation_allowed_in_free_only=True,
    quota_can_become_paid=True,
    exposes_quota_headers=False,
    model_discovery=True,
)

_GROQ = ProviderInfo(
    slug="groq",
    display_name="Groq",
    classification=ProviderClass.FREE_PLAN,
    protocol=PROTOCOL_OPENAI,
    base_url="https://api.groq.com/openai/v1",
    auth_style=AuthStyle.BEARER,
    docs_url="https://console.groq.com/docs",
    pricing_url="https://console.groq.com/docs/rate-limits",
    data_use_policy="See Groq privacy policy; API traffic policy per GroqCloud terms.",
    commercial_use_allowed=True,
    free_tier_policy=(
        "Free tier (last verified 2026-10-09): per-model limits, e.g. "
        "openai/gpt-oss-120b, openai/gpt-oss-20b and qwen/qwen3.8-27b at "
        "30 RPM / 1K RPD / 8K TPM / 200K TPD. Live values are read from the "
        "x-ratelimit-* response headers on every call."
    ),
    generation_allowed_in_free_only=True,
    quota_can_become_paid=False,
    exposes_quota_headers=True,
)

_OPENROUTER = ProviderInfo(
    slug="openrouter",
    display_name="OpenRouter",
    classification=ProviderClass.FREE_PLAN,
    protocol=PROTOCOL_OPENAI,
    base_url="https://openrouter.ai/api/v1",
    auth_style=AuthStyle.BEARER,
    docs_url="https://openrouter.ai/docs",
    pricing_url="https://openrouter.ai/docs",
    data_use_policy=(
        "Gateway over third-party backends; prompt data handling depends on the "
        "upstream provider selected per model. Review model pages."
    ),
    commercial_use_allowed=True,
    free_tier_policy=(
        "Models with the :free suffix plus the openrouter/free router. Free "
        "accounts (last verified 2026-10-09): 20 RPM and 50 requests/day; "
        "accounts with >= $10 purchased credits get 1000 requests/day. "
        "GET /api/v1/key exposes free_model_daily_requests {used, limit, "
        "remaining}; a local ledger is kept because failed calls may still "
        "consume quota."
    ),
    generation_allowed_in_free_only=True,
    quota_can_become_paid=False,
    exposes_quota_headers=True,
)

_MISTRAL = ProviderInfo(
    slug="mistral",
    display_name="Mistral (La Plateforme)",
    classification=ProviderClass.FREE_PLAN,
    protocol=PROTOCOL_OPENAI,
    base_url="https://api.mistral.ai/v1",
    auth_style=AuthStyle.BEARER,
    docs_url="https://docs.mistral.ai",
    pricing_url="https://docs.mistral.ai/admin/billing-usage/usage-limits",
    data_use_policy="See Mistral terms of service for API data retention.",
    commercial_use_allowed=True,
    free_tier_policy=(
        "Free mode (last verified 2026-10-09): API keys work with included "
        "monthly usage inside the per-model limits on the Admin Panel Limits "
        "page; pay-as-you-go only extends usage beyond it. The exact limit is "
        "not hard-coded and is observed at runtime."
    ),
    generation_allowed_in_free_only=True,
    quota_can_become_paid=False,
    exposes_quota_headers=True,
)

_SAMBANOVA = ProviderInfo(
    slug="sambanova",
    display_name="SambaNova (SambaCloud)",
    classification=ProviderClass.FREE_PLAN,
    protocol=PROTOCOL_OPENAI,
    base_url="https://api.sambanova.ai/v1",
    auth_style=AuthStyle.BEARER,
    docs_url="https://docs.sambanova.ai",
    pricing_url="https://docs.sambanova.ai/docs/en/models/rate-limits",
    data_use_policy="See SambaNova cloud terms.",
    commercial_use_allowed=True,
    free_tier_policy=(
        "Free tier = no payment method on the account (last verified "
        "2026-10-09): e.g. Meta-Llama-3.3-70B-Instruct at 20 RPM / 20 RPD / "
        "200K TPD; the Developer tier (payment method linked) raises these. "
        "Limits are per model — never assumed equal. Multiple API keys are "
        "supported by the provider and by Gamas rotation."
    ),
    generation_allowed_in_free_only=True,
    quota_can_become_paid=False,
    exposes_quota_headers=True,
)

_ZAI = ProviderInfo(
    slug="zai",
    display_name="Z.AI (GLM)",
    classification=ProviderClass.PROMOTIONAL_FREE,
    protocol=PROTOCOL_OPENAI,
    base_url="https://api.z.ai/api/paas/v4",
    auth_style=AuthStyle.BEARER,
    docs_url="https://docs.z.ai",
    pricing_url="https://docs.z.ai/guides/overview/pricing",
    data_use_policy="See Z.AI terms.",
    commercial_use_allowed=True,
    free_tier_policy=(
        "Mixed catalog (last verified 2026-10-09): GLM-4.5-Flash and "
        "GLM-4.7-Flash are listed as permanently Free; most other models are "
        "paid. Gamas records free_type (permanent|promotional|paid) and "
        "free_until per model and stops using promotional models after expiry."
    ),
    generation_allowed_in_free_only=True,
    quota_can_become_paid=False,
    exposes_quota_headers=False,
)

_NVIDIA = ProviderInfo(
    slug="nvidia",
    display_name="NVIDIA NIM / build.nvidia.com",
    classification=ProviderClass.PROMOTIONAL_FREE,
    protocol=PROTOCOL_OPENAI,
    base_url="https://integrate.api.nvidia.com/v1",
    auth_style=AuthStyle.BEARER,
    docs_url="https://build.nvidia.com/docs",
    pricing_url="https://build.nvidia.com",
    data_use_policy="See NVIDIA build terms; demo endpoints may log traffic.",
    commercial_use_allowed=True,
    free_tier_policy=(
        'Endpoints listed in the live catalog at https://integrate.api.nvidia.com/v1/models '
        "(last verified 2026-10-09); availability and deprecation are "
        "model-level live facts, synced on demand — never assumed permanent."
    ),
    generation_allowed_in_free_only=True,
    quota_can_become_paid=False,
    exposes_quota_headers=False,
)

_CLOUDFLARE = ProviderInfo(
    slug="cloudflare",
    display_name="Cloudflare Workers AI",
    classification=ProviderClass.PERMANENT_FREE,
    protocol=PROTOCOL_OPENAI,
    base_url=None,  # account-scoped: built from the account ID
    auth_style=AuthStyle.BEARER,
    docs_url="https://developers.cloudflare.com/workers-ai",
    pricing_url="https://developers.cloudflare.com/workers-ai/platform/pricing",
    data_use_policy="See Cloudflare Workers AI terms; REST API via account token.",
    commercial_use_allowed=True,
    free_tier_policy=(
        "10,000 Neurons/day free allocation per account (last verified "
        "2026-10-09); the allocation resets daily at 00:00 UTC and usage above "
        "it bills at $0.011 per 1,000 Neurons. Some models require paid "
        "billing — each model carries requires_paid_billing and FREE_ONLY "
        "rejects those."
    ),
    generation_allowed_in_free_only=True,
    quota_can_become_paid=False,
    exposes_quota_headers=False,
)

_HUGGINGFACE = ProviderInfo(
    slug="huggingface",
    display_name="Hugging Face Inference Providers",
    # Current official pricing (verified 2026-10-09): free HF users receive NO
    # monthly inference credit — usage requires purchased credits or a paid
    # PRO/Team/Enterprise subscription ($2/seat monthly credits). There is no
    # free tier, so this is paid-only capacity that stays experimental.
    classification=ProviderClass.PAID_ONLY,
    protocol=PROTOCOL_OPENAI,
    base_url="https://router.huggingface.co/v1",
    auth_style=AuthStyle.BEARER,
    docs_url="https://huggingface.co/docs/inference-providers",
    pricing_url="https://huggingface.co/docs/inference-providers/pricing",
    data_use_policy="Traffic is served by the selected third-party provider; see HF terms.",
    commercial_use_allowed=True,
    free_tier_policy=(
        "No free tier for free accounts (last verified 2026-10-09): Inference "
        "Providers usage is pay-as-you-go on purchased credits; PRO/Team/Ent "
        "subscriptions include $2/seat monthly credits. Kept experimental for "
        "testing/benchmarks/emergency fallback only — never a primary "
        "provider, and FREE_ONLY never routes here."
    ),
    generation_allowed_in_free_only=False,  # explicit admin opt-in
    quota_can_become_paid=True,
    exposes_quota_headers=False,
    experimental_only=True,
)

_ALIBABA = ProviderInfo(
    slug="alibaba",
    display_name="Alibaba Cloud Model Studio",
    classification=ProviderClass.REGION_RESTRICTED,
    protocol=PROTOCOL_OPENAI,
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    auth_style=AuthStyle.BEARER,
    docs_url="https://help.aliyun.com/en/model-studio",
    pricing_url="https://help.aliyun.com/en/model-studio/model-pricing",
    data_use_policy="See Alibaba Cloud Model Studio terms (regional).",
    commercial_use_allowed=True,
    free_tier_policy=(
        "Free quota is region/model/account dependent with an expiry "
        "(help.aliyun.com/en/model-studio/new-free-quota). Disabled for global "
        "free routing by default; admins opt in per deployment after explicit "
        "confirmation."
    ),
    generation_allowed_in_free_only=False,
    quota_can_become_paid=False,
    exposes_quota_headers=False,
    requires_explicit_enable=True,
    region_restriction="Region/account-dependent free quota; endpoint per region.",
)

_COHERE = ProviderInfo(
    slug="cohere",
    display_name="Cohere (Trial API)",
    classification=ProviderClass.TRIAL_ONLY,
    protocol=PROTOCOL_OPENAI,
    base_url="https://api.cohere.com/compatibility/v1",
    auth_style=AuthStyle.BEARER,
    docs_url="https://docs.cohere.com",
    pricing_url="https://docs.cohere.com/docs/rate-limits",
    data_use_policy="Trial keys: see Cohere trial API terms.",
    commercial_use_allowed=False,
    free_tier_policy=(
        "Trial keys are evaluation keys (last verified 2026-10-09): free but "
        "limited to 1,000 API calls/month and 20 req/min per model. NOT "
        "production or commercial credentials. Kept for evaluation/benchmarks "
        "only."
    ),
    generation_allowed_in_free_only=False,
    quota_can_become_paid=False,
    exposes_quota_headers=False,
    experimental_only=True,
)

_CEREBRAS = ProviderInfo(
    slug="cerebras",
    display_name="Cerebras",
    classification=ProviderClass.TRIAL_ONLY,
    protocol=PROTOCOL_OPENAI,
    base_url="https://api.cerebras.ai/v1",
    auth_style=AuthStyle.BEARER,
    docs_url="https://inference-docs.cerebras.ai",
    pricing_url="https://inference-docs.cerebras.ai/support/rate-limits",
    data_use_policy="See Cerebras terms.",
    commercial_use_allowed=True,
    free_tier_policy=(
        "Free Trial (last verified 2026-10-09): $5 in credits that expire 30 "
        "days after grant, require a verified payment method, and cover the "
        "shared catalog (e.g. gpt-oss-120b, qwen-3.8-27b at 5 RPM / 30K "
        "uncached TPM). There is NO permanently free tier — modelled as "
        "trial_only, used for benchmarks/evaluation only."
    ),
    generation_allowed_in_free_only=False,
    quota_can_become_paid=False,
    exposes_quota_headers=True,
)

_ANTHROPIC = ProviderInfo(
    slug="anthropic",
    display_name="Anthropic",
    classification=ProviderClass.PAID_ONLY,
    protocol=PROTOCOL_ANTHROPIC,
    base_url="https://api.anthropic.com/v1",
    auth_style=AuthStyle.X_API_KEY,
    docs_url="https://platform.claude.com/docs",
    pricing_url="https://platform.claude.com/docs/en/about-claude/pricing",
    data_use_policy="See Anthropic commercial terms.",
    commercial_use_allowed=True,
    free_tier_policy="No free tier; kept for backward compatibility with NOTE_API_PROVIDER=anthropic.",
    generation_allowed_in_free_only=False,
    quota_can_become_paid=True,
    exposes_quota_headers=True,
    aliases=(),
)

_OPENAI_GENERIC = ProviderInfo(
    slug="openai_compatible",
    display_name="OpenAI-compatible (generic)",
    classification=ProviderClass.PAID_ONLY,
    protocol=PROTOCOL_OPENAI,
    base_url="https://api.openai.com/v1",
    auth_style=AuthStyle.BEARER,
    docs_url="https://platform.openai.com/docs",
    pricing_url="https://openai.com/pricing",  # default gateway; adapters may override
    data_use_policy="Depends on the configured gateway.",
    commercial_use_allowed=True,
    free_tier_policy=(
        "Generic OpenAI-compatible gateway. FREE_ONLY treats it as paid unless the "
        "base URL resolves to a known free provider (see resolve_canonical)."
    ),
    generation_allowed_in_free_only=False,
    quota_can_become_paid=True,
    exposes_quota_headers=False,
)

PROVIDER_REGISTRY: dict[str, ProviderInfo] = {
    info.slug: info
    for info in (
        _GEMINI,
        _NARA,
        _GROQ,
        _OPENROUTER,
        _MISTRAL,
        _SAMBANOVA,
        _ZAI,
        _NVIDIA,
        _CLOUDFLARE,
        _HUGGINGFACE,
        _ALIBABA,
        _COHERE,
        _CEREBRAS,
        _ANTHROPIC,
        _OPENAI_GENERIC,
    )
}

#: Slugs offered for note generation (credential pools + routing).
#: ``openai_compatible`` stays for backward compatibility and generic gateways.
PROVIDER_CHOICES_NOTES = frozenset(PROVIDER_REGISTRY)

#: Percent-encoded/host substrings → canonical slug (URL-based classification).
_HOST_HINTS: tuple[tuple[str, str], ...] = (
    ("router.bynara.id", "nara"),
    ("api.groq.com", "groq"),
    ("openrouter.ai", "openrouter"),
    ("api.mistral.ai", "mistral"),
    ("api.sambanova.ai", "sambanova"),
    ("api.z.ai", "zai"),
    ("integrate.api.nvidia.com", "nvidia"),
    ("api.cloudflare.com", "cloudflare"),
    ("router.huggingface.co", "huggingface"),
    ("huggingface.co", "huggingface"),
    ("dashscope", "alibaba"),
    ("aliyuncs.com", "alibaba"),
    ("api.cohere.", "cohere"),
    ("api.cerebras.ai", "cerebras"),
    ("generativelanguage.googleapis.com", "gemini"),
    ("api.anthropic.com", "anthropic"),
)

#: Which legacy NOTE_API_PROVIDER slugs keep their credential pool naming.
LEGACY_NOTE_SLUGS = frozenset({"gemini", "openai_compatible", "anthropic"})


def registry_info(slug: str) -> ProviderInfo:
    """Registry record for ``slug`` (generic fallback when unknown)."""
    return PROVIDER_REGISTRY.get(slug, _OPENAI_GENERIC)


def resolve_canonical(provider: str, base_url: str | None = None) -> str:
    """Canonical provider slug for a configured provider + base URL.

    A legacy ``openai_compatible`` deployment keeps *working* while gaining the
    right adapter/classification: ``router.bynara.id`` resolves to ``nara``,
    ``api.groq.com`` to ``groq``, and so on. Unknown hosts stay generic.
    """
    slug = (provider or "").strip().lower()
    host = (base_url or "").strip().lower()
    for hint, canonical in _HOST_HINTS:
        if hint in host:
            return canonical
    if slug in PROVIDER_REGISTRY:
        return slug
    if slug in LEGACY_NOTE_SLUGS:
        return slug
    return "openai_compatible"


def classification_of(slug: str) -> ProviderClass:
    return registry_info(slug).classification


def free_only_generation_allowed(slug: str, *, admin_enabled: bool = True) -> bool:
    """Provider-level FREE_ONLY gate (model-level checks happen later).

    ``region_restricted`` providers can be opted in by an admin after explicit
    confirmation; ``experimental_only`` providers never auto-route.
    """
    info = registry_info(slug)
    if info.experimental_only and not admin_enabled:
        return False
    if info.requires_explicit_enable and not admin_enabled:
        return False
    return info.generation_allowed_in_free_only


def free_class_fa(slug: str) -> str:
    """Human-readable Persian label for the admin panel."""
    return _CLASS_FA[registry_info(slug).classification]


_CLASS_FA = {
    ProviderClass.PERMANENT_FREE: "رایگان دائمی",
    ProviderClass.FREE_PLAN: "پلن رایگان",
    ProviderClass.PROMOTIONAL_FREE: "رایگان موقت/پروموشن",
    ProviderClass.TRIAL_ONLY: "فقط دورهٔ آزمایشی",
    ProviderClass.PAID_ONLY: "فقط پولی",
    ProviderClass.UNAVAILABLE: "در دسترس نیست",
    ProviderClass.REGION_RESTRICTED: "محدود به منطقه",
}

#: Admin-panel status vocabulary (spec §51).
ROUTE_STATUS_FA = {
    "active": "فعال",
    "degraded": "کاهش کیفیت",
    "rate_limited": "سقف درخواست",
    "quota_exhausted": "اتمام سهمیه",
    "billing_required": "نیازمند صورتحساب",
    "auth_failed": "خطای احراز هویت",
    "model_unavailable": "مدل در دسترس نیست",
    "deprecated": "منسوخ",
    "disabled": "غیرفعال",
    "experimental": "آزمایشی",
}
