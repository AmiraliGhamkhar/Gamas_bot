"""Race-free non-blocking capacity reservation for external providers and stages.

Standard ``asyncio.Semaphore`` answers only *wait until a slot opens*, which is
wrong when a healthy secondary provider is idle: a job stuck behind a full
Speechmatics or primary-LLM semaphore delays users even though Deepgram or the
backup LLM route could start immediately.

``CapacityPool`` separates two distinct steps on a single asyncio event loop:

1. **Immediate non-blocking pass (zero ``await``):** inspect candidate slots in
   priority order and atomically reserve the first candidate below its limit.
   Because check-and-increment runs synchronously without yielding to the loop,
   two coroutines cannot claim the same slot and overflow to a secondary
   provider happens with zero queueing delay.
2. **Bounded wait pass:** entered *only* when every eligible candidate is
   simultaneously full, capped by both a maximum waiter count (no unbounded
   local backlog) and a wall-clock timeout.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass
import time
from types import TracebackType
import weakref


class CapacityOverloadError(RuntimeError):
    """Raised when local waiter capacity is full and another waiter would pile up."""


@dataclass
class SlotLease:
    """Idempotent reservation token that always releases its slot at most once."""

    pool: CapacityPool
    key: str
    acquired_at: float
    _released: bool = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> float:
        if self._released:
            return 0.0
        self._released = True
        elapsed = max(0.0, time.monotonic() - self.acquired_at)
        self.pool._release_key(self.key)
        return elapsed

    def __enter__(self) -> SlotLease:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    async def __aenter__(self) -> SlotLease:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


class CapacityPool:
    """Loop-local slot manager supporting atomic ``try_acquire`` and overflow."""

    def __init__(self) -> None:
        self._in_flight: dict[str, int] = {}
        self._peak_in_flight: dict[str, int] = {}
        self._waiters = 0
        self._event = asyncio.Event()

    def in_flight(self, key: str) -> int:
        return self._in_flight.get(key, 0)

    def peak_in_flight(self, key: str) -> int:
        return self._peak_in_flight.get(key, 0)

    @property
    def waiters(self) -> int:
        return self._waiters

    def try_acquire(self, key: str, limit: int) -> SlotLease | None:
        """Reserve one slot on *key* immediately without yielding, or return ``None``."""
        effective_limit = max(1, int(limit))
        current = self._in_flight.get(key, 0)
        if current >= effective_limit:
            return None
        updated = current + 1
        self._in_flight[key] = updated
        if updated > self._peak_in_flight.get(key, 0):
            self._peak_in_flight[key] = updated
        return SlotLease(pool=self, key=key, acquired_at=time.monotonic())

    def _release_key(self, key: str) -> None:
        current = self._in_flight.get(key, 0)
        if current <= 1:
            self._in_flight.pop(key, None)
        else:
            self._in_flight[key] = current - 1
        # Pulse the event so any bounded waiter re-scans candidate slots in order.
        self._event.set()

    async def acquire_any(
        self,
        candidates: Sequence[tuple[str, int]],
        *,
        timeout: float = 0.0,
        max_waiters: int = 64,
    ) -> tuple[str, SlotLease, bool] | None:
        """Acquire the highest-priority open slot, overflowing without waiting.

        Returns ``(key, lease, waited)`` when a slot is reserved, or ``None`` if
        all candidates stay full until *timeout* expires.  Raises
        :class:`CapacityOverloadError` if all slots are full and ``max_waiters``
        coroutines are already waiting.
        """
        if not candidates:
            return None

        # Pass 1: completely non-blocking scan across all candidates in order.
        for key, limit in candidates:
            lease = self.try_acquire(key, limit)
            if lease is not None:
                return key, lease, False

        # Every candidate is at capacity right now.
        if timeout <= 0:
            return None
        if self._waiters >= max(1, max_waiters):
            raise CapacityOverloadError("ظرفیت صف انتظار سرویس تکمیل است.")

        deadline = time.monotonic() + timeout
        self._waiters += 1
        try:
            while True:
                # Clear BEFORE re-checking slots with no ``await`` between them,
                # eliminating lost wakeups if a release raced with our loop turn.
                self._event.clear()
                for key, limit in candidates:
                    lease = self.try_acquire(key, limit)
                    if lease is not None:
                        # Wake any sibling waiter if another slot also opened.
                        if any(
                            self._in_flight.get(k, 0) < max(1, int(lim))
                            for k, lim in candidates
                        ):
                            self._event.set()
                        return key, lease, True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(self._event.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    for key, limit in candidates:
                        lease = self.try_acquire(key, limit)
                        if lease is not None:
                            return key, lease, True
                    return None
        finally:
            self._waiters = max(0, self._waiters - 1)


_LOOP_POOLS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[str, weakref.WeakKeyDictionary[object, CapacityPool]],
] = weakref.WeakKeyDictionary()


def pool_for(namespace: str, owner: object) -> CapacityPool:
    """Return the ``CapacityPool`` shared by all tasks using *owner* on this loop."""
    loop = asyncio.get_running_loop()
    by_ns = _LOOP_POOLS.setdefault(loop, {})
    by_owner = by_ns.setdefault(namespace, weakref.WeakKeyDictionary())
    pool = by_owner.get(owner)
    if pool is None:
        pool = CapacityPool()
        by_owner[owner] = pool
    return pool


def reset_pools() -> None:
    """Clear cached pools across loops (test helper)."""
    _LOOP_POOLS.clear()


class CooldownRegistry:
    """In-memory monotonic deadline tracker for provider/route cooldowns (S2).

    Accepts an injectable monotonic clock (DIP) so callers or tests can drive
    time deterministically without duplicating deadline arithmetic across STT
    and LLM layers.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._deadlines: dict[Hashable, float] = {}

    def set(self, key: Hashable, seconds: float) -> None:
        if seconds <= 0:
            self._deadlines.pop(key, None)
            return
        self._deadlines[key] = self._clock() + seconds

    def remaining(self, key: Hashable) -> float:
        deadline = self._deadlines.get(key)
        if deadline is None:
            return 0.0
        left = deadline - self._clock()
        if left <= 0:
            self._deadlines.pop(key, None)
            return 0.0
        return left

    def clear(self) -> None:
        self._deadlines.clear()


class LoopCooldownRegistry:
    """Event-loop-scoped cooldown tracker isolating sessions per asyncio loop."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._by_loop: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, CooldownRegistry
        ] = weakref.WeakKeyDictionary()

    def set(self, owner: object, slug: str, seconds: float) -> None:
        if seconds <= 0:
            return
        loop = asyncio.get_running_loop()
        registry = self._by_loop.get(loop)
        if registry is None:
            registry = CooldownRegistry(clock=self._clock)
            self._by_loop[loop] = registry
        registry.set((id(owner), slug), seconds)

    def remaining(self, owner: object, slug: str) -> float:
        loop = asyncio.get_running_loop()
        registry = self._by_loop.get(loop)
        if registry is None:
            return 0.0
        return registry.remaining((id(owner), slug))

    def clear(self) -> None:
        self._by_loop.clear()
