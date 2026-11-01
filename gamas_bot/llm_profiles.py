"""Per-provider capability profiles and JSON Schema dialect transformers (SRP / OCP).

Why this module exists:

* **Single Responsibility (SRP)** — wire dialects (:mod:`gamas_bot.llm`) handle
  HTTP request/response serialization, whereas this module owns the pure,
  side-effect-free transformation of canonical JSON Schemas and token/reasoning
  parameters for each provider's documented API constraints.
* **Open/Closed Principle (OCP)** — adding or customizing a provider's schema
  or parameter policy is a :func:`register_provider_profile` call without
  editing transport loops or caller prompts.

Verified provider rules:

* **Gemini** (``generativelanguage.googleapis.com/v1beta``): OpenAPI 3.0
  ``Schema`` uses upper-case ``type`` names, honours ``required`` (without it,
  every property is optional), and rejects ``additionalProperties``.
* **NaraRouter** (``agnes-2.5-flash``): documents standard ``chat/completions``
  fields (``model``, ``messages``, ``temperature``, ``max_tokens``, ``stream``,
  ``reasoning_effort``) and does not document ``response_format`` or
  ``stream_options``.
* **OpenRouter**: treats ``response_format: {"type": "json_schema"}`` as an
  endpoint capability filter (returning HTTP 404 when no upstream endpoint for
  a ``:free`` model advertises ``structured_outputs``); ``json_object`` avoids
  filtering out free-pool endpoints.
* **Groq**: forbids ``stream: true`` with ``json_schema`` Structured Outputs,
  requires ``strict: true`` schemas to list all properties in ``required`` with
  ``additionalProperties: false``, and forbids ``reasoning_format: "raw"`` with
  JSON or Structured Outputs.
* **Cerebras**: supports ``strict: true`` JSON Schema with streaming, requires
  ``additionalProperties: false`` on every object (optional properties omitted
  from ``required`` are allowed), rejects ``minItems``/``maxItems``/``oneOf``/
  ``allOf``/``pattern``, caps ``gpt-oss-120b`` completion tokens at 40,960, and
  forbids ``reasoning_format: "raw"`` with JSON or Structured Outputs.
* **OpenAI** (``api.openai.com``): ``strict: true`` requires
  ``additionalProperties: false`` and all properties in ``required``; ``o1``/
  ``o3``/``o4``/``gpt-5`` reasoning families require ``max_completion_tokens``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .config import NOTE_JSON_OBJECT_PROVIDERS

if TYPE_CHECKING:
    from .config import NoteRoute, Settings


#: JSON Schema keywords rejected by Gemini's OpenAPI 3.0 ``responseSchema``.
_GEMINI_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "additionalProperties",
        "$schema",
        "$defs",
        "definitions",
        "title",
        "default",
        "examples",
        "const",
        "oneOf",
        "allOf",
        "uniqueItems",
        "minItems",
        "maxItems",
        "pattern",
    }
)

#: JSON Schema keywords rejected by strict-mode grammars on Cerebras / Groq / OpenAI.
_STRICT_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "pattern",
        "format",
        "uniqueItems",
        "oneOf",
        "allOf",
        "$schema",
        "title",
        "default",
        "examples",
    }
)

#: Groq model prefixes/ids documented as supporting ``json_schema`` with ``strict: true``.
_GROQ_STRICT_SCHEMA_PREFIXES = (
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "gpt-oss-120b",
    "gpt-oss-20b",
    "qwen/qwen3",
    "qwen3",
    "moonshotai/kimi-k2",
)

#: Model families on Groq / Cerebras that emit reasoning tokens and forbid
#: ``reasoning_format="raw"`` alongside ``json_object`` / ``json_schema``.
_REASONING_FORMAT_MODEL_MARKERS = (
    "gpt-oss",
    "qwen3",
    "qwen-3",
    "qwq",
    "deepseek-r1",
)

#: Native OpenAI reasoning model prefixes that require ``max_completion_tokens``
#: and reject non-default ``temperature`` on ``api.openai.com``.
_OPENAI_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5")


def to_gemini_schema(node: object) -> object:
    """Convert a JSON Schema into Google Gemini's OpenAPI 3.0 ``Schema`` dialect.

    * Upper-cases ``type`` values (``"object"`` -> ``"OBJECT"``) and converts
      ``["<type>", "null"]`` type unions into ``nullable: True``.
    * Preserves ``required`` filtered to keys actually declared in
      ``properties`` (Gemini treats omitted ``required`` as all-optional).
    * Strips ``additionalProperties`` and other unsupported JSON Schema fields.
    """
    if isinstance(node, list):
        return [to_gemini_schema(item) for item in node]
    if not isinstance(node, dict):
        return node

    converted: dict[str, Any] = {}
    raw_props = node.get("properties")
    prop_names = set(raw_props) if isinstance(raw_props, dict) else None

    for key, value in node.items():
        if key in _GEMINI_UNSUPPORTED_SCHEMA_KEYS:
            continue
        if key == "type":
            if isinstance(value, str):
                converted["type"] = value.upper()
            elif isinstance(value, list):
                non_null = [
                    item for item in value if isinstance(item, str) and item != "null"
                ]
                if non_null:
                    converted["type"] = non_null[0].upper()
                if "null" in value:
                    converted["nullable"] = True
            continue
        if key == "required" and isinstance(value, list):
            kept = [
                item
                for item in value
                if isinstance(item, str)
                and (prop_names is None or item in prop_names)
            ]
            if kept:
                converted["required"] = kept
            continue
        if key == "properties" and isinstance(value, dict):
            converted["properties"] = {
                name: to_gemini_schema(sub) for name, sub in value.items()
            }
            continue
        converted[key] = to_gemini_schema(value)
    return converted


def _make_nullable_schema_node(node: dict[str, Any]) -> dict[str, Any]:
    """Allow ``null`` on an optional property forced into strict ``required``."""
    updated = dict(node)
    raw_type = updated.get("type")
    if isinstance(raw_type, str) and raw_type != "null":
        updated["type"] = [raw_type, "null"]
        if isinstance(updated.get("enum"), list) and None not in updated["enum"]:
            updated["enum"] = [*updated["enum"], None]
    elif isinstance(raw_type, list) and "null" not in raw_type:
        updated["type"] = [*raw_type, "null"]
    return updated


def _transform_strict_schema_node(
    node: object,
    *,
    require_all_properties: bool,
) -> object:
    if isinstance(node, list):
        return [
            _transform_strict_schema_node(
                item, require_all_properties=require_all_properties
            )
            for item in node
        ]
    if not isinstance(node, dict):
        return node

    converted: dict[str, Any] = {}
    raw_props = node.get("properties")
    raw_required = node.get("required")
    originally_required = (
        {item for item in raw_required if isinstance(item, str)}
        if isinstance(raw_required, list)
        else set()
    )

    for key, value in node.items():
        if key in _STRICT_UNSUPPORTED_SCHEMA_KEYS:
            continue
        if key in ("properties", "required", "additionalProperties"):
            continue
        converted[key] = _transform_strict_schema_node(
            value, require_all_properties=require_all_properties
        )

    is_object = converted.get("type") == "object" or isinstance(raw_props, dict)
    if isinstance(raw_props, dict):
        new_props: dict[str, Any] = {}
        for prop_name, prop_schema in raw_props.items():
            transformed_prop = _transform_strict_schema_node(
                prop_schema, require_all_properties=require_all_properties
            )
            if (
                require_all_properties
                and prop_name not in originally_required
                and isinstance(transformed_prop, dict)
            ):
                transformed_prop = _make_nullable_schema_node(transformed_prop)
            new_props[prop_name] = transformed_prop
        converted["properties"] = new_props
        if require_all_properties:
            converted["required"] = list(new_props.keys())
        else:
            kept_req = [name for name in originally_required if name in new_props]
            if isinstance(raw_required, list):
                converted["required"] = [
                    item
                    for item in raw_required
                    if isinstance(item, str) and item in new_props
                ]
            elif kept_req:
                converted["required"] = kept_req

    if is_object:
        converted["additionalProperties"] = False
    return converted


def to_cerebras_json_schema(
    schema: dict[str, Any], *, name: str = "gamas_notes"
) -> dict[str, Any]:
    """Build Cerebras's documented ``strict: true`` ``json_schema`` envelope."""
    transformed = _transform_strict_schema_node(
        schema, require_all_properties=False
    )
    return {"name": name, "strict": True, "schema": transformed}


