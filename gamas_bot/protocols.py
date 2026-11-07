"""Segregated structural protocols for Gamas Bot subsystems (ISP, DIP, LSP).

Why this module exists:

* **Interface Segregation (ISP)** — callers that only read user bans, settle a
  billing hold, or manage support tickets declare narrow protocol contracts
  rather than depending on the full 80+-method :class:`gamas_bot.db.Database`
  surface or the :class:`gamas_bot.bot.GamasBot` orchestrator.
* **Dependency Inversion (DIP)** — high-level workflows depend on abstract
  provider dialects, STT adapters, clocks, and persistence slices rather than
  concrete HTTP payloads or SQLite internals.
* **Liskov Substitution (LSP)** — every STT backend and every LLM wire dialect
  honors one uniform behavioral contract (non-empty normalized output,
  deterministic error hierarchy, and side-effect-free inspection helpers).
"""

from __future__ import annotations

from collections.abc import Awaitable, Collection, Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    import aiohttp

    from .config import NoteRoute, Settings
    from .db_core import SubmissionRow, UserRow
    from .transcript import RawTranscript


@runtime_checkable
class ClockProtocol(Protocol):
    """Injectable time source for deterministic workflow and repository logic."""

    def utc_now(self) -> datetime: ...
    def monotonic(self) -> float: ...


@runtime_checkable
class STTProviderAdapter(Protocol):
    """Uniform contract every speech-to-text backend satisfies (OCP / LSP)."""

    @property
    def slug(self) -> str: ...

    @property
    def label(self) -> str: ...

    def is_configured(self, settings: Settings) -> bool: ...
    def resolve_model(self, settings: Settings) -> str: ...
    def resolve_base_url(self, settings: Settings) -> str: ...
    def resolve_upload_limit(self, settings: Settings) -> int: ...
    def resolve_max_in_flight(self, settings: Settings) -> int: ...
    def run(
        self,
        session: aiohttp.ClientSession,
        audio_path: Path,
        settings: Settings,
    ) -> Awaitable[RawTranscript]: ...


@runtime_checkable
class LLMProviderProfileProtocol(Protocol):
    """Per-provider capability & schema policy contract (SRP / OCP)."""

    @property
    def slug(self) -> str: ...

    def supports_response_format(self, route: NoteRoute) -> bool: ...
    def supports_stream_options(self, route: NoteRoute) -> bool: ...
    def build_response_format(
        self,
        route: NoteRoute,
        settings: Settings,
        schema: dict[str, Any] | None,
        *,
        streaming: bool,
    ) -> dict[str, Any] | None: ...


@runtime_checkable
class LLMProviderDialect(Protocol):
    """Wire-format strategy for one LLM provider family (OpenAI, Gemini, Anthropic)."""

    @property
    def style(self) -> str: ...

    def build_post_request(
        self,
        route: NoteRoute,
        settings: Settings,
        system: str,
        user: str,
        *,
        schema: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, str], dict[str, Any]]: ...

    def build_get_request(
        self,
        route: NoteRoute,
        target: str = "models",
    ) -> tuple[str, dict[str, str]]: ...

    def is_output_truncated(self, payload: dict[str, Any]) -> bool: ...
    def is_reasoning_exhausted(self, payload: dict[str, Any]) -> bool: ...
    def parse_response(self, payload: dict[str, Any]) -> str: ...
    def extract_usage(
        self,
        payload: dict[str, Any],
    ) -> tuple[str, int | None, int | None, int | None]: ...


@runtime_checkable
class UserStoreProtocol(Protocol):
    """Narrow persistence view for user identity, moderation, and free gifts."""

    async def ensure_user(
        self,
        telegram_id: int,
        first_name: str | None = None,
        *,
        username: str | None = None,
        last_name: str | None = None,
    ) -> UserRow: ...

    async def is_banned(self, telegram_id: int) -> bool: ...
    async def is_unlimited(self, user_id: int) -> bool: ...
    async def user_identity(self, user_id: int) -> dict[str, Any] | None: ...
    async def identities_for(
        self, user_ids: Iterable[int]
    ) -> dict[int, dict[str, Any]]: ...


@runtime_checkable
class BillingStoreProtocol(Protocol):
    """Narrow persistence view for the append-only second-granularity ledger (B1–B4)."""

    async def available_seconds(self, user_id: int) -> int: ...
    async def add_credit(
        self,
        user_id: int,
        seconds: int,
        *,
        note: str,
        validity_days: int | None = None,
        admin_id: int | None = None,
    ) -> None: ...

    async def reserve(
        self, user_id: int, submission_id: int, seconds: int
    ) -> None: ...

    async def settle_hold(
        self, submission_id: int, measured_seconds: int
    ) -> None: ...

    async def release_hold(self, submission_id: int, reason: str) -> bool: ...
    async def spend_seconds(
        self,
        user_id: int,
        seconds: int,
        note: str,
        *,
        operation_id: str | None = None,
    ) -> bool: ...

    async def refund_learning_charge(
        self, user_id: int, operation_id: str
    ) -> bool: ...


