"""SQLite persistence facade for Gamas Bot v3 (SRP, ISP, DIP).

Architecture:

* :mod:`gamas_bot.db_core` — WAL connection, read-only secondary handle,
  ``BEGIN IMMEDIATE`` write lock, numbered additive migrations, and permissions.
* :mod:`gamas_bot.db_users` — user accounts, identity, bans, and free tries.
* :mod:`gamas_bot.db_library` — submissions, transcripts, and Telegram library.
* :mod:`gamas_bot.db_documents` — owner-scoped study document cache.
* :mod:`gamas_bot.db_learning` — quizzes, flashcards, and leaderboards.
* :mod:`gamas_bot.db_billing` — append-only ledger, FIFO lots, and holds.
* :mod:`gamas_bot.db_payments` — tariff catalog and manual receipt payments.
* :mod:`gamas_bot.db_support` — support tickets and state transitions.
* :mod:`gamas_bot.db_reporting` — admin reporting, schema integrity, and audit.

Callers needing a narrow persistence contract (ISP) can depend on the protocol
slices exposed by :attr:`Database.users`, :attr:`Database.billing`,
:attr:`Database.payments`, :attr:`Database.support`, :attr:`Database.library`,
:attr:`Database.documents`, :attr:`Database.learning`, and
:attr:`Database.reporting` (typed in :mod:`gamas_bot.protocols`).
"""

from __future__ import annotations

import logging

from .db_billing import BillingRepositoryMixin
from .db_core import (
    ADD_COLUMN_RE,
    APPLICATION_TABLES,
    MIGRATIONS_DIR,
    TERMINAL_SUBMISSION_STATUSES,
    DatabaseCore,
    InsufficientBalanceError,
    OpenPaymentError,
    PaymentSnapshotError,
    SubmissionRow,
    TicketStateError,
    TooManyActiveJobsError,
    UserBannedError,
    UserRow,
    _rows,
    iso,
    utc_now,
)
from .db_documents import DocumentsRepositoryMixin
from .db_learning import LearningRepositoryMixin
from .db_library import LibraryRepositoryMixin
from .db_payments import PaymentsRepositoryMixin
from .db_referrals import ReferralsRepositoryMixin
from .db_reporting import ReportingRepositoryMixin
from .db_support import SupportRepositoryMixin
from .db_users import UsersRepositoryMixin
from .protocols import (
    BillingStoreProtocol,
    DocumentStoreProtocol,
    LearningStoreProtocol,
    LibraryStoreProtocol,
    PaymentStoreProtocol,
    ReportingStoreProtocol,
    SupportStoreProtocol,
    UserStoreProtocol,
)

logger = logging.getLogger(__name__)


class Database(
    ReferralsRepositoryMixin,
    LibraryRepositoryMixin,
    DocumentsRepositoryMixin,
    LearningRepositoryMixin,
    PaymentsRepositoryMixin,
    SupportRepositoryMixin,
    ReportingRepositoryMixin,
    UsersRepositoryMixin,
    BillingRepositoryMixin,
    DatabaseCore,
):
    """Unified persistence facade backed by single-responsibility domain slices."""

    @property
    def users(self) -> UserStoreProtocol:
        """Segregated user/moderation persistence view (ISP)."""
        return self

    @property
    def billing(self) -> BillingStoreProtocol:
        """Segregated ledger/hold persistence view (ISP)."""
        return self

    @property
    def payments(self) -> PaymentStoreProtocol:
        """Segregated plan/payment persistence view (ISP)."""
        return self

    @property
    def support(self) -> SupportStoreProtocol:
        """Segregated support-ticket persistence view (ISP)."""
        return self

    @property
    def library(self) -> LibraryStoreProtocol:
        """Segregated submission/library persistence view (ISP)."""
        return self

    @property
    def documents(self) -> DocumentStoreProtocol:
        """Segregated study-document persistence view (ISP)."""
        return self

    @property
    def learning(self) -> LearningStoreProtocol:
        """Segregated quiz/flashcard/leaderboard persistence view (ISP)."""
        return self

    @property
    def reporting(self) -> ReportingStoreProtocol:
        """Segregated read-only admin reporting/audit view (ISP)."""
        return self


__all__ = [
    "ADD_COLUMN_RE",
    "APPLICATION_TABLES",
    "Database",
    "DatabaseCore",
    "InsufficientBalanceError",
    "MIGRATIONS_DIR",
    "OpenPaymentError",
    "PaymentSnapshotError",
    "SubmissionRow",
    "TERMINAL_SUBMISSION_STATUSES",
    "TicketStateError",
    "TooManyActiveJobsError",
    "UserBannedError",
    "UserRow",
    "_rows",
    "iso",
    "utc_now",
]
