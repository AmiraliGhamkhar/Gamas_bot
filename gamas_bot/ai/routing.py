"""Provider routing: task-aware order, FREE_ONLY filtering, provider failover.

Sits *above* the credential pool (rotation) and *below* the note pipeline:

* a **route** is an ordered list of providers per task type, stored in SQLite
  (``ai_provider_routes``) and seeded from the environment/defaults;
* each leg is filtered for FREE_ONLY eligibility (provider classification +
  model free status + quota ledger) before it is even tried;
* failures normalize through the adapter and drive the right action:
  credential cooldown/quarantine/rotation, one structured-output downgrade,
  then failover to the next provider leg;
* every attempt and every transition is accounted (usage rows + events).

The module never sees a raw prompt or answer beyond the length statistics it
records; it raises existing :class:`StructuringError` subclasses so the note
pipeline (QA/repair/compilation) behaves exactly as before.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

import aiohttp

from ..config import Settings
from ..provider_credentials import (
    ProviderCredential,
    ProviderCredentialManager,
)
from ..structuring import (
    ProviderHTTPError,
    ProviderTransientError,
    StructuringError,
)
from . import tokens as token_budget
from .adapters import (
    FAIL_AUTH,
    FAIL_BAD_REQUEST,
    FAIL_CONTRACT,
    FAIL_MODEL,
    FAIL_QUOTA,
    FAIL_RATE_LIMITED,
    FAIL_TRANSIENT,
    NoteAdapter,
    NoteFailure,
    NoteResponse,
    RequestContext,
    STRATEGY_PROMPT,
    adapter_for,
    resolve_json_strategy,
)
from .models import FREE_PROMOTIONAL, ModelInfo, ModelRegistry, PAID, fallback_free_model
from .profiles import NoteProviderProfile, profile_for
from .registry import (
    ProviderClass,
    classification_of,
    free_only_generation_allowed,
    registry_info,
    resolve_canonical,
)
from .usage import AIUsageTracker, sanitize_text

logger = logging.getLogger(__name__)

#: Task types with their own route row (spec §23).
TASK_OUTLINE = "outline"
TASK_CHUNK = "chunk_structuring"
TASK_REPAIR = "repair"
TASK_COMPILE = "final_compilation"
TASK_HEALTH = "health_test"
TASK_BENCHMARK = "benchmark"
ROUTE_TASKS = (TASK_CHUNK, TASK_OUTLINE, TASK_REPAIR, TASK_COMPILE, TASK_HEALTH, TASK_BENCHMARK)

#: Default note route order (spec §23 example); experimental/trial providers
#: are deliberately absent — they only enter routes by explicit admin action.
DEFAULT_NOTE_ORDER = (
    "gemini", "nara", "groq", "openrouter", "mistral", "sambanova",
    "zai", "nvidia", "cloudflare",
)

#: Providers an environment-only legacy deployment keeps first when seeding.
_SEED_FROM_ENV_SLUGS = {"gemini", "openai_compatible", "anthropic"}

#: Error message texts must stay user-facing Persian and content-free.
_ALL_FAILED_FA = "هیچ‌کدام از سرویس‌های تولید جزوه پاسخ مناسبی نداد."


@dataclass(frozen=True, slots=True)
class RouteLeg:
    provider: str          # credential-pool slug
    canonical: str
    model: str | None = None
    free_only: bool = True
    index: int = 0


@dataclass(slots=True)
class PlannedRoute:
    """The eligible provider legs (+ pipeline policy) for one note job."""

    legs: list[RouteLeg]
    profile: NoteProviderProfile
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def primary(self) -> RouteLeg | None:
        return self.legs[0] if self.legs else None

    @property
    def outline_enabled(self) -> bool:
        return self.profile.outline_enabled

    @property
    def repair_enabled(self) -> bool:
        return self.profile.repair_enabled

    @property
    def compile_enabled(self) -> bool:
        return self.profile.final_compile_enabled


class AllProvidersFailedError(StructuringError):
    """Every eligible provider leg failed (spec §45)."""

    def __init__(self, message: str, *, failures: list[NoteFailure]):
        super().__init__(message)
        self.failures = list(failures)
        self.error_class = failures[-1].category if failures else "unavailable"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class ProviderRouter:
    """Resolves routes, filters FREE_ONLY, executes with failover."""

    def __init__(
        self,
        db,
        settings: Settings,
        credentials: ProviderCredentialManager,
        tracker: AIUsageTracker,
        model_registry: ModelRegistry,
    ):
        self.db = db
        self.settings = settings
        self.credentials = credentials
        self.tracker = tracker
        self.models = model_registry
        self._provider_settings_cache: dict | None = None

    # -- configuration ------------------------------------------------------

    async def provider_settings(self) -> dict:
        if self._provider_settings_cache is None:
            try:
                self._provider_settings_cache = await self.db.ai_provider_settings_all()
            except Exception:
                self._provider_settings_cache = {}
        return self._provider_settings_cache

    def invalidate_cache(self) -> None:
        self._provider_settings_cache = None

    async def ensure_seeded(self) -> None:
        """Seed route rows once. Never overwrites an admin-configured route."""
        for task in ROUTE_TASKS:
            existing = await self._safe_routes("notes", task)
            if existing is not None and existing:
                continue
            order = self._seed_order(task)
            await self.db.ai_routes_replace(
                "notes",
                task,
                [
                    {"provider": slug, "enabled": 1, "free_only": 1}
                    for slug in order
                ],
                admin_id=None,
            )

    def _seed_order(self, task: str) -> list[str]:
        """Seeded order: the env-configured provider first, defaults after.

        A legacy deployment (e.g. NOTE_API_PROVIDER=openai_compatible +
        NaraRouter base URL) keeps its provider as route position zero, so
        enabling the platform changes billing safety, never which provider
        serves production traffic.
        """
        if task == TASK_BENCHMARK:
            order: list[str] = list(DEFAULT_NOTE_ORDER) + ["cerebras", "cohere", "huggingface"]
        else:
            order = list(DEFAULT_NOTE_ORDER)
        env_provider = (self.settings.note_api_provider or "").strip().lower()
        if env_provider in _SEED_FROM_ENV_SLUGS and (
            self.settings.effective_note_api_key or self.settings.note_api_base_url
        ):
            canonical = resolve_canonical(env_provider, self.settings.note_api_base_url)
            if canonical in order:
                order.remove(canonical)
            order.insert(0, canonical)
        override = (getattr(self.settings, "ai_default_note_route", "") or "").strip()
        if task == TASK_CHUNK and override:
            explicit = [s.strip().lower() for s in override.split(",") if s.strip()]
            if explicit:
                merged = [s for s in explicit if s in order]
                merged += [s for s in order if s not in merged]
                order = merged
        return order

    async def _safe_routes(self, service: str, task: str) -> list[dict] | None:
        try:
            return await self.db.ai_routes_list(service, task)
        except Exception:
            logger.warning("Could not read AI routes task=%s", task)
            return None

    async def route(self, task: str = TASK_CHUNK) -> list[RouteLeg]:
        rows = await self._safe_routes("notes", task)
        if not rows and task != TASK_CHUNK:
            rows = await self._safe_routes("notes", TASK_CHUNK)
        if not rows:
            rows = [
                {"provider": slug, "enabled": 1, "free_only": 1, "model": None}
                for slug in self._seed_order(task)
            ]
        legs: list[RouteLeg] = []
        for index, row in enumerate(rows):
            slug = str(row.get("provider") or "").strip().lower()
            if slug not in self._known_slugs():
                continue
            canonical = resolve_canonical(slug, self._slug_base_url(slug, row.get("model")))
            legs.append(
                RouteLeg(
                    provider=slug,
                    canonical=canonical,
                    model=row.get("model"),
                    free_only=bool(row.get("free_only", 1)) and bool(row.get("enabled", 1)),
                    index=index,
                )
            )
        return legs

    def _known_slugs(self) -> frozenset:
        from .registry import PROVIDER_CHOICES_NOTES

        return PROVIDER_CHOICES_NOTES

    def _slug_base_url(self, slug: str, model: str | None) -> str | None:
        if slug == (self.settings.note_api_provider or "").lower():
            return self.settings.note_api_base_url
        return None

    # -- FREE_ONLY planning ---------------------------------------------------

    async def plan(self, task: str = TASK_CHUNK) -> PlannedRoute:
        """Eligible legs after FREE_ONLY/quota/admin filtering."""
        settings = await self.provider_settings()
        legs = await self.route(task)
        free_only_mode = bool(getattr(self.settings, "ai_free_only", True))
        allow_paid = bool(getattr(self.settings, "ai_allow_paid_fallback", False))
        skipped: list[tuple[str, str]] = []
        eligible: list[RouteLeg] = []
        for leg in legs:
            info = registry_info(leg.canonical)
            stored = settings.get(leg.canonical, {})
            if info.experimental_only and not stored.get("experimental_unlocked"):
                skipped.append((leg.canonical, "experimental_locked"))
                continue
            if info.requires_explicit_enable and not stored.get("experimental_unlocked"):
                skipped.append((leg.canonical, "region_restricted_locked"))
                continue
            if stored.get("enabled") == 0:
                skipped.append((leg.canonical, "disabled"))
                continue
            if info.classification in {ProviderClass.UNAVAILABLE}:
                skipped.append((leg.canonical, "unavailable"))
                continue
            if free_only_mode and leg.free_only:
                trial = info.classification is ProviderClass.TRIAL_ONLY
                paid = info.classification is ProviderClass.PAID_ONLY
                blocked_flag = bool(stored.get("free_only_blocked"))
                if trial or paid or blocked_flag:
                    skipped.append((leg.canonical, "billing_blocked"))
                    continue
                if not free_only_generation_allowed(
                    leg.canonical, admin_enabled=bool(stored.get("experimental_unlocked"))
                ):
                    skipped.append((leg.canonical, "not_free_eligible"))
                    continue
                if await self._daily_quota_exhausted(leg.canonical):
                    skipped.append((leg.canonical, "quota_exhausted"))
                    continue
            elif free_only_mode and not leg.free_only and not allow_paid:
                skipped.append((leg.canonical, "paid_fallback_disabled"))
                continue
            eligible.append(leg)
        # Paid-fallback legs are strictly a last resort: every free leg is
        # tried before any paid one (stable partition keeps route order
        # inside each class). This is an invariant over the whole plan, so
        # administrators cannot accidentally promote paid traffic above
        # free providers by reordering routes.
        if free_only_mode:
            eligible = [lg for lg in eligible if lg.free_only] + [
                lg for lg in eligible if not lg.free_only
            ]
        profile = (
            profile_for(eligible[0].canonical)
            if eligible
            else profile_for(legs[0].canonical if legs else "openai_compatible")
        )
        return PlannedRoute(legs=eligible, profile=profile, skipped=skipped)

    async def _daily_quota_exhausted(self, canonical: str) -> bool:
        # A live entitlement probe (OpenRouter GET /api/v1/key) is the
        # authoritative counter and wins over the conservative static default
        # — e.g. an account with purchased credits gets 1000/day, not 50.
        try:
            live = await self.db.ai_quota_live_remaining(canonical)
        except Exception:
            live = None
        if live is not None:
            if live <= 0:
                await self.tracker.event(
                    "provider_quota_warning",
                    service="notes",
                    provider=canonical,
                    request_type=TASK_CHUNK,
                    detail=f"live free-daily remaining={live}",
                )
            return live <= 0
        profile = profile_for(canonical)
        if not profile.daily_request_limit:
            return False
        try:
            used = await self.tracker.today_request_count(canonical)
        except Exception:
            return False
        if used >= profile.daily_request_limit:
            return True
        return False

    # -- credential targets -----------------------------------------------------

    async def credentials_for(self, leg: RouteLeg) -> list[ProviderCredential]:
        """Ready credentials for a leg: exact pool, legacy host match, env fallback."""
        found: list[ProviderCredential] = []
        seen_ids: set[tuple] = set()
        pools: list[str] = [leg.provider]
        env_provider = (self.settings.note_api_provider or "").strip().lower()
        if (
            leg.canonical == resolve_canonical(env_provider, self.settings.note_api_base_url)
            and env_provider in _SEED_FROM_ENV_SLUGS
        ):
            if env_provider not in pools:
                pools.append(env_provider)
        elif leg.canonical != "openai_compatible" and "openai_compatible" not in pools:
            # Gateway rows stored under the generic slug may host-match.
            pools.append("openai_compatible")
        for pool in pools:
            fallback_secret = None
            fallback_base = None
            fallback_model = None
            # The single env credential attaches to the leg whose canonical
            # provider the environment actually configures (e.g. a NaraRouter
            # deployment via NOTE_API_PROVIDER=openai_compatible serves the
            # nara leg, and ONLY that leg).
            if (
                pool == env_provider
                and resolve_canonical(pool, self.settings.note_api_base_url) == leg.canonical
            ):
                fallback_secret = self.settings.effective_note_api_key
                fallback_base = self.settings.note_api_base_url
                fallback_model = self.settings.effective_note_model
            try:
                candidates = await self.credentials.candidates(
                    "notes",
                    pool,
                    fallback_secret=fallback_secret,
                    fallback_base_url=fallback_base,
                    fallback_model=fallback_model,
                )
            except Exception:
                logger.warning("Credential pool unavailable provider=%s", pool)
                continue
            for candidate in candidates:
                key = (candidate.id, candidate.provider)
                if key in seen_ids:
                    continue
                if resolve_canonical(pool, candidate.base_url) != leg.canonical:
                    continue
                if not self._credential_allowed_for_leg(leg, candidate):
                    continue
                seen_ids.add(key)
                found.append(candidate)
                if len(found) >= 5:
                    return found
        return found

    @staticmethod
    def _credential_allowed_for_leg(leg: RouteLeg, credential: ProviderCredential) -> bool:
        """Admin billing flags on a key gate which legs it may serve.

        * FREE_ONLY leg: a key explicitly marked paid (``free_only=0``) is
          skipped — the admin stated this key bills money.
        * paid-fallback leg (only reachable with AI_ALLOW_PAID_FALLBACK): a
          key explicitly marked ``paid_allowed=0`` is skipped.
        Unmarked keys (NULL) are allowed everywhere, preserving the legacy
        behaviour for existing deployments.
        """
        if leg.free_only and credential.free_only == 0:
            return False
        if not leg.free_only and credential.paid_allowed == 0:
            return False
        return True

    def _model_for(self, leg: RouteLeg, credential: ProviderCredential, profile: NoteProviderProfile) -> str:
        if leg.model:
            return leg.model
        if credential.model:
            return credential.model
        env_provider = (self.settings.note_api_provider or "").strip().lower()
        if resolve_canonical(env_provider, self.settings.note_api_base_url) == leg.canonical:
            env_model = self.settings.effective_note_model
            if env_model and env_model != "gpt-4o-mini":
                return env_model
        return fallback_free_model(leg.canonical) or self._default_model(leg.canonical)

    @staticmethod
    def _default_model(canonical: str) -> str:
        from .models import default_model_for

        model = default_model_for(canonical)
        if model:
            return model
        return {
            "gemini": "gemini-2.5-flash",
            "openai_compatible": "gpt-4o-mini",
            "anthropic": "claude-haiku-4-5",
        }.get(canonical, "gpt-4o-mini")

    # -- execution ---------------------------------------------------------------

    async def execute(
        self,
        *,
        task: str,
        leg: RouteLeg,
        plan: PlannedRoute,
        system_prompt: str,
        user_text: str,
        session,
        position: int,
        job_id: str,
        submission_id: int | None,
        failure_log: list[NoteFailure],
    ) -> NoteResponse:
        """Run one leg over its credential pool; raise on leg failure."""
        profile = profile_for(leg.canonical)
        adapter = adapter_for(leg.canonical, self.settings)
        base_url = self._base_url_for(leg)
        if task == TASK_HEALTH:
            profile = profile.with_policy(replace(profile.policy, max_retries=0))
        pool = await self.credentials_for(leg)
        if not pool:
            failure = NoteFailure(
                category=FAIL_AUTH,
                provider=leg.canonical,
                model="",
                credential_invalid=True,
                message="no configured credential",
            )
            failure_log.append(failure)
            await self.tracker.event(
                "provider_model_unavailable",
                provider=leg.provider,
                canonical=leg.canonical,
                request_type=task,
                detail="no eligible credential",
                job_id=job_id,
                service="notes",
            )
            raise _LegFailed(failure)
        last_failure: NoteFailure | None = None
        downgraded = False
        for credential in pool:
            model = self._model_for(leg, credential, profile)
            model_info = await self.models.resolve(leg.canonical, model)
            blocked = self._model_blocked(leg, model_info)
            if blocked:
                await self.tracker.event(
                    "provider_model_unavailable",
                    provider=leg.provider,
                    canonical=leg.canonical,
                    model=model,
                    request_type=task,
                    detail=blocked,
                    job_id=job_id,
                    service="notes",
                )
                last_failure = NoteFailure(
                    category=FAIL_MODEL,
                    provider=leg.canonical,
                    model=model,
                    model_unavailable=True,
                    message=blocked,
                )
                failure_log.append(last_failure)
                continue
            strategy = self._strategy_for(adapter, model_info, leg)
            if downgraded:
                strategy = STRATEGY_PROMPT if adapter.protocol != "gemini_native" else strategy
            ctx = RequestContext(
                service="notes",
                request_type=task,
                provider=leg.provider,
                canonical=leg.canonical,
                base_url=credential.base_url or base_url,
                model=model,
                system_prompt=system_prompt,
                user_text=user_text,
                max_output_tokens=self._output_budget(profile, model_info),
                model_info=model_info,
                json_strategy=strategy,
                temperature=None
                if profile.temperature_policy == "omit"
                else self._temperature_for(profile),
                reasoning_policy=profile.reasoning_policy,
                extra_headers=self.settings.note_api_extra_headers,
                job_id=job_id,
                submission_id=submission_id,
                route_position=position,
            )
            try:
                response = await self._execute_with_retries(
                    adapter=adapter, ctx=ctx, credential=credential,
                    profile=profile, session=session, position=position,
                )
                return response
            except _DowngradeRequested:
                # One structured-output downgrade: schema → plain prompt JSON.
                downgraded = True
                ctx = replace(ctx, json_strategy=STRATEGY_PROMPT
                              if adapter.protocol != "gemini_native" else "gemini_mime")
                try:
                    response = await self._execute_with_retries(
                        adapter=adapter, ctx=ctx, credential=credential,
                        profile=profile, session=session, position=position,
                    )
                    return response
                except NoteFailure as exc:
                    last_failure = exc
            except NoteFailure as exc:
                last_failure = exc
            assert last_failure is not None
            failure_log.append(last_failure)
            await self._apply_credential_state(credential, last_failure, job_id)
            if last_failure.category == FAIL_BAD_REQUEST and not downgraded:
                continue  # try the same provider with a fresh credential? -> rotate
        if last_failure is None:
            last_failure = NoteFailure(category=FAIL_MODEL, provider=leg.canonical, model="", model_unavailable=True, message="no usable model")
            failure_log.append(last_failure)
        raise _LegFailed(last_failure)

    async def generation_test(
        self,
        *,
        credential: ProviderCredential,
        canonical: str,
        model: str | None = None,
    ) -> dict:
        """Explicit, billable single-shot generation test (admin action only).

        Distinct from the read-only health probes: one real completion with a
        tiny budget, accounted like traffic, and logged with
        ``check_type='generation'`` (spec §37).
        """
        adapter = adapter_for(canonical, self.settings)
        profile = profile_for(canonical)
        leg = RouteLeg(provider=credential.provider, canonical=canonical, model=model)
        model_id = model or self._model_for(leg, credential, profile)
        model_info = await self.models.resolve(canonical, model_id)
        strategy = "prompt"
        ctx = RequestContext(
            service="notes",
            request_type="health_test",
            provider=credential.provider,
            canonical=canonical,
            base_url=credential.base_url or self._base_url_for_slug(canonical),
            model=model_id,
            system_prompt="",
            user_text="با یک کلمهٔ فارسی پاسخ بده: سلام",
            max_output_tokens=min(16, self._output_budget(profile, model_info)),
            model_info=model_info,
            json_strategy=strategy,
            temperature=None,
        )
        started = time.perf_counter()
        result: dict = {"ok": False, "http_status": None, "latency_ms": 0}
        try:
            request = adapter.build(ctx, credential.secret)
            timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    request.url, headers=request.headers, json=request.json_body
                ) as response:
                    status = int(response.status)
                    result["http_status"] = status
                    if 200 <= status < 300:
                        payload = await response.json(content_type=None)
                        parsed = adapter.parse(
                            payload,
                            http_status=status,
                            headers=response.headers,
                            latency_ms=int((time.perf_counter() - started) * 1000),
                        )
                        result.update(
                            ok=True,
                            request_id=parsed.request_id,
                            finish_reason=parsed.finish_reason,
                            input_tokens=parsed.usage.input_tokens,
                            output_tokens=parsed.usage.output_tokens,
                        )
                    else:
                        body = await response.read()
                        failure = adapter.map_error(
                            status, body, response.headers, model=model_id,
                            key=credential.secret or None,
                        )
                        result["error_class"] = failure.category
                        result["detail"] = sanitize_text(failure.message, (credential.secret,))
        except Exception as exc:
            result["error_class"] = type(exc).__name__
            result["detail"] = sanitize_text(type(exc).__name__, (credential.secret,))
        result["latency_ms"] = int((time.perf_counter() - started) * 1000)
        await self.tracker.event(
            "generation_test",
            service="notes",
            provider=credential.provider,
            canonical=canonical,
            model=model_id,
            request_type="health_test",
            http_status=result.get("http_status"),
            latency_ms=result["latency_ms"],
            error_class=result.get("error_class"),
            detail=result.get("detail", ""),
            check_type="generation",
        )
        await self.tracker.record(
            {
                "service": "notes",
                "provider": credential.provider,
                "canonical": canonical,
                "credential_id": credential.id,
                "credential_label": credential.label,
                "model": model_id,
                "request_type": "health_test",
                "route_position": 0,
                "attempt": 1,
                "latency_ms": result["latency_ms"],
                "http_status": result.get("http_status"),
                "actual_input_tokens": result.get("input_tokens"),
                "actual_output_tokens": result.get("output_tokens"),
                "json_strategy": strategy,
                "result": "success" if result["ok"] else "failure",
                "error_class": result.get("error_class"),
                "request_id": (result.get("request_id") or "")[:64],
                "free_class": self._free_class(ctx),
            }
        )
        result["model"] = model_id
        return result

    def _model_blocked(self, leg: RouteLeg, info: ModelInfo) -> str | None:
        free_only = bool(getattr(self.settings, "ai_free_only", True)) and leg.free_only
        if info.deprecated or not info.available:
            return "model unavailable/deprecated"
        if info.capabilities.requires_paid_billing and free_only:
            return "model requires paid billing (FREE_ONLY)"
        if not info.commercial_use_allowed:
            return "model terms disallow this use"
        if free_only:
            if info.free_status == PAID:
                return "model is paid (FREE_ONLY)"
            if info.free_status == FREE_PROMOTIONAL and not info.free_now():
                return "promotional free period expired"
        return None

    def _strategy_for(self, adapter: NoteAdapter, model_info: ModelInfo, leg: RouteLeg) -> str:
        return resolve_json_strategy(
            adapter,
            model_info,
            legacy_json_mode=bool(self.settings.note_api_json_mode),
            provider_slug=leg.canonical,
        )

    def _base_url_for(self, leg: RouteLeg) -> str:
        return self._base_url_for_slug(leg.canonical)

    def _base_url_for_slug(self, canonical: str) -> str:
        env_provider = (self.settings.note_api_provider or "").strip().lower()
        if resolve_canonical(env_provider, self.settings.note_api_base_url) == canonical:
            if self.settings.note_api_base_url:
                return self.settings.note_api_base_url
        return registry_info(canonical).base_url or ""

    @staticmethod
    def _temperature_for(profile: NoteProviderProfile) -> float | None:
        if profile.temperature_policy == "model_default":
            return None
        return 0.2

    @staticmethod
    def _output_budget(profile: NoteProviderProfile, model: ModelInfo) -> int:
        budget = profile.max_output_tokens
        if model.max_output_tokens:
            budget = min(budget, model.max_output_tokens)
        return budget

    async def _execute_with_retries(
        self,
        *,
        adapter: NoteAdapter,
        ctx: RequestContext,
        credential: ProviderCredential,
        profile: NoteProviderProfile,
        session,
        position: int,
    ) -> NoteResponse:
        policy = profile.policy
        retries = policy.max_retries
        override = getattr(self.settings, "ai_max_generation_retries", -1)
        if override is not None and int(override) >= 0:
            retries = min(retries, int(override))
        attempts = retries + 1
        estimated = token_budget.estimate_tokens(ctx.system_prompt) + token_budget.estimate_tokens(
            ctx.user_text
        )
        last: NoteFailure | None = None
        for attempt in range(1, attempts + 1):
            request_ctx = replace(ctx, attempt=attempt)
            started = time.perf_counter()
            if attempt == 1:
                await self.tracker.event(
                    "note_request_started",
                    service="notes",
                    provider=ctx.provider,
                    canonical=ctx.canonical,
                    model=ctx.model,
                    request_type=ctx.request_type,
                    route_position=position,
                    attempt=attempt,
                    estimated_input_tokens=estimated,
                    input_chars=ctx.input_chars,
                    json_strategy=ctx.json_strategy,
                    job_id=ctx.job_id,
                    level="info",
                )
            try:
                request = adapter.build(request_ctx, credential.secret)
            except StructuringError as exc:
                raise NoteFailure(
                    category=FAIL_CONTRACT,
                    provider=ctx.canonical,
                    model=ctx.model,
                    message="request build failed (configuration)",
                ) from exc
            status = None
            try:
                timeout = aiohttp.ClientTimeout(
                    total=policy.timeout_seconds,
                    connect=min(30, policy.timeout_seconds),
                    sock_read=policy.timeout_seconds,
                )
                async with session.post(
                    request.url,
                    headers=request.headers,
                    params=request.params or None,
                    json=request.json_body,
                    timeout=timeout,
                ) as response:
                    status = int(response.status)
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    if 200 <= status < 300:
                        payload = await response.json(content_type=None)
                        try:
                            parsed = adapter.parse(
                                payload,
                                http_status=status,
                                headers=response.headers,
                                latency_ms=latency_ms,
                            )
                        except ValueError as exc:
                            raise NoteFailure(
                                category=FAIL_CONTRACT,
                                provider=ctx.canonical,
                                model=ctx.model,
                                http_status=status,
                                retryable=False,
                                message=str(exc)[:120],
                            ) from exc
                        await self._record_attempt(
                            ctx=request_ctx, credential=credential, result="success",
                            latency_ms=latency_ms, http_status=status,
                            estimated_tokens=estimated, parsed=parsed, position=position,
                        )
                        await self.credentials.record_result(
                            credential, result="success", status_code=status
                        )
                        await self.tracker.event(
                            "note_request_succeeded",
                            service="notes",
                            provider=ctx.provider,
                            canonical=ctx.canonical,
                            model=ctx.model,
                            request_type=ctx.request_type,
                            route_position=position,
                            attempt=attempt,
                            http_status=status,
                            latency_ms=latency_ms,
                            json_strategy=ctx.json_strategy,
                            job_id=ctx.job_id,
                        )
                        await self.tracker.quota_snapshot(
                            ctx.canonical, credential.id, ctx.model, parsed.quota_headers
                        )
                        return parsed
                    raw = await response.read()
                    failure = adapter.map_error(
                        status, raw, response.headers, model=ctx.model,
                        key=credential.secret or None,
                    )
                    failure = dataclasses.replace(
                        failure,
                        message=sanitize_text(failure.message, (credential.secret,)),
                    )
                    delay = self._retry_delay(policy, failure, attempt)
                    will_retry = (
                        failure.retryable
                        and attempt < attempts
                        and (failure.category != FAIL_RATE_LIMITED or policy.retry_on_rate_limit)
                    )
                    await self._record_attempt(
                        ctx=request_ctx, credential=credential, result="failure",
                        latency_ms=latency_ms, http_status=status,
                        estimated_tokens=estimated, parsed=None, position=position,
                        failure=failure,
                    )
                    if failure.category == FAIL_BAD_REQUEST and self._json_incompatible(failure, ctx):
                        raise _DowngradeRequested()
                    if will_retry:
                        await self.tracker.event(
                            "note_request_retry",
                            service="notes",
                            provider=ctx.provider,
                            canonical=ctx.canonical,
                            model=ctx.model,
                            request_type=ctx.request_type,
                            route_position=position,
                            attempt=attempt,
                            http_status=status,
                            latency_ms=latency_ms,
                            error_class=failure.category,
                            detail=failure.message,
                            job_id=ctx.job_id,
                        )
                        await asyncio.sleep(delay)
                        last = failure
                        continue
                    last = failure
                    break
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                latency_ms = int((time.perf_counter() - started) * 1000)
                failure = NoteFailure(
                    category=FAIL_TRANSIENT,
                    provider=ctx.canonical,
                    model=ctx.model,
                    retryable=True,
                    message=type(exc).__name__,
                )
                await self._record_attempt(
                    ctx=request_ctx, credential=credential, result="failure",
                    latency_ms=latency_ms, http_status=status,
                    estimated_tokens=estimated, parsed=None, position=position,
                    failure=failure,
                )
                if attempt < attempts:
                    delay = min(policy.backoff_base_seconds ** attempt, policy.backoff_cap_seconds)
                    await self.tracker.event(
                        "note_request_retry",
                        service="notes",
                        provider=ctx.provider,
                        canonical=ctx.canonical,
                        model=ctx.model,
                        request_type=ctx.request_type,
                        route_position=position,
                        attempt=attempt,
                        error_class=FAIL_TRANSIENT,
                        detail=type(exc).__name__,
                        job_id=ctx.job_id,
                    )
                    await asyncio.sleep(delay)
                    last = failure
                    continue
                last = failure
                break
        assert last is not None
        raise last

    def _json_incompatible(self, failure: NoteFailure, ctx: RequestContext) -> bool:
        if ctx.json_strategy in {STRATEGY_PROMPT, "gemini_mime"}:
            return False
        text = (failure.message or "").lower()
        return any(
            needle in text
            for needle in ("response_format", "json_schema", "json mode", "invalid parameter",
                            "unsupported", "schema")
        )

    @staticmethod
    def _retry_delay(policy, failure: NoteFailure, attempt: int) -> float:
        if failure.retry_after is not None:
            return min(max(failure.retry_after, 0.25), policy.retry_after_cap_seconds)
        return min(policy.backoff_base_seconds ** attempt, policy.backoff_cap_seconds)

    async def _record_attempt(
        self,
        *,
        ctx: RequestContext,
        credential: ProviderCredential,
        result: str,
        latency_ms: int,
        http_status: int | None,
        estimated_tokens: int,
        parsed: NoteResponse | None,
        position: int,
        failure: NoteFailure | None = None,
    ) -> None:
        usage = parsed.usage if parsed else None
        row = {
            "service": "notes",
            "provider": ctx.provider,
            "canonical": ctx.canonical,
            "credential_id": credential.id,
            "credential_label": credential.label,
            "model": ctx.model,
            "request_type": ctx.request_type,
            "route_position": position,
            "attempt": ctx.attempt,
            "job_id": ctx.job_id,
            "submission_id": ctx.submission_id,
            "latency_ms": latency_ms,
            "http_status": http_status,
            "estimated_input_tokens": estimated_tokens,
            "actual_input_tokens": usage.input_tokens if usage else None,
            "actual_output_tokens": usage.output_tokens if usage else None,
            "total_tokens": usage.total_tokens if usage else None,
            "finish_reason": parsed.finish_reason if parsed else "",
            "retry_after_seconds": failure.retry_after if failure else None,
            "quota_headers_json": None,
            "json_strategy": ctx.json_strategy,
            "result": result,
            "error_class": failure.category if failure else None,
            "free_class": self._free_class(ctx),
            "request_id": (parsed.request_id if parsed else "")[:64],
        }
        if parsed is not None and parsed.quota_headers:
            import json as _json

            row["quota_headers_json"] = _json.dumps(parsed.quota_headers, sort_keys=True)[:500]
        await self.tracker.record(row)

    def _free_class(self, ctx: RequestContext) -> str:
        classification = classification_of(ctx.canonical)
        if classification in {ProviderClass.PERMANENT_FREE, ProviderClass.FREE_PLAN}:
            return "free"
        if classification is ProviderClass.PROMOTIONAL_FREE:
            return "free"
        return "unknown"

    async def _apply_credential_state(
        self, credential: ProviderCredential, failure: NoteFailure, job_id: str
    ) -> None:
        """Feed failures into the existing cooldown/quarantine machinery."""
        try:
            if failure.category == FAIL_AUTH:
                await self.credentials.record_result(
                    credential,
                    result="quarantined",
                    status_code=failure.http_status,
                    safe_error=f"HTTP {failure.http_status}",
                )
                await self.tracker.event(
                    "credential_quarantined",
                    service="notes",
                    provider=credential.provider,
                    detail=f"credential #{credential.id or 'env'}",
                    job_id=job_id,
                )
            elif failure.category in {FAIL_RATE_LIMITED, FAIL_QUOTA}:
                await self.credentials.record_result(
                    credential,
                    result="cooldown",
                    status_code=failure.http_status or 429,
                    retry_after_seconds=failure.retry_after,
                    safe_error="HTTP 429",
                )
                await self.tracker.event(
                    "credential_cooldown",
                    service="notes",
                    provider=credential.provider,
                    detail=f"credential #{credential.id or 'env'}",
                    job_id=job_id,
                )
            else:
                await self.credentials.record_result(
                    credential,
                    result="error",
                    status_code=failure.http_status,
                    safe_error=failure.category,
                )
        except Exception:
            logger.warning("Could not persist credential state provider=%s", credential.provider)


class _DowngradeRequested(Exception):
    pass


class _LegFailed(Exception):
    def __init__(self, failure: NoteFailure):
        super().__init__(failure.category)
        self.failure = failure


# ---------------------------------------------------------------------------
# Job session: the object structuring.py talks to
# ---------------------------------------------------------------------------


class NoteJobSession:
    """One note job's provider state: plan, budget policy, HTTP session."""

    def __init__(
        self,
        router: ProviderRouter,
        plan: PlannedRoute,
        settings: Settings,
        *,
        job_id: str = "",
        submission_id: int | None = None,
    ):
        self.router = router
        self.plan = plan
        self.settings = settings
        self.job_id = job_id
        self.submission_id = submission_id
        self.failures: list[NoteFailure] = []
        self.fallbacks = 0

    # -- budgeting ----------------------------------------------------------

    def chunk_chars(self, *, safety_margin: float | None = None) -> int:
        """Effective character chunk budget from the primary provider's tokens."""
        profile = self.plan.profile
        margin = (
            float(self.settings.ai_quota_safety_margin)
            if safety_margin is None
            else safety_margin
        )
        return token_budget.chunk_char_budget(
            profile.chunk_token_budget,
            char_cap=profile.chunk_char_cap,
            overhead_tokens=0,
            safety_margin=margin,
        )

    # -- generation -----------------------------------------------------------

    async def generate(
        self,
        *,
        request_type: str,
        task: str,
        system_prompt: str,
        user_text: str,
        session,
    ) -> str:
        """Route one pipeline call through the plan with provider failover."""
        plan_legs = self.plan.legs
        if not plan_legs:
            raise AllProvidersFailedError(_ALL_FAILED_FA, failures=self.failures)
        max_failovers = max(0, int(getattr(self.settings, "ai_max_provider_failovers", 3)))
        legs = plan_legs[: max_failovers + 1]
        last_error: Exception | None = None
        for position, leg in enumerate(legs):
            if position > 0:
                self.fallbacks += 1
                await self.router.tracker.event(
                    "note_request_fallback",
                    service="notes",
                    provider=leg.provider,
                    canonical=leg.canonical,
                    request_type=task,
                    route_position=position,
                    job_id=self.job_id,
                    detail=self.failures[-1].category if self.failures else "",
                )
                await self.router.tracker.event(
                    "provider_route_changed",
                    service="notes",
                    provider=leg.provider,
                    canonical=leg.canonical,
                    request_type=task,
                    route_position=position,
                    job_id=self.job_id,
                    level="info",
                )
            try:
                response = await self.router.execute(
                    task=task or request_type,
                    leg=leg,
                    plan=self.plan,
                    system_prompt=system_prompt,
                    user_text=user_text,
                    session=session,
                    position=position,
                    job_id=self.job_id,
                    submission_id=self.submission_id,
                    failure_log=self.failures,
                )
                return response.text
            except _LegFailed as exc:
                last_error = exc.failure
                continue
            except AllProvidersFailedError as exc:
                last_error = exc
                break
        raise self._as_legacy_error(last_error)

    def _as_legacy_error(self, error: NoteFailure | Exception | None) -> StructuringError:
        """Map normalized failure back onto the legacy exception vocabulary."""
        if isinstance(error, AllProvidersFailedError):
            return error
        if isinstance(error, NoteFailure):
            if error.category in {FAIL_TRANSIENT}:
                return ProviderTransientError(
                    f"ارتباط با سرویس تولید جزوه ({error.provider}) برقرار نشد."
                )
            if error.http_status:
                message = f"سرویس {error.provider} خطای HTTP {error.http_status} داد."
                if error.message:
                    message += f" ({error.message})"
                return ProviderHTTPError(
                    message,
                    status=error.http_status,
                    retry_after_seconds=error.retry_after,
                )
            return AllProvidersFailedError(_ALL_FAILED_FA, failures=self.failures)
        return AllProvidersFailedError(_ALL_FAILED_FA, failures=self.failures)


_CURRENT_JOB_SESSION: ContextVar = ContextVar("gamas_note_job_session", default=None)


@contextmanager
def job_session_scope(session: NoteJobSession | None):
    """Bind the active provider job session for structuring.py's call sites."""
    token = _CURRENT_JOB_SESSION.set(session)
    try:
        yield
    finally:
        _CURRENT_JOB_SESSION.reset(token)


def current_job_session() -> NoteJobSession | None:
    return _CURRENT_JOB_SESSION.get()
