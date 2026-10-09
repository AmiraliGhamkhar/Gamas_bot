"""Provider adapters: request building, response parsing, error mapping.

One adapter owns everything *transport* for a provider family:

* **build** — URL, authentication headers, payload (capability-aware sampling
  and structured-output fields only; never vendor parameters a model did not
  advertise);
* **parse** — a normalized :class:`NoteResponse` (text, usage, finish reason,
  request id, quota headers);
* **map_error** — a normalized :class:`NoteFailure` (retryable? quota
  exhausted? billing required? credential invalid? model unavailable?);
* **discovery** — where to list models and how to normalize the listing.

Adapters never log and never include secrets in reprs; the only place a secret
exists is inside the built request's headers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from ..config import RESERVED_HEADER_NAMES, Settings
from ..structuring import (
    _endpoint,
    _extract_error_detail,
    _retry_after_seconds,
    anthropic_sampling_params,
)
from .models import ModelInfo, parse_discovery
from .registry import (
    PROTOCOL_ANTHROPIC,
    PROTOCOL_GEMINI_NATIVE,
    PROTOCOL_GEMINI_OPENAI,
    PROTOCOL_OPENAI,
    registry_info,
)
from .schema import gemini_compat, note_json_schema, strict_note_json_schema

logger = logging.getLogger(__name__)

NOTE_TEMPERATURE = 0.2  # identical to the legacy deterministic sampling

#: Structured-output strategy vocabulary.
STRATEGY_STRICT_SCHEMA = "strict_json_schema"
STRATEGY_JSON_SCHEMA = "json_schema"
STRATEGY_GEMINI_SCHEMA = "gemini_schema"
STRATEGY_GEMINI_MIME = "gemini_mime"   # responseMimeType application/json
STRATEGY_JSON_OBJECT = "json_object"
STRATEGY_PROMPT = "prompt"

JSON_STRATEGIES = (
    STRATEGY_STRICT_SCHEMA,
    STRATEGY_JSON_SCHEMA,
    STRATEGY_GEMINI_SCHEMA,
    STRATEGY_GEMINI_MIME,
    STRATEGY_JSON_OBJECT,
    STRATEGY_PROMPT,
)

#: Request types flowing through the platform.
REQUEST_OUTLINE = "outline"
REQUEST_CHUNK = "chunk_structuring"
REQUEST_REPAIR = "repair"
REQUEST_COMPILE = "final_compilation"
REQUEST_HEALTH = "health_test"
REQUEST_BENCHMARK = "benchmark"

#: Failure categories (normalized across providers).
FAIL_AUTH = "auth"
FAIL_RATE_LIMITED = "rate_limited"
FAIL_SERVER = "server"
FAIL_BAD_REQUEST = "bad_request"
FAIL_TRANSIENT = "transient"
FAIL_QUOTA = "quota_exhausted"
FAIL_BILLING = "billing_required"
FAIL_MODEL = "model_unavailable"
FAIL_CONTRACT = "contract"
FAIL_CONTENT_BLOCKED = "content_blocked"
FAIL_UNKNOWN = "unknown"

RETRYABLE_CATEGORIES = frozenset(
    {FAIL_TRANSIENT, FAIL_RATE_LIMITED, FAIL_SERVER}
)

#: Quota/rate-limit headers worth persisting per request (allowlist).
QUOTA_HEADER_PREFIXES = ("x-ratelimit-", "x-request-quota", "retry-after")
QUOTA_HEADER_NAMES = ("x-request-id", "x-usage", "x-ep-token-remaining")


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Everything an adapter needs to build one request."""

    service: str
    request_type: str
    provider: str          # credential-pool slug (legacy naming preserved)
    canonical: str         # registry slug
    base_url: str
    model: str
    system_prompt: str
    user_text: str         # prompt + document + reminder (never logged)
    max_output_tokens: int
    model_info: ModelInfo
    json_strategy: str
    temperature: float | None = NOTE_TEMPERATURE
    reasoning_policy: str = "model_default"   # none | model_default | low | medium | high
    extra_headers: tuple[tuple[str, str], ...] = ()
    request_id: str = ""
    job_id: str = ""
    submission_id: int | None = None
    route_position: int = 0
    attempt: int = 1

    @property
    def input_chars(self) -> int:
        return len(self.system_prompt) + len(self.user_text)


