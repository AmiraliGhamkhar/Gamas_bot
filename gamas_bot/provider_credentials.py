"""Encrypted provider-key storage, rotation state and request-scoped access.

Secrets are encrypted before they cross the database boundary. The Fernet
master key is read only from the process environment (Settings); it is never
stored or generated into SQLite. Credential reprs, audit records, and UI
summaries deliberately contain only a label and the last four characters.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from .config import Settings
from .database import Database
from .stt_platform.registry import STT_PROVIDER_CHOICES

logger = logging.getLogger(__name__)

#: Note-provider credential pools. The legacy three stay; the rest are the
#: provider-platform registry slugs (kept in sync by tests).
NOTE_PROVIDER_CHOICES = frozenset(
    {
        "gemini", "anthropic", "openai_compatible",
        "nara", "groq", "openrouter", "mistral", "sambanova", "zai",
        "nvidia", "cloudflare", "huggingface", "alibaba", "cohere", "cerebras",
    }
)

#: Legacy STT providers keep their historical settings fields; every other
#: registry provider is stored through the generic native mapping below.
LEGACY_STT_CREDENTIAL_PROVIDERS = frozenset({"speechmatics", "deepgram", "openai_compatible"})

PROVIDER_CHOICES = {
    "stt": STT_PROVIDER_CHOICES,
    "notes": NOTE_PROVIDER_CHOICES,
}


def _upsert_pair(pairs: tuple, key: str, value: str) -> tuple:
    """Replace one ``(slug, value)`` entry in a config tuple, keeping the rest."""
    merged = dict(pairs)
    merged[key] = value
    return tuple(sorted(merged.items()))


def _apply_native_stt(settings: Settings, credential: "ProviderCredential") -> Settings:
    updates: dict[str, object] = {
        "stt_provider_api_keys": _upsert_pair(
            settings.stt_provider_api_keys, credential.provider, credential.secret
        ),
    }
    if credential.model:
        updates["stt_provider_models"] = _upsert_pair(
            settings.stt_provider_models, credential.provider, credential.model
        )
    if credential.base_url:
        updates["stt_provider_base_urls"] = _upsert_pair(
            settings.stt_provider_base_urls, credential.provider, credential.base_url.rstrip("/")
        )
    return replace(settings, **updates)


class CredentialStoreError(RuntimeError):
    """A safe-to-display vault/configuration problem (never includes a secret)."""


@dataclass(frozen=True, slots=True)
class ProviderCredential:
    id: int | None
    service: str
    provider: str
    label: str
    secret: str = field(repr=False)
    last4: str = ""
    base_url: str | None = None
    model: str | None = None
    source: str = "database"
    #: Admin billing flags (None = unmarked/unknown): a key explicitly marked
    #: free_only=0 is a paid key and never serves FREE_ONLY legs; a key
    #: explicitly marked paid_allowed=0 never serves paid-fallback legs.
    free_only: int | None = None
    paid_allowed: int | None = None
    billing_state: str = "unknown"
    billing_attested_at: str | None = None
    billing_attested_by_admin_id: int | None = None

    @property
    def masked(self) -> str:
        return "••••••••" + self.last4


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _contains_secret(value: str | None, secret: str) -> bool:
    if not value or not secret:
        return False
    haystack = value.casefold()
    needle = secret.casefold()
    if needle in haystack:
        return True
    # Catch meaningful accidental plaintext fragments while keeping the
    # threshold high enough not to match ordinary provider/model labels.
    fragment_length = 12
    return len(needle) >= fragment_length and any(
        needle[index:index + fragment_length] in haystack
        for index in range(len(needle) - fragment_length + 1)
    )


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


class ProviderCredentialManager:
    """Per-provider credential pool backed by encrypted SQLite records."""

    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self._fernet = None
        self._fernet_error: str | None = None
        self._environment_state: dict[tuple[str, str], dict] = {}

    @property
    def encryption_configured(self) -> bool:
        return bool(self.settings.provider_credentials_encryption_key)

    def _cipher(self):
        if self._fernet is not None:
            return self._fernet
        if not self.encryption_configured:
            self._fernet_error = "not_configured"
            raise CredentialStoreError(
                "رمزگذاری کلیدهای API فعال نیست؛ متغیر PROVIDER_CREDENTIALS_ENCRYPTION_KEY را در محیط سرور تنظیم کنید."
            )
        try:
            from cryptography.fernet import Fernet

            key = self.settings.provider_credentials_encryption_key
            self._fernet = Fernet(str(key).encode("ascii"))
            return self._fernet
        except Exception as exc:
            self._fernet_error = type(exc).__name__
            raise CredentialStoreError(
                "کلید رمزگذاری API نامعتبر است؛ مقدار PROVIDER_CREDENTIALS_ENCRYPTION_KEY را بررسی کنید."
            ) from None

    def encrypt_secret(self, secret: str) -> tuple[str, str]:
        value = secret.strip()
        if not value or len(value) > 512 or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise CredentialStoreError("کلید API خالی، بیش‌ازحد بلند یا دارای نویسهٔ کنترلی است.")
        token = self._cipher().encrypt(value.encode("utf-8")).decode("ascii")
        return token, value[-4:]

    def _decrypt(self, ciphertext: str) -> str:
        try:
            from cryptography.fernet import InvalidToken
        except Exception:
            raise CredentialStoreError("وابستگی رمزگذاری کلیدهای API در دسترس نیست.") from None
        try:
            return self._cipher().decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except CredentialStoreError:
            raise
        except (InvalidToken, UnicodeError, ValueError, TypeError):
            raise CredentialStoreError(
                "یکی از کلیدهای ذخیره‌شده با کلید اصلی فعلی باز نمی‌شود؛ مقدار محیطی یا ciphertext را بررسی کنید."
            ) from None

    async def add_credential(
        self,
        *,
        service: str,
        provider: str,
        label: str,
        secret: str,
        admin_id: int,
        base_url: str | None = None,
        model: str | None = None,
        priority: int = 100,
        enabled: bool = True,
        free_only: bool | None = None,
        paid_allowed: bool | None = None,
    ) -> int:
        service = service.strip().lower()
        provider = provider.strip().lower()
        if service not in PROVIDER_CHOICES or provider not in PROVIDER_CHOICES[service]:
            raise CredentialStoreError("سرویس یا provider انتخاب‌شده پشتیبانی نمی‌شود.")
        label = " ".join(label.split())
        if not label or len(label) > 80 or any(ord(char) < 32 for char in label):
            raise CredentialStoreError("نام نمایشی کلید معتبر نیست.")
        secret_value = secret.strip()
        clean_base = (base_url or "").strip() or None
        if clean_base:
            parsed = urlparse(clean_base)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise CredentialStoreError("Base URL باید یک نشانی کامل http یا https باشد.")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise CredentialStoreError(
                    "Base URL نباید نام‌کاربری، رمز، query یا fragment داشته باشد؛ "
                    "کلید باید جداگانه و رمزگذاری‌شده ثبت شود."
                )
        if clean_base and not (
            (service == "stt" and provider == "openai_compatible")
            or service == "notes"
        ):
            raise CredentialStoreError("برای این provider امکان تعیین Base URL وجود ندارد.")
        clean_model = (model or "").strip() or None
        if clean_model and len(clean_model) > 120:
            raise CredentialStoreError("نام مدل بیش‌ازحد بلند است.")
        for field_name, field_value in (
            ("label", label), ("Base URL", clean_base), ("نام مدل", clean_model)
        ):
            if _contains_secret(field_value, secret_value):
                raise CredentialStoreError(
                    f"{field_name} نباید شامل کلید API یا بخشی از آن باشد."
                )
        ciphertext, last4 = self.encrypt_secret(secret)
        return await self.db.add_provider_credential(
            service=service,
            provider=provider,
            label=label,
            ciphertext=ciphertext,
            last4=last4,
            base_url=clean_base,
            model=clean_model,
            priority=max(-1000, min(int(priority), 1000)),
            admin_id=admin_id,
            enabled=enabled,
            free_only=free_only,
            paid_allowed=paid_allowed,
        )

    async def replace_secret(self, credential_id: int, new_secret: str, admin_id: int) -> bool:
        """Rotate one key's secret. Old and new values are never displayed."""
        record = await self.db.provider_credential_record(int(credential_id))
        if not record:
            raise CredentialStoreError("کلید انتخاب‌شده پیدا نشد.")
        ciphertext, last4 = self.encrypt_secret(new_secret)
        changed = await self.db.replace_provider_credential_secret(
            int(credential_id), ciphertext=ciphertext, last4=last4, admin_id=int(admin_id)
        )
        if changed:
            self._environment_state.pop(
                (str(record["service"]), str(record["provider"])), None
            )
        return changed

    async def update_metadata(
        self,
        credential_id: int,
        *,
        label: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        admin_id: int,
    ) -> bool:
        """Edit label/base URL/model with the same validation as add."""
        clean_base = (base_url or "").strip() or None
        if clean_base:
            parsed = urlparse(clean_base)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise CredentialStoreError("Base URL باید یک نشانی کامل http یا https باشد.")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise CredentialStoreError(
                    "Base URL نباید نام‌کاربری، رمز، query یا fragment داشته باشد."
                )
        clean_model = (model or "").strip() or None
        if clean_model and len(clean_model) > 120:
            raise CredentialStoreError("نام مدل بیش‌ازحد بلند است.")
        clean_label = " ".join(label.split()) if label is not None else None
        if clean_label is not None and (not clean_label or len(clean_label) > 80):
            raise CredentialStoreError("نام نمایشی کلید معتبر نیست.")
        try:
            return await self.db.update_provider_credential_metadata(
                int(credential_id),
                label=clean_label,
                base_url=clean_base,
                model=clean_model,
                admin_id=int(admin_id),
            )
        except ValueError as exc:
            raise CredentialStoreError(str(exc)) from None

    async def set_billing_flags(
        self,
        credential_id: int,
        *,
        free_only: bool | None = None,
        paid_allowed: bool | None = None,
        admin_id: int,
    ) -> bool:
        return await self.db.set_provider_credential_billing_flags(
            int(credential_id),
            free_only=free_only,
            paid_allowed=paid_allowed,
            admin_id=int(admin_id),
        )

    async def set_billing_attestation(
        self, credential_id: int, state: str, admin_id: int
    ) -> bool:
        """Record explicit free-no-overage or paid-use authorization."""
        return await self.db.set_provider_credential_billing_attestation(
            int(credential_id), state=state, admin_id=int(admin_id)
        )

    async def set_primary(self, credential_id: int, admin_id: int) -> bool:
        return await self.db.set_provider_credential_primary(int(credential_id), int(admin_id))

    async def candidates(
        self,
        service: str,
        provider: str,
        *,
        fallback_secret: str | None = None,
        fallback_base_url: str | None = None,
        fallback_model: str | None = None,
    ) -> list[ProviderCredential]:
        """Return ready stored keys in priority order followed by a static env key."""
        if service not in PROVIDER_CHOICES or provider not in PROVIDER_CHOICES[service]:
            return []
        now = _utc_now()
        result: list[ProviderCredential] = []
        try:
            records = await self.db.provider_credential_records(service, provider)
        except Exception:
            logger.exception("Could not load provider credentials service=%s provider=%s", service, provider)
            records = []
        for row in records:
            if row.get("quarantined_at"):
                continue
            cooldown = _parse_utc(row.get("cooldown_until"))
            if cooldown is not None and cooldown > now:
                continue
            try:
                secret = self._decrypt(str(row["secret_ciphertext"]))
            except CredentialStoreError as exc:
                # Do not log ciphertext, key material, or provider response data.
                logger.warning(
                    "Stored provider credential unavailable id=%s reason=%s",
                    row.get("id"),
                    str(exc).split("؛", 1)[0],
                )
                continue
            result.append(
                ProviderCredential(
                    id=int(row["id"]),
                    service=service,
                    provider=provider,
                    label=str(row["label"]),
                    secret=secret,
                    last4=str(row["secret_last4"]),
                    base_url=row.get("base_url"),
                    model=row.get("model"),
                    free_only=row.get("free_only"),
                    paid_allowed=row.get("paid_allowed"),
                    billing_state=str(row.get("billing_state") or "unknown"),
                    billing_attested_at=row.get("billing_attested_at"),
                    billing_attested_by_admin_id=row.get("billing_attested_by_admin_id"),
                )
            )
        allow_keyless = False
        if (
            not fallback_secret
            and fallback_base_url
            and provider == "openai_compatible"
        ):
            try:
                allow_keyless = not await self.db.provider_credentials_exist(service, provider)
            except Exception:
                logger.exception(
                    "Could not verify keyless provider configuration service=%s provider=%s",
                    service,
                    provider,
                )
        if fallback_secret or allow_keyless:
            state = self._environment_state.setdefault((service, provider), {})
            if not state.get("quarantined_at") and not (
                state.get("cooldown_until") and state["cooldown_until"] > now
            ):
                secret = fallback_secret or ""
                result.append(
                    ProviderCredential(
                        id=None,
                        service=service,
                        provider=provider,
                        label="environment" if secret else "unauthenticated endpoint",
                        secret=secret,
                        last4=secret[-4:],
                        base_url=fallback_base_url,
                        model=fallback_model,
                        source="environment",
                    )
                )
        # Bound worst-case API work per call even if administrators add many keys.
        return result[:5]

    async def has_available(self, service: str, provider: str, **fallback) -> bool:
        return bool(await self.candidates(service, provider, **fallback))

    @staticmethod
    def apply_to_settings(
        settings: Settings, credential: ProviderCredential
    ) -> Settings:
        """Return a request-local Settings copy; the master/config stays immutable."""
        if credential.service == "stt":
            if credential.provider == "speechmatics":
                return replace(
                    settings,
                    speechmatics_api_key=credential.secret,
                    speechmatics_base_url=credential.base_url or settings.speechmatics_base_url,
                )
            if credential.provider == "deepgram":
                return replace(settings, deepgram_api_key=credential.secret)
            if credential.provider == "openai_compatible":
                return replace(
                    settings,
                    stt_openai_api_key=credential.secret,
                    stt_openai_base_url=credential.base_url or settings.stt_openai_base_url,
                    stt_openai_model=credential.model or settings.stt_openai_model,
                )
            if credential.provider in STT_PROVIDER_CHOICES:
                return _apply_native_stt(settings, credential)
        if credential.service == "notes":
            return replace(
                settings,
                note_api_key=credential.secret,
                note_api_base_url=credential.base_url or settings.note_api_base_url,
                note_api_model=credential.model or settings.note_api_model,
            )
        return settings

    async def record_result(
        self,
        credential: ProviderCredential,
        *,
        result: str,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
        safe_error: str | None = None,
    ) -> None:
        """Persist cooldown/quarantine/last outcome without storing raw errors."""
        if credential.id is not None:
            await self.db.record_provider_credential_result(
                credential.id,
                status_code=status_code,
                result=result,
                retry_after_seconds=retry_after_seconds,
                safe_error=safe_error,
            )
            return
        state = self._environment_state.setdefault((credential.service, credential.provider), {})
        now = _utc_now()
        state["last_status_code"] = status_code
        state["last_error"] = safe_error
        if result == "success":
            state["last_success_at"] = now
            state.pop("cooldown_until", None)
            state.pop("quarantined_at", None)
        elif result == "cooldown":
            state["last_failure_at"] = now
            state["cooldown_until"] = now + timedelta(seconds=max(1, int(retry_after_seconds or 30)))
        elif result == "quarantined":
            state["last_failure_at"] = now
            state["quarantined_at"] = now
        else:
            state["last_failure_at"] = now

    async def list_summaries(self) -> list[dict]:
        return await self.db.provider_credential_summaries()

    async def credential_for_test(self, credential_id: int) -> ProviderCredential:
        """Decrypt exactly one stored credential for an explicit admin action.

        Unlike :meth:`candidates` this ignores cooldown, quarantine and the
        ``enabled`` flag: an administrator asking to test a key must get a real
        answer for that key. The returned object keeps the secret out of its
        ``repr`` (``ProviderCredential.secret`` is ``repr=False``).
        """
        record = await self.db.provider_credential_record(int(credential_id))
        if not record:
            raise CredentialStoreError("کلید انتخاب‌شده پیدا نشد.")
        secret = self._decrypt(str(record["secret_ciphertext"]))
        return ProviderCredential(
            id=int(record["id"]),
            service=str(record["service"]),
            provider=str(record["provider"]),
            label=str(record["label"]),
            secret=secret,
            last4=str(record["secret_last4"]),
            base_url=record.get("base_url"),
            model=record.get("model"),
            source="database",
            free_only=record.get("free_only"),
            paid_allowed=record.get("paid_allowed"),
            billing_state=str(record.get("billing_state") or "unknown"),
            billing_attested_at=record.get("billing_attested_at"),
            billing_attested_by_admin_id=record.get("billing_attested_by_admin_id"),
        )

    async def reorder(self, credential_id: int, direction: str, admin_id: int) -> bool:
        """Deterministic priority change inside one provider pool."""
        return await self.db.reorder_provider_credential(int(credential_id), direction, int(admin_id))

    async def enable(self, credential_id: int, admin_id: int) -> bool:
        return await self.db.set_provider_credential_enabled(credential_id, True, admin_id)

    async def disable(self, credential_id: int, admin_id: int) -> bool:
        return await self.db.set_provider_credential_enabled(credential_id, False, admin_id)

    async def delete(self, credential_id: int, admin_id: int) -> bool:
        return await self.db.delete_provider_credential(credential_id, admin_id)

    def environment_health(self) -> dict[tuple[str, str], dict]:
        """Process-local status for env-backed keys, safe for admin display only."""
        return {key: dict(value) for key, value in self._environment_state.items()}


_CURRENT_CREDENTIAL_MANAGER: ContextVar[ProviderCredentialManager | None] = ContextVar(
    "gamas_provider_credential_manager", default=None
)


@contextmanager
def use_provider_credentials(manager: ProviderCredentialManager | None):
    token: Token = _CURRENT_CREDENTIAL_MANAGER.set(manager)
    try:
        yield
    finally:
        _CURRENT_CREDENTIAL_MANAGER.reset(token)


def current_provider_credentials() -> ProviderCredentialManager | None:
    return _CURRENT_CREDENTIAL_MANAGER.get()
