"""Declarative callback router replacing monolithic ``if/elif`` switches (OCP / SRP).

Workflows register exact callback keys and prefix patterns on a
:class:`CallbackRouter`.  Exact keys resolve in O(1); prefix patterns resolve
by longest matching prefix first so specific sub-routes (e.g. ``lib:docs`` or
``adm:hub:``) naturally take precedence over broader prefixes (e.g. ``lib:`` or
``adm:``) regardless of registration order.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

CallbackHandler = Callable[[Any, int, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PrefixRoute:
    prefix: str
    handler: CallbackHandler


class CallbackRouter:
    """Extensible exact + longest-prefix callback dispatcher (OCP)."""

    def __init__(self) -> None:
        self._exact: dict[str, CallbackHandler] = {}
        self._prefixes: list[PrefixRoute] = []

    def register_exact(self, key: str, handler: CallbackHandler) -> None:
        self._exact[key] = handler

    def register_prefix(self, prefix: str, handler: CallbackHandler) -> None:
        self._prefixes = [r for r in self._prefixes if r.prefix != prefix]
        self._prefixes.append(PrefixRoute(prefix=prefix, handler=handler))
        # Longest prefix wins deterministically regardless of registration order.
        self._prefixes.sort(key=lambda r: len(r.prefix), reverse=True)

    def resolve(self, data: str) -> CallbackHandler | None:
        exact = self._exact.get(data)
        if exact is not None:
            return exact
        for route in self._prefixes:
            if data.startswith(route.prefix):
                return route.handler
        return None

    async def dispatch(self, event: Any, user_id: int, data: str) -> bool:
        handler = self.resolve(data)
        if handler is None:
            return False
        await handler(event, user_id, data)
        return True