def to_strict_openai_json_schema(
    schema: dict[str, Any], *, name: str = "gamas_notes"
) -> dict[str, Any]:
    """Build OpenAI/Groq's ``strict: true`` ``json_schema`` envelope."""
    transformed = _transform_strict_schema_node(
        schema, require_all_properties=True
    )
    return {"name": name, "strict": True, "schema": transformed}


def to_non_strict_openai_json_schema(
    schema: dict[str, Any], *, name: str = "gamas_notes"
) -> dict[str, Any]:
    """Build the portable non-strict ``json_schema`` envelope for generic gateways."""
    return {"name": name, "schema": schema}


def _is_openai_official_host(base_url: str) -> bool:
    host = (urlsplit(base_url).hostname or "").lower()
    return host == "api.openai.com" or host.endswith(".openai.com")


def _is_openai_reasoning_model(model: str) -> bool:
    bare = model.strip().lower().rsplit("/", 1)[-1]
    return any(
        bare == prefix or bare.startswith(prefix + "-")
        for prefix in _OPENAI_REASONING_PREFIXES
    )


def _needs_parsed_reasoning_format(model: str) -> bool:
    lower = model.strip().lower()
    return any(marker in lower for marker in _REASONING_FORMAT_MODEL_MARKERS)


def _groq_supports_strict_schema(model: str) -> bool:
    lower = model.strip().lower()
    return any(
        lower == prefix or lower.startswith(prefix)
        for prefix in _GROQ_STRICT_SCHEMA_PREFIXES
    )