@dataclass(frozen=True, slots=True)
class NoteRequest:
    """A fully-built, provider-specific request."""

    method: str
    url: str
    headers: dict[str, str] = field(repr=False)
    json_body: dict = field(repr=False)
    params: dict[str, str] = field(default_factory=dict)
    json_strategy: str = STRATEGY_PROMPT

    def redacted(self) -> "NoteRequest":
        """Log/debug view: headers masked, content replaced by metadata."""
        masked_headers = {}
        for name, value in self.headers.items():
            lowered = name.lower()
            if lowered in RESERVED_HEADER_NAMES or "key" in lowered or "token" in lowered:
                masked_headers[name] = "***" + value[-4:] if len(value) >= 8 else "***"
            else:
                masked_headers[name] = value
        body: dict = {"_meta": {"bytes": len(str(self.json_body))}} if self.json_body else {}
        return NoteRequest(
            method=self.method,
            url=self.url,
            headers=masked_headers,
            params=dict(self.params),
            json_body=body,
            json_strategy=self.json_strategy,
        )


@dataclass(frozen=True, slots=True)
class UsageStats:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class NoteResponse:
    """Normalized successful answer."""

    text: str
    provider: str
    model: str
    usage: UsageStats = field(default_factory=UsageStats)
    finish_reason: str = ""
    request_id: str = ""
    latency_ms: int = 0
    http_status: int = 200
    quota_headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NoteFailure(Exception):
    """Normalized failure with a safe, content-free message.

    Raised by adapters/routers so the failover engine can classify and act
    without ever leaking request content into exception strings.
    """

    category: str
    provider: str
    model: str
    http_status: int | None = None
    retryable: bool = False
    retry_after: float | None = None
    billing_required: bool = False
    quota_exhausted: bool = False
    credential_invalid: bool = False
    model_unavailable: bool = False
    local_budget_blocked: bool = False
    message: str = ""

    def __str__(self) -> str:  # never raise with content
        status = f"HTTP {self.http_status}" if self.http_status else self.category
        return f"{status}: {self.message}" if self.message else status


def quota_headers(headers) -> dict[str, str]:
    """Allowlisted rate-limit/request-id headers (everything else is dropped)."""
    captured: dict[str, str] = {}
    for name, value in dict(headers).items():
        lowered = str(name).lower()
        if lowered.startswith(QUOTA_HEADER_PREFIXES) or lowered in QUOTA_HEADER_NAMES:
            captured[lowered] = str(value)[:120]
    return captured


