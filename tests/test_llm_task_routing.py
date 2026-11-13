from types import SimpleNamespace

from gamas_bot.llm import (
    _ordered_routes,
    _sensitivity_eligible_routes,
    _task_ordered_routes,
)
from gamas_bot.config import APPROVED_LLM_PROVIDERS, NOTE_PROVIDER_SLUGS
from gamas_bot.llm_profiles import get_provider_profile


def routes(*slugs):
    return tuple(SimpleNamespace(slug=slug) for slug in slugs)


def test_active_llm_allowlist_is_exact():
    assert APPROVED_LLM_PROVIDERS == {"gemini", "avalai", "openrouter"}
    assert set(NOTE_PROVIDER_SLUGS) == APPROVED_LLM_PROVIDERS


def test_task_routes_ignore_global_order():
    available = routes("openrouter", "gemini", "avalai")
    assert [r.slug for r in _task_ordered_routes(available, "BOOKLET_GENERATION")] == [
        "avalai", "gemini", "openrouter"
    ]
    assert [r.slug for r in _task_ordered_routes(available, "QUIZ_GENERATION")] == [
        "gemini", "avalai", "openrouter"
    ]
    assert [r.slug for r in _task_ordered_routes(available, "QUIZ_20")] == [
        "avalai", "gemini", "openrouter"
    ]


def test_unknown_data_never_routes_to_openrouter_and_sensitive_needs_approval():
    available = routes("avalai", "gemini", "openrouter")
    settings = SimpleNamespace(
        avalai_allow_sensitive_data=False,
        gemini_free_allow_sensitive_data=False,
        openrouter_free_allow_sensitive_data=False,
    )
    assert [r.slug for r in _sensitivity_eligible_routes(available, settings, "UNKNOWN")] == [
        "avalai", "gemini"
    ]
    assert _sensitivity_eligible_routes(available, settings, "PHI_OR_PII") == ()


def test_reference_model_profiles_keep_context_and_output_distinct():
    assert get_provider_profile("gemini").context_window_tokens == 1_048_576
    assert get_provider_profile("gemini").max_output_tokens_cap == 65_536
    assert get_provider_profile("avalai").context_window_tokens == 1_000_000
    assert get_provider_profile("avalai").max_output_tokens_cap == 393_216
    assert get_provider_profile("openrouter").context_window_tokens == 1_000_000
    assert get_provider_profile("openrouter").max_output_tokens_cap == 131_072


def test_unapproved_capability_profiles_cannot_be_registered_or_resolved():
    import pytest
    from gamas_bot.llm_profiles import ProviderCapabilityProfile, register_provider_profile

    with pytest.raises(ValueError, match="Unapproved"):
        register_provider_profile(ProviderCapabilityProfile(slug="deepseek"))
    with pytest.raises(ValueError, match="Unapproved"):
        get_provider_profile("nara")


def test_openrouter_preference_cannot_elevate_emergency_fallback():
    available = routes("avalai", "gemini", "openrouter")
    assert [r.slug for r in _ordered_routes(available, "openrouter")] == [
        "avalai", "gemini", "openrouter"
    ]
