"""Modular provider & model capability profiles and JSON-schema converters.

Separating provider-specific and model-specific API contracts from
:mod:`gamas_bot.llm` keeps the HTTP transport and failover loop generic while
ensuring every outgoing request strictly honors the documented contract of the
active models:

1. **Google Gemini** (``gemini-3.5-flash-lite``):
   - Native ``generateContent`` / ``streamGenerateContent?alt=sse`` with
     ``x-goog-api-key`` header.
   - 1,048,576 input / 65,536 max output tokens.
   - ``generationConfig.responseFormat.text`` with ``mimeType: "APPLICATION_JSON"``
     and optional lowercase JSON Schema ``schema``.
   - ``generationConfig.thinkingConfig.thinkingLevel`` (``MINIMAL`` default,
     ``LOW``, ``MEDIUM``, ``HIGH``).
   - Sampling parameters (``temperature``, ``topP``, ``topK``) and
     ``candidateCount`` are deprecated for Gemini 3.5 Flash-Lite and omitted.
   - ``systemInstruction`` is supported; prefilled trailing ``model`` turns are
     never sent.

2. **AvalAI** (``deepseek-v4.1-flash``, base ``DeepSeek-V4.1-Flash-1017``):
   - OpenAI-compatible ``POST /v1/chat/completions`` with Bearer auth.
   - 1,000,000 input / 393,216 (384K) max output tokens.
   - Supports ``system`` role, portable non-strict ``response_format``
     (``json_schema`` with fallback to ``json_object``), implicit prompt
     caching, and thinking / non-thinking responses (``reasoning_content``).
   - Does not send an unverified ``reasoning_effort`` enum or ``stream_options``.

3. **OpenRouter** (``nvidia/nemotron-3.5-super-vl:free``):
   - OpenAI-compatible ``POST /api/v1/chat/completions`` with Bearer auth.
   - 1,000,000 input / 131,072 (128K) max output tokens.
   - Supports ``system`` role, ``stream_options.include_usage``,
     ``provider.sort="throughput"``, and OpenRouter's unified ``reasoning``
     object (``{"enabled": False}`` when disabled; ``{"effort": ..., "exclude": True}``
     when an effort level is configured).
   - Uses ``response_format: {"type": "json_object"}`` on ``:free`` endpoints to
     avoid HTTP 404 endpoint filtering while still enforcing valid JSON.

Extensibility (Open-Closed Principle):
New models can be registered via :func:`register_model_profile`, approved
provider profiles can be customized via :func:`register_provider_profile`, and
new wire formats can be registered via :func:`gamas_bot.llm.register_llm_dialect`
without modifying transport code.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .config import (
    APPROVED_LLM_PROVIDERS,
    DEFAULT_AVALAI_MODEL,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_OPENROUTER_MODEL,
    NOTE_JSON_OBJECT_PROVIDERS,
    NoteRoute,
    Settings,
)


#: Keys that Gemini ``responseFormat.text.schema`` rejects or does not accept.
_GEMINI_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "$schema",
        "$id",
        "$defs",
        "definitions",
        "additionalProperties",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "pattern",
        "default",
        "examples",
        "title",
        "uniqueItems",
    }
)

#: Keys rejected by strict OpenAI-compatible ``json_schema`` validators.
_STRICT_OPENAI_UNSUPPORTED_KEYS = frozenset(
    {
        "$schema",
        "$id",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "pattern",
        "default",
        "examples",
        "uniqueItems",
    }
)


def normalize_model_id(model: str) -> str:
    """Normalize a configured model identifier for capability lookup."""
    raw = (model or "").strip().lower()
    if raw.startswith("models/"):
        raw = raw[len("models/") :]
    for suffix in (":generatecontent", ":streamgeneratecontent"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
    return raw.strip()


def fold_system_into_user(system: str, user: str) -> str:
    """Fold a system instruction into the user turn when a model forbids system roles."""
    sys_clean = (system or "").strip()
    user_clean = user or ""
    if not sys_clean:
        return user_clean
    if not user_clean.strip():
        return sys_clean
    return f"{sys_clean}\n\n---\n\n{user_clean}"


def to_gemini_response_format_schema(node: object) -> object:
    """Convert JSON Schema for Gemini 3.x ``generationConfig.responseFormat.text.schema``.

    Per Google's Gemini GenerateContent REST specification,
    ``responseFormat.text.schema`` accepts standard lowercase JSON Schema
    types (including ``["string", "null"]`` unions) while omitting unsupported
    validation keywords such as ``additionalProperties``.
    """
    if isinstance(node, list):
        return [to_gemini_response_format_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    converted: dict[str, object] = {}
    for key, value in node.items():
        if key in _GEMINI_UNSUPPORTED_SCHEMA_KEYS:
            continue
        if key == "type":
            if isinstance(value, str):
                converted["type"] = value.lower()
            elif isinstance(value, list):
                types = [str(item).lower() for item in value if isinstance(item, str)]
                if len(types) == 1:
                    converted["type"] = types[0]
                elif types:
                    converted["type"] = types
            continue
        if key == "properties" and isinstance(value, dict):
            converted["properties"] = {
                name: to_gemini_response_format_schema(sub)
                for name, sub in value.items()
            }
            continue
        converted[key] = to_gemini_response_format_schema(value)
    props = converted.get("properties")
    req = converted.get("required")
    if isinstance(props, dict) and isinstance(req, list):
        converted["required"] = [name for name in req if name in props]
    return converted


def _make_nullable_schema_node(node: dict[str, Any]) -> dict[str, Any]:
    """Allow ``null`` on an optional property in OpenAI strict mode."""
    updated = dict(node)
    raw_type = updated.get("type")
    if isinstance(raw_type, str):
        if raw_type != "null":
            updated["type"] = [raw_type, "null"]
    elif isinstance(raw_type, list):
        if "null" not in raw_type:
            updated["type"] = [*raw_type, "null"]
    return updated


def _normalize_strict_openai_node(node: object) -> object:
    """Recursively normalize a JSON Schema node for OpenAI strict mode."""
    if isinstance(node, list):
        return [_normalize_strict_openai_node(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    raw_props = node.get("properties")
    raw_required = node.get("required")
    originally_required = (
        {item for item in raw_required if isinstance(item, str)}
        if isinstance(raw_required, list)
        else set()
    )
    for key, value in node.items():
        if key in _STRICT_OPENAI_UNSUPPORTED_KEYS:
            continue
        if key in ("properties", "required", "additionalProperties"):
            continue
        out[key] = _normalize_strict_openai_node(value)
    node_type = out.get("type")
    is_object = (
        node_type == "object"
        or (isinstance(node_type, list) and "object" in node_type)
        or isinstance(raw_props, dict)
    )
    if isinstance(raw_props, dict):
        new_props: dict[str, Any] = {}
        for prop_name, prop_schema in raw_props.items():
            transformed_prop = _normalize_strict_openai_node(prop_schema)
            if prop_name not in originally_required and isinstance(transformed_prop, dict):
                transformed_prop = _make_nullable_schema_node(transformed_prop)
            new_props[prop_name] = transformed_prop
        out["properties"] = new_props
        out["required"] = list(new_props.keys())
    if is_object:
        out["additionalProperties"] = False
    return out


def to_strict_openai_json_schema(
    schema: dict[str, Any], *, name: str = "gamas_notes"
) -> dict[str, Any]:
    """Return an OpenAI-compatible strict ``json_schema`` descriptor."""
    normalized = _normalize_strict_openai_node(copy.deepcopy(schema))
    return {
        "name": name,
        "strict": True,
        "schema": normalized,
    }


def _strip_unsupported_non_strict_keys(node: object) -> object:
    """Remove only metadata keys; preserve optional properties for non-strict mode."""
    if isinstance(node, list):
        return [_strip_unsupported_non_strict_keys(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, object] = {}
    for key, value in node.items():
        if key in ("$schema", "$id"):
            continue
        if key == "properties" and isinstance(value, dict):
            out["properties"] = {
                k: _strip_unsupported_non_strict_keys(v) for k, v in value.items()
            }
            continue
        out[key] = _strip_unsupported_non_strict_keys(value)
    return out


def to_non_strict_openai_json_schema(
    schema: dict[str, Any], *, name: str = "gamas_notes"
) -> dict[str, Any]:
    """Return a portable non-strict OpenAI-compatible ``json_schema`` descriptor."""
    cleaned = _strip_unsupported_non_strict_keys(copy.deepcopy(schema))
    return {
        "name": name,
        "schema": cleaned,
    }


@dataclass(frozen=True)
class ModelCapabilityProfile:
    """Data-driven API capability contract for a single LLM model.

    All wire-level differences between models are declared here rather than
    scattered as ad-hoc model-name checks inside :mod:`gamas_bot.llm`.
    """

    model_id: str
    provider_slug: str
    context_window: int | None = None
    max_output_tokens: int | None = None
    supports_system_prompt: bool = True
    #: Whether sampling fields such as ``temperature`` may be sent.
    #: ``False`` for Gemini 3.5 Flash-Lite, where sampling parameters are
    #: deprecated and ignored (and will error in future generations).
    send_sampling_params: bool = True
    #: Gemini wire contract: ``"response_format"`` (Gemini 3.x native
    #: ``generationConfig.responseFormat.text``) or ``"none"``.
    gemini_response_format_style: str = "response_format"
    #: Gemini thinking config style: ``"thinking_level"`` (Gemini 3.x) or ``"none"``.
    gemini_thinking_style: str = "thinking_level"
    #: OpenAI-compatible reasoning control style:
    #: - ``"none"``: do not send a reasoning control field.
    #: - ``"openrouter_reasoning"``: send OpenRouter's documented ``reasoning``
    #:   object (``{"enabled": False}`` for ``none``, or
    #:   ``{"effort": level, "exclude": True}``).
    #: - ``"reasoning_effort"``: send top-level ``reasoning_effort`` (extension
    #:   hook for future OpenAI-compatible models that document it).
    reasoning_param_style: str = "none"
    #: OpenAI-compatible structured-output preference when a schema is provided:
    #: ``"json_schema"``, ``"json_object"``, or ``"none"``.
    structured_output_mode: str = "json_schema"
    supports_prompt_caching: bool = False
    supports_streaming: bool = True


#: Registered capability profiles for the three active production models only.
#: Additional models can be registered dynamically via :func:`register_model_profile`.
MODEL_PROFILES: dict[str, ModelCapabilityProfile] = {
    normalize_model_id(DEFAULT_GEMINI_MODEL): ModelCapabilityProfile(
        model_id=DEFAULT_GEMINI_MODEL,
        provider_slug="gemini",
        context_window=1_048_576,
        max_output_tokens=65_536,
        supports_system_prompt=True,
        send_sampling_params=False,
        gemini_response_format_style="response_format",
        gemini_thinking_style="thinking_level",
        reasoning_param_style="none",
        structured_output_mode="none",
        supports_prompt_caching=True,
        supports_streaming=True,
    ),
    normalize_model_id(DEFAULT_AVALAI_MODEL): ModelCapabilityProfile(
        model_id=DEFAULT_AVALAI_MODEL,
        provider_slug="avalai",
        context_window=1_000_000,
        max_output_tokens=393_216,
        supports_system_prompt=True,
        send_sampling_params=True,
        gemini_response_format_style="none",
        gemini_thinking_style="none",
        reasoning_param_style="none",
        structured_output_mode="json_schema",
        supports_prompt_caching=True,
        supports_streaming=True,
    ),
    normalize_model_id(DEFAULT_OPENROUTER_MODEL): ModelCapabilityProfile(
        model_id=DEFAULT_OPENROUTER_MODEL,
        provider_slug="openrouter",
        context_window=1_000_000,
        max_output_tokens=131_072,
        supports_system_prompt=True,
        send_sampling_params=True,
        gemini_response_format_style="none",
        gemini_thinking_style="none",
        reasoning_param_style="openrouter_reasoning",
        structured_output_mode="json_object",
        supports_prompt_caching=False,
        supports_streaming=True,
    ),
}


def register_model_profile(profile: ModelCapabilityProfile) -> None:
    """Register or override a model capability profile (OCP extension point)."""
    MODEL_PROFILES[normalize_model_id(profile.model_id)] = profile


def _default_model_profile_for_provider(
    model: str, provider_slug: str
) -> ModelCapabilityProfile:
    """Derive conservative capability defaults for an unregistered model override."""
    slug = (provider_slug or "").strip().lower()
    lower = normalize_model_id(model)
    provider = PROVIDER_PROFILES.get(slug)
    if slug == "gemini":
        return ModelCapabilityProfile(
            model_id=model,
            provider_slug="gemini",
            context_window=provider.context_window_tokens if provider else None,
            max_output_tokens=provider.max_output_tokens_cap if provider else None,
            supports_system_prompt=provider.supports_system_prompt if provider else True,
            send_sampling_params=False,
            gemini_response_format_style="response_format",
            gemini_thinking_style="thinking_level",
            reasoning_param_style="none",
            structured_output_mode="none",
            supports_prompt_caching=True,
            supports_streaming=True,
        )
    if slug == "openrouter":
        structured_mode = "json_object" if lower.endswith(":free") else "json_schema"
        return ModelCapabilityProfile(
            model_id=model,
            provider_slug="openrouter",
            context_window=provider.context_window_tokens if provider else None,
            max_output_tokens=provider.max_output_tokens_cap if provider else None,
            supports_system_prompt=provider.supports_system_prompt if provider else True,
            send_sampling_params=True,
            gemini_response_format_style="none",
            gemini_thinking_style="none",
            reasoning_param_style="openrouter_reasoning",
            structured_output_mode=structured_mode,
            supports_prompt_caching=False,
            supports_streaming=True,
        )
    return ModelCapabilityProfile(
        model_id=model,
        provider_slug=slug or "avalai",
        context_window=provider.context_window_tokens if provider else None,
        max_output_tokens=provider.max_output_tokens_cap if provider else None,
        supports_system_prompt=provider.supports_system_prompt if provider else True,
        send_sampling_params=provider.default_send_sampling_params if provider else True,
        gemini_response_format_style="none",
        gemini_thinking_style="none",
        reasoning_param_style=provider.default_reasoning_param_style if provider else "none",
        structured_output_mode="json_schema",
        supports_prompt_caching=False,
        supports_streaming=True,
    )


def get_model_profile(
    model: str, *, provider_slug: str = ""
) -> ModelCapabilityProfile:
    """Return the registered :class:`ModelCapabilityProfile` for *model*.

    Exact matches in :data:`MODEL_PROFILES` take precedence. Unknown/future
    model IDs configured through environment overrides receive conservative
    provider-derived defaults so operators can test new models without editing
    source code.
    """
    normalized = normalize_model_id(model)
    registered = MODEL_PROFILES.get(normalized)
    if registered is not None:
        return registered
    return _default_model_profile_for_provider(model, provider_slug)


def model_supports_system_prompt(model: str, *, provider_slug: str = "") -> bool:
    """Return whether *model* accepts native system instructions / system roles."""
    return get_model_profile(model, provider_slug=provider_slug).supports_system_prompt


@dataclass(frozen=True, slots=True)
class ProviderCapabilityProfile:
    """Documented wire-level capabilities of an approved LLM provider gateway."""

    slug: str
    supports_system_prompt: bool = True
    documented_json_object: bool = True
    documented_json_schema: bool = True
    strict_json_schema: bool = False
    supports_streaming_with_json_schema: bool = True
    documented_stream_options: bool = False
    supports_throughput_routing: bool = False
    default_send_sampling_params: bool = True
    default_reasoning_param_style: str = "none"
    extra_headers: tuple[tuple[str, str], ...] = ()
    #: Reference completion cap (tokens), separate from operational budgets.
    max_output_tokens_cap: int | None = None
    #: Reference context window (tokens) for the shipped default model.
    context_window_tokens: int | None = None
    #: Reference maximum input tokens for the shipped default model.
    max_input_tokens: int | None = None

    def model_profile_for(self, route: NoteRoute) -> ModelCapabilityProfile:
        """Resolve the effective :class:`ModelCapabilityProfile` for *route*."""
        return get_model_profile(route.model, provider_slug=self.slug)

    def supports_response_format(self, route: NoteRoute) -> bool:
        """Whether OpenAI ``response_format`` JSON mode is supported on *route*."""
        return bool(route.json_object_mode) or (
            self.documented_json_object and route.slug in NOTE_JSON_OBJECT_PROVIDERS
        )

    def supports_stream_options(self, route: NoteRoute) -> bool:
        """Whether ``stream_options: {"include_usage": True}`` is documented."""
        _ = route
        return self.documented_stream_options

    def clamp_output_tokens(self, route: NoteRoute, budget: int) -> int:
        """Clamp requested output budget to the tighter of model & provider caps."""
        capped = max(1, int(budget))
        model_profile = self.model_profile_for(route)
        for cap in (self.max_output_tokens_cap, model_profile.max_output_tokens):
            if cap is not None:
                capped = min(capped, cap)
        return capped

    def build_response_format(
        self,
        route: NoteRoute,
        settings: Settings,
        schema: dict[str, Any] | None,
        *,
        streaming: bool = False,
    ) -> dict[str, Any] | None:
        """Build the OpenAI-compatible ``response_format`` object, or ``None``."""
        if not self.supports_response_format(route):
            return None
        model_profile = self.model_profile_for(route)
        structured_enabled = bool(getattr(settings, "note_structured_output", True))
        if schema is not None and structured_enabled:
            mode = model_profile.structured_output_mode
            if mode == "none":
                return None
            if (
                mode == "json_schema"
                and self.documented_json_schema
                and (not streaming or self.supports_streaming_with_json_schema)
            ):
                descriptor = (
                    to_strict_openai_json_schema(schema)
                    if self.strict_json_schema
                    else to_non_strict_openai_json_schema(schema)
                )
                return {"type": "json_schema", "json_schema": descriptor}
            return {"type": "json_object"}
        if route.json_object_mode or settings.note_json_mode:
            return {"type": "json_object"}
        return None

    def apply_openai_extras(
        self,
        payload: dict[str, Any],
        route: NoteRoute,
        settings: Settings,
    ) -> None:
        """Apply documented provider- and model-specific OpenAI-compatible fields."""
        model_profile = self.model_profile_for(route)
        if self.supports_throughput_routing and getattr(
            settings, "note_openrouter_throughput", False
        ):
            payload["provider"] = {"sort": "throughput"}
        effort = (getattr(settings, "note_reasoning_effort", "") or "").strip().lower()
        if not effort:
            return
        style = model_profile.reasoning_param_style
        if style == "openrouter_reasoning":
            if effort == "none":
                payload["reasoning"] = {"enabled": False}
            else:
                payload["reasoning"] = {"effort": effort, "exclude": True}
        elif style == "reasoning_effort":
            payload["reasoning_effort"] = effort


#: Capability profiles for the three approved LLM gateways.
PROVIDER_PROFILES: dict[str, ProviderCapabilityProfile] = {
    "openrouter": ProviderCapabilityProfile(
        slug="openrouter",
        supports_system_prompt=True,
        documented_json_object=True,
        documented_json_schema=True,
        strict_json_schema=False,
        supports_streaming_with_json_schema=True,
        documented_stream_options=True,
        supports_throughput_routing=True,
        default_send_sampling_params=True,
        default_reasoning_param_style="openrouter_reasoning",
        context_window_tokens=1_000_000,
        max_input_tokens=1_000_000,
        max_output_tokens_cap=131_072,
    ),
    "avalai": ProviderCapabilityProfile(
        slug="avalai",
        supports_system_prompt=True,
        documented_json_object=True,
        documented_json_schema=True,
        strict_json_schema=False,
        supports_streaming_with_json_schema=True,
        documented_stream_options=False,
        supports_throughput_routing=False,
        default_send_sampling_params=True,
        default_reasoning_param_style="none",
        context_window_tokens=1_000_000,
        max_input_tokens=1_000_000,
        max_output_tokens_cap=393_216,
    ),
    "gemini": ProviderCapabilityProfile(
        slug="gemini",
        supports_system_prompt=True,
        documented_json_object=False,
        documented_json_schema=False,
        strict_json_schema=False,
        supports_streaming_with_json_schema=True,
        documented_stream_options=False,
        supports_throughput_routing=False,
        default_send_sampling_params=False,
        default_reasoning_param_style="none",
        context_window_tokens=1_048_576,
        max_input_tokens=1_048_576,
        max_output_tokens_cap=65_536,
    ),
}


def register_provider_profile(profile: ProviderCapabilityProfile) -> None:
    """Register an approved provider profile; transport extensibility is allowlisted."""
    if profile.slug not in APPROVED_LLM_PROVIDERS:
        raise ValueError(f"Unapproved LLM provider profile: {profile.slug}")
    PROVIDER_PROFILES[profile.slug] = profile


def get_provider_profile(slug: str) -> ProviderCapabilityProfile:
    """Return an approved provider profile or fail closed for unknown providers."""
    if slug not in APPROVED_LLM_PROVIDERS:
        raise ValueError(f"Unapproved LLM provider profile: {slug}")
    return PROVIDER_PROFILES[slug]