class NoteAdapter:
    """Base class. Subclasses set ``canonical``/``protocol`` and override hooks."""

    canonical = "openai_compatible"
    protocol = PROTOCOL_OPENAI
    supports_response_format = True     # OpenAI-family response_format field
    supports_strict_schema = False      # strict JSON schema transport exists
    supports_discovery = True

    def __init__(self, settings: Settings):
        self.settings = settings

    # -- construction ------------------------------------------------------

    def default_base_url(self) -> str:
        base = registry_info(self.canonical).base_url
        return base or "https://api.openai.com/v1"

    def base_url_for(self, ctx: RequestContext) -> str:
        return (ctx.base_url or self.default_base_url()).rstrip("/")

    def auth_headers(self, secret: str) -> dict[str, str]:
        style = registry_info(self.canonical).auth_style
        if not secret:
            return {}
        from .registry import AuthStyle

        if style is AuthStyle.BEARER:
            return {"Authorization": f"Bearer {secret}"}
        if style is AuthStyle.X_GOOG_API_KEY:
            return {"x-goog-api-key": secret}
        if style is AuthStyle.X_API_KEY:
            return {"x-api-key": secret}
        return {}

    def _merge_extra_headers(self, ctx: RequestContext) -> dict[str, str]:
        extra = {
            name: value
            for name, value in ctx.extra_headers
            if name.lower() not in RESERVED_HEADER_NAMES
        }
        if len(extra) != len(ctx.extra_headers):
            logger.warning(
                "Ignored reserved authentication header(s) provider=%s", ctx.canonical
            )
        return extra

    def sampling_params(self, ctx: RequestContext) -> dict:
        """Capability-checked sampling fields (never a blind parameter)."""
        caps = ctx.model_info.capabilities
        if self.protocol == PROTOCOL_ANTHROPIC:
            return anthropic_sampling_params(ctx.model)
        params: dict = {}
        if ctx.temperature is not None and caps.supports_temperature:
            params["temperature"] = NOTE_TEMPERATURE
        return params

    def structured_output_fields(self, ctx: RequestContext) -> dict:
        """Transport-level structured output for the resolved strategy."""
        strategy = ctx.json_strategy
        if strategy == STRATEGY_STRICT_SCHEMA:
            return {
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "gamas_note",
                        "schema": strict_note_json_schema(),
                        "strict": True,
                    },
                }
            }
        if strategy == STRATEGY_JSON_SCHEMA:
            return {
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "gamas_note",
                        "schema": note_json_schema(),
                        "strict": False,
                    },
                }
            }
        if strategy == STRATEGY_JSON_OBJECT:
            return {"response_format": {"type": "json_object"}}
        return {}

    def reasoning_fields(self, ctx: RequestContext) -> dict:
        return {}

    def build(self, ctx: RequestContext, secret: str) -> NoteRequest:
        raise NotImplementedError

    # -- response parsing ---------------------------------------------------

    def parse(self, payload: dict, *, http_status: int, headers, latency_ms: int) -> NoteResponse:
        raise NotImplementedError

    # -- error mapping ------------------------------------------------------

    def map_error(
        self, status: int, raw_body: bytes | None, headers, *, model: str, key: str | None
    ) -> NoteFailure:
        detail = _extract_error_detail(raw_body, key) or ""
        retry_after = _retry_after_seconds(headers)
        retryable = status in {408, 409, 425, 500, 502, 503, 504}
        if status == 429:
            category = FAIL_RATE_LIMITED
            retryable = True
        elif status in {401, 403}:
            category = FAIL_AUTH
        elif status == 402:
            category = FAIL_BILLING
        elif status == 404:
            category = FAIL_MODEL
        elif status >= 500 or status in {408, 425}:
            category = FAIL_SERVER
        elif status == 400:
            category = FAIL_BAD_REQUEST
        else:
            category = FAIL_UNKNOWN
        return NoteFailure(
            category=category,
            provider=self.canonical,
            model=model,
            http_status=status,
            retryable=retryable,
            retry_after=retry_after,
            billing_required=(status == 402),
            quota_exhausted=(status == 429),
            credential_invalid=(status in {401, 403}),
            model_unavailable=(status == 404),
            message=detail,
        )

    # -- discovery ----------------------------------------------------------

    def discovery_request(
        self, base_url: str, secret: str, *, page: int = 1
    ) -> tuple[str, str, dict[str, str]] | None:
        if not self.supports_discovery or not base_url:
            return None
        return ("GET", _endpoint(base_url.rstrip("/"), "models"), self.auth_headers(secret))

    def parse_discovery(self, payload, **_kwargs) -> list[ModelInfo]:
        return parse_discovery(self.canonical, payload)

    # -- probes -------------------------------------------------------------

    def probe_request(self, credential) -> tuple[str, str, dict[str, str], str]:
        """Read-only health probe: method, url, headers, check_type."""
        base = (credential.base_url or self.default_base_url()).rstrip("/")
        discovery = self.discovery_request(base, credential.secret)
        if discovery is None:
            raise ValueError("no read-only probe available")
        method, url, headers = discovery
        return (method, url, headers, "read_only")


class OpenAICompatibleAdapter(NoteAdapter):
    """legacy ``openai_compatible`` + every chat/completions-style provider."""

    canonical = "openai_compatible"
    protocol = PROTOCOL_OPENAI
    #: Field name for the output budget (some gateways renamed it).
    max_tokens_field = "max_tokens"
    #: Extra top-level fields merged into every chat request.
    extra_body_fields: dict = {}

    def build(self, ctx: RequestContext, secret: str) -> NoteRequest:
        base = self.base_url_for(ctx)
        headers = self.auth_headers(secret)
        headers.update(self._merge_extra_headers(ctx))
        messages = [{"role": "user", "content": ctx.user_text}]
        if ctx.system_prompt:
            messages.insert(0, {"role": "system", "content": ctx.system_prompt})
        payload: dict = {
            "model": ctx.model,
            "messages": messages,
            self.max_tokens_field: ctx.max_output_tokens,
        }
        payload.update(self.sampling_params(ctx))
        payload.update(self.structured_output_fields(ctx))
        payload.update(self.reasoning_fields(ctx))
        payload.update(self.extra_body_fields)
        return NoteRequest(
            method="POST",
            url=_endpoint(base, "chat/completions"),
            headers=headers,
            params={},
            json_body=payload,
            json_strategy=ctx.json_strategy,
        )

    def parse(self, payload: dict, *, http_status: int, headers, latency_ms: int) -> NoteResponse:
        choice_error = None
        try:
            choice = payload["choices"][0]
            finish = str(choice.get("finish_reason") or "")
            if finish == "length":
                from ..structuring import StructuringError

                raise StructuringError(
                    "خروجی جزوه به سقف توکن رسید؛ متن خام برگردانده می‌شود."
                )
            content = choice["message"].get("content")
            if isinstance(content, list):
                text = "".join(
                    str(part.get("text", "")) for part in content if isinstance(part, dict)
                )
            else:
                text = "" if content is None else str(content)
            text = text.strip()
            if not text:
                choice_error = "empty"
        except KeyError:
            choice_error = "contract"
        if choice_error:
            raise ValueError(choice_error)
        usage_raw = payload.get("usage") if isinstance(payload, dict) else None
        usage = UsageStats()
        if isinstance(usage_raw, dict):
            usage = UsageStats(
                input_tokens=_int_or_none(usage_raw.get("prompt_tokens")),
                output_tokens=_int_or_none(usage_raw.get("completion_tokens")),
                total_tokens=_int_or_none(usage_raw.get("total_tokens")),
            )
        request_id = ""
        if isinstance(payload, dict) and isinstance(payload.get("id"), str):
            request_id = str(payload["id"])[:64]
        request_id = request_id or str(
            dict(headers).get("x-request-id", "")
        )[:64]
        return NoteResponse(
            text=text,
            provider=self.canonical,
            model=str(payload.get("model") or ""),
            usage=usage,
            finish_reason=finish,
            request_id=request_id,
            latency_ms=latency_ms,
            http_status=http_status,
            quota_headers=quota_headers(headers),
        )