def _openrouter_prefers_json_object(model: str) -> bool:
    lower = model.strip().lower()
    return lower.endswith(":free") or lower in ("openrouter/free", "openrouter/auto")


@dataclass(frozen=True, slots=True)
class ProviderCapabilityProfile:
    """Declarative capability and request-shaping rules for one provider slug."""

    slug: str
    documented_json_object: bool = True
    documented_json_schema: bool = True
    supports_streaming_with_json_schema: bool = True
    documented_stream_options: bool = True
    documented_reasoning_effort: bool = False
    parsed_reasoning_for_json: bool = False
    max_output_tokens_cap: int | None = None

    def supports_response_format(self, route: NoteRoute) -> bool:
        return bool(route.json_object_mode) or (
            self.documented_json_object and route.slug in NOTE_JSON_OBJECT_PROVIDERS
        )

    def supports_stream_options(self, route: NoteRoute) -> bool:
        _ = route
        return self.documented_stream_options

    def clamp_output_tokens(self, route: NoteRoute, budget: int) -> int:
        capped = max(1, int(budget))
        if self.max_output_tokens_cap is not None:
            capped = min(capped, self.max_output_tokens_cap)
        if self.slug == "groq":
            lower_model = route.model.strip().lower()
            if "qwen" in lower_model or "llama" in lower_model:
                capped = min(capped, 32_768)
            else:
                capped = min(capped, 65_536)
        return capped

    def build_response_format(
        self,
        route: NoteRoute,
        settings: Settings,
        schema: dict[str, Any] | None,
        *,
        streaming: bool,
    ) -> dict[str, Any] | None:
        use_schema = schema is not None and bool(
            getattr(settings, "note_structured_output", True)
        )
        if use_schema and schema is not None:
            if not self.supports_response_format(route) or not self.documented_json_schema:
                return None
            if self.slug == "openrouter" and _openrouter_prefers_json_object(route.model):
                return {"type": "json_object"}
            if self.slug == "groq":
                if streaming or not _groq_supports_strict_schema(route.model):
                    return {"type": "json_object"}
                return {
                    "type": "json_schema",
                    "json_schema": to_strict_openai_json_schema(schema),
                }
            if self.slug == "cerebras":
                return {
                    "type": "json_schema",
                    "json_schema": to_cerebras_json_schema(schema),
                }
            if self.slug == "openai_compatible" and _is_openai_official_host(
                route.base_url
            ):
                return {
                    "type": "json_schema",
                    "json_schema": to_strict_openai_json_schema(schema),
                }
            return {
                "type": "json_schema",
                "json_schema": to_non_strict_openai_json_schema(schema),
            }
        if route.json_object_mode and self.supports_response_format(route):
            return {"type": "json_object"}
        return None

    def apply_openai_extras(
        self,
        payload: dict[str, Any],
        route: NoteRoute,
        settings: Settings,
    ) -> None:
        """Attach provider-specific OpenAI-compatible extensions safely."""
        if self.slug == "groq" and "max_tokens" in payload:
            payload["max_completion_tokens"] = payload.pop("max_tokens")

        if (
            self.slug == "openai_compatible"
            and _is_openai_official_host(route.base_url)
            and _is_openai_reasoning_model(route.model)
        ):
            if "max_tokens" in payload:
                payload["max_completion_tokens"] = payload.pop("max_tokens")
            payload.pop("temperature", None)

        if (
            self.parsed_reasoning_for_json
            and "response_format" in payload
            and _needs_parsed_reasoning_format(route.model)
        ):
            payload["reasoning_format"] = "parsed"

        if (
            getattr(settings, "note_openrouter_throughput", False)
            and self.slug == "openrouter"
        ):
            payload["provider"] = {"sort": "throughput"}

        if settings.note_reasoning_effort and self.documented_reasoning_effort:
            payload["reasoning_effort"] = settings.note_reasoning_effort


