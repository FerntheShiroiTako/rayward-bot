from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Iterable, Iterator, Protocol, Sequence, TypeVar

T = TypeVar("T")


def chunked(items: Sequence[T], size: int) -> Iterator[list[T]]:
    if size <= 0:
        raise ValueError("chunk size must be positive")
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Clock(Protocol):
    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return utcnow()

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)


def unique(items: Iterable[T]) -> list[T]:
    return list(dict.fromkeys(items))
