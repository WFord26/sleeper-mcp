"""
TTL aware cache with optional disk persistence.

Replaces the module level global dicts in the original server, which never
expired and never evicted. Harmless in a short lived stdio process, a real bug
in a long running web server: a permanently cached /state/nfl means the
dashboard still believes it is week 1 in December.

Policy per data class lives in config.py. The important case is matchups, where
completed weeks are immutable (cached forever) and only the current week is hot.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from . import config


class TTLCache:
    """In memory cache where each entry carries its own expiry."""

    def __init__(self) -> None:
        self._data: Dict[str, Tuple[Optional[float], Any]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def get(self, key: str) -> Optional[Any]:
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at is not None and time.monotonic() > expires_at:
            del self._data[key]
            return None
        return value

    def set(self, key: str, value: Any, ttl: Optional[float]) -> None:
        expires_at = None if ttl is None else time.monotonic() + ttl
        self._data[key] = (expires_at, value)

    def invalidate(self, key: str) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()

    def _lock_for(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def get_or_fetch(
        self,
        key: str,
        fetch: Callable[[], Awaitable[Any]],
        ttl: Optional[float],
    ) -> Any:
        """
        Return the cached value, or await fetch() and cache it.

        Holds a per key lock so that N concurrent callers asking for the same
        cold key produce one upstream request, not N. This matters on dashboard
        startup where several endpoints want the roster list at once.
        """
        hit = self.get(key)
        if hit is not None:
            return hit

        async with self._lock_for(key):
            hit = self.get(key)  # another waiter may have filled it
            if hit is not None:
                return hit
            value = await fetch()
            self.set(key, value, ttl)
            return value

    def stats(self) -> Dict[str, Any]:
        now = time.monotonic()
        live = sum(
            1 for exp, _ in self._data.values() if exp is None or exp > now
        )
        return {"entries": len(self._data), "live": live}


class DiskCache:
    """
    JSON on disk, for payloads that are expensive and must survive restarts.

    Only the ~5 MB player map warrants this. Sleeper's docs are explicit that it
    should be fetched at most once per day and stored locally.
    """

    def __init__(self, directory: Optional[Path] = None) -> None:
        self.dir = Path(directory or config.CACHE_DIR)

    def _path(self, key: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in key)
        return self.dir / f"{safe}.json"

    def get(self, key: str, ttl: Optional[float]) -> Optional[Any]:
        path = self._path(key)
        try:
            if not path.exists():
                return None
            if ttl is not None and (time.time() - path.stat().st_mtime) > ttl:
                return None
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            # A corrupt or unreadable cache file must never be fatal; the
            # caller simply refetches.
            return None

    def set(self, key: str, value: Any) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self._path(key)
            tmp = path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(value, fh)
            tmp.replace(path)  # atomic, so a crash mid write cannot corrupt
        except OSError:
            pass  # disk cache is an optimization, never a requirement


memory = TTLCache()
disk = DiskCache()