class NaraAdapter(OpenAICompatibleAdapter):
    """NaraRouter with account catalog + explicit public Free-plan evidence."""

    canonical = "nara"
    FREE_PLAN_URL = "https://router.bynara.id/api/plans"

    def public_plan_request(self) -> tuple[str, str, dict[str, str]]:
        """Public, read-only tier/model catalog; it does not use an API key."""
        return ("GET", self.FREE_PLAN_URL, {})

    @staticmethod
    def plan_model_statuses(payload) -> dict[str, str] | None:
        """Map public plan model IDs to free/paid only when the plan is explicit.

        A missing/malformed Free plan returns ``None`` so the adapter leaves
        every account-discovered model as unknown rather than inferring from a
        zero price field.
        """
        from .models import FREE_PLAN, PAID

        plans = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(plans, list):
            return None
        active_free = [
            item for item in plans
            if isinstance(item, dict)
            and str(item.get("code", "")).strip().casefold() == "free"
            and item.get("is_active") is True
            and isinstance(item.get("models"), list)
        ]
        if not active_free:
            return None
        free_models = {
            str(model).strip().casefold()
            for plan in active_free
            for model in plan["models"]
            if isinstance(model, str) and model.strip()
        }
        paid_models = {
            str(model).strip().casefold()
            for plan in plans
            if isinstance(plan, dict)
            and str(plan.get("code", "")).strip().casefold() != "free"
            and plan.get("is_active") is True
            and isinstance(plan.get("models"), list)
            for model in plan["models"]
            if isinstance(model, str) and model.strip()
        }
        statuses = dict.fromkeys(free_models, FREE_PLAN)
        statuses.update(dict.fromkeys(paid_models - free_models, PAID))
        return statuses

    def parse_discovery(self, payload, *, plan_model_statuses=None) -> list[ModelInfo]:
        from dataclasses import replace

        from .models import FREE_UNKNOWN

        discovered = super().parse_discovery(payload)
        statuses = plan_model_statuses or {}
        return [
            replace(
                item,
                free_status=statuses.get(item.model_id.casefold(), FREE_UNKNOWN),
                source="live:/v1/models+api/plans" if plan_model_statuses is not None else "live:/v1/models",
            )
            for item in discovered
        ]


class GroqAdapter(OpenAICompatibleAdapter):
    """Groq: strict JSON schema where advertised + reasoning controls."""

    canonical = "groq"
    supports_strict_schema = True
    #: Structured outputs require non-streaming; streaming is never enabled.
    extra_body_fields: dict = {"stream": False}

    def reasoning_fields(self, ctx: RequestContext) -> dict:
        caps = ctx.model_info.capabilities
        if not caps.supports_reasoning_effort:
            return {}
        recommended = caps.recommended_reasoning_effort
        policy = ctx.reasoning_policy
        effort = policy if policy in {"none", "low", "medium", "high"} else recommended
        if not effort:
            return {}
        return {"reasoning_effort": effort}

    def map_error(self, status, raw_body, headers, *, model, key):
        failure = super().map_error(status, raw_body, headers, model=model, key=key)
        quota = quota_headers(headers)
        if quota and not failure.message:
            remaining = quota.get("x-ratelimit-remaining-requests")
            if remaining is not None:
                failure = _replace_failure(failure, message=f"rate limited (remaining={remaining})")
        return failure


