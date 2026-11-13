"""Only approved text providers may be configured, including on stale redeploys."""

from dataclasses import fields
import os
from unittest import mock

import pytest

from gamas_bot import config
from gamas_bot.config import from_env, note_routes, note_slot_credentials
from tests.test_config import note_env


@pytest.mark.parametrize("legacy", ["nara", "deepseek", "groq", "cerebras", "anthropic", "openai_compatible"])
def test_legacy_single_slot_is_ignored_without_blocking_startup(legacy, caplog):
    values = note_env(NOTE_PROVIDER=legacy, NOTE_API_KEY="obsolete-slot-secret")
    with mock.patch.dict(os.environ, values, clear=True):
        settings = from_env(env_file="/nonexistent")
    assert note_routes(settings) == ()
    assert not settings.note_configured
    assert settings.note_api_key == settings.note_api_base_url == ""
    assert "Unsupported NOTE_PROVIDER/NOTE_BASE_URL/NOTE_API_KEY" in caplog.text
    assert "obsolete-slot-secret" not in caplog.text


def test_approved_default_models_and_operational_budgets():
    with mock.patch.dict(os.environ, note_env(), clear=True):
        settings = from_env(env_file="/nonexistent")
    assert settings.gemini_model == "gemini-3.5-flash-lite"
    assert settings.avalai_model == "deepseek-v4.1-flash"
    assert settings.openrouter_model == "nvidia/nemotron-3.5-super-vl:free"
    assert settings.note_providers == ("avalai", "gemini", "openrouter")
    assert (settings.avalai_rpm_limit, settings.avalai_tpm_limit, settings.avalai_max_in_flight) == (35, 750000, 3)
    assert (settings.openrouter_rpm_limit, settings.openrouter_rpd_limit, settings.openrouter_max_in_flight) == (10, 15, 1)
    assert (settings.gemini_rpm_limit, settings.gemini_tpm_limit, settings.gemini_rpd_limit) == (0, 0, 0)
    assert settings.gemini_max_in_flight == 1
    assert settings.llm_primary_max_in_flight == 3
    assert settings.llm_fallback_max_in_flight == 1
    assert settings.note_hedge_after == 0
    assert (settings.note_context_tokens, settings.note_chunk_chars, settings.note_max_output_tokens) == (131072, 64000, 16384)
    assert not settings.note_compile


@pytest.mark.parametrize("source", ["environment", "dotenv"])
@pytest.mark.parametrize("prefix", ["NARA", "DEEPSEEK", "GROQ", "CEREBRAS", "ANTHROPIC", "OPENAI"])
def test_removed_credentials_are_inert_from_both_sources(prefix, source, tmp_path, caplog):
    # Every value is deliberately distinctive; warnings must contain names only.
    stale = {
        f"{prefix}_API_KEY": "obsolete-secret-do-not-log",
        f"{prefix}_BASE_URL": "https://obsolete.example.test/private-endpoint",
        f"{prefix}_MODEL": "obsolete-model-do-not-log",
    }
    file_path = tmp_path / "stale.env"
    values = note_env()
    if source == "environment":
        values.update(stale)
    else:
        file_path.write_text("\n".join(f"{k}={v}" for k, v in stale.items()), encoding="utf-8")
    with mock.patch.dict(os.environ, values, clear=True):
        settings = from_env(env_file=str(file_path))
    assert note_routes(settings) == ()
    assert not settings.note_configured
    assert f"{prefix}_API_KEY" in caplog.text
    assert all(value not in caplog.text for value in stale.values())
    assert note_slot_credentials(settings, prefix.lower()) == ("", "", "")


def test_mixed_stale_dotenv_and_current_environment_preserve_approved_credentials(tmp_path, caplog):
    file_path = tmp_path / "stale.env"
    file_path.write_text(
        "NARA_API_KEY=obsolete-secret\nDEEPSEEK_API_KEY=another-obsolete-secret\n"
        "AVALAI_API_KEY=file-secret\nNOTE_PROVIDERS=nara,openrouter,gemini,avalai,deepseek\n",
        encoding="utf-8",
    )
    values = note_env(AVALAI_API_KEY="env-secret", GEMINI_API_KEY="g-secret", OPENROUTER_API_KEY="or-secret")
    with mock.patch.dict(os.environ, values, clear=True):
        settings = from_env(env_file=str(file_path))
    routes = note_routes(settings)
    assert [(r.slug, r.api_key) for r in routes] == [
        ("gemini", "g-secret"), ("avalai", "env-secret"), ("openrouter", "or-secret")
    ]
    assert "Legacy/unknown LLM provider in NOTE_PROVIDERS ignored" in caplog.text
    assert all(value not in caplog.text for value in (
        "obsolete-secret", "another-obsolete-secret", "file-secret", "env-secret", "g-secret", "or-secret"
    ))


def test_removed_slugs_have_no_active_settings_or_registrations():
    approved = {"gemini", "avalai", "openrouter"}
    assert config.APPROVED_LLM_PROVIDERS == approved
    for registry in (
        config.NOTE_PROVIDER_SLUGS, config.NOTE_REQUEST_STYLE, config.NOTE_PROVIDER_LABELS,
        config.NOTE_BASE_URL_DEFAULTS, config.DEFAULT_NOTE_MODELS,
    ):
        assert set(registry) == approved
    assert config.NOTE_JSON_OBJECT_PROVIDERS == {"avalai", "openrouter"}
    names = {field.name for field in fields(config.Settings)}
    for prefix in ("nara", "deepseek", "groq", "cerebras", "anthropic"):
        assert not any(name.startswith(prefix + "_") for name in names)
    # The shared OpenAI-compatible transport remains necessary for approved LLM
    # gateways and for STT. It is not a fourth text-generation provider.
    assert "stt_openai_api_key" in names