_DEFAULT_PROFILE = ProviderCapabilityProfile(
    slug="openai_compatible",
    documented_json_object=True,
    documented_json_schema=True,
    supports_streaming_with_json_schema=True,
    documented_stream_options=True,
    documented_reasoning_effort=False,
)

PROVIDER_PROFILES: dict[str, ProviderCapabilityProfile] = {
    "nara": ProviderCapabilityProfile(
        slug="nara",
        documented_json_object=False,
        documented_json_schema=False,
        supports_streaming_with_json_schema=False,
        documented_stream_options=False,
        documented_reasoning_effort=True,
    ),
    "openrouter": ProviderCapabilityProfile(
        slug="openrouter",
        documented_json_object=True,
        documented_json_schema=True,
        supports_streaming_with_json_schema=True,
        documented_stream_options=True,
        documented_reasoning_effort=True,
    ),
    "groq": ProviderCapabilityProfile(
        slug="groq",
        documented_json_object=True,
        documented_json_schema=True,
        supports_streaming_with_json_schema=False,
        documented_stream_options=True,
        documented_reasoning_effort=True,
        parsed_reasoning_for_json=True,
    ),
    "cerebras": ProviderCapabilityProfile(
        slug="cerebras",
        documented_json_object=True,
        documented_json_schema=True,
        supports_streaming_with_json_schema=True,
        documented_stream_options=True,
        documented_reasoning_effort=True,
        parsed_reasoning_for_json=True,
        max_output_tokens_cap=40_960,
    ),
    "openai_compatible": _DEFAULT_PROFILE,
    "gemini": ProviderCapabilityProfile(
        slug="gemini",
        documented_json_object=True,
        documented_json_schema=True,
        supports_streaming_with_json_schema=True,
        documented_stream_options=False,
        documented_reasoning_effort=False,
    ),
    "anthropic": ProviderCapabilityProfile(
        slug="anthropic",
        documented_json_object=False,
        documented_json_schema=False,
        supports_streaming_with_json_schema=False,
        documented_stream_options=False,
        documented_reasoning_effort=False,
    ),
}


def register_provider_profile(profile: ProviderCapabilityProfile) -> None:
    """Register or override a provider capability profile (OCP extension point)."""
    PROVIDER_PROFILES[profile.slug] = profile


def get_provider_profile(slug: str) -> ProviderCapabilityProfile:
    """Return the capability profile for *slug* (defaults to generic OpenAI)."""
    return PROVIDER_PROFILES.get(slug, _DEFAULT_PROFILE)