class OpenRouterAdapter(OpenAICompatibleAdapter):
    canonical = "openrouter"
    supports_strict_schema = True
    extra_body_fields: dict = {"usage": {"include": True}}

    def build(self, ctx: RequestContext, secret: str) -> NoteRequest:
        request = super().build(ctx, secret)
        # OpenRouter documents app-attribution headers; they carry no data.
        request.headers.setdefault("HTTP-Referer", "https://github.com/AmiraliGhamkhar/Gamas_bot")
        request.headers.setdefault("X-Title", "Gamas Study Bot")
        return request

    def key_info_request(self, base_url: str, secret: str) -> tuple[str, str, dict[str, str]] | None:
        """Read-only entitlement probe: GET /api/v1/key.

        The response carries ``free_model_daily_requests {used, limit,
        remaining}`` and ``is_free_tier`` — the authoritative free-ledger
        state for the account (verified against openrouter.ai/docs
        2026-10-09). Successful chat responses carry no X-RateLimit headers,
        so this endpoint is the only live source for the daily counter.
        """
        if not base_url:
            return None
        return (
            "GET",
            _endpoint(base_url.rstrip("/"), "key"),
            self.auth_headers(secret),
        )

    def map_error(self, status, raw_body, headers, *, model, key):
        failure = super().map_error(status, raw_body, headers, model=model, key=key)
        lowered = (failure.message or "").lower()
        # OpenRouter classifies daily free-model quota exhaustion as 429; a
        # body mention of "free" quota makes it explicit (never from price).
        if status == 429 and ("free" in lowered or "daily" in lowered):
            failure = _replace_failure(failure, category=FAIL_QUOTA, quota_exhausted=True)
        if status == 402:
            failure = _replace_failure(failure, category=FAIL_BILLING, billing_required=True)
        return failure


class MistralAdapter(OpenAICompatibleAdapter):
    canonical = "mistral"


class SambanovaAdapter(OpenAICompatibleAdapter):
    canonical = "sambanova"


class ZaiAdapter(OpenAICompatibleAdapter):
    canonical = "zai"

    def reasoning_fields(self, ctx: RequestContext) -> dict:
        # GLM-4.5 models run "thinking" by default; the provider documents a
        # thinking toggle, sent only for models advertised as reasoning models.
        if (
            ctx.model_info.capabilities.supports_reasoning
            and ctx.reasoning_policy == "none"
        ):
            return {"thinking": {"type": "disabled"}}
        return {}


class NvidiaAdapter(OpenAICompatibleAdapter):
    canonical = "nvidia"


class CloudflareAdapter(OpenAICompatibleAdapter):
    canonical = "cloudflare"
    discovery_page_size = 100
    max_discovery_pages = 20

    def default_base_url(self) -> str:
        # Account-scoped; a missing account id is a configuration error.
        account_id = getattr(self.settings, "cloudflare_account_id", "") or ""
        if account_id:
            return (
                "https://api.cloudflare.com/client/v4/accounts/"
                f"{quote(account_id.strip(), safe='')}/ai/v1"
            )
        return ""

    def _account_model_search_url(self, base_url: str, *, page: int) -> str | None:
        parsed = urlsplit(base_url)
        parts = [part for part in parsed.path.split("/") if part]
        account_id = ""
        if "accounts" in parts:
            index = parts.index("accounts")
            if index + 1 < len(parts):
                account_id = parts[index + 1]
        account_id = account_id or (getattr(self.settings, "cloudflare_account_id", "") or "").strip()
        if not account_id or not parsed.scheme or not parsed.netloc:
            return None
        path = f"/client/v4/accounts/{quote(account_id, safe='')}/ai/models/search"
        query = urlencode({"page": max(1, int(page)), "per_page": self.discovery_page_size, "format": "openrouter"})
        return urlunsplit((parsed.scheme, parsed.netloc, path, query, ""))

    def discovery_request(self, base_url, secret, *, page: int = 1):
        url = self._account_model_search_url(base_url, page=page)
        if not url:
            return None
        return ("GET", url, self.auth_headers(secret))

    @staticmethod
    def _model_search_entries(payload):
        if not isinstance(payload, dict) or payload.get("success") is False:
            return None
        entries = payload.get("data")
        if entries is None:
            entries = payload.get("result")
        if isinstance(entries, dict):
            entries = entries.get("data")
        return entries if isinstance(entries, list) else None

    def discovery_page_count(self, payload) -> int | None:
        entries = self._model_search_entries(payload)
        return len(entries) if entries is not None else None

    def parse_discovery(self, payload, **_kwargs) -> list[ModelInfo]:
        from datetime import datetime, timezone

        from .models import FREE_UNKNOWN, ModelCapabilities

        entries = self._model_search_entries(payload)
        if entries is None:
            raise ValueError("invalid Cloudflare Model Search page")
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        discovered = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("malformed Cloudflare model entry")
            model_id = entry.get("id")
            if not isinstance(model_id, str) or not model_id.strip():
                raise ValueError("Cloudflare model entry has no documented id")
            # Model Search's OpenRouter format is used only for stable IDs.
            # Pricing, capabilities, and FREE_ONLY status are not inferred.
            discovered.append(
                ModelInfo(
                    provider=self.canonical,
                    model_id=model_id.strip(),
                    capabilities=ModelCapabilities(),
                    free_status=FREE_UNKNOWN,
                    source="live:/accounts/{account_id}/ai/models/search?format=openrouter",
                    source_last_verified_at=now,
                )
            )
        return discovered

    def build(self, ctx: RequestContext, secret: str) -> NoteRequest:
        if not self.base_url_for(ctx):
            from ..structuring import StructuringError

            raise StructuringError(
                "برای Cloudflare Workers AI باید حساب و Base URL حساب‌محور تنظیم شود."
            )
        return super().build(ctx, secret)


