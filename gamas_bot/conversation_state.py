"""Per-user and per-admin conversational wizard state store (SRP).

Centralizes all in-memory conversational prompt/wizard dictionaries and their
navigation-boundary disarm rules so :class:`gamas_bot.bot.GamasBot` does not
scatter manual ``.pop()`` / ``.discard()`` resets across message and callback
handlers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .office import OfficeSession


@dataclass
class ConversationStateStore:
    """Owns ephemeral conversational state and boundary reset transitions."""

    pending_payments: dict[int, int] = field(default_factory=dict)
    pending_broadcast: set[int] = field(default_factory=set)
    support_states: dict[int, tuple[str, object, float]] = field(default_factory=dict)
    support_admin_states: dict[int, tuple[int, float]] = field(default_factory=dict)
    pending_reject_reason: dict[int, int] = field(default_factory=dict)
    pending_flashcard: dict[int, tuple[int, str]] = field(default_factory=dict)
    pending_flashcard_front: dict[int, str] = field(default_factory=dict)
    curation: dict[int, dict[str, Any]] = field(default_factory=dict)
    pending_user_search: set[int] = field(default_factory=set)
    pending_free_reset: dict[int, int] = field(default_factory=dict)
    office_sessions: dict[int, OfficeSession] = field(default_factory=dict)

    def sweep_support_states(self, now: float) -> None:
        """Expire abandoned support prompts without retaining a separate timer per user."""
        for user_id, state in list(self.support_states.items()):
            if now - state[2] >= 1800:
                self.support_states.pop(user_id, None)
        for admin_id, state in list(self.support_admin_states.items()):
            if now - state[1] >= 1800:
                self.support_admin_states.pop(admin_id, None)

    def reset_on_start(self, user_id: int) -> None:
        """``/start`` is a hard reset boundary for every armed prompt."""
        self.pending_reject_reason.pop(user_id, None)
        self.pending_broadcast.discard(user_id)
        self.pending_payments.pop(user_id, None)
        self.support_states.pop(user_id, None)
        self.support_admin_states.pop(user_id, None)
        self.pending_flashcard.pop(user_id, None)
        self.pending_flashcard_front.pop(user_id, None)
        self.pending_user_search.discard(user_id)
        self.pending_free_reset.pop(user_id, None)
        self.office_sessions.pop(user_id, None)

    def reset_on_cancel(self, user_id: int) -> None:
        """``/cancel`` disarms user-facing and support/search prompts."""
        self.pending_payments.pop(user_id, None)
        self.office_sessions.pop(user_id, None)
        self.support_states.pop(user_id, None)
        self.support_admin_states.pop(user_id, None)
        self.pending_flashcard.pop(user_id, None)
        self.pending_flashcard_front.pop(user_id, None)
        self.pending_user_search.discard(user_id)
        self.pending_free_reset.pop(user_id, None)

    def disarm_admin_command_prompts(self, admin_id: int) -> None:
        """Any slash-command from an admin exits support-reply and manual-card flows."""
        self.support_admin_states.pop(admin_id, None)
        self.pending_flashcard.pop(admin_id, None)
        self.pending_flashcard_front.pop(admin_id, None)

    def disarm_manual_flashcard(self, user_id: int) -> None:
        self.pending_flashcard.pop(user_id, None)
        self.pending_flashcard_front.pop(user_id, None)

    def on_callback_navigation(self, user_id: int, data: str) -> None:
        """Apply navigation-boundary disarm rules when callback *data* fires."""
        if data.startswith("adm:") and data != "adm:bcast":
            self.pending_broadcast.discard(user_id)
            self.pending_reject_reason.pop(user_id, None)

        if not data.startswith("adm:hub:usr"):
            self.pending_user_search.discard(user_id)
            self.pending_free_reset.pop(user_id, None)
        else:
            if not data.startswith("adm:hub:usr:frx:"):
                self._clear_free_reset(user_id)
            self.pending_user_search.discard(user_id)

        if data.startswith("payv:"):
            self.pending_reject_reason.pop(user_id, None)

        if not data.startswith(("payr:", "plan:", "payopen:")):
            self.pending_payments.pop(user_id, None)

        if not data.startswith(("sup:cat:", "sup:r:")):
            self.support_states.pop(user_id, None)
        if not data.startswith("adm:sr:"):
            self.support_admin_states.pop(user_id, None)

        if not data.startswith("fc:man:"):
            self.pending_flashcard.pop(user_id, None)
            self.pending_flashcard_front.pop(user_id, None)

        if not data.startswith("office:"):
            self.office_sessions.pop(user_id, None)

    def _clear_free_reset(self, user_id: int) -> None:
        self.pending_free_reset.pop(user_id, None)