@runtime_checkable
class PaymentStoreProtocol(Protocol):
    """Narrow persistence view for plans and manual card-to-card payments (B7)."""

    async def plans(self) -> list[dict[str, Any]]: ...
    async def plan(self, code: str) -> dict[str, Any] | None: ...
    async def create_payment(self, user_id: int, plan_code: str) -> int: ...
    async def cancel_payment(self, payment_id: int, user_id: int) -> bool: ...
    async def set_payment_receipt(
        self,
        payment_id: int,
        receipt_path: str,
        user_id: int | None = None,
    ) -> bool: ...

    async def payment(self, payment_id: int) -> dict[str, Any] | None: ...
    async def open_awaiting_payment_for_user(
        self, user_id: int
    ) -> dict[str, Any] | None: ...

    async def review_payment(
        self,
        payment_id: int,
        approved: bool,
        admin_id: int,
        *,
        reason: str = "",
    ) -> bool: ...


@runtime_checkable
class SupportStoreProtocol(Protocol):
    """Narrow persistence view for user/admin support tickets."""

    async def create_ticket(
        self,
        user_id: int,
        category: str,
        subject: str,
        *,
        initial_message: str | None = None,
        telegram_message_id: int | None = None,
        max_open_tickets: int = 3,
        cooldown_seconds: int = 600,
    ) -> dict[str, Any]: ...

    async def get_ticket(self, ticket_id: int) -> dict[str, Any] | None: ...
    async def list_user_tickets(
        self, user_id: int, limit: int = 10, offset: int = 0
    ) -> list[dict[str, Any]]: ...

    async def list_tickets(
        self,
        *,
        view: str = "active",
        category: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...

    async def add_ticket_message(
        self,
        ticket_id: int,
        sender_id: int,
        sender_role: str,
        message_text: str,
        telegram_message_id: int | None = None,
    ) -> dict[str, Any]: ...

    async def close_ticket(
        self, ticket_id: int, actor_id: int, *, admin: bool
    ) -> bool: ...

    async def reopen_ticket(
        self, ticket_id: int, actor_id: int, *, admin: bool
    ) -> bool: ...


@runtime_checkable
class LibraryStoreProtocol(Protocol):
    """Narrow persistence view for submissions and Telegram-backed library items."""

    async def submission(self, submission_id: int) -> SubmissionRow | None: ...
    async def library_add(
        self,
        user_id: int,
        kind: str,
        chat_id: int,
        message_id: int,
        submission_id: int | None = None,
        *,
        title: str = "",
    ) -> int: ...

    async def library_item(
        self, user_id: int, item_id: int
    ) -> dict[str, Any] | None: ...

    async def library_prune(
        self, user_id: int, keep: int, *, protected_item_ids: Collection[int] = ()
    ) -> int: ...


@runtime_checkable
class DocumentStoreProtocol(Protocol):
    """Narrow persistence view for owner-scoped extracted study documents (D1)."""

    async def document_by_hash(
        self, user_id: int, sha256: str
    ) -> dict[str, Any] | None: ...

    async def document_get(
        self, user_id: int, document_id: int
    ) -> dict[str, Any] | None: ...

    async def documents_list(
        self, user_id: int, limit: int = 6, offset: int = 0
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class LearningStoreProtocol(Protocol):
    """Narrow persistence view for quizzes, flashcards, and leaderboards."""

    async def quiz_get_session(
        self, session_id: int, user_id: int
    ) -> dict[str, Any] | None: ...

    async def deck_get(
        self, deck_id: int, user_id: int
    ) -> dict[str, Any] | None: ...

    async def leaderboard_top(self, limit: int = 20) -> list[dict[str, Any]]: ...


@runtime_checkable
class ReportingStoreProtocol(Protocol):
    """Read-only reporting and audit persistence slice (R4)."""

    async def admin_health(self) -> dict[str, Any]: ...
    async def admin_report(
        self,
        *,
        period_start: str | None,
        now: datetime | None = None,
        expiring_within_days: int = 7,
        with_timings: bool = True,
    ) -> dict[str, Any]: ...

    async def audit(
        self,
        admin_id: int,
        action: str,
        detail: str,
        subject_user: int | None = None,
    ) -> None: ...

    async def recent_audit(
        self, limit: int = 30, offset: int = 0
    ) -> list[dict[str, Any]]: ...