class HuggingFaceAdapter(OpenAICompatibleAdapter):
    canonical = "huggingface"


class AlibabaAdapter(OpenAICompatibleAdapter):
    canonical = "alibaba"


class CohereAdapter(OpenAICompatibleAdapter):
    canonical = "cohere"

    def discovery_request(self, base_url, secret, *, page: int = 1):
        # The compatibility endpoint has no model list; the native v1 API has.
        return (
            "GET",
            "https://api.cohere.com/v1/models?page_size=100",
            self.auth_headers(secret),
        )

    def parse_discovery(self, payload, **_kwargs):
        results = []
        entries = payload.get("models") if isinstance(payload, dict) else None
        from datetime import datetime, timezone

        from .models import ModelInfo, infer_capabilities, infer_free_status

        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        for entry in entries or []:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            model_id = str(entry["name"]).strip()
            results.append(
                ModelInfo(
                    provider=self.canonical,
                    model_id=model_id,
                    capabilities=infer_capabilities(self.canonical, model_id),
                    free_status=infer_free_status(self.canonical, model_id),
                    source="live:/v1/models",
                    source_last_verified_at=now,
                )
            )
        return results


class CerebrasAdapter(OpenAICompatibleAdapter):
    canonical = "cerebras"
    supports_strict_schema = True


