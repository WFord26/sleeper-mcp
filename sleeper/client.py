"""
Shared HTTP client.

The original server opened a fresh httpx.AsyncClient for every single request,
so no connection was ever reused. Acceptable at conversational volume, wasteful
at the poll rate the dashboard runs at. This module keeps one pooled client per
event loop and adds retry with backoff plus a rate limit guard.
"""

import asyncio
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Union

import httpx

from . import config

_clients: Dict[Any, httpx.AsyncClient] = {}
_call_times: Deque[float] = deque()
_rate_lock: Optional[asyncio.Lock] = None


class SleeperAPIError(Exception):
    """Raised when the upstream API fails in a way the caller should surface."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def get_client() -> httpx.AsyncClient:
    """
    Return the pooled client for the running event loop.

    Keyed by loop because a client bound to a closed loop is unusable, and the
    MCP server and test runners do not share one.
    """
    loop = asyncio.get_event_loop()
    client = _clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=config.HTTP_TIMEOUT,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={"User-Agent": "sleeper-mcp/2.0 (personal, non commercial)"},
            follow_redirects=True,
        )
        _clients[loop] = client
    return client


async def close_client() -> None:
    loop = asyncio.get_event_loop()
    client = _clients.pop(loop, None)
    if client is not None and not client.is_closed:
        await client.aclose()


async def _respect_rate_limit() -> None:
    """
    Block briefly if we are near the configured calls per minute ceiling.

    Sleeper documents IP blocking above 1,000 per minute. Normal operation is
    nowhere near that, so this exists to make a runaway loop degrade into
    slowness rather than an IP ban.
    """
    global _rate_lock
    if _rate_lock is None:
        _rate_lock = asyncio.Lock()

    async with _rate_lock:
        now = time.monotonic()
        while _call_times and now - _call_times[0] > 60.0:
            _call_times.popleft()
        if len(_call_times) >= config.RATE_LIMIT_PER_MINUTE:
            sleep_for = 60.0 - (now - _call_times[0]) + 0.1
            await asyncio.sleep(max(sleep_for, 0))
            now = time.monotonic()
            while _call_times and now - _call_times[0] > 60.0:
                _call_times.popleft()
        _call_times.append(time.monotonic())


async def request_json(
    url: str,
    params: Optional[Union[Dict, List]] = None,
    *,
    retries: Optional[int] = None,
    timeout: Optional[float] = None,
) -> Any:
    """
    GET a URL and return parsed JSON, retrying transient failures.

    Retries on 429, 5xx, and timeouts with exponential backoff. Does not retry
    4xx other than 429, since those will not resolve themselves.
    """
    attempts = config.MAX_RETRIES if retries is None else retries
    client = get_client()
    last_exc: Optional[Exception] = None

    for attempt in range(attempts):
        await _respect_rate_limit()
        try:
            resp = await client.get(
                url,
                params=params,
                timeout=timeout or config.HTTP_TIMEOUT,
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = SleeperAPIError(
                    f"upstream returned HTTP {resp.status_code}", resp.status_code
                )
                if attempt < attempts - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise last_exc
            resp.raise_for_status()
            return resp.json()
        except httpx.TimeoutException as exc:
            last_exc = exc
            if attempt < attempts - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise
        except httpx.HTTPStatusError:
            raise  # a real 4xx, retrying will not help

    if last_exc:
        raise last_exc
    raise SleeperAPIError("request failed with no exception recorded")


async def sleeper_get(endpoint: str, params: Optional[Union[Dict, List]] = None) -> Any:
    """GET against the Sleeper API base."""
    return await request_json(f"{config.API_BASE}{endpoint}", params)


async def gather(*coroutines: Any) -> List[Any]:
    """Run coroutines concurrently, preserving order."""
    return await asyncio.gather(*coroutines)


def describe_error(exc: Exception) -> str:
    """Human readable one liner for any upstream failure."""
    if isinstance(exc, SleeperAPIError):
        if exc.status_code == 429:
            return "Error: Sleeper API rate limit hit. Wait a moment and retry."
        return f"Error: {exc}"
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 404:
            return "Error: Not found — check that the username or league ID is correct."
        if code == 429:
            return "Error: Sleeper API rate limit hit. Wait a moment and retry."
        return f"Error: Sleeper API returned HTTP {code}."
    if isinstance(exc, httpx.TimeoutException):
        return "Error: Request timed out — the Sleeper API may be slow. Retry in a moment."
    return f"Error: {type(exc).__name__}: {exc}"
