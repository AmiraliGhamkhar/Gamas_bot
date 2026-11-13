import os
from unittest import mock

import pytest

from gamas_bot.config import from_env
from tests.test_config import env


@pytest.mark.parametrize("legacy", ["nara", "deepseek", "groq", "cerebras", "openai_compatible"])
def test_legacy_provider_is_rejected_at_startup(legacy):
    with mock.patch.dict(os.environ, env(NOTE_PROVIDER=legacy), clear=False):
        with pytest.raises(ValueError, match="Unapproved NOTE_PROVIDER"):
            from_env(env_file="/nonexistent")


def test_approved_default_models_and_operational_budgets():
    with mock.patch.dict(os.environ, env(), clear=True):
        settings = from_env(env_file="/nonexistent")
    assert settings.gemini_model == "gemini-3.5-flash-lite"
    assert settings.avalai_model == "deepseek-v4.1-flash"
    assert settings.openrouter_model == "nvidia/nemotron-3.5-super-vl:free"
    assert settings.note_providers == ("avalai", "gemini", "openrouter")
    assert (settings.avalai_rpm_limit, settings.avalai_tpm_limit) == (35, 750000)
    assert (settings.openrouter_rpm_limit, settings.openrouter_rpd_limit) == (10, 15)
    assert settings.llm_primary_max_in_flight == 3
    assert settings.llm_fallback_max_in_flight == 1
    assert settings.note_hedge_after == 0


@pytest.mark.parametrize("legacy_key", ["NARA_API_KEY", "DEEPSEEK_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY"])
def test_unapproved_legacy_credentials_are_rejected(legacy_key):
    values = env()
    values[legacy_key] = "obsolete-key"
    with mock.patch.dict(os.environ, values, clear=True):
        with pytest.raises(ValueError, match="Unapproved LLM credential"):
            from_env(env_file="/nonexistent")


def test_unapproved_provider_in_chain_is_rejected_at_startup():
    with mock.patch.dict(os.environ, env(NOTE_PROVIDERS="gemini,nara,openrouter"), clear=False):
        with pytest.raises(ValueError, match="Unapproved LLM provider"):
            from_env(env_file="/nonexistent")