class GeminiNativeAdapter(NoteAdapter):
    """Native Gemini ``generateContent`` (structured output favoured)."""

    canonical = "gemini"
    protocol = PROTOCOL_GEMINI_NATIVE
    supports_response_format = True
    supports_strict_schema = True

    def build(self, ctx: RequestContext, secret: str) -> NoteRequest:
        if not secret:
            from ..structuring import StructuringError

            raise StructuringError("کلید NOTE_API_KEY یا GEMINI_API_KEY تنظیم نشده است.")
        base = self.base_url_for(ctx)
        model_id = quote(ctx.model.strip().removeprefix("models/"), safe="")
        generation_config: dict = {"maxOutputTokens": ctx.max_output_tokens}
        if ctx.temperature is not None and ctx.model_info.capabilities.supports_temperature:
            generation_config["temperature"] = NOTE_TEMPERATURE
        if ctx.json_strategy == STRATEGY_GEMINI_SCHEMA:
            generation_config["responseMimeType"] = "application/json"
            generation_config["responseSchema"] = gemini_compat()
        elif ctx.json_strategy in {STRATEGY_GEMINI_MIME, STRATEGY_JSON_OBJECT}:
            generation_config["responseMimeType"] = "application/json"
        contents = [
            {"role": "user", "parts": [{"text": ctx.user_text}]},
        ]
        payload: dict = {
            "contents": contents,
            "generationConfig": generation_config,
        }
        if ctx.system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": ctx.system_prompt}]}
        headers = {"x-goog-api-key": secret}
        headers.update(self._merge_extra_headers(ctx))
        return NoteRequest(
            method="POST",
            url=_endpoint(base, f"models/{model_id}:generateContent"),
            headers=headers,
            params={},
            json_body=payload,
            json_strategy=ctx.json_strategy,
        )

    def parse(self, payload: dict, *, http_status: int, headers, latency_ms: int) -> NoteResponse:
        from ..structuring import StructuringError, _sanitize_error_text

        candidates = payload.get("candidates") if isinstance(payload, dict) else None
        if not candidates:
            feedback = payload.get("promptFeedback") if isinstance(payload, dict) else None
            if isinstance(feedback, dict) and isinstance(feedback.get("blockReason"), str):
                reason = _sanitize_error_text(feedback["blockReason"], None)
                raise StructuringError(f"پاسخ سرویس تولید جزوه مسدود شد ({reason}).")
            raise ValueError("empty answer")
        finish = str(candidates[0].get("finishReason") or "")
        if finish == "MAX_TOKENS":
            raise StructuringError(
                "خروجی جزوه به سقف توکن رسید؛ متن خام برگردانده می‌شود."
            )
        try:
            parts = candidates[0]["content"]["parts"]
            text = "".join(str(part.get("text", "")) for part in parts).strip()
        except (AttributeError, KeyError, IndexError, TypeError) as exc:
            raise ValueError("contract") from exc
        if not text:
            raise ValueError("empty")
        usage_meta = payload.get("usageMetadata") if isinstance(payload, dict) else None
        usage = UsageStats()
        if isinstance(usage_meta, dict):
            usage = UsageStats(
                input_tokens=_int_or_none(usage_meta.get("promptTokenCount")),
                output_tokens=_int_or_none(usage_meta.get("candidatesTokenCount")),
                total_tokens=_int_or_none(usage_meta.get("totalTokenCount")),
            )
        return NoteResponse(
            text=text,
            provider=self.canonical,
            model=str(payload.get("modelVersion") or ""),
            usage=usage,
            finish_reason=finish,
            request_id=str(dict(headers).get("x-request-id", ""))[:64],
            latency_ms=latency_ms,
            http_status=http_status,
            quota_headers=quota_headers(headers),
        )

    def map_error(self, status, raw_body, headers, *, model, key):
        failure = super().map_error(status, raw_body, headers, model=model, key=key)
        lowered = (failure.message or "").lower()
        if status == 429 and "quota" in lowered and "per_day" in lowered:
            failure = _replace_failure(failure, category=FAIL_QUOTA, quota_exhausted=True)
        # Gemini reports paid-plan requirements with "billing" phrasing.
        if status == 400 and "billing" in lowered:
            failure = _replace_failure(failure, billing_required=True, category=FAIL_BILLING)
        return failure

    def discovery_request(self, base_url, secret, *, page: int = 1):
        return (
            "GET",
            _endpoint(base_url.rstrip("/"), "models"),
            self.auth_headers(secret),
        )


class GeminiOpenAIAdapter(OpenAICompatibleAdapter):
    """Gemini through its OpenAI-compatible endpoint (secondary path)."""

    canonical = "gemini"
    protocol = PROTOCOL_GEMINI_OPENAI
    supports_strict_schema = True

    def default_base_url(self) -> str:
        return "https://generativelanguage.googleapis.com/v1beta/openai"

    def auth_headers(self, secret: str) -> dict[str, str]:
        # The OpenAI-compatible Gemini endpoint authenticates as Bearer.
        return {"Authorization": f"Bearer {secret}"} if secret else {}


class AnthropicAdapter(NoteAdapter):
    canonical = "anthropic"
    protocol = PROTOCOL_ANTHROPIC
    supports_response_format = False

    def build(self, ctx: RequestContext, secret: str) -> NoteRequest:
        from ..structuring import StructuringError

        if not secret:
            raise StructuringError("برای Anthropic باید NOTE_API_KEY تنظیم شود.")
        base = self.base_url_for(ctx)
        headers = {"x-api-key": secret, "anthropic-version": "2023-06-01"}
        headers.update(self._merge_extra_headers(ctx))
        payload: dict = {
            "model": ctx.model,
            "max_tokens": ctx.max_output_tokens,
            "messages": [{"role": "user", "content": ctx.user_text}],
        }
        if ctx.system_prompt:
            payload["system"] = ctx.system_prompt
        payload.update(self.sampling_params(ctx))
        return NoteRequest(
            method="POST",
            url=_endpoint(base, "messages"),
            headers=headers,
            params={},
            json_body=payload,
            json_strategy=STRATEGY_PROMPT,
        )

    def parse(self, payload: dict, *, http_status: int, headers, latency_ms: int) -> NoteResponse:
        from ..structuring import StructuringError

        if payload.get("stop_reason") == "max_tokens":
            raise StructuringError(
                "خروجی جزوه به سقف توکن رسید؛ متن خام برگردانده می‌شود."
            )
        try:
            text = "".join(
                str(part.get("text", ""))
                for part in payload["content"]
                if part.get("type") == "text"
            ).strip()
        except (AttributeError, KeyError, IndexError, TypeError) as exc:
            raise ValueError("contract") from exc
        if not text:
            raise ValueError("empty")
        usage_raw = payload.get("usage") if isinstance(payload, dict) else None
        usage = UsageStats()
        if isinstance(usage_raw, dict):
            usage = UsageStats(
                input_tokens=_int_or_none(usage_raw.get("input_tokens")),
                output_tokens=_int_or_none(usage_raw.get("output_tokens")),
            )
        return NoteResponse(
            text=text,
            provider=self.canonical,
            model=str(payload.get("model") or ""),
            usage=usage,
            finish_reason=str(payload.get("stop_reason") or ""),
            request_id=str(payload.get("id") or "")[:64],
            latency_ms=latency_ms,
            http_status=http_status,
            quota_headers=quota_headers(headers),
        )


