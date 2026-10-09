"""Per-provider request profiles: budgets, retries, JSON strategy, pass policy.

These are *starting policies*, adjustable per deployment through the routing
configuration. The numbers for free providers are deliberately conservative —
they reflect the free-tier reality (small TPM budgets, failed calls still
costing quota) rather than the paid ceilings.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..structuring import TRANSCRIPT_CHUNK_CHARS


@dataclass(frozen=True, slots=True)
class RequestPolicy:
    """How one request type is executed against a provider."""

    timeout_seconds: int = 240
    max_retries: int = 2              # within one credential
    retry_on_rate_limit: bool = True  # respect Retry-After inside one attempt budget
    backoff_base_seconds: float = 2.0
    backoff_cap_seconds: float = 15.0
    retry_after_cap_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class NoteProviderProfile:
    """Provider-wide note pipeline policy.

    ``chunk_token_budget`` is the *token* budget of the model-facing document
    per chunk call (roughly: what a TPM-limited free tier can absorb), while
    ``chunk_char_cap`` is the lossless-splitting hard ceiling kept as a safety
    fallback. The effective chunk size is computed from both.
    """

    provider: str
    protocol: str = "openai_compatible"
    chunk_token_budget: int = 6000
    chunk_char_cap: int = TRANSCRIPT_CHUNK_CHARS
    max_output_tokens: int = 8192
    compile_token_budget: int = 16000
    temperature_policy: str = "fixed"    # fixed | omit | model_default
    reasoning_policy: str = "model_default"  # none | model_default | low | medium | high
    json_strategy: str = "auto"          # auto -> capability resolution
    global_context_enabled: bool = True
    outline_enabled: bool = True
    repair_enabled: bool = True
    final_compile_enabled: bool = True
    free_only: bool = True
    max_concurrency: int = 1
    #: Static request-economy hints for free tiers (0 = unknown/unbounded).
    daily_request_limit: int = 0
    daily_token_limit: int = 0
    requests_per_minute: int = 0
    tokens_per_minute: int = 0
    policy: RequestPolicy = RequestPolicy()

    def with_policy(self, policy: RequestPolicy) -> "NoteProviderProfile":
        from dataclasses import replace

        return replace(self, policy=policy)


DEFAULT_PROFILES: dict[str, NoteProviderProfile] = {
    # Large context, native schema, full pipeline.
    "gemini": NoteProviderProfile(
        provider="gemini",
        protocol="gemini_native",
        chunk_token_budget=12000,
        chunk_char_cap=TRANSCRIPT_CHUNK_CHARS,
        max_output_tokens=8192,
        compile_token_budget=20000,
    ),
    # Large context, prompt JSON until model capabilities are verified.
    "nara": NoteProviderProfile(
        provider="nara",
        chunk_token_budget=6000,
        chunk_char_cap=TRANSCRIPT_CHUNK_CHARS,
        max_output_tokens=8192,
        requests_per_minute=15,
        daily_token_limit=7_000_000,
    ),
    # The Developer-plan limit table is not proof of free eligibility. Keep
    # conservative per-model budgets and no extra passes by default.
    "groq": NoteProviderProfile(
        provider="groq",
        chunk_token_budget=2400,
        chunk_char_cap=8000,
        max_output_tokens=4096,
        outline_enabled=False,
        repair_enabled=False,
        final_compile_enabled=False,
        requests_per_minute=30,
        daily_request_limit=1000,
        tokens_per_minute=8000,
        policy=RequestPolicy(max_retries=1, retry_on_rate_limit=True),
    ),
    # Free ledger: failed calls may still consume quota, so retries stay tiny.
    "openrouter": NoteProviderProfile(
        provider="openrouter",
        chunk_token_budget=2400,
        chunk_char_cap=8000,
        max_output_tokens=4096,
        outline_enabled=False,
        repair_enabled=False,
        final_compile_enabled=False,
        daily_request_limit=50,
        requests_per_minute=20,
        policy=RequestPolicy(max_retries=1),
    ),
    "mistral": NoteProviderProfile(
        provider="mistral",
        chunk_token_budget=5000,
        max_output_tokens=8192,
    ),
    # Free tier (no payment method, verified 2026-10-09): 20 RPM / 20 RPD /
    # 200K TPD for Meta-Llama-3.3-70B-Instruct — model-specific, so the hints
    # stay conservative; live x-ratelimit-* headers refine them at runtime.
    "sambanova": NoteProviderProfile(
        provider="sambanova",
        chunk_token_budget=4000,
        max_output_tokens=8192,
        requests_per_minute=20,
        daily_request_limit=20,
        daily_token_limit=200_000,
        policy=RequestPolicy(max_retries=1),
    ),
    "zai": NoteProviderProfile(
        provider="zai",
        chunk_token_budget=6000,
        max_output_tokens=8192,
    ),
    "nvidia": NoteProviderProfile(
        provider="nvidia",
        chunk_token_budget=4000,
        max_output_tokens=8000,
    ),
    "cloudflare": NoteProviderProfile(
        provider="cloudflare",
        chunk_token_budget=1800,
        chunk_char_cap=6000,
        max_output_tokens=4096,
        outline_enabled=False,
        repair_enabled=False,
        final_compile_enabled=False,
    ),
    "huggingface": NoteProviderProfile(
        provider="huggingface",
        chunk_token_budget=1800,
        chunk_char_cap=6000,
        max_output_tokens=4096,
        outline_enabled=False,
        repair_enabled=False,
        final_compile_enabled=False,
        policy=RequestPolicy(max_retries=1),
    ),
    "alibaba": NoteProviderProfile(
        provider="alibaba",
        chunk_token_budget=5000,
        max_output_tokens=8192,
    ),
    "cohere": NoteProviderProfile(
        provider="cohere",
        chunk_token_budget=4000,
        max_output_tokens=4096,
        outline_enabled=False,
        repair_enabled=False,
        final_compile_enabled=False,
        policy=RequestPolicy(max_retries=1),
    ),
    "cerebras": NoteProviderProfile(
        provider="cerebras",
        chunk_token_budget=3000,
        max_output_tokens=8192,
        outline_enabled=False,
        repair_enabled=False,
        final_compile_enabled=False,
        policy=RequestPolicy(max_retries=1),
    ),
    "anthropic": NoteProviderProfile(
        provider="anthropic",
        protocol="anthropic_messages",
        chunk_token_budget=8000,
        max_output_tokens=8192,
    ),
    "openai_compatible": NoteProviderProfile(
        provider="openai_compatible",
        chunk_token_budget=6000,
        max_output_tokens=8192,
    ),
}


def profile_for(canonical: str) -> NoteProviderProfile:
    return DEFAULT_PROFILES.get(canonical, DEFAULT_PROFILES["openai_compatible"])
