"""Shared HTTP plumbing: per-host throttle, 429/5xx backoff, JSON decoding.

Callers must treat every raised exception as "we learned nothing" (Inconclusive),
never as "clean".
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Protocol

import aiohttp

log = logging.getLogger(__name__)

SleepFn = Callable[[float], Awaitable[None]]


class NetworkError(Exception):
    """Connection failure or timeout."""


class HttpError(Exception):
    def __init__(self, status: int, body: Any):
        super().__init__(f"HTTP {status}: {_short(body)}")
        self.status = status
        self.body = body


class TransientHttpError(HttpError):
    """429 or 5xx after retries were exhausted."""


@dataclass(frozen=True)
class JsonResponse:
    status: int
    body: Any
    headers: Mapping[str, str]


class Throttle:
    """Enforces a minimum interval between requests to one host."""

    def __init__(self, min_interval_s: float, *, sleep: SleepFn | None = None, monotonic=time.monotonic):
        self._min = max(min_interval_s, 0.0)
        self._sleep = sleep or asyncio.sleep
        self._mono = monotonic
        self._lock = asyncio.Lock()
        self._last = -1e9

    async def wait(self) -> None:
        async with self._lock:
            now = self._mono()
            gap = self._last + self._min - now
            if gap > 0:
                await self._sleep(gap)
            self._last = self._mono()


class Requester(Protocol):
    async def __call__(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> JsonResponse: ...


def _short(body: Any, limit: int = 300) -> str:
    s = str(body)
    return s if len(s) <= limit else s[:limit] + "..."


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    ra = headers.get("Retry-After") or headers.get("retry-after")
    if not ra:
        return None
    try:
        return float(ra)
    except ValueError:
        return None


class AiohttpRequester:
    """Requester bound to one aiohttp session + one throttle + one backoff policy."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        throttle: Throttle,
        timeout_s: float,
        max_retries: int,
        backoff_base_s: float,
        backoff_max_s: float,
        sleep: SleepFn | None = None,
        name: str = "http",
    ):
        self._session = session
        self._throttle = throttle
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._max_retries = max(max_retries, 0)
        self._base = backoff_base_s
        self._cap = backoff_max_s
        self._sleep = sleep or asyncio.sleep
        self._name = name

    def _backoff(self, attempt: int, headers: Mapping[str, str] | None = None) -> float:
        ra = _retry_after_seconds(headers or {})
        if ra is not None:
            return min(max(ra, 0.0), self._cap)
        return min(self._base * (2**attempt), self._cap)

    async def __call__(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> JsonResponse:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            await self._throttle.wait()
            try:
                async with self._session.request(
                    method, url, json=json_body, headers=headers, timeout=self._timeout
                ) as resp:
                    status = resp.status
                    resp_headers = dict(resp.headers)
                    try:
                        body = await resp.json(content_type=None)
                    except Exception:
                        body = await resp.text()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_exc = NetworkError(f"{type(e).__name__}: {e}")
                if attempt < self._max_retries:
                    delay = self._backoff(attempt)
                    log.warning("%s %s %s: network error (%s); retry in %.1fs", self._name, method, url, e, delay)
                    await self._sleep(delay)
                    continue
                raise last_exc from e

            if status == 429 or status >= 500:
                last_exc = TransientHttpError(status, body)
                if attempt < self._max_retries:
                    delay = self._backoff(attempt, resp_headers)
                    log.warning("%s %s %s: HTTP %s; retry in %.1fs", self._name, method, url, status, delay)
                    await self._sleep(delay)
                    continue
                raise last_exc
            return JsonResponse(status=status, body=body, headers=resp_headers)
        # Unreachable, but keeps type checkers happy.
        raise last_exc or NetworkError("no attempts made")