_ADAPTERS: dict[str, type[NoteAdapter]] = {
    "gemini": GeminiNativeAdapter,
    "gemini_openai": GeminiOpenAIAdapter,
    "nara": NaraAdapter,
    "groq": GroqAdapter,
    "openrouter": OpenRouterAdapter,
    "mistral": MistralAdapter,
    "sambanova": SambanovaAdapter,
    "zai": ZaiAdapter,
    "nvidia": NvidiaAdapter,
    "cloudflare": CloudflareAdapter,
    "huggingface": HuggingFaceAdapter,
    "alibaba": AlibabaAdapter,
    "cohere": CohereAdapter,
    "cerebras": CerebrasAdapter,
    "anthropic": AnthropicAdapter,
    "openai_compatible": OpenAICompatibleAdapter,
}


def adapter_for(canonical: str, settings: Settings, *, protocol_override: str | None = None) -> NoteAdapter:
    """The adapter instance for a canonical slug (generic fallback)."""
    if protocol_override == PROTOCOL_GEMINI_OPENAI:
        return GeminiOpenAIAdapter(settings)
    adapter_cls = _ADAPTERS.get(canonical, OpenAICompatibleAdapter)
    return adapter_cls(settings)


def resolve_json_strategy(
    adapter: NoteAdapter,
    model_info: ModelInfo,
    *,
    legacy_json_mode: bool = False,
    provider_slug: str | None = None,
) -> str:
    """Strongest safe structured-output mechanism for this model (spec §7).

    Order: strict JSON schema → JSON schema → Gemini native schema → Gemini
    JSON mime → JSON object → prompt-enforced JSON. Model capabilities always
    win over provider-wide toggles; ``NOTE_API_JSON_MODE`` only applies to the
    generic legacy gateway as a backward-compatible hint.
    """
    canonical = provider_slug or adapter.canonical
    caps = model_info.capabilities
    if adapter.protocol == PROTOCOL_GEMINI_NATIVE:
        if caps.supports_json_schema or caps.supports_strict_json_schema:
            return STRATEGY_GEMINI_SCHEMA
        return STRATEGY_GEMINI_MIME  # responseMimeType json: the existing default
    if adapter.protocol == PROTOCOL_ANTHROPIC:
        return STRATEGY_PROMPT
    if adapter.protocol == PROTOCOL_GEMINI_OPENAI:
        if caps.supports_strict_json_schema:
            return STRATEGY_STRICT_SCHEMA
        if caps.supports_json_schema:
            return STRATEGY_JSON_SCHEMA
        if caps.supports_json_object:
            return STRATEGY_JSON_OBJECT
        return STRATEGY_PROMPT
    # OpenAI-family transports.
    if not adapter.supports_response_format:
        return STRATEGY_PROMPT
    if caps.supports_strict_json_schema and adapter.supports_strict_schema:
        return STRATEGY_STRICT_SCHEMA
    if caps.supports_json_schema:
        return STRATEGY_JSON_SCHEMA
    if caps.supports_json_object:
        return STRATEGY_JSON_OBJECT
    if legacy_json_mode and canonical in {"openai_compatible", "openrouter"}:
        return STRATEGY_JSON_OBJECT
    return STRATEGY_PROMPT


def _replace_failure(failure: NoteFailure, **changes) -> NoteFailure:
    """slots-dataclass copy with attribute overrides."""
    import dataclasses

    return dataclasses.replace(failure, **changes)


def _int_or_none(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None
